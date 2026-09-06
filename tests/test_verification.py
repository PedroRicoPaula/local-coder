import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verification
from execution import Status


def _write(root, rel, text=""):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _which_always_found():
    """npm/cargo/go are not installed on every machine that runs this suite,
    and discovery deliberately drops a candidate whose binary is missing --
    so the tests that assert those candidates pretend the binary is there."""
    return mock.patch("shutil.which", return_value="/usr/bin/stub")


class TestDiscover(unittest.TestCase):
    def test_pytest_ini(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pytest.ini", "[pytest]\n")
            _write(root, "app.py", "x = 1\n")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "pytest")
            self.assertEqual(command.argv, [sys.executable, "-m", "pytest", "-q", "-x"])

    def test_pyproject_pytest_section(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pyproject.toml", "[tool.pytest.ini_options]\naddopts = ''\n")
            self.assertEqual(verification.discover(root, []).kind, "pytest")

    def test_setup_cfg_pytest_section(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "setup.cfg", "[tool:pytest]\n")
            self.assertEqual(verification.discover(root, []).kind, "pytest")

    def test_tests_directory(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "tests/test_x.py", "")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "unittest")
            self.assertEqual(
                command.argv,
                [sys.executable, "-m", "unittest", "discover", "-q", "-s", "tests", "-t", "."],
            )

    def test_top_level_test_file(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "test_x.py", "")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "unittest")
            self.assertEqual(command.argv, [sys.executable, "-m", "unittest", "discover", "-q"])

    def test_package_json_with_a_real_test_script(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "package.json", json.dumps({"scripts": {"test": "jest"}}))
            with _which_always_found():
                command = verification.discover(root, [])
            self.assertEqual(command.kind, "npm")
            self.assertEqual(command.argv, ["npm", "test", "--silent"])

    def test_npm_placeholder_script_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "package.json",
                   json.dumps({"scripts": {"test": verification.NPM_PLACEHOLDER_TEST}}))
            _write(root, "app.py", "x = 1\n")
            self.assertEqual(verification.discover(root, []).kind, "pysyntax")

    def test_cargo(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            with _which_always_found():
                command = verification.discover(root, [])
            self.assertEqual(command.kind, "cargo")
            self.assertEqual(command.argv, ["cargo", "test", "--quiet"])

    def test_go(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "go.mod", "module x\n")
            with _which_always_found():
                command = verification.discover(root, [])
            self.assertEqual(command.kind, "go")
            self.assertEqual(command.argv, ["go", "test", "./..."])

    def test_python_syntax_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "pysyntax")
            self.assertEqual(command.argv, [sys.executable, "-m", "compileall", "-q", "."])

    def test_empty_directory_yields_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(verification.discover(root, []))

    def test_missing_executable_drops_the_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            _write(root, "app.py", "x = 1\n")
            with mock.patch("shutil.which", return_value=None):
                self.assertEqual(verification.discover(root, []).kind, "pysyntax")

    def test_language_of_mutation_promotion(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            _write(root, "src/lib.rs", "fn main() {}\n")
            _write(root, "tests/test_x.py", "")
            with _which_always_found():
                self.assertEqual(verification.discover(root, ["src/lib.rs"]).kind, "cargo")
                self.assertEqual(verification.discover(root, ["app.py"]).kind, "unittest")
                # No changed paths -> no promotion -> plain table order.
                self.assertEqual(verification.discover(root, []).kind, "unittest")

    def test_override_wins_over_everything(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            command = verification.discover(root, [], override_argv=["make", "check"])
            self.assertEqual(command.kind, "override")
            self.assertEqual(command.argv, ["make", "check"])
            self.assertEqual(command.label, "make check")


class TestNeedsRepair(unittest.TestCase):
    def _make_outcome(self, status):
        result = verification.CommandResult(
            kind="argv", display="x", status=status, exit_code=1, stdout="", stderr="",
            duration_s=0.0, timeout_s=180, truncated=False,
        )
        command = verification.VerificationCommand(kind="unittest", argv=["x"], label="x")
        return verification.VerificationOutcome(True, command, result, "")

    def test_failed_and_timeout_need_repair(self):
        self.assertTrue(self._make_outcome(Status.FAILED).needs_repair)
        self.assertTrue(self._make_outcome(Status.TIMEOUT).needs_repair)

    def test_everything_else_does_not(self):
        for status in (Status.OK, Status.NO_TESTS, Status.LAUNCH_ERROR, Status.DECLINED):
            self.assertFalse(self._make_outcome(status).needs_repair, status)

    def test_a_skipped_outcome_never_needs_repair(self):
        outcome = verification.VerificationOutcome(False, None, None, "verification declined")
        self.assertFalse(outcome.needs_repair)


class TestStripAnsi(unittest.TestCase):
    def test_removes_csi_sequences(self):
        self.assertEqual(verification.strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def test_removes_osc_sequences(self):
        self.assertEqual(verification.strip_ansi("\x1b]0;title\x07body"), "body")

    def test_collapses_carriage_return_redraws(self):
        self.assertEqual(
            verification.strip_ansi("building 10%\rbuilding 50%\rbuilding 100%\ndone"),
            "building 100%\ndone",
        )


class TestExtractFailureContext(unittest.TestCase):
    def test_keeps_the_traceback_and_the_final_summary(self):
        stdout = "\n".join(
            ["noise"] * 200
            + ["Traceback (most recent call last):",
               '  File "calc.py", line 2, in divide',
               "ZeroDivisionError: division by zero"]
            + ["filler"] * 200
            + ["FAILED (errors=1)"]
        )
        extracted = verification.extract_failure_context(stdout, "")
        self.assertIn("Traceback (most recent call last):", extracted)
        self.assertIn("ZeroDivisionError", extracted)
        self.assertIn("FAILED (errors=1)", extracted)
        self.assertNotIn("noise\nnoise\nnoise\nnoise\nnoise\nnoise", extracted)

    def test_never_exceeds_the_cap(self):
        extracted = verification.extract_failure_context("x" * 50_000, "y" * 50_000)
        self.assertLessEqual(len(extracted), verification.FEEDBACK_MAX_CHARS)

    def test_falls_back_to_the_tail_when_no_marker_matches(self):
        stdout = "\n".join(f"line {i}" for i in range(500))
        extracted = verification.extract_failure_context(stdout, "")
        self.assertIn("line 499", extracted)
        self.assertNotIn("line 0\n", extracted)

    def test_empty_output_is_empty(self):
        self.assertEqual(verification.extract_failure_context("", ""), "")


class TestFailureFeedback(unittest.TestCase):
    def _make_outcome(self, status, exit_code, stdout):
        command = verification.VerificationCommand(
            kind="unittest",
            argv=[sys.executable, "-m", "unittest", "discover", "-q"],
            label="python3 -m unittest discover -q",
        )
        result = verification.CommandResult(
            kind="argv", display=command.label, status=status, exit_code=exit_code,
            stdout=stdout, stderr="", duration_s=1.0, timeout_s=180, truncated=False,
        )
        return verification.VerificationOutcome(True, command, result, "")

    def test_failed_feedback_shape(self):
        text = verification.failure_feedback(
            self._make_outcome(Status.FAILED, 1, "AssertionError: 1 != 2\nFAILED (failures=1)")
        )
        self.assertIn("--- VERIFICATION FAILED AFTER YOUR CHANGE ---", text)
        self.assertIn("python3 -m unittest discover -q", text)
        self.assertIn("exit code:\n1", text)
        self.assertIn("AssertionError", text)
        self.assertIn("```edit", text)
        self.assertIn("--- END VERIFICATION RESULT ---", text)

    def test_timeout_feedback_reports_the_timeout_instead_of_an_exit_code(self):
        text = verification.failure_feedback(self._make_outcome(Status.TIMEOUT, None, "partial output"))
        self.assertIn("timed out after 180s", text)
        self.assertNotIn("exit code:", text)
        self.assertIn("partial output", text)


if __name__ == "__main__":
    unittest.main()
