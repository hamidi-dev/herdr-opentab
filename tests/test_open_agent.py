import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import open_agent  # noqa: E402
import config  # noqa: E402
from herdr import Agent  # noqa: E402


def agent(pane="w1:p1", session=None):
    return Agent(
        {"pane_id": pane, "agent": "codex", "cwd": "/same/repo", "agent_session": session}
    )


class OpenAgentTests(unittest.TestCase):
    def test_context_never_guesses_from_cwd_or_non_pane_context(self):
        with self.assertRaisesRegex(ValueError, "Focus an agent pane"):
            open_agent.focused_pane_id(json.dumps({"workspace_cwd": "/same/repo"}))
        with self.assertRaisesRegex(ValueError, "valid pane context"):
            open_agent.focused_pane_id("not json")

    def test_exact_session_wins_even_when_other_agent_shares_project(self):
        agents = [
            agent(session={"kind": "path", "value": "/logs/rollout-2026-08-16-"
                   "0199c0de-1234-4abc-8def-000000000005.jsonl"}),
            agent(pane="w1:p2", session={"kind": "id", "value": "ses_other123456"}),
        ]
        self.assertEqual(
            open_agent.session_for_pane("w1:p1", agents),
            "0199c0de-1234-4abc-8def-000000000005",
        )
        with self.assertRaisesRegex(ValueError, "no detected agent"):
            open_agent.session_for_pane("w1:p3", agents)

    def test_no_session_never_opens_latest_project_even_when_fallback_enabled(self):
        with self.assertRaisesRegex(ValueError, "official Herdr integration"):
            open_agent.session_for_pane("w1:p1", [agent()])

    @patch("open_agent.subprocess.run")
    @patch("open_agent.shutil.which", return_value="/bin/opentab")
    @patch("open_agent.herdr.agent_list")
    @patch("open_agent.config.load")
    def test_action_opens_configured_placement_for_focused_session_only(self, load, agent_list, _which, run):
        agent_list.return_value = [
            agent(pane="w1:p2", session={"kind": "id", "value": "ses_wrong123456"}),
            agent(session={"kind": "id", "value": "ses_right123456"}),
        ]
        run.return_value = SimpleNamespace(returncode=0, stderr="", stdout="")
        for placement in ("overlay", "popup", "split", "tab", "zoomed"):
            for direction in ("right", "down"):
                with self.subTest(placement=placement, direction=direction):
                    load.return_value = config.Config({
                        **config.DEFAULTS, "opentab_bin": "/bin/opentab", "opentab_args": ["--demo"],
                        "open_placement": placement, "open_direction": direction,
                    }, [])
                    run.reset_mock()
                    with patch.dict(os.environ, {
                        "HERDR_PLUGIN_CONTEXT_JSON": '{"focused_pane_id":"w1:p1"}',
                        "HERDR_BIN_PATH": "/bin/herdr", "HERDR_SOCKET_PATH": "/test.sock",
                    }):
                        open_agent.open_agent()
                    self.assertEqual(run.call_count, 1)  # no catalog preflight before the TUI
                    expected = [
                        "/bin/herdr", "plugin", "pane", "open", "--plugin", "opentab",
                        "--entrypoint", "session", "--placement", placement, "--focus",
                        "--env", "OPENTAB_OPEN_SESSION=ses_right123456",
                        "--env", "OPENTAB_OPEN_PANE=w1:p1",
                    ]
                    if placement in ("split", "zoomed"):
                        expected += ["--target-pane", "w1:p1", "--direction", direction]
                    self.assertEqual(run.call_args.args[0], expected)

    @patch("open_agent.subprocess.run")
    @patch("open_agent.shutil.which", return_value="/bin/opentab")
    @patch("open_agent.herdr.agent_list")
    @patch("open_agent.config.load")
    def test_unsupported_placement_reports_herdr_error(self, load, agent_list, _which, run):
        load.return_value = config.Config({**config.DEFAULTS, "open_placement": "popup"}, [])
        agent_list.return_value = [agent(session={"kind": "id", "value": "ses_right123456"})]
        run.return_value = SimpleNamespace(returncode=2, stderr="invalid placement: popup", stdout="")
        with patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": '{"focused_pane_id":"w1:p1"}'}):
            with self.assertRaisesRegex(ValueError, r"OpenTab \(popup\): invalid placement: popup"):
                open_agent.open_agent()

    @patch("open_agent.os.execvp")
    @patch("open_agent.config.load")
    def test_overlay_execs_tui_with_session_as_separate_argument(self, load, execvp):
        load.return_value = SimpleNamespace(opentab_bin="/bin/opentab")
        with patch.dict(os.environ, {
            "OPENTAB_OPEN_SESSION": "ses_right123456", "OPENTAB_OPEN_PANE": "w1:p1",
            "HERDR_PLUGIN_CONTEXT_JSON": '{"focused_pane_id":"w1:p1"}',
        }):
            open_agent.run_tui()
        execvp.assert_called_once_with(
            "/bin/opentab", ["/bin/opentab", "--goto", "ses_right123456"]
        )

    @patch("open_agent.os.execvp")
    def test_focus_change_does_not_launch_stale_target(self, execvp):
        with patch.dict(os.environ, {
            "OPENTAB_OPEN_SESSION": "ses_right123456", "OPENTAB_OPEN_PANE": "w1:p1",
            "HERDR_PLUGIN_CONTEXT_JSON": '{"focused_pane_id":"w1:p2"}',
        }):
            with self.assertRaisesRegex(ValueError, "Pane focus changed"):
                open_agent.run_tui()
        execvp.assert_not_called()

    @patch("open_agent.subprocess.run")
    @patch("open_agent.open_agent", side_effect=ValueError("No native session ID"))
    def test_detached_action_errors_notify_and_survive_notification_failure(self, _open, run):
        run.side_effect = OSError("server unavailable")
        with patch.object(sys, "argv", ["open_agent.py"]), patch("sys.stderr"):
            self.assertEqual(open_agent.main(), 1)
        self.assertIn("No native session ID", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
