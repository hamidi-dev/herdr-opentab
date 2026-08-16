"""Deciding what to price and what to report. No I/O lives here.

Two questions, both pure, both worth testing without a herdr server running:

  1. Which opentab target stands for this agent pane?
  2. Given a price table, which panes actually need a herdr call?
"""

from __future__ import annotations

import os
import re
from typing import Iterable, NamedTuple

from herdr import Agent

# A session id in opentab's sense: something with no path separator that does
# not exist on disk. Herdr hands us ids from official agent integrations, so
# the shapes are the harnesses' own -- UUIDs (Claude, Codex, pi, Zaly) and
# prefixed ids (`ses_...` for OpenCode).
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_PREFIXED_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_-]{6,}$")
_SESSION_SUFFIXES = (".jsonl", ".json", ".log", ".db", ".sqlite")
# The amount at the end of a price: "$12.21", "~$1,004.50", "12.21 EUR" has none.
_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?$")


class Assignment(NamedTuple):
    """One agent pane and the opentab target that prices it."""

    pane_id: str
    target: str | None
    # "session" is this agent's own id; "project" is the pane's directory, which
    # prices whatever session in that project ran most recently.
    kind: str | None


class Update(NamedTuple):
    pane_id: str
    value: str | None  # None clears the token


def session_id_from_path(path: str) -> str | None:
    """Pull a session id out of a session file path, or None.

    Herdr reports `kind = "path"` for agents whose native resume takes a file
    rather than an id, but opentab only takes an id (a target containing a path
    separator is read as a directory). The id is in the file name either way:
    Claude names the transcript after the session, and a Codex rollout carries
    the thread id after its timestamp.
    """
    name = os.path.basename(path.rstrip("/"))
    for suffix in _SESSION_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    if not name:
        return None
    matches = _UUID.findall(name)
    if matches:
        # The last one: a rollout file's leading field is a timestamp, and any
        # other embedded id precedes the session's own.
        return matches[-1]
    if _PREFIXED_ID.match(name):
        return name
    return None


def is_usable_session_id(value: str) -> bool:
    """Whether opentab would read `value` as a session id rather than a path."""
    if not value or len(value) > 200:
        return False
    if os.sep in value or (os.altsep and os.altsep in value):
        return False
    if any(ord(c) < 32 or c in "\t" for c in value):
        return False
    # opentab treats an existing path as a directory target, so an id that
    # happens to name a file in the cwd would price the wrong thing.
    return not os.path.exists(os.path.expanduser(value))


def target_for(agent: Agent, project_fallback: bool) -> tuple[str | None, str | None]:
    """The opentab target for one agent, as `(target, kind)`.

    A native session id is the only answer that is certainly *this* pane's
    spend. The directory fallback answers with the project's most recent
    session instead, which is right for the common one-agent-per-project case
    and wrong when two agents share a project -- hence the switch.
    """
    value = agent.session_value
    if value:
        if agent.session_kind == "path":
            extracted = session_id_from_path(value)
            if extracted and is_usable_session_id(extracted):
                return extracted, "session"
        elif is_usable_session_id(value):
            return value, "session"

    if not project_fallback:
        return None, None
    # foreground_cwd is the cwd of the process actually holding the pane -- the
    # agent itself -- while cwd is the pane's label path, which a `cd` inside a
    # shell pane can leave behind.
    directory = agent.foreground_cwd or agent.cwd
    if not directory:
        return None, None
    return directory, "project"


def project_key(directory: str | None) -> str | None:
    """The project opentab will price `directory` as, or None.

    Not the directory itself: opentab walks up to the nearest `.git` and folds a
    linked worktree back into its main repo (`util.git_root` /
    `util.resolve_project_root`), so `/repo`, `/repo/src` and
    `/repo/.worktrees/feature` are *one* target answered with one number. This
    mirrors that, because the ambiguity check below is only sound if it groups
    panes the same way opentab does.

    Being coarser than opentab is safe here (a pane loses a price it could have
    had); being finer is not (a pane shows a price that is not its own), which
    is why the fallbacks all return something no narrower than what was asked.
    """
    if not directory:
        return None
    try:
        current = os.path.abspath(os.path.expanduser(directory))
    except (OSError, ValueError):
        return _fold_worktree(directory)
    if os.path.isdir(current):
        while True:
            if os.path.exists(os.path.join(current, ".git")):
                return _fold_worktree(current)
            parent = os.path.dirname(current)
            if parent == current:  # filesystem root, no repo above us
                break
            current = parent
    return _fold_worktree(directory)


