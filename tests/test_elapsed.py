import os
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import config  # noqa: E402
import daemon  # noqa: E402
import elapsed  # noqa: E402
import herdr  # noqa: E402


def agent(status="working", sequence=1, session="s1", pane="w1:p1", **extra):
    return herdr.Agent(
        {
            "pane_id": pane,
            "terminal_id": pane,
            "agent": "opencode",
            "agent_status": status,
            "state_change_seq": sequence,
            "agent_session": {"kind": "id", "value": session},
            **extra,
        }
    )


def cfg(**overrides):
    warnings = []
    return config.Config(config._coerce(overrides, warnings), warnings)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = elapsed.Tracker()

    def value(self, now, **kwargs):
        return self.tracker.values([agent(**kwargs)], now).get("w1:p1")

    def test_starting_mid_state_is_approximate_and_ticks(self):
        self.assertEqual(self.value(100), "~00:00")
        self.assertEqual(self.value(102), "~00:02")

    def test_working_idle_and_blocked_changes_reset(self):
        self.value(100)
        self.assertEqual(self.value(102, status="idle", sequence=5), "00:00")
        self.assertEqual(self.value(105, status="idle", sequence=5), "00:03")
        self.assertEqual(self.value(106, status="blocked", sequence=6), "00:00")
        self.assertEqual(self.value(107, status="working", sequence=7), "00:00")

    def test_viewing_done_output_is_not_a_state_change(self):
        self.value(100)
        self.assertEqual(self.value(101, status="done", sequence=2), "00:00")
        self.assertEqual(self.value(104, status="idle", sequence=2), "00:03")

    def test_sequence_change_in_same_state_detects_a_missed_cycle(self):
        self.value(100)
        self.assertEqual(self.value(102, sequence=4), "~00:00")

    def test_session_switch_and_terminal_replacement_reset(self):
        self.value(100)
        self.assertEqual(self.value(102, session="s2"), "~00:00")
        self.assertEqual(self.value(103, session="s2", terminal_id="new"), "~00:00")

    def test_same_repository_does_not_merge_timers(self):
        self.tracker.values([agent(pane="w1:p1", cwd="/repo")], 100)
        values = self.tracker.values(
            [
                agent(pane="w1:p1", cwd="/repo"),
                agent(pane="w1:p2", cwd="/repo"),
            ],
            102,
        )
        self.assertEqual(values, {"w1:p1": "~00:02", "w1:p2": "~00:00"})

    def test_unknown_and_removed_agents_lose_history(self):
        self.value(100)
        self.assertIsNone(self.value(101, status="unknown"))
        self.assertEqual(self.value(102), "~00:00")
        self.assertEqual(self.tracker.values([], 103), {})
        self.assertEqual(self.tracker.states, {})

    def test_gap_and_clock_reversal_restart_observation(self):
        self.value(100)
        self.assertEqual(self.value(120), "~00:00")
        self.assertEqual(self.value(119), "~00:00")

    def test_hour_format_and_no_rounding_up(self):
        self.value(0)
        self.tracker.last_poll = 3660
        self.assertEqual(self.value(3661.9), "~1:01:01")


