"""Parses the model's fenced ```write:path blocks out of its response and
applies them to disk, with a confirmation prompt. This replaces JSON
tool-calling: over Ollama, qwen2.5-coder's tool_call output was found to be
unreliable (plain text instead of the structured schema Ollama expects), so
file edits use a plain text convention the CLI parses itself -- deterministic
either way, no dependency on the model correctly filling a JSON schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from security import find_suspected_secrets

WRITE_BLOCK_RE = re.compile(
    r"(`{3,})write:([^\n`]+)\n(.*?)\1", re.DOTALL
)
SHELL_BLOCK_RE = re.compile(
    r"(`{3,})shell\n(.*?)\1", re.DOTALL
)


@dataclass
class FileWrite:
    path: str
    content: str


def extract_writes(model_output: str) -> list[FileWrite]:
    return [
        FileWrite(path=m.group(2).strip(), content=m.group(3))
        for m in WRITE_BLOCK_RE.finditer(model_output)
    ]


def extract_shell_suggestions(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in SHELL_BLOCK_RE.finditer(model_output)]


def strip_action_blocks(model_output: str) -> str:
    """The prose the model wrote around the blocks, for display."""
    text = WRITE_BLOCK_RE.sub("[file written -- see below]", model_output)
    text = SHELL_BLOCK_RE.sub("[shell suggestion -- see below]", text)
    return text.strip()


def apply_write(project_root: str, write: FileWrite, confirm: bool = True) -> bool:
    target = (Path(project_root) / write.path).resolve()
    root = Path(project_root).resolve()
    if root not in target.parents and target != root:
        print(f"[actions] refusing to write outside project root: {write.path}")
        return False

    existed = target.exists()
    action = "overwrite" if existed else "create"

    suspects = find_suspected_secrets(write.content)
    if suspects:
        print(f"  [security] {write.path} contains something shaped like a secret: {suspects[0][:12]}...")
        print("  [security] this is a pattern-match warning, not a certainty -- check before confirming.")

    if confirm:
        answer = input(f"  {action} {write.path} ({len(write.content)} bytes)? [y/N] ").strip().lower()
        if answer != "y":
            print(f"  skipped {write.path}")
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(write.content)
    print(f"  {'wrote' if not existed else 'updated'} {write.path}")
    return True
