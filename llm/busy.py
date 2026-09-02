"""Orphaned-generation detection. Ollama/llama.cpp doesn't cancel a
request server-side when the client disconnects mid-computation (see
docs/BACKLOG.md) -- with OLLAMA_NUM_PARALLEL=1, that orphaned computation
silently blocks the next request, which then looks hung rather than queued.

`GET /api/ps` alone can't tell "loaded and idle" apart from "loaded and
mid-generation" -- no such flag exists in that response, and a model can sit
loaded-but-idle for the whole OLLAMA_KEEP_ALIVE window. So this pairs it
with a host-scoped advisory lock file recording which PID currently has an
in-flight request: a *stale* lock (its PID no longer exists) combined with
a model still shown loaded is the actual signature of an orphaned
generation left behind by an earlier, now-dead localcoder process -- not
proof by itself, but a strong enough signal to warn instead of staying
silent.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

LOCK_FILE = Path.home() / ".cache" / "localcoder" / "inflight.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def check_stale_lock() -> int | None:
    """Returns the dead PID left behind in a stale lock file, or None if
    there isn't one -- no lock at all, a corrupt lock (ignored, not fatal:
    this is advisory, not a real mutex), or a lock whose PID is still alive
    (a genuinely concurrent localcoder session, not an orphan)."""
    try:
        data = json.loads(LOCK_FILE.read_text())
    except (OSError, ValueError):
        return None
    pid = data.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        return pid
    return None


def list_running(host: str) -> list[str]:
    """Model names Ollama currently shows loaded, via GET /api/ps. An empty
    result doesn't prove "not busy" on its own -- it only corroborates a
    stale-lock finding when non-empty (a dead PID's lock plus a model still
    loaded is a much stronger signal than either alone)."""
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/api/ps")
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return [m.get("name", "") for m in body.get("models", [])]


@contextmanager
def held():
    """Marks "this process has an outstanding Ollama request" for the
    duration of the wrapped block. Removed on any exit path, including an
    exception -- a lock left behind after a clean exit would itself look
    stale and falsely warn the next run."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    try:
        yield
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
