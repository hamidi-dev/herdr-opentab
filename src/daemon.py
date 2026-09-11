"""The price daemon: one loop, one opentab call per round, N herdr writes.

Herdr's startup hook is one-shot ("not supervised daemons"), so `--startup`
spawns this file again, detached, and returns immediately. Only one daemon may
run, which an advisory `flock` on a file in the state directory enforces -- the
kernel releases it however the holder dies, so there is no stale lock to
reclaim.

Run it in the foreground to watch it work:

    HERDR_OPENTAB_FOREGROUND=1 python3 src/daemon.py --run
"""

from __future__ import annotations

import argparse
import fcntl
import io
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

import config
import core
import elapsed
import herdr
import opentab

# Consecutive failed `herdr agent list` calls before the daemon gives up. Herdr
# answers locally, so a run this long means the server is gone -- during a live
# handoff it is back within a second or two, and the new server's startup hook
# would restart us anyway.
MAX_HERDR_FAILURES = 10

# How often to ask herdr whether this plugin is still enabled. Cheap enough to
# be unnoticeable, frequent enough that a disabled plugin stops publishing
# before anyone wonders why the sidebar still moves.
REVOKE_CHECK_SECS = 60

# The loop sleeps in slices so a wake file written by an event hook is noticed
# within one slice instead of at the end of the interval.
SLICE = 0.5

# How long the final cleanup may spend taking prices down. One herdr call per
# priced pane, and a wedged server makes each of them cost CALL_TIMEOUT, so
# without a budget a busy session's shutdown outlasts anything `--stop` could
# reasonably wait for. Whatever is left uncleared is left to its lease --
# which is exactly what the lease is for.
SHUTDOWN_BUDGET = 5.0

# How long `--stop` waits for the daemon to actually let go of the lock. SIGTERM
# only sets a flag -- the loop finishes what it is doing first -- so this has to
# outlast the longest thing a round can be stuck in: one opentab batch
# (BATCH_TIMEOUT), the herdr call it may be inside, and the bounded shutdown
# that follows. Bounded is the point: without SHUTDOWN_BUDGET the cleanup is one
# call per priced pane and no constant here could cover it.
STOP_TIMEOUT = (
    opentab.BATCH_TIMEOUT + herdr.CALL_TIMEOUT + SHUTDOWN_BUDGET + elapsed.SHUTDOWN_TIMEOUT + 5.0
)

# How long to wait before telling the user this is going to take a moment.
STOP_QUIET = 3.0

LOG_LIMIT = 256 * 1024


# What this daemon wrote for a pane: the value, when it was written, and the
# lease it was written with -- which is not necessarily the lease the config
# names now, and only the written one says when herdr will drop the value.
Reported = Tuple[str, float, Optional[int]]


class Lock:
    """One daemon at a time, enforced by the kernel.

    `flock` rather than a pid file, for two reasons a pid file cannot cover:
    the kernel drops the lock when the holder dies however it dies, so there is
    no stale state to reclaim and no window between "I made the lock" and "I
    wrote my pid into it" for a second daemon to slip through; and the pid
    inside is only ever read while the lock is *held*, so it cannot name a
    process that died and had its pid handed to something else.

    BSD `flock` is per open file description, so the extra handle `running_pid`
    opens cannot release the daemon's own lock (that is the POSIX-record-lock
    trap, not this one). macOS and Linux both give us BSD semantics; Windows is
    out of scope for this plugin anyway.
    """

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "daemon.lock")
        self.handle: "io.TextIOWrapper | None" = None

    @property
    def held(self) -> bool:
        return self.handle is not None

    def _open(self, create: bool) -> "io.TextIOWrapper | None":
        try:
            if create:
                os.makedirs(self.state_dir, exist_ok=True)
            return open(self.path, "a+" if create else "r", encoding="utf-8")
        except OSError:
            return None

    def acquire(self) -> bool:
        handle = self._open(create=True)
        if handle is None:
            return False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        except OSError:
            pass
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None

    def running_pid(self) -> int | None:
        """The pid of the daemon holding the lock, or None if nobody holds it."""
        if self.held:
            return os.getpid()
        handle = self._open(create=False)
        if handle is None:
            return None
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Someone holds it, so the pid recorded inside is alive.
                return _read_pid(handle)
            fcntl.flock(handle, fcntl.LOCK_UN)
            return None
        finally:
            handle.close()


