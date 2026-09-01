"""Heuristic head+tail truncation for text that CCE cannot compress --
CCE only exposes compress_file/get_symbol, both requiring a real file path
under CCE_ROOT (confirmed against its own Rust source), so shell/fetch/
search output flowing through the follow-up-hop loop in main.py's run_turn
has no quality-preserving compression available and falls back to this
instead: keep the head and tail (where the interesting output usually is --
a command's opening context and its final result/error) and elide the
middle with a visible marker, never silently.
"""
from __future__ import annotations

DEFAULT_HEAD_LINES = 40
DEFAULT_TAIL_LINES = 40
DEFAULT_MAX_CHARS = 4000  # matches execution.py's existing MAX_OUTPUT_CHARS


def truncate_text(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    head_lines: int = DEFAULT_HEAD_LINES,
    tail_lines: int = DEFAULT_TAIL_LINES,
) -> str:
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines:
        # Too few lines to elide by line count (e.g. one huge minified
        # line) -- fall back to a raw char-based head+tail split.
        half = max_chars // 2
        elided_chars = len(text) - max_chars
        return f"{text[:half]}\n...({elided_chars} chars elididos)...\n{text[-half:]}"
    elided = len(lines) - head_lines - tail_lines
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    return f"{head}\n...({elided} linhas elididas)...\n{tail}"
