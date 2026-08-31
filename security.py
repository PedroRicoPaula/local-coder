"""Lightweight, heuristic scan for secret-shaped strings in model output.

This is a warning, not a guarantee -- pattern matching on token shapes will
miss plenty and false-positive on some. It exists so a hallucinated or
echoed credential gets a human's eyes on it before it lands in a file,
matching the "require approval before high-impact actions" principle: the
approval prompt (already required for every write, see actions.py) is where
this warning surfaces, it does not add a second gate of its own.
"""
from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),          # PEM private key
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                          # OpenAI/Anthropic-shaped secret key
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),                    # GitHub token
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
]


def find_suspected_secrets(text: str) -> list[str]:
    hits = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            hits.append(match.group(0))
    return hits