def _fold_worktree(directory: str) -> str:
    """`/repo/.worktrees/feature` -> `/repo`, the way opentab folds it."""
    try:
        dotgit = os.path.join(os.path.expanduser(directory), ".git")
        if os.path.isfile(dotgit):
            # A linked worktree's `.git` is a file: "gitdir: <main>/.git/worktrees/<name>".
            with open(dotgit, encoding="utf-8") as handle:
                line = handle.read(4096).strip()
            if line.startswith("gitdir:"):
                gitdir = line[len("gitdir:") :].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(
                        os.path.join(os.path.expanduser(directory), gitdir)
                    )
                marker = f"{os.sep}.git{os.sep}worktrees{os.sep}"
                if marker in gitdir:
                    main = gitdir[: gitdir.index(marker)]
                    if main:
                        return main
    except OSError:
        pass
    for marker in (f"{os.sep}.worktrees{os.sep}", f"{os.sep}.git{os.sep}worktrees{os.sep}"):
        index = directory.find(marker)
        if index > 0:
            return os.path.normpath(directory[:index])
    # normpath even when nothing folded: a directory that no longer exists is
    # never made absolute above, and opentab keys its store by a normalised
    # path, so `/gone/sub/..` and `/gone` are one project to it and would
    # otherwise be two here -- which is the unsafe direction.
    return os.path.normpath(directory)


def plan(
    agents: Iterable[Agent],
    project_fallback: bool = True,
    only_agents: list[str] | None = None,
) -> tuple[list[str], list[Assignment]]:
    """Map agents onto targets and collect the batch's target list.

    The target list is deduplicated in first-seen order: two panes on the same
    project (a split, or a subagent that resolves to the same root) are one row
    to ask about, and opentab keys its answer by the exact string asked.
    """
    agents = list(agents)

    # Counted over *every* agent herdr reports, before `only_agents` narrows
    # anything: an agent this plugin was told to ignore still spends money in
    # that project, and its session can still be the most recent one there.
    crowded: dict[str, int] = {}
    keys: dict[str, str | None] = {}
    for agent in agents:
        if not agent.pane_id:
            continue
        # Resolved once per pane: every call walks the filesystem, and the
        # answer for a pane's directory cannot change inside one round.
        key = keys[agent.pane_id] = project_key(agent.foreground_cwd or agent.cwd)
        if key:
            crowded[key] = crowded.get(key, 0) + 1

    assignments: list[Assignment] = []
    targets: list[str] = []
    seen: set[str] = set()
    for agent in agents:
        if not agent.pane_id:
            continue
        if only_agents is not None and agent.agent not in only_agents:
            continue
        target, kind = target_for(agent, project_fallback)

        # A directory prices that project's most recently active session, which
        # is one answer for however many agents are working there -- and which
        # of them it belongs to is not knowable from here. Handing it to any of
        # them would put a figure next to an agent that did not spend it, so a
        # project with company prices nobody by directory: the sidebar promises
        # per-agent spend, and "nothing" is the only honest per-agent answer
        # available. A session id is exact, so a pane that has one keeps its
        # price no matter how crowded the project is.
        #
        # Deliberately blunt: `opentab_args` could pin a harness
        # (`["--harness", "claude"]`), which would make a claude pane's
        # directory unambiguous again even with a codex agent next door. Reading
        # that out of the user's argv would couple this to opentab's flag
        # spelling for a case that ends in a blank cell rather than a wrong
        # number -- the safe direction, and the one worth erring in.
        if kind == "project" and crowded.get(keys.get(agent.pane_id) or "", 0) > 1:
            assignments.append(Assignment(agent.pane_id, None, "shared"))
            continue

        assignments.append(Assignment(agent.pane_id, target, kind))
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return targets, assignments


