"""Git-based safety net for confirmed writes/deletes -- /undo in main.py
reverts the last commit localcoder made. This is the recovery path for
after you've already said "yes" to a change and regret it; the y/N prompt
itself is the recovery path for before you say yes, and stays the only one
outside a git repo.

Never the reason an action fails: every function here swallows git errors
rather than raising -- the write/delete already succeeded on disk by the
time these are called, and a failed safety-net commit must never look like
a failed action to the user.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import re
import subprocess

COMMIT_PREFIX = "localcoder: "
UNDO_SCAN_LIMIT = 50
# git's own revert body: "This reverts commit <40-hex-sha>."
# Match by SHA, not subject, so identical subjects cannot be confused.
_REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{40})\.")


def is_git_repo(project_root: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_root, capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def commit_change(project_root: str, message: str, paths: Sequence[str]) -> None:
    """Stages and commits ONLY `paths`. Broad staging could sweep a user's
    unrelated modified or staged work into a `localcoder:` commit; the
    commit pathspec leaves separately staged files staged and uncommitted.
    Every git error is still swallowed -- the on-disk action already
    succeeded, and a failed safety-net commit must never look like a failed
    action."""
    if not is_git_repo(project_root):
        return
    pathspec = [p for p in paths if p]
    if not pathspec:
        return
    try:
        subprocess.run(
            ["git", "add", "--", *pathspec],
            cwd=project_root, capture_output=True, timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", *pathspec],
            cwd=project_root, capture_output=True, timeout=10,
        )
        if staged.returncode == 0:
            return
        subprocess.run(
            ["git", "commit", "-m", f"{COMMIT_PREFIX}{message}", "--", *pathspec],
            cwd=project_root, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _git(project_root: str, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=project_root, capture_output=True, text=True, timeout=timeout,
    )


def undo_last(project_root: str) -> tuple[bool, str]:
    """Revert the newest un-reverted localcoder commit without rewriting
    history or overwriting uncommitted changes."""
    if not is_git_repo(project_root):
        return False, "not a git repo -- nothing to undo this way"
    try:
        if _git(project_root, ["rev-parse", "--verify", "-q", "HEAD"], 5).returncode != 0:
            return False, "no commits yet"

        log = _git(project_root, ["log", f"-n{UNDO_SCAN_LIMIT}", "--pretty=%H%x1f%s%x1f%b%x1e"])
        if log.returncode != 0:
            return False, "could not read git log"
        records: list[tuple[str, str]] = []
        reverted: set[str] = set()
        for entry in log.stdout.split("\x1e"):
            entry = entry.strip("\n")
            if not entry.strip():
                continue
            sha, _, rest = entry.partition("\x1f")
            subject, _, body = rest.partition("\x1f")
            records.append((sha, subject))
            reverted.update(_REVERTS_RE.findall(body))

        target = next(
            ((sha, subject) for sha, subject in records
             if subject.startswith(COMMIT_PREFIX) and sha not in reverted),
            None,
        )
        if target is None:
            return False, (
                f"nothing localcoder committed in the last {UNDO_SCAN_LIMIT} "
                "commits is left to undo"
            )
        sha, subject = target

        changed = _git(
            project_root,
            ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha],
        )
        paths = [path for path in changed.stdout.splitlines() if path]
        if paths:
            status = _git(project_root, ["status", "--porcelain", "--", *paths])
            if status.stdout.strip():
                return False, (
                    f"uncommitted changes in {', '.join(paths)} would be overwritten "
                    "-- commit or stash them first"
                )

        result = _git(project_root, ["revert", "--no-edit", "--no-rerere-autoupdate", sha], 30)
        if result.returncode == 0:
            head = _git(project_root, ["rev-parse", "--short", "HEAD"], 5)
            new_sha = head.stdout.strip()
            suffix = f" (new commit {new_sha})" if head.returncode == 0 and new_sha else ""
            return True, f"reverted: {subject}{suffix}"

        marker = _git(project_root, ["rev-parse", "--git-path", "REVERT_HEAD"], 5)
        marker_path = Path(project_root, marker.stdout.strip())
        if marker.returncode == 0 and marker_path.exists():
            abort = _git(project_root, ["revert", "--abort"], 30)
            if abort.returncode == 0 and not marker_path.exists():
                return False, f"revert conflicted and was rolled back -- resolve {subject} by hand"
            return False, (
                "revert conflicted and cleanup could not be confirmed; the repository "
                "may still have a revert in progress -- inspect git status and run "
                "git revert --abort"
            )
        first_line = (result.stderr.strip().splitlines() or [""])[0]
        return False, f"git refused the revert: {first_line}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"git command failed: {e}"
