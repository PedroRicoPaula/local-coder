import tempfile
import unittest
from pathlib import Path

from context.tree import build_tree, list_source_files


def _make_project(root: str) -> None:
    Path(root, "b_module").mkdir()
    Path(root, "a_module").mkdir()
    Path(root, "b_module", "second.py").write_text("x = 2\n")
    Path(root, "a_module", "first.py").write_text("x = 1\n")
    Path(root, "top.py").write_text("x = 0\n")
    Path(root, ".env").write_text("SECRET=1\n")
    Path(root, "id_rsa").write_text("not a real key\n")
    Path(root, "node_modules").mkdir()
    Path(root, "node_modules", "junk.py").write_text("x = 99\n")


class TestBuildTree(unittest.TestCase):
    def test_no_root_name_line(self):
        # The bug this session hit and fixed: the root directory's own name
        # must not appear as a tree line, or a model echoes it into write
        # paths ("myproject/myproject/file.py").
        with tempfile.TemporaryDirectory() as root:
            _make_project(root)
            tree = build_tree(root)
            root_name = Path(root).name
            self.assertNotIn(f"{root_name}/", tree.splitlines()[0] if tree.splitlines() else "")

    def test_always_ignored_dirs_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            _make_project(root)
            tree = build_tree(root)
            self.assertNotIn("node_modules", tree)
            self.assertNotIn("junk.py", tree)


class TestListSourceFiles(unittest.TestCase):
    def test_sorted_shallowest_first(self):
        with tempfile.TemporaryDirectory() as root:
            _make_project(root)
            files = list_source_files(root)
            self.assertEqual(files[0], "top.py")  # shallowest path sorts first

    def test_denylisted_files_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            _make_project(root)
            files = list_source_files(root, extensions=(".py", ""))
            self.assertNotIn("id_rsa", files)

    def test_env_never_listed_as_source(self):
        with tempfile.TemporaryDirectory() as root:
            _make_project(root)
            files = list_source_files(root)
            self.assertFalse(any(".env" in f for f in files))


if __name__ == "__main__":
    unittest.main()
