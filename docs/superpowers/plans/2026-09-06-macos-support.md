# macOS Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** localcoder installs and runs on macOS the same way it does on Linux, without a second Ollama server and without Windows work.

**Architecture:** Keep one POSIX runtime. Teach `detect_hardware.py` Darwin `sysctl` + Apple-Silicon Metal, teach `install.sh` to skip systemd instead of exiting, and make the process-liveness helper in the execution tests POSIX so killpg coverage runs on Mac.

**Tech Stack:** stdlib Python 3.10+, bash, existing unittest suite. No pip. No LaunchAgent.

**Spec:** `docs/superpowers/specs/2026-09-06-macos-support-design.md`

## Global Constraints

- Zero pip dependencies; stdlib only.
- No new fenced action blocks; no `llm/prompts.py` edits (KV-cache prefix).
- Windows is out of scope. Do not add Job Objects, `cmd.exe` denylist, or `sys.platform == "win32"` branches except an explicit refuse if someone later asks.
- Do not install a LaunchAgent or auto-start `ollama-serve-tuned.sh` on Darwin.
- Do not create `scripts/install-macos.sh`. One installer.
- Every human-facing new string goes through `ui.py` only if it is runtime CLI chrome. Installer messages are plain `echo` (already the convention).
- Fast tests must stay green on this Linux box. Darwin paths are unit-tested with mocks.
- Commit after each task, conventional message, no `--no-verify`.

---

### Task 1: POSIX process-liveness helper for killpg tests

**Files:**
- Modify: `tests/test_execution.py` (`_dead_or_zombie`, ~lines 53-59)
- Test: same file — existing `test_timeout_kills_the_whole_process_group` and `test_timeout_kills_grandchild_that_ignores_sigterm` must keep passing on Linux and become Mac-safe

**Interfaces:**
- Consumes: `os.kill`, `/proc/<pid>/stat` when present, `ps -o state= -p <pid>` otherwise
- Produces: `_dead_or_zombie(pid: int) -> bool` — True if the pid is gone or a zombie

- [ ] **Step 1: Write a unit test for the helper itself (Linux-safe mocks)**

Add this class near `_dead_or_zombie` in `tests/test_execution.py`:

```python
class TestDeadOrZombie(unittest.TestCase):
    def test_process_lookup_error_is_dead(self):
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertTrue(_dead_or_zombie(999999))

    def test_linux_zombie_stat_is_dead(self):
        with mock.patch("os.kill", return_value=None), mock.patch(
            "builtins.open", mock.mock_open(read_data="123 (sleep) Z 1")
        ):
            self.assertTrue(_dead_or_zombie(123))

    def test_alive_without_proc_uses_ps_state(self):
        ps = mock.Mock(returncode=0, stdout="S\n")
        with mock.patch("os.kill", return_value=None), mock.patch(
            "builtins.open", side_effect=OSError
        ), mock.patch("subprocess.run", return_value=ps) as run:
            self.assertFalse(_dead_or_zombie(123))
            run.assert_called()

    def test_ps_zombie_or_missing_pid_is_dead(self):
        missing = mock.Mock(returncode=1, stdout="")
        with mock.patch("os.kill", return_value=None), mock.patch(
            "builtins.open", side_effect=OSError
        ), mock.patch("subprocess.run", return_value=missing):
            self.assertTrue(_dead_or_zombie(123))
```

- [ ] **Step 2: Run the new tests to verify they fail (helper still Linux-only)**

```bash
python3 -m unittest tests.test_execution.TestDeadOrZombie -v
```

Expected: FAIL — `ps` path does not exist yet, or `open` is unconditional and the new cases do not match.

- [ ] **Step 3: Replace `_dead_or_zombie` with the POSIX helper**

```python
def _dead_or_zombie(pid: int) -> bool:
    """True if pid is gone or a zombie (Linux /proc or POSIX ps)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(") ", 1)[1].split()[0] == "Z"
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if out.returncode != 0 or not out.stdout.strip():
        return True
    return out.stdout.strip().upper().startswith("Z")
```

Ensure `os` and `subprocess` are already imported in this file (they are).

- [ ] **Step 4: Run the helper tests and the two live killpg tests**

```bash
python3 -m unittest tests.test_execution.TestDeadOrZombie tests.test_execution.TestRunArgv.test_timeout_kills_the_whole_process_group tests.test_execution.TestRunArgv.test_timeout_kills_grandchild_that_ignores_sigterm -v
```

Expected: PASS (4 helper + 2 live).

- [ ] **Step 5: Commit**

```bash
git add tests/test_execution.py
git commit -m "$(cat <<'EOF'
fix: POSIX process-liveness check so killpg tests run on macOS

EOF
)"
```

---

### Task 2: Darwin hardware detection

**Files:**
- Modify: `scripts/detect_hardware.py`
- Test: `tests/test_detect_hardware.py`

**Interfaces:**
- Consumes: `/proc/*` when present; else `sysctl -n hw.physicalcpu` / `hw.memsize`; `os.uname().machine`
- Produces: unchanged `Hardware` dataclass and `detect() -> Hardware`. `_gpu()` still `(bool, float | None)`. Apple Silicon ⇒ `(True, None)`.

