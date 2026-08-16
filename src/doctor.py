"""Why isn't a price showing? Answer it in one screen.

Every layer this plugin depends on can fail quietly: opentab may be missing,
herdr may report no session id for an agent, the sidebar layout may never
mention the token, the daemon may have exited. Each gets one line here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import config
import core
import daemon
import herdr
import opentab
import setup_sidebar

OK = "ok"
WARN = "warn"
BAD = "fail"


def line(status: str, message: str) -> None:
    print(f"[{status:>4}] {message}")


def _opentab_version(binary: str) -> tuple[str | None, str]:
    """`(version, why not)`. Nonzero exit is a failure however chatty it was.

    A wrapper script that prints a usage message and exits 1 would otherwise be
    reported as a working opentab, and the user would go looking for the real
    problem everywhere except here.
    """
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15
        )
    except FileNotFoundError:
        return None, "not found on PATH"
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error) or type(error).__name__

    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    first = output.splitlines()[0] if output else ""
    if completed.returncode != 0:
        return None, f"exit {completed.returncode}" + (f": {first}" if first else "")
    return (first or None), "printed nothing"


def main() -> int:
    cfg = config.load()
    failed = False

    print("opentab herdr plugin — doctor\n")
    line(OK, f"config     {config.config_path()}")
    line(OK, f"state      {config.state_dir()}")
    for warning in cfg.warnings:
        line(WARN, f"config     {warning}")

    resolved = shutil.which(cfg.opentab_bin) or cfg.opentab_bin
    version, why = _opentab_version(cfg.opentab_bin)
    if version:
        line(OK, f"opentab    {version} ({resolved})")
    else:
        failed = True
        line(
            BAD,
            f"opentab    cannot run {cfg.opentab_bin!r} ({why}); "
            "put it on PATH or set opentab_bin",
        )

    herdr_bin = config.herdr_bin()
    agents = herdr.agent_list()
    if agents is None:
        failed = True
        line(BAD, f"herdr      {herdr_bin} did not answer `agent list` (is a session running?)")
    else:
        line(OK, f"herdr      {herdr_bin}, {len(agents)} agent(s)")

    pid = daemon.Lock(config.state_dir()).running_pid()
    if pid:
        line(OK, f"daemon     running (pid {pid}), every {cfg.interval_secs}s while working")
    else:
        line(WARN, "daemon     not running; `herdr plugin action invoke opentab.refresh` starts it")

    sidebar_path = setup_sidebar.herdr_config_path()
    try:
        with open(sidebar_path, encoding="utf-8") as handle:
            sidebar = handle.read()
    except OSError:
        sidebar = ""
    if setup_sidebar.mentions_token(sidebar, cfg.token):
        line(OK, f"sidebar    ${cfg.token} is in {sidebar_path}")
    else:
        line(
            WARN,
            f"sidebar    ${cfg.token} is not in {sidebar_path}; "
            "run `herdr plugin action invoke opentab.setup`",
        )

    if agents:
        print("\nagents")
        targets, assignments = core.plan(agents, cfg.project_fallback, cfg.agents)
        table: dict[str, str] = {}
        error = None
        try:
            table = opentab.price(targets, cfg) if targets else {}
        except opentab.BatchUnavailable as exc:
            error = str(exc)
        by_pane = {agent.pane_id: agent for agent in agents}
        for pane_id, target, kind in assignments:
            agent = by_pane.get(pane_id)
            label = agent.agent if agent else "?"
            if kind == "shared":
                detail = (
                    "no price: this project has more than one agent, so its "
                    "directory cannot say which one spent it "
                    "(run `herdr integration install <agent>`)"
                )
            elif not target:
                detail = "no session id reported and the project fallback is off"
            else:
                price = table.get(target)
                short = target if len(target) <= 48 else "…" + target[-47:]
                detail = f"{kind}: {short} → {price if price else '(opentab priced nothing)'}"
            print(f"  {pane_id:<10} {label:<12} {detail}")
        if error:
            failed = True
            print(f"\n  opentab batch failed: {error}")

    log_path = os.path.join(config.state_dir(), "daemon.log")
    if os.path.exists(log_path):
        print(f"\nlast daemon log lines ({log_path})")
        try:
            with open(log_path, encoding="utf-8") as handle:
                for entry in handle.readlines()[-5:]:
                    print(f"  {entry.rstrip()}")
        except OSError:
            pass

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
