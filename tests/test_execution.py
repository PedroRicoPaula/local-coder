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


if __name__ == "__main__":
    unittest.main()