- [ ] **Step 1: Write the failing Darwin tests**

Append to `tests/test_detect_hardware.py`:

```python
class TestDarwinSysctl(unittest.TestCase):
    def test_physical_cores_reads_sysctl_when_proc_missing(self):
        sysctl = mock.Mock(returncode=0, stdout="8\n")
        with mock.patch.object(Path, "read_text", side_effect=OSError), mock.patch(
            "subprocess.run", return_value=sysctl
        ):
            self.assertEqual(dh._physical_cores(), 8)

    def test_ram_gb_reads_hw_memsize_when_proc_missing(self):
        sysctl = mock.Mock(returncode=0, stdout=str(16 * 1024**3) + "\n")
        with mock.patch.object(Path, "read_text", side_effect=OSError), mock.patch(
            "subprocess.run", return_value=sysctl
        ):
            self.assertAlmostEqual(dh._ram_gb(), 16.0, places=1)

    def test_arm64_darwin_counts_as_gpu_without_nvidia(self):
        uname = mock.Mock(machine="arm64")
        with mock.patch("shutil.which", return_value=None), mock.patch(
            "os.uname", return_value=uname
        ):
            self.assertEqual(dh._gpu(), (True, None))

    def test_intel_mac_without_nvidia_is_not_gpu(self):
        uname = mock.Mock(machine="x86_64")
        with mock.patch("shutil.which", return_value=None), mock.patch(
            "os.uname", return_value=uname
        ):
            self.assertEqual(dh._gpu(), (False, None))
```

- [ ] **Step 2: Run them to verify they fail**

```bash
python3 -m unittest tests.test_detect_hardware.TestDarwinSysctl -v
```

Expected: FAIL — `_physical_cores` falls back to `os.cpu_count()` instead of sysctl; `_ram_gb` returns `0.0`; `_gpu` returns `(False, None)` on arm64.

- [ ] **Step 3: Implement Darwin fallbacks in `scripts/detect_hardware.py`**

Add a tiny helper (stdlib only):

```python
def _sysctl_int(name: str) -> int | None:
    if not shutil.which("sysctl"):
        return None
    try:
        out = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None
```

`_physical_cores`: on `OSError` from `/proc/cpuinfo`, `return _sysctl_int("hw.physicalcpu") or (os.cpu_count() or 1)` — do **not** call `os.cpu_count()` before trying sysctl.

`_ram_gb`: on `OSError` from `/proc/meminfo`, `nbytes = _sysctl_int("hw.memsize"); return nbytes / 1024 / 1024 / 1024 if nbytes else 0.0`.

`_gpu`: after the existing nvidia-smi block fails, `if getattr(os.uname(), "machine", "") == "arm64": return True, None`. Then `return False, None`.

Update the module docstring: Linux and macOS, not “this Linux machine” only.

- [ ] **Step 4: Run Darwin tests + existing hardware tests**

```bash
python3 -m unittest tests.test_detect_hardware -v
```

Expected: PASS. Live `python3 scripts/detect_hardware.py` on this Linux box still prints `cpu-weak` (do not break the reference machine).

- [ ] **Step 5: Commit**

```bash
git add scripts/detect_hardware.py tests/test_detect_hardware.py
git commit -m "$(cat <<'EOF'
feat: detect Apple Silicon and Darwin cores/RAM via sysctl

EOF
)"
```

---

### Task 3: `install.sh` does not exit on Darwin

**Files:**
- Modify: `scripts/install.sh`
- Test: no Python test (bash). Verify with a dry logic check below.

**Interfaces:**
- Consumes: `command -v systemctl`, `curl` or Python one-liner to `http://127.0.0.1:11434/api/version`
- Produces: same `~/.local/bin/localcoder` launcher; systemd unit only when `systemctl` exists

- [ ] **Step 1: Replace the hard `systemctl` exit with a branch**

Delete the block that `exit 1`s when `systemctl` is missing (lines 25-31). After writing `config.json` (section 3), wrap section 4:

```bash
# --- 4. Ollama service (Linux + systemd only) --------------------------
if command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$LOCAL_BIN" "$SYSTEMD_USER_DIR"
    chmod +x "$REPO_ROOT/scripts/ollama-serve-tuned.sh"
    ln -sf "$REPO_ROOT/scripts/ollama-serve-tuned.sh" "$LOCAL_BIN/ollama-serve-tuned"
    echo "symlinked $LOCAL_BIN/ollama-serve-tuned -> repo's scripts/ollama-serve-tuned.sh"
    cp "$REPO_ROOT/scripts/ollama-tuned.service" "$SYSTEMD_USER_DIR/ollama-tuned.service"
    systemctl --user daemon-reload
    systemctl --user enable --now ollama-tuned.service
    echo "ollama-tuned.service enabled and started"
else
    mkdir -p "$LOCAL_BIN"
    chmod +x "$REPO_ROOT/scripts/ollama-serve-tuned.sh"
    echo "no systemd -- skipping ollama-tuned.service (expected on macOS)."
    echo "If Ollama.app is already running, use that. Otherwise start:"
    echo "  $REPO_ROOT/scripts/ollama-serve-tuned.sh"
    echo "in its own terminal (do not background it — see README Troubleshooting)."
fi
```

