import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import core  # noqa: E402
from herdr import Agent  # noqa: E402


def agent(**fields):
    raw = {"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"}
    raw.update(fields)
    return Agent(raw)


class SessionIdFromPath(unittest.TestCase):
    def test_claude_transcript_is_named_after_its_session(self):
        self.assertEqual(
            core.session_id_from_path(
                "/Users/mo/.claude/projects/-Users-mo-x/"
                "0199c0de-1234-4abc-8def-000000000001.jsonl"
            ),
            "0199c0de-1234-4abc-8def-000000000001",
        )

    def test_codex_rollout_keeps_the_id_after_its_timestamp(self):
        self.assertEqual(
            core.session_id_from_path(
                "/Users/mo/.codex/sessions/2026/08/16/"
                "rollout-2026-08-16T10-19-00-0199c0de-1234-4abc-8def-000000000002.jsonl"
            ),
            "0199c0de-1234-4abc-8def-000000000002",
        )

    def test_prefixed_ids_survive_without_a_uuid(self):
        self.assertEqual(core.session_id_from_path("/tmp/ses_7f3a9c21b4.json"), "ses_7f3a9c21b4")

    def test_a_path_with_no_id_in_it_yields_none(self):
        self.assertIsNone(core.session_id_from_path("/tmp/transcript.jsonl"))
        self.assertIsNone(core.session_id_from_path("/tmp/"))


class UsableSessionId(unittest.TestCase):
    def test_a_path_is_not_an_id(self):
        self.assertFalse(core.is_usable_session_id("/Users/mo/project"))
        self.assertFalse(core.is_usable_session_id(""))

    def test_an_id_naming_an_existing_file_is_refused(self):
        # opentab reads an existing path as a directory target, so such an "id"
        # would silently price whatever project the file sits in.
        with tempfile.TemporaryDirectory() as directory:
            name = "0199c0de-1234-4abc-8def-000000000003"
            open(os.path.join(directory, name), "w").close()
            cwd = os.getcwd()
            os.chdir(directory)
            try:
                self.assertFalse(core.is_usable_session_id(name))
            finally:
                os.chdir(cwd)

    def test_a_plain_id_passes(self):
        self.assertTrue(core.is_usable_session_id("0199c0de-1234-4abc-8def-000000000004"))


class TargetFor(unittest.TestCase):
    def test_a_reported_id_wins_over_the_directory(self):
        target, kind = core.target_for(
            agent(
                cwd="/Users/mo/project",
                agent_session={"source": "claude", "agent": "claude", "kind": "id", "value": "abc"},
            ),
            project_fallback=True,
        )
        self.assertEqual((target, kind), ("abc", "session"))

    def test_a_reported_path_is_reduced_to_its_id(self):
        target, kind = core.target_for(
            agent(
                cwd="/Users/mo/project",
                agent_session={
                    "source": "codex",
                    "agent": "codex",
                    "kind": "path",
                    "value": "/x/rollout-2026-08-16T10-19-00-"
                    "0199c0de-1234-4abc-8def-000000000005.jsonl",
                },
            ),
            project_fallback=False,
        )
        self.assertEqual((target, kind), ("0199c0de-1234-4abc-8def-000000000005", "session"))

    def test_the_agents_own_cwd_is_preferred_over_the_pane_label(self):
        target, kind = core.target_for(
            agent(cwd="/Users/mo", foreground_cwd="/Users/mo/project"), project_fallback=True
        )
        self.assertEqual((target, kind), ("/Users/mo/project", "project"))

    def test_no_fallback_means_no_target(self):
        target, kind = core.target_for(agent(cwd="/Users/mo/project"), project_fallback=False)
        self.assertIsNone(target)
        self.assertIsNone(kind)


