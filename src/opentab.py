"""The opentab side: one process prices every target.

`opentab cost --batch -` reads targets from stdin (one per line) and prints
`<target>\\t<price>`, in the order asked, omitting whatever it could not price.
Batching is the whole reason this plugin has a daemon instead of a per-pane
worker: opentab's interpreter start dwarfs the pricing, and a batch that shares
one session between two panes parses that session once.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from typing import Iterable

import config

# Long enough for a cold run over a large corpus, short enough that a wedged
# opentab cannot stall the loop for long. Workmux's sidebar allows the same
# command 10s on a 30s interval; twice that here because a round lost to a
# timeout is the whole interval's worth of staleness (60s while idle), and a
# cold opentab is slow exactly once.
BATCH_TIMEOUT = 20

# How often to look in on the child while waiting for it.
POLL = 0.05

# Most output one batch may contribute. A row echoes its target and adds a
# price, so the bound is stated relative to what was asked -- a fixed ceiling
# would refuse a legitimate answer from a session with very many panes, and
# refusing every round leaves the sidebar blank for good. The floor is a
# megabyte, which is orders of magnitude more than any real table.
MIN_OUTPUT = 1024 * 1024
OUTPUT_FACTOR = 4


class BatchUnavailable(Exception):
    """opentab could not produce a table this round. Keep the previous one."""


def price(targets: Iterable[str], cfg: config.Config) -> dict[str, str]:
    """Price every target in one call: `{target: price}`.

    Raises BatchUnavailable when opentab is missing, times out, or exits
    nonzero. A nonzero exit means the table is missing rows it should have had
    (a backend read failed) — publishing it would blank cells that were correct
    a moment ago, which is exactly the failure the caller must avoid. Targets
    opentab simply could not price are absent from a *successful* table, and
    those legitimately have no price.

    Temp files rather than pipes on every stream, which is not a detail: a pipe
    reaches EOF only once *every* process holding its write end is gone, so a
    backgrounded grandchild of opentab keeps `communicate()` blocked long after
    opentab itself exited — and the timeout does not save us, because Python
    kills the child and then waits on that same pipe again. Against a file the
    deadline below is the only thing this daemon depends on, and the output is
    read back bounded instead of held in memory whatever its size.
    """
    targets = [t for t in targets if t and "\t" not in t and "\n" not in t]
    if not targets:
        return {}

    # `--` before the final `-`: extra args are the user's, and a flag with an
    # *optional* value (opentab's own `--demo`) would otherwise read `-` as that
    # value and exit 2, which would silently freeze every price in the sidebar.
    argv = [cfg.opentab_bin, "cost", "--batch", *cfg.opentab_args, "--", "-"]
    payload = ("\n".join(targets) + "\n").encode("utf-8")  # never the locale's
    cap = max(MIN_OUTPUT, len(payload) * OUTPUT_FACTOR)
    with tempfile.TemporaryFile() as stdin_file:
        stdin_file.write(payload)
        stdin_file.seek(0)
        with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
            try:
                process = subprocess.Popen(  # noqa: S603 -- argv, never a shell
                    argv, stdin=stdin_file, stdout=out_file, stderr=err_file
                )
            except FileNotFoundError as error:
                raise BatchUnavailable(f"{cfg.opentab_bin} not found on PATH") from error
            except OSError as error:
                raise BatchUnavailable(str(error)) from error

            deadline = time.monotonic() + BATCH_TIMEOUT
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()  # immediate: nothing of ours holds a pipe open
                    raise BatchUnavailable(f"timed out after {BATCH_TIMEOUT}s")
                # Checked while it runs, not afterwards: the files are on disk,
                # so a runaway would otherwise fill the filesystem for as long as
                # the timeout allows rather than costing us any memory.
                if _too_big(out_file, cap) or _too_big(err_file, cap):
                    process.kill()
                    process.wait()
                    raise BatchUnavailable(f"wrote more than {cap} bytes")
                time.sleep(POLL)

            if process.returncode != 0:
                raise BatchUnavailable(f"exit {process.returncode}{_detail(err_file)}")
            # A table cut off at the cap is not a table: the rows past the cut
            # would look like targets opentab could not price, and the caller
            # would clear prices that were perfectly good. Refuse it instead --
            # the previous round's numbers stay up, which is the honest answer.
            if _too_big(out_file, cap):
                raise BatchUnavailable(f"wrote more than {cap} bytes")
            out_file.seek(0)
            text = out_file.read(cap).decode("utf-8", "replace")
    return parse_table(text, strip_approx=cfg.strip_approx)


def _too_big(handle, cap: int) -> bool:  # noqa: ANN001 -- any binary file object
    """Whether the child has written past the cap. Its fd, so its size."""
    try:
        return os.fstat(handle.fileno()).st_size > cap
    except OSError:
        return False


def _detail(err_file) -> str:  # noqa: ANN001 -- any binary file object
    """opentab's first line of stderr, for the log. Bounded; never fatal."""
    try:
        err_file.seek(0)
        first = err_file.read(4096).decode("utf-8", "replace").strip().splitlines()
    except OSError:
        return ""
    return f": {first[0]}" if first else ""


def parse_table(text: str, strip_approx: bool = False) -> dict[str, str]:
    """Read opentab's `<target>\\t<price>` lines into a map.

    Later rows win, which only matters if a caller asked for the same target
    twice — opentab prices it twice and prints the same answer both times.
    """
    table: dict[str, str] = {}
    for line in text.splitlines():
        target, separator, value = line.partition("\t")
        if not separator:
            continue
        value = value.strip()
        if not value:
            continue
        if strip_approx:
            value = value.lstrip("~") or value
        # Herdr caps a token value at 80 characters and strips control
        # characters; a price is far shorter, so anything longer is not one.
        if len(value) > 80 or any(ord(c) < 32 for c in value):
            continue
        table[target] = value
    return table
