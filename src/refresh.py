"""Nudge the daemon (event hooks) or price once right now (the action).

Event hooks fire on every agent detection and status change, so they must stay
cheap: this writes a wake file and, if no daemon holds the lock, starts one.
That second part is what makes `herdr plugin link` work without a restart --
linking a plugin does not run its startup hook.
"""

from __future__ import annotations

import argparse
import sys
import time

import config
import core
import daemon
import herdr
import opentab


def refresh_now(cfg: config.Config) -> int:
    """One synchronous round, so an explicitly asked-for refresh is visible."""
    agents = herdr.agent_list()
    if agents is None:
        print("opentab: herdr did not answer `agent list`", file=sys.stderr)
        return 1
    targets, assignments = core.plan(agents, cfg.project_fallback, cfg.agents)
    if not assignments:
        print("opentab: no agents to price")
        return 0
    try:
        table = opentab.price(targets, cfg) if targets else {}
    except opentab.BatchUnavailable as error:
        print(f"opentab: {error} — keeping the prices herdr already shows", file=sys.stderr)
        return 1
    if cfg.align:
        table = core.align_table(table)

    seq = int(time.time() * 1000)
    changed = 0
    for pane_id, value in core.updates(agents, assignments, table, cfg.token):
        ok = (
            herdr.clear_token(pane_id, cfg.token, seq)
            if value is None
            # cfg.lease_ms, never the raw cfg.ttl_ms: herdr replaces a token's
            # whole expiry on every write, so reporting the default (null, "no
            # ttl at all") here would strip the lease the daemon is renewing and
            # leave a permanent number behind if the daemon then died.
            else herdr.report_token(pane_id, cfg.token, value, cfg.lease_ms, seq)
        )
        changed += 1 if ok else 0
    priced = sum(1 for _, target, _ in assignments if target and target in table)
    print(f"opentab: priced {priced}/{len(assignments)} agents, updated {changed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh OpenTab prices in the herdr sidebar")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--event", action="store_true", help="herdr event hook: wake the daemon")
    group.add_argument("--now", action="store_true", help="price every agent in this process")
    args = parser.parse_args(argv)

    state_dir = config.state_dir()
    if not args.event:
        # Asking for a price out loud undoes `opentab.stop`; an event hook does
        # not, or the daemon would be back a second after the user stopped it.
        daemon.set_stopped(state_dir, False)
    daemon.spawn(state_dir, foreground_ok=False)
    daemon.touch_wake(state_dir)
    if args.event:
        return 0
    return refresh_now(config.load())


if __name__ == "__main__":
    sys.exit(main())
