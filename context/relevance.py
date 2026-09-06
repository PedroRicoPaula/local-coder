"""Deterministic file ranking for context assembly.

Replaces the old "first 5 source files by path depth" heuristic, which
sent whatever happened to sit at the repo root -- useless for a task about
a deep module. No embeddings, no SQLite: keyword overlap on path + a
capped file read, stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from context.tree import list_source_files

STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "your",
    "fix", "add", "make", "please", "just", "some", "have", "been",
    "when", "then", "than", "also", "should", "would", "could", "will",
    "can", "not", "but", "are", "was", "were", "has", "had", "its",
    "using", "use", "used", "file", "code", "function", "class", "bug",
    "error", "issue", "need", "needs", "want", "so", "it", "in", "on",
    "to", "of", "a", "an", "is", "be",
})

# Min 2 chars so `f7` / `db` match; 1-char noise is dropped by STOPWORDS.
_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{1,}")
_FILE_RE = re.compile(r"[a-zA-Z0-9_./-]+\.[a-zA-Z0-9]+")
CONTENT_CAP = 32_000
# Score content only for the best path-matches -- reading every file in a
# large tree would add wall time before the (already slow) LLM call.
CONTENT_CANDIDATES = 40
MAX_FILES = 400


@dataclass(frozen=True)
class ScoredFile:
    path: str
    score: float


def tokenize_task(task: str) -> list[str]:
    lowered = task.lower()
    words = [w for w in _TOKEN_RE.findall(lowered) if w not in STOPWORDS]
    for name in _FILE_RE.findall(lowered):
        words.append(name)
        stem = Path(name).stem
        if stem:
            words.append(stem)
    return words


def _path_score(path: str, tokens: list[str]) -> float:
    score = 0.0
    lower = path.lower().replace("\\", "/")
    stem = Path(path).stem.lower()
    name = Path(path).name.lower()
    for t in tokens:
        if t == stem or t == name:
            score += 8.0
        elif t in lower:
            score += 4.0
        elif len(t) >= 4 and len(stem) >= 3 and (stem in t or t in stem):
            score += 5.0
    return score


def _content_score(text: str, tokens: list[str]) -> float:
    lower = text.lower()
    score = 0.0
    for t in tokens:
        n = lower.count(t)
        if n:
            score += min(n, 8) * 0.5
    return score


def rank_source_files(root: str, task: str) -> list[ScoredFile]:
    tokens = tokenize_task(task)
    files = list_source_files(root)[:MAX_FILES]
    if not files:
        return []

    path_ranked = sorted(
        ((p, _path_score(p, tokens)) for p in files),
        key=lambda x: (-x[1], x[0].count("/"), x[0]),
    )
    to_read = [p for p, _ in path_ranked[:CONTENT_CANDIDATES]]
    read_set = set(to_read)
    scored: list[ScoredFile] = []
    for path in to_read:
        try:
            text = Path(root, path).read_text(errors="replace")[:CONTENT_CAP]
        except OSError:
            text = ""
        score = _path_score(path, tokens) + _content_score(text, tokens)
        scored.append(ScoredFile(path, score))
    for path, pscore in path_ranked:
        if path not in read_set:
            scored.append(ScoredFile(path, pscore))
    scored.sort(key=lambda s: (-s.score, s.path.count("/"), s.path))
    return scored


def select_files_for_task(root: str, task: str, limit: int = 5) -> list[ScoredFile]:
    return rank_source_files(root, task)[:limit]


def format_selection(
    selected: list[ScoredFile],
    *,
    budget_chars: int,
    used_chars: int,
    num_ctx: int,
    pinned: bool,
) -> str:
    lines = [
        "Context budget",
        "",
        f"  File assembly: {used_chars} / {budget_chars} chars",
        f"  Model window:  {num_ctx} tokens (num_ctx)",
        f"  Selection:     {'pinned via /files' if pinned else 'ranked by task keywords'}",
        "",
        "Selected files:",
    ]
    if not selected:
        lines.append("  (none)")
        return "\n".join(lines)
    max_score = max(s.score for s in selected) or 1.0
    for item in selected:
        shown = item.score if pinned else (item.score / max_score if max_score else 0.0)
        lines.append(f"  {shown:5.2f}  {item.path}")
    return "\n".join(lines)
