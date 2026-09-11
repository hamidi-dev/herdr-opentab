"""Observed time in each pane's current state, independent of transcript reads."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time

import config
import herdr

TOKEN = "elapsed"
# Metadata sequences are per pane/source, not per token. The pricing process
# and manual refresh must not invalidate this worker's writes (or vice versa).
SOURCE = "opentab-elapsed"
INTERVAL = 1.0
LEASE_MS = 5000
SHUTDOWN_TIMEOUT = 2 * herdr.CALL_TIMEOUT + 2


@dataclass
class State:
    identity: tuple
    status: str
    sequence: object
    started: float
    approximate: bool


class Tracker:
    def __init__(self) -> None:
        self.states: dict[str, State] = {}
        self.last_poll: float | None = None

    def values(self, agents: list[herdr.Agent], now: float) -> dict[str, str]:
        # A missed poll, sleep or clock adjustment can hide whole working/idle
        # cycles. Resume observation rather than inventing their history.
        if self.last_poll is not None and not 0 <= now - self.last_poll <= LEASE_MS / 1000:
            self.states.clear()
        self.last_poll = now
        current: dict[str, State] = {}
        values: dict[str, str] = {}
        for agent in agents:
            status = "idle" if agent.status == "done" else agent.status
            if not agent.pane_id or status not in ("working", "idle", "blocked"):
                continue
            identity = (
                agent.raw.get("terminal_id"),
                agent.agent,
                agent.session_kind,
                agent.session_value,
                agent.session.get("source") if agent.session else None,
            )
            sequence = agent.raw.get("state_change_seq")
            previous = self.states.get(agent.pane_id)
            if previous is None or previous.identity != identity:
                state = State(identity, status, sequence, now, True)
            elif previous.status != status or previous.sequence != sequence:
                # A changed sequence with the same status means we missed an
                # intervening state. Global sequence numbers need not be adjacent.
                state = State(identity, status, sequence, now, previous.status == status)
            else:
                state = previous
            current[agent.pane_id] = state
            seconds = max(0, int(now - state.started))
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            duration = f"{minutes:02}:{seconds:02}"
            if hours:
                duration = f"{hours}:{duration}"
            values[agent.pane_id] = ("~" if state.approximate else "") + duration
        self.states = current
        return values


def run(stop: threading.Event) -> None:
    """A lightweight worker in the existing daemon, not another process."""
    tracker = Tracker()
    reported: dict[str, float] = {}
    seq = 0
    try:
        while not stop.is_set():
            cfg = config.load()
            agents = herdr.agent_list() if cfg.elapsed else []
            now = time.time()
            seq = max(seq + 1, time.time_ns() // 1_000_000)
            if agents is None:
                tracker.states.clear()
            else:
                agents = [a for a in agents if cfg.agents is None or a.agent in cfg.agents]
                values = tracker.values(agents, now)
                for pane in list(reported):
                    if stop.is_set():
                        break
                    if pane not in values:
                        if (
                            herdr.clear_token(pane, TOKEN, seq, source=SOURCE)
                            or time.monotonic() - reported[pane] >= LEASE_MS / 1000
                        ):
                            reported.pop(pane, None)
                for pane, value in values.items():
                    if stop.is_set():
                        break
                    if herdr.report_token(pane, TOKEN, value, LEASE_MS, seq, source=SOURCE):
                        reported[pane] = time.monotonic()
            stop.wait(INTERVAL)
    finally:
        # One bounded cleanup pass; a wedged server leaves the rest to their
        # short leases. Check the budget between calls, including many panes.
        deadline = time.monotonic() + 1
        seq = max(seq + 1, time.time_ns() // 1_000_000)
        for pane in reported:
            if time.monotonic() >= deadline:
                break
            herdr.clear_token(pane, TOKEN, seq, source=SOURCE)
