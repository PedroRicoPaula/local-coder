"""Runs a shell command the model asked for, always behind confirmation.

A short denylist refuses outright -- no prompt at all -- for patterns with
no legitimate reason to appear in a coding task and every reason to be
catastrophic if run by accident. This is mitigation, not a promise, same
spirit as context/denylist.py: everything else still needs an explicit y/N,
which is the real gate. Matches the standard guidance for agentic CLIs
(OWASP's AI Agent Security Cheat Sheet): never grant blanket execution,
always require approval for anything with real-world effect.
"""
from __future__ import annotations

import re
import subprocess

import ui

MAX_OUTPUT_CHARS = 4000
TIMEOUT_S = 120

_DENIED_PATTERNS = [
    re.compile(r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r"),  # rm -rf, -fr, etc
    re.compile(r"\brm\b.*(/\*|/\s*$|~\s*$)"),                    # rm targeting / or ~
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),      # fork bomb
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\bsudo\b"),
]


def is_denied(command: str) -> bool:
    return any(p.search(command) for p in _DENIED_PATTERNS)


def apply_run(project_root: str, command: str, confirm: bool = True) -> str | None:
    """Returns captured output to feed back to the model, or None if the
    command was refused/denied/skipped (nothing to feed back)."""
    if is_denied(command):
        ui.error(f"refusing to run (matches a denied pattern): {command}")
        return None

    if confirm:
        if not ui.confirm(f"  run `{command}`?"):
            ui.sub("skipped")
            return None

    try:
        result = subprocess.run(
            command, shell=True, cwd=project_root,
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        ui.sub(f"command timed out after {TIMEOUT_S}s")
        return f"(command timed out after {TIMEOUT_S}s: {command})"
    except OSError as e:
        ui.sub(f"could not run command: {e}")
        return None

    output = (result.stdout or "") + (result.stderr or "")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n...(truncated)"
    ui.sub(output if output.strip() else "(no output)")
    return f"$ {command}\n(exit {result.returncode})\n{output}"
