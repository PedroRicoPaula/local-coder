import tempfile
import unittest
from pathlib import Path

import actions


class TestExtraction(unittest.TestCase):
    def test_extract_write_basic(self):
        text = "prose\n```write:hello.py\nprint('hi')\n```\nmore prose"
        writes = actions.extract_writes(text)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].path, "hello.py")
        self.assertEqual(writes[0].content, "print('hi')\n")

    def test_nested_fence_needs_four_backticks(self):
        # The bug this session actually hit and fixed: a write block whose
        # own content contains a ``` fence must not truncate there when the
        # outer fence uses four backticks.
        text = (
            "````write:README.md\n"
            "# example\n"
            "```python\n"
            "print(\"inner fence\")\n"
            "```\n"
            "````"
        )
        writes = actions.extract_writes(text)
        self.assertEqual(len(writes), 1)
        self.assertIn("```python", writes[0].content)
        self.assertIn('print("inner fence")', writes[0].content)

    def test_three_backtick_write_stops_at_first_inner_fence(self):
        # Documents the known limitation (README's "Why no JSON tool-calling"):
        # a three-backtick outer fence closes at the FIRST inner ``` it sees.
        # This is the parser behaving correctly, not a bug -- the model choosing
        # not to escalate to four backticks is the separate, undocumented-away
        # risk.
        text = "```write:x.md\nkeep this\n```\nlost this\n```\n"
        writes = actions.extract_writes(text)
        self.assertEqual(writes[0].content, "keep this\n")

    def test_extract_delete(self):
        text = "```delete:old_file.py\n```"
        self.assertEqual(actions.extract_deletes(text), ["old_file.py"])

    def test_extract_run(self):
        text = "```run\npython3 -m pytest\n```"
        self.assertEqual(actions.extract_runs(text), ["python3 -m pytest"])

    def test_extract_fetch(self):
        text = "```fetch:https://example.com/page\n```"
        self.assertEqual(actions.extract_fetches(text), ["https://example.com/page"])

    def test_extract_shell_suggestion(self):
        text = "```shell\nls -la\n```"
        self.assertEqual(actions.extract_shell_suggestions(text), ["ls -la"])

    def test_extract_search(self):
        text = "```search:python asyncio timeout\n```"
        self.assertEqual(actions.extract_searches(text), ["python asyncio timeout"])

    def test_extract_symbol_request(self):
        text = "```symbol:src/util.py#parse_config\n```"
        self.assertEqual(actions.extract_symbol_requests(text), [("src/util.py", "parse_config")])

    def test_extract_symbol_request_malformed_skipped(self):
        text = "```symbol:src/util.py\n```"
        self.assertEqual(actions.extract_symbol_requests(text), [])

    def test_strip_action_blocks_leaves_readable_prose(self):
        text = "Adding a function.\n```write:a.py\ndef f(): pass\n```\nDone."
        prose = actions.strip_action_blocks(text)
        self.assertNotIn("def f()", prose)
        self.assertIn("Adding a function.", prose)
        self.assertIn("Done.", prose)


class TestApplyWrite(unittest.TestCase):
    def test_write_creates_file(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path="sub/dir/hello.py", content="print('hi')\n")
            ok = actions.apply_write(root, write, confirm=False)
            self.assertTrue(ok)
            self.assertEqual((Path(root) / "sub/dir/hello.py").read_text(), "print('hi')\n")

    def test_write_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path="../../etc/passwd", content="evil")
            ok = actions.apply_write(root, write, confirm=False)
            self.assertFalse(ok)


class TestApplyDelete(unittest.TestCase):
    def test_delete_removes_file(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "gone.py"
            target.write_text("temp")
            ok = actions.apply_delete(root, "gone.py", confirm=False)
            self.assertTrue(ok)
            self.assertFalse(target.exists())

    def test_delete_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            ok = actions.apply_delete(root, "../../etc/passwd", confirm=False)
            self.assertFalse(ok)

    def test_delete_refuses_missing_file(self):
        with tempfile.TemporaryDirectory() as root:
            ok = actions.apply_delete(root, "nope.py", confirm=False)
            self.assertFalse(ok)

    def test_delete_refuses_directory(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "adir").mkdir()
            ok = actions.apply_delete(root, "adir", confirm=False)
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
