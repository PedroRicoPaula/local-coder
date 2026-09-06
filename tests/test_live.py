"""The one true end-to-end test: spawns main.py for real, against a real
running Ollama, and checks it actually completes a task correctly. Slow
(CPU-only inference: minutes, not seconds) and requires 'ollama-tuned' (or
plain 'ollama serve') to already be up with qwen2.5-coder:7b pulled --
skipped by default so the rest of the suite stays fast and dependency-free.

Run explicitly:  LOCALCODER_LIVE_TESTS=1 python3 -m unittest tests.test_live
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_REASON = "set LOCALCODER_LIVE_TESTS=1 to run (needs Ollama up, takes minutes)"


@unittest.skipUnless(os.environ.get("LOCALCODER_LIVE_TESTS") == "1", SKIP_REASON)
class TestLiveEndToEnd(unittest.TestCase):
    def test_fixes_a_real_bug(self):
        with tempfile.TemporaryDirectory() as project:
            calc = Path(project, "calc.py")
            calc.write_text("def divide(a, b):\n    return a / b\n")

            script = (
                "/files calc.py\n"
                "Fix the divide function so it raises a clear ValueError "
                "instead of a ZeroDivisionError when b is 0.\n"
                "y\n"
                "/quit\n"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "main.py")],
                cwd=project, input=script, capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            # Run the actual resulting code rather than grepping its text --
            # a real correctness check, not a string match on formatting.
            sys.path.insert(0, project)
            import calc as fixed_calc  # noqa: E402 -- must import after path insert

            self.assertEqual(fixed_calc.divide(4, 2), 2)
            with self.assertRaises(ValueError):
                fixed_calc.divide(1, 0)

    def test_ranks_relevant_file_without_files_pin(self):
        """Without /files, context used to be the 5 shallowest paths. A
        decoy-filled tree must still get the auth module into context so
        the model can fix it."""
        with tempfile.TemporaryDirectory() as project:
            for name in ("aaa.py", "bbb.py", "ccc.py", "ddd.py", "eee.py"):
                Path(project, name).write_text("# decoy\n")
            pkg = Path(project, "pkg")
            pkg.mkdir()
            (pkg / "auth.py").write_text("def divide(a, b):\n    return a / b\n")

            script = (
                "Fix the authentication divide function so it raises a clear "
                "ValueError instead of a ZeroDivisionError when b is 0.\n"
                "y\n"
                "/quit\n"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "main.py")],
                cwd=project, input=script, capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            sys.path.insert(0, str(pkg))
            import auth as fixed_auth  # noqa: E402

            self.assertEqual(fixed_auth.divide(4, 2), 2)
            with self.assertRaises(ValueError):
                fixed_auth.divide(1, 0)


if __name__ == "__main__":
    unittest.main()
