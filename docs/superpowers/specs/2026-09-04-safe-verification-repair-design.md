# Design: safe mutation foundation + deterministic verification/repair

Date: 2026-09-04
Branch: `feature/safe-verify-repair`
Baseline: the repository's current uncommitted working tree (the `edit`
action, `context/relevance.py` ranking, `/context`/`/why`, and the
failed-edit follow-up hop are all part of the baseline, not part of this
phase).

Status: design scope approved; written specification awaiting user review.
After approval, implementation proceeds in two sequential vertical slices.
Slice A (blocking safety foundation) must be complete and green before
Slice B (verification -> repair) starts.

---

## 1. Goals

**Slice A -- blocking safety foundation**

1. No model-emitted action can mutate git internals (`.git/**`) or a
   credential/key file; `.gitignore`, `.gitattributes`, `.github/**` stay
   writable.
2. The git safety net stages only the exact path it is committing, never
   `git add -A`, so a user's unrelated staged/unstaged work is never swept
   into a `localcoder:` commit.
3. `/undo` becomes non-destructive: it never discards uncommitted work and
   never rewrites history.
4. Byte-level fidelity on mutation: CRLF files stay CRLF, a UTF-8 BOM
   survives, an undecodable (non-UTF-8) file is refused rather than
   mangled, and an overwrite shows a unified diff before the y/N.
5. No filesystem error, path error, exhausted stdin (EOF), or subprocess
   timeout can crash the CLI or leave an orphaned process group.
6. A malformed `edit` block (fence present, SEARCH/REPLACE body broken) is
   no longer silently dropped -- the model gets the same structured `ERROR:`
   feedback a failed edit already gets.

**Slice B -- deterministic verification -> repair**

7. After a successful mutation, localcoder runs the project's own
   verification command, discovered deterministically (no model call) for
   Python unittest, Python pytest, Node `package.json` test script, Rust
   and Go, with a Python syntax-only fallback and an explicit
   "unsupported project" path.
8. Discovered commands execute as `argv` with `shell=False`; every single
   execution requires its own y/N. There is no once-per-session blanket
   approval.
9. Passing verification costs zero extra model calls. Failing or timed-out
   verification produces exactly one focused repair call, followed by one
   final verification that can never produce another repair call.
10. `/verify` uses the same discovery instead of the hardcoded
    `compileall`.

## 2. Non-goals (this phase)

- No provider abstraction, no second LLM backend.
- No index, no embeddings, no vector store, no telemetry.
- No extra LLM calls at runtime for planning, reviewing, summarizing,
  choosing a verification command, or interpreting failures. Everything
  outside the repair call is deterministic Python.
- No new fenced block kinds; the ` ```write / ```edit / ```delete / ```run /
  ```fetch / ```search / ```symbol / ```shell ` protocol is unchanged.
- No change to `llm/prompts.py`'s `BASE_SYSTEM_PROMPT` (see §7.5 -- this is
  load-bearing for KV-cache reuse, not just conservatism).
- No native tool-calling / JSON schema; no CCE tool beyond
  `compress_file` / `get_symbol`.
- No sandboxing, containers, or privilege dropping for verification
  commands; the y/N prompt remains the gate.
- No repair loop longer than one hop, no "keep trying until green".
- No pip dependencies. Python 3.10+ stdlib only.

## 3. Current evidence

Baseline facts, read from the working tree:

- `gitsafety.commit_change()` runs `git add -A` then `git commit`, so any
  unrelated file the user had modified or staged is committed under a
  `localcoder:` message (`gitsafety.py:34-38`).
- `gitsafety.undo_last()` runs `git reset --hard HEAD~1`
  (`gitsafety.py:76-79`). That silently destroys uncommitted worktree
  changes and rewrites history, and it refuses outright when the commit is
  the repository's first.
- `actions._resolve_in_root()` blocks traversal outside the project root
  but nothing else: `.git/config`, `.git/hooks/pre-commit`, and `.env` are
  all writable today (`actions.py:142-152`). `context/denylist.py` is
  consulted only for *reading* context (`main.py:84-86`).
- `apply_write()` writes with `Path.write_text()`
  (`actions.py:202`), which translates `\n` to `os.linesep`-independent
  `\n` and destroys CRLF; `apply_edit()` reads with `errors="replace"`
  (`actions.py:256`), which silently corrupts any byte it cannot decode
  and then writes the corrupted text back.
- `apply_write()` shows no diff before overwriting an existing file; only
  `apply_edit()` does (`actions.py:288-290`).
- `ui.confirm()` calls `input()` with no `EOFError` handling
  (`ui.py:74-76`). `main()` catches `EOFError` only around the REPL prompt
  (`main.py:458-459`) and its outer handler catches only
  `KeyboardInterrupt` (`main.py:551`). A confirmation prompt reached after
  piped stdin is exhausted therefore ends the process with a traceback and
  a non-zero exit code -- which is exactly the shape of
  `tests/test_live.py`, where the script supplies a fixed number of `y`
  lines.
- `execution.apply_run()` uses `subprocess.run(..., shell=True,
  timeout=...)`. On `TimeoutExpired` the shell's children are not killed;
  the wall-clock cost keeps running (`execution.py:50-56`).
- `actions.extract_edits()` silently skips an `edit` fence whose body has
  no `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` triple
  (`actions.py:79-89`), so the turn simply ends with nothing applied and
  nothing said.
- `run_turn()` bounds itself at `MAX_FOLLOWUP_TURNS = 2` follow-up hops and
  threads Ollama's `context` array between them, sending only the delta
  when a cached prefix exists (`main.py:216-312`).
- `/verify` is hardcoded to `python3 -m compileall -q .` through the
  model's shell path (`main.py:471-473`).

Behaviours measured directly on this machine for this design (git 2.55.0,
Python 3.14.7), so none of the following is assumed:

| Question | Measured answer |
|---|---|
| `git revert --no-edit` on a **root** commit | exit 0, works (the current "refuse the first commit" restriction is unnecessary once we stop using `reset --hard`) |
| `git revert` with an unrelated dirty/untracked file | exit 0, succeeds |
| `git revert` with the worktree dirty on a path the commit touched | exit **128**, "local changes would be overwritten", nothing modified, and **no revert is in progress** (`git revert --abort` then also fails with 128) |
| `git revert` with a staged change on an affected path | exit 128, same shape |
| `git revert` producing a content conflict | exit **1**, `UU` in status, `.git/REVERT_HEAD` present, `git revert --abort` exits 0 and fully restores the prior state |
| `git commit -m msg -- <path>` with other files staged | commits only `<path>`; the user's staged `two.txt` stays staged and uncommitted |
| Changed paths of a commit, including a root commit | `git diff-tree --root --no-commit-id --name-only -r <sha>` (without `--root` a root commit prints nothing) |
| Default revert commit shape | subject `Revert "<original subject>"`, body `This reverts commit <40-hex-sha>.` |
| `git rev-parse --git-path REVERT_HEAD` | resolves the marker path in any worktree layout |
| `python3 -m unittest discover -q` with zero tests | exit **5** ("NO TESTS RAN") -- must not be treated as a failure |
| `python3 -m compileall -q .` with a syntax error | exit 1, error text on stdout |

