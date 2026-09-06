"""Parses the model's fenced action blocks out of its response and applies
them, each behind a confirmation prompt. This replaces JSON tool-calling:
over Ollama, qwen2.5-coder's tool_call output was found to be unreliable
(plain text instead of the structured schema Ollama expects), so every
action uses a plain text convention the CLI parses itself -- deterministic
either way, no dependency on the model correctly filling a JSON schema.

Eight block kinds, all using the same variable-length-fence convention (a
run of 3+ backticks, closed by the same count -- so a file whose own
content has a ``` fence, e.g. a README with a code example, doesn't
truncate the block: escalate to four+ backticks and the parser follows):

  ```write:path      -- create or replace a file, whole content
  ```edit:path       -- replace one unique snippet (<<<<<<< SEARCH / ======= / >>>>>>> REPLACE)
  ```delete:path      -- remove a file
  ```run              -- execute a shell command (confirmed, output fed back)
  ```fetch:url          -- fetch a web page (confirmed, text fed back)
  ```shell             -- show a command WITHOUT running it (display only)
  ```search:query       -- web search (confirmed, results fed back)
  ```symbol:path#name   -- ask CCE for one elided function's full body
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import gitsafety
import ui
from security import find_suspected_secrets

WRITE_BLOCK_RE = re.compile(r"(`{3,})write:([^\n`]+)\n(.*?)\1", re.DOTALL)
EDIT_BLOCK_RE = re.compile(r"(`{3,})edit:([^\n`]+)\n(.*?)\1", re.DOTALL)
DELETE_BLOCK_RE = re.compile(r"(`{3,})delete:([^\n`]+)\n?(.*?)\1", re.DOTALL)
RUN_BLOCK_RE = re.compile(r"(`{3,})run\n(.*?)\1", re.DOTALL)
FETCH_BLOCK_RE = re.compile(r"(`{3,})fetch:([^\n`]+)\n?(.*?)\1", re.DOTALL)
SHELL_BLOCK_RE = re.compile(r"(`{3,})shell\n(.*?)\1", re.DOTALL)
SEARCH_BLOCK_RE = re.compile(r"(`{3,})search:([^\n`]+)\n?(.*?)\1", re.DOTALL)
SYMBOL_BLOCK_RE = re.compile(r"(`{3,})symbol:([^\n`]+)\n?(.*?)\1", re.DOTALL)
# Literal conflict-marker body inside an edit fence. Exact text, including
# the newline that sits in front of ======= / >>>>>>> REPLACE.
EDIT_BODY_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)=======\n(.*?)>>>>>>> REPLACE",
    re.DOTALL,
)


@dataclass
class FileWrite:
    path: str
    content: str


@dataclass
class FileEdit:
    path: str
    search: str
    replace: str


@dataclass
class EditResult:
    """ok=True means the file was written. error is set only when the model
    should retry (malformed / 0 matches / ambiguous) -- a declined y/N is
    ok=False with error=None so it does not burn a follow-up hop."""
    ok: bool
    error: str | None = None


def extract_writes(model_output: str) -> list[FileWrite]:
    return [
        FileWrite(path=m.group(2).strip(), content=m.group(3))
        for m in WRITE_BLOCK_RE.finditer(model_output)
    ]


def extract_edits(model_output: str) -> list[FileEdit]:
    """Skips edit fences whose body is not a well-formed SEARCH/REPLACE pair
    -- same spirit as extract_symbol_requests skipping a missing '#'."""
    out: list[FileEdit] = []
    for m in EDIT_BLOCK_RE.finditer(model_output):
        path = m.group(2).strip()
        body = m.group(3)
        inner = EDIT_BODY_RE.search(body)
        if inner is None:
            continue
        out.append(FileEdit(path=path, search=inner.group(1), replace=inner.group(2)))
    return out


def extract_deletes(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in DELETE_BLOCK_RE.finditer(model_output)]


def extract_runs(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in RUN_BLOCK_RE.finditer(model_output)]


def extract_fetches(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in FETCH_BLOCK_RE.finditer(model_output)]


def extract_shell_suggestions(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in SHELL_BLOCK_RE.finditer(model_output)]


def extract_searches(model_output: str) -> list[str]:
    return [m.group(2).strip() for m in SEARCH_BLOCK_RE.finditer(model_output)]


def extract_symbol_requests(model_output: str) -> list[tuple[str, str]]:
    """Each ```symbol block must be `path#symbol_name` -- a spec with no
    `#` is silently skipped (malformed, nothing sensible to act on)."""
    out: list[tuple[str, str]] = []
    for m in SYMBOL_BLOCK_RE.finditer(model_output):
        spec = m.group(2).strip()
        if "#" not in spec:
            continue
        path, _, name = spec.partition("#")
        out.append((path.strip(), name.strip()))
    return out


def strip_action_blocks(model_output: str) -> str:
    """The prose the model wrote around the blocks, for display."""
    text = model_output
    for pattern, label in (
        (WRITE_BLOCK_RE, "file written"),
        (EDIT_BLOCK_RE, "edit requested"),
        (DELETE_BLOCK_RE, "delete requested"),
        (RUN_BLOCK_RE, "command requested"),
        (FETCH_BLOCK_RE, "fetch requested"),
        (SHELL_BLOCK_RE, "shell suggestion"),
        (SEARCH_BLOCK_RE, "search requested"),
        (SYMBOL_BLOCK_RE, "symbol requested"),
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


def format_action_error(
    *,
    action: str,
    reason: str,
    path: str = "",
    suggestion: str = "",
) -> str:
    """Compact, stable shape for follow-up hops -- qwen2.5-coder:7b follows
    labeled fields more reliably than a prose paragraph."""
    lines = ["ERROR:", f"{action} failed", "", "reason:", reason]
    if path:
        lines.extend(["", "file:", path])
    if suggestion:
        lines.extend(["", "suggestion:", suggestion])
    return "\n".join(lines)


def unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )


def apply_write(project_root: str, write: FileWrite, confirm: bool = True) -> bool:
    target = _resolve_in_root(project_root, write.path)
    if target is None:
        ui.error(f"refusing to write outside project root: {write.path}")
        return False

    existed = target.exists()
    action = "overwrite" if existed else "create"

    suspects = find_suspected_secrets(write.content)
    if suspects:
        ui.warn(f"{write.path} contains something shaped like a secret: {suspects[0][:12]}...")
        ui.warn("this is a pattern-match warning, not a certainty -- check before confirming.")

    if confirm:
        if not ui.confirm(f"  {action} {write.path} ({len(write.content)} bytes)?"):
            ui.sub(f"skipped {write.path}")
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(write.content)
    ui.sub(f"{'wrote' if not existed else 'updated'} {write.path}")
    gitsafety.commit_change(project_root, f"write {write.path}")
    return True


def apply_delete(project_root: str, path: str, confirm: bool = True) -> bool:
    target = _resolve_in_root(project_root, path)
    if target is None:
        ui.error(f"refusing to delete outside project root: {path}")
        return False
    if not target.exists():
        ui.sub(f"{path} doesn't exist, nothing to delete")
        return False
    if target.is_dir():
        ui.error(f"refusing to delete a directory ({path}) -- one file at a time")
        return False

    if confirm:
        if not ui.confirm(f"  delete {path}?"):
            ui.sub(f"skipped {path}")
            return False

    target.unlink()
    ui.sub(f"deleted {path}")
    gitsafety.commit_change(project_root, f"delete {path}")
    return True


def apply_edit(project_root: str, edit: FileEdit, confirm: bool = True) -> EditResult:
    target = _resolve_in_root(project_root, edit.path)
    if target is None:
        ui.error(f"refusing to edit outside project root: {edit.path}")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason="path is outside project root",
                path=edit.path,
                suggestion="use a path relative to the project root",
            ),
        )
    if not target.exists() or not target.is_file():
        ui.error(f"{edit.path} does not exist")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason="target file does not exist",
                path=edit.path,
                suggestion="inspect the project tree and retry, or use write to create a new file",
            ),
        )

    original = target.read_text(errors="replace")
    if not edit.search:
        ui.error(f"edit {edit.path}: empty search block")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason="empty search block",
                path=edit.path,
                suggestion="include the exact snippet to replace between SEARCH and =======",
            ),
        )
    matches = original.count(edit.search)
    if matches != 1:
        reason = f"search block matched {matches} times (need exactly 1)"
        ui.error(f"edit {edit.path}: {reason}")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason=reason,
                path=edit.path,
                suggestion="inspect the current file (or ```symbol) and use a unique snippet",
            ),
        )

    updated = original.replace(edit.search, edit.replace, 1)
    suspects = find_suspected_secrets(edit.replace)
    if suspects:
        ui.warn(f"{edit.path} edit contains something shaped like a secret: {suspects[0][:12]}...")
        ui.warn("this is a pattern-match warning, not a certainty -- check before confirming.")

    diff = unified_diff(edit.path, original, updated)
    if diff:
        ui.sub(diff.rstrip("\n"))

    if confirm:
        if not ui.confirm(f"  apply edit to {edit.path}?"):
            ui.sub(f"skipped {edit.path}")
            return EditResult(False)

    target.write_text(updated)
    ui.sub(f"edited {edit.path}")
    gitsafety.commit_change(project_root, f"edit {edit.path}")
    return EditResult(True)
