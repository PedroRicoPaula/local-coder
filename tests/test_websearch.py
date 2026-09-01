import unittest
import urllib.error
from unittest import mock

import websearch


class TestExtractResults(unittest.TestCase):
    def test_parses_title_url_snippet(self):
        html = (
            '<div class="results">'
            '<div class="result">'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">'
            "Example Title</a>"
            '<a class="result__snippet">A short description of the page.</a>'
            "</div>"
            "</div>"
        )
        results = websearch._extract_results(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Example Title")
        self.assertEqual(results[0].url, "https://example.com/page")
        self.assertEqual(results[0].snippet, "A short description of the page.")

    def test_unparseable_markup_returns_empty(self):
        self.assertEqual(websearch._extract_results("<html><body>nothing here</body></html>"), [])


class TestIsOnline(unittest.TestCase):
    def test_reachable_returns_true(self):
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = lambda s, *a: False
            self.assertTrue(websearch.is_online())

    def test_unreachable_returns_false(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            self.assertFalse(websearch.is_online())


class TestApplySearch(unittest.TestCase):
    def test_declined_search_returns_none(self):
        with mock.patch("websearch.is_online", return_value=True), \
             mock.patch("builtins.input", return_value="n"):
            result = websearch.apply_search("test query", confirm=True)
        self.assertIsNone(result)

    def test_offline_skips_without_prompting(self):
        with mock.patch("websearch.is_online", return_value=False), \
             mock.patch("builtins.input", side_effect=AssertionError("should never prompt")):
            result = websearch.apply_search("test query", confirm=True)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
