import os
import tempfile
import unittest
from pathlib import Path

import pathpolicy


class TestResolveForMutation(unittest.TestCase):
    def test_allows_an_ordinary_relative_path(self):
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, "src/app.py")
            self.assertTrue(decision.ok)
            self.assertEqual(decision.relpath, "src/app.py")
            self.assertEqual(decision.reason, "")
            self.assertEqual(decision.suggestion, "")
            self.assertEqual(decision.path, Path(root).resolve() / "src" / "app.py")

    def test_rejects_empty_path(self):
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, "   ")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "empty path")
            self.assertIsNone(decision.path)
            self.assertEqual(decision.relpath, "")

    def test_rejects_traversal_outside_root(self):
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, "../../etc/passwd")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "path is outside project root")
            self.assertEqual(decision.suggestion, "use a path relative to the project root")

    def test_rejects_symlink_pointing_outside_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            Path(outside, "target.txt").write_text("secret\n")
            os.symlink(Path(outside, "target.txt"), Path(root, "link.txt"))
            decision = pathpolicy.resolve_for_mutation(root, "link.txt")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "path is outside project root")

    def test_rejects_git_internals(self):
        with tempfile.TemporaryDirectory() as root:
            for raw in (".git/config", ".git/hooks/pre-commit", ".git", "sub/.git/hooks/pre-commit",
                        ".GIT/config"):
                decision = pathpolicy.resolve_for_mutation(root, raw)
                self.assertFalse(decision.ok, raw)
                self.assertEqual(decision.reason, "path is inside the .git directory", raw)
                self.assertEqual(
                    decision.suggestion,
                    "never modify git internals; change tracked files instead",
                )

    def test_allows_git_adjacent_files(self):
        with tempfile.TemporaryDirectory() as root:
            for raw in (".gitignore", ".gitattributes", ".gitmodules",
                        ".github/workflows/ci.yml"):
                decision = pathpolicy.resolve_for_mutation(root, raw)
                self.assertTrue(decision.ok, raw)

    def test_rejects_credential_and_key_files(self):
        with tempfile.TemporaryDirectory() as root:
            for raw in (".env", ".env.local", "id_ecdsa", "deploy.pem", "secrets.yaml",
                        "conf/.env.production"):
                decision = pathpolicy.resolve_for_mutation(root, raw)
                self.assertFalse(decision.ok, raw)
                self.assertEqual(decision.reason, "path looks like a credential or key file", raw)
                self.assertEqual(
                    decision.suggestion,
                    "create or edit credential files by hand, outside localcoder",
                )

    def test_rejects_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "adir").mkdir()
            decision = pathpolicy.resolve_for_mutation(root, "adir")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "path is a directory")
            self.assertEqual(decision.suggestion, "name a file, not a directory")

    def test_git_check_wins_over_denylist_check(self):
        """Check order is fixed: .git component (3) is tested before the
        credential denylist (4), so a file inside .git named like a secret
        reports the .git reason."""
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, ".git/.env")
            self.assertEqual(decision.reason, "path is inside the .git directory")


if __name__ == "__main__":
    unittest.main()
