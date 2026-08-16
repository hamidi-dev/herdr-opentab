import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import config  # noqa: E402
import opentab  # noqa: E402


def make_config(**overrides):
    values, warnings = dict(config.DEFAULTS), []
    values.update(overrides)
    return config.Config(config._coerce(values, warnings), warnings)


def fake_opentab(directory, body):
    """A stand-in binary, so the batch contract is tested against a real process."""
    path = os.path.join(directory, "opentab-stub")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ParseTable(unittest.TestCase):
    def test_tab_separated_rows_become_a_map(self):
        self.assertEqual(
            opentab.parse_table("/p\t~$1.00\n/q\t~$2.50\n"), {"/p": "~$1.00", "/q": "~$2.50"}
        )

    def test_rows_without_a_tab_are_ignored(self):
        self.assertEqual(opentab.parse_table("opentab: something went sideways\n"), {})

    def test_a_path_with_spaces_still_keys_correctly(self):
        self.assertEqual(opentab.parse_table("/My Project\t~$3.00\n"), {"/My Project": "~$3.00"})

    def test_an_empty_price_is_no_price(self):
        self.assertEqual(opentab.parse_table("/p\t\n"), {})

    def test_the_approximation_marker_can_be_dropped(self):
        self.assertEqual(opentab.parse_table("/p\t~$1.00\n", strip_approx=True), {"/p": "$1.00"})

    def test_values_herdr_would_reject_are_dropped(self):
        self.assertEqual(opentab.parse_table("/p\t" + "$" * 81 + "\n"), {})
        self.assertEqual(opentab.parse_table("/p\t~$1\x07\n"), {})


