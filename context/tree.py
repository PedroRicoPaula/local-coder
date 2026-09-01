"""Walk a project directory into a compact file tree, respecting .gitignore
(best-effort: common patterns only, no full git integration -- this is a
context-budget tool, not a git client)."""
from __future__ import annotations

import fnmatch
from pathlib import Path

from context.denylist import is_denied

ALWAYS_IGNORE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "target",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".cache", ".idea",
    ".vscode", "*.pyc", ".DS_Store",
}


def _load_gitignore_patterns(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.exists():
        return []
    patterns = []
    for line in gi.read_text(errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line.rstrip("/"))
    return patterns


def _is_ignored(name: str, patterns: list[str]) -> bool:
    if name in ALWAYS_IGNORE:
        return True
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def build_tree(root: str, max_entries: int = 400) -> str:
    """Returns an indented text tree, capped at max_entries lines so it can't
    blow the context budget on a huge repo."""
    root_path = Path(root).resolve()
    patterns = _load_gitignore_patterns(root_path)
    lines: list[str] = []

    def walk(path: Path, depth: int) -> None:
        if len(lines) >= max_entries:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return
        for entry in entries:
            if len(lines) >= max_entries:
                lines.append("  " * depth + "... (truncated)")
                return
            if _is_ignored(entry.name, patterns):
                continue
            prefix = "  " * depth
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                walk(entry, depth + 1)
            else:
                lines.append(f"{prefix}{entry.name}")

    # Deliberately no root-name line here: every entry below is already
    # relative to root, and a model that sees the root's own name as the
    # first tree line tends to echo it into write paths (e.g.
    # "myproject/myproject/file.py" instead of "myproject/file.py").
    walk(root_path, 0)
    return "\n".join(lines)


def list_source_files(root: str, extensions: tuple[str, ...] = (
    ".py", ".rs", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".cs", ".rb", ".c", ".cpp", ".h",
)) -> list[str]:
    """Flat list of source file paths (relative to root), for callers that
    want to pick a subset to compress and inject."""
    root_path = Path(root).resolve()
    patterns = _load_gitignore_patterns(root_path)
    out: list[str] = []

    def walk(path: Path) -> None:
        try:
            entries = list(path.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if _is_ignored(entry.name, patterns):
                continue
            if entry.is_dir():
                walk(entry)
            elif entry.suffix in extensions and not is_denied(str(entry)):
                out.append(str(entry.relative_to(root_path)))

    walk(root_path)
    # Sorted so "first N" (main.py's default file-selection heuristic when
    # nothing is pinned via /files) is at least deterministic and shallow
    # paths first, rather than whatever order the filesystem happens to
    # return -- not relevance ranking, just not arbitrary.
    out.sort(key=lambda p: (p.count("/"), p))
    return out
