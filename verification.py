"""Deterministic verification: work out this project's own test/check
command with filesystem checks only (never a model call), run it as argv
with shell=False behind its own y/N, and turn a failure into one focused
repair prompt.

No model is ever asked "how do I test this project?" -- on CPU-only hardware
that question costs minutes and can be answered with a handful of stat()
calls. Everything outside main.py's single repair call is plain Python.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from context import tree
from context.truncate import truncate_text
from execution import CommandResult, Status

FEEDBACK_MAX_CHARS = 2000
PACKAGE_JSON_MAX_BYTES = 200_000
NPM_PLACEHOLDER_TEST = 'echo "Error: no test specified" && exit 1'


@dataclass(frozen=True)
class VerifyConfig:
    enabled: bool
    timeout_s: int
    override_argv: list[str] | None


@dataclass(frozen=True)
class VerificationCommand:
    kind: str          # "pytest"|"unittest"|"npm"|"cargo"|"go"|"pysyntax"|"override"
    argv: list[str]
    label: str         # shlex.join(argv), for display and confirmation


@dataclass(frozen=True)
class VerificationOutcome:
    ran: bool
    command: VerificationCommand | None
    result: CommandResult | None
    skip_reason: str   # "" when ran; else why nothing ran

    @property
    def needs_repair(self) -> bool:
        # LAUNCH_ERROR is deliberately excluded: a missing toolchain is not
        # a code defect the model can fix.
        return (
            self.ran
            and self.result is not None
            and self.result.status in (Status.FAILED, Status.TIMEOUT)
        )


def _command(kind: str, argv: list[str]) -> VerificationCommand:
    return VerificationCommand(kind=kind, argv=argv, label=shlex.join(argv))


def _pytest_configured(root: Path) -> bool:
    if (root / "pytest.ini").is_file():
        return True
    for name, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]")):
        path = root / name
        try:
            if path.is_file() and marker in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def _package_scripts(root: Path) -> dict:
    path = root / "package.json"
    try:
        if not path.is_file() or path.stat().st_size > PACKAGE_JSON_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _candidates(root: Path) -> list[VerificationCommand]:
    """Table order from the design doc -- this is the tie-break order, before
    language-of-mutation promotion re-sorts it."""
    out: list[VerificationCommand] = []
    if _pytest_configured(root):
        out.append(_command("pytest", [sys.executable, "-m", "pytest", "-q", "-x"]))
    if any((root / "tests").glob("test_*.py")):
        out.append(_command("unittest", [
            sys.executable, "-m", "unittest", "discover", "-q", "-s", "tests", "-t", ".",
        ]))
    if any(root.glob("test_*.py")):
        out.append(_command("unittest", [sys.executable, "-m", "unittest", "discover", "-q"]))
    test_script = _package_scripts(root).get("test")
    if (isinstance(test_script, str) and test_script.strip()
            and test_script.strip() != NPM_PLACEHOLDER_TEST):
        out.append(_command("npm", ["npm", "test", "--silent"]))
    if (root / "Cargo.toml").is_file():
        out.append(_command("cargo", ["cargo", "test", "--quiet"]))
    if (root / "go.mod").is_file():
        out.append(_command("go", ["go", "test", "./..."]))
    if any(p.endswith(".py") for p in tree.list_source_files(str(root))):
        out.append(_command("pysyntax", [sys.executable, "-m", "compileall", "-q", "."]))
    return out


_PROMOTED_KINDS: dict[str, tuple[str, ...]] = {
    ".py": ("pytest", "unittest", "pysyntax"),
    ".pyi": ("pytest", "unittest", "pysyntax"),
    ".js": ("npm",), ".jsx": ("npm",), ".ts": ("npm",), ".tsx": ("npm",),
    ".mjs": ("npm",), ".cjs": ("npm",), ".json": ("npm",),
    ".rs": ("cargo",),
    ".go": ("go",),
}


def _executable_available(argv: Sequence[str]) -> bool:
    # sys.executable is the interpreter already running us -- no PATH lookup
    # needed, and patching shutil.which in a test must not drop it.
    return argv[0] == sys.executable or shutil.which(argv[0]) is not None


def _promoted_kinds(changed_paths: Sequence[str]) -> set[str]:
    kinds: set[str] = set()
    for path in changed_paths:
        kinds.update(_PROMOTED_KINDS.get(Path(path).suffix.lower(), ()))
    return kinds


def discover(project_root: str, changed_paths: Sequence[str],
             override_argv: Sequence[str] | None = None) -> VerificationCommand | None:
    """Recomputed per run rather than cached: a handful of stat() calls
    against a turn that costs minutes, and a project that just gained a
    tests/ directory should be picked up immediately."""
    if override_argv:
        return _command("override", list(override_argv))
    root = Path(project_root).resolve()
    candidates = [c for c in _candidates(root) if _executable_available(c.argv)]
    if not candidates:
        return None
    promoted = _promoted_kinds(changed_paths)
    if promoted:
        # list.sort is stable, so within each group the table order decides.
        candidates.sort(key=lambda c: 0 if c.kind in promoted else 1)
    return candidates[0]


LEAD_IN_LINES = 3
WINDOW_LINES = 60
TAIL_LINES = 15

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# First match wins; the window opens three lines before it so the failing
# test's own name is included.
_MARKERS = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^E   "),
    re.compile(r"AssertionError"),
    re.compile(r"SyntaxError"),
    re.compile(r"^FAILED"),
    re.compile(r"^FAIL\b"),
    re.compile(r"^--- FAIL"),
    re.compile(r"^ERROR[: ]"),
    re.compile(r"^error\[E\d+\]"),
    re.compile(r"^error:"),
    re.compile(r"panicked at"),
    re.compile(r"npm ERR!"),
]


def strip_ansi(text: str) -> str:
    """CSI + OSC removal, then \\r-redraw collapsing: pytest/cargo/npm
    progress bars would otherwise dominate a 2000-char budget with noise."""
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    return "\n".join(line.split("\r")[-1] for line in text.split("\n"))


def extract_failure_context(stdout: str, stderr: str,
                            max_chars: int = FEEDBACK_MAX_CHARS) -> str:
    joined = "\n".join(
        part for part in (strip_ansi(stdout or "").strip(), strip_ansi(stderr or "").strip())
        if part
    )
    lines = joined.splitlines()
    if not lines:
        return ""

    index = next(
        (i for i, line in enumerate(lines) if any(m.search(line) for m in _MARKERS)),
        None,
    )
    tail_start = max(0, len(lines) - TAIL_LINES)
    if index is None:
        kept = lines[max(0, len(lines) - WINDOW_LINES):]
    else:
        start = max(0, index - LEAD_IN_LINES)
        end = min(len(lines), index + WINDOW_LINES)
        kept = lines[start:end]
        if end < len(lines):
            # The summary line ("FAILED (failures=1)", "test result: FAILED",
            # "2 passed, 1 failed") is where a 7B model orients fastest, so
            # the tail is always included -- with an explicit "..." when it is
            # not already contiguous with the marker window.
            if tail_start > end:
                kept = kept + ["..."] + lines[tail_start:]
            else:
                kept = lines[start:]

    text = truncate_text("\n".join(kept), max_chars=max_chars)
    # truncate_text's char-based fallback adds an elision marker on top of
    # max_chars; this text is appended to a prompt on a machine where every
    # character is paid for in prefill time, so the cap is hard.
    return text if len(text) <= max_chars else text[:max_chars]


def failure_feedback(outcome: VerificationOutcome) -> str:
    """The labeled-field shape actions.format_action_error already
    established for this model."""
    command = outcome.command
    result = outcome.result
    if result.status is Status.TIMEOUT:
        status_block = f"result:\ntimed out after {result.timeout_s}s"
    else:
        status_block = f"exit code:\n{result.exit_code}"
    return "\n".join([
        "--- VERIFICATION FAILED AFTER YOUR CHANGE ---",
        "command:",
        command.label,
        status_block,
        "output (trimmed):",
        extract_failure_context(result.stdout, result.stderr),
        "",
        "Fix the cause with a single ```edit block on the file that is wrong.",
        "Do not run commands, do not fetch, do not search.",
        "--- END VERIFICATION RESULT ---",
    ])