class Plan(unittest.TestCase):
    def test_two_agents_in_one_project_are_priced_for_neither(self):
        # One directory answers with one number, and it cannot say which of the
        # two agents spent it -- so it is shown to neither.
        targets, assignments = core.plan(
            [
                agent(pane_id="w1:p1", cwd="/p"),
                agent(pane_id="w1:p2", cwd="/p"),
                agent(pane_id="w1:p3", cwd="/q"),
            ],
            project_fallback=True,
        )
        self.assertEqual(targets, ["/q"])
        self.assertEqual(
            assignments,
            [
                core.Assignment("w1:p1", None, "shared"),
                core.Assignment("w1:p2", None, "shared"),
                core.Assignment("w1:p3", "/q", "project"),
            ],
        )

    def test_two_panes_on_one_session_share_its_price(self):
        # A session id is exact: two panes on the same session really do have
        # the same spend, and it is asked for once.
        session = "d89649b3-c5e5-42b4-b9f3-c95b0235a5ce"
        targets, assignments = core.plan(
            [
                agent(
                    pane_id="w1:p1",
                    cwd="/p",
                    agent_session={"source": "claude", "kind": "id", "value": session},
                ),
                agent(
                    pane_id="w1:p2",
                    cwd="/q",
                    agent_session={"source": "claude", "kind": "id", "value": session},
                ),
            ],
            project_fallback=True,
        )
        self.assertEqual(targets, [session])
        self.assertEqual([a.kind for a in assignments], ["session", "session"])

    def test_an_agent_filter_drops_the_others(self):
        targets, assignments = core.plan(
            [agent(pane_id="w1:p1", agent="claude", cwd="/p"),
             agent(pane_id="w1:p2", agent="codex", cwd="/q")],
            project_fallback=True,
            only_agents=["codex"],
        )
        self.assertEqual(targets, ["/q"])
        self.assertEqual([a.pane_id for a in assignments], ["w1:p2"])

    def test_a_row_without_a_pane_is_skipped(self):
        targets, assignments = core.plan([agent(pane_id="", cwd="/p")], project_fallback=True)
        self.assertEqual((targets, assignments), ([], []))