## 4. Architecture

Two new leaf modules and one new orchestration module; everything else is
an edit to an existing file. No import cycles: `ui.py` stays project-import
free, `pathpolicy.py` and `textfile.py` import only stdlib (+
`context.denylist`), `verification.py` imports `execution` + `ui`, and
`main.py` sits on top.

```
pathpolicy.py     NEW  one decision function for "may this path be mutated?"
textfile.py       NEW  UTF-8/BOM/EOL-preserving read+write primitives
verification.py   NEW  discovery, confirmed argv execution, failure extraction
actions.py        EDIT routes write/edit/delete through pathpolicy + textfile,
                       diff-on-overwrite, malformed-edit extraction
gitsafety.py      EDIT path-scoped staging, revert-based /undo
execution.py      EDIT CommandResult, run_shell/run_argv, process-group timeout
ui.py             EDIT confirm() treats EOF as "no"
config.py         EDIT three new defaults
main.py           EDIT verification phase in run_turn, /verify, REPL y/n no-op
llm/prompts.py    UNCHANGED (deliberately -- see §7.5)
```

Slice A touches `pathpolicy.py`, `textfile.py`, `actions.py`,
`gitsafety.py`, `execution.py`, `ui.py`. Slice B touches
`verification.py`, `execution.py`, `config.py`, `main.py`.

## 5. Data flow

```
REPL line
  -> context assembly (unchanged)
  -> hop 0 .. hop N  (N <= MAX_FOLLOWUP_TURNS = 2)      [exploration]
       stream -> extract blocks -> apply each, confirmed
       write/edit/delete success -> append rel path to `mutated`
       run/fetch/search/symbol output or failed/malformed edit
          -> truncated -> next hop delta (existing behaviour)
       no results, or hop budget spent -> leave the loop
  -> verification phase                                  [new, not a loop]
       if verify disabled, or `mutated` empty  -> done, 0 extra model calls
       discover(project_root, mutated)
       none discovered -> ui.info("unsupported project"), done
       ui.confirm(argv)  -- declined -> done
       execute argv, shell=False, cwd=project_root, timeout
       status OK / NO_TESTS -> ui.success, done, 0 extra model calls
       status FAILED / TIMEOUT
          -> failure_feedback() -> ONE repair model call (delta + cached kv)
          -> apply that call's blocks, confirmed; results NOT fed back
          -> if it mutated anything: ONE final verification, confirmed
          -> report result to the human only; never another model call
  -> return kv context to the caller (unchanged contract)
```

## 6. Interfaces

Exact signatures to implement. Everything is `from __future__ import
annotations`, stdlib only.

### 6.1 `pathpolicy.py` (new)

```python
@dataclass(frozen=True)
class PathDecision:
    ok: bool
    path: Path | None      # resolved absolute path, None when not ok
    relpath: str           # path relative to project_root, "" when not ok
    reason: str            # "" when ok -- stable text, asserted in tests
    suggestion: str        # "" when ok

def resolve_for_mutation(project_root: str, raw_path: str) -> PathDecision: ...
```

Checks, in this order (first failure wins):

| # | Check | `reason` | `suggestion` |
|---|---|---|---|
| 1 | `raw_path.strip()` is empty | `empty path` | `name the file to change, relative to the project root` |
| 2 | `(root / raw).resolve()` is not `root` or below it (symlinks resolved, so a symlink pointing outside is caught) | `path is outside project root` | `use a path relative to the project root` |
| 3 | any component of the relative path equals `.git` (case-insensitive) | `path is inside the .git directory` | `never modify git internals; change tracked files instead` |
| 4 | `context.denylist.is_denied(resolved.name)` | `path looks like a credential or key file` | `create or edit credential files by hand, outside localcoder` |
| 5 | resolved path exists and is a directory | `path is a directory` | `name a file, not a directory` |

Check 3 is **component equality**, never a prefix match: that is precisely
what keeps `.gitignore`, `.gitattributes`, `.gitmodules` and
`.github/workflows/ci.yml` writable while blocking `.git/config` and
`sub/.git/hooks/pre-commit`.

`reason` for check 2 keeps the exact existing wording (`path is outside
project root`) so today's assertions in `tests/test_actions.py` continue to
pass unchanged.

### 6.2 `textfile.py` (new)

```python
BOM = "\ufeff"

@dataclass(frozen=True)
class TextFile:
    text: str    # exact decoded content, BOM stripped, EOLs NOT normalized
    eol: str     # dominant EOL: "\r\n" or "\n"
    bom: bool
    mixed_eol: bool

def detect_eol(text: str) -> tuple[str, bool]:  # (dominant, mixed?)
def read(path: Path) -> TextFile:               # raises UnicodeDecodeError / OSError
def write(path: Path, text: str, *, bom: bool) -> None
def to_eol(text: str, eol: str) -> str          # normalizes to "\n" first, then renders
```

- `read()` opens bytes, strips a leading UTF-8 BOM, decodes with strict
  `utf-8`. No `errors="replace"` anywhere in the mutation path.
- `detect_eol()` counts `\r\n` occurrences against bare `\n` occurrences;
  CRLF wins only on a strict majority; empty/one-line content yields
  `("\n", False)`.
- `write()` opens with `newline=""` so Python performs no translation, and
  re-prepends the BOM when `bom=True`.

### 6.3 `actions.py` (edited)

```python
@dataclass
class MalformedEdit:
    path: str
    body: str

def extract_malformed_edits(model_output: str) -> list[MalformedEdit]: ...
```

`extract_edits()` keeps its current signature and return type
(`list[FileEdit]`) so the existing parser tests are untouched;
`extract_malformed_edits()` returns the fences it skipped.

`apply_write(project_root, write, confirm=True) -> bool` becomes:

1. `pathpolicy.resolve_for_mutation` -- refusal prints
   `ui.error(f"refusing to write {path}: {reason}")` and returns `False`.
