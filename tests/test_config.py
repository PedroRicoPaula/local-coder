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


if __name__ == "__main__":
    unittest.main()
