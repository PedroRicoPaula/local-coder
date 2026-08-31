"""Loads user-editable Markdown rule files from skills/ and injects them
into the system prompt. This is the "teach the model your stack" mechanism
from the spec -- no fine-tuning, just prompt injection with a budget cap.
"""
from __future__ import annotations

from pathlib import Path

MAX_CHARS_PER_FILE = 4000
MAX_TOTAL_CHARS = 12000


def load_skills(skills_dir: str) -> list[str]:
    path = Path(skills_dir)
    if not path.exists():
        return []
    snippets = []
    total = 0
    for md_file in sorted(path.glob("*.md")):
        text = md_file.read_text(errors="replace").strip()
        if not text:
            continue
        if len(text) > MAX_CHARS_PER_FILE:
            text = text[:MAX_CHARS_PER_FILE] + "\n...(truncated, file too long)"
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        snippets.append(f"## {md_file.name}\n{text}")
        total += len(text)
    return snippets
