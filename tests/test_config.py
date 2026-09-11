import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import config  # noqa: E402


class ConfigDir(unittest.TestCase):
    """Every test writes a real config.json into a throwaway HERDR_PLUGIN_CONFIG_DIR."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.saved = {
            key: os.environ.get(key)
            for key in ("HERDR_PLUGIN_CONFIG_DIR", "HERDR_OPENTAB_INTERVAL", "HERDR_OPENTAB_BIN")
        }
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = self.directory.name
        os.environ.pop("HERDR_OPENTAB_INTERVAL", None)
        os.environ.pop("HERDR_OPENTAB_BIN", None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write(self, values):
        with open(os.path.join(self.directory.name, "config.json"), "w", encoding="utf-8") as f:
            json.dump(values, f)


class Defaults(ConfigDir):
    def test_a_missing_file_is_not_an_error(self):
        cfg = config.load()
        self.assertEqual(cfg.token, "cost")
        self.assertEqual(cfg.warnings, [])
        # On by default, and safe: core.plan withholds a directory price from
        # every pane of a project that has more than one agent.
        self.assertTrue(cfg.project_fallback)

    def test_values_are_read(self):
        self.write({"token": "spend", "interval_secs": 30, "fallback": "off"})
        cfg = config.load()
        self.assertEqual(cfg.token, "spend")
        self.assertEqual(cfg.interval_secs, 30)
        self.assertFalse(cfg.project_fallback)


class Rejections(ConfigDir):
    def test_a_token_herdr_would_refuse_falls_back(self):
        self.write({"token": "not a token!"})
        cfg = config.load()
        self.assertEqual(cfg.token, "cost")
        self.assertTrue(cfg.warnings)

    def test_a_too_fast_interval_is_refused(self):
        self.write({"interval_secs": 0})
        cfg = config.load()
        self.assertEqual(cfg.interval_secs, config.DEFAULTS["interval_secs"])

    def test_an_idle_interval_below_the_working_one_is_lifted(self):
        self.write({"interval_secs": 45, "idle_interval_secs": 10})
        self.assertEqual(config.load().idle_interval_secs, 45)

    def test_an_out_of_range_ttl_is_dropped(self):
        self.write({"ttl_ms": 99_999_999})
        cfg = config.load()
        self.assertIsNone(cfg.ttl_ms)
        self.assertTrue(any("ttl_ms" in w for w in cfg.warnings))

    def test_an_unknown_key_is_reported_not_obeyed(self):
        self.write({"colour": "blue"})
        cfg = config.load()
        self.assertTrue(any("colour" in w for w in cfg.warnings))

    def test_broken_json_leaves_the_defaults_standing(self):
        with open(os.path.join(self.directory.name, "config.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        cfg = config.load()
        self.assertEqual(cfg.token, "cost")
        self.assertTrue(cfg.warnings)

    def test_a_bad_agents_list_prices_everything(self):
        self.write({"agents": "claude"})
        cfg = config.load()
        self.assertIsNone(cfg.agents)


class EnvOverrides(ConfigDir):
    def test_interval_env_wins_over_the_file(self):
        self.write({"interval_secs": 60})
        os.environ["HERDR_OPENTAB_INTERVAL"] = "5"
        self.assertEqual(config.load().interval_secs, 5)

    def test_bin_env_wins_over_the_file(self):
        self.write({"opentab_bin": "opentab"})
        os.environ["HERDR_OPENTAB_BIN"] = "/opt/opentab"
        self.assertEqual(config.load().opentab_bin, "/opt/opentab")


class Booleans(unittest.TestCase):
    def test_elapsed_can_be_disabled(self):
        self.assertFalse(config._coerce({"elapsed": False}, [])["elapsed"])

    def test_elapsed_rejects_a_string(self):
        warnings = []
        self.assertTrue(config._coerce({"elapsed": "false"}, warnings)["elapsed"])
        self.assertTrue(warnings)

    def test_existing_cost_token_takes_precedence_over_timer(self):
        warnings = []
        merged = config._coerce({"token": "elapsed"}, warnings)
        self.assertEqual(merged["token"], "elapsed")
        self.assertFalse(merged["elapsed"])
        self.assertTrue(warnings)

    def test_a_string_is_not_a_boolean(self):
        warnings = []
        merged = config._coerce({"strip_approx": "false"}, warnings)
        self.assertIs(merged["strip_approx"], False)
        self.assertTrue(warnings)

    def test_zero_is_not_a_boolean(self):
        warnings = []
        merged = config._coerce({"align": 0}, warnings)
        self.assertIs(merged["align"], True)
        self.assertTrue(warnings)

    def test_a_real_boolean_passes(self):
        warnings = []
        self.assertIs(config._coerce({"align": False}, warnings)["align"], False)
        self.assertEqual(warnings, [])


class NonFiniteNumbers(unittest.TestCase):
    def test_an_infinite_interval_falls_back_instead_of_raising(self):
        # `1e999` is valid JSON and parses as inf; int(inf) would raise, and
        # this module promises never to raise.
        warnings = []
        merged = config._coerce({"interval_secs": float("inf")}, warnings)
        self.assertEqual(merged["interval_secs"], config.DEFAULTS["interval_secs"])
        self.assertTrue(warnings)

    def test_a_nan_interval_falls_back(self):
        merged = config._coerce({"interval_secs": float("nan")}, [])
        self.assertEqual(merged["interval_secs"], config.DEFAULTS["interval_secs"])


if __name__ == "__main__":
    unittest.main()


class Leases(ConfigDir):
    """What `--ttl-ms` this plugin actually sends, and why."""

    def test_the_derived_lease_outlasts_three_idle_rounds(self):
        self.write({"idle_interval_secs": 60})
        cfg = config.load()
        self.assertEqual(cfg.lease_ms, 180_000)
        self.assertEqual(cfg.renew_after_secs, 60.0)

    def test_a_lease_can_never_exceed_what_herdr_accepts(self):
        # Herdr rejects a ttl over METADATA_TTL_MAX_MS outright, and a rejected
        # report is not a stale price -- it is no price at all.
        # The largest interval the config accepts is exactly the one whose
        # three-round lease still fits under herdr's ceiling.
        self.write({"idle_interval_secs": config._MAX_INTERVAL})
        self.assertEqual(config.load().lease_ms, config.MAX_TTL_MS)

    def test_a_ttl_herdr_would_refuse_falls_back_to_a_derived_one(self):
        self.write({"ttl_ms": 999_999_999})
        cfg = config.load()
        self.assertEqual(cfg.lease_ms, 180_000)
        self.assertTrue(any("ttl_ms" in warning for warning in cfg.warnings))

    def test_zero_means_no_lease(self):
        self.write({"ttl_ms": 0})
        cfg = config.load()
        self.assertIsNone(cfg.lease_ms)
        self.assertEqual(cfg.renew_after_secs, float("inf"))

    def test_a_lease_shorter_than_two_rounds_is_called_out(self):
        self.write({"ttl_ms": 1000, "idle_interval_secs": 60})
        self.assertTrue(any("blank between updates" in w for w in config.load().warnings))


class HugeNumbers(ConfigDir):
    def test_an_absurd_interval_is_rejected_without_raising(self):
        # math.isfinite() converts to float and raises OverflowError on a
        # 400-digit int; this module promises never to raise, whatever is in
        # the file. And 10**400 seconds is not a poll interval.
        self.write({"interval_secs": 10**400})
        cfg = config.load()
        self.assertEqual(cfg.interval_secs, config.DEFAULTS["interval_secs"])
        self.assertTrue(cfg.warnings)

    def test_an_interval_whose_lease_would_not_fit_is_rejected(self):
        # A longer round than this would derive a lease herdr caps below one
        # round, so every price would lapse before the round that renews it.
        self.write({"interval_secs": config._MAX_INTERVAL + 1})
        self.assertEqual(config.load().interval_secs, config.DEFAULTS["interval_secs"])
        self.write(
            {"interval_secs": config._MAX_INTERVAL, "idle_interval_secs": config._MAX_INTERVAL}
        )
        self.assertEqual(config.load().interval_secs, config._MAX_INTERVAL)


if __name__ == "__main__":
    unittest.main()
