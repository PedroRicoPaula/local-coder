"""Fetches a URL the model asked for, always behind confirmation, and
strips it down to plain text. This is the one feature in localcoder that
needs real internet access -- everything else is offline by design (see
README). It's opt-in per fetch, never automatic, and confirmed explicitly:
using it is a deliberate choice each time, not a standing capability.

The HTML-to-text extraction is a crude stdlib-only stripper (drops
script/style, joins remaining text nodes) -- no readability-grade content
extraction, no external dependency either. Good enough for "what does this
page say", not for scraping structured data.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from html.parser import HTMLParser

import ui
from websearch import is_online

MAX_BYTES = 500_000
MAX_TEXT_CHARS = 8000
TIMEOUT_S = 10


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return "\n".join(extractor.chunks)


def apply_fetch(url: str, confirm: bool = True) -> str | None:
    """Returns extracted page text to feed back to the model, or None if
    refused/skipped/failed (nothing to feed back)."""
    if not (url.startswith("http://") or url.startswith("https://")):
        ui.error(f"refusing to fetch non-http(s) URL: {url}")
        return None

    if not is_online():
        ui.sub("offline -- skipping fetch (no confirmation needed for something guaranteed to fail)")
        return None

    if confirm:
        if not ui.confirm(f"  fetch {url}?"):
            ui.sub("skipped")
            return None

    req = urllib.request.Request(url, headers={"User-Agent": "localcoder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, OSError) as e:
        ui.sub(f"could not fetch {url}: {e}")
        return None

    html = raw.decode(charset, errors="replace")
    text = _html_to_text(html)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n...(truncated)"
    ui.sub(f"fetched {url} ({len(text)} chars of extracted text)")
    return f"Content of {url}:\n{text}"
