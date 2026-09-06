import tempfile
import unittest
from pathlib import Path

from context.relevance import rank_source_files, select_files_for_task


class TestRankSourceFiles(unittest.TestCase):
    def test_task_filename_beats_shallow_sort_order(self):
        """The old heuristic sent the 5 shallowest paths. A real task about
        authentication must pick a deep auth file over aaa.py at the root."""
        with tempfile.TemporaryDirectory() as root:
            for name in ("aaa.py", "bbb.py", "ccc.py", "ddd.py", "eee.py"):
                Path(root, name).write_text("# unrelated filler\n")
            pkg = Path(root, "pkg")
            pkg.mkdir()
            Path(pkg, "auth.py").write_text(
                "def login():\n    return 'ok'\n\ndef refresh_token():\n    pass\n"
            )
            ranked = rank_source_files(root, "Fix the authentication refresh_token bug")
            self.assertTrue(ranked)
            self.assertEqual(ranked[0].path, "pkg/auth.py")
            self.assertGreater(ranked[0].score, ranked[-1].score)

    def test_select_respects_limit(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(8):
                Path(root, f"f{i}.py").write_text(f"x = {i}\n")
            selected = select_files_for_task(root, "update f7", limit=3)
            self.assertEqual(len(selected), 3)
            self.assertEqual(selected[0].path, "f7.py")

    def test_empty_project_returns_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(rank_source_files(root, "anything"), [])


class TestFormatSelection(unittest.TestCase):
    def test_report_includes_budget_and_paths(self):
        from context.relevance import ScoredFile, format_selection
        text = format_selection(
            [ScoredFile("pkg/auth.py", 10.0), ScoredFile("aaa.py", 1.0)],
            budget_chars=17800,
            used_chars=400,
            num_ctx=8192,
            pinned=False,
        )
        self.assertIn("400 / 17800", text)
        self.assertIn("8192", text)
        self.assertIn("pkg/auth.py", text)
        self.assertIn("ranked by task keywords", text)
