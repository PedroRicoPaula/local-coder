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


if __name__ == "__main__":
    unittest.main()
