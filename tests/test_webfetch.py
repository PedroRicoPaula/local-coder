import unittest
from unittest import mock

import webfetch


class TestHtmlToText(unittest.TestCase):
    def test_strips_script_and_style(self):
        html = "<html><head><style>.x{}</style></head><body><script>evil()</script><p>Hello world</p></body></html>"
        text = webfetch._html_to_text(html)
        self.assertIn("Hello world", text)
        self.assertNotIn("evil()", text)
        self.assertNotIn(".x{}", text)


class TestApplyFetch(unittest.TestCase):
    def test_refuses_non_http_scheme(self):
        with mock.patch("builtins.input", side_effect=AssertionError("should never prompt")):
            result = webfetch.apply_fetch("file:///etc/passwd", confirm=True)
        self.assertIsNone(result)

    def test_declined_fetch_returns_none(self):
        with mock.patch("builtins.input", return_value="n"):
            result = webfetch.apply_fetch("https://example.com", confirm=True)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