class WorkerTests(unittest.TestCase):
    def run_ticks(self, snapshots, configs=None, clear_ok=True):
        stop = threading.Event()
        writes, clears = [], []
        polls = iter(snapshots)
        configurations = iter(configs or [cfg()] * len(snapshots))

        def report(pane, token, value, ttl, seq, source):
            writes.append((pane, token, value, ttl, seq, source))
            return True

        def clear(pane, token, seq, source):
            clears.append((pane, token, source))
            return clear_ok

        ticks = 0

        def wait(_interval):
            nonlocal ticks
            ticks += 1
            if ticks >= len(snapshots):
                stop.set()

        with patch.object(config, "load", side_effect=lambda: next(configurations)), patch.object(
            herdr, "agent_list", side_effect=lambda: next(polls)
        ), patch.object(herdr, "report_token", side_effect=report), patch.object(
            herdr, "clear_token", side_effect=clear
        ), patch.object(stop, "wait", side_effect=wait):
            elapsed.run(stop)
        return writes, clears

    def test_publishes_short_leases_with_separate_source_and_cleans_up(self):
        writes, clears = self.run_ticks([[agent()], [agent()]])
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][1:4], ("elapsed", "~00:00", 5000))
        self.assertEqual(writes[0][5], "opentab-elapsed")
        self.assertGreater(writes[1][4], writes[0][4])
        self.assertEqual(clears, [("w1:p1", "elapsed", "opentab-elapsed")])

    def test_removed_agent_is_cleared(self):
        writes, clears = self.run_ticks([[agent()], []])
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(clears), 1)

    def test_filter_and_disabled_setting_clear_existing_timer(self):
        for setting in ({"elapsed": False}, {"agents": ["claude"]}):
            with self.subTest(setting=setting):
                writes, clears = self.run_ticks([[agent()], [agent()]], [cfg(), cfg(**setting)])
                self.assertEqual(len(writes), 1)
                self.assertEqual(len(clears), 1)

    def test_failed_clear_is_retried(self):
        _, clears = self.run_ticks([[agent()], [], []], clear_ok=False)
        self.assertGreaterEqual(len(clears), 2)

    def test_herdr_failure_does_not_publish_and_recovery_is_approximate(self):
        writes, _ = self.run_ticks([[agent()], None, [agent()]])
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1][2], "~00:00")

    def test_renaming_cost_to_elapsed_retires_timer_through_guarded_cleanup(self):
        writes, clears = self.run_ticks([[agent()], []], [cfg(), cfg(token="elapsed")])
        self.assertEqual(len(writes), 1)
        self.assertEqual(clears, [("w1:p1", "elapsed", "opentab-elapsed")])

    def test_timer_ticks_while_pricing_is_blocked_and_joins_on_exit(self):
        ticks = threading.Event()
        reports = []

        def report(*args, **kwargs):
            reports.append(args)
            if len(reports) >= 2:
                ticks.set()
            return True

        def slow_price(*args):
            self.assertTrue(ticks.wait(3), "timer froze during pricing")
            return True, True

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HERDR_OPENTAB_ONCE": "1"}
        ), patch.object(config, "load", return_value=cfg()), patch.object(
            daemon.signal, "signal"
        ), patch.object(daemon, "round_once", side_effect=slow_price), patch.object(
            elapsed, "INTERVAL", 0.01
        ), patch.object(herdr, "agent_list", return_value=[agent()]), patch.object(
            herdr, "report_token", side_effect=report
        ), patch.object(herdr, "clear_token", return_value=True) as clear:
            self.assertEqual(daemon.run(directory), 0)
            self.assertTrue(clear.called)
        self.assertFalse(any(t.name == "elapsed" for t in threading.enumerate()))


class MetadataTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = patch.object(config, "state_dir", return_value=directory.name)
        state.start()
        self.addCleanup(state.stop)
        settings = patch.object(config, "load", return_value=cfg())
        self.settings = settings.start()
        self.addCleanup(settings.stop)

    def test_old_timer_report_and_cleanup_cannot_replace_new_price(self):
        self.settings.return_value = cfg(token="elapsed")
        with patch.object(herdr, "_run", return_value={}) as call:
            self.assertTrue(herdr.report_token("p", "elapsed", "$1", 180000, 1))
            self.assertFalse(
                herdr.report_token(
                    "p",
                    "elapsed",
                    "~00:00",
                    5000,
                    2,
                    source=elapsed.SOURCE,
                )
            )
            self.assertTrue(herdr.clear_token("p", "elapsed", 3, source=elapsed.SOURCE))
            self.assertEqual(call.call_count, 1)

    def test_old_price_report_and_cleanup_cannot_replace_new_timer(self):
        with patch.object(herdr, "_run", return_value={}) as call:
            self.assertTrue(
                herdr.report_token(
                    "p",
                    "elapsed",
                    "~00:00",
                    5000,
                    1,
                    source=elapsed.SOURCE,
                )
            )
            self.assertFalse(herdr.report_token("p", "elapsed", "$1", 180000, 2))
            self.assertTrue(herdr.clear_token("p", "elapsed", 3))
            self.assertEqual(call.call_count, 1)

    def test_concurrent_writer_skips_instead_of_waiting_then_retries_new_owner(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def pending(argv):
            calls.append(argv)
            entered.set()
            release.wait(3)
            return {}

        with patch.object(herdr, "_run", side_effect=pending):
            timer = threading.Thread(
                target=lambda: herdr.report_token(
                    "p",
                    "elapsed",
                    "~00:00",
                    5000,
                    1,
                    source=elapsed.SOURCE,
                )
            )
            timer.start()
            try:
                self.assertTrue(entered.wait(3))
                self.settings.return_value = cfg(token="elapsed")
                self.assertFalse(herdr.report_token("p", "elapsed", "$1", 180000, 1))
            finally:
                release.set()
                timer.join(3)
            self.assertTrue(herdr.report_token("p", "elapsed", "$1", 180000, 2))
            self.assertFalse(
                herdr.report_token(
                    "p",
                    "elapsed",
                    "~00:00",
                    5000,
                    2,
                    source=elapsed.SOURCE,
                )
            )
            self.assertEqual(len(calls), 2)

    def test_disabling_timer_still_allows_cleanup(self):
        with patch.object(herdr, "_run", return_value={}) as call:
            herdr.report_token("p", "elapsed", "~00:00", 5000, 1, source=elapsed.SOURCE)
            call.reset_mock()
            self.settings.return_value = cfg(elapsed=False)
            self.assertTrue(herdr.clear_token("p", "elapsed", 1, source=elapsed.SOURCE))
            self.assertEqual(call.call_count, 1)

    def test_old_unleased_price_is_cleared_when_no_timer_replaces_it(self):
        self.settings.return_value = cfg(token="cost", elapsed=False, ttl_ms=0)
        reported = {"p": ("$1", 0, None)}
        with patch.object(herdr, "_run", return_value={}) as call:
            daemon.clear_all(reported, "elapsed", 1)
            self.assertEqual(reported, {})
            self.assertEqual(call.call_count, 1)
            self.assertIn("--clear-token", call.call_args[0][0])

    def test_enabled_but_filtered_timer_does_not_prevent_old_price_cleanup(self):
        self.settings.return_value = cfg(token="elapsed", ttl_ms=0)
        with patch.object(herdr, "_run", return_value={}) as call:
            herdr.report_token("p", "elapsed", "$1", None, 1)
            self.settings.return_value = cfg(token="cost", agents=["claude"])
            self.assertTrue(herdr.clear_token("p", "elapsed", 2))
            self.assertEqual(call.call_count, 2)
            self.assertIn("--clear-token", call.call_args[0][0])

    def test_timer_omits_server_sequence_so_clock_rollback_cannot_fake_ownership(self):
        with patch.object(herdr, "_run", return_value={}) as call:
            herdr.report_token("p", "elapsed", "~00:00", 5000, 1, source=elapsed.SOURCE)
            self.assertNotIn("--seq", call.call_args[0][0])
            herdr.clear_token("p", "elapsed", 0, source=elapsed.SOURCE)
            self.assertNotIn("--seq", call.call_args[0][0])

    def test_closed_pane_does_not_accumulate_in_owner_file(self):
        with patch.object(herdr, "_run", return_value={}) as call:
            herdr.report_token("p", "elapsed", "~00:00", 5000, 1, source=elapsed.SOURCE)
            call.return_value = None
            self.assertFalse(herdr.clear_token("p", "elapsed", 2, source=elapsed.SOURCE))
        with open(os.path.join(config.state_dir(), "elapsed.lock"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})

    def test_timer_source_is_distinct_from_default_price_source(self):
        with patch.object(herdr, "_run", return_value={}) as call:
            herdr.report_token("w1:p1", "cost", "$1.00", 1000, 1)
            price_argv = call.call_args[0][0]
            self.assertEqual(price_argv[price_argv.index("--source") + 1], "opentab")
            herdr.report_token("w1:p1", "elapsed", "00:01", 5000, 1, source=elapsed.SOURCE)
            timer_argv = call.call_args[0][0]
            self.assertEqual(timer_argv[timer_argv.index("--source") + 1], elapsed.SOURCE)


if __name__ == "__main__":
    unittest.main()