class Price(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_targets_reach_the_process_on_stdin(self):
        recorded = os.path.join(self.directory.name, "stdin.txt")
        stub = fake_opentab(
            self.directory.name, f"cat > {recorded}\nprintf '/p\\t~$1.00\\n'\n"
        )
        table = opentab.price(["/p", "/q"], make_config(opentab_bin=stub))
        self.assertEqual(table, {"/p": "~$1.00"})
        with open(recorded, encoding="utf-8") as handle:
            self.assertEqual(handle.read().splitlines(), ["/p", "/q"])

    def test_extra_args_cannot_swallow_the_stdin_marker(self):
        # opentab's own --demo takes an *optional* value, so without the `--`
        # it reads the trailing `-` as that value and exits 2 -- which would
        # freeze every price in the sidebar instead of failing loudly.
        recorded = os.path.join(self.directory.name, "argv.txt")
        stub = fake_opentab(self.directory.name, f'printf "%s\\n" "$@" > {recorded}\n')
        opentab.price(["/p"], make_config(opentab_bin=stub, opentab_args=["--demo"]))
        with open(recorded, encoding="utf-8") as handle:
            self.assertEqual(
                handle.read().splitlines(), ["cost", "--batch", "--demo", "--", "-"]
            )

    def test_a_non_ascii_target_survives_a_non_utf8_locale(self):
        recorded = os.path.join(self.directory.name, "stdin.txt")
        stub = fake_opentab(self.directory.name, f"cat > {recorded}\nprintf '/p/\\xe6\\xbc\\xa2\\t~$1.00\\n'\n")
        with mock.patch.dict(os.environ, {"LC_ALL": "C", "LANG": "C"}):
            table = opentab.price(["/p/\u6f22"], make_config(opentab_bin=stub))
        self.assertEqual(table, {"/p/\u6f22": "~$1.00"})

    def test_a_nonzero_exit_keeps_the_previous_table(self):
        # opentab exits 1 when a backend read failed: the table it printed is
        # missing rows, and publishing it would blank correct cells.
        stub = fake_opentab(self.directory.name, "printf '/p\\t~$1.00\\n'\nexit 1\n")
        with self.assertRaises(opentab.BatchUnavailable):
            opentab.price(["/p"], make_config(opentab_bin=stub))

    def test_a_missing_binary_is_reported_not_raised_as_oserror(self):
        with self.assertRaises(opentab.BatchUnavailable):
            opentab.price(["/p"], make_config(opentab_bin="definitely-not-opentab"))

    def test_no_targets_means_no_process(self):
        self.assertEqual(opentab.price([], make_config(opentab_bin="definitely-not-opentab")), {})

    def test_a_backgrounded_grandchild_cannot_hold_the_round_open(self):
        # The reason every stream is a temp file: a pipe reaches EOF only when
        # the last writer closes it, so a process opentab leaves behind keeps
        # the read blocked long after opentab exited. This must finish at
        # opentab's exit, not at the grandchild's.
        stub = fake_opentab(
            self.directory.name, "printf '/p\\t~$1.00\\n'\nsleep 30 &\nexit 0\n"
        )
        started = time.monotonic()
        table = opentab.price(["/p"], make_config(opentab_bin=stub))
        self.assertEqual(table, {"/p": "~$1.00"})
        self.assertLess(time.monotonic() - started, 5)

    def test_the_cap_scales_with_what_was_asked(self):
        # A row echoes its target, so a session with very many panes produces a
        # legitimately large table. A fixed ceiling would refuse it every round
        # and leave the sidebar blank for good.
        rows = "".join(f"printf '/p{i}\\t~$1.00\\n'\n" for i in range(40))
        stub = fake_opentab(self.directory.name, rows)
        targets = [f"/p{i}" for i in range(40)]
        with mock.patch.object(opentab, "MIN_OUTPUT", 64), mock.patch.object(
            opentab, "OUTPUT_FACTOR", 8
        ):
            table = opentab.price(targets, make_config(opentab_bin=stub))
        self.assertEqual(len(table), 40)

    def test_a_runaway_is_refused_rather_than_truncated(self):
        # A table cut off at the cap would look like a complete one whose later
        # targets could not be priced, and the caller would clear prices that
        # were perfectly good. Refusing keeps the previous round's numbers up.
        stub = fake_opentab(self.directory.name, "printf '/p\\t~$1.00\\n/q\\t~$2.00\\n'\n")
        with mock.patch.object(opentab, "MIN_OUTPUT", 12), mock.patch.object(
            opentab, "OUTPUT_FACTOR", 1
        ):
            with self.assertRaises(opentab.BatchUnavailable) as raised:
                opentab.price(["/p", "/q"], make_config(opentab_bin=stub))
        self.assertIn("more than", str(raised.exception))

    def test_a_flood_is_killed_while_it_runs(self):
        # Stdout is a file, so an unbounded writer costs disk, not memory --
        # and waiting for the timeout to notice would let it write for 20s.
        stub = fake_opentab(self.directory.name, "yes '/p\\t~$1.00' | head -100000\nsleep 20\n")
        started = time.monotonic()
        with mock.patch.object(opentab, "MIN_OUTPUT", 4096), mock.patch.object(
            opentab, "OUTPUT_FACTOR", 1
        ):
            with self.assertRaises(opentab.BatchUnavailable):
                opentab.price(["/p"], make_config(opentab_bin=stub))
        self.assertLess(time.monotonic() - started, 10)

    def test_a_wedged_opentab_gives_up_on_the_round(self):
        stub = fake_opentab(self.directory.name, "sleep 30\n")
        started = time.monotonic()
        with mock.patch.object(opentab, "BATCH_TIMEOUT", 0.3):
            with self.assertRaises(opentab.BatchUnavailable) as raised:
                opentab.price(["/p"], make_config(opentab_bin=stub))
        self.assertIn("timed out", str(raised.exception))
        self.assertLess(time.monotonic() - started, 5)

    def test_a_failure_carries_opentabs_own_first_line(self):
        stub = fake_opentab(self.directory.name, "echo 'no such backend' >&2\nexit 3\n")
        with self.assertRaises(opentab.BatchUnavailable) as raised:
            opentab.price(["/p"], make_config(opentab_bin=stub))
        self.assertEqual(str(raised.exception), "exit 3: no such backend")

    def test_targets_a_tsv_cannot_key_are_dropped(self):
        recorded = os.path.join(self.directory.name, "stdin.txt")
        stub = fake_opentab(self.directory.name, f"cat > {recorded}\n")
        opentab.price(["/p\twith-tab", "/q"], make_config(opentab_bin=stub))
        with open(recorded, encoding="utf-8") as handle:
            self.assertEqual(handle.read().splitlines(), ["/q"])


if __name__ == "__main__":
    unittest.main()