Keep section 5 (launcher) as-is so Darwin still gets `~/.local/bin/localcoder`.

- [ ] **Step 2: After the launcher, probe Ollama without starting it**

```bash
if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11434/api/version', timeout=2)" 2>/dev/null; then
    echo "Ollama is already answering on 127.0.0.1:11434 -- using that (do not start a second server)."
else
    echo "Ollama is not answering on 127.0.0.1:11434 yet. Open Ollama.app or run scripts/ollama-serve-tuned.sh, then: ollama pull qwen2.5-coder:7b"
fi
```

Do not start a server from this script on the non-systemd path.

- [ ] **Step 3: Update the file header comment**

Replace “Linux only, by design” with: Linux (systemd unit) or macOS (launcher + existing Ollama.app). Point at the spec, not a missing BACKLOG paragraph.

- [ ] **Step 4: Syntax-check the script (cannot run the systemd half here without changing the machine)**

```bash
bash -n scripts/install.sh
```

Expected: no output, exit 0.

On this Linux box, do **not** re-run `./scripts/install.sh` unless you intend to rewrite the unit — it is idempotent for config.json (leaves existing) but will `enable --now` again, which is fine. Prefer `bash -n` only.

- [ ] **Step 5: Commit**

```bash
git add scripts/install.sh
git commit -m "$(cat <<'EOF'
feat: install launcher on macOS without requiring systemd

EOF
)"
```

---

### Task 4: Docs — Mac is a supported install path

**Files:**
- Modify: `README.md` (Quick start ~line 8, repo layout ~333, Ollama tuning ~388, Troubleshooting ~574)
- Modify: `docs/BACKLOG.md` (new Done bullet)
- Modify: `AGENTS.md` and `CLAUDE.md` only if they still say the installer is Linux-only (today they do not mention install.sh — leave them unless a sentence is now false)

**Interfaces:** none

- [ ] **Step 1: README Quick start**

Change the heading to mention Mac. After the Linux `./scripts/install.sh` block, add:

```markdown
On macOS the same `./scripts/install.sh` writes `config.json` (if missing)
and puts `localcoder` on `~/.local/bin`. It does **not** install a
LaunchAgent and will not start a second Ollama if Ollama.app is already
on `:11434`. If `~/.local/bin` is not on `PATH` (default zsh):

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```
```

In Troubleshooting, add: if `:11434` is already taken by Ollama.app, do not also run `ollama-serve-tuned.sh`. Restart the app after `launchctl setenv` if you want the tuned env vars; otherwise the app’s defaults are fine on Apple Silicon.

Remove “Linux + systemd only (see docs/BACKLOG.md)” from the install.sh comment in Quick start.

- [ ] **Step 2: BACKLOG Done bullet**

Add at the top of Done:

```markdown
- **macOS install path** (`scripts/install.sh`, `scripts/detect_hardware.py`).
  Darwin no longer exits for missing systemd. Apple Silicon is the `gpu`
  tier (Metal). Killpg tests use a POSIX liveness helper instead of
  `/proc/<pid>/stat`. See
  `docs/superpowers/specs/2026-09-06-macos-support-design.md`.
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/BACKLOG.md
git commit -m "$(cat <<'EOF'
docs: document the macOS install path and Ollama.app coexistence

EOF
)"
```

---

### Task 5: Whole-tree check on Linux + Mac smoke list

**Files:** none required if Tasks 1–4 are done. Do not “fix” extra scope here.

- [ ] **Step 1: Fast suite + py_compile on this Linux box**

```bash
python3 -m unittest discover tests
python3 -m py_compile scripts/detect_hardware.py execution.py
bash -n scripts/install.sh
```

Expected: same pass/skip counts as `main` before this branch, plus the new tests. No new skips.

- [ ] **Step 2: Hand the user this Mac checklist (they run it; this agent cannot)**

On the Mac, from a clone of this branch:

```bash
python3 -m unittest discover tests
./scripts/install.sh
python3 scripts/detect_hardware.py   # Apple Silicon: "tier": "gpu"
which localcoder
cd /tmp && mkdir -p lc-mac-smoke && cd lc-mac-smoke && git init
# start localcoder, then:
#   /verify          (should say no tests / compileall, not crash)
#   ask it to run: echo hi   and confirm y
```

If any step fails, file it as a new TDD task — do not patch blind from Linux.

- [ ] **Step 3: Commit only if Step 1 forced a tiny fix.** Otherwise stop.

---

## Spec coverage

| Spec § | Task |
|---|---|
| 2 already-works list | no code (constraint) |
| 5.1 detect_hardware | Task 2 |
| 5.2 install.sh | Task 3 |
| 5.3 tests POSIX + mocked Darwin | Tasks 1 and 2 |
| 5.4 docs | Task 4 |
| 6 Mac success | Task 5 (user) |
| 4 non-goals | Global Constraints |

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-06-macos-support.md`. Spec: `docs/superpowers/specs/2026-09-06-macos-support-design.md`.
