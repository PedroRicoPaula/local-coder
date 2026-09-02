import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config


class TestLoadConfig(unittest.TestCase):
    def test_defaults_present(self):
        cfg = config.load_config()
        self.assertIn("model", cfg)
        self.assertIn("num_ctx", cfg)
        self.assertIn("ollama_host", cfg)
        self.assertIn("max_total_context_chars", cfg)

    def test_json_overrides_merge_over_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_file = Path(tmp) / "config.json"
            override_file.write_text(json.dumps({"num_ctx": 1234}))
            with mock.patch.object(config, "CONFIG_FILE", override_file):
                cfg = config.load_config()
            self.assertEqual(cfg["num_ctx"], 1234)
            self.assertIn("model", cfg)  # untouched defaults still present

    def test_malformed_json_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_file = Path(tmp) / "config.json"
            bad_file.write_text("{not valid json")
            with mock.patch.object(config, "CONFIG_FILE", bad_file):
                cfg = config.load_config()
            self.assertEqual(cfg["num_ctx"], config.DEFAULTS["num_ctx"])


class TestExplicitModelInConfigFile(unittest.TestCase):
    def test_none_when_no_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "CONFIG_FILE", Path(tmp) / "missing.json"):
                self.assertIsNone(config.explicit_model_in_config_file())

    def test_none_when_config_file_has_no_model_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_file = Path(tmp) / "config.json"
            override_file.write_text(json.dumps({"num_ctx": 1234}))
            with mock.patch.object(config, "CONFIG_FILE", override_file):
                self.assertIsNone(config.explicit_model_in_config_file())

    def test_returns_explicit_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_file = Path(tmp) / "config.json"
            override_file.write_text(json.dumps({"model": "qwen3:8b"}))
            with mock.patch.object(config, "CONFIG_FILE", override_file):
                self.assertEqual(config.explicit_model_in_config_file(), "qwen3:8b")

    def test_malformed_json_returns_none_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_file = Path(tmp) / "config.json"
            bad_file.write_text("{not valid json")
            with mock.patch.object(config, "CONFIG_FILE", bad_file):
                self.assertIsNone(config.explicit_model_in_config_file())


class TestModelProfiles(unittest.TestCase):
    def test_default_profiles_present(self):
        cfg = config.load_config()
        self.assertIn("fast", cfg["model_profiles"])
        self.assertIn("quality", cfg["model_profiles"])
        self.assertEqual(cfg["default_profile"], "quality")

    def test_reuse_context_across_turns_defaults_true(self):
        cfg = config.load_config()
        self.assertTrue(cfg["reuse_context_across_turns"])


class TestDeriveMaxContextChars(unittest.TestCase):
    def test_derive_max_context_chars_bound(self):
        derived = config.derive_max_context_chars(8192)
        # Sanity bound: must be well under num_ctx * CHARS_PER_TOKEN (the
        # unreserved fraction only), and clearly less than the old flat
        # 60000 default this replaces -- the whole point of deriving it.
        self.assertLess(derived, 8192 * config.CHARS_PER_TOKEN)
        self.assertLess(derived, 60000)
        self.assertGreater(derived, 0)

    def test_auto_derives_budget_when_num_ctx_changes_and_chars_not_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_file = Path(tmp) / "config.json"
            override_file.write_text(json.dumps({"num_ctx": 1234}))
            with mock.patch.object(config, "CONFIG_FILE", override_file):
                cfg = config.load_config()
            self.assertEqual(cfg["max_total_context_chars"], config.derive_max_context_chars(1234))

    def test_respects_explicit_override_even_if_it_exceeds_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_file = Path(tmp) / "config.json"
            override_file.write_text(json.dumps({"num_ctx": 1234, "max_total_context_chars": 99999}))
            with mock.patch.object(config, "CONFIG_FILE", override_file):
                cfg = config.load_config()
            self.assertEqual(cfg["max_total_context_chars"], 99999)


if __name__ == "__main__":
    unittest.main()
