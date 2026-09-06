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

    def test_extract_edit_search_replace(self):
        text = (
            "```edit:calc.py\n"
            "<<<<<<< SEARCH\n"
            "    return a / b\n"
            "=======\n"
            "    if b == 0:\n"
            "        raise ValueError('cannot divide by zero')\n"
            "    return a / b\n"
            ">>>>>>> REPLACE\n"
            "```"
        )
        edits = actions.extract_edits(text)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].path, "calc.py")
        self.assertEqual(edits[0].search, "    return a / b\n")
        self.assertIn("ValueError", edits[0].replace)

    def test_extract_edit_malformed_missing_markers_skipped(self):
        text = "```edit:calc.py\njust some text without markers\n```"
        self.assertEqual(actions.extract_edits(text), [])

    def test_strip_action_blocks_leaves_readable_prose(self):
        text = "Adding a function.\n```write:a.py\ndef f(): pass\n```\nDone."
        prose = actions.strip_action_blocks(text)
        self.assertNotIn("def f()", prose)
        self.assertIn("Adding a function.", prose)
        self.assertIn("Done.", prose)

    def test_strip_also_hides_edit_blocks(self):
        text = "Patching.\n```edit:a.py\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n```\nDone."
        prose = actions.strip_action_blocks(text)
        self.assertNotIn("<<<<<<< SEARCH", prose)


class TestFormatActionError(unittest.TestCase):
    def test_error_block_is_structured_for_a_small_model(self):
        msg = actions.format_action_error(
            action="edit",
            reason="search block matched 0 times",
            path="src/auth.py",
            suggestion="inspect the current file before retrying",
        )
        self.assertIn("ERROR:", msg)
        self.assertIn("edit failed", msg)
        self.assertIn("search block matched 0 times", msg)
        self.assertIn("src/auth.py", msg)
        self.assertIn("inspect the current file", msg)


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


class TestApplyEdit(unittest.TestCase):
    def _edit(self, search: str, replace: str, path: str = "calc.py") -> actions.FileEdit:
        return actions.FileEdit(path=path, search=search, replace=replace)

    def test_edit_replaces_unique_snippet(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "calc.py").write_text("def divide(a, b):\n    return a / b\n")
            result = actions.apply_edit(
                root, self._edit("    return a / b\n", "    return a * b\n"), confirm=False
            )
            self.assertTrue(result.ok)
            self.assertIsNone(result.error)
            self.assertEqual(
                (Path(root) / "calc.py").read_text(),
                "def divide(a, b):\n    return a * b\n",
            )

    def test_edit_refuses_empty_search(self):
        with tempfile.TemporaryDirectory() as root:
            original = "x = 1\n"
            (Path(root) / "a.py").write_text(original)
            result = actions.apply_edit(
                root, self._edit("", "y\n", path="a.py"), confirm=False
            )
            self.assertFalse(result.ok)
            self.assertIn("empty search", result.error)
            self.assertEqual((Path(root) / "a.py").read_text(), original)

    def test_edit_refuses_zero_matches_without_writing(self):
        with tempfile.TemporaryDirectory() as root:
            original = "def divide(a, b):\n    return a / b\n"
            (Path(root) / "calc.py").write_text(original)
            result = actions.apply_edit(
                root, self._edit("this is not in the file\n", "x\n"), confirm=False
            )
            self.assertFalse(result.ok)
            self.assertIn("matched 0 times", result.error)
            self.assertEqual((Path(root) / "calc.py").read_text(), original)

    def test_edit_refuses_ambiguous_match(self):
        with tempfile.TemporaryDirectory() as root:
            original = "x = 1\nx = 1\n"
            (Path(root) / "a.py").write_text(original)
            result = actions.apply_edit(
                root, self._edit("x = 1\n", "x = 2\n", path="a.py"), confirm=False
            )
            self.assertFalse(result.ok)
            self.assertIn("matched 2 times", result.error)
            self.assertEqual((Path(root) / "a.py").read_text(), original)

    def test_edit_refuses_missing_file(self):
        with tempfile.TemporaryDirectory() as root:
            result = actions.apply_edit(
                root, self._edit("a", "b", path="nope.py"), confirm=False
            )
            self.assertFalse(result.ok)
            self.assertIn("does not exist", result.error)

    def test_edit_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            result = actions.apply_edit(
                root, self._edit("a", "b", path="../../etc/passwd"), confirm=False
            )
            self.assertFalse(result.ok)
            self.assertIn("outside project root", result.error)

    def test_unified_diff_shows_removed_and_added_lines(self):
        diff = actions.unified_diff("a.py", "old\n", "new\n")
        self.assertIn("--- a.py", diff)
        self.assertIn("+++ a.py", diff)
        self.assertIn("-old", diff)
        self.assertIn("+new", diff)


if __name__ == "__main__":
    unittest.main()
