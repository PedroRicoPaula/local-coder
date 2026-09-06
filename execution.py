"""Runs model shell commands and trusted local argv commands.

Model-emitted strings use shell=True and a short denylist; deterministic
argument vectors built by localcoder use shell=False so metacharacters remain
literal. Confirmation belongs to the calling layer (apply_run for model
commands and verification.py for argv). The denylist is mitigation, not a
promise: explicit y/N confirmation remains the real gate, matching OWASP's
guidance for agentic CLIs.
"""
from __future__ import annotations

import enum
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass

import ui

MAX_OUTPUT_CHARS = 4000
TIMEOUT_S = 120
PIPE_DRAIN_S = 0.25
REAP_TIMEOUT_S = 0.5

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


class Status(str, enum.Enum):
    OK = "ok"
    FAILED = "failed"
    NO_TESTS = "no_tests"
    TIMEOUT = "timeout"
    DENIED = "denied"
    DECLINED = "declined"
    LAUNCH_ERROR = "launch_error"


@dataclass(frozen=True)
class CommandResult:
    kind: str
    display: str
    status: Status
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timeout_s: int
    truncated: bool

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    @property
    def combined(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part.strip())


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate only the private process group created for this command."""
    # start_new_session=True makes the child's PID its process-group ID.
    # Using that known ID avoids looking up a group after a timeout race.
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # The direct child may exit while a grandchild in the same private group
    # ignores SIGTERM. Kill the group either way; ESRCH means it is already gone.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _close_pipes(proc: subprocess.Popen) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None and not pipe.closed:
            try:
                pipe.close()
            except OSError:
                pass


def _reap_bounded(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=REAP_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass


def _partial_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _capture(
    command,
    *,
    shell: bool,
    kind: str,
    display: str,
    project_root: str,
    timeout_s: int,
) -> CommandResult:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            shell=shell,
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        return CommandResult(
            kind=kind,
            display=display,
            status=Status.LAUNCH_ERROR,
            exit_code=None,
            stdout="",
            stderr=str(error),
            duration_s=time.monotonic() - start,
            timeout_s=timeout_s,
            truncated=False,
        )

    timed_out = False
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as timeout:
            timed_out = True
            stdout = _partial_text(timeout.output)
            stderr = _partial_text(timeout.stderr)
            _kill_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=PIPE_DRAIN_S)
            except subprocess.TimeoutExpired as cleanup_timeout:
                # communicate() reports cumulative output, so replace rather
                # than append when a bounded drain captured anything further.
                if cleanup_timeout.output is not None:
                    stdout = _partial_text(cleanup_timeout.output)
                if cleanup_timeout.stderr is not None:
                    stderr = _partial_text(cleanup_timeout.stderr)
                _reap_bounded(proc)
    except BaseException:
        _kill_group(proc)
        _close_pipes(proc)
        _reap_bounded(proc)
        raise
    finally:
        _close_pipes(proc)

    stdout, stderr = stdout or "", stderr or ""
    if timed_out:
        status, exit_code = Status.TIMEOUT, None
    elif proc.returncode == 0:
        status, exit_code = Status.OK, 0
    else:
        status, exit_code = Status.FAILED, proc.returncode
    return CommandResult(
        kind=kind,
        display=display,
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=time.monotonic() - start,
        timeout_s=timeout_s,
        truncated=len(stdout) + len(stderr) > MAX_OUTPUT_CHARS,
    )


def run_shell(
    project_root: str, command: str, timeout_s: int = TIMEOUT_S
) -> CommandResult:
    """Run a model-emitted command string through the shell and denylist."""
    if is_denied(command):
        return CommandResult(
            kind="shell",
            display=command,
            status=Status.DENIED,
            exit_code=None,
            stdout="",
            stderr="",
            duration_s=0.0,
            timeout_s=timeout_s,
            truncated=False,
        )
    return _capture(
        command,
        shell=True,
        kind="shell",
        display=command,
        project_root=project_root,
        timeout_s=timeout_s,
    )


def run_argv(
    project_root: str, argv: Sequence[str], timeout_s: int
) -> CommandResult:
    """Run a localcoder-built argument vector without shell interpretation."""
    argv = list(argv)
    return _capture(
        argv,
        shell=False,
        kind="argv",
        display=shlex.join(argv),
        project_root=project_root,
        timeout_s=timeout_s,
    )


def apply_run(project_root: str, command: str, confirm: bool = True) -> str | None:
    """Returns captured output to feed back to the model, or None if the
    command was refused/denied/skipped. Its existing text contract is kept."""
    if is_denied(command):
        ui.error(f"refusing to run (matches a denied pattern): {command}")
        return None

    if confirm and not ui.confirm(f"  run `{command}`?"):
        ui.sub("skipped")
        return None

    result = run_shell(project_root, command)
    if result.status is Status.DENIED:
        return None
    if result.status is Status.TIMEOUT:
        ui.sub(f"command timed out after {TIMEOUT_S}s")
        return f"(command timed out after {TIMEOUT_S}s: {command})"
    if result.status is Status.LAUNCH_ERROR:
        ui.sub(f"could not run command: {result.stderr}")
        return None

    output = result.stdout + result.stderr
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n...(truncated)"
    ui.sub(output if output.strip() else "(no output)")
    return f"$ {command}\n(exit {result.exit_code})\n{output}"
