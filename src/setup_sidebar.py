"""Put the `$cost` token into the user's Agent sidebar rows.

Herdr renders custom tokens only where the layout asks for them, so a freshly
installed plugin reports a price nobody can see until `ui.sidebar.agents.rows`
mentions it. This action writes that block when it can do so without guessing,
and otherwise prints the exact lines to paste -- it never rewrites a layout the
user already tuned.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import config

SECTION = "[ui.sidebar.agents]"

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def herdr_config_path() -> str:
    override = os.environ.get("HERDR_CONFIG_PATH")
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "herdr", "config.toml")


def block(token: str) -> str:
    """The default Agent layout, with the price leading the agent row.

    Price first on purpose: herdr lays a row out left to right, so a price that
    follows the agent label starts wherever that label happens to end and the
    amounts zig-zag down the sidebar. Leading its row, every price starts in the
    same column -- and the daemon pads them to a common width, so the digits and
    the labels after them line up too.
    """
    return (
        f"{SECTION}\n"
        "rows = [\n"
        '  ["state_icon", "workspace", "tab"],\n'
        f'  ["${token}", "agent"],\n'
        "]\n"
    )


def has_section(text: str) -> bool:
    """Whether appending `[ui.sidebar.agents]` to this file could collide.

    A scan, not a TOML parse (the stdlib had no parser before 3.11 and this
    supports 3.9), so the question it answers is deliberately the pessimistic
    one: *could* the table already exist? An inline table
    (`ui = { sidebar = { agents = ... } }`) or a quoted spelling this scan
    cannot follow counts as yes -- appending a second definition of a table TOML
    already has makes the whole config unparseable, and herdr would then load
    none of the user's settings. Declining to edit costs them one paste.
    """
    for line in text.splitlines():
        stripped = _strip_comment(line).strip()
        if not stripped:
            continue
        key = _header_key(stripped)
        if key == "ui.sidebar.agents":
            return True
        if key in ("ui.sidebar", "ui") and _table_defines_agents(text, key):
            return True
        if re.match(r"^ui\.sidebar\.agents\b", stripped):
            return True
        # Shapes this scan cannot read, where being wrong corrupts the file:
        # `ui = { … }` in any nesting, and any quoted part of a `ui…` header.
        if re.match(r"^ui(\.[A-Za-z0-9_\"'.-]+)*\s*=\s*\{", stripped):
            return True
        if stripped.startswith("[") and "ui" in stripped and ('"' in stripped or "'" in stripped):
            return True
    return False


def mentions_token(text: str, token: str) -> bool:
    """Whether the layout actually asks herdr to render `$token`.

    Three things have to hold, and each of them has bitten this check: the token
    is quoted (that is how it appears in a row, and prose about `$cost` in a
    comment configures nothing), it is not inside a comment (the
    `["$cost", "agent"]` somebody pasted and then commented out while trying
    something else), and it is inside the Agent sidebar table -- `note = "$cost"`
    under `[some.other.table]` puts it nowhere herdr will ever draw it.

    Getting this wrong is not cosmetic: `doctor` would call the sidebar
    configured while the price it reports has nowhere to appear, and `setup`
    would decline to add the row that would have fixed it.
    """
    needles = (f'"${token}"', f"'${token}'")
    inside = False  # within [ui.sidebar.agents] or one of its subtables
    depth = 0  # unclosed brackets of a rows array reached by a dotted key
    for line in text.splitlines():
        code = _strip_comment(line).strip()
        header = _header_key(code) if depth == 0 else None
        if header is not None:
            # The table itself, and `[ui.sidebar.agents.rows_by_agent]` --
            # per-agent overrides are rows herdr draws too.
            inside = header == "ui.sidebar.agents" or header.startswith("ui.sidebar.agents.")
            continue
        # A dotted key reaches the same table from an outer one:
        # `agents.rows = [...]` under [ui.sidebar], or the whole path spelled out.
        dotted = re.match(r"^(ui\.sidebar\.agents|sidebar\.agents|agents)\b", code)
        if not (inside or depth or dotted):
            continue
        if any(needle in code for needle in needles):
            return True
        if not inside:  # a dotted key's array can run over several lines
            depth = max(0, depth + _bracket_delta(code))
    return False


def _bracket_delta(code: str) -> int:
    """Unclosed `[` on this line, ignoring brackets inside strings."""
    quote = None
    delta = 0
    for char in code:
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "[":
            delta += 1
        elif char == "]":
            delta -= 1
    return delta


def _strip_comment(line: str) -> str:
    """Drop a TOML end-of-line comment, leaving `#` inside a string alone."""
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _header_key(line: str) -> str | None:
    """`[ui.sidebar."agents"]` -> `ui.sidebar.agents`, anything else -> None.

    TOML lets any part of a table header be quoted, and `[ui.sidebar."agents"]`
    is the *same table* as `[ui.sidebar.agents]` -- appending a second header
    for it would make the file fail to parse.
    """
    if not (line.startswith("[") and line.endswith("]")) or line.startswith("[["):
        return None
    parts = [part.strip().strip("\"'") for part in line[1:-1].split(".")]
    # Every part must look like a bare TOML key, or a row of an array
    # (`["state_icon", "workspace"]`) would parse as a table header and end
    # whatever section the reader thought it was in.
    if not parts or not all(_BARE_KEY.match(part) for part in parts):
        return None
    return ".".join(parts)


def _table_defines_agents(text: str, header: str) -> bool:
    """True when `[ui]`/`[ui.sidebar]` sets the agents table with dotted keys."""
    key = "sidebar.agents" if header == "ui" else "agents"
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = _header_key(stripped) == header
            continue
        if inside and not stripped.startswith("#") and re.match(rf"^{re.escape(key)}\b", stripped):
            return True
    return False


def install(token: str) -> int:
    path = herdr_config_path()
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = None
    except OSError as error:
        print(f"opentab: cannot read {path}: {error}", file=sys.stderr)
        return 1

    if text is not None and mentions_token(text, token):
        print(f"opentab: ${token} is already in {path}; nothing to do")
        return 0

    if text is not None and has_section(text):
        print(
            f"opentab: {path} already configures {SECTION}, so it is yours to edit.\n"
            f'Add "${token}" to one of its rows -- first in the row keeps the\n'
            f"prices in one column:\n\n"
            f'  ["${token}", "agent"],\n'
        )
        return 0

    body = block(token)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            if text:
                handle.write("\n" if text.endswith("\n") else "\n\n")
            handle.write("# added by the opentab herdr plugin\n")
            handle.write(body)
    except OSError as error:
        print(f"opentab: cannot write {path}: {error}", file=sys.stderr)
        return 1

    print(f"opentab: wrote the Agent sidebar layout to {path}:\n\n{body}")
    print("herdr reloads config.toml on save; run `herdr config check` if the rows look off.")
    return 0


def show(token: str) -> int:
    print(f"Add this to {herdr_config_path()}:\n\n{block(token)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add the OpenTab price token to the sidebar")
    parser.add_argument("mode", nargs="?", default="install", choices=["install", "show"])
    args = parser.parse_args(argv)
    token = config.load().token
    return install(token) if args.mode == "install" else show(token)


if __name__ == "__main__":
    sys.exit(main())
