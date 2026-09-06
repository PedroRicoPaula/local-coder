"""One decision function for "may this path be mutated?".

Every write/edit/delete in actions.py funnels through resolve_for_mutation()
so the answer is computed in exactly one place -- three separate copies of
"is this safe?" is how a gap gets introduced later. Returns a frozen
PathDecision rather than raising, so callers can render one consistent
refusal (humans) or one structured ERROR block (the model) without
try/except plumbing.

Stdlib + context.denylist only: this is a leaf module, importable from
anywhere without a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from context.denylist import is_denied


@dataclass(frozen=True)
class PathDecision:
    ok: bool
    path: Path | None = None   # resolved absolute path, None when not ok
    relpath: str = ""          # path relative to project_root, "" when not ok
    reason: str = ""           # "" when ok -- stable text, asserted in tests
    suggestion: str = ""       # "" when ok


def _refuse(reason: str, suggestion: str) -> PathDecision:
    return PathDecision(ok=False, path=None, relpath="", reason=reason, suggestion=suggestion)


def resolve_for_mutation(project_root: str, raw_path: str) -> PathDecision:
    """Five ordered checks, first failure wins. The `.git` check is
    component equality, never a prefix match -- that is precisely what keeps
    .gitignore/.gitattributes/.github/ writable while blocking .git/config
    and sub/.git/hooks/pre-commit."""
    if not raw_path.strip():
        return _refuse(
            "empty path",
            "name the file to change, relative to the project root",
        )

    root = Path(project_root).resolve()
    resolved = (root / raw_path).resolve()
    if resolved != root and root not in resolved.parents:
        return _refuse(
            "path is outside project root",
            "use a path relative to the project root",
        )

    relative = resolved.relative_to(root)
    if any(part.lower() == ".git" for part in relative.parts):
        return _refuse(
            "path is inside the .git directory",
            "never modify git internals; change tracked files instead",
        )

    if is_denied(resolved.name):
        return _refuse(
            "path looks like a credential or key file",
            "create or edit credential files by hand, outside localcoder",
        )

    if resolved.is_dir():
        return _refuse("path is a directory", "name a file, not a directory")

    return PathDecision(ok=True, path=resolved, relpath=relative.as_posix())
