import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestApplyWriteByteFidelity(unittest.TestCase):
    def test_overwriting_a_crlf_file_keeps_crlf(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "win.txt")
            target.write_bytes(b"one\r\ntwo\r\n")
            write = actions.FileWrite(path="win.txt", content="one\nthree\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(target.read_bytes(), b"one\r\nthree\r\n")

    def test_overwriting_a_bom_file_keeps_the_bom(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "bom.txt")
            target.write_bytes(b"\xef\xbb\xbfold\n")
            write = actions.FileWrite(path="bom.txt", content="new\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfnew\n")

    def test_new_file_gets_lf_and_no_bom(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path="fresh.txt", content="a\r\nb\r\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(Path(root, "fresh.txt").read_bytes(), b"a\nb\n")

    def test_refuses_to_overwrite_a_non_utf8_file(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "latin1.txt")
            target.write_bytes(b"caf\xe9\n")
            write = actions.FileWrite(path="latin1.txt", content="cafe\n")
            self.assertFalse(actions.apply_write(root, write, confirm=False))
            self.assertEqual(target.read_bytes(), b"caf\xe9\n")

    def test_identical_content_is_skipped_without_prompting(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "same.txt")
            target.write_text("x = 1\n")
            write = actions.FileWrite(path="same.txt", content="x = 1\n")
            with mock.patch("builtins.input", side_effect=AssertionError("should never prompt")):
                self.assertFalse(actions.apply_write(root, write, confirm=True))

    def test_overwrite_shows_a_unified_diff_before_the_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("old\n")
            write = actions.FileWrite(path="a.py", content="new\n")
            buffer = io.StringIO()
            with mock.patch("ui._enabled", return_value=False), \
                 contextlib.redirect_stdout(buffer), \
                 mock.patch("builtins.input", return_value="n"):
                actions.apply_write(root, write, confirm=True)
            printed = buffer.getvalue()
            self.assertIn("--- a.py", printed)
            self.assertIn("-old", printed)
            self.assertIn("+new", printed)

    def test_a_huge_diff_is_capped(self):
        old = "".join(f"line {i}\n" for i in range(1000))
        new = "".join(f"changed {i}\n" for i in range(1000))
        capped = actions._cap_diff(actions.unified_diff("big.txt", old, new))
        self.assertIn("diff lines elided", capped)
        self.assertLessEqual(len(capped.splitlines()), actions.MAX_DIFF_LINES + 1)


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


class TestMutationPathPolicy(unittest.TestCase):
    def test_write_refuses_git_internals(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".git").mkdir()
            write = actions.FileWrite(path=".git/config", content="[core]\n")
            self.assertFalse(actions.apply_write(root, write, confirm=False))
            self.assertFalse(Path(root, ".git/config").exists())

    def test_write_refuses_a_credential_file(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path=".env", content="TOKEN=abc\n")
            self.assertFalse(actions.apply_write(root, write, confirm=False))
            self.assertFalse(Path(root, ".env").exists())

    def test_write_allows_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path=".gitignore", content="__pycache__/\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(Path(root, ".gitignore").read_text(), "__pycache__/\n")

    def test_delete_refuses_git_internals(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".git").mkdir()
            Path(root, ".git/config").write_text("[core]\n")
            self.assertFalse(actions.apply_delete(root, ".git/config", confirm=False))
            self.assertTrue(Path(root, ".git/config").exists())

    def test_edit_refuses_credential_file_with_structured_error(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".env").write_text("TOKEN=abc\n")
            result = actions.apply_edit(
                root, actions.FileEdit(path=".env", search="abc", replace="xyz"), confirm=False
            )
            self.assertFalse(result.ok)
            self.assertIn("credential or key file", result.error)
            self.assertIn("outside localcoder", result.error)
            self.assertEqual(Path(root, ".env").read_text(), "TOKEN=abc\n")

    def test_write_refuses_empty_path(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(actions.apply_write(root, actions.FileWrite(path="  ", content="x"),
                                                 confirm=False))


if __name__ == "__main__":
    unittest.main()
