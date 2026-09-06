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
                cwd=project, input=script, capture_output=True, text=True, timeout=900,
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
                cwd=project, input=script, capture_output=True, text=True, timeout=900,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            sys.path.insert(0, str(pkg))
            import auth as fixed_auth  # noqa: E402

            self.assertEqual(fixed_auth.divide(4, 2), 2)
            with self.assertRaises(ValueError):
                fixed_auth.divide(1, 0)

    def test_verification_repairs_a_failing_test(self):
        """Real model, real Ollama, real verification: localcoder must write
        a fix, run the discovered unittest command behind a y/N, and -- if
        the first attempt fails -- repair exactly once, never twice."""
        with tempfile.TemporaryDirectory() as project:
            Path(project, "calc.py").write_text("def divide(a, b):\n    return a / b\n")
            Path(project, "tests").mkdir()
            Path(project, "tests", "test_calc.py").write_text(
                "import unittest\n"
                "\n"
                "from calc import divide\n"
                "\n"
                "\n"
                "class TestDivide(unittest.TestCase):\n"
                "    def test_by_zero_raises_value_error(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            divide(1, 0)\n"
                "\n"
                "    def test_ordinary_division(self):\n"
                "        self.assertEqual(divide(4, 2), 2)\n"
            )

            script = (
                "/files calc.py tests/test_calc.py\n"
                "Make tests/test_calc.py pass by fixing calc.py.\n"
                "y\n"   # apply the write/edit
                "y\n"   # run verification
                "y\n"   # possible repair edit
                "y\n"   # final verification
                "/quit\n"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "main.py")],
                cwd=project, input=script, capture_output=True, text=True, timeout=900,
            )
            combined = result.stdout + result.stderr

            # 1. Surplus `y` lines are now no-ops and a missing one declines
            #    cleanly, so this also regression-tests the old EOF crash.
            self.assertEqual(result.returncode, 0, combined)

            # 2. Real behaviour, not a grep of the source: the resulting code
            #    must actually pass its own suite.
            verify = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=project, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

            # 3. Verification really ran, using the discovered command.
            self.assertIn("run verification", combined)
            self.assertIn("unittest discover", combined)

            # 4. The single-repair bound holds against a real model.
            self.assertLessEqual(combined.count("VERIFICATION FAILED"), 1)


if __name__ == "__main__":
    unittest.main()
