import subprocess
import tempfile
import unittest
from pathlib import Path

import gitsafety


def _init_repo(root: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@localcoder"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "localcoder-test"], cwd=root, check=True)


class TestIsGitRepo(unittest.TestCase):
    def test_non_repo_is_false(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(gitsafety.is_git_repo(root))

    def test_real_repo_is_true(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            self.assertTrue(gitsafety.is_git_repo(root))


class TestCommitAndUndo(unittest.TestCase):
    def test_commit_change_never_raises_outside_repo(self):
        with tempfile.TemporaryDirectory() as root:
            gitsafety.commit_change(root, "should be a silent no-op", ["a.py"])  # must not raise

    def test_commit_change_stages_only_the_named_path(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "seed.txt").write_text("seed\n")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            # The user's own separate work, deliberately staged.
            Path(root, "b.py").write_text("users_work = True\n")
            subprocess.run(["git", "add", "b.py"], cwd=root, check=True)

            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])

            committed = subprocess.run(
                ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.split()
            self.assertEqual(committed, ["a.py"])

            still_staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.split()
            self.assertEqual(still_staged, ["b.py"])

    def test_commit_change_creates_no_commit_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            before = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                                    cwd=root, capture_output=True, text=True, check=True).stdout
            gitsafety.commit_change(root, "write a.py", ["a.py"])  # content identical
            after = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                                   cwd=root, capture_output=True, text=True, check=True).stdout
            self.assertEqual(before, after)

    def test_commit_then_undo_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            # A prior human commit, same as any real project localcoder gets
            # pointed at -- the localcoder commit being undone is not the
            # repo's first, which is the realistic case (see the dedicated
            # first-commit test below for the edge case where it is).
            Path(root, "README.md").write_text("preexisting\n")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            target = Path(root) / "a.py"
            target.write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])

            ok, message = gitsafety.undo_last(root)
            self.assertTrue(ok, message)
            self.assertFalse(target.exists())
            self.assertTrue(Path(root, "README.md").exists())  # prior history untouched

    def test_undo_refuses_the_repos_very_first_commit(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            target = Path(root) / "a.py"
            target.write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])  # this is the ONLY commit

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertTrue(target.exists())  # refused, nothing touched

    def test_undo_refuses_a_commit_it_did_not_make(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "a human's own commit"], cwd=root, check=True)

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertTrue(Path(root, "a.py").exists())


if __name__ == "__main__":
    unittest.main()
