# macOS support for localcoder

Date: 2026-09-06
Status: draft — implement only after the user approves this spec and the matching plan

## 1. Goal

A Mac with Python 3.10+ and Ollama can install and run localcoder the same
way Linux does today: `localcoder` on PATH, a hardware-tuned `config.json`
if none exists, write/edit/delete/run/verify/undo all working, and the
fast unittest suite green without `/proc`.

Windows is out of scope.

## 2. What already works on Darwin (do not touch)

macOS is POSIX. These paths are already correct and must stay shared:

- `execution.py`: `start_new_session=True` + `os.killpg` (SIGTERM then SIGKILL)
- `gitsafety.py`, `pathpolicy.py`, `textfile.py`, `actions.py`, `verification.py`
- `llm/busy.py`: `os.kill(pid, 0)` is valid on Darwin
- `ui.py`, `main.py` REPL, action fences, CCE optional fallback
- command denylist (`rm -rf`, `mkfs`, …) — same Unix surface as Linux

The product is not “a Mac port of the agent”. It is “stop assuming Linux
in the three places that do”.

## 3. What is actually Linux-only today

| Surface | Breakage on Mac |
|---|---|
| `scripts/install.sh` | exits if `systemctl` is missing |
| `scripts/detect_hardware.py` | `/proc/cpuinfo` + `/proc/meminfo`; GPU = `nvidia-smi` only. An M-series Mac would be classified `cpu-weak` (0 RAM from missing `/proc/meminfo`, no NVIDIA) and get the `fast` profile (`qwen3:4b`) — the worst default on a machine that likely has Metal |
| `tests/test_execution.py` `_dead_or_zombie` | opens `/proc/<pid>/stat`; timeout-kill tests fail or skip-wrongly |
| README / BACKLOG | “Linux + systemd only”; the old “why macOS isn’t covered” note is already gone from BACKLOG |

## 4. Non-goals

- Windows / Job Objects / `cmd.exe` denylist
- A second installer script (`install-macos.sh`)
- A LaunchAgent that runs `ollama-serve-tuned.sh` (fights the official Ollama.app on `:11434`)
- Bundling or auto-building CCE; same optional `cargo build --release` as Linux
- Changing `BASE_SYSTEM_PROMPT` or the action-block protocol
- New pip dependencies

## 5. Design

### 5.1 Hardware detection

Keep one module. Branch inside the existing helpers:

- **Cores:** if `/proc/cpuinfo` is readable, keep today’s parser. Else
  `sysctl -n hw.physicalcpu` (Darwin). Else `os.cpu_count()`.
- **RAM:** if `/proc/meminfo` is readable, keep today. Else
  `sysctl -n hw.memsize` (bytes) / 1024³. Else `0.0` (same as today’s
  unreadable-proc fallback).
- **GPU:** keep `nvidia-smi` first (Linux + rare eGPU). Then: Darwin +
  `arm64` (`os.uname().machine`) ⇒ `has_gpu=True`, `vram_gb=None`
  (unified memory; do not invent a VRAM number). Intel Mac ⇒ no GPU
  unless `nvidia-smi` works. Ollama on Apple Silicon uses Metal without
  extra flags; the `gpu` tier already leaves `num_thread`/`num_batch`
  unset so Ollama’s defaults apply.

Tier thresholds stay unchanged (`gpu` wins; else ≥4 physical cores and
≥16 GB ⇒ `cpu-strong`; else `cpu-weak`).

### 5.2 Installer

One `scripts/install.sh`. After the `python3` / `ollama` checks:

- **Linux + systemd:** today’s path (unit, enable --now, launcher).
- **Darwin (or Linux without systemd):** do **not** exit. Write
  `config.json` if missing, install the `localcoder` launcher at
  `~/.local/bin/localcoder`, chmod +x. Skip the systemd unit.
- If `http://127.0.0.1:11434/api/version` already answers, print that
  the existing Ollama (usually Ollama.app) will be used and do not start
  a second server.
- If nothing is listening, print how to start one: open Ollama.app, or
  `scripts/ollama-serve-tuned.sh` in a dedicated terminal. Do not
  auto-background the script (that is the orphan-`llama-server` failure
  already documented).
- PATH hint: if `~/.local/bin` is missing from `PATH`, print the zsh
  line Mac users need (`~/.zprofile`).

`ollama-serve-tuned.sh` stays Linux-service-oriented. On Mac it remains
an optional manual wrapper for the env vars (`OLLAMA_KV_CACHE_TYPE`,
flash attention, keep-alive, max-loaded, num-parallel). The official app
does not read that script; documenting `launchctl setenv` / quit-and-reopen
is enough. Do not install a competing plist in v1.

### 5.3 Tests

- `_dead_or_zombie(pid)` becomes POSIX: `os.kill(pid, 0)` →
  `ProcessLookupError` means gone. If the process still exists, treat a
  Linux `/proc/.../stat` state `Z` **or** `ps -o state= -p <pid>` starting
  with `Z` as dead-enough. No `@unittest.skipIf` that would hide a
  killpg regression on Mac — those tests must run on Darwin.
- New unit tests for Darwin `sysctl` parsing and `arm64` ⇒ GPU, all
  mocked (this Linux CI box has no `hw.physicalcpu` of the Mac kind;
  do not require a Mac to keep the suite green).

### 5.4 Docs

README Quick start grows a Mac subsection (clone, `ollama pull`,
`./scripts/install.sh`, optional CCE, `localcoder`). Troubleshooting
gains “Ollama.app already running — do not also start the tuned script”
and “`~/.local/bin` not on zsh PATH”. BACKLOG Done entry. One line in
AGENTS.md/CLAUDE.md Commands: installer is Linux-or-Darwin, not systemd-only.

## 6. Success on the user’s Mac (manual; this Linux box cannot do it)

1. `python3 -m unittest discover tests` — all fast tests pass.
2. `./scripts/install.sh` exits 0, writes launcher, does not error on
   missing `systemctl`, does not start a second Ollama if the app is up.
3. `localcoder` from a small project directory reaches the REPL.
4. One confirmed ` ```run ` (`echo hi`) works; `/verify` runs discovery.
5. Apple Silicon: `detect_hardware.py` prints `"tier": "gpu"`.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Second Ollama on :11434 | Installer never starts a server when `/api/version` answers |
| Intel Mac mis-tiered as GPU | Only `arm64` counts as Metal; Intel stays CPU unless nvidia-smi |
| `~/.local/bin` not on default Mac PATH | Installer prints the exact `~/.zprofile` line |
| CCE not built | Existing fallback; README already says optional |
| killpg tests flake on zombies | POSIX helper treats `Z` as dead, same as today’s `/proc` check |
