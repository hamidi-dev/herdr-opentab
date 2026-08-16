import io
import os
import time
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)

import config  # noqa: E402
import daemon  # noqa: E402
import herdr  # noqa: E402
import opentab  # noqa: E402
from herdr import Agent  # noqa: E402


def make_config(**overrides):
    values, warnings = dict(config.DEFAULTS), []
    values.update(overrides)
    return config.Config(config._coerce(values, warnings), warnings)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_one_holder_at_a_time(self):
        first = daemon.Lock(self.directory.name)
        second = daemon.Lock(self.directory.name)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_a_lock_file_nobody_holds_is_free(self):
        # The file outlives the daemon; the kernel's lock does not. A leftover
        # file with a dead pid inside must not keep the next daemon out.
        lock = daemon.Lock(self.directory.name)
        with open(lock.path, "w", encoding="utf-8") as handle:
            handle.write("2147483646\n")  # a pid that cannot be running
        self.assertTrue(lock.acquire())
        self.assertEqual(lock.running_pid(), os.getpid())
        lock.release()

    def test_an_empty_lock_file_is_free(self):
        lock = daemon.Lock(self.directory.name)
        open(lock.path, "w").close()
        self.assertTrue(lock.acquire())
        lock.release()

    def test_running_pid_is_none_when_nothing_holds_it(self):
        self.assertIsNone(daemon.Lock(self.directory.name).running_pid())

    def test_another_process_is_locked_out_and_named(self):
        holder = self._holder()
        lock = daemon.Lock(self.directory.name)
        self.assertEqual(lock.running_pid(), holder.pid)
        self.assertFalse(lock.acquire())

    def test_a_killed_holder_leaves_the_lock_free(self):
        # SIGKILL: no cleanup runs, which is exactly the case a pid file has to
        # guess about and the kernel simply knows.
        holder = self._holder()
        holder.kill()
        holder.wait(timeout=5)
        lock = daemon.Lock(self.directory.name)
        self.assertIsNone(lock.running_pid())
        self.assertTrue(lock.acquire())
        lock.release()

    def _holder(self):
        """A real second process holding the lock, cleaned up by the test."""
        script = (
            "import sys, time, daemon;"
            "lock = daemon.Lock(sys.argv[1]);"
            "print(lock.acquire(), flush=True);"
            "time.sleep(60)"
        )
        env = dict(os.environ, PYTHONPATH=SRC)
        holder = subprocess.Popen(
            [sys.executable, "-c", script, self.directory.name],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        self.addCleanup(self._reap, holder)
        self.assertEqual(holder.stdout.readline().strip(), "True")
        return holder

    @staticmethod
    def _reap(holder):
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=5)
        holder.stdout.close()