class CrowdedProjects(unittest.TestCase):
    """Two agents in one repo must not be priced by that repo, however spelled.

    opentab folds any directory to its git root, so panes that look like
    different targets get one identical answer -- verified against opentab
    1.17.0: `/repo`, `/repo/src` and `/repo/src/pkg` all price the same.
    """

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        os.mkdir(os.path.join(self.repo, ".git"))
        self.subdir = os.path.join(self.repo, "frontend")
        os.mkdir(self.subdir)

    def test_a_subdirectory_is_the_same_project(self):
        self.assertEqual(core.project_key(self.subdir), core.project_key(self.repo))

    def test_two_agents_in_one_repo_are_priced_for_neither(self):
        targets, assignments = core.plan(
            [agent(pane_id="w1:p1", cwd=self.repo), agent(pane_id="w1:p2", cwd=self.subdir)],
            project_fallback=True,
        )
        self.assertEqual(targets, [])
        self.assertEqual([a.kind for a in assignments], ["shared", "shared"])

    def test_a_session_pane_makes_its_neighbour_unpriceable(self):
        # The directory answers with the project's most recent session, which
        # may well be the session-priced agent's own -- so the pane without one
        # gets nothing, while the exact price stays.
        session = "d89649b3-c5e5-42b4-b9f3-c95b0235a5ce"
        targets, assignments = core.plan(
            [
                agent(
                    pane_id="w1:p1",
                    cwd=self.repo,
                    agent_session={"source": "claude", "kind": "id", "value": session},
                ),
                agent(pane_id="w1:p2", cwd=self.subdir),
            ],
            project_fallback=True,
        )
        self.assertEqual(targets, [session])
        self.assertEqual([a.kind for a in assignments], ["session", "shared"])

    def test_an_ignored_agent_still_crowds_its_project(self):
        # `agents: ["codex"]` hides the claude pane from the sidebar; it does
        # not stop claude spending money in that repo.
        targets, assignments = core.plan(
            [
                agent(pane_id="w1:p1", agent="claude", cwd=self.repo),
                agent(pane_id="w1:p2", agent="codex", cwd=self.subdir),
            ],
            project_fallback=True,
            only_agents=["codex"],
        )
        self.assertEqual(targets, [])
        self.assertEqual(assignments, [core.Assignment("w1:p2", None, "shared")])

    def test_a_worktree_folds_into_its_repo(self):
        # opentab folds `<repo>/.worktrees/<name>` back into <repo>, so an agent
        # in a worktree and one in the main checkout share a price.
        worktree = os.path.join(self.repo, ".worktrees", "feature")
        os.makedirs(worktree)
        self.assertEqual(core.project_key(worktree), core.project_key(self.repo))

    def test_a_directory_that_is_not_a_repo_is_its_own_project(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        targets, _ = core.plan(
            [agent(pane_id="w1:p1", cwd=self.repo), agent(pane_id="w1:p2", cwd=other)],
            project_fallback=True,
        )
        self.assertEqual(sorted(targets), sorted([self.repo, other]))


class AlignTable(unittest.TestCase):
    def test_amounts_are_padded_to_one_width(self):
        table = {"/p": "~$9.46", "/q": "~$34.39", "/r": "~$123.00"}
        self.assertEqual(
            core.align_table(table),
            {"/p": "~$  9.46", "/q": "~$ 34.39", "/r": "~$123.00"},
        )

    def test_padding_goes_after_the_prefix_because_herdr_trims_the_value(self):
        aligned = core.align_table({"/p": "$9.46", "/q": "$34.39"})
        for value in aligned.values():
            self.assertEqual(value, value.strip())
            self.assertTrue(value.startswith("$"))

    def test_mixed_prefixes_still_end_in_the_same_column(self):
        aligned = core.align_table({"/p": "~$9.46", "/q": "$34.39"})
        self.assertEqual({len(value) for value in aligned.values()}, {6})

    def test_a_single_price_is_left_alone(self):
        self.assertEqual(core.align_table({"/p": "~$9.46"}), {"/p": "~$9.46"})

    def test_equal_widths_need_no_padding(self):
        table = {"/p": "~$9.46", "/q": "~$3.10"}
        self.assertEqual(core.align_table(table), table)

    def test_a_value_without_an_amount_is_untouched(self):
        table = {"/p": "~$9.46", "/q": "n/a", "/r": "~$34.39"}
        self.assertEqual(core.align_table(table)["/q"], "n/a")

    def test_a_bare_number_is_never_padded(self):
        # Herdr trims a reported value, so a leading pad would come straight
        # back off and the value would look changed on every round.
        self.assertEqual(core.align_table({"a": "9", "b": "10"}), {"a": "9", "b": "10"})

    def test_one_absurd_value_does_not_stretch_the_others(self):
        table = {"/p": "~$9.46", "/q": "~$1234567890123.00"}
        self.assertEqual(core.align_table(table)["/p"], "~$9.46")


class Updates(unittest.TestCase):
    def test_only_changed_panes_are_reported(self):
        agents = [
            agent(pane_id="w1:p1", cwd="/p", tokens={"cost": "~$1.00"}),
            agent(pane_id="w1:p2", cwd="/q", tokens={"cost": "~$2.00"}),
        ]
        _, assignments = core.plan(agents, project_fallback=True)
        # Both panes are already known to this daemon and their leases are
        # fresh, so only the one whose price moved is written.
        reported = {"w1:p1": ("~$1.00", 100.0), "w1:p2": ("~$2.00", 100.0)}
        result = core.updates(
            agents, assignments, {"/p": "~$1.00", "/q": "~$9.00"}, "cost", reported, 60.0, 130.0
        )
        self.assertEqual(result, [core.Update("w1:p2", "~$9.00")])

    def test_a_price_that_vanished_from_a_good_table_is_cleared(self):
        agents = [agent(pane_id="w1:p1", cwd="/p", tokens={"cost": "~$1.00"})]
        _, assignments = core.plan(agents, project_fallback=True)
        self.assertEqual(
            core.updates(agents, assignments, {}, "cost"), [core.Update("w1:p1", None)]
        )

    def test_an_unpriced_pane_with_no_token_needs_no_call(self):
        agents = [agent(pane_id="w1:p1", cwd="/p")]
        _, assignments = core.plan(agents, project_fallback=True)
        self.assertEqual(core.updates(agents, assignments, {}, "cost"), [])

    def test_another_plugins_token_is_left_alone(self):
        agents = [agent(pane_id="w1:p1", cwd="/p", tokens={"burn": "$3"})]
        _, assignments = core.plan(agents, project_fallback=True)
        self.assertEqual(
            core.updates(agents, assignments, {"/p": "~$1.00"}, "cost"),
            [core.Update("w1:p1", "~$1.00")],
        )


if __name__ == "__main__":
    unittest.main()