2. If the target exists: `textfile.read()`. `UnicodeDecodeError` ->
   `ui.error("refusing to overwrite {path}: file is not valid UTF-8")` and
   return `False`. (Rationale: the y/N prompt is the real gate, and a
   prompt localcoder cannot show a diff for is not a gate. The escape hatch
   is an explicit `delete` -- itself confirmed -- followed by a `write`.)
3. Render the model's content to the existing file's dominant EOL and BOM;
   a new file gets `"\n"` and no BOM. `mixed_eol` prints one
   `ui.warn("{path} has mixed line endings; writing all lines as CRLF/LF")`.
4. If the rendered content equals the existing content byte for byte:
   `ui.sub("{path} already has this content -- nothing to write")`, return
   `False` (no prompt, no commit).
5. Secret scan (unchanged), then unified diff of old vs. new (both
   normalized to `\n` for the comparison so a CRLF file does not show every
   line as changed), capped at `MAX_DIFF_LINES = 200` -- first 150 lines,
   `...(N diff lines elided)...`, last 50.
6. y/N, then `mkdir` + `textfile.write`, both inside `try/except OSError`
   -> `ui.error`, return `False`.
7. `gitsafety.commit_change(project_root, f"write {rel}", [rel])`.

`apply_edit(project_root, edit, confirm=True) -> EditResult` becomes:

1. `pathpolicy.resolve_for_mutation`; on refusal return
   `EditResult(False, format_action_error(action="edit", reason=..., path=..., suggestion=...))`.
2. Missing / not a file -> unchanged message (`target file does not exist`).
3. `textfile.read()`; `UnicodeDecodeError` -> `EditResult(False, ...
   reason="file is not valid UTF-8 -- refusing to edit it", suggestion=
   "this file is not text localcoder can safely edit")`. `OSError` ->
   `EditResult(False, ... reason="could not read the file: {e}")`.
4. Empty search -> unchanged (`empty search block`), still checked *before*
   counting (`str.count("")` gotcha, `docs/LESSONS_LEARNED.md`).
5. **EOL-tolerant matching, no whole-file normalization.** Build candidate
   needles `[search]` plus `search.replace("\n", "\r\n")` when that differs.
   Count occurrences of each candidate in the raw text; the *sum* must be
   exactly `1`, otherwise the existing message
   `search block matched {n} times (need exactly 1)` is returned. The
   candidate that matched selects the EOL used to render the replacement.
   Splicing with `raw.replace(needle, rendered_replace, 1)` leaves every
   byte outside the replaced region untouched -- CRLF, mixed EOLs and BOM
   all survive by construction.
6. Secret scan, diff, y/N, `textfile.write` (all as today, plus
   `except OSError` -> `EditResult(False, ...)`).
7. `gitsafety.commit_change(project_root, f"edit {rel}", [rel])`.

`apply_delete(project_root, path, confirm=True) -> bool`: same path policy
gate, and `unlink()` wrapped so a `FileNotFoundError` race or a
`PermissionError` is reported, not raised. Commits with `[rel]`.

Refusals from `write`/`delete` are **display-only** and do not start a
follow-up hop; only `edit` has a feedback channel (`EditResult.error`).
That matches today's rule -- "successful or refused write/delete never
costs a hop" -- and keeps the model-call bound in §7.8 provable.

### 6.4 `gitsafety.py` (edited)

```python
COMMIT_PREFIX = "localcoder: "
UNDO_SCAN_LIMIT = 50
_REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{40})\.")

def commit_change(project_root: str, message: str, paths: Sequence[str]) -> None: ...
def undo_last(project_root: str) -> tuple[bool, str]: ...
```

`commit_change` (third parameter is required -- the single caller,
`actions.py`, always knows the path):

1. Not a git repo -> return.
2. `git add -- <each path>` (records deletions too).
3. `git diff --cached --quiet -- <paths>`; exit 0 means nothing staged for
   those paths -> return without committing (no empty-commit noise).
4. `git commit -m "{COMMIT_PREFIX}{message}" -- <paths>`. The pathspec on
   `commit` is what guarantees isolation: measured above, a user's
   separately staged file stays staged and out of the commit.
5. Every git error is still swallowed -- a failed safety-net commit must
   never look like a failed action.

`undo_last` -- exact semantics:

1. Not a git repo -> `(False, "not a git repo -- nothing to undo this way")`.
2. `git rev-parse --verify -q HEAD` fails -> `(False, "no commits yet")`.
3. Read the last `UNDO_SCAN_LIMIT` commits as
   `git log -n 50 --pretty=%H%x1f%s%x1f%b%x1e`. Collect the set of SHAs
   already reverted by scanning bodies with `_REVERTS_RE` (git's own
   `This reverts commit <sha>.` body, measured above -- SHA matching, not
   subject matching, so two commits with identical subjects can never be
   confused).
4. Target = the newest commit in that window whose subject starts with
   `COMMIT_PREFIX` and whose SHA is not in the reverted set. None ->
   `(False, "nothing localcoder committed in the last 50 commits is left to undo")`.
   A commit the user made is never a candidate, exactly as today.
5. Changed paths = `git diff-tree --root --no-commit-id --name-only -r <sha>`
   (`--root` is required for a root commit).
6. Dirty check: `git status --porcelain -- <changed paths>`. Any output ->
   `(False, "uncommitted changes in <paths> would be overwritten -- commit or stash them first")`.
   This pre-empts git's own exit-128 refusal with a message that names the
   fix, and it is why `/undo` can never destroy uncommitted work: the only
   destructive branch is refused before git is asked to do anything.
7. `git revert --no-edit --no-rerere-autoupdate <sha>`, timeout 30s.
   - exit 0 -> `(True, "reverted: <subject> (new commit <short sha>)")`.
   - exit != 0 -> if `git rev-parse --git-path REVERT_HEAD` exists on disk
     (a conflict left a revert in progress), run `git revert --abort` and
     report `(False, "revert conflicted and was rolled back -- resolve <subject> by hand")`.
     Otherwise (git refused before starting, exit 128) report
     `(False, "git refused the revert: <first stderr line>")`. `--abort` is
     called *only* when the marker exists, because on the exit-128 path
     `--abort` itself fails with 128.
8. Root commits are supported now (measured), so the old "can't auto-undo
   the repo's very first commit" refusal is removed.

Accepted trade-off, documented in README: `/undo` adds a revert commit
instead of erasing history, and it is not a toggle -- a commit already
reverted is skipped by step 3, so pressing `/undo` repeatedly walks
backwards through localcoder's own commits rather than flip-flopping one.

### 6.5 `execution.py` (edited)

