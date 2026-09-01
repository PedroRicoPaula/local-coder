import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from llm.ollama_client import Usage


class _FakeCCE:
    def __init__(self, available=False, compress_result=None):
        self._available = available
        self._compress_result = compress_result

    @property
    def available(self):
        return self._available

    def compress_file(self, path, task_description=""):
        return self._compress_result


class TestAssembleFileContext(unittest.TestCase):
    def test_raw_read_when_cce_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("print('hi')\n")
            cce = _FakeCCE(available=False)
            context, warnings = main.assemble_file_context(cce, root, ["a.py"], budget_chars=1000)
            self.assertIn("print('hi')", context)
            self.assertEqual(warnings, [])

    def test_cce_compression_used_when_available(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("print('hi')\n")
            cce = _FakeCCE(available=True, compress_result="COMPRESSED")
            context, warnings = main.assemble_file_context(cce, root, ["a.py"], budget_chars=1000)
            self.assertIn("COMPRESSED", context)
            self.assertEqual(warnings, [])

    def test_denied_path_produces_warning_and_is_skipped(self):
        cce = _FakeCCE(available=False)
        context, warnings = main.assemble_file_context(cce, "/tmp", [".env"], budget_chars=1000)
        self.assertEqual(context, "")
        self.assertEqual(len(warnings), 1)
        self.assertIn(".env", warnings[0])

    def test_truncates_when_over_budget(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "big.py").write_text("x" * 1000)
            cce = _FakeCCE(available=False)
            context, warnings = main.assemble_file_context(cce, root, ["big.py"], budget_chars=300)
            self.assertIn("truncated to fit budget", context)
            self.assertTrue(any("truncated" in w for w in warnings))

    def test_skips_when_budget_already_exhausted(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("x" * 50)
            (Path(root) / "b.py").write_text("y" * 50)
            cce = _FakeCCE(available=False)
            context, warnings = main.assemble_file_context(cce, root, ["a.py", "b.py"], budget_chars=60)
            self.assertTrue(any("skipped entirely" in w for w in warnings))


class TestUsageFromChunk(unittest.TestCase):
    def test_full_chunk(self):
        chunk = {
            "done": True,
            "prompt_eval_count": 100,
            "eval_count": 50,
            "prompt_eval_duration": 1_000_000_000,
            "eval_duration": 2_000_000_000,
            "load_duration": 500_000_000,
            "total_duration": 3_500_000_000,
        }
        usage = Usage.from_chunk(chunk)
        self.assertEqual(usage.total_tokens, 150)

    def test_missing_fields_give_none_total(self):
        usage = Usage.from_chunk({"done": True})
        self.assertIsNone(usage.total_tokens)


class TestStreamAndPrint(unittest.TestCase):
    def test_accumulates_response_and_returns_usage(self):
        chunks = [
            {"response": "hi", "done": False},
            {"response": " there", "done": True, "prompt_eval_count": 10, "eval_count": 5},
        ]
        with mock.patch("ui._enabled", return_value=False):
            text, usage = main.stream_and_print(iter(chunks))
        self.assertEqual(text, "hi there")
        self.assertEqual(usage.prompt_eval_count, 10)
        self.assertEqual(usage.eval_count, 5)

    def test_empty_stream_returns_empty_text_and_no_usage(self):
        with mock.patch("ui._enabled", return_value=False):
            text, usage = main.stream_and_print(iter([]))
        self.assertEqual(text, "")
        self.assertIsNone(usage)


if __name__ == "__main__":
    unittest.main()
