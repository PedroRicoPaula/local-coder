import sys
import time
import unittest
from unittest import mock

import execution


class TestDeniedPatterns(unittest.TestCase):
    def test_catastrophic_commands_denied(self):
        for cmd in (
            "rm -rf /",
            "rm -fr ~",
            "sudo rm -rf /home",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            ":(){ :|:& };:",
            "shutdown -h now",
            "reboot",
            "echo hi > /dev/sda",
        ):
            self.assertTrue(execution.is_denied(cmd), cmd)

    def test_ordinary_commands_not_denied(self):
        for cmd in ("python3 -m pytest", "npm install", "git status", "ls -la"):
            self.assertFalse(execution.is_denied(cmd), cmd)

    def test_denied_command_never_prompts(self):
        # A denied command must be refused before input() is ever called --
        # asking for confirmation on something we're about to refuse anyway
        # would be actively misleading.
        with mock.patch("builtins.input", side_effect=AssertionError("should never prompt")):
            result = execution.apply_run("/tmp", "rm -rf /", confirm=True)
        self.assertIsNone(result)

    def test_declined_command_returns_none(self):
        with mock.patch("builtins.input", return_value="n"):
            result = execution.apply_run("/tmp", "echo hi", confirm=True)
        self.assertIsNone(result)

    def test_confirmed_ordinary_command_runs(self):
        with mock.patch("builtins.input", return_value="y"):
            result = execution.apply_run("/tmp", "echo hello", confirm=True)
        self.assertIsNotNone(result)
        self.assertIn("hello", result)


def _dead_or_zombie(pid: int) -> bool:
    """Return whether a Linux process is gone or awaiting parent reaping."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(") ", 1)[1].split()[0] == "Z"
    except OSError:
        return True


class TestRunArgv(unittest.TestCase):
    def test_shell_metacharacters_are_passed_literally(self):
        result = execution.run_argv("/tmp", ["printf", "%s", "$HOME;ls"], timeout_s=10)
        self.assertIs(result.status, execution.Status.OK)
        self.assertEqual(result.stdout, "$HOME;ls")
        self.assertEqual(result.kind, "argv")

    def test_timeout_kills_the_whole_process_group(self):
        start = time.monotonic()
        result = execution.run_argv(
            "/tmp",
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                "print(p.pid, flush=True); time.sleep(30)",
            ],
            timeout_s=1,
        )
        elapsed = time.monotonic() - start
        self.assertIs(result.status, execution.Status.TIMEOUT)
        self.assertIsNone(result.exit_code)
        self.assertLess(elapsed, 10)

        grandchild_pid = int(result.stdout.split()[0])
        for _ in range(30):
            if _dead_or_zombie(grandchild_pid):
                break
            time.sleep(0.1)
        else:
            self.fail("the grandchild survived the process-group kill")

    def test_timeout_kills_grandchild_that_ignores_sigterm(self):
        start = time.monotonic()
        result = execution.run_argv(
            "/tmp",
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(5)']);"
                "print(p.pid, flush=True); time.sleep(30)",
            ],
            timeout_s=1,
        )
        self.assertIs(result.status, execution.Status.TIMEOUT)
        self.assertLess(time.monotonic() - start, 4)

        grandchild_pid = int(result.stdout.split()[0])
        for _ in range(30):
            if _dead_or_zombie(grandchild_pid):
                break
            time.sleep(0.1)
        else:
            self.fail("the SIGTERM-ignoring grandchild survived")

    def test_fields_populated_for_ok_and_failed(self):
        ok = execution.run_argv(
            "/tmp", [sys.executable, "-c", "print('hi')"], timeout_s=10
        )
        self.assertTrue(ok.ok)
        self.assertEqual(ok.exit_code, 0)
        self.assertIn("hi", ok.stdout)
        self.assertEqual(ok.timeout_s, 10)
        self.assertGreaterEqual(ok.duration_s, 0.0)
        self.assertFalse(ok.truncated)

        bad = execution.run_argv(
            "/tmp",
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('boom'); sys.exit(3)",
            ],
            timeout_s=10,
        )
        self.assertFalse(bad.ok)
        self.assertIs(bad.status, execution.Status.FAILED)
        self.assertEqual(bad.exit_code, 3)
        self.assertIn("boom", bad.combined)

    def test_missing_binary_is_a_launch_error_not_a_crash(self):
        result = execution.run_argv(
            "/tmp", ["localcoder-no-such-binary"], timeout_s=5
        )
        self.assertIs(result.status, execution.Status.LAUNCH_ERROR)
        self.assertIsNone(result.exit_code)


class TestRunShell(unittest.TestCase):
    def test_denied_command_never_executes_even_without_apply_run(self):
        result = execution.run_shell("/tmp", "rm -rf /")
        self.assertIs(result.status, execution.Status.DENIED)
        self.assertEqual(result.kind, "shell")
        self.assertEqual(result.stdout, "")

    def test_shell_features_still_work(self):
        result = execution.run_shell("/tmp", "echo one && echo two")
        self.assertIs(result.status, execution.Status.OK)
        self.assertIn("one", result.stdout)
        self.assertIn("two", result.stdout)


if __name__ == "__main__":
    unittest.main()