```python
class Status(str, enum.Enum):
    OK = "ok"                    # exit 0
    FAILED = "failed"            # exit != 0
    NO_TESTS = "no_tests"        # assigned by verification.py only (see below)
    TIMEOUT = "timeout"          # killed after timeout_s
    DENIED = "denied"            # denylist, never prompted
    DECLINED = "declined"        # y/N answered no (or stdin at EOF)
    LAUNCH_ERROR = "launch_error"  # OSError: binary missing, not executable

@dataclass(frozen=True)
class CommandResult:
    kind: str               # "shell" (model-emitted string) | "argv" (localcoder-built)
    display: str            # the model's string, or shlex.join(argv)
    status: Status
    exit_code: int | None   # None for DENIED/DECLINED/LAUNCH_ERROR/TIMEOUT
    stdout: str
    stderr: str
    duration_s: float
    timeout_s: int
    truncated: bool         # output hit MAX_OUTPUT_CHARS

    @property
    def ok(self) -> bool: ...          # status is OK
    @property
    def combined(self) -> str: ...     # stdout then stderr, blank parts dropped

def run_shell(project_root: str, command: str, timeout_s: int = TIMEOUT_S) -> CommandResult
def run_argv(project_root: str, argv: Sequence[str], timeout_s: int) -> CommandResult
def apply_run(project_root: str, command: str, confirm: bool = True) -> str | None
```

The `kind` field is the whole distinction the design needs: `"shell"` means
a string the **model** produced, so it goes through
`shell=True` *and* through `is_denied()` first; `"argv"` means a list
**localcoder** produced (a literal from `verification.py`, or the user's own
`verify_command` in `config.json`), so it goes through `shell=False` with no
shell metacharacter interpretation and no denylist -- there is no untrusted
string to filter. A model-emitted string is never turned into an argv list
and never bypasses the denylist; a discovered argv is never joined into a
shell string.

`run_shell()` calls `is_denied()` itself and returns
`Status.DENIED` without executing or prompting, so the denylist cannot be
bypassed by a future caller that forgets it; `apply_run()` maps that status
back to `None` for its existing contract. `Status.DECLINED` is produced by
the confirming layers (`apply_run`, `verify_project`), never by the runners.

`Status.NO_TESTS` is never produced by `execution.py`, which cannot know a
tool's exit-code semantics. `verification.py` reclassifies exit code 5 to
`NO_TESTS` for the `unittest` and `pytest` kinds only (measured: `unittest
discover` exits 5 when it collects nothing; pytest uses the same code for
"no tests collected").

Both runners share one capture helper:

```
Popen(..., stdout=PIPE, stderr=PIPE, text=True, cwd=project_root,
      start_new_session=True)
try: out, err = proc.communicate(timeout=timeout_s)
except TimeoutExpired:
    os.killpg(os.getpgid(proc.pid), SIGTERM); wait 3s;
    still alive -> os.killpg(..., SIGKILL)
    out, err = proc.communicate()      # collect what was produced first
    -> Status.TIMEOUT
```

`start_new_session=True` + `killpg` is what fixes the measured
"timeout leaves the work running" behaviour, for `shell=True` grandchildren
as well.

`apply_run()` keeps its exact current signature, return type (`str | None`)
and returned text format (`"$ {cmd}\n(exit {n})\n{output}"`, or the
`(command timed out after Ns: cmd)` string), so `main.py`'s existing call
sites and `tests/test_execution.py` are unaffected. It is now a thin wrapper
over `run_shell`.

### 6.6 `verification.py` (new)

```python
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
        return self.ran and self.result.status in (Status.FAILED, Status.TIMEOUT)

def discover(project_root: str, changed_paths: Sequence[str],
             override_argv: Sequence[str] | None = None) -> VerificationCommand | None
def verify_project(project_root: str, changed_paths: Sequence[str],
                   cfg: VerifyConfig, *, confirm: bool = True) -> VerificationOutcome
def failure_feedback(outcome: VerificationOutcome) -> str
def extract_failure_context(stdout: str, stderr: str,
                            max_chars: int = FEEDBACK_MAX_CHARS) -> str
def strip_ansi(text: str) -> str
```

