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
import subprocess

COMMIT_PREFIX = "localcoder: "


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


def undo_last(project_root: str) -> tuple[bool, str]:
    """Reverts the last commit, but only if localcoder made it -- checked by
    the commit message prefix, so this can never discard a commit that was
    actually the user's own work."""
    if not is_git_repo(project_root):
        return False, "not a git repo -- nothing to undo this way"
    try:
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=project_root, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"could not read git log: {e}"
    if log.returncode != 0:
        return False, "no commits yet"
    last_subject = log.stdout.strip()
    if not last_subject.startswith(COMMIT_PREFIX):
        return False, "the last commit wasn't made by localcoder -- refusing to touch it"

    has_parent = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD~1"],
        cwd=project_root, capture_output=True, timeout=5,
    ).returncode == 0
    if not has_parent:
        # HEAD~1 doesn't exist -- this commit is the repo's very first.
        # `git reset --hard HEAD~1` would fail with a confusing git error;
        # refusing cleanly is safer than the more involved dance of undoing
        # a repository back to zero commits for a case that shouldn't come
        # up in real use (localcoder acting on a project that already had
        # history before it touched anything).
        return False, "can't auto-undo the repo's very first commit -- remove it by hand if you really want to"

    try:
        result = subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=project_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"git reset failed: {e}"
    if result.returncode != 0:
        return False, f"git reset failed: {result.stderr.strip()}"
    return True, f"reverted: {last_subject}"
