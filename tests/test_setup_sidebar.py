import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import setup_sidebar  # noqa: E402


class Detection(unittest.TestCase):
    def test_the_section_is_found(self):
        self.assertTrue(setup_sidebar.has_section("[ui.sidebar.agents]\nrows = []\n"))

    def test_dotted_keys_count_as_the_section(self):
        self.assertTrue(setup_sidebar.has_section("[ui.sidebar]\nagents.rows = [[\"agent\"]]\n"))
        self.assertTrue(setup_sidebar.has_section("[ui]\nsidebar.agents.rows = [[\"agent\"]]\n"))

    def test_a_commented_out_section_does_not_count(self):
        self.assertFalse(setup_sidebar.has_section("# [ui.sidebar.agents]\n"))

    def test_an_inline_table_counts_as_the_section(self):
        # `ui = { sidebar = { agents = ... } }` defines the same table. Appending
        # a [ui.sidebar.agents] header after it makes the file unparseable, and
        # herdr would then load none of the user's settings.
        self.assertTrue(
            setup_sidebar.has_section('ui = { sidebar = { agents = { rows = [["$cost"]] } } }\n')
        )

    def test_a_quoted_header_this_scan_cannot_read_counts(self):
        self.assertTrue(setup_sidebar.has_section('[ui.sidebar."\\u0061gents"]\nrows = []\n'))

    def test_an_unrelated_config_does_not_count(self):
        self.assertFalse(setup_sidebar.has_section("[ui]\nsidebar_width = 30\n"))
        self.assertFalse(setup_sidebar.has_section("[ui.sidebar.spaces]\nrows = []\n"))


class MentionsToken(unittest.TestCase):
    """What counts as "the sidebar is configured" — for `setup` and `doctor`."""

    def test_a_quoted_token_in_a_row_counts(self):
        self.assertTrue(
            setup_sidebar.mentions_token(
                '[ui.sidebar.agents]\nrows = [["$cost", "agent"]]\n', "cost"
            )
        )

    def test_prose_about_the_token_does_not(self):
        self.assertFalse(setup_sidebar.mentions_token("# put $cost somewhere\n", "cost"))

    def test_a_commented_out_row_does_not(self):
        # The shape of "I tried it and turned it off again": doctor would
        # otherwise call this configured and send the user hunting elsewhere.
        self.assertFalse(
            setup_sidebar.mentions_token(
                '[ui.sidebar.agents]\n# rows = [["$cost", "agent"]]\n', "cost"
            )
        )

    def test_a_hash_inside_a_string_does_not_hide_the_token(self):
        self.assertTrue(
            setup_sidebar.mentions_token('[ui.sidebar.agents]\nrows = [["#", "$cost"]]\n', "cost")
        )

    def test_a_trailing_comment_is_still_dropped(self):
        self.assertFalse(
            setup_sidebar.mentions_token(
                '[ui.sidebar.agents]\nrows = [["agent"]] # "$cost"\n', "cost"
            )
        )

    def test_another_plugins_token_is_not_ours(self):
        self.assertFalse(
            setup_sidebar.mentions_token('[ui.sidebar.agents]\nrows = [["$cost_usd"]]\n', "cost")
        )

    def test_a_token_outside_the_agents_table_does_not_count(self):
        # It parses, it is quoted, it is not in a comment -- and herdr has
        # nowhere to draw it, so doctor must not call the sidebar configured.
        self.assertFalse(
            setup_sidebar.mentions_token('[ui.sidebar.spaces]\nrows = [["$cost"]]\n', "cost")
        )
        self.assertFalse(setup_sidebar.mentions_token('[notes]\nnote = "$cost"\n', "cost"))

    def test_a_dotted_key_from_an_outer_table_counts(self):
        self.assertTrue(
            setup_sidebar.mentions_token(
                '[ui.sidebar]\nagents.rows = [["$cost", "agent"]]\n', "cost"
            )
        )

    def test_a_dotted_key_whose_array_spans_lines_counts(self):
        text = '[ui.sidebar]\nagents.rows = [\n  ["state_icon", "workspace"],\n  ["$cost", "agent"],\n]\n'
        self.assertTrue(setup_sidebar.mentions_token(text, "cost"))

    def test_per_agent_overrides_count(self):
        text = '[ui.sidebar.agents.rows_by_agent]\nclaude = [["$cost", "agent"]]\n'
        self.assertTrue(setup_sidebar.mentions_token(text, "cost"))


class Install(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "config.toml")
        saved = os.environ.get("HERDR_CONFIG_PATH")
        os.environ["HERDR_CONFIG_PATH"] = self.path

        def restore():
            if saved is None:
                os.environ.pop("HERDR_CONFIG_PATH", None)
            else:
                os.environ["HERDR_CONFIG_PATH"] = saved

        self.addCleanup(restore)

    def install(self, token="cost"):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = setup_sidebar.install(token)
        return code, buffer.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def test_a_missing_config_is_created(self):
        code, output = self.install()
        self.assertEqual(code, 0)
        self.assertIn('["$cost", "$elapsed", "agent"]', self.read())
        self.assertIn("herdr server reload-config", output)

    def test_the_block_is_appended_to_an_unrelated_config(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[ui]\nsidebar_width = 30\n")
        self.install()
        text = self.read()
        self.assertIn("sidebar_width = 30", text)
        self.assertIn("[ui.sidebar.agents]", text)

    def test_running_twice_changes_nothing(self):
        self.install()
        first = self.read()
        code, output = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(self.read(), first)
        self.assertIn("already", output)

    def test_an_existing_layout_is_never_rewritten(self):
        original = '[ui.sidebar.agents]\nrows = [["state_icon", "agent"]]\n'
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(original)
        code, output = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(self.read(), original)
        self.assertIn("$cost", output)

    def test_a_mention_in_a_comment_is_not_a_configured_row(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("# I used to have $cost here\n")
        self.install()
        self.assertIn('["$cost", "$elapsed", "agent"]', self.read())

    def test_a_quoted_table_header_is_the_same_table(self):
        # `[ui.sidebar."agents"]` is the same TOML table, so appending our own
        # header would make the file fail to parse with "declared twice".
        original = '[ui.sidebar."agents"]\nrows = [["state_icon", "agent"]]\n'
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(original)
        self.install()
        self.assertEqual(self.read(), original)

    def test_a_renamed_token_is_the_one_written(self):
        self.install(token="spend")
        self.assertIn('["$spend", "$elapsed", "agent"]', self.read())

    def test_existing_cost_only_layout_gets_timer_instructions_not_rewritten(self):
        original = '[ui.sidebar.agents]\nrows = [["$cost", "agent"]]\n'
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(original)
        code, output = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(self.read(), original)
        self.assertIn('"$elapsed"', output)
        self.assertIn("herdr server reload-config", output)
        self.assertNotIn("nothing to do", output)

    def test_disabled_timer_is_not_in_layout(self):
        self.assertNotIn("$elapsed", setup_sidebar.block("cost", elapsed=False))


if __name__ == "__main__":
    unittest.main()