**Discovery.** `override_argv` (from `config.json`'s `verify_command`) short
-circuits everything and yields `kind="override"`. Otherwise candidates are
built by cheap filesystem checks -- at most a handful of `Path.exists()`
calls plus one capped `package.json` read -- and each candidate whose
executable is missing from `shutil.which()` is dropped:

| Priority | Detected by | Exact argv |
|---|---|---|
| 1 | `pytest.ini`, or `pyproject.toml` containing `[tool.pytest`, or `setup.cfg` containing `[tool:pytest]` | `[sys.executable, "-m", "pytest", "-q", "-x"]` |
| 2 | `tests/` contains at least one `test_*.py` | `[sys.executable, "-m", "unittest", "discover", "-q", "-s", "tests", "-t", "."]` |
| 3 | at least one top-level `test_*.py` | `[sys.executable, "-m", "unittest", "discover", "-q"]` |
| 4 | `package.json` whose `scripts.test` exists, is a string, and is not npm's placeholder (`echo "Error: no test specified" && exit 1`) | `["npm", "test", "--silent"]` |
| 5 | `Cargo.toml` at the root | `["cargo", "test", "--quiet"]` |
| 6 | `go.mod` at the root | `["go", "test", "./..."]` |
| 7 | any `*.py` in `context.tree.list_source_files()` | `[sys.executable, "-m", "compileall", "-q", "."]` |
| -- | nothing above | `None` -> "unsupported project" |

`sys.executable` (not the string `"python3"`) guarantees the same
interpreter that is running localcoder, with no `PATH` ambiguity.

**Language-of-mutation preference.** The table above is the *tie-break*
order. Before applying it, candidates are stably re-sorted by the suffixes
of `changed_paths`: `.py`/`.pyi` promote kinds `pytest, unittest, pysyntax`;
`.js/.jsx/.ts/.tsx/.mjs/.cjs/.json` promote `npm`; `.rs` promotes `cargo`;
`.go` promotes `go`. A promoted candidate wins over a higher-priority
unpromoted one; within each group the table order decides. This is what
stops a README edit in a polyglot repo from triggering `cargo test`, and it
stays fully deterministic -- no model involvement, no heuristics beyond a
suffix map. `changed_paths` empty (the `/verify` command) means no
promotion, pure table order.

Discovery is recomputed per verification run rather than cached: it costs a
handful of `stat` calls against a turn that costs minutes, and a project
that just gained a `tests/` directory should be picked up immediately.

**`verify_project`.**

1. `cfg.enabled` false -> `VerificationOutcome(ran=False, skip_reason="verification disabled in config")`.
2. `discover(...)` returns `None` -> `ran=False,
   skip_reason="no verification command for this project"`; `main` reports
   it with `ui.info`, once per turn, never as an error.
3. `ui.info(f"verification: {label} (timeout {cfg.timeout_s}s)")` then
   `ui.confirm(f"  run verification `{label}`?")`. Declined ->
   `ran=False, skip_reason="verification declined"`, and a
   `CommandResult(status=DECLINED)` is attached for the record.
4. `execution.run_argv(project_root, cmd.argv, cfg.timeout_s)`; reclassify
   exit 5 to `NO_TESTS` for `pytest`/`unittest`.
5. Report to the human: `ui.success("verification passed")`,
   `ui.warn("verification ran no tests")`,
   `ui.error("verification failed (exit N)")`, or
   `ui.error("verification timed out after Ns")`, plus the extracted
   failure context via `ui.sub` on the failing paths.

**Failure extraction** (`extract_failure_context`):

1. `strip_ansi`: `\x1b\[[0-9;?]*[ -/]*[@-~]` (CSI) and
   `\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)` (OSC), then collapse carriage-return
   redraws by keeping `line.split("\r")[-1]` for each line -- progress bars
   from pytest/cargo/npm otherwise dominate the budget with noise.
2. Concatenate the non-blank of stdout, then stderr.
3. Find the first line matching any marker:
   `Traceback (most recent call last)`, `^E   ` (pytest), `AssertionError`,
   `SyntaxError`, `^FAILED`, `^FAIL\b`, `^--- FAIL`, `^ERROR[: ]`,
   `^error\[E\d+\]`, `^error:`, `panicked at`, `npm ERR!`.
   Found at index `i` -> keep `lines[max(0, i - 3) : i + 60]` (three lines
   of lead-in for the failing test's name).
4. Always append the last 15 lines (the summary line -- `FAILED
   (failures=1)`, `test result: FAILED`, `2 passed, 1 failed` -- is where a
   7B model orients fastest), separated by `...` when they are not already
   contiguous with the kept region.
5. No marker matched -> the last 60 lines.
6. Hard cap through `context.truncate.truncate_text(text,
   max_chars=FEEDBACK_MAX_CHARS)` with `FEEDBACK_MAX_CHARS = 2000`. That is
   half of `MAX_OUTPUT_CHARS`, deliberately: this text is appended to a task
   that has already grown across up to two hops, on a machine where every
   prompt character is paid for in prefill time.

**Repair prompt** (`failure_feedback`) -- the labeled-field shape
`actions.format_action_error` already established for this model:

```
--- VERIFICATION FAILED AFTER YOUR CHANGE ---
command:
python3 -m unittest discover -q -s tests -t .
exit code:
1
output (trimmed):
<extracted context>

Fix the cause with a single ```edit block on the file that is wrong.
Do not run commands, do not fetch, do not search.
--- END VERIFICATION RESULT ---
```

For a timeout, `exit code:` is replaced by
`result:\ntimed out after {n}s` and the partial output captured before the
kill is used.

### 6.7 `config.py` (edited)

Three keys added to `DEFAULTS`:

```python
"verify_after_change": True,   # run the discovered check after a mutation
"verify_timeout_s": 180,       # per verification execution
"verify_command": None,        # list[str] argv override; skips discovery
```

`main()` validates `verify_command`: anything other than `None` or a
non-empty `list[str]` produces `ui.warn("verify_command must be a list of
strings -- ignoring it")` and falls back to discovery. No other config
changes; `derive_max_context_chars()` is untouched.

The number of repair hops is **not** configurable: `MAX_REPAIR_HOPS = 1` is
a constant in `main.py`. Making it a config key would put the one bound
that prevents an unbounded fix/verify loop into a file the user can edit
casually, and there is no evidence a second hop helps a 7B model.

### 6.8 `main.py` (edited)

```python
MAX_REPAIR_HOPS = 1

def run_turn(agent, task, context, project_root, cce, num_ctx,
             max_total_context_chars, initial_kv_context=None,
             verify: verification.VerifyConfig | None = None) -> list[int] | None
```

`verify=None` means "no verification", which keeps every existing test in
`tests/test_main.py` valid without edits; `main()` always passes a real
`VerifyConfig`.

Two internal helpers keep the new phase from duplicating the budget logic:

```python
def _append_result(task, current_task, results, max_total_context_chars,
                   num_ctx, kv_context) -> tuple[str, str, list[int] | None]
    """Returns (new current_task, delta to send, kv_context or None).
    Exactly today's logic, extracted: append when it fits; otherwise warn,
    rebuild current_task from `task` plus the most recent result only, and
    drop the cached kv_context because it covers text we just dropped.
    `delta` is only meaningful when the returned kv_context survives; the
    caller sends `current_task` instead whenever it comes back None."""

def _apply_blocks(output, project_root, cce, mutated) -> list[str]
    """Applies every block in `output` in today's order (writes, deletes,
    shell suggestions, edits, runs, fetches, searches, symbols), appends
    each successfully mutated relative path to `mutated`, and returns the
    truncated action results that would feed a follow-up hop."""
```

**Turn state** is four locals, not a state machine: `current_task`,
`hop_kv_context`, `mutated: list[str]` (ordered, de-duplicated), and the
`hop` counter. The repair phase is straight-line code after the exploration
loop -- there is no loop around it, which is how "exactly one repair hop"
is enforced structurally rather than by a counter that could be
miscomputed. `MAX_REPAIR_HOPS = 1` exists as the documented name of that
bound and is asserted in tests.

Repair phase, in full:

```python
outcome = verification.verify_project(project_root, mutated, verify)
if outcome.needs_repair:
    current_task, delta, hop_kv_context = _append_result(
        task, current_task, [verification.failure_feedback(outcome)],
        max_total_context_chars, num_ctx, hop_kv_context)
    prompt, ctx = (delta, "") if hop_kv_context is not None else (current_task, context)
    output, usage, hop_kv_context = stream_and_print(
        agent.run_stream(prompt, ctx, kv_context=hop_kv_context))
    print_usage_summary(usage, num_ctx)
    repair_mutated: list[str] = []
    _apply_blocks(output, project_root, cce, repair_mutated)   # results discarded
    if repair_mutated:
        final = verification.verify_project(project_root, repair_mutated, verify)
        # reported to the human by verify_project; never fed back anywhere
```

Blocks emitted by the repair call are still applied and still individually
confirmed (a ` ```run ` in the repair response executes if the human says
yes, and its output is shown) -- but the returned action results are
discarded rather than appended, so no code path can start another model
call after the repair call.

REPL changes:

- `/verify`: one call to `verification.verify_project(project_root, [],
  cfg)` -- discovery happens inside it, and a `ran=False` outcome is
  reported with `ui.warn(outcome.skip_reason)`. It never makes a model
  call: it is a REPL command, not a turn. `changed_paths=[]` means no
  language promotion, so `/verify` always follows plain table order.
- BANNER line for `/verify` becomes
  `run this project's test/check command (discovered, confirmed)`.
- A top-level input line whose lowercase strip is in
  `{"y", "n", "yes", "no"}` is ignored with
  `ui.info("nada a confirmar agora -- linha ignorada")` instead of being
  sent to the model. This costs nothing interactively and makes a piped
  script with a spare `y` harmless instead of triggering a multi-minute
  phantom turn (see §7.6).

### 6.9 `ui.py` (edited)

```python
def confirm(prompt: str) -> bool:
    try:
        answer = input(f"{bold(prompt)} [y/N] ").strip().lower()
    except EOFError:
        print("  (sem entrada disponível -- assumido 'n')", flush=True)
        return False
    return answer == "y"
```

EOF is treated as "no", never as "yes". This is the only change to `ui.py`
and it keeps the module project-import free.

**Message language.** New human-facing strings in `pathpolicy.py`,
`textfile.py`, `actions.py`, `execution.py`, `gitsafety.py` and
`verification.py` are English, matching those files today; new strings
inserted into `main.py`'s REPL are Portuguese, matching its surrounding
lines. The exact strings tests assert on are the ones quoted in this
document.

## 7. Resolved design decisions

### 7.1 Safe `/undo`
Revert, never reset (§6.4). Non-destructive by construction: history grows,
never shrinks, and the only branch that could touch uncommitted work is
refused before git runs. Dirty worktree on an affected path -> refuse with
a message naming the paths and the fix. Conflict -> `git revert --abort`
(only when `REVERT_HEAD` exists) and report. Root commits are now
supported. Repeated `/undo` walks backwards through localcoder's own
un-reverted commits, identified by SHA in later commits' `This reverts
commit <sha>.` bodies.

### 7.2 Path policy and error shape
One function, one frozen `PathDecision`, five ordered checks, stable
`reason`/`suggestion` strings (§6.1). Component-equality matching on `.git`
is what keeps `.gitignore` writable. Humans see
`ui.error("refusing to <action> <path>: <reason>")`; the model sees the
existing `format_action_error` block, and only for `edit`.

### 7.3 `CommandResult` and the two command kinds
Fields and statuses in §6.5. `kind="shell"` = model string, `shell=True`,
denylist enforced. `kind="argv"` = localcoder/user list, `shell=False`, no
denylist needed because there is no untrusted string. `NO_TESTS` is a
verification-layer reclassification of exit 5, not something `execution.py`
invents.

### 7.4 Discovery priority and exact commands
Table in §6.6, with language-of-mutation promotion as the pre-sort and
`shutil.which()` gating each candidate. Exact argv lists are literals in
`verification.py`; `sys.executable` for anything Python.

### 7.5 Mutation tracking and turn state
Four locals (§6.8), one `_apply_blocks` helper that appends successfully
mutated relative paths, straight-line repair phase. No state-machine class,
no per-hop verification: verification runs once after the exploration loop
ends, because running the suite between hops would test a half-finished
change and multiply the cost on hardware where each execution is real wall
time.

`BASE_SYSTEM_PROMPT` is deliberately unchanged. Everything the model needs
to know about verification arrives in the follow-up delta of the one turn
where it matters, so the cached system-prompt prefix stays byte-identical
and the measured 4.84x prompt-eval saving from KV reuse
(`docs/BENCHMARKS.md`) is preserved. Adding verification instructions to the
system prompt would tax every turn for something relevant to a minority of
them.

### 7.6 Verification confirmation under piped stdin
Each verification execution consumes exactly one stdin line, always the
last prompt(s) of a turn, in a fully deterministic order: action-block
confirmations first (writes, deletes, edits, runs, fetches, searches -- the
existing order in `run_turn`), then the verification confirmation, then
(only if the repair call mutated something) the repair call's action
confirmations and the final verification confirmation. A live script can
therefore be written exactly. Two guards make it robust anyway: EOF is
"no" (§6.9), so an under-supplied script declines and exits cleanly with
returncode 0 instead of crashing on `EOFError`; and a spare `y` at the REPL
prompt is a logged no-op (§6.8) instead of a phantom turn.

### 7.7 Failure extraction
ANSI CSI+OSC stripping, `\r` redraw collapsing, first-marker window
(`-3 .. +60` lines) plus the last 15 lines, fallback to the last 60 lines,
hard cap 2000 chars via `context.truncate.truncate_text` (§6.6).

### 7.8 Model-call and hop bounds, every path

| Path | Model calls | Verification executions |
|---|---|---|
| No action blocks | 1 | 0 |
| Successful write/edit/delete only, verification passes / no tests / declined / unsupported / disabled | 1 | 0 or 1 |
| Same, verification fails, repair emits no mutation | 2 | 1 |
| Same, verification fails, repair mutates | 2 | 2 |
| Two exploration hops, no mutation | 3 | 0 |
| Two exploration hops + mutation + failing verification | 4 | 1 or 2 |
| `/verify` REPL command | **0** | 1 |

Absolute bound: `1 + MAX_FOLLOWUP_TURNS + MAX_REPAIR_HOPS = 4` model calls
and 2 verification executions per turn. The repair call's own action
results are discarded, and the final verification's result is never fed
back -- those two rules are what make the bound structural rather than
arithmetic. Ollama KV reuse is preserved throughout: the repair call sends
only the delta with the previous hop's context array, falling back to a
full resend exactly when `_append_result` had to drop the cache. A
verification run longer than `OLLAMA_KEEP_ALIVE` could cost a re-prefill,
never a wrong answer; at the 180s default versus a 30m keep-alive this
cannot happen in practice.

### 7.9 Backward compatibility and config defaults
- Fenced protocol, block kinds, fence-length rule, and system prompt: all
  unchanged.
- `execution.apply_run`, `actions.extract_edits`, `actions.EditResult`,
  `actions.format_action_error`, `run_turn`'s positional parameters and
  return type: unchanged.
- `gitsafety.commit_change` gains a required third parameter (`paths`);
  `actions.py` is its only caller, and `tests/test_gitsafety.py` is updated
  with it.
- `/undo`'s return contract `(bool, str)` is unchanged; its behaviour and
  messages change, which is the point of the slice.
- `/verify`'s command changes from hardcoded `compileall` to discovery. No
  test asserts the old string.
- New config keys default to on with a conservative timeout
  (`verify_after_change: true`, `verify_timeout_s: 180`,
  `verify_command: null`); an existing `config.json` with none of them
  behaves as designed. `verify_after_change: false` is the kill switch for
  anyone on slower hardware.
- Behaviour changes a user can notice: writes to `.env`-family and `.git/**`
  paths are now refused (escape hatch: create them by hand); overwriting a
  non-UTF-8 file is refused (escape hatch: `delete` then `write`); an
  overwrite that would change nothing is skipped rather than committed.

## 8. Security invariants

Each is testable and gets at least one test.

1. No write, edit or delete resolves outside the project root, into `.git/`,
   or onto a credential/key path -- enforced in one function that all three
   actions call.
2. Every write, edit, delete, run, fetch, search **and every verification
   execution** requires an explicit y/N. There is no session-level "always
   yes", no `--yes` flag, and no env var that bypasses a prompt.
3. EOF on stdin is "no". A non-interactive run can never auto-approve.
4. Model-emitted command strings never reach `shell=False` argv execution,
   and localcoder-built argv lists are never joined into a shell string.
   Model strings still pass `execution.is_denied()` first.
5. Verification argv elements are literals from `verification.py` or the
   user's own `config.json`; no model output ever becomes an argv element.
6. `git add` is always path-scoped; `git add -A` does not appear in the
   codebase after this phase.
7. `/undo` never runs `git reset --hard`, never rewrites history, and never
   touches a commit without the `localcoder: ` prefix.
8. A verification or model-emitted command that exceeds its timeout has its
   entire process group killed.
9. Credential-file content never enters the model's context (existing
   `context/denylist.py` behaviour, now mirrored on the write side).
10. CCE remains optional: every path degrades to raw reads, and no
    verification behaviour depends on it.

## 9. Error handling

| Situation | Behaviour |
|---|---|
| Path refused by policy (write/delete) | `ui.error`, action skipped, no hop, no commit |
| Path refused by policy (edit) | `EditResult(False, structured ERROR)`, fed back within the existing hop budget |
| Target file not valid UTF-8 | write refused with `ui.error`; edit refused with structured ERROR; file untouched |
| `OSError` on mkdir/read/write/unlink | `ui.error` (edit: structured ERROR), action fails, CLI continues |
| Overwrite with identical content | skipped before the prompt, no commit |
| Malformed `edit` fence | structured ERROR (`reason="edit block is not a valid SEARCH/REPLACE pair"`), fed back |
| Any git failure in `commit_change` | swallowed; the on-disk action already succeeded |
| `/undo` dirty worktree / conflict / nothing to undo | `(False, message)`, worktree untouched, no revert left in progress |
| Verification binary missing | candidate dropped at discovery (`shutil.which`); if none remain, "unsupported project" |
| Verification launch fails anyway | `Status.LAUNCH_ERROR`, treated as *not* a failure -- reported, no repair (a missing toolchain is not a code defect the model can fix) |
| Verification exit 5 (unittest/pytest) | `Status.NO_TESTS`, reported, no repair |
| Verification timeout | `Status.TIMEOUT`, process group killed, partial output fed to the single repair call |
| Verification declined | `ran=False`, no repair, turn ends |
| Repair call raises `OllamaError` | `ui.error`, turn ends returning the last known kv context (existing pattern) |
| Follow-up budget exceeded when appending the repair block | existing drop-history branch: warn, rebuild from `task` + newest result, drop the kv cache |
| EOF at any confirmation | treated as "no" |
| Ctrl-C anywhere | existing `KeyboardInterrupt` handler in `main()` |

## 10. Testing and acceptance criteria

All fast tests must stay offline, Ollama-free, and keep the full suite
under a couple of seconds. Every git test builds a real temp repo (the
existing `_init_repo` pattern in `tests/test_gitsafety.py`).

**New: `tests/test_pathpolicy.py`**
- rejects `.git/config`, `sub/.git/hooks/pre-commit`, `.git` itself;
- allows `.gitignore`, `.gitattributes`, `.gitmodules`,
  `.github/workflows/ci.yml`;
- rejects `.env`, `.env.local`, `id_ecdsa`, `deploy.pem`, `secrets.yaml`;
- rejects `../../etc/passwd`, an in-root symlink pointing outside the root,
  an existing directory, and the empty path;
- each rejection carries the exact `reason` string from §6.1.

**New: `tests/test_textfile.py`**
- `detect_eol` for pure LF, pure CRLF, mixed (dominant + `mixed_eol=True`),
  empty;
- read/write round-trip is byte-identical for CRLF and for BOM+CRLF;
- `read()` raises `UnicodeDecodeError` on invalid UTF-8 bytes.

**`tests/test_actions.py` (additions)**
- editing one line of a CRLF file leaves every other line's `\r\n` intact
  and the edited line CRLF (byte comparison);
- editing a file with a BOM preserves the BOM;
- editing a non-UTF-8 file returns a structured error and leaves the file
  byte-identical;
- `write` over `.git/config` and over `.env` is refused; over `.gitignore`
  is allowed;
- `write` over an existing file prints a unified diff before the prompt;
- `write` with identical content returns `False` and does not commit;
- `extract_malformed_edits` finds the fence that `extract_edits` skips, and
  `run_turn` turns it into an `ERROR:` block;
- all existing assertions in this file still pass unmodified.

**`tests/test_gitsafety.py` (rewritten around the new contract)**
- `commit_change(root, msg, ["a.py"])` commits only `a.py` while a
  separately staged `b.py` stays staged and uncommitted;
- `commit_change` with nothing actually changed creates no commit;
- `undo_last` creates a revert commit (commit count +1, previous commit
  still present, file content restored);
- `undo_last` succeeds on a repository whose only commit is localcoder's
  (the previously refused root-commit case);
- `undo_last` refuses when a path the target commit touched is dirty, and
  the worktree is untouched afterwards;
- `undo_last` on a conflicting revert returns `False` and leaves no
  `REVERT_HEAD` and no `UU` entry;
- `undo_last` refuses a human commit;
- two consecutive `undo_last` calls revert two different localcoder
  commits, never the revert commit itself.

**`tests/test_execution.py` (additions)**
- `run_argv` passes shell metacharacters literally (`["printf", "%s",
  "$HOME;ls"]` produces the literal string);
- `run_argv` with a 1s timeout on a 30s sleep returns `Status.TIMEOUT` in
  under 5s and the child process is gone (`os.kill(pid, 0)` raises);
- `CommandResult` fields populated for OK and FAILED;
- `run_shell` still refuses a denylisted command without prompting;
- existing `apply_run` tests pass unmodified.

**New: `tests/test_verification.py`**
- discovery for each fixture: `pytest.ini`; `tests/test_x.py`; top-level
  `test_x.py`; `package.json` with a real test script; `package.json` with
  the npm placeholder (rejected, falls through); `Cargo.toml`; `go.mod`;
  Python-only fallback; empty directory -> `None`;
- `shutil.which` patched to `None` drops the npm/cargo/go candidate;
- polyglot fixture: `changed_paths=["src/lib.rs"]` picks cargo,
  `["app.py"]` picks the Python candidate, `[]` picks table order;
- `override_argv` wins over everything and yields `kind="override"`;
- exit 5 becomes `NO_TESTS` for unittest/pytest and stays `FAILED` for
  other kinds;
- `strip_ansi` removes CSI and OSC sequences and collapses `\r` redraws;
- `extract_failure_context` keeps the traceback and the final summary line,
  drops the middle, and never exceeds 2000 chars;
- `failure_feedback` contains the command, the exit code and the
  `VERIFICATION FAILED` marker;
- `verify_project` declined at the prompt returns `ran=False` and never
  executes anything (patched `run_argv` asserts it was not called).

**`tests/test_main.py` (additions -- the deterministic lifecycle tests)**
Using the existing `_FakeAgent`/`_FakeCCE` plus `mock.patch` on
`verification.verify_project`:
- mutation + passing verification: exactly 1 model call, 1 verification,
  no `VERIFICATION FAILED` anywhere in any prompt;
- mutation + failing verification: exactly 2 model calls; the second
  prompt contains `VERIFICATION FAILED` and the extracted output; exactly 2
  verifications when the repair mutates, 1 when it does not;
- repair response containing a ` ```run ` block: the run executes, and
  there is still no third model call;
- declined verification: 1 model call, no repair;
- no mutation at all: `verify_project` never called;
- two exploration hops then a failing verification: exactly 4 model calls,
  never 5;
- the repair call reuses the previous hop's kv context and sends the delta
  only; after the drop-history branch it falls back to a full resend with
  `kv=None`;
- `verify=None` reproduces today's behaviour exactly (all pre-existing
  tests in this file pass unmodified).

**`tests/test_live.py` (one new real end-to-end case, same
`LOCALCODER_LIVE_TESTS=1` gate, timeout 900s)**

Fixture: `calc.py` with `def divide(a, b): return a / b` and
`tests/test_calc.py` asserting that `divide(1, 0)` raises `ValueError` and
`divide(4, 2) == 2`. Script:

```
/files calc.py tests/test_calc.py
Make tests/test_calc.py pass by fixing calc.py.
y      <- apply the write/edit
y      <- run verification
y      <- possible repair edit
y      <- final verification
/quit
```

Assertions:
1. exit code 0 (this alone regression-tests the EOF crash, since surplus
   `y` lines are now no-ops and a missing one declines cleanly);
2. running `python3 -m unittest discover -s tests` in that project after
   the session exits 0 -- real behaviour, not a grep of the source;
3. stdout contains `run verification` at least once and the discovered
   label `unittest discover`;
4. stdout contains at most one `VERIFICATION FAILED` marker, proving the
   single-repair bound against a real model.

**Acceptance criteria for the phase**
- `python3 -m unittest discover tests` is green and still runs in ~1s.
- `rg "git add -A"` finds nothing outside tests' own fixtures.
- `rg "reset --hard"` finds nothing.
- Every new module compiles under `python3 -m py_compile`.
- `README.md`, `docs/BACKLOG.md` and `docs/LESSONS_LEARNED.md` updated in
  the same commits (§11), not afterwards.

## 11. Rollout order

Each step ends with the full fast suite green and its own commit; Slice A
ships completely before Slice B starts.

**Slice A**
1. `pathpolicy.py` + tests; route `apply_write`/`apply_edit`/`apply_delete`
   through it.
2. `gitsafety.py`: path-scoped `commit_change` (+ caller update), then
   revert-based `undo_last`; rewrite `tests/test_gitsafety.py`.
3. `textfile.py` + tests; CRLF/BOM/strict-UTF-8 handling and
   diff-on-overwrite in `actions.py`.
4. `ui.confirm` EOF handling; `OSError` wrapping across the three actions;
   `extract_malformed_edits` + its `run_turn` feedback.
5. Docs: README security/actions sections, BACKLOG "Done" entries,
   LESSONS_LEARNED entries for the measured git and `unittest` exit-code
   behaviours.

**Slice B**
6. `execution.py`: `CommandResult`/`Status`, `run_shell`/`run_argv`,
   process-group timeout, `apply_run` as a wrapper.
7. `verification.py`: discovery, `verify_project`, ANSI stripping and
   failure extraction, `failure_feedback` + tests.
8. `config.py` keys and validation; `main.py` `_append_result` /
   `_apply_blocks` extraction, the repair phase, `/verify`, the REPL y/n
   no-op, BANNER text.
9. `tests/test_main.py` lifecycle tests, then the live case.
10. Docs: README "Verification and repair" section and the updated actions
    table footnote, BACKLOG, LESSONS_LEARNED.

## 12. Risks

| Risk | Mitigation |
|---|---|
| A discovered command is expensive or has side effects (`npm test` starting a server, `cargo test` compiling for minutes) | Per-execution y/N showing the exact argv, a 180s timeout, process-group kill, and `verify_after_change: false` |
| Verification roughly doubles turn wall time on the reference CPU | Only after a real mutation, at most twice per turn, both confirmed; the syntax fallback is near-instant for the common Python case |
| A flaky suite triggers a pointless repair call | Bounded to one call; the human confirms the run and sees the failure before the model does |
| The repair edit makes things worse | Every repair block is confirmed and auto-committed individually, and `/undo` is now non-destructive |
| Revert-based `/undo` leaves the reverted commit in history | Documented in README; SHA-based "already reverted" detection means repeated `/undo` walks backwards instead of flip-flopping |
| Refusing `.env` writes annoys someone with a legitimate need | Documented escape hatch (create it by hand); the denylist already governs the read side, so this is consistency, not a new restriction |
| Exit-code semantics differ per tool, so "failed" is occasionally wrong | Only unittest/pytest exit 5 is special-cased (measured); `LAUNCH_ERROR` is explicitly not a repair trigger; anything else is reported to the human before the model sees it |
| `shell=False` breaks a workflow that relied on shell features | Discovered commands never needed a shell; model-emitted ` ```run ` still uses `shell=True` unchanged |
| Extraction hides the one line that mattered | Marker window plus an always-included tail plus the full output printed to the terminal, so the human sees everything even when the model gets 2000 chars |
| A long verification evicts Ollama's KV cache | Costs one re-prefill, never correctness; 180s versus a 30m keep-alive makes it a non-event |