class StopTests(unittest.TestCase):
    """`--stop` must not report success while the daemon still holds the lock."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def stop(self):
        """`--stop` with its printed output swallowed."""
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return daemon.stop_daemon(self.directory.name)

    def test_stop_waits_for_the_lock_to_be_free(self):
        # A stop that returns early makes `stop && refresh` a coin flip: the
        # spawn sees the dying daemon's lock and declines to start a new one.
        holder = self._holder(handles_sigterm=True)
        self.assertEqual(self.stop(), 0)
        self.assertIsNone(daemon.Lock(self.directory.name).running_pid())
        holder.wait(timeout=5)

    def test_a_daemon_that_ignores_sigterm_is_a_failure(self):
        holder = self._holder(handles_sigterm=False)
        with unittest.mock.patch.object(daemon, "STOP_TIMEOUT", 0.5):
            self.assertEqual(self.stop(), 1)
        self.assertEqual(daemon.Lock(self.directory.name).running_pid(), holder.pid)

    def test_nothing_running_is_not_a_failure(self):
        self.assertEqual(self.stop(), 0)

    def _holder(self, handles_sigterm):
        ignore = "" if handles_sigterm else "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        script = (
            "import signal, sys, time, daemon;"
            f"{ignore}"
            "lock = daemon.Lock(sys.argv[1]);"
            "print(lock.acquire(), flush=True);"
            "time.sleep(60)"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", script, self.directory.name],
            stdout=subprocess.PIPE,
            text=True,
            env=dict(os.environ, PYTHONPATH=SRC),
        )
        self.addCleanup(LockTests._reap, holder)
        self.assertEqual(holder.stdout.readline().strip(), "True")
        return holder


class StoppedFlagTests(unittest.TestCase):
    """`opentab.stop` has to survive the event hook that fires a second later."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_a_stopped_daemon_is_not_respawned_by_an_event(self):
        daemon.set_stopped(self.directory.name, True)
        spawned = []
        with unittest.mock.patch.object(subprocess, "Popen", spawned.append):
            self.assertEqual(daemon.spawn(self.directory.name, foreground_ok=False), 0)
        self.assertEqual(spawned, [])

    def test_a_new_herdr_session_clears_the_flag(self):
        daemon.set_stopped(self.directory.name, True)
        with unittest.mock.patch.object(config, "state_dir", lambda: self.directory.name):
            with unittest.mock.patch.object(daemon, "spawn", lambda _dir: 0):
                daemon.main([])  # the startup hook
        self.assertFalse(daemon.is_stopped(self.directory.name))

    def test_stopping_nothing_still_records_the_intent(self):
        # No daemon is running *yet*: a `stop` before the startup hook must not
        # be forgotten, or the hook starts one the user just said no to.
        with redirect_stdout(io.StringIO()):
            daemon.stop_daemon(self.directory.name)
        self.assertTrue(daemon.is_stopped(self.directory.name))


