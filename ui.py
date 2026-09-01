"""Hand-rolled ANSI terminal styling -- zero pip dependencies, stdlib only,
matching the rest of this project. Every function degrades to plain text
when NO_COLOR is set or stdout isn't a real terminal (tests, pipes, logs),
so nothing here can break a redirected/non-interactive run.

No project imports here on purpose: this is a leaf module so anything else
in the codebase can import it without risking a circular import.
"""
from __future__ import annotations

import os
import sys
import threading
import time


class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def _enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _wrap(text: str, *codes: str) -> str:
    if not codes or not _enabled():
        return text
    return "".join(codes) + text + _C.RESET


def bold(text: str) -> str:
    return _wrap(text, _C.BOLD)


def dim(text: str) -> str:
    return _wrap(text, _C.DIM)


def info(msg: str) -> None:
    print(_wrap(f"[localcoder] {msg}", _C.CYAN), flush=True)


def success(msg: str) -> None:
    print(_wrap(f"[localcoder] {msg}", _C.GREEN), flush=True)


def warn(msg: str) -> None:
    print(_wrap(f"[localcoder] aviso: {msg}", _C.YELLOW), flush=True)


def error(msg: str) -> None:
    print(_wrap(f"[localcoder] {msg}", _C.RED, _C.BOLD), flush=True)


def sub(msg: str) -> None:
    """Two-space-indented sub-step line -- the existing visual convention
    used throughout actions.py/execution.py/webfetch.py for "under a turn"
    messages, now centralized here instead of ad hoc f-strings."""
    print(f"  {msg}", flush=True)


def confirm(prompt: str) -> bool:
    answer = input(f"{bold(prompt)} [y/N] ").strip().lower()
    return answer == "y"


def color_for_pct(pct: float) -> str:
    if pct < 0.60:
        return _C.GREEN
    if pct < 0.85:
        return _C.YELLOW
    return _C.RED


def usage_bar(prompt_tokens: int, response_tokens: int, num_ctx: int, width: int = 24) -> str:
    total = prompt_tokens + response_tokens
    pct = min(total / num_ctx, 1.0) if num_ctx else 0.0
    filled = int(pct * width)
    bar = "#" * filled + "-" * (width - filled)
    text = (
        f"[{bar}] {total}/{num_ctx} tokens ({pct * 100:.0f}%) "
        f"-- prompt {prompt_tokens} + resposta {response_tokens}"
    )
    return _wrap(text, color_for_pct(pct))


class Spinner:
    """Animated 'please wait' line covering the silent gap between sending
    a request and the first token arriving -- exactly the "is it hung?"
    window on CPU-only hardware. Falls back to a single static line (no
    animation, no \\r) when color/TTY is disabled, so it never corrupts
    piped output or a test capturing stdout.

    start()/stop() are both idempotent: stop() before start(), or stop()
    called twice, are both safe no-ops.
    """
    _FRAMES = ["|", "/", "-", "\\"]

    def __init__(self, message: str):
        self.message = message
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._start_time = 0.0

    def start(self) -> None:
        self._start_time = time.monotonic()
        if not _enabled():
            print(f"{self.message}...", flush=True)
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._stop_event is not None
        i = 0
        while True:
            elapsed = time.monotonic() - self._start_time
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stdout.write(f"\r{dim(f'{frame} {self.message}... {elapsed:.1f}s')}")
            sys.stdout.flush()
            i += 1
            if self._stop_event.wait(0.1):
                break

    def stop(self) -> None:
        if self._stop_event is None:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()
        self._stop_event = None
        self._thread = None
