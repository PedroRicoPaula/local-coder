"""Parses the model's fenced action blocks out of its response and applies
them, each behind a confirmation prompt. This replaces JSON tool-calling:
over Ollama, qwen2.5-coder's tool_call output was found to be unreliable
(plain text instead of the structured schema Ollama expects), so every
action uses a plain text convention the CLI parses itself -- deterministic
either way, no dependency on the model correctly filling a JSON schema.

Five block kinds, all using the same variable-length-fence convention (a
run of 3+ backticks, closed by the same count -- so a file whose own
content has a ``` fence, e.g. a README with a code example, doesn't
truncate the block: escalate to four+ backticks and the parser follows):

  ```write:path      -- create or replace a file, whole content
  ```delete:path      -- remove a file
  ```run              -- execute a shell command (confirmed, output fed back)
  ```fetch:url          -- fetch a web page (confirmed, text fed back)
  ```shell             -- show a command WITHOUT running it (display only)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import gitsafety
from security import find_suspected_secrets

WRITE_BLOCK_RE = re.compile(r"(`{3,})write:([^\n`]+)\n(.*?)\1", re.DOTALL)
DELETE_BLOCK_RE = re.compile(r"(`{3,})delete:([^\n`]+)\n?(.*?)\1", re.DOTALL)
RUN_BLOCK_RE = re.compile(r"(`{3,})run\n(.*?)\1", re.DOTALL)
FETCH_BLOCK_RE = re.compile(r"(`{3,})fetch:([^\n`]+)\n?(.*?)\1", re.DOTALL)
SHELL_BLOCK_RE = re.compile(r"(`{3,})shell\n(.*?)\1", re.DOTALL)


@dataclass
class FileWrite:
    path: str
    content: str


def extract_writes(model_output: str) -> list[FileWrite]:
    return [
        FileWrite(path=m.group(2).strip(), content=m.group(3))
        for m in WRITE_BLOCK_RE.finditer(model_output)
    ]


def extract_deletes(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in DELETE_BLOCK_RE.finditer(model_output)]


def extract_runs(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in RUN_BLOCK_RE.finditer(model_output)]


def extract_fetches(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in FETCH_BLOCK_RE.finditer(model_output)]


def extract_shell_suggestions(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in SHELL_BLOCK_RE.finditer(model_output)]


def strip_action_blocks(model_output: str) -> str:
    """The prose the model wrote around the blocks, for display."""
    text = model_output
    for pattern, label in (
        (WRITE_BLOCK_RE, "file written"),
        (DELETE_BLOCK_RE, "delete requested"),
        (RUN_BLOCK_RE, "command requested"),
        (FETCH_BLOCK_RE, "fetch requested"),
        (SHELL_BLOCK_RE, "shell suggestion"),
    ):
        text = pattern.sub(f"[{label} -- see below]", text)
    return text.strip()


def _resolve_in_root(project_root: str, path: str) -> Path | None:
    """Resolves `path` under `project_root`, refusing anything that escapes
    it (a crafted `../../etc/passwd`-style path in a model-emitted block).
    Returns None -- not an exception -- so callers can print one consistent
    refusal message rather than handling this two different ways."""
    root = Path(project_root).resolve()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        return None
    return target


def apply_write(project_root: str, write: FileWrite, confirm: bool = True) -> bool:
    target = _resolve_in_root(project_root, write.path)
    if target is None:
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
    gitsafety.commit_change(project_root, f"write {write.path}")
    return True


def apply_delete(project_root: str, path: str, confirm: bool = True) -> bool:
    target = _resolve_in_root(project_root, path)
    if target is None:
        print(f"[actions] refusing to delete outside project root: {path}")
        return False
    if not target.exists():
        print(f"  {path} doesn't exist, nothing to delete")
        return False
    if target.is_dir():
        print(f"[actions] refusing to delete a directory ({path}) -- one file at a time")
        return False

    if confirm:
        answer = input(f"  delete {path}? [y/N] ").strip().lower()
        if answer != "y":
            print(f"  skipped {path}")
            return False

    target.unlink()
    print(f"  deleted {path}")
    gitsafety.commit_change(project_root, f"delete {path}")
    return True