def align_table(table: dict[str, str], limit: int = 4) -> dict[str, str]:
    """Pad prices so their digits line up in a column.

    Herdr joins the tokens of a sidebar row with " · " and *trims* a reported
    value, so a price cannot be right-aligned by padding it at the front. What
    survives is padding inside the value, between the currency prefix and the
    number: "$ 9.46" and "$34.39" then end at the same column, which lines up
    both the amounts and whatever token follows them in the row.

    Only worth anything when the price starts its row -- the layout
    `opentab.setup` writes. Values without a number are left exactly as opentab
    printed them, and so is anything `limit` or more characters short of the
    widest: one four-figure outlier should not open a chasm in front of every
    other price.
    """
    parts: dict[str, tuple[str, str]] = {}
    for target, value in table.items():
        match = _AMOUNT.search(value)
        # A bare number has nowhere to put the padding: herdr would trim a
        # leading space straight back off, and the value would then differ from
        # what we reported every single round.
        if match and match.start() > 0:
            parts[target] = (value[: match.start()], match.group())
    if len(parts) < 2:
        return dict(table)

    width = max(len(prefix) + len(number) for prefix, number in parts.values())
    out = dict(table)
    for target, (prefix, number) in parts.items():
        pad = width - len(prefix) - len(number)
        if 0 < pad <= limit:
            out[target] = f"{prefix}{' ' * pad}{number}"
    return out


def updates(
    agents: Iterable[Agent],
    assignments: Iterable[Assignment],
    table: dict[str, str],
    token: str,
    reported: dict[str, tuple] | None = None,
    renew_after: float = float("inf"),
    now: float = 0.0,
) -> list[Update]:
    """The herdr calls this round needs -- and only those.

    Reporting an unchanged value would spend a subprocess and a pane revision
    on nothing, so the comparison is against what herdr itself currently shows
    (`tokens` from the same `agent list` this round was planned from). That
    also self-heals: if anything else clears the token, the next round sets it
    again without this process having to remember it did.

    A pane whose target is absent from a *successful* table has no price, and
    its stale one is cleared. A failed batch never reaches here -- the caller
    keeps the previous table instead, so an unreadable backend cannot blank a
    cell that was right a moment ago.

    `reported` is what this daemon last wrote, as `{pane_id: (value, when)}`.
    It buys two things a diff against herdr alone cannot give:

    * **renewal.** Prices are reported with a lease (`--ttl-ms`), so a value
      nobody refreshes disappears instead of lingering as a plausible number in
      a sidebar whose daemon died. An unchanged price is therefore re-reported
      once it is `renew_after` seconds old.
    * **cleanup.** A pane this daemon priced that has dropped out of the plan --
      its agent exited, or an `agents` filter now excludes it -- gets its token
      cleared, rather than keeping a number that will never be updated again.
    """
    current = {agent.pane_id: agent.tokens.get(token) for agent in agents}
    reported = reported or {}
    out: list[Update] = []
    planned: set[str] = set()
    for pane_id, target, _kind in assignments:
        planned.add(pane_id)
        value = table.get(target) if target else None
        shown = current.get(pane_id)
        if value:
            if value != shown:
                out.append(Update(pane_id, value))
            else:
                entry = reported.get(pane_id)
                when = entry[1] if entry else None
                if when is None or now - when >= renew_after:
                    out.append(Update(pane_id, value))
        elif shown:
            out.append(Update(pane_id, None))

    # Not conditional on what herdr shows: a pane whose agent exited is gone
    # from `agent list` entirely, so its token would otherwise sit there
    # unreachable. The caller forgets the pane afterwards, so this is attempted
    # once; if the pane closed too, herdr simply refuses and nothing is lost.
    for pane_id in reported:
        if pane_id not in planned:
            out.append(Update(pane_id, None))
    return out
