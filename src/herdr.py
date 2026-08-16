"""The herdr side: read the agents, write the sidebar token.

Everything goes through `HERDR_BIN_PATH` rather than the raw socket. The socket
is a unix path on macOS/Linux and a named pipe on Windows; the CLI is the same
call everywhere, and it is the documented plugin API.
"""

from __future__ import annotations

import json
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


def report_token(pane_id: str, token: str, value: str, ttl_ms: int | None, seq: int) -> bool:
    """Set one pane metadata token. `seq` lets herdr drop a late round."""
    argv = [
        config.herdr_bin(),
        "pane",
        "report-metadata",
        pane_id,
        "--source",
        config.SOURCE,
        "--token",
        f"{token}={value}",
        "--seq",
        str(seq),
    ]
    if ttl_ms is not None:
        argv += ["--ttl-ms", str(ttl_ms)]
    return _run(argv) is not None


def clear_token(pane_id: str, token: str, seq: int) -> bool:
    argv = [
        config.herdr_bin(),
        "pane",
        "report-metadata",
        pane_id,
        "--source",
        config.SOURCE,
        "--clear-token",
        token,
        "--seq",
        str(seq),
    ]
    return _run(argv) is not None
