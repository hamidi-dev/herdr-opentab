"""Open the focused Herdr agent's native session in the configured placement."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import config
import core
import herdr


def focused_pane_id(context_json: str | None) -> str:
    try:
        context = json.loads(context_json or "{}")
    except ValueError as error:
        raise ValueError("Herdr did not supply a valid pane context") from error
    pane_id = context.get("focused_pane_id") if isinstance(context, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        raise ValueError("Focus an agent pane before invoking opentab.open")
    return pane_id


def session_for_pane(pane_id: str, agents: list[herdr.Agent]) -> str:
    agent = next((item for item in agents if item.pane_id == pane_id), None)
    if agent is None or not agent.agent:
        raise ValueError("The focused pane has no detected agent")
    if agent.session_kind not in ("id", "path"):
        raise ValueError(
            "No native session ID for this pane; install the official Herdr integration "
            f"for {agent.agent} and start or resume the agent"
        )
    session, kind = core.target_for(agent, project_fallback=False)
    if kind != "session" or not session:
        raise ValueError(
            "No native session ID for this pane; install the official Herdr integration "
            f"for {agent.agent} and start or resume the agent"
        )
    return session


def open_agent() -> None:
    pane_id = focused_pane_id(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON"))
    agents = herdr.agent_list()
    if agents is None:
        raise ValueError("Could not read Herdr agents")
    session = session_for_pane(pane_id, agents)
    cfg = config.load()
    binary = cfg.opentab_bin
    if shutil.which(binary) is None:
        raise ValueError(f"OpenTab executable {binary!r} was not found")
    # Let --goto resolve subagent IDs and handle unavailable transcripts. A
    # separate catalog query duplicates startup work and requires newer OpenTab.
    # Pass the resolved ID before opening: once OpenTab takes focus the
    # pane context points at OpenTab itself. Neither cwd nor cost-only args are
    # suitable for opening the TUI. --env is one argv element, never shell code.
    argv = [
        config.herdr_bin(), "plugin", "pane", "open", "--plugin", "opentab",
        "--entrypoint", "session", "--placement", cfg.open_placement, "--focus",
        "--env", f"OPENTAB_OPEN_SESSION={session}",
        "--env", f"OPENTAB_OPEN_PANE={pane_id}",
    ]
    if cfg.open_placement in ("split", "zoomed"):
        argv += ["--target-pane", pane_id, "--direction", cfg.open_direction]
    result = subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10
    )
    if result.returncode:
        raise ValueError(
            f"Could not open OpenTab ({cfg.open_placement}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def run_tui() -> None:
    # Herdr supplies pre-launch context: the explicit target for split/zoomed,
    # or the active pane for overlay/popup/tab. Reject a focus race in the latter
    # modes rather than showing a session from a different pane or workspace.
    origin = os.environ.get("OPENTAB_OPEN_PANE")
    if not origin or focused_pane_id(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")) != origin:
        raise ValueError("Pane focus changed; invoke opentab.open again from the agent pane")
    session = os.environ.get("OPENTAB_OPEN_SESSION", "")
    if not core.is_usable_session_id(session):
        raise ValueError("OpenTab pane is missing a valid session ID")
    binary = config.load().opentab_bin
    os.execvp(binary, [binary, "--goto", session])


def main() -> int:
    try:
        if sys.argv[1:] == ["--run"]:
            run_tui()
        elif not sys.argv[1:]:
            open_agent()
        else:
            raise ValueError("Unknown open_agent arguments")
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"opentab.open: {error}", file=sys.stderr)
        # Actions run detached; stderr is recorded in plugin logs, not the pane.
        # Keep the original error even if Herdr itself is unreachable.
        try:
            subprocess.run(
                [config.herdr_bin(), "notification", "show", "OpenTab",
                 "--body", str(error), "--sound", "none"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
