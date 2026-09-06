import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gitsafety


def _init_repo(root: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@localcoder"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "localcoder-test"], cwd=root, check=True)


def _count_commits(root: str) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"],
                              cwd=root, capture_output=True, text=True, check=True).stdout)


class TestIsGitRepo(unittest.TestCase):
    def test_non_repo_is_false(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(gitsafety.is_git_repo(root))

    def test_real_repo_is_true(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            self.assertTrue(gitsafety.is_git_repo(root))


class TestCommitChange(unittest.TestCase):
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


class TestUndoLast(unittest.TestCase):
    def test_undo_adds_a_revert_commit_and_restores_content(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "README.md").write_text("preexisting\n")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            before = _count_commits(root)

            ok, message = gitsafety.undo_last(root)
            self.assertTrue(ok, message)
            self.assertEqual(_count_commits(root), before + 1)
            new_head = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertIn(f"(new commit {new_head})", message)
            self.assertFalse(Path(root, "a.py").exists())
            self.assertTrue(Path(root, "README.md").exists())

    def test_undo_works_on_the_repos_root_commit(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])

            ok, message = gitsafety.undo_last(root)
            self.assertTrue(ok, message)
            self.assertFalse(Path(root, "a.py").exists())

    def test_head_lookup_failure_after_revert_still_reports_success(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "README.md").write_text("preexisting\n")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            before = _count_commits(root)

            real_git = gitsafety._git

            def fail_head_lookup(project_root, args, timeout=10):
                if args == ["rev-parse", "--short", "HEAD"]:
                    raise subprocess.TimeoutExpired(["git", *args], timeout)
                return real_git(project_root, args, timeout)

            with mock.patch.object(gitsafety, "_git", side_effect=fail_head_lookup):
                ok, message = gitsafety.undo_last(root)

            self.assertTrue(ok, message)
            self.assertEqual(message, "reverted: localcoder: write a.py")
            self.assertEqual(_count_commits(root), before + 1)
            self.assertFalse(Path(root, "a.py").exists())
            self.assertTrue(Path(root, "README.md").exists())

    def test_undo_refuses_when_an_affected_path_is_dirty(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            Path(root, "a.py").write_text("x = 999  # my own edit\n")

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertIn("would be overwritten", message)
            self.assertIn("a.py", message)
            self.assertEqual(Path(root, "a.py").read_text(), "x = 999  # my own edit\n")

    def test_a_conflicting_revert_is_rolled_back_completely(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("line1\nline2\nline3\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            Path(root, "a.py").write_text("line1\nLOCALCODER\nline3\n")
            gitsafety.commit_change(root, "edit a.py", ["a.py"])

            Path(root, "a.py").write_text("line1\nHUMAN AGAIN\nline3\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "my own change"], cwd=root, check=True)

            before = _count_commits(root)
            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertIn("conflicted and was rolled back", message)
            self.assertEqual(_count_commits(root), before)

            marker = subprocess.run(["git", "rev-parse", "--git-path", "REVERT_HEAD"],
                                    cwd=root, capture_output=True, text=True, check=True)
            self.assertFalse(Path(root, marker.stdout.strip()).exists())
            status = subprocess.run(["git", "status", "--porcelain"],
                                    cwd=root, capture_output=True, text=True, check=True)
            self.assertNotIn("UU", status.stdout)
            self.assertEqual(Path(root, "a.py").read_text(), "line1\nHUMAN AGAIN\nline3\n")

    def test_abort_failure_never_claims_conflict_was_rolled_back(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("line1\nline2\nline3\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            Path(root, "a.py").write_text("line1\nLOCALCODER\nline3\n")
            gitsafety.commit_change(root, "edit a.py", ["a.py"])
            Path(root, "a.py").write_text("line1\nHUMAN AGAIN\nline3\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "my own change"], cwd=root, check=True)

            real_git = gitsafety._git

            def fail_abort(project_root, args, timeout=10):
                if args == ["revert", "--abort"]:
                    return subprocess.CompletedProcess(
                        ["git", *args], 1, "", "simulated abort failure",
                    )
                return real_git(project_root, args, timeout)

            with mock.patch.object(gitsafety, "_git", side_effect=fail_abort):
                ok, message = gitsafety.undo_last(root)

            self.assertFalse(ok)
            self.assertNotIn("was rolled back", message)
            self.assertIn("may still have a revert in progress", message)
            marker = subprocess.run(
                ["git", "rev-parse", "--git-path", "REVERT_HEAD"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertTrue(Path(root, marker).exists())

    def test_undo_refuses_a_human_commit(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "a human's own commit"],
                           cwd=root, check=True)

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertTrue(Path(root, "a.py").exists())

    def test_two_undos_revert_two_different_localcoder_commits(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("a\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            Path(root, "b.py").write_text("b\n")
            gitsafety.commit_change(root, "write b.py", ["b.py"])

            ok1, msg1 = gitsafety.undo_last(root)
            ok2, msg2 = gitsafety.undo_last(root)
            self.assertTrue(ok1, msg1)
            self.assertTrue(ok2, msg2)
            self.assertFalse(Path(root, "a.py").exists())
            self.assertFalse(Path(root, "b.py").exists())
            self.assertIn("write b.py", msg1)
            self.assertIn("write a.py", msg2)

    def test_undo_reports_nothing_left_when_all_localcoder_commits_are_reverted(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("a\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            self.assertTrue(gitsafety.undo_last(root)[0])

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertIn("left to undo", message)


if __name__ == "__main__":
    unittest.main()