def _read_pid(handle: "io.TextIOWrapper") -> int | None:
    try:
        handle.seek(0)
        return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def wake_path(state_dir: str) -> str:
    return os.path.join(state_dir, "wake")


def stopped_path(state_dir: str) -> str:
    return os.path.join(state_dir, "stopped")


def set_stopped(state_dir: str, stopped: bool) -> None:
    """Remember that the user stopped the daemon on purpose.

    Without this, `opentab.stop` lasts until the next agent event: the hooks
    revive the daemon within a second, which is the right behaviour for a daemon
    that died and the wrong one for a daemon somebody switched off. The flag is
    cleared by the startup hook (a new herdr session starts fresh) and by an
    explicit `refresh`, which is the user asking for a price in so many words.
    """
    try:
        if stopped:
            os.makedirs(state_dir, exist_ok=True)
            with open(stopped_path(state_dir), "w", encoding="utf-8") as handle:
                handle.write(f"{time.time()}\n")
        else:
            os.remove(stopped_path(state_dir))
    except OSError:
        pass


def is_stopped(state_dir: str) -> bool:
    return os.path.exists(stopped_path(state_dir))


def touch_wake(state_dir: str) -> None:
    """Ask a running daemon to start its next round now."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(wake_path(state_dir), "w", encoding="utf-8") as handle:
            handle.write(f"{time.time()}\n")
    except OSError:
        pass


def _wake_stamp(state_dir: str) -> float:
    try:
        return os.stat(wake_path(state_dir)).st_mtime
    except OSError:
        return 0.0


def log(state_dir: str, message: str) -> None:
    """Append one line to the plugin's own log.

    Herdr logs a plugin *command's* completion, which for a detached daemon is
    only ever "the spawner exited fine" -- so the loop keeps its own record.
    """
    path = os.path.join(state_dir, "daemon.log")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        os.makedirs(state_dir, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def round_once(
    cfg: config.Config,
    state_dir: str,
    seq: int,
    reported: dict[str, Reported] | None = None,
) -> tuple[bool, bool]:
    """One pricing round. Returns `(herdr answered, any agent working)`.

    `reported` is this daemon's memory of what it wrote, kept so leases can be
    renewed and abandoned panes cleaned up; it is updated in place.
    """
    reported = {} if reported is None else reported
    agents = herdr.agent_list()
    if agents is None:
        return False, False

    targets, assignments = core.plan(agents, cfg.project_fallback, cfg.agents)
    working = any(agent.working for agent in agents)
    if not assignments and not reported:
        return True, working

    if targets:
        try:
            table = opentab.price(targets, cfg)
        except opentab.BatchUnavailable as error:
            # Deliberately not a blank: an incomplete answer must not erase a
            # correct one. Herdr keeps whatever it is already showing.
            log(state_dir, f"opentab unavailable ({error}); keeping previous prices")
            return True, working
    else:
        table = {}
    if cfg.align:
        table = core.align_table(table)

    now = time.monotonic()
    for pane_id, value in core.updates(
        agents, assignments, table, cfg.token, reported, cfg.renew_after_secs, now
    ):
        if value is None:
            _clear(pane_id, cfg.token, seq, reported, now)
        elif herdr.report_token(pane_id, cfg.token, value, cfg.lease_ms, seq):
            reported[pane_id] = (value, now, cfg.lease_ms)
        # A rejected write does not revoke the previous value or its lease.
        # Keep that record for renewal and cleanup, including a token rename
        # while this pricing batch was running.
    return True, working


def _clear(
    pane_id: str,
    token: str,
    seq: int,
    reported: dict[str, Reported],
    now: float,
) -> None:
    """Clear one pane's token, keeping it on the books until that succeeds.

    A failed clear is the one case where forgetting the pane is wrong: it has
    already left the plan, so nothing else will ever revisit it, and the price
    would stay on screen until its lease ran out -- forever, with `ttl_ms: 0`.
    So it is retried on the following rounds, and given up on only once the
    lease it was written with has expired, because at that point herdr has taken
    the value down anyway and there is nothing left to clear.
    """
    if herdr.clear_token(pane_id, token, seq):
        reported.pop(pane_id, None)
        return
    entry = reported.get(pane_id)
    if entry is None:
        return
    # The lease this value was *written* with, not whatever the config says
    # now: reloading a shorter one does not shorten an expiry herdr already
    # holds, and reloading `ttl_ms: 0` does not make an expired one immortal.
    _, when, lease_ms = entry
    if lease_ms is not None and now - when >= lease_ms / 1000:
        reported.pop(pane_id, None)


def clear_all(
    reported: dict[str, Reported],
    token: str,
    seq: int,
    state_dir: str | None = None,
) -> None:
    """Take this daemon's prices down before it stops maintaining them.

    Retried within a budget rather than attempted once: with `ttl_ms: 0` there
    is no lease to expire the value, so a herdr call that times out here would
    strand a price on screen forever. Bounded because the caller is on its way
    out -- `--stop` is waiting on this, and one call per pane against a wedged
    server adds up faster than anyone will wait.
    """
    deadline = time.monotonic() + SHUTDOWN_BUDGET
    while reported:
        for pane_id in list(reported):
            if herdr.clear_token(pane_id, token, seq):
                reported.pop(pane_id, None)
        if not reported or time.monotonic() >= deadline:
            break
        time.sleep(SLICE / 10)
    if reported and state_dir:
        log(state_dir, f"could not clear {len(reported)} pane(s); leaving them to the lease")


def revoked(state_dir: str) -> bool:
    """Whether herdr has disabled or removed this plugin under us.

    Disabling a plugin does not stop a process its startup hook detached
    (`herdr@0.7.5:src/app/api/plugins/mod.rs`), and nothing clears the tokens it
    reported, so a daemon that never checks would keep publishing prices for a
    plugin the user switched off.
    """
    root = os.environ.get("HERDR_PLUGIN_ROOT")
    if root and not os.path.isdir(root):
        log(state_dir, "plugin root is gone; exiting")
        return True
    enabled = herdr.plugin_enabled(config.SOURCE)
    if enabled is False:
        log(state_dir, "plugin disabled; clearing prices and exiting")
        return True
    return False


def run(state_dir: str) -> int:
    lock = Lock(state_dir)
    if not lock.acquire():
        return 0

    stop = {"now": False}
    timer_stop = threading.Event()

    def handle(_signum, _frame):  # noqa: ANN001 -- signal handler signature
        stop["now"] = True
        timer_stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGHUP, handle)

    cfg = config.load()
    for warning in cfg.warnings:
        log(state_dir, f"config: {warning}")
    log(state_dir, f"daemon start pid={os.getpid()} interval={cfg.interval_secs}s")

    failures = 0
    seen_wake = _wake_stamp(state_dir)
    once = os.environ.get("HERDR_OPENTAB_ONCE") == "1"
    reported: dict[str, Reported] = {}
    token = cfg.token
    next_check = time.monotonic() + REVOKE_CHECK_SECS
    timer = threading.Thread(target=elapsed.run, args=(timer_stop,), name="elapsed", daemon=True)
    timer.start()
    try:
        while not stop["now"]:
            # Reloaded every round: editing config.json should take effect
            # without hunting down a pid.
            cfg = config.load()
            if cfg.token != token:
                # Renaming the token strands every value written under the old
                # name, and only this process still knows what that name was.
                clear_all(reported, token, int(time.time() * 1000), state_dir)
                token = cfg.token
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + REVOKE_CHECK_SECS
                if revoked(state_dir):
                    clear_all(reported, token, int(time.time() * 1000), state_dir)
                    break
            answered, working = round_once(cfg, state_dir, int(time.time() * 1000), reported)
            if answered:
                failures = 0
            else:
                failures += 1
                if failures >= MAX_HERDR_FAILURES:
                    log(state_dir, "herdr stopped answering; exiting")
                    break
            if once:
                break

            interval = cfg.interval_secs if working else cfg.idle_interval_secs
            deadline = time.monotonic() + interval
            while not stop["now"] and time.monotonic() < deadline:
                time.sleep(min(SLICE, max(0.0, deadline - time.monotonic())))
                stamp = _wake_stamp(state_dir)
                if stamp != seen_wake:
                    seen_wake = stamp
                    break
    finally:
        timer_stop.set()
        timer.join(timeout=elapsed.SHUTDOWN_TIMEOUT)
        # Prices this daemon will no longer renew must not outlive it: herdr
        # keeps a reported token in the server, where it would sit looking
        # current forever.
        clear_all(reported, token, int(time.time() * 1000), state_dir)
        log(state_dir, "daemon stop")
        lock.release()
    return 0


def spawn(state_dir: str, foreground_ok: bool = True) -> int:
    """Start the loop detached, unless one already holds the lock.

    `foreground_ok` is false for callers that must return promptly whatever the
    debugging env says -- an event hook that ran the loop inline would hold a
    herdr plugin command open forever.
    """
    if Lock(state_dir).running_pid() or is_stopped(state_dir):
        return 0
    if foreground_ok and os.environ.get("HERDR_OPENTAB_FOREGROUND") == "1":
        return run(state_dir)
    argv = [sys.executable or "python3", os.path.abspath(__file__), "--run"]
    try:
        os.makedirs(state_dir, exist_ok=True)
        subprocess.Popen(  # noqa: S603 -- our own file, absolute path
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
    except OSError as error:
        log(state_dir, f"could not spawn daemon: {error}")
        return 1
    return 0


def stop_daemon(state_dir: str) -> int:
    lock = Lock(state_dir)
    pid = lock.running_pid()
    # Set first: an agent event arriving during the shutdown below would
    # otherwise start a fresh daemon before this one has finished exiting.
    set_stopped(state_dir, True)
    if not pid:
        print("opentab: no price daemon running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        print(f"opentab: could not stop pid {pid}: {error}", file=sys.stderr)
        return 1

    # Wait for the kernel to drop the lock, not merely for the signal to be
    # delivered. `stop` and then `refresh` is the obvious way to restart this
    # daemon, and a spawn that runs while the old process is still taking its
    # prices down finds the lock held and quietly does nothing at all -- leaving
    # the user with no daemon and a command that said it succeeded.
    started = time.monotonic()
    warned = False
    while lock.running_pid() == pid:
        waited = time.monotonic() - started
        if waited >= STOP_TIMEOUT:
            print(
                f"opentab: pid {pid} did not stop within {STOP_TIMEOUT:g}s",
                file=sys.stderr,
            )
            return 1
        if waited >= STOP_QUIET and not warned:
            warned = True
            print(f"opentab: waiting for pid {pid} to finish its round…")
        time.sleep(SLICE / 10)
    # Written again, deliberately: a herdr live handoff runs the startup hook,
    # which clears this flag, and it can land in the middle of the wait above --
    # leaving neither a daemon nor a record that the user asked for one to stop.
    set_stopped(state_dir, True)
    print(f"opentab: stopped price daemon (pid {pid})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenTab price daemon for herdr")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--startup", action="store_true", help="spawn the daemon and exit")
    group.add_argument("--run", action="store_true", help="run the loop in this process")
    group.add_argument("--stop", action="store_true", help="stop a running daemon")
    group.add_argument("--status", action="store_true", help="print the daemon's pid, if any")
    args = parser.parse_args(argv)

    state_dir = config.state_dir()
    if args.run:
        return run(state_dir)
    if args.stop:
        return stop_daemon(state_dir)
    if args.status:
        pid = Lock(state_dir).running_pid()
        stopped = " (stopped by the user)" if is_stopped(state_dir) else ""
        print(f"running (pid {pid})" if pid else f"not running{stopped}")
        return 0
    # --startup, i.e. a new herdr session: that is exactly how long `opentab.stop`
    # was documented to last, so the flag it left behind is cleared here.
    set_stopped(state_dir, False)
    return spawn(state_dir)


if __name__ == "__main__":
    sys.exit(main())
