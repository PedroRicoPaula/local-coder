import os
import unittest
from unittest import mock

import ui


class TestEnabled(unittest.TestCase):
    def test_no_color_env_disables(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
             mock.patch("sys.stdout.isatty", return_value=True):
            self.assertFalse(ui._enabled())

    def test_non_tty_disables(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.stdout.isatty", return_value=False):
            self.assertFalse(ui._enabled())

    def test_tty_without_no_color_enables(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.stdout.isatty", return_value=True):
            self.assertTrue(ui._enabled())

    def test_wrap_noop_when_disabled(self):
        with mock.patch("ui._enabled", return_value=False):
            self.assertEqual(ui._wrap("text", ui._C.RED), "text")

    def test_wrap_applies_codes_when_enabled(self):
        with mock.patch("ui._enabled", return_value=True):
            wrapped = ui._wrap("text", ui._C.RED)
            self.assertIn("text", wrapped)
            self.assertIn(ui._C.RED, wrapped)
            self.assertIn(ui._C.RESET, wrapped)


class TestColorForPct(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(ui.color_for_pct(0.0), ui._C.GREEN)
        self.assertEqual(ui.color_for_pct(0.59), ui._C.GREEN)
        self.assertEqual(ui.color_for_pct(0.60), ui._C.YELLOW)
        self.assertEqual(ui.color_for_pct(0.84), ui._C.YELLOW)
        self.assertEqual(ui.color_for_pct(0.85), ui._C.RED)
        self.assertEqual(ui.color_for_pct(0.86), ui._C.RED)
        self.assertEqual(ui.color_for_pct(1.0), ui._C.RED)


class TestUsageBar(unittest.TestCase):
    def test_contains_counts_and_percentage(self):
        with mock.patch("ui._enabled", return_value=False):
            bar = ui.usage_bar(3120, 480, 8192)
        self.assertIn("3600/8192", bar)
        self.assertIn("44%", bar)
        self.assertIn("3120", bar)
        self.assertIn("480", bar)

    def test_handles_zero_num_ctx(self):
        with mock.patch("ui._enabled", return_value=False):
            bar = ui.usage_bar(0, 0, 0)
        self.assertIn("0/0", bar)


class TestSpinner(unittest.TestCase):
    def test_non_tty_start_stop_is_idempotent(self):
        with mock.patch("ui._enabled", return_value=False):
            sp = ui.Spinner("waiting")
            sp.start()
            sp.stop()
            sp.stop()  # must not raise

    def test_stop_before_start_is_safe(self):
        sp = ui.Spinner("waiting")
        sp.stop()  # must not raise

    def test_tty_start_stop_cleans_up_thread(self):
        with mock.patch("ui._enabled", return_value=True):
            sp = ui.Spinner("waiting")
            sp.start()
            self.assertIsNotNone(sp._thread)
            sp.stop()
            self.assertIsNone(sp._thread)
            self.assertIsNone(sp._stop_event)


if __name__ == "__main__":
    unittest.main()
