"""The herdr side: read the agents, write the sidebar token.

Everything goes through `HERDR_BIN_PATH` rather than the raw socket. The socket
is a unix path on macOS/Linux and a named pipe on Windows; the CLI is the same
call everywhere, and it is the documented plugin API.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from typing import Any

import config

# A herdr CLI call that hangs would freeze a round. Herdr answers these locally
# in milliseconds, so a few seconds is already pathological.
CALL_TIMEOUT = 10


class Agent:
    """One row of `herdr agent list`, reduced to what pricing needs."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.pane_id: str = str(raw.get("pane_id") or "")
        self.agent: str = str(raw.get("agent") or "")
        self.status: str = str(raw.get("agent_status") or "")
        self.cwd: str | None = raw.get("cwd")
        self.foreground_cwd: str | None = raw.get("foreground_cwd")
        session = raw.get("agent_session")
        self.session: dict[str, Any] | None = session if isinstance(session, dict) else None
        tokens = raw.get("tokens")
        self.tokens: dict[str, str] = tokens if isinstance(tokens, dict) else {}

    @property
    def session_kind(self) -> str | None:
        return self.session.get("kind") if self.session else None

    @property
    def session_value(self) -> str | None:
        value = self.session.get("value") if self.session else None
        return value if isinstance(value, str) and value else None

    @property
    def working(self) -> bool:
        return self.status == "working"


def _run(argv: list[str]) -> dict[str, Any] | None:
    """Run a herdr CLI call and return its `result` object, or None."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",  # not the locale's: pane paths can be anything
            errors="replace",
            timeout=CALL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):  # a bare array or null is still valid JSON
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def plugin_enabled(plugin_id: str) -> bool | None:
    """Whether herdr still has this plugin enabled; None when it didn't answer.

    None is deliberately not False: a herdr that cannot be reached must never be
    read as "you were uninstalled".
    """
    result = _run([config.herdr_bin(), "plugin", "list", "--json", "--plugin", plugin_id])
    if result is None:
        return None
    plugins = result.get("plugins")
    if not isinstance(plugins, list):
        return None
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("plugin_id") == plugin_id:
            return bool(plugin.get("enabled"))
    return False


def agent_list() -> list[Agent] | None:
    """Every agent herdr currently tracks, or None when herdr didn't answer.

    None is not "no agents": the daemon exits after a run of failed calls
    (herdr quit), and an empty list is a perfectly normal session.
    """
    result = _run([config.herdr_bin(), "agent", "list"])
    if result is None:
        return None
    agents = result.get("agents")
    if not isinstance(agents, list):
        return None
    return [Agent(raw) for raw in agents if isinstance(raw, dict)]


def report_token(
    pane_id: str,
    token: str,
    value: str,
    ttl_ms: int | None,
    seq: int,
    source: str = config.SOURCE,
) -> bool:
    """Set one pane metadata token. `seq` lets herdr drop a late round."""
    argv = [
        config.herdr_bin(),
        "pane",
        "report-metadata",
        pane_id,
        "--source",
        source,
        "--token",
        f"{token}={value}",
        "--seq",
        str(seq),
    ]
    if ttl_ms is not None:
        argv += ["--ttl-ms", str(ttl_ms)]
    return _write_metadata(argv, token, source)


def clear_token(pane_id: str, token: str, seq: int, source: str = config.SOURCE) -> bool:
    argv = [
        config.herdr_bin(),
        "pane",
        "report-metadata",
        pane_id,
        "--source",
        source,
        "--clear-token",
        token,
        "--seq",
        str(seq),
    ]
    return _write_metadata(argv, token, source, clearing=True)


def _write_metadata(argv: list[str], token: str, source: str, clearing: bool = False) -> bool:
    if token != "elapsed":
        return _run(argv) is not None
    # `elapsed` may already be a user's price token. Serialize its handoff
    # across the timer, price loop AND manual refresh process. Check current
    # config inside the lock so an in-flight old round cannot reclaim it.
    try:
        directory = config.state_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "elapsed.lock"), "a+", encoding="utf-8") as handle:
            # Never queue behind a wedged CLI call. The next tick retries.
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            try:
                owners = json.load(handle)
            except ValueError:
                owners = {}
            if not isinstance(owners, dict):
                owners = {}
            pane = argv[3]
            cfg = config.load()
            if clearing:
                # Protect an actual replacement, not just an enabled worker:
                # filters/unknown states may leave this pane without a timer.
                # Before timers shipped, only the price source wrote this key.
                owns = owners.get(pane, config.SOURCE) == source
            else:
                owns = (
                    (cfg.token == token)
                    if source == config.SOURCE
                    else (cfg.elapsed and cfg.token != token)
                )
            if not owns:
                # An obsolete clear is already satisfied: the new owner must
                # keep its value. An obsolete report must not be remembered.
                return clearing
            if source != config.SOURCE:
                # This single worker is serialized by the lock; sequencing adds
                # no ordering and survives restarts/clock rollback in Herdr.
                argv = list(argv)
                index = argv.index("--seq")
                del argv[index : index + 2]
            succeeded = _run(argv) is not None
            if clearing and (succeeded or source != config.SOURCE):
                # A failed timer clear can be left to its five-second lease.
                # Do not retain closed panes in the ownership file forever.
                owners.pop(pane, None)
            elif succeeded:
                owners[pane] = source
            handle.seek(0)
            handle.truncate()
            json.dump(owners, handle)
            handle.flush()
            return succeeded
    except OSError:
        return False