class ClearRetryTests(unittest.TestCase):
    """A clear that failed is the one thing nothing else will come back to."""

    def setUp(self):
        self.saved = herdr.clear_token
        self.addCleanup(lambda: setattr(herdr, "clear_token", self.saved))

    def test_a_failed_clear_is_kept_for_the_next_round(self):
        herdr.clear_token = lambda pane, token, seq: False
        reported = {"w1:p1": ("$1.00", 100.0, 60_000)}
        daemon._clear("w1:p1", "cost", 1, reported, 110.0)
        self.assertIn("w1:p1", reported)

    def test_it_is_given_up_on_once_the_lease_has_expired(self):
        # By then herdr has taken the value down itself; retrying forever would
        # spend a call every round on a pane that is probably gone.
        herdr.clear_token = lambda pane, token, seq: False
        reported = {"w1:p1": ("$1.00", 100.0, 60_000)}
        daemon._clear("w1:p1", "cost", 1, reported, 161.0)
        self.assertEqual(reported, {})

    def test_the_lease_that_counts_is_the_one_it_was_written_with(self):
        # Reloading a shorter ttl_ms does not shorten an expiry herdr is
        # already holding, so the retry window must not shrink with it.
        herdr.clear_token = lambda pane, token, seq: False
        reported = {"w1:p1": ("$1.00", 100.0, 180_000)}
        daemon._clear("w1:p1", "cost", 1, reported, 161.0)
        self.assertIn("w1:p1", reported)

    def test_without_a_lease_it_is_retried_indefinitely(self):
        # `ttl_ms: 0` means nothing else will ever remove it.
        herdr.clear_token = lambda pane, token, seq: False
        reported = {"w1:p1": ("$1.00", 0.0, None)}
        daemon._clear("w1:p1", "cost", 1, reported, 1e9)
        self.assertIn("w1:p1", reported)

    def test_a_successful_clear_forgets_the_pane(self):
        herdr.clear_token = lambda pane, token, seq: True
        reported = {"w1:p1": ("$1.00", 100.0, 60_000)}
        daemon._clear("w1:p1", "cost", 1, reported, 110.0)
        self.assertEqual(reported, {})

    def test_shutdown_retries_a_failed_clear_within_its_budget(self):
        # With ttl_ms: 0 there is no lease to expire the value, so a single
        # failed call at shutdown would strand a price on screen for good.
        calls = []

        def flaky(pane, token, seq):
            calls.append(pane)
            return len(calls) > 2

        herdr.clear_token = flaky
        reported = {"w1:p1": ("$1.00", 0.0, None)}
        with unittest.mock.patch.object(daemon, "SHUTDOWN_BUDGET", 1.0):
            daemon.clear_all(reported, "cost", 1)
        self.assertEqual(reported, {})
        self.assertGreater(len(calls), 1)

    def test_shutdown_gives_up_rather_than_hanging(self):
        herdr.clear_token = lambda pane, token, seq: False
        reported = {"w1:p1": ("$1.00", 0.0, None)}
        started = time.monotonic()
        with unittest.mock.patch.object(daemon, "SHUTDOWN_BUDGET", 0.3):
            daemon.clear_all(reported, "cost", 1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("w1:p1", reported)


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_touching_the_wake_file_changes_the_stamp(self):
        before = daemon._wake_stamp(self.directory.name)
        daemon.touch_wake(self.directory.name)
        self.assertNotEqual(daemon._wake_stamp(self.directory.name), before)


class RoundTests(unittest.TestCase):
    """`round_once` against stubbed herdr and opentab modules."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.reported = []
        self.cleared = []
        self.saved = (herdr.agent_list, herdr.report_token, herdr.clear_token, opentab.price)

        def restore():
            (herdr.agent_list, herdr.report_token, herdr.clear_token, opentab.price) = self.saved

        self.addCleanup(restore)
        herdr.report_token = lambda pane, token, value, ttl, seq: (
            self.reported.append((pane, token, value, ttl)) or True
        )
        herdr.clear_token = lambda pane, token, seq: self.cleared.append((pane, token)) or True

    def stub_agents(self, agents):
        herdr.agent_list = lambda: agents

    def test_a_price_reaches_the_pane(self):
        self.stub_agents([Agent({"pane_id": "w1:p1", "agent": "claude", "cwd": "/p"})])
        opentab.price = lambda targets, cfg: {"/p": "~$4.20"}
        cfg = make_config(fallback="project")
        answered, working = daemon.round_once(cfg, self.directory.name, 1)
        self.assertTrue(answered)
        self.assertFalse(working)
        self.assertEqual(self.reported, [("w1:p1", "cost", "~$4.20", cfg.lease_ms)])

    def test_a_failed_batch_reports_nothing_at_all(self):
        self.stub_agents(
            [Agent({"pane_id": "w1:p1", "agent": "claude", "cwd": "/p", "tokens": {"cost": "~$1"}})]
        )

        def fail(targets, cfg):
            raise opentab.BatchUnavailable("exit 1")

        opentab.price = fail
        answered, _ = daemon.round_once(make_config(fallback="project"), self.directory.name, 1)
        self.assertTrue(answered)
        self.assertEqual((self.reported, self.cleared), ([], []))

    def test_a_working_agent_selects_the_fast_interval(self):
        self.stub_agents(
            [Agent({"pane_id": "w1:p1", "agent": "codex", "agent_status": "working", "cwd": "/p"})]
        )
        opentab.price = lambda targets, cfg: {}
        _, working = daemon.round_once(make_config(), self.directory.name, 1)
        self.assertTrue(working)

    def test_a_silent_herdr_is_not_an_empty_session(self):
        herdr.agent_list = lambda: None
        answered, _ = daemon.round_once(make_config(), self.directory.name, 1)
        self.assertFalse(answered)

    def test_no_agents_means_no_opentab_call(self):
        self.stub_agents([])

        def explode(targets, cfg):
            raise AssertionError("opentab must not run without agents")

        opentab.price = explode
        answered, _ = daemon.round_once(make_config(), self.directory.name, 1)
        self.assertTrue(answered)


if __name__ == "__main__":
    unittest.main()
