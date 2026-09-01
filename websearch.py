"""Best-effort web search via DuckDuckGo's HTML-only endpoint
(html.duckduckgo.com/html/), scraped with a hand-rolled stdlib HTMLParser --
no API key, same spirit as webfetch.py's HTML-to-text stripping (though this
needs structured results -- title/url/snippet -- not flattened text, so it's
a sibling module, not a reuse of webfetch.py itself).

Fragile by nature: DuckDuckGo's markup and bot-detection heuristics can
change at any time with no notice and silently break this. Heuristic, not a
guarantee -- same spirit as webfetch.py and security.py.

This is also the one place `is_online()` lives -- webfetch.py imports it
from here (one-directional: this module never imports webfetch.py) so both
`fetch` and `search` can skip the confirmation prompt when a request is
already known to be doomed.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

import ui

SEARCH_URL = "https://html.duckduckgo.com/html/"
MAX_RESULTS = 5
MAX_BYTES = 500_000
TIMEOUT_S = 10
USER_AGENT = "localcoder/1.0"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _unwrap_ddg_redirect(href: str) -> str:
    """DuckDuckGo's result links are redirects of the form
    //duckduckgo.com/l/?uddg=<url-encoded-real-url>&rut=... -- unwrap to the
    real destination. Returns `href` unchanged if it doesn't match (a direct
    link, or DuckDuckGo's markup having changed)."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    if "uddg" in query and query["uddg"]:
        return urllib.parse.unquote(query["uddg"][0])
    return href


class _ResultExtractor(HTMLParser):
    """Parses DuckDuckGo's HTML-only results page: each result is an
    `<a class="result__a">` (title text + redirect href) followed elsewhere
    by an element whose class contains "result__snippet" (summary text)."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResult] = []
        self._in_title = False
        self._in_snippet = False
        self._snippet_depth = 0
        self._current_title = ""
        self._current_url = ""
        self._current_snippet = ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr_dict = dict(attrs)
        classes = (attr_dict.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._in_title = True
            self._current_title = ""
            self._current_url = _unwrap_ddg_redirect(attr_dict.get("href", ""))
        elif any("result__snippet" in c for c in classes):
            self._in_snippet = True
            self._snippet_depth = 1
            self._current_snippet = ""
        elif self._in_snippet:
            self._snippet_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "a":
            self._in_title = False
            if self._current_title.strip() and self._current_url:
                self.results.append(
                    SearchResult(title=self._current_title.strip(), url=self._current_url, snippet="")
                )
        elif self._in_snippet:
            self._snippet_depth -= 1
            if self._snippet_depth <= 0:
                self._in_snippet = False
                if self.results:
                    self.results[-1].snippet = self._current_snippet.strip()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._current_title += data
        elif self._in_snippet:
            self._current_snippet += data


def _extract_results(html: str) -> list[SearchResult]:
    extractor = _ResultExtractor()
    extractor.feed(html)
    return extractor.results


def is_online(timeout_s: float = 2.0) -> bool:
    """Cheap liveness probe against the one thing this feature actually
    depends on (DuckDuckGo's HTML endpoint) -- NOT a general internet check:
    `fetch` to some other URL could still work even if this specific host is
    blocked, and vice versa. Best-effort: a captive portal or DNS-only
    outage can still fool this."""
    req = urllib.request.Request(SEARCH_URL, headers={"User-Agent": USER_AGENT})
    try:
        urllib.request.urlopen(req, timeout=timeout_s)
        return True
    except (urllib.error.URLError, OSError):
        return False


def apply_search(query: str, confirm: bool = True) -> str | None:
    """Returns formatted search results to feed back to the model, or None
    if offline/refused/failed/empty (nothing to feed back)."""
    if not is_online():
        ui.sub("offline -- skipping search (no confirmation needed for something guaranteed to fail)")
        return None

    if confirm:
        if not ui.confirm(f'  search the web for "{query}"?'):
            ui.sub("skipped")
            return None

    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(SEARCH_URL, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, OSError) as e:
        ui.sub(f"could not search: {e}")
        return None

    html = raw.decode(charset, errors="replace")
    results = _extract_results(html)[:MAX_RESULTS]
    if not results:
        ui.sub("no results parsed (DuckDuckGo's markup may have changed -- best-effort scraper)")
        return None

    ui.sub(f'found {len(results)} result(s) for "{query}"')
    lines = [f'Web search results for "{query}" (best-effort scrape, no guarantee of accuracy):']
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title}\n   {r.url}\n   {r.snippet}")
    return "\n".join(lines)
