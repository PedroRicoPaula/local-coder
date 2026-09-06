# Safe Mutation Foundation + Deterministic Verification/Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every model-driven mutation in localcoder safe by construction (path policy, byte fidelity, path-scoped git, non-destructive `/undo`, no orphaned processes), then run the project's own test command after a mutation and give the model exactly one deterministic repair attempt when it fails.

**Architecture:** Three new leaf/orchestration modules (`pathpolicy.py`, `textfile.py`, `verification.py`) plus edits to `actions.py`, `gitsafety.py`, `execution.py`, `ui.py`, `config.py` and `main.py`. Slice A is the blocking safety foundation and must be fully green before Slice B starts. Slice B adds deterministic verification discovery, confirmed `argv` execution, and a straight-line (non-looping) repair phase after `run_turn`'s existing exploration loop. `llm/prompts.py` is deliberately untouched so Ollama's cached system-prompt prefix stays byte-identical.

**Tech Stack:** Python 3.10+, standard library only (no pip packages, no build step). `unittest` for the fast suite, `git` CLI via `subprocess`, Ollama HTTP for the one live test.

**Spec:** `docs/superpowers/specs/2026-09-04-safe-verification-repair-design.md` — read it in full before starting. Every task argues from a numbered section of that spec; the spec travels with this plan.

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **No pip dependencies. Python 3.10+ stdlib only.** Every new module is `from __future__ import annotations`, stdlib only.
- **No provider abstraction, no second LLM backend.**
- **No index, no embeddings, no vector store, no telemetry.**
- **No extra LLM calls at runtime** for planning, reviewing, summarizing, choosing a verification command, or interpreting failures. Everything outside the single repair call is deterministic Python.
- **No new fenced block kinds**; the ` ```write / ```edit / ```delete / ```run / ```fetch / ```search / ```symbol / ```shell ` protocol is unchanged.
- **No change to `llm/prompts.py`'s `BASE_SYSTEM_PROMPT`** — load-bearing for KV-cache reuse (spec §7.5), not just conservatism.
- **No native tool-calling / JSON schema**; no CCE tool beyond `compress_file` / `get_symbol`.
- **No sandboxing, containers, or privilege dropping** for verification commands; the y/N prompt remains the gate.
- **No repair loop longer than one hop**, no "keep trying until green".
- **Every write, edit, delete, run, fetch, search AND every verification execution requires its own explicit y/N.** There is no session-level "always yes", no `--yes` flag, and no env var that bypasses a prompt.
- **EOF on stdin is "no".** A non-interactive run can never auto-approve.
- **`git add` is always path-scoped**; `git add -A` must not appear in production code after this phase.
- **`/undo` never runs `git reset --hard`**, never rewrites history, and never touches a commit without the `localcoder: ` prefix. Conflicts are aborted and leave no `REVERT_HEAD`.
- **Model-emitted command strings never reach `shell=False` argv execution**, and localcoder-built argv lists are never joined into a shell string. Model strings still pass `execution.is_denied()` first.
- **Verification argv elements are literals** from `verification.py` or the user's own `config.json`; no model output ever becomes an argv element.
- **A command that exceeds its timeout has its entire process group killed** (`start_new_session=True` + `os.killpg`).
- **Strict UTF-8 on the mutation path.** No `errors="replace"` anywhere a file is written back. CRLF stays CRLF, a UTF-8 BOM survives.
- **`unittest`/`pytest` exit code 5 means NO_TESTS**, never failure. Only those two kinds get that reclassification.
- **Verification runs only after a real mutation**, once, after the exploration loop — never between hops. Pass costs zero model calls; fail/timeout costs exactly one separate repair call; the final verification after a repair can never produce another model call.
- **Backward compatibility:** `execution.apply_run`, `actions.extract_edits`, `actions.EditResult`, `actions.format_action_error`, `gitsafety.undo_last`'s `(bool, str)` contract, and `run_turn(...) -> list[int] | None` with its existing positional parameters must all stay valid. `run_turn`'s new `verify` parameter is keyword-only-by-position-last and defaults to `None` (= today's behaviour).
- **KV semantics unchanged:** hop 0 sends the full task; later hops send only the delta with the previous hop's `context` array; the drop-history branch drops the cached array and falls back to a full uncached resend.
- **Message language:** new human-facing strings in `pathpolicy.py`, `textfile.py`, `actions.py`, `execution.py`, `gitsafety.py`, `verification.py` are English; new strings inserted into `main.py`'s REPL are Portuguese, matching surrounding lines. All strings tests assert on are quoted verbatim in this plan.
- **Every human-facing message goes through `ui.py`** (`info`/`success`/`warn`/`error`/`sub`/`confirm`), never bare `print()`.
- **The fast suite must stay offline, Ollama-free, and finish in ~1 second.**

---

## Baseline (read before Task 1)

The working tree on `feature/safe-verify-repair` already contains **uncommitted, in-scope-but-not-part-of-this-phase work** that must be preserved exactly:

- `actions.py` — the ` ```edit ` action (`EDIT_BLOCK_RE`, `EDIT_BODY_RE`, `FileEdit`, `EditResult`, `extract_edits`, `apply_edit`, `format_action_error`, `unified_diff`).
- `context/relevance.py` (new, untracked) — keyword ranking, `ScoredFile`, `select_files_for_task`, `format_selection`.
- `main.py` — `/context`, `/why`, ranked default file selection, the failed-edit follow-up hop.
- `llm/prompts.py` — the `edit` block documentation already added to `BASE_SYSTEM_PROMPT`.
- `tests/test_relevance.py` (new, untracked), plus additions in `tests/test_actions.py`, `tests/test_main.py`, `tests/test_live.py`.
- `README.md`, `docs/BACKLOG.md`, `docs/LESSONS_LEARNED.md`, `AGENTS.md` (new, untracked).

**Never** run `git checkout -- <path>`, `git restore`, `git stash drop`, `git reset --hard`, or `git clean` on this branch. **Never** rewrite `context/relevance.py` from scratch. Task 1 exists solely to get this baseline safely committed before anything else is touched.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `pathpolicy.py` | **Create** | One decision function answering "may this path be mutated?" Returns a frozen `PathDecision`. Imports stdlib + `context.denylist` only. |
| `textfile.py` | **Create** | UTF-8/BOM/EOL-preserving read+write primitives. Stdlib only. |
| `verification.py` | **Create** | Verification command discovery, confirmed argv execution, ANSI stripping, failure extraction, repair-prompt formatting. Imports `execution`, `ui`, `context.tree`, `context.truncate`. |
| `actions.py` | Modify | Routes write/edit/delete through `pathpolicy` + `textfile`; diff-on-overwrite; `extract_malformed_edits`. |
| `gitsafety.py` | Modify | Path-scoped `commit_change`; revert-based `undo_last`. |
| `execution.py` | Modify | `Status`/`CommandResult`, `run_shell`/`run_argv`, process-group timeout kill, `apply_run` as a thin wrapper. |
| `ui.py` | Modify | `confirm()` treats EOF as "no". Stays project-import free. |
| `config.py` | Modify | Three new `DEFAULTS` keys. |
| `main.py` | Modify | `_append_result` / `_apply_blocks` extraction, verification+repair phase, `/verify`, REPL y/n no-op, BANNER text. |
| `llm/prompts.py` | **Unchanged** | Deliberate (spec §7.5). |

No import cycles: `ui.py` stays project-import free; `pathpolicy.py` and `textfile.py` import only stdlib (+ `context.denylist`); `verification.py` sits above `execution` + `ui`; `main.py` sits on top of everything.

---

## Task 1: Commit the baseline before touching anything

**Model:** gpt-5.6-sol-medium (Git/process execution work)

**Files:**
- Modify: none — this task only records the existing working tree.

**Interfaces:**
- Consumes: nothing.
- Produces: a clean `git status` on `feature/safe-verify-repair` with every baseline file committed, so every later task's diff is reviewable in isolation.

- [ ] **Step 1: Confirm the branch and inspect the working tree**

```bash
cd /home/pedro/Documents/Progamacao/localcoder
git branch --show-current      # must print: feature/safe-verify-repair
git status --porcelain
```

Expected: `feature/safe-verify-repair`, and modified/untracked entries for `AGENTS.md`, `README.md`, `actions.py`, `context/relevance.py`, `docs/BACKLOG.md`, `docs/LESSONS_LEARNED.md`, `docs/superpowers/specs/2026-09-04-safe-verification-repair-design.md`, `llm/prompts.py`, `main.py`, `tests/test_actions.py`, `tests/test_live.py`, `tests/test_main.py`, `tests/test_relevance.py`, and this plan file.

If the branch is anything else, **stop** and report — do not switch branches.

- [ ] **Step 2: Run the full fast suite against the baseline**

Run: `python3 -m unittest discover tests`
Expected: PASS (this is the green starting point every later task compares against). If it is already red, **stop and report** — do not "fix" baseline work as part of this plan.

- [ ] **Step 3: Commit the baseline, path-scoped**

```bash
git add AGENTS.md README.md actions.py context/relevance.py \
        docs/BACKLOG.md docs/LESSONS_LEARNED.md docs/superpowers \
        llm/prompts.py main.py \
        tests/test_actions.py tests/test_live.py tests/test_main.py tests/test_relevance.py
git commit -m "feat: baseline edit action, ranked context, /context, spec and plan"
git status --porcelain    # must print nothing
```

- [ ] **Step 4: Verify nothing was lost**

```bash
git show --stat HEAD
python3 -m unittest discover tests
```

Expected: the stat lists all baseline files; the suite is still green.

---

# SLICE A — Blocking safety foundation

Slice A (Tasks 2–10) must be **completely finished and green** before Task 11 starts. Do not begin any Slice B task early, even if it looks independent.

---

## Task 2: `pathpolicy.py` — one mutation-path decision function

**Model:** composer-2.5-fast (complete-spec, 2-file mechanical task)

**Spec:** §6.1, §7.2, §8.1

**Files:**
- Create: `pathpolicy.py`
- Create test: `tests/test_pathpolicy.py`

**Interfaces:**
- Consumes: `context.denylist.is_denied(path: str) -> bool` (existing, `context/denylist.py:28`).
- Produces, for Tasks 3, 7 and 8:
  - `pathpolicy.PathDecision` — frozen dataclass with fields `ok: bool`, `path: Path | None`, `relpath: str`, `reason: str`, `suggestion: str`.
  - `pathpolicy.resolve_for_mutation(project_root: str, raw_path: str) -> PathDecision`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pathpolicy.py`:

```python
import os
import tempfile
import unittest
from pathlib import Path

import pathpolicy


class TestResolveForMutation(unittest.TestCase):
    def test_allows_an_ordinary_relative_path(self):
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, "src/app.py")
            self.assertTrue(decision.ok)
            self.assertEqual(decision.relpath, "src/app.py")
            self.assertEqual(decision.reason, "")
            self.assertEqual(decision.suggestion, "")
            self.assertEqual(decision.path, Path(root).resolve() / "src" / "app.py")

    def test_rejects_empty_path(self):
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, "   ")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "empty path")
            self.assertIsNone(decision.path)
            self.assertEqual(decision.relpath, "")

    def test_rejects_traversal_outside_root(self):
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, "../../etc/passwd")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "path is outside project root")
            self.assertEqual(decision.suggestion, "use a path relative to the project root")

    def test_rejects_symlink_pointing_outside_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            Path(outside, "target.txt").write_text("secret\n")
            os.symlink(Path(outside, "target.txt"), Path(root, "link.txt"))
            decision = pathpolicy.resolve_for_mutation(root, "link.txt")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "path is outside project root")

    def test_rejects_git_internals(self):
        with tempfile.TemporaryDirectory() as root:
            for raw in (".git/config", ".git/hooks/pre-commit", ".git", "sub/.git/hooks/pre-commit",
                        ".GIT/config"):
                decision = pathpolicy.resolve_for_mutation(root, raw)
                self.assertFalse(decision.ok, raw)
                self.assertEqual(decision.reason, "path is inside the .git directory", raw)
                self.assertEqual(
                    decision.suggestion,
                    "never modify git internals; change tracked files instead",
                )

    def test_allows_git_adjacent_files(self):
        with tempfile.TemporaryDirectory() as root:
            for raw in (".gitignore", ".gitattributes", ".gitmodules",
                        ".github/workflows/ci.yml"):
                decision = pathpolicy.resolve_for_mutation(root, raw)
                self.assertTrue(decision.ok, raw)

    def test_rejects_credential_and_key_files(self):
        with tempfile.TemporaryDirectory() as root:
            for raw in (".env", ".env.local", "id_ecdsa", "deploy.pem", "secrets.yaml",
                        "conf/.env.production"):
                decision = pathpolicy.resolve_for_mutation(root, raw)
                self.assertFalse(decision.ok, raw)
                self.assertEqual(decision.reason, "path looks like a credential or key file", raw)
                self.assertEqual(
                    decision.suggestion,
                    "create or edit credential files by hand, outside localcoder",
                )

    def test_rejects_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "adir").mkdir()
            decision = pathpolicy.resolve_for_mutation(root, "adir")
            self.assertFalse(decision.ok)
            self.assertEqual(decision.reason, "path is a directory")
            self.assertEqual(decision.suggestion, "name a file, not a directory")

    def test_git_check_wins_over_denylist_check(self):
        """Check order is fixed: .git component (3) is tested before the
        credential denylist (4), so a file inside .git named like a secret
        reports the .git reason."""
        with tempfile.TemporaryDirectory() as root:
            decision = pathpolicy.resolve_for_mutation(root, ".git/.env")
            self.assertEqual(decision.reason, "path is inside the .git directory")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails for the expected reason**

Run: `python3 -m unittest tests.test_pathpolicy -v`
Expected: FAIL — collection error `ModuleNotFoundError: No module named 'pathpolicy'`. That is the correct pre-implementation failure; anything else means the test file itself is wrong.

- [ ] **Step 3: Write the implementation**

Create `pathpolicy.py`:

```python
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
```

Note on the "outside root" wording: `"path is outside project root"` is copied verbatim from today's `apply_edit` error so the existing assertion in `tests/test_actions.py::TestApplyEdit::test_edit_refuses_path_traversal` (`self.assertIn("outside project root", result.error)`) keeps passing after Task 3.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_pathpolicy -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile pathpolicy.py
```

Expected: both silent/green, suite under ~2s.

- [ ] **Step 6: Commit**

```bash
git add pathpolicy.py tests/test_pathpolicy.py
git commit -m "feat: pathpolicy.resolve_for_mutation, one gate for every mutation path"
```

---

## Task 3: Route write/edit/delete through `pathpolicy`

**Model:** gpt-5.6-sol-medium (multi-file integration)

**Spec:** §6.3 (steps 1 of each apply_*), §7.2, §8.1, §9

**Files:**
- Modify: `actions.py` — replace `_resolve_in_root` (`actions.py:142-152`) usage in `apply_write` (`actions.py:182-205`), `apply_delete` (`actions.py:208-228`), `apply_edit` (`actions.py:231-300`).
- Modify: `README.md` — "Actions" section (the paragraph starting `` `run`, `fetch`, and `search` are the only things `` at ~line 172) and "Security" section (~line 378).
- Test: `tests/test_actions.py`

**Interfaces:**
- Consumes: `pathpolicy.resolve_for_mutation(project_root, raw_path) -> PathDecision` from Task 2.
- Produces, for Tasks 7/8/10: `apply_write`, `apply_delete`, `apply_edit` keep their exact existing signatures and return types, but every refusal message is now `f"refusing to {action} {raw_path}: {decision.reason}"` (humans) and `format_action_error(action="edit", reason=decision.reason, path=edit.path, suggestion=decision.suggestion)` (model, edit only). Every downstream use of the target path uses `decision.relpath` (POSIX-relative), not the raw model string.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_actions.py`, inside a new class placed after `TestApplyEdit`:

```python
class TestMutationPathPolicy(unittest.TestCase):
    def test_write_refuses_git_internals(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".git").mkdir()
            write = actions.FileWrite(path=".git/config", content="[core]\n")
            self.assertFalse(actions.apply_write(root, write, confirm=False))
            self.assertFalse(Path(root, ".git/config").exists())

    def test_write_refuses_a_credential_file(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path=".env", content="TOKEN=abc\n")
            self.assertFalse(actions.apply_write(root, write, confirm=False))
            self.assertFalse(Path(root, ".env").exists())

    def test_write_allows_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path=".gitignore", content="__pycache__/\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(Path(root, ".gitignore").read_text(), "__pycache__/\n")

    def test_delete_refuses_git_internals(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".git").mkdir()
            Path(root, ".git/config").write_text("[core]\n")
            self.assertFalse(actions.apply_delete(root, ".git/config", confirm=False))
            self.assertTrue(Path(root, ".git/config").exists())

    def test_edit_refuses_credential_file_with_structured_error(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".env").write_text("TOKEN=abc\n")
            result = actions.apply_edit(
                root, actions.FileEdit(path=".env", search="abc", replace="xyz"), confirm=False
            )
            self.assertFalse(result.ok)
            self.assertIn("credential or key file", result.error)
            self.assertIn("outside localcoder", result.error)
            self.assertEqual(Path(root, ".env").read_text(), "TOKEN=abc\n")

    def test_write_refuses_empty_path(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(actions.apply_write(root, actions.FileWrite(path="  ", content="x"),
                                                 confirm=False))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_actions.TestMutationPathPolicy -v`
Expected: FAIL — `test_write_refuses_git_internals`, `test_write_refuses_a_credential_file`, `test_delete_refuses_git_internals` and `test_edit_refuses_credential_file_with_structured_error` all fail because today's `_resolve_in_root` allows those paths (the file gets written/deleted, or the edit returns a "matched N times" error instead of a credential refusal).

- [ ] **Step 3: Write the implementation**

In `actions.py`, add `import pathpolicy` next to the existing `import gitsafety` / `import ui`, and **delete** `_resolve_in_root` entirely (it has no other callers — verified by `rg -n "_resolve_in_root"`).

Replace the head of each apply function:

```python
def apply_write(project_root: str, write: FileWrite, confirm: bool = True) -> bool:
    decision = pathpolicy.resolve_for_mutation(project_root, write.path)
    if not decision.ok:
        ui.error(f"refusing to write {write.path}: {decision.reason}")
        return False
    target = decision.path
    rel = decision.relpath
    existed = target.exists()
    action = "overwrite" if existed else "create"
    # ...rest of today's body, with every `write.path` display/commit use
    #    replaced by `rel`...
```

```python
def apply_delete(project_root: str, path: str, confirm: bool = True) -> bool:
    decision = pathpolicy.resolve_for_mutation(project_root, path)
    if not decision.ok:
        ui.error(f"refusing to delete {path}: {decision.reason}")
        return False
    target = decision.path
    rel = decision.relpath
    if not target.exists():
        ui.sub(f"{rel} doesn't exist, nothing to delete")
        return False
    # NOTE: the old `target.is_dir()` branch is gone -- pathpolicy check 5
    # already refuses a directory with reason "path is a directory".
    if confirm and not ui.confirm(f"  delete {rel}?"):
        ui.sub(f"skipped {rel}")
        return False
    target.unlink()
    ui.sub(f"deleted {rel}")
    gitsafety.commit_change(project_root, f"delete {rel}")
    return True
```

```python
def apply_edit(project_root: str, edit: FileEdit, confirm: bool = True) -> EditResult:
    decision = pathpolicy.resolve_for_mutation(project_root, edit.path)
    if not decision.ok:
        ui.error(f"refusing to edit {edit.path}: {decision.reason}")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason=decision.reason,
                path=edit.path,
                suggestion=decision.suggestion,
            ),
        )
    target = decision.path
    rel = decision.relpath
    if not target.exists() or not target.is_file():
        ui.error(f"{rel} does not exist")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason="target file does not exist",
                path=edit.path,
                suggestion="inspect the project tree and retry, or use write to create a new file",
            ),
        )
    # ...rest of today's body, with every `edit.path` display/commit use
    #    replaced by `rel` (keep `path=edit.path` inside format_action_error
    #    calls -- the model should see the path it actually wrote)...
```

`gitsafety.commit_change` still takes two arguments at this point; Task 4 adds the third.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m unittest tests.test_actions -v
```

Expected: PASS, including every pre-existing assertion (`test_write_refuses_path_traversal`, `test_delete_refuses_directory`, `test_delete_refuses_missing_file`, `test_edit_refuses_path_traversal`) unmodified.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile actions.py pathpolicy.py
```

- [ ] **Step 6: Update the docs this task's behaviour changes**

In `README.md`, in the **Actions** section, after the paragraph beginning `` `run`, `fetch`, and `search` are the only things ``, add:

```markdown
`write`, `edit` and `delete` all resolve their target through one gate
(`pathpolicy.py`) before anything else happens: nothing outside the project
root, nothing inside `.git/` (component match, so `.gitignore`,
`.gitattributes`, `.gitmodules` and `.github/` stay writable), nothing that
`context/denylist.py` recognises as a credential or key file, and no
directories. A refused `write`/`delete` is display-only; a refused `edit`
also gets a structured `ERROR:` block so the model can retry with a
different path. The escape hatch for a genuinely needed `.env` is to create
it by hand, outside localcoder.
```

In `README.md`'s **Security** section, add a bullet:

```markdown
- Model-emitted mutations can never touch git internals or credential
  files: `pathpolicy.resolve_for_mutation()` is the single gate all three
  mutating actions call, so there is no second code path to keep in sync.
```

- [ ] **Step 7: Commit**

```bash
git add actions.py tests/test_actions.py README.md
git commit -m "feat: route write/edit/delete through pathpolicy, refuse .git and credential paths"
```

---

## Task 4: Path-scoped `gitsafety.commit_change`

**Model:** gpt-5.6-sol-medium (Git/process execution work)

**Spec:** §6.4 (`commit_change`), §8.6, §9

**Files:**
- Modify: `gitsafety.py:30-40`
- Modify: `actions.py` — the three `gitsafety.commit_change(...)` call sites (currently `actions.py:204`, `actions.py:227`, `actions.py:299`).
- Test: `tests/test_gitsafety.py`

**Interfaces:**
- Consumes: `pathpolicy`-produced `rel` strings from Task 3.
- Produces, for Task 5 and beyond: `gitsafety.commit_change(project_root: str, message: str, paths: Sequence[str]) -> None` — the third parameter is **required**. Call sites become `gitsafety.commit_change(project_root, f"write {rel}", [rel])`, `... f"delete {rel}", [rel])`, `... f"edit {rel}", [rel])`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_gitsafety.py`, replace `test_commit_change_never_raises_outside_repo` and add two new tests (leave `_init_repo`, `TestIsGitRepo` alone):

```python
    def test_commit_change_never_raises_outside_repo(self):
        with tempfile.TemporaryDirectory() as root:
            gitsafety.commit_change(root, "should be a silent no-op", ["a.py"])  # must not raise

    def test_commit_change_stages_only_the_named_path(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "seed.txt").write_text("seed\n")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            # The user's own separate work, deliberately staged.
            Path(root, "b.py").write_text("users_work = True\n")
            subprocess.run(["git", "add", "b.py"], cwd=root, check=True)

            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])

            committed = subprocess.run(
                ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.split()
            self.assertEqual(committed, ["a.py"])

            still_staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout.split()
            self.assertEqual(still_staged, ["b.py"])

    def test_commit_change_creates_no_commit_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            before = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                                    cwd=root, capture_output=True, text=True, check=True).stdout
            gitsafety.commit_change(root, "write a.py", ["a.py"])  # content identical
            after = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                                   cwd=root, capture_output=True, text=True, check=True).stdout
            self.assertEqual(before, after)
```

Also update the existing `test_commit_then_undo_roundtrip` and `test_undo_refuses_the_repos_very_first_commit` call sites to pass `["a.py"]` as the third argument (their `undo_last` assertions are rewritten in Task 5; for now they just need to keep compiling and passing against the current `undo_last`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_gitsafety -v`
Expected: FAIL with `TypeError: commit_change() takes 2 positional arguments but 3 were given`.

- [ ] **Step 3: Write the implementation**

Replace `gitsafety.commit_change`:

```python
def commit_change(project_root: str, message: str, paths: Sequence[str]) -> None:
    """Stages and commits ONLY `paths`. Never `git add -A`: a user's
    unrelated modified or staged work must never be swept into a
    `localcoder:` commit (measured: `git commit -m msg -- <path>` leaves a
    separately staged file staged and uncommitted). Every git error is
    still swallowed -- the on-disk action already succeeded, and a failed
    safety-net commit must never look like a failed action."""
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
            return  # nothing staged for those paths -- no empty-commit noise
        subprocess.run(
            ["git", "commit", "-m", f"{COMMIT_PREFIX}{message}", "--", *pathspec],
            cwd=project_root, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
```

Add `from collections.abc import Sequence` to `gitsafety.py`'s imports.

Update the three call sites in `actions.py`:

```python
    gitsafety.commit_change(project_root, f"write {rel}", [rel])
    gitsafety.commit_change(project_root, f"delete {rel}", [rel])
    gitsafety.commit_change(project_root, f"edit {rel}", [rel])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_gitsafety -v`
Expected: PASS.

- [ ] **Step 5: Run the full fast suite, py_compile, and the forbidden-pattern check**

```bash
python3 -m unittest discover tests
python3 -m py_compile gitsafety.py actions.py
rg -n "git add -A" --glob '!docs/**' --glob '!tests/**' .
```

Expected: suite green; the `rg` finds **nothing**.

- [ ] **Step 6: Commit**

```bash
git add gitsafety.py actions.py tests/test_gitsafety.py
git commit -m "fix: path-scoped git staging so localcoder never commits a user's unrelated work"
```

---

## Task 5: Revert-based, non-destructive `/undo`

**Model:** gpt-5.6-sol-medium (Git/process execution work)

**Spec:** §6.4 (`undo_last`), §7.1, §8.7, §9

**Files:**
- Modify: `gitsafety.py:43-84` (replaces the whole `undo_last` body)
- Modify: `README.md` — "Git safety net" section (~lines 212-222)
- Modify: `docs/LESSONS_LEARNED.md` — new section at the end
- Test: `tests/test_gitsafety.py`

**Interfaces:**
- Consumes: `gitsafety.commit_change(project_root, message, paths)` from Task 4.
- Produces: `gitsafety.undo_last(project_root: str) -> tuple[bool, str]` — same contract, new behaviour. Module constants `UNDO_SCAN_LIMIT = 50` and `_REVERTS_RE`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_gitsafety.py`, **delete** `test_undo_refuses_the_repos_very_first_commit` (root commits are supported now) and rewrite `test_commit_then_undo_roundtrip`; add the rest:

```python
def _count_commits(root: str) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"],
                              cwd=root, capture_output=True, text=True, check=True).stdout)


class TestUndoLast(unittest.TestCase):
    def test_undo_adds_a_revert_commit_and_restores_content(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "README.md").write_text("preexisting\n")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            before = _count_commits(root)

            ok, message = gitsafety.undo_last(root)
            self.assertTrue(ok, message)
            self.assertEqual(_count_commits(root), before + 1)   # history grew, never shrank
            self.assertFalse(Path(root, "a.py").exists())
            self.assertTrue(Path(root, "README.md").exists())

    def test_undo_works_on_the_repos_root_commit(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])  # the ONLY commit

            ok, message = gitsafety.undo_last(root)
            self.assertTrue(ok, message)
            self.assertFalse(Path(root, "a.py").exists())

    def test_undo_refuses_when_an_affected_path_is_dirty(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            Path(root, "a.py").write_text("x = 999  # my own edit\n")

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertIn("would be overwritten", message)
            self.assertIn("a.py", message)
            self.assertEqual(Path(root, "a.py").read_text(), "x = 999  # my own edit\n")

    def test_a_conflicting_revert_is_rolled_back_completely(self):
        """A real content conflict, not the dirty-worktree refusal: the
        localcoder commit changes a line, a later human commit changes the
        same line, so reverting the localcoder one conflicts (git exits 1
        with REVERT_HEAD present) and must be aborted."""
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("line1\nline2\nline3\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

            Path(root, "a.py").write_text("line1\nLOCALCODER\nline3\n")
            gitsafety.commit_change(root, "edit a.py", ["a.py"])

            Path(root, "a.py").write_text("line1\nHUMAN AGAIN\nline3\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "my own change"], cwd=root, check=True)

            before = _count_commits(root)
            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertIn("conflicted and was rolled back", message)
            self.assertEqual(_count_commits(root), before)   # nothing added

            marker = subprocess.run(["git", "rev-parse", "--git-path", "REVERT_HEAD"],
                                    cwd=root, capture_output=True, text=True, check=True)
            self.assertFalse(Path(root, marker.stdout.strip()).exists())
            status = subprocess.run(["git", "status", "--porcelain"],
                                    cwd=root, capture_output=True, text=True, check=True)
            self.assertNotIn("UU", status.stdout)
            self.assertEqual(Path(root, "a.py").read_text(), "line1\nHUMAN AGAIN\nline3\n")

    def test_undo_refuses_a_human_commit(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "a human's own commit"],
                           cwd=root, check=True)

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertTrue(Path(root, "a.py").exists())

    def test_two_undos_revert_two_different_localcoder_commits(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("a\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            Path(root, "b.py").write_text("b\n")
            gitsafety.commit_change(root, "write b.py", ["b.py"])

            ok1, msg1 = gitsafety.undo_last(root)
            ok2, msg2 = gitsafety.undo_last(root)
            self.assertTrue(ok1, msg1)
            self.assertTrue(ok2, msg2)
            self.assertFalse(Path(root, "a.py").exists())
            self.assertFalse(Path(root, "b.py").exists())
            self.assertIn("write b.py", msg1)
            self.assertIn("write a.py", msg2)

    def test_undo_reports_nothing_left_when_all_localcoder_commits_are_reverted(self):
        with tempfile.TemporaryDirectory() as root:
            _init_repo(root)
            Path(root, "a.py").write_text("a\n")
            gitsafety.commit_change(root, "write a.py", ["a.py"])
            self.assertTrue(gitsafety.undo_last(root)[0])

            ok, message = gitsafety.undo_last(root)
            self.assertFalse(ok)
            self.assertIn("left to undo", message)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_gitsafety.TestUndoLast -v`
Expected: FAIL — `test_undo_adds_a_revert_commit_and_restores_content` fails because today's `reset --hard` *shrinks* history (`_count_commits` goes down, not up); `test_undo_works_on_the_repos_root_commit` fails on the "can't auto-undo the repo's very first commit" refusal; `test_two_undos_revert_two_different_localcoder_commits` fails because today's `undo_last` only ever looks at `HEAD`.

- [ ] **Step 3: Write the implementation**

In `gitsafety.py`, add `import re` and `from pathlib import Path`, add the constants, and replace `undo_last` entirely:

```python
UNDO_SCAN_LIMIT = 50
# git's own revert body, measured: "This reverts commit <40-hex-sha>."
# SHA matching, not subject matching, so two commits with identical
# subjects can never be confused for one another.
_REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{40})\.")


def _git(project_root: str, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=project_root, capture_output=True, text=True, timeout=timeout,
    )


def undo_last(project_root: str) -> tuple[bool, str]:
    """Reverts the newest un-reverted `localcoder: ` commit with `git revert`
    -- never `git reset --hard`. History only ever grows, and the one branch
    that could destroy uncommitted work (a dirty path the target commit
    touched) is refused before git is asked to do anything. Repeated calls
    walk backwards through localcoder's own commits rather than flip-flopping
    one, because a commit already named in a later commit's
    "This reverts commit <sha>." body is skipped."""
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

        # --root is required or a root commit prints nothing at all.
        changed = _git(project_root,
                       ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha])
        paths = [p for p in changed.stdout.splitlines() if p]
        if paths:
            status = _git(project_root, ["status", "--porcelain", "--", *paths])
            if status.stdout.strip():
                return False, (
                    f"uncommitted changes in {', '.join(paths)} would be overwritten "
                    "-- commit or stash them first"
                )

        result = _git(project_root, ["revert", "--no-edit", "--no-rerere-autoupdate", sha], 30)
        if result.returncode == 0:
            return True, f"reverted: {subject} (new commit {sha[:7]})"

        # --abort is called ONLY when the marker exists: on git's exit-128
        # "refused before starting" path, `git revert --abort` itself fails
        # with 128 and would turn a clean refusal into a confusing one.
        marker = _git(project_root, ["rev-parse", "--git-path", "REVERT_HEAD"], 5)
        if marker.returncode == 0 and Path(project_root, marker.stdout.strip()).exists():
            _git(project_root, ["revert", "--abort"], 30)
            return False, f"revert conflicted and was rolled back -- resolve {subject} by hand"
        first_line = (result.stderr.strip().splitlines() or [""])[0]
        return False, f"git refused the revert: {first_line}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"git command failed: {e}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_gitsafety -v`
Expected: PASS, all cases.

- [ ] **Step 5: Run the full fast suite, py_compile, and the forbidden-pattern check**

```bash
python3 -m unittest discover tests
python3 -m py_compile gitsafety.py
rg -n "reset --hard" --glob '!docs/**' .
```

Expected: suite green; `rg` finds **nothing**.

- [ ] **Step 6: Update the docs this task's behaviour changes**

Replace `README.md`'s **Git safety net** section body with:

```markdown
If the current directory is a git repo, every confirmed `write`/`edit`/`delete`
is auto-committed with a `localcoder: ` prefixed message (`gitsafety.py`),
staging **only** the file that action touched -- your own unrelated staged or
modified work is never swept into a `localcoder:` commit.

`/undo` reverts the newest `localcoder: ` commit with `git revert`, never
`git reset --hard`. That means:

- it can never discard uncommitted work -- if a file the target commit
  touched is dirty, `/undo` refuses and tells you to commit or stash first;
- it never rewrites history -- the revert is a *new* commit, so the reverted
  commit stays in the log (accepted trade-off);
- it works on the repository's very first commit, which the old
  `reset --hard` implementation had to refuse;
- pressing `/undo` repeatedly walks backwards through localcoder's own
  commits instead of flip-flopping one, because a commit already named in a
  later commit's `This reverts commit <sha>.` body is skipped;
- a conflicting revert is rolled back with `git revert --abort` and leaves no
  revert in progress;
- a commit you made yourself is never a candidate.

Outside a git repo, the y/N prompt at write/delete time is the only safety
net there is -- `git init` first if you want `/undo` available.
```

Append to `docs/LESSONS_LEARNED.md`:

```markdown
## `git revert`'s failure modes are three different things, and only one of them is abortable

Measured directly (git 2.55.0, 2026-09-04) while designing the non-destructive
`/undo`:

| Situation | Exit | State left behind |
|---|---|---|
| Clean revert, including of a **root** commit | 0 | new revert commit |
| Unrelated dirty/untracked file present | 0 | new revert commit |
| Dirty or staged change on a path the commit touched | **128** | nothing modified, **no revert in progress** -- `git revert --abort` also fails with 128 |
| Content conflict | **1** | `UU` in status, `.git/REVERT_HEAD` present, `git revert --abort` exits 0 and fully restores |

**Takeaway**: never call `git revert --abort` unconditionally after a
non-zero exit -- check `git rev-parse --git-path REVERT_HEAD` on disk first,
because the exit-128 path has nothing to abort and turns a clean refusal
into a confusing double failure. Also: `git diff-tree` needs `--root` or a
root commit reports zero changed paths, and reverting a root commit works
fine, so the old "can't auto-undo the repo's very first commit" refusal was
an artifact of `reset --hard`, not a git limitation.
```

- [ ] **Step 7: Commit**

```bash
git add gitsafety.py tests/test_gitsafety.py README.md docs/LESSONS_LEARNED.md
git commit -m "feat: non-destructive /undo via git revert, never reset --hard"
```

---

## Task 6: `textfile.py` — UTF-8/BOM/EOL-preserving primitives

**Model:** composer-2.5-fast (complete-spec, 2-file mechanical task)

**Spec:** §6.2

**Files:**
- Create: `textfile.py`
- Create test: `tests/test_textfile.py`

**Interfaces:**
- Consumes: nothing (stdlib leaf module).
- Produces, for Tasks 7 and 8:
  - `textfile.BOM = "\ufeff"`
  - `textfile.TextFile` — frozen dataclass with `text: str` (BOM stripped, EOLs **not** normalized), `eol: str`, `bom: bool`, `mixed_eol: bool`.
  - `textfile.detect_eol(text: str) -> tuple[str, bool]`
  - `textfile.read(path: Path) -> TextFile` — raises `UnicodeDecodeError` / `OSError`.
  - `textfile.write(path: Path, text: str, *, bom: bool) -> None`
  - `textfile.to_eol(text: str, eol: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_textfile.py`:

```python
import tempfile
import unittest
from pathlib import Path

import textfile


class TestDetectEol(unittest.TestCase):
    def test_pure_lf(self):
        self.assertEqual(textfile.detect_eol("a\nb\nc\n"), ("\n", False))

    def test_pure_crlf(self):
        self.assertEqual(textfile.detect_eol("a\r\nb\r\nc\r\n"), ("\r\n", False))

    def test_mixed_crlf_dominant(self):
        eol, mixed = textfile.detect_eol("a\r\nb\r\nc\n")
        self.assertEqual(eol, "\r\n")
        self.assertTrue(mixed)

    def test_mixed_lf_dominant(self):
        eol, mixed = textfile.detect_eol("a\nb\nc\r\n")
        self.assertEqual(eol, "\n")
        self.assertTrue(mixed)

    def test_tie_is_not_a_strict_crlf_majority(self):
        eol, mixed = textfile.detect_eol("a\r\nb\n")
        self.assertEqual(eol, "\n")
        self.assertTrue(mixed)

    def test_empty_and_single_line(self):
        self.assertEqual(textfile.detect_eol(""), ("\n", False))
        self.assertEqual(textfile.detect_eol("no newline here"), ("\n", False))


class TestReadWriteRoundTrip(unittest.TestCase):
    def test_crlf_round_trip_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "crlf.txt")
            raw = b"one\r\ntwo\r\nthree\r\n"
            path.write_bytes(raw)
            parsed = textfile.read(path)
            self.assertEqual(parsed.eol, "\r\n")
            self.assertFalse(parsed.bom)
            self.assertFalse(parsed.mixed_eol)
            textfile.write(path, parsed.text, bom=parsed.bom)
            self.assertEqual(path.read_bytes(), raw)

    def test_bom_plus_crlf_round_trip_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "bom.txt")
            raw = b"\xef\xbb\xbfone\r\ntwo\r\n"
            path.write_bytes(raw)
            parsed = textfile.read(path)
            self.assertTrue(parsed.bom)
            self.assertEqual(parsed.text, "one\r\ntwo\r\n")   # BOM stripped from text
            textfile.write(path, parsed.text, bom=parsed.bom)
            self.assertEqual(path.read_bytes(), raw)

    def test_read_raises_on_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "latin1.txt")
            path.write_bytes(b"caf\xe9\n")
            with self.assertRaises(UnicodeDecodeError):
                textfile.read(path)


class TestToEol(unittest.TestCase):
    def test_renders_lf_source_as_crlf(self):
        self.assertEqual(textfile.to_eol("a\nb\n", "\r\n"), "a\r\nb\r\n")

    def test_renders_crlf_source_as_lf(self):
        self.assertEqual(textfile.to_eol("a\r\nb\r\n", "\n"), "a\nb\n")

    def test_is_idempotent(self):
        self.assertEqual(textfile.to_eol(textfile.to_eol("a\nb\n", "\r\n"), "\r\n"), "a\r\nb\r\n")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_textfile -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'textfile'`.

- [ ] **Step 3: Write the implementation**

Create `textfile.py`:

```python
"""Byte-faithful text file primitives for the mutation path.

Path.write_text() destroys CRLF and read_text(errors="replace") silently
corrupts any byte it cannot decode -- both were doing exactly that in
actions.py. Everything here is strict: decode with plain utf-8 (so a
non-UTF-8 file raises instead of being mangled), keep the file's own EOLs in
`text` rather than normalizing them, and write with newline="" so Python
performs no translation of its own.

Stdlib only, no project imports: a leaf module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BOM = "\ufeff"
_BOM_BYTES = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class TextFile:
    text: str        # exact decoded content, BOM stripped, EOLs NOT normalized
    eol: str         # dominant EOL: "\r\n" or "\n"
    bom: bool
    mixed_eol: bool


def detect_eol(text: str) -> tuple[str, bool]:
    """(dominant EOL, whether both kinds appear). CRLF wins only on a strict
    majority, so a tie -- or empty/one-line content -- yields ("\\n", ...)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return "\n", False
    return ("\r\n" if crlf > lf else "\n"), (crlf > 0 and lf > 0)


def read(path: Path) -> TextFile:
    """Raises UnicodeDecodeError on a non-UTF-8 file and OSError on an
    unreadable one -- both are refusals the caller must surface, never
    something to paper over with errors="replace"."""
    raw = path.read_bytes()
    bom = raw.startswith(_BOM_BYTES)
    if bom:
        raw = raw[len(_BOM_BYTES):]
    text = raw.decode("utf-8")
    eol, mixed = detect_eol(text)
    return TextFile(text=text, eol=eol, bom=bom, mixed_eol=mixed)


def write(path: Path, text: str, *, bom: bool) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        if bom:
            handle.write(BOM)
        handle.write(text)


def to_eol(text: str, eol: str) -> str:
    """Normalizes to "\\n" first so mixed input renders consistently."""
    normalized = text.replace("\r\n", "\n")
    return normalized if eol == "\n" else normalized.replace("\n", eol)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_textfile -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile textfile.py
```

- [ ] **Step 6: Commit**

```bash
git add textfile.py tests/test_textfile.py
git commit -m "feat: textfile, strict-UTF-8 read/write preserving BOM and EOLs"
```

---

## Task 7: Byte-faithful `apply_write` with diff-on-overwrite

**Model:** gpt-5.6-sol-medium (multi-file integration)

**Spec:** §6.3 (`apply_write` steps 2-7), §9

**Files:**
- Modify: `actions.py` — `apply_write` (post-Task-3 body), plus new module constants.
- Modify: `README.md` — "Actions" table row for ` ```write:path `.
- Test: `tests/test_actions.py`

**Interfaces:**
- Consumes: `textfile.read/write/to_eol` (Task 6), `pathpolicy.resolve_for_mutation` (Task 2), `gitsafety.commit_change(root, msg, [rel])` (Task 4).
- Produces, for Task 8 and Task 10: module constants `MAX_DIFF_LINES = 200`, `DIFF_HEAD_LINES = 150`, `DIFF_TAIL_LINES = 50`, and helper `_cap_diff(diff: str) -> str`. `apply_write` returns `False` (no prompt, no commit) when the rendered content already matches the file byte for byte.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_actions.py`:

```python
class TestApplyWriteByteFidelity(unittest.TestCase):
    def test_overwriting_a_crlf_file_keeps_crlf(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "win.txt")
            target.write_bytes(b"one\r\ntwo\r\n")
            write = actions.FileWrite(path="win.txt", content="one\nthree\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(target.read_bytes(), b"one\r\nthree\r\n")

    def test_overwriting_a_bom_file_keeps_the_bom(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "bom.txt")
            target.write_bytes(b"\xef\xbb\xbfold\n")
            write = actions.FileWrite(path="bom.txt", content="new\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfnew\n")

    def test_new_file_gets_lf_and_no_bom(self):
        with tempfile.TemporaryDirectory() as root:
            write = actions.FileWrite(path="fresh.txt", content="a\r\nb\r\n")
            self.assertTrue(actions.apply_write(root, write, confirm=False))
            self.assertEqual(Path(root, "fresh.txt").read_bytes(), b"a\nb\n")

    def test_refuses_to_overwrite_a_non_utf8_file(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "latin1.txt")
            target.write_bytes(b"caf\xe9\n")
            write = actions.FileWrite(path="latin1.txt", content="cafe\n")
            self.assertFalse(actions.apply_write(root, write, confirm=False))
            self.assertEqual(target.read_bytes(), b"caf\xe9\n")

    def test_identical_content_is_skipped_without_prompting(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "same.txt")
            target.write_text("x = 1\n")
            write = actions.FileWrite(path="same.txt", content="x = 1\n")
            with mock.patch("builtins.input", side_effect=AssertionError("should never prompt")):
                self.assertFalse(actions.apply_write(root, write, confirm=True))

    def test_overwrite_shows_a_unified_diff_before_the_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("old\n")
            write = actions.FileWrite(path="a.py", content="new\n")
            buffer = io.StringIO()
            with mock.patch("ui._enabled", return_value=False), \
                 contextlib.redirect_stdout(buffer), \
                 mock.patch("builtins.input", return_value="n"):
                actions.apply_write(root, write, confirm=True)
            printed = buffer.getvalue()
            self.assertIn("--- a.py", printed)
            self.assertIn("-old", printed)
            self.assertIn("+new", printed)

    def test_a_huge_diff_is_capped(self):
        old = "".join(f"line {i}\n" for i in range(1000))
        new = "".join(f"changed {i}\n" for i in range(1000))
        capped = actions._cap_diff(actions.unified_diff("big.txt", old, new))
        self.assertIn("diff lines elided", capped)
        self.assertLessEqual(len(capped.splitlines()), actions.MAX_DIFF_LINES + 1)
```

Add `import contextlib`, `import io` and `from unittest import mock` to the top of `tests/test_actions.py` if not already present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_actions.TestApplyWriteByteFidelity -v`
Expected: FAIL — `test_overwriting_a_crlf_file_keeps_crlf` gets `b"one\nthree\n"` (CRLF destroyed by `Path.write_text`); `test_a_huge_diff_is_capped` fails with `AttributeError: module 'actions' has no attribute '_cap_diff'`; `test_identical_content_is_skipped_without_prompting` fails with the `AssertionError` because today's code always prompts.

- [ ] **Step 3: Write the implementation**

Add near the top of `actions.py` (after the block regexes), plus `import textfile`:

```python
MAX_DIFF_LINES = 200
DIFF_HEAD_LINES = 150
DIFF_TAIL_LINES = 50


def _cap_diff(diff: str) -> str:
    """A whole-file rewrite can produce thousands of diff lines; the human
    reading the y/N prompt needs the shape of the change, not all of it."""
    lines = diff.splitlines()
    if len(lines) <= MAX_DIFF_LINES:
        return diff
    elided = len(lines) - DIFF_HEAD_LINES - DIFF_TAIL_LINES
    return "\n".join(
        lines[:DIFF_HEAD_LINES]
        + [f"...({elided} diff lines elided)..."]
        + lines[-DIFF_TAIL_LINES:]
    )
```

Replace the body of `apply_write` after the path-policy gate:

```python
    existed = target.exists()
    if existed:
        try:
            current = textfile.read(target)
        except UnicodeDecodeError:
            # The y/N prompt is the real gate, and a prompt we cannot show a
            # diff for is not a gate. Escape hatch: an explicit (confirmed)
            # `delete`, then a `write`.
            ui.error(f"refusing to overwrite {rel}: file is not valid UTF-8")
            return False
        except OSError as e:
            ui.error(f"refusing to overwrite {rel}: could not read the file: {e}")
            return False
        if current.mixed_eol:
            label = "CRLF" if current.eol == "\r\n" else "LF"
            ui.warn(f"{rel} has mixed line endings; writing all lines as {label}")
        old_text = current.text
        rendered = textfile.to_eol(write.content, current.eol)
        bom = current.bom
    else:
        old_text = ""
        rendered = textfile.to_eol(write.content, "\n")
        bom = False

    if existed and rendered == old_text:
        ui.sub(f"{rel} already has this content -- nothing to write")
        return False

    suspects = find_suspected_secrets(write.content)
    if suspects:
        ui.warn(f"{rel} contains something shaped like a secret: {suspects[0][:12]}...")
        ui.warn("this is a pattern-match warning, not a certainty -- check before confirming.")

    # Compare on LF-normalized text so a CRLF file does not show every line
    # as changed just because the EOLs round-tripped.
    diff = unified_diff(rel, old_text.replace("\r\n", "\n"), rendered.replace("\r\n", "\n"))
    if diff:
        ui.sub(_cap_diff(diff).rstrip("\n"))

    action = "overwrite" if existed else "create"
    if confirm and not ui.confirm(f"  {action} {rel} ({len(write.content)} bytes)?"):
        ui.sub(f"skipped {rel}")
        return False

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        textfile.write(target, rendered, bom=bom)
    except OSError as e:
        ui.error(f"could not write {rel}: {e}")
        return False

    ui.sub(f"{'wrote' if not existed else 'updated'} {rel}")
    gitsafety.commit_change(project_root, f"write {rel}", [rel])
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_actions -v`
Expected: PASS, including the pre-existing `TestApplyWrite` cases.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile actions.py textfile.py
```

- [ ] **Step 6: Update the docs this task's behaviour changes**

In `README.md`'s **Actions** table, replace the ` ```write:path ` row with:

```markdown
| ` ```write:path ` | create/replace a file; preserves the existing file's CRLF/LF and UTF-8 BOM, refuses a non-UTF-8 target, shows a unified diff (capped at 200 lines) before overwriting, and skips silently when the content is already identical | yes, y/N | no |
```

- [ ] **Step 7: Commit**

```bash
git add actions.py tests/test_actions.py README.md
git commit -m "feat: byte-faithful write with diff-on-overwrite and no-op skip"
```

---

## Task 8: EOL-tolerant, byte-faithful `apply_edit` + `OSError` wrapping

**Model:** gpt-5.6-sol-medium (multi-file integration)

**Spec:** §6.3 (`apply_edit` steps 3-7, `apply_delete` unlink wrapping), §9

**Files:**
- Modify: `actions.py` — `apply_edit`, plus the `target.unlink()` in `apply_delete`.
- Test: `tests/test_actions.py`

**Interfaces:**
- Consumes: `textfile.read/write` (Task 6), `_cap_diff` (Task 7).
- Produces: `apply_edit` keeps `-> EditResult`. Matching is EOL-tolerant: candidates are `[edit.search]` plus `edit.search.replace("\n", "\r\n")` when that differs; the **sum** of their occurrence counts must be exactly 1; the matching candidate selects the EOL used to render `edit.replace`. Splicing is `raw.replace(needle, rendered_replace, 1)`, so every byte outside the replaced region is untouched.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_actions.py`:

```python
class TestApplyEditByteFidelity(unittest.TestCase):
    def test_editing_one_line_of_a_crlf_file_keeps_every_other_crlf(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "win.py")
            target.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
            result = actions.apply_edit(
                root, actions.FileEdit(path="win.py", search="b = 2\n", replace="b = 22\n"),
                confirm=False,
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(target.read_bytes(), b"a = 1\r\nb = 22\r\nc = 3\r\n")

    def test_editing_a_bom_file_preserves_the_bom(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "bom.py")
            target.write_bytes(b"\xef\xbb\xbfx = 1\n")
            result = actions.apply_edit(
                root, actions.FileEdit(path="bom.py", search="x = 1\n", replace="x = 2\n"),
                confirm=False,
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfx = 2\n")

    def test_editing_a_non_utf8_file_is_refused_and_leaves_it_byte_identical(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "latin1.txt")
            raw = b"caf\xe9\n"
            target.write_bytes(raw)
            result = actions.apply_edit(
                root, actions.FileEdit(path="latin1.txt", search="cafe", replace="coffee"),
                confirm=False,
            )
            self.assertFalse(result.ok)
            self.assertIn("not valid UTF-8", result.error)
            self.assertIn("ERROR:", result.error)
            self.assertEqual(target.read_bytes(), raw)

    def test_lf_search_matching_a_crlf_file_is_still_unique(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "win.py").write_bytes(b"only = 1\r\n")
            result = actions.apply_edit(
                root, actions.FileEdit(path="win.py", search="only = 1\n", replace="only = 2\n"),
                confirm=False,
            )
            self.assertTrue(result.ok, result.error)

    def test_ambiguous_match_still_reports_the_total_count(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "win.py").write_bytes(b"x = 1\r\nx = 1\r\n")
            result = actions.apply_edit(
                root, actions.FileEdit(path="win.py", search="x = 1\n", replace="x = 2\n"),
                confirm=False,
            )
            self.assertFalse(result.ok)
            self.assertIn("matched 2 times", result.error)


class TestMutationOsErrorHandling(unittest.TestCase):
    def test_write_reports_oserror_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("textfile.write", side_effect=OSError("disk on fire")):
                ok = actions.apply_write(
                    root, actions.FileWrite(path="a.py", content="x\n"), confirm=False
                )
            self.assertFalse(ok)

    def test_edit_reports_oserror_as_a_structured_error(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("x = 1\n")
            with mock.patch("textfile.write", side_effect=OSError("disk on fire")):
                result = actions.apply_edit(
                    root, actions.FileEdit(path="a.py", search="x = 1\n", replace="x = 2\n"),
                    confirm=False,
                )
            self.assertFalse(result.ok)
            self.assertIn("ERROR:", result.error)
            self.assertIn("disk on fire", result.error)

    def test_delete_reports_oserror_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("x\n")
            with mock.patch("pathlib.Path.unlink", side_effect=OSError("busy")):
                self.assertFalse(actions.apply_delete(root, "a.py", confirm=False))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_actions.TestApplyEditByteFidelity tests.test_actions.TestMutationOsErrorHandling -v`
Expected: FAIL — the CRLF test writes `b"a = 1\nb = 22\nc = 3\n"` (whole file re-normalized by `write_text`); the non-UTF-8 test currently *succeeds* at corrupting the file via `errors="replace"`; the `OSError` tests raise instead of returning.

- [ ] **Step 3: Write the implementation**

Replace `apply_edit`'s body after the "target file does not exist" branch:

```python
    try:
        current = textfile.read(target)
    except UnicodeDecodeError:
        ui.error(f"edit {rel}: file is not valid UTF-8")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason="file is not valid UTF-8 -- refusing to edit it",
                path=edit.path,
                suggestion="this file is not text localcoder can safely edit",
            ),
        )
    except OSError as e:
        ui.error(f"edit {rel}: could not read the file: {e}")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason=f"could not read the file: {e}",
                path=edit.path,
                suggestion="check the file's permissions and retry",
            ),
        )

    # Empty search is checked BEFORE counting: str.count("") returns
    # len(text) + 1, not 0 (docs/LESSONS_LEARNED.md).
    if not edit.search:
        ...unchanged empty-search branch, with `rel` in the ui.error...

    raw = current.text
    candidates = [edit.search]
    crlf_search = edit.search.replace("\n", "\r\n")
    if crlf_search != edit.search:
        candidates.append(crlf_search)
    counts = [raw.count(candidate) for candidate in candidates]
    total = sum(counts)
    if total != 1:
        reason = f"search block matched {total} times (need exactly 1)"
        ui.error(f"edit {rel}: {reason}")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason=reason,
                path=edit.path,
                suggestion="inspect the current file (or ```symbol) and use a unique snippet",
            ),
        )

    needle = candidates[counts.index(1)]
    replacement = edit.replace if needle == edit.search else edit.replace.replace("\n", "\r\n")
    # Splicing the raw text leaves every byte outside the replaced region
    # untouched -- CRLF, mixed EOLs and the BOM all survive by construction.
    updated = raw.replace(needle, replacement, 1)

    suspects = find_suspected_secrets(edit.replace)
    if suspects:
        ui.warn(f"{rel} edit contains something shaped like a secret: {suspects[0][:12]}...")
        ui.warn("this is a pattern-match warning, not a certainty -- check before confirming.")

    diff = unified_diff(rel, raw.replace("\r\n", "\n"), updated.replace("\r\n", "\n"))
    if diff:
        ui.sub(_cap_diff(diff).rstrip("\n"))

    if confirm and not ui.confirm(f"  apply edit to {rel}?"):
        ui.sub(f"skipped {rel}")
        return EditResult(False)

    try:
        textfile.write(target, updated, bom=current.bom)
    except OSError as e:
        ui.error(f"could not write {rel}: {e}")
        return EditResult(
            False,
            format_action_error(
                action="edit",
                reason=f"could not write the file: {e}",
                path=edit.path,
                suggestion="check the file's permissions and retry",
            ),
        )

    ui.sub(f"edited {rel}")
    gitsafety.commit_change(project_root, f"edit {rel}", [rel])
    return EditResult(True)
```

In `apply_delete`, wrap the unlink:

```python
    try:
        target.unlink()
    except OSError as e:
        ui.error(f"could not delete {rel}: {e}")
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_actions -v`
Expected: PASS, including every pre-existing `TestApplyEdit` case (`test_edit_replaces_unique_snippet`, `test_edit_refuses_empty_search`, `test_edit_refuses_zero_matches_without_writing`, `test_edit_refuses_ambiguous_match`, `test_edit_refuses_missing_file`, `test_edit_refuses_path_traversal`).

- [ ] **Step 5: Run the full fast suite, py_compile, and a forbidden-pattern check**

```bash
python3 -m unittest discover tests
python3 -m py_compile actions.py
rg -n 'errors="replace"' actions.py
```

Expected: suite green; the `rg` finds **nothing in `actions.py`** (`main.py:90`'s read-side fallback is out of scope and stays).

- [ ] **Step 6: Commit**

```bash
git add actions.py tests/test_actions.py
git commit -m "feat: EOL-tolerant byte-faithful edit, strict UTF-8, OSError never escapes an action"
```

---

## Task 9: `ui.confirm()` treats EOF as "no"

**Model:** composer-2.5-fast (complete-spec, 2-file mechanical task)

**Spec:** §6.9, §7.6, §8.3

**Files:**
- Modify: `ui.py:74-76`
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ui.confirm(prompt: str) -> bool` — unchanged signature; returns `False` on `EOFError` after printing `  (sem entrada disponível -- assumido 'n')`. `ui.py` stays project-import free.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui.py`:

```python
class TestConfirm(unittest.TestCase):
    def test_y_confirms(self):
        with mock.patch("builtins.input", return_value="Y"):
            self.assertTrue(ui.confirm("do it?"))

    def test_anything_else_declines(self):
        for answer in ("n", "", "yes", "sure", "  N  "):
            with mock.patch("builtins.input", return_value=answer):
                self.assertFalse(ui.confirm("do it?"), answer)

    def test_eof_is_no_and_never_raises(self):
        buffer = io.StringIO()
        with mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch("ui._enabled", return_value=False), \
             contextlib.redirect_stdout(buffer):
            self.assertFalse(ui.confirm("do it?"))
        self.assertIn("assumido 'n'", buffer.getvalue())
```

Add `import contextlib`, `import io`, `from unittest import mock` to `tests/test_ui.py` if missing.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_ui.TestConfirm -v`
Expected: `test_eof_is_no_and_never_raises` FAILS with `EOFError` propagating out of `ui.confirm`. The other two pass already (they document today's behaviour and must keep passing).

- [ ] **Step 3: Write the implementation**

```python
def confirm(prompt: str) -> bool:
    """EOF is "no", never "yes". A piped/non-interactive run that runs out of
    input must decline cleanly instead of ending the process with a
    traceback -- and it must never be able to auto-approve."""
    try:
        answer = input(f"{bold(prompt)} [y/N] ").strip().lower()
    except EOFError:
        print("  (sem entrada disponível -- assumido 'n')", flush=True)
        return False
    return answer == "y"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_ui -v`
Expected: PASS.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile ui.py
```

- [ ] **Step 6: Commit**

```bash
git add ui.py tests/test_ui.py
git commit -m "fix: EOF at a confirmation prompt is 'no', not a crash"
```

---

## Task 10: Malformed `edit` fences get structured feedback (closes Slice A)

**Model:** gpt-5.6-sol-medium (multi-file integration)

**Spec:** §6.3 (`MalformedEdit`, `extract_malformed_edits`), §9, §10 (`tests/test_actions.py` and `tests/test_main.py` additions)

**Files:**
- Modify: `actions.py` — new `MalformedEdit` dataclass + `extract_malformed_edits`, placed directly after `extract_edits` (`actions.py:78-89`).
- Modify: `main.py` — `run_turn`'s edit-handling block (`main.py:255-258`).
- Modify: `README.md` — "Actions" table row for ` ```edit:path `, plus the Security bullet about EOF.
- Modify: `docs/BACKLOG.md` — a "Done" entry for Slice A.
- Test: `tests/test_actions.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `actions.format_action_error` (existing), `context.truncate.truncate_text` (existing).
- Produces, for Task 16 (`_apply_blocks`):
  - `actions.MalformedEdit` — dataclass with `path: str`, `body: str`.
  - `actions.extract_malformed_edits(model_output: str) -> list[MalformedEdit]` — returns exactly the `edit` fences `extract_edits` skipped.
  - In `run_turn`, each malformed fence appends one `truncate_text(format_action_error(action="edit", reason="edit block is not a valid SEARCH/REPLACE pair", path=..., suggestion=...))` to `action_results`, immediately after the `extract_edits` loop.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_actions.py`:

```python
class TestExtractMalformedEdits(unittest.TestCase):
    def test_finds_the_fence_extract_edits_skips(self):
        text = "```edit:calc.py\njust some text without markers\n```"
        self.assertEqual(actions.extract_edits(text), [])
        malformed = actions.extract_malformed_edits(text)
        self.assertEqual(len(malformed), 1)
        self.assertEqual(malformed[0].path, "calc.py")
        self.assertIn("without markers", malformed[0].body)

    def test_a_well_formed_fence_is_not_reported_as_malformed(self):
        text = (
            "```edit:calc.py\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
            "```"
        )
        self.assertEqual(actions.extract_malformed_edits(text), [])
        self.assertEqual(len(actions.extract_edits(text)), 1)

    def test_mixed_output_splits_correctly(self):
        text = (
            "```edit:good.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n```\n"
            "```edit:bad.py\nnope\n```"
        )
        self.assertEqual([e.path for e in actions.extract_edits(text)], ["good.py"])
        self.assertEqual([m.path for m in actions.extract_malformed_edits(text)], ["bad.py"])
```

Append to `tests/test_main.py` (next to the existing `_failed_edit_chunk` helper):

```python
def _malformed_edit_chunk(context: list[int] | None) -> dict:
    chunk = {
        "response": "```edit:calc.py\nI think you should change the divide function\n```",
        "done": True, "prompt_eval_count": 1, "eval_count": 1,
    }
    if context is not None:
        chunk["context"] = context
    return chunk


class TestMalformedEditFeedback(unittest.TestCase):
    def test_malformed_edit_fence_feeds_a_structured_error_back(self):
        agent = _FakeAgent([[_malformed_edit_chunk([7])], [_final_chunk([8])]])
        cce = _FakeCCE(available=False)
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("ui._enabled", return_value=False):
                main.run_turn(
                    agent, "patch the file", "", root, cce,
                    num_ctx=8192, max_total_context_chars=100_000,
                )
        self.assertEqual(len(agent.calls), 2)
        hop1_task = agent.calls[1][0]
        self.assertIn("ERROR:", hop1_task)
        self.assertIn("not a valid SEARCH/REPLACE pair", hop1_task)
        self.assertIn("calc.py", hop1_task)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_actions.TestExtractMalformedEdits tests.test_main.TestMalformedEditFeedback -v`
Expected: FAIL — `AttributeError: module 'actions' has no attribute 'extract_malformed_edits'`, and the `run_turn` test additionally fails on `len(agent.calls) == 1` (today the turn ends silently with nothing applied and nothing said).

- [ ] **Step 3: Write the implementation**

In `actions.py`, directly after `extract_edits`:

```python
@dataclass
class MalformedEdit:
    path: str
    body: str


def extract_malformed_edits(model_output: str) -> list[MalformedEdit]:
    """The exact complement of extract_edits: every `edit` fence whose body
    is not a well-formed SEARCH/REPLACE pair. These used to be dropped
    silently, so the turn simply ended with nothing applied and nothing
    said; run_turn now feeds each one back as a structured ERROR."""
    return [
        MalformedEdit(path=m.group(2).strip(), body=m.group(3))
        for m in EDIT_BLOCK_RE.finditer(model_output)
        if EDIT_BODY_RE.search(m.group(3)) is None
    ]
```

In `main.py`'s `run_turn`, immediately after the `for edit in actions.extract_edits(output):` loop:

```python
        for bad in actions.extract_malformed_edits(output):
            action_results.append(truncate_text(actions.format_action_error(
                action="edit",
                reason="edit block is not a valid SEARCH/REPLACE pair",
                path=bad.path,
                suggestion=(
                    "use exactly:\n<<<<<<< SEARCH\n<the old lines>\n=======\n"
                    "<the new lines>\n>>>>>>> REPLACE"
                ),
            )))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_actions tests.test_main -v`
Expected: PASS, including every pre-existing test in both files.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile actions.py main.py
```

- [ ] **Step 6: Update the docs this task's behaviour changes**

In `README.md`'s **Actions** table, replace the ` ```edit:path ` row's last cell with:

```markdown
| **on failure *and* on a malformed fence** (structured ERROR block, up to 2 hops) |
```

In `README.md`'s **Security** section, add:

```markdown
- EOF on stdin is treated as "no" at every confirmation prompt
  (`ui.confirm`), so a piped or non-interactive run can never auto-approve
  and can never die with a traceback halfway through a turn.
```

In `docs/BACKLOG.md`, at the top of **## Done**, add:

```markdown
- **Slice A: blocking safety foundation** (`pathpolicy.py`, `textfile.py`,
  `actions.py`, `gitsafety.py`, `ui.py`). One path gate for every mutation
  (no `.git/**`, no credential/key files, `.gitignore`/`.github/` still
  writable); byte-faithful writes (CRLF and BOM preserved, non-UTF-8
  refused rather than mangled, diff shown before every overwrite, identical
  content skipped); `git add`/`commit` path-scoped so a user's unrelated
  staged work is never swept into a `localcoder:` commit; `/undo` rebuilt on
  `git revert` -- non-destructive, root-commit-capable, conflict-aborting,
  SHA-based "already reverted" detection; EOF at a confirmation is "no";
  `OSError` never escapes an action; a malformed `edit` fence now gets the
  same structured `ERROR:` feedback a failed edit gets instead of being
  dropped silently. See
  `docs/superpowers/specs/2026-09-04-safe-verification-repair-design.md`.
```

- [ ] **Step 7: Commit and confirm Slice A is complete**

```bash
git add actions.py main.py tests/test_actions.py tests/test_main.py README.md docs/BACKLOG.md
git commit -m "feat: malformed edit fences get structured feedback; close Slice A"
python3 -m unittest discover tests
rg -n "git add -A" --glob '!docs/**' --glob '!tests/**' .
rg -n "reset --hard" --glob '!docs/**' .
```

Expected: suite green, both `rg` searches find nothing. **Slice A is now complete. Do not start Task 11 until this checkpoint passes.**

---

# SLICE B — Deterministic verification → repair

---

## Task 11: `execution.py` — `CommandResult`, `run_shell`/`run_argv`, process-group kill

**Model:** gpt-5.6-sol-medium (process execution work)

**Spec:** §6.5, §7.3, §8.4, §8.8, §9

**Files:**
- Modify: `execution.py` (whole file; `apply_run` at `execution.py:37-65` becomes a wrapper)
- Test: `tests/test_execution.py`

**Interfaces:**
- Consumes: `ui` (existing), `is_denied` (existing, `execution.py:33`).
- Produces, for Tasks 13 and 14:
  - `execution.Status` — `str, enum.Enum` with `OK`, `FAILED`, `NO_TESTS`, `TIMEOUT`, `DENIED`, `DECLINED`, `LAUNCH_ERROR` (values `"ok"`, `"failed"`, `"no_tests"`, `"timeout"`, `"denied"`, `"declined"`, `"launch_error"`).
  - `execution.CommandResult` — frozen dataclass: `kind: str`, `display: str`, `status: Status`, `exit_code: int | None`, `stdout: str`, `stderr: str`, `duration_s: float`, `timeout_s: int`, `truncated: bool`; properties `ok -> bool` and `combined -> str`.
  - `execution.run_shell(project_root: str, command: str, timeout_s: int = TIMEOUT_S) -> CommandResult` (`kind="shell"`, `shell=True`, calls `is_denied` itself).
  - `execution.run_argv(project_root: str, argv: Sequence[str], timeout_s: int) -> CommandResult` (`kind="argv"`, `shell=False`, no denylist).
  - `execution.apply_run(project_root, command, confirm=True) -> str | None` — signature, return type and returned text format all unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_execution.py`:

```python
def _dead_or_zombie(pid: int) -> bool:
    """Linux-specific and deliberately so: after a killpg the grandchild may
    briefly be a zombie awaiting reaping, and os.kill(pid, 0) succeeds for a
    zombie -- which would make this test pass for the wrong reason if the
    kill had failed, and fail flakily when it worked."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(") ", 1)[1].split()[0] == "Z"
    except OSError:
        return True


class TestRunArgv(unittest.TestCase):
    def test_shell_metacharacters_are_passed_literally(self):
        result = execution.run_argv("/tmp", ["printf", "%s", "$HOME;ls"], timeout_s=10)
        self.assertIs(result.status, execution.Status.OK)
        self.assertEqual(result.stdout, "$HOME;ls")
        self.assertEqual(result.kind, "argv")

    def test_timeout_kills_the_whole_process_group(self):
        start = time.monotonic()
        result = execution.run_argv(
            "/tmp", [sys.executable, "-c",
                     "import subprocess,sys,time;"
                     "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                     "print(p.pid, flush=True); time.sleep(30)"],
            timeout_s=1,
        )
        elapsed = time.monotonic() - start
        self.assertIs(result.status, execution.Status.TIMEOUT)
        self.assertIsNone(result.exit_code)
        self.assertLess(elapsed, 10)

        grandchild_pid = int(result.stdout.split()[0])
        for _ in range(30):
            if _dead_or_zombie(grandchild_pid):
                break
            time.sleep(0.1)
        else:
            self.fail("the grandchild survived the process-group kill")

    def test_fields_populated_for_ok_and_failed(self):
        ok = execution.run_argv("/tmp", [sys.executable, "-c", "print('hi')"], timeout_s=10)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.exit_code, 0)
        self.assertIn("hi", ok.stdout)
        self.assertEqual(ok.timeout_s, 10)
        self.assertGreaterEqual(ok.duration_s, 0.0)
        self.assertFalse(ok.truncated)

        bad = execution.run_argv(
            "/tmp", [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            timeout_s=10,
        )
        self.assertFalse(bad.ok)
        self.assertIs(bad.status, execution.Status.FAILED)
        self.assertEqual(bad.exit_code, 3)
        self.assertIn("boom", bad.combined)

    def test_missing_binary_is_a_launch_error_not_a_crash(self):
        result = execution.run_argv("/tmp", ["localcoder-no-such-binary"], timeout_s=5)
        self.assertIs(result.status, execution.Status.LAUNCH_ERROR)
        self.assertIsNone(result.exit_code)


class TestRunShell(unittest.TestCase):
    def test_denied_command_never_executes_even_without_apply_run(self):
        result = execution.run_shell("/tmp", "rm -rf /")
        self.assertIs(result.status, execution.Status.DENIED)
        self.assertEqual(result.kind, "shell")
        self.assertEqual(result.stdout, "")

    def test_shell_features_still_work(self):
        result = execution.run_shell("/tmp", "echo one && echo two")
        self.assertIs(result.status, execution.Status.OK)
        self.assertIn("one", result.stdout)
        self.assertIn("two", result.stdout)
```

Add `import sys` and `import time` to `tests/test_execution.py` (the existing `import unittest` / `from unittest import mock` / `import execution` stay).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_execution -v`
Expected: FAIL — `AttributeError: module 'execution' has no attribute 'run_argv'` (and `Status`, `run_shell`).

- [ ] **Step 3: Write the implementation**

Rewrite `execution.py` below the denylist (keep the module docstring, `MAX_OUTPUT_CHARS`, `TIMEOUT_S`, `_DENIED_PATTERNS`, `is_denied` exactly as they are):

```python
import enum
import os
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass


class Status(str, enum.Enum):
    OK = "ok"                      # exit 0
    FAILED = "failed"              # exit != 0
    NO_TESTS = "no_tests"          # assigned by verification.py only
    TIMEOUT = "timeout"            # killed after timeout_s
    DENIED = "denied"              # denylist, never prompted
    DECLINED = "declined"          # y/N answered no (or stdin at EOF)
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
    truncated: bool         # combined output exceeded MAX_OUTPUT_CHARS

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    @property
    def combined(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part.strip())


def _kill_group(proc: subprocess.Popen) -> None:
    """start_new_session=True gave this process its own group, so one killpg
    takes the shell's grandchildren with it -- without this, a timed-out
    command's real work keeps burning the CPU after we stop waiting."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _capture(command, *, shell: bool, kind: str, display: str,
             project_root: str, timeout_s: int) -> CommandResult:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            command, shell=shell, cwd=project_root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except OSError as e:
        return CommandResult(
            kind=kind, display=display, status=Status.LAUNCH_ERROR, exit_code=None,
            stdout="", stderr=str(e), duration_s=time.monotonic() - start,
            timeout_s=timeout_s, truncated=False,
        )

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        out, err = proc.communicate()   # collect whatever was produced first

    out, err = out or "", err or ""
    if timed_out:
        status, exit_code = Status.TIMEOUT, None
    elif proc.returncode == 0:
        status, exit_code = Status.OK, 0
    else:
        status, exit_code = Status.FAILED, proc.returncode
    return CommandResult(
        kind=kind, display=display, status=status, exit_code=exit_code,
        stdout=out, stderr=err, duration_s=time.monotonic() - start,
        timeout_s=timeout_s, truncated=len(out) + len(err) > MAX_OUTPUT_CHARS,
    )


def run_shell(project_root: str, command: str, timeout_s: int = TIMEOUT_S) -> CommandResult:
    """A string the MODEL produced: shell=True, and the denylist is checked
    here rather than only in apply_run, so no future caller can bypass it."""
    if is_denied(command):
        return CommandResult(
            kind="shell", display=command, status=Status.DENIED, exit_code=None,
            stdout="", stderr="", duration_s=0.0, timeout_s=timeout_s, truncated=False,
        )
    return _capture(command, shell=True, kind="shell", display=command,
                    project_root=project_root, timeout_s=timeout_s)


def run_argv(project_root: str, argv: Sequence[str], timeout_s: int) -> CommandResult:
    """A list LOCALCODER produced (a literal from verification.py, or the
    user's own config.json verify_command): shell=False, so no metacharacter
    is ever interpreted, and no denylist -- there is no untrusted string to
    filter. A model-emitted string is never turned into an argv list."""
    argv = list(argv)
    return _capture(argv, shell=False, kind="argv", display=shlex.join(argv),
                    project_root=project_root, timeout_s=timeout_s)


def apply_run(project_root: str, command: str, confirm: bool = True) -> str | None:
    """Returns captured output to feed back to the model, or None if the
    command was refused/denied/skipped. Thin wrapper over run_shell -- the
    signature, return type and text format are unchanged on purpose."""
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
```

Keep `re` in the imports (the denylist patterns still need it) and keep `import ui` — the existing `MAX_OUTPUT_CHARS = 4000`, `TIMEOUT_S = 120`, `_DENIED_PATTERNS` and `is_denied` are unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_execution -v`
Expected: PASS, including every pre-existing `apply_run` test unmodified.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile execution.py
```

Note: the timeout test sleeps ~1.5s, so the suite grows from ~1s to ~3s. That is the one acceptable exception to the "~1s" target; nothing else in Slice B may add wall time.

- [ ] **Step 6: Commit**

```bash
git add execution.py tests/test_execution.py
git commit -m "feat: structured CommandResult, run_shell/run_argv, process-group kill on timeout"
```

---

## Task 12: `verification.py` — data types and deterministic discovery

**Model:** composer-2.5-fast (complete-spec, 2-file mechanical task)

**Spec:** §6.6 (discovery table, language-of-mutation promotion), §7.4

**Files:**
- Create: `verification.py`
- Create test: `tests/test_verification.py`

**Interfaces:**
- Consumes: `execution.Status`, `execution.CommandResult` (Task 11); `context.tree.list_source_files(root: str) -> list[str]` (existing, `context/tree.py:71`).
- Produces, for Tasks 13, 14, 15, 17:
  - `verification.VerifyConfig` — frozen dataclass: `enabled: bool`, `timeout_s: int`, `override_argv: list[str] | None`.
  - `verification.VerificationCommand` — frozen dataclass: `kind: str`, `argv: list[str]`, `label: str`.
  - `verification.VerificationOutcome` — frozen dataclass: `ran: bool`, `command: VerificationCommand | None`, `result: CommandResult | None`, `skip_reason: str`; property `needs_repair -> bool`.
  - `verification.discover(project_root: str, changed_paths: Sequence[str], override_argv: Sequence[str] | None = None) -> VerificationCommand | None`.
  - Constant `NPM_PLACEHOLDER_TEST`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_verification.py`:

```python
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verification
from execution import Status


def _write(root, rel, text=""):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _which_always_found():
    """npm/cargo/go are not installed on every machine that runs this suite,
    and discovery deliberately drops a candidate whose binary is missing --
    so the tests that assert those candidates pretend the binary is there."""
    return mock.patch("shutil.which", return_value="/usr/bin/stub")


class TestDiscover(unittest.TestCase):
    def test_pytest_ini(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pytest.ini", "[pytest]\n")
            _write(root, "app.py", "x = 1\n")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "pytest")
            self.assertEqual(command.argv, [sys.executable, "-m", "pytest", "-q", "-x"])

    def test_pyproject_pytest_section(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pyproject.toml", "[tool.pytest.ini_options]\naddopts = ''\n")
            self.assertEqual(verification.discover(root, []).kind, "pytest")

    def test_setup_cfg_pytest_section(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "setup.cfg", "[tool:pytest]\n")
            self.assertEqual(verification.discover(root, []).kind, "pytest")

    def test_tests_directory(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "tests/test_x.py", "")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "unittest")
            self.assertEqual(
                command.argv,
                [sys.executable, "-m", "unittest", "discover", "-q", "-s", "tests", "-t", "."],
            )

    def test_top_level_test_file(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "test_x.py", "")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "unittest")
            self.assertEqual(command.argv, [sys.executable, "-m", "unittest", "discover", "-q"])

    def test_package_json_with_a_real_test_script(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "package.json", json.dumps({"scripts": {"test": "jest"}}))
            with _which_always_found():
                command = verification.discover(root, [])
            self.assertEqual(command.kind, "npm")
            self.assertEqual(command.argv, ["npm", "test", "--silent"])

    def test_npm_placeholder_script_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "package.json",
                   json.dumps({"scripts": {"test": verification.NPM_PLACEHOLDER_TEST}}))
            _write(root, "app.py", "x = 1\n")
            self.assertEqual(verification.discover(root, []).kind, "pysyntax")

    def test_cargo(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            with _which_always_found():
                command = verification.discover(root, [])
            self.assertEqual(command.kind, "cargo")
            self.assertEqual(command.argv, ["cargo", "test", "--quiet"])

    def test_go(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "go.mod", "module x\n")
            with _which_always_found():
                command = verification.discover(root, [])
            self.assertEqual(command.kind, "go")
            self.assertEqual(command.argv, ["go", "test", "./..."])

    def test_python_syntax_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            command = verification.discover(root, [])
            self.assertEqual(command.kind, "pysyntax")
            self.assertEqual(command.argv, [sys.executable, "-m", "compileall", "-q", "."])

    def test_empty_directory_yields_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(verification.discover(root, []))

    def test_missing_executable_drops_the_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            _write(root, "app.py", "x = 1\n")
            with mock.patch("shutil.which", return_value=None):
                self.assertEqual(verification.discover(root, []).kind, "pysyntax")

    def test_language_of_mutation_promotion(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            _write(root, "src/lib.rs", "fn main() {}\n")
            _write(root, "tests/test_x.py", "")
            with _which_always_found():
                self.assertEqual(verification.discover(root, ["src/lib.rs"]).kind, "cargo")
                self.assertEqual(verification.discover(root, ["app.py"]).kind, "unittest")
                # No changed paths -> no promotion -> plain table order.
                self.assertEqual(verification.discover(root, []).kind, "unittest")

    def test_override_wins_over_everything(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "Cargo.toml", "[package]\nname = 'x'\n")
            command = verification.discover(root, [], override_argv=["make", "check"])
            self.assertEqual(command.kind, "override")
            self.assertEqual(command.argv, ["make", "check"])
            self.assertEqual(command.label, "make check")


class TestNeedsRepair(unittest.TestCase):
    def _outcome(self, status):
        result = verification.CommandResult(
            kind="argv", display="x", status=status, exit_code=1, stdout="", stderr="",
            duration_s=0.0, timeout_s=180, truncated=False,
        )
        command = verification.VerificationCommand(kind="unittest", argv=["x"], label="x")
        return verification.VerificationOutcome(True, command, result, "")

    def test_failed_and_timeout_need_repair(self):
        self.assertTrue(self._outcome(Status.FAILED).needs_repair)
        self.assertTrue(self._outcome(Status.TIMEOUT).needs_repair)

    def test_everything_else_does_not(self):
        for status in (Status.OK, Status.NO_TESTS, Status.LAUNCH_ERROR, Status.DECLINED):
            self.assertFalse(self._outcome(status).needs_repair, status)

    def test_a_skipped_outcome_never_needs_repair(self):
        outcome = verification.VerificationOutcome(False, None, None, "verification declined")
        self.assertFalse(outcome.needs_repair)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_verification -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'verification'`.

- [ ] **Step 3: Write the implementation**

Create `verification.py`:

```python
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
from dataclasses import dataclass, replace
from pathlib import Path

import execution
import ui
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_verification -v`
Expected: PASS.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile verification.py
```

- [ ] **Step 6: Commit**

```bash
git add verification.py tests/test_verification.py
git commit -m "feat: deterministic verification command discovery"
```

---

## Task 13: `verification.py` — ANSI stripping, failure extraction, repair prompt

**Model:** composer-2.5-fast (complete-spec, 2-file mechanical task)

**Spec:** §6.6 (Failure extraction, Repair prompt), §7.7

**Files:**
- Modify: `verification.py` (append; do not change anything from Task 12)
- Test: `tests/test_verification.py`

**Interfaces:**
- Consumes: `VerificationOutcome`, `FEEDBACK_MAX_CHARS`, `Status` (Task 12); `context.truncate.truncate_text` (existing).
- Produces, for Tasks 14 and 17:
  - `verification.strip_ansi(text: str) -> str`
  - `verification.extract_failure_context(stdout: str, stderr: str, max_chars: int = FEEDBACK_MAX_CHARS) -> str`
  - `verification.failure_feedback(outcome: VerificationOutcome) -> str` — the string appended to the task for the single repair call; always contains the literal marker `--- VERIFICATION FAILED AFTER YOUR CHANGE ---`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_verification.py`:

```python
class TestStripAnsi(unittest.TestCase):
    def test_removes_csi_sequences(self):
        self.assertEqual(verification.strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def test_removes_osc_sequences(self):
        self.assertEqual(verification.strip_ansi("\x1b]0;title\x07body"), "body")

    def test_collapses_carriage_return_redraws(self):
        self.assertEqual(
            verification.strip_ansi("building 10%\rbuilding 50%\rbuilding 100%\ndone"),
            "building 100%\ndone",
        )


class TestExtractFailureContext(unittest.TestCase):
    def test_keeps_the_traceback_and_the_final_summary(self):
        stdout = "\n".join(
            ["noise"] * 200
            + ["Traceback (most recent call last):",
               '  File "calc.py", line 2, in divide',
               "ZeroDivisionError: division by zero"]
            + ["filler"] * 200
            + ["FAILED (errors=1)"]
        )
        extracted = verification.extract_failure_context(stdout, "")
        self.assertIn("Traceback (most recent call last):", extracted)
        self.assertIn("ZeroDivisionError", extracted)
        self.assertIn("FAILED (errors=1)", extracted)
        self.assertNotIn("noise\nnoise\nnoise\nnoise\nnoise\nnoise", extracted)

    def test_never_exceeds_the_cap(self):
        extracted = verification.extract_failure_context("x" * 50_000, "y" * 50_000)
        self.assertLessEqual(len(extracted), verification.FEEDBACK_MAX_CHARS)

    def test_falls_back_to_the_tail_when_no_marker_matches(self):
        stdout = "\n".join(f"line {i}" for i in range(500))
        extracted = verification.extract_failure_context(stdout, "")
        self.assertIn("line 499", extracted)
        self.assertNotIn("line 0\n", extracted)

    def test_empty_output_is_empty(self):
        self.assertEqual(verification.extract_failure_context("", ""), "")


class TestFailureFeedback(unittest.TestCase):
    def _outcome(self, status, exit_code, stdout):
        command = verification.VerificationCommand(
            kind="unittest",
            argv=[sys.executable, "-m", "unittest", "discover", "-q"],
            label="python3 -m unittest discover -q",
        )
        result = verification.CommandResult(
            kind="argv", display=command.label, status=status, exit_code=exit_code,
            stdout=stdout, stderr="", duration_s=1.0, timeout_s=180, truncated=False,
        )
        return verification.VerificationOutcome(True, command, result, "")

    def test_failed_feedback_shape(self):
        text = verification.failure_feedback(
            self._outcome(Status.FAILED, 1, "AssertionError: 1 != 2\nFAILED (failures=1)")
        )
        self.assertIn("--- VERIFICATION FAILED AFTER YOUR CHANGE ---", text)
        self.assertIn("python3 -m unittest discover -q", text)
        self.assertIn("exit code:\n1", text)
        self.assertIn("AssertionError", text)
        self.assertIn("```edit", text)
        self.assertIn("--- END VERIFICATION RESULT ---", text)

    def test_timeout_feedback_reports_the_timeout_instead_of_an_exit_code(self):
        text = verification.failure_feedback(self._outcome(Status.TIMEOUT, None, "partial output"))
        self.assertIn("timed out after 180s", text)
        self.assertNotIn("exit code:", text)
        self.assertIn("partial output", text)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_verification.TestStripAnsi tests.test_verification.TestExtractFailureContext tests.test_verification.TestFailureFeedback -v`
Expected: FAIL — `AttributeError: module 'verification' has no attribute 'strip_ansi'`.

- [ ] **Step 3: Write the implementation**

Append to `verification.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_verification -v`
Expected: PASS.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile verification.py
```

- [ ] **Step 6: Commit**

```bash
git add verification.py tests/test_verification.py
git commit -m "feat: ANSI-stripped failure extraction and the single repair prompt"
```

---

## Task 14: `verification.verify_project` — confirmed execution and reporting

**Model:** gpt-5.6-sol-medium (multi-file integration + process execution)

**Spec:** §6.6 (`verify_project`), §7.3 (NO_TESTS reclassification), §8.2, §8.5, §9

**Files:**
- Modify: `verification.py` (append)
- Modify: `docs/LESSONS_LEARNED.md`
- Test: `tests/test_verification.py`

**Interfaces:**
- Consumes: `discover` (Task 12), `extract_failure_context` (Task 13), `execution.run_argv` (Task 11), `ui.info/success/warn/error/sub/confirm`.
- Produces, for Tasks 17 and 18: `verification.verify_project(project_root: str, changed_paths: Sequence[str], cfg: VerifyConfig, *, confirm: bool = True) -> VerificationOutcome`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_verification.py`:

```python
class TestVerifyProject(unittest.TestCase):
    def _cfg(self, enabled=True, timeout_s=180, override_argv=None):
        return verification.VerifyConfig(
            enabled=enabled, timeout_s=timeout_s, override_argv=override_argv
        )

    def test_disabled_config_runs_nothing(self):
        with mock.patch("execution.run_argv", side_effect=AssertionError("must not run")):
            outcome = verification.verify_project("/tmp", [], self._cfg(enabled=False))
        self.assertFalse(outcome.ran)
        self.assertEqual(outcome.skip_reason, "verification disabled in config")

    def test_unsupported_project_reports_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("execution.run_argv", side_effect=AssertionError("must not run")):
                outcome = verification.verify_project(root, [], self._cfg())
        self.assertFalse(outcome.ran)
        self.assertEqual(outcome.skip_reason, "no verification command for this project")

    def test_declined_never_executes_anything(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            with mock.patch("execution.run_argv", side_effect=AssertionError("must not run")), \
                 mock.patch("builtins.input", return_value="n"), \
                 mock.patch("ui._enabled", return_value=False):
                outcome = verification.verify_project(root, [], self._cfg())
        self.assertFalse(outcome.ran)
        self.assertEqual(outcome.skip_reason, "verification declined")
        self.assertIs(outcome.result.status, Status.DECLINED)
        self.assertFalse(outcome.needs_repair)

    def test_every_execution_is_confirmed_separately(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            with mock.patch("builtins.input", return_value="y") as prompt, \
                 mock.patch("ui._enabled", return_value=False):
                verification.verify_project(root, [], self._cfg())
                verification.verify_project(root, [], self._cfg())
            self.assertEqual(prompt.call_count, 2)   # no session-level "always yes"

    def test_passing_run_reports_ok(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "app.py", "x = 1\n")
            with mock.patch("builtins.input", return_value="y"), \
                 mock.patch("ui._enabled", return_value=False):
                outcome = verification.verify_project(root, ["app.py"], self._cfg())
        self.assertTrue(outcome.ran)
        self.assertIs(outcome.result.status, Status.OK)
        self.assertFalse(outcome.needs_repair)

    def test_failing_run_needs_repair(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "broken.py", "def f(:\n")
            with mock.patch("builtins.input", return_value="y"), \
                 mock.patch("ui._enabled", return_value=False):
                outcome = verification.verify_project(root, ["broken.py"], self._cfg())
        self.assertTrue(outcome.ran)
        self.assertIs(outcome.result.status, Status.FAILED)
        self.assertTrue(outcome.needs_repair)

    def test_exit_five_is_no_tests_for_unittest(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "test_empty.py", "# no test cases at all\n")
            with mock.patch("builtins.input", return_value="y"), \
                 mock.patch("ui._enabled", return_value=False):
                outcome = verification.verify_project(root, [], self._cfg())
        self.assertEqual(outcome.command.kind, "unittest")
        self.assertIs(outcome.result.status, Status.NO_TESTS)
        self.assertFalse(outcome.needs_repair)

    def test_exit_five_stays_failed_for_other_kinds(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("builtins.input", return_value="y"), \
                 mock.patch("ui._enabled", return_value=False):
                outcome = verification.verify_project(
                    root, [], self._cfg(override_argv=[sys.executable, "-c", "raise SystemExit(5)"])
                )
        self.assertEqual(outcome.command.kind, "override")
        self.assertIs(outcome.result.status, Status.FAILED)
        self.assertTrue(outcome.needs_repair)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_verification.TestVerifyProject -v`
Expected: FAIL — `AttributeError: module 'verification' has no attribute 'verify_project'`.

- [ ] **Step 3: Write the implementation**

Append to `verification.py`:

```python
_NO_TESTS_KINDS = ("unittest", "pytest")


def _report(command: VerificationCommand, result: CommandResult) -> None:
    if result.status is Status.OK:
        ui.success("verification passed")
        return
    if result.status is Status.NO_TESTS:
        ui.warn("verification ran no tests")
        return
    if result.status is Status.TIMEOUT:
        ui.error(f"verification timed out after {result.timeout_s}s")
    elif result.status is Status.LAUNCH_ERROR:
        ui.error(f"verification could not start: {result.stderr.strip()}")
    else:
        ui.error(f"verification failed (exit {result.exit_code})")
    for line in extract_failure_context(result.stdout, result.stderr).splitlines():
        ui.sub(line)


def verify_project(project_root: str, changed_paths: Sequence[str], cfg: VerifyConfig,
                   *, confirm: bool = True) -> VerificationOutcome:
    """Runs the discovered command once, behind its own y/N. There is no
    once-per-session blanket approval and no --yes flag: every single
    execution is confirmed separately."""
    if not cfg.enabled:
        return VerificationOutcome(False, None, None, "verification disabled in config")

    command = discover(project_root, changed_paths, cfg.override_argv)
    if command is None:
        return VerificationOutcome(False, None, None, "no verification command for this project")

    ui.info(f"verification: {command.label} (timeout {cfg.timeout_s}s)")
    if confirm and not ui.confirm(f"  run verification `{command.label}`?"):
        declined = CommandResult(
            kind="argv", display=command.label, status=Status.DECLINED, exit_code=None,
            stdout="", stderr="", duration_s=0.0, timeout_s=cfg.timeout_s, truncated=False,
        )
        return VerificationOutcome(False, command, declined, "verification declined")

    result = execution.run_argv(project_root, command.argv, cfg.timeout_s)
    # Measured: `unittest discover` exits 5 when it collects nothing, and
    # pytest uses the same code for "no tests collected". execution.py cannot
    # know a tool's exit-code semantics, so the reclassification lives here
    # and applies to those two kinds only.
    if (result.status is Status.FAILED and result.exit_code == 5
            and command.kind in _NO_TESTS_KINDS):
        result = replace(result, status=Status.NO_TESTS)

    _report(command, result)
    return VerificationOutcome(True, command, result, "")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_verification -v`
Expected: PASS.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile verification.py
```

- [ ] **Step 6: Update the docs this task's behaviour changes**

Append to `docs/LESSONS_LEARNED.md`:

```markdown
## Exit code 5 from `unittest`/`pytest` means "no tests", not "failure"

**Symptom risk**: `python3 -m unittest discover -q` in a directory with no
test cases exits **5** ("NO TESTS RAN"), and pytest uses the same code for
"no tests collected". Treating any non-zero exit as a failure would fire a
pointless repair call -- an extra multi-minute model call on CPU-only
hardware -- every time localcoder touched a project that has no tests yet.

**Takeaway**: exit-code semantics are per-tool, so the reclassification
belongs in the layer that knows which tool ran (`verification.py`), not in
the generic runner (`execution.py`). Only `unittest` and `pytest` get the
special case; a `LAUNCH_ERROR` (missing toolchain) is likewise reported but
never treated as a code defect the model can fix.
```

- [ ] **Step 7: Commit**

```bash
git add verification.py tests/test_verification.py docs/LESSONS_LEARNED.md
git commit -m "feat: verify_project, one confirmed argv execution with exit-5 handling"
```

---

## Task 15: `config.py` — three verification keys

**Model:** composer-2.5-fast (complete-spec, 2-file mechanical task)

**Spec:** §6.7, §7.9

**Files:**
- Modify: `config.py` — `DEFAULTS` (`config.py:28-101`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces, for Task 17: `config.DEFAULTS["verify_after_change"] = True`, `config.DEFAULTS["verify_timeout_s"] = 180`, `config.DEFAULTS["verify_command"] = None`. `derive_max_context_chars()` is untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
class TestVerificationDefaults(unittest.TestCase):
    def test_new_keys_exist_with_the_designed_defaults(self):
        self.assertIs(config.DEFAULTS["verify_after_change"], True)
        self.assertEqual(config.DEFAULTS["verify_timeout_s"], 180)
        self.assertIsNone(config.DEFAULTS["verify_command"])

    def test_an_existing_config_without_them_still_loads_them(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "config.json")
            path.write_text(json.dumps({"model": "qwen2.5-coder:7b"}))
            with mock.patch.object(config, "CONFIG_FILE", path):
                cfg = config.load_config()
        self.assertIs(cfg["verify_after_change"], True)
        self.assertEqual(cfg["verify_timeout_s"], 180)
        self.assertIsNone(cfg["verify_command"])
```

Match the import style already used in `tests/test_config.py`; add `import json`, `import tempfile`, `from pathlib import Path`, `from unittest import mock` if missing.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_config.TestVerificationDefaults -v`
Expected: FAIL with `KeyError: 'verify_after_change'`.

- [ ] **Step 3: Write the implementation**

In `config.py`'s `DEFAULTS`, after the `"max_tree_entries": 400,` entry:

```python
    # Verification (see verification.py). Defaults are on with a
    # conservative timeout: an existing config.json with none of these keys
    # behaves exactly as designed. `verify_after_change: false` is the kill
    # switch for slower hardware. The number of repair hops is deliberately
    # NOT configurable -- MAX_REPAIR_HOPS lives in main.py, because the one
    # bound preventing an unbounded fix/verify loop should not sit in a file
    # a user edits casually.
    "verify_after_change": True,   # run the discovered check after a mutation
    "verify_timeout_s": 180,       # per verification execution
    "verify_command": None,        # list[str] argv override; skips discovery
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_config -v`
Expected: PASS.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile config.py
```

- [ ] **Step 6: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: verify_after_change / verify_timeout_s / verify_command defaults"
```

---

## Task 16: Extract `_append_result` and `_apply_blocks` from `run_turn` (pure refactor)

**Model:** claude-opus-5-thinking-high (lifecycle/KV integration)

**Spec:** §6.8 (the two helpers), §7.5

**Files:**
- Modify: `main.py:180-312` (`run_turn`)
- Test: `tests/test_main.py` (additions only; **every existing test must pass unmodified**)

**Interfaces:**
- Consumes: `actions.extract_malformed_edits` (Task 10), `truncate_text` (existing).
- Produces, for Task 17:
  - `main._append_result(task: str, current_task: str, results: list[str], max_total_context_chars: int, num_ctx: int, kv_context: list[int] | None) -> tuple[str, str, list[int] | None]` — returns `(new current_task, delta to send, kv_context or None)`. The delta is only meaningful when the returned `kv_context` survives; the caller sends `current_task` instead whenever it comes back `None`.
  - `main._apply_blocks(output: str, project_root: str, cce: CCEClient, mutated: list[str]) -> list[str]` — applies every block in today's order (writes, deletes, shell suggestions, edits, malformed edits, runs, fetches, searches, symbols), appends each successfully mutated relative path to `mutated` (ordered, de-duplicated), and returns the truncated action results.

**This task must not change any observable behaviour.** It exists so Task 17's repair phase reuses the budget logic instead of duplicating it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main.py`:

```python
class TestAppendResultHelper(unittest.TestCase):
    def test_fits_budget_keeps_the_cache_and_grows_the_task(self):
        new_task, delta, kv = main._append_result(
            "task", "task", ["RESULT"], max_total_context_chars=100_000,
            num_ctx=8192, kv_context=[1, 2],
        )
        self.assertIn("RESULT OF YOUR LAST ACTION", delta)
        self.assertIn("RESULT", delta)
        self.assertEqual(new_task, "task" + delta)
        self.assertEqual(kv, [1, 2])

    def test_over_budget_drops_history_and_the_cache(self):
        with mock.patch("ui._enabled", return_value=False):
            new_task, delta, kv = main._append_result(
                "task", "task" + "x" * 500, ["NEWEST"], max_total_context_chars=200,
                num_ctx=8192, kv_context=[1, 2],
            )
        self.assertIsNone(kv)
        self.assertTrue(new_task.startswith("task"))
        self.assertIn("NEWEST", new_task)
        self.assertNotIn("x" * 500, new_task)


class TestApplyBlocksHelper(unittest.TestCase):
    def test_tracks_mutated_paths_and_returns_action_results(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "old.py").write_text("bye\n")
            output = (
                "```write:new.py\nprint('hi')\n```\n"
                "```delete:old.py\n```\n"
                "```edit:missing.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n```"
            )
            mutated: list[str] = []
            with mock.patch("ui._enabled", return_value=False), \
                 mock.patch("builtins.input", return_value="y"):
                results = main._apply_blocks(output, root, _FakeCCE(available=False), mutated)
            self.assertEqual(mutated, ["new.py", "old.py"])
            self.assertEqual(len(results), 1)
            self.assertIn("does not exist", results[0])

    def test_declined_write_is_not_tracked_as_mutated(self):
        with tempfile.TemporaryDirectory() as root:
            mutated: list[str] = []
            with mock.patch("ui._enabled", return_value=False), \
                 mock.patch("builtins.input", return_value="n"):
                main._apply_blocks("```write:new.py\nx\n```", root,
                                   _FakeCCE(available=False), mutated)
            self.assertEqual(mutated, [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_main.TestAppendResultHelper tests.test_main.TestApplyBlocksHelper -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute '_append_result'`.

- [ ] **Step 3: Write the implementation**

Add above `run_turn` in `main.py`:

```python
def _append_result(
    task: str,
    current_task: str,
    results: list[str],
    max_total_context_chars: int,
    num_ctx: int,
    kv_context: list[int] | None,
) -> tuple[str, str, list[int] | None]:
    """Returns (new current_task, delta to send, kv_context or None).

    Exactly the pre-existing hop-budget logic, extracted: append when it
    fits; otherwise warn, rebuild current_task from `task` plus the most
    recent result only, and drop the cached kv_context because it covers
    text we just dropped. `delta` is only meaningful when the returned
    kv_context survives; the caller sends `current_task` instead whenever it
    comes back None."""
    appended = (
        "\n\n--- RESULT OF YOUR LAST ACTION ---\n"
        + "\n\n".join(results)
        + "\n--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
    )
    if len(current_task) + len(appended) > max_total_context_chars:
        ui.warn(
            f"contexto acumulado dos follow-ups excede o orçamento seguro em tokens "
            f"({max_total_context_chars} chars, derivado de num_ctx={num_ctx}) -- "
            f"a descartar histórico mais antigo, mantendo só o resultado mais recente"
        )
        rebuilt = (
            f"{task}\n\n--- RESULT OF YOUR LAST ACTION ---\n"
            f"{truncate_text(results[-1], max_chars=max_total_context_chars // 2)}\n"
            "--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
        )
        return rebuilt, appended, None
    return current_task + appended, appended, kv_context


def _apply_blocks(output: str, project_root: str, cce: CCEClient,
                  mutated: list[str]) -> list[str]:
    """Applies every block in `output` in today's order (writes, deletes,
    shell suggestions, edits, malformed edits, runs, fetches, searches,
    symbols), appends each successfully mutated relative path to `mutated`,
    and returns the truncated action results that would feed a follow-up
    hop."""
    def track(path: str) -> None:
        if path not in mutated:
            mutated.append(path)

    for write in actions.extract_writes(output):
        if actions.apply_write(project_root, write):
            track(write.path)
    for path in actions.extract_deletes(output):
        if actions.apply_delete(project_root, path):
            track(path)
    for cmd in actions.extract_shell_suggestions(output):
        ui.info(f"suggested command -- not run automatically:\n  $ {cmd}")

    action_results: list[str] = []
    for edit in actions.extract_edits(output):
        result = actions.apply_edit(project_root, edit)
        if result.ok:
            track(edit.path)
        if result.error:
            action_results.append(truncate_text(result.error))
    for bad in actions.extract_malformed_edits(output):
        action_results.append(truncate_text(actions.format_action_error(
            action="edit",
            reason="edit block is not a valid SEARCH/REPLACE pair",
            path=bad.path,
            suggestion=(
                "use exactly:\n<<<<<<< SEARCH\n<the old lines>\n=======\n"
                "<the new lines>\n>>>>>>> REPLACE"
            ),
        )))
    for cmd in actions.extract_runs(output):
        result = execution.apply_run(project_root, cmd)
        if result:
            action_results.append(truncate_text(result))
    for url in actions.extract_fetches(output):
        result = webfetch.apply_fetch(url)
        if result:
            action_results.append(truncate_text(result))
    for query in actions.extract_searches(output):
        result = websearch.apply_search(query)
        if result:
            action_results.append(truncate_text(result))
    for path, symbol in actions.extract_symbol_requests(output):
        if not cce.available:
            ui.warn(f"pedido get_symbol({path}, {symbol}) mas o CCE não está ligado -- a ignorar")
            continue
        snippet = cce.get_symbol(path, symbol)
        action_results.append(
            truncate_text(snippet) if snippet
            else f"get_symbol: símbolo `{symbol}` não encontrado em {path}"
        )
    return action_results
```

Then replace `run_turn`'s hop body (`main.py:247-311`) with:

```python
        mutated_this_hop: list[str] = []
        action_results = _apply_blocks(output, project_root, cce, mutated_this_hop)

        if not action_results:
            return hop_kv_context
        if hop >= MAX_FOLLOWUP_TURNS:
            ui.info(f"follow-up limit reached ({MAX_FOLLOWUP_TURNS}) -- stopping here")
            return hop_kv_context

        current_task, appended, hop_kv_context = _append_result(
            task, current_task, action_results, max_total_context_chars, num_ctx, hop_kv_context,
        )
```

(`mutated_this_hop` is discarded for now; Task 17 replaces it with a turn-level `mutated` list.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_main -v`
Expected: PASS — critically including `TestRunTurnKvContextThreading` **unmodified**, especially `test_history_drop_branch_also_drops_the_cached_kv_context`, whose budget arithmetic depends on the delta string being byte-identical to before.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile main.py
```

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "refactor: extract _append_result and _apply_blocks from run_turn, no behaviour change"
```

---

## Task 17: The verification → repair phase in `run_turn`

**Model:** claude-opus-5-thinking-high (lifecycle/KV/repair integration)

**Spec:** §5 (data flow), §6.7 (`verify_command` validation), §6.8 (repair phase), §7.5, §7.8, §9

**Files:**
- Modify: `main.py` — `MAX_REPAIR_HOPS`, `build_verify_config`, `run_turn`'s signature and repair phase, `call_run_turn`, `main()`'s config validation.
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `verification.VerifyConfig`, `verification.verify_project`, `verification.failure_feedback` (Tasks 12-14); `_append_result`, `_apply_blocks` (Task 16); `config.DEFAULTS` keys (Task 15).
- Produces, for Task 18:
  - `main.MAX_REPAIR_HOPS = 1` (a constant, deliberately not configurable).
  - `main.build_verify_config(cfg: dict) -> verification.VerifyConfig`.
  - `main.run_turn(agent, task, context, project_root, cce, num_ctx, max_total_context_chars, initial_kv_context=None, verify=None) -> list[int] | None` — `verify=None` means "no verification", which is exactly today's behaviour.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main.py`:

```python
def _write_chunk(context, path="calc.py", body="x = 1\n"):
    chunk = {
        "response": f"```write:{path}\n{body}```",
        "done": True, "prompt_eval_count": 1, "eval_count": 1,
    }
    if context is not None:
        chunk["context"] = context
    return chunk


def _outcome(needs_repair, feedback="--- VERIFICATION FAILED AFTER YOUR CHANGE ---\nboom"):
    """A minimal stand-in for verification.VerificationOutcome."""
    return mock.Mock(ran=True, needs_repair=needs_repair, skip_reason="", _feedback=feedback)


class TestBuildVerifyConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = main.build_verify_config(
            {"verify_after_change": True, "verify_timeout_s": 180, "verify_command": None}
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.timeout_s, 180)
        self.assertIsNone(cfg.override_argv)

    def test_valid_override(self):
        cfg = main.build_verify_config({"verify_command": ["make", "check"]})
        self.assertEqual(cfg.override_argv, ["make", "check"])

    def test_invalid_override_is_warned_and_ignored(self):
        buffer = io.StringIO()
        with mock.patch("ui._enabled", return_value=False), \
             contextlib.redirect_stdout(buffer):
            cfg = main.build_verify_config({"verify_command": "make check"})
        self.assertIsNone(cfg.override_argv)
        self.assertIn("verify_command must be a list of strings", buffer.getvalue())

    def test_empty_list_override_is_also_rejected(self):
        with mock.patch("ui._enabled", return_value=False):
            self.assertIsNone(main.build_verify_config({"verify_command": []}).override_argv)


class TestVerificationRepairLifecycle(unittest.TestCase):
    """The model-call and verification-execution bounds from the design's
    §7.8 table, asserted exactly."""

    def _run(self, agent, root, verify_side_effect):
        with mock.patch("ui._enabled", return_value=False), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch("verification.verify_project",
                        side_effect=verify_side_effect) as verify_project, \
             mock.patch("verification.failure_feedback",
                        side_effect=lambda outcome: outcome._feedback):
            main.run_turn(
                agent, "task", "", root, _FakeCCE(available=False),
                num_ctx=8192, max_total_context_chars=100_000,
                verify=verification.VerifyConfig(enabled=True, timeout_s=180, override_argv=None),
            )
        return verify_project

    def test_no_mutation_never_calls_verify_project(self):
        agent = _FakeAgent([[_final_chunk([1])]])
        with tempfile.TemporaryDirectory() as root:
            verify_project = self._run(agent, root, [])
        self.assertEqual(len(agent.calls), 1)
        verify_project.assert_not_called()

    def test_mutation_plus_passing_verification_costs_no_extra_model_call(self):
        agent = _FakeAgent([[_write_chunk([1])]])
        with tempfile.TemporaryDirectory() as root:
            verify_project = self._run(agent, root, [_outcome(False)])
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(verify_project.call_count, 1)
        self.assertNotIn("VERIFICATION FAILED", agent.calls[0][0])

    def test_failing_verification_costs_exactly_one_repair_call(self):
        agent = _FakeAgent([[_write_chunk([1])], [_write_chunk([2], body="x = 2\n")]])
        with tempfile.TemporaryDirectory() as root:
            verify_project = self._run(agent, root, [_outcome(True), _outcome(False)])
        self.assertEqual(len(agent.calls), 2)
        self.assertIn("VERIFICATION FAILED", agent.calls[1][0])
        self.assertIn("boom", agent.calls[1][0])
        self.assertEqual(verify_project.call_count, 2)   # initial + final

    def test_repair_that_mutates_nothing_skips_the_final_verification(self):
        agent = _FakeAgent([[_write_chunk([1])], [_final_chunk([2])]])
        with tempfile.TemporaryDirectory() as root:
            verify_project = self._run(agent, root, [_outcome(True)])
        self.assertEqual(len(agent.calls), 2)
        self.assertEqual(verify_project.call_count, 1)

    def test_a_run_block_in_the_repair_response_executes_but_starts_no_third_call(self):
        repair = {
            "response": "```run\necho repairing\n```",
            "done": True, "prompt_eval_count": 1, "eval_count": 1, "context": [2],
        }
        agent = _FakeAgent([[_write_chunk([1])], [repair]])
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("execution.apply_run", return_value="$ echo\n(exit 0)\n") as run:
                verify_project = self._run(agent, root, [_outcome(True)])
            run.assert_called_once()
        self.assertEqual(len(agent.calls), 2)   # results discarded, never fed back
        self.assertEqual(verify_project.call_count, 1)

    def test_declined_verification_produces_no_repair(self):
        declined = mock.Mock(ran=False, needs_repair=False, skip_reason="verification declined")
        agent = _FakeAgent([[_write_chunk([1])]])
        with tempfile.TemporaryDirectory() as root:
            self._run(agent, root, [declined])
        self.assertEqual(len(agent.calls), 1)

    def test_two_exploration_hops_plus_failing_verification_is_exactly_four_calls(self):
        agent = _FakeAgent([
            [_symbol_chunk([1])],
            [_symbol_chunk([2])],
            [_write_chunk([3])],
            [_final_chunk([4])],
        ])
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("ui._enabled", return_value=False), \
                 mock.patch("builtins.input", return_value="y"), \
                 mock.patch("verification.verify_project",
                            side_effect=[_outcome(True)]), \
                 mock.patch("verification.failure_feedback",
                            side_effect=lambda outcome: outcome._feedback):
                main.run_turn(
                    agent, "task", "", root,
                    _FakeCCE(available=True, symbol_result="def foo(): ..."),
                    num_ctx=8192, max_total_context_chars=100_000,
                    verify=verification.VerifyConfig(
                        enabled=True, timeout_s=180, override_argv=None),
                )
        self.assertEqual(len(agent.calls), 4)

    def test_repair_call_reuses_the_previous_hop_kv_context_and_sends_the_delta_only(self):
        agent = _FakeAgent([[_write_chunk([100, 101])], [_final_chunk([200])]])
        with tempfile.TemporaryDirectory() as root:
            self._run(agent, root, [_outcome(True)])
        repair_task, repair_ctx, repair_kv = agent.calls[1]
        self.assertEqual(repair_kv, [100, 101])
        self.assertEqual(repair_ctx, "")
        self.assertIn("VERIFICATION FAILED", repair_task)
        self.assertNotIn("task\n\n--- RESULT", repair_task)

    def test_repair_call_falls_back_to_a_full_resend_after_the_drop_history_branch(self):
        agent = _FakeAgent([[_write_chunk([100])], [_final_chunk([200])]])
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("ui._enabled", return_value=False), \
                 mock.patch("builtins.input", return_value="y"), \
                 mock.patch("verification.verify_project", side_effect=[_outcome(True)]), \
                 mock.patch("verification.failure_feedback",
                            side_effect=lambda outcome: outcome._feedback):
                main.run_turn(
                    agent, "task", "FILECTX", root, _FakeCCE(available=False),
                    num_ctx=8192, max_total_context_chars=50,   # far too small: forces the drop
                    verify=verification.VerifyConfig(
                        enabled=True, timeout_s=180, override_argv=None),
                )
        repair_task, repair_ctx, repair_kv = agent.calls[1]
        self.assertIsNone(repair_kv)
        self.assertEqual(repair_ctx, "FILECTX")
        self.assertIn("VERIFICATION FAILED", repair_task)

    def test_verify_none_reproduces_todays_behaviour(self):
        agent = _FakeAgent([[_write_chunk([1])]])
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("ui._enabled", return_value=False), \
                 mock.patch("builtins.input", return_value="y"), \
                 mock.patch("verification.verify_project",
                            side_effect=AssertionError("must not be called")):
                main.run_turn(
                    agent, "task", "", root, _FakeCCE(available=False),
                    num_ctx=8192, max_total_context_chars=100_000,
                )
        self.assertEqual(len(agent.calls), 1)


class TestRepairBound(unittest.TestCase):
    def test_max_repair_hops_is_one(self):
        self.assertEqual(main.MAX_REPAIR_HOPS, 1)
```

Add `import contextlib`, `import io`, `import verification` to `tests/test_main.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_main.TestBuildVerifyConfig tests.test_main.TestVerificationRepairLifecycle tests.test_main.TestRepairBound -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'build_verify_config'` and `... 'MAX_REPAIR_HOPS'`; the lifecycle tests additionally fail with `TypeError: run_turn() got an unexpected keyword argument 'verify'`.

- [ ] **Step 3: Write the implementation**

In `main.py`, add `import verification` next to the other project imports and:

```python
MAX_REPAIR_HOPS = 1  # NOT configurable: this is the one bound preventing an
                     # unbounded fix/verify loop, and there is no evidence a
                     # second hop helps a 7B model.


def build_verify_config(cfg: dict) -> verification.VerifyConfig:
    override = cfg.get("verify_command")
    if override is not None and not (
        isinstance(override, list) and override
        and all(isinstance(part, str) for part in override)
    ):
        ui.warn("verify_command must be a list of strings -- ignoring it")
        override = None
    return verification.VerifyConfig(
        enabled=bool(cfg.get("verify_after_change", True)),
        timeout_s=int(cfg.get("verify_timeout_s", 180)),
        override_argv=list(override) if override else None,
    )
```

Change `run_turn`'s signature (append the parameter last, so every existing positional call keeps working):

```python
def run_turn(
    agent: Agent,
    task: str,
    context: str,
    project_root: str,
    cce: CCEClient,
    num_ctx: int,
    max_total_context_chars: int,
    initial_kv_context: list[int] | None = None,
    verify: verification.VerifyConfig | None = None,
) -> list[int] | None:
```

Extend the docstring with:

```
    `verify` enables the post-mutation verification phase; None means "no
    verification", which is exactly the pre-existing behaviour. Verification
    runs once, after the exploration loop -- never between hops, because
    running the suite mid-change tests a half-finished edit and multiplies
    the cost on hardware where each execution is real wall time.
```

Turn state becomes four locals; replace `mutated_this_hop` with a turn-level list and restructure the loop's exits so the repair phase is reachable:

```python
    current_task = task
    hop_kv_context = initial_kv_context
    mutated: list[str] = []
    for hop in range(MAX_FOLLOWUP_TURNS + 1):
        ...unchanged prompt-selection and streaming...
        action_results = _apply_blocks(output, project_root, cce, mutated)
        if not action_results:
            break
        if hop >= MAX_FOLLOWUP_TURNS:
            ui.info(f"follow-up limit reached ({MAX_FOLLOWUP_TURNS}) -- stopping here")
            break
        current_task, appended, hop_kv_context = _append_result(
            task, current_task, action_results, max_total_context_chars, num_ctx, hop_kv_context,
        )
```

(The `except OllamaError` branch keeps `return hop_kv_context` — an Ollama failure ends the turn without a verification phase.)

Then, after the loop, the repair phase — **straight-line code, no loop**, which is how "exactly one repair hop" is enforced structurally rather than by a counter:

```python
    if verify is None or not mutated:
        return hop_kv_context

    outcome = verification.verify_project(project_root, mutated, verify)
    if not outcome.needs_repair:
        return hop_kv_context

    current_task, delta, hop_kv_context = _append_result(
        task, current_task, [verification.failure_feedback(outcome)],
        max_total_context_chars, num_ctx, hop_kv_context,
    )
    prompt, ctx = (delta, "") if hop_kv_context is not None else (current_task, context)
    try:
        output, usage, hop_kv_context = stream_and_print(
            agent.run_stream(prompt, ctx, kv_context=hop_kv_context)
        )
    except OllamaError as e:
        ui.error(str(e))
        return hop_kv_context
    print_usage_summary(usage, num_ctx)

    repair_mutated: list[str] = []
    # Every repair block is still applied and still individually confirmed
    # (a ```run in the repair response executes if the human says yes), but
    # the returned action results are DISCARDED -- that is what makes the
    # model-call bound structural rather than arithmetic.
    _apply_blocks(output, project_root, cce, repair_mutated)
    if repair_mutated:
        # Reported to the human by verify_project; never fed back anywhere.
        verification.verify_project(project_root, repair_mutated, verify)
    return hop_kv_context
```

Finally, in `main()`, build the config once and thread it through `call_run_turn`:

```python
    verify_cfg = build_verify_config(cfg)
```

```python
            return run_turn(
                agent, task, ctx, project_root, cce, cfg["num_ctx"], cfg["max_total_context_chars"],
                initial_kv_context=initial_kv_context, verify=verify_cfg,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_main -v`
Expected: PASS — including `TestRunTurnKvContextThreading` and `TestMalformedEditFeedback` unmodified (they pass no `verify`, so the phase never runs).

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile main.py verification.py
```

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: post-mutation verification with exactly one bounded repair call"
```

---

## Task 18: `/verify` via discovery, BANNER, and the REPL y/n no-op

**Model:** gpt-5.6-sol-medium (multi-file integration)

**Spec:** §6.8 (REPL changes), §7.6

**Files:**
- Modify: `main.py` — `BANNER` (`main.py:41-56`), the `/verify` branch (`main.py:471-473`), and the top of the REPL dispatch (after `main.py:460-461`).
- Modify: `README.md` — new "Verification and repair" section, placed immediately after "Git safety net".
- Modify: `docs/BACKLOG.md` — a "Done" entry for Slice B.
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `verification.verify_project` (Task 14), `main.build_verify_config` (Task 17).
- Produces: `/verify` calls `verification.verify_project(project_root, [], verify_cfg)` — `changed_paths=[]` means no language promotion, so `/verify` always follows plain table order, and it makes **zero** model calls.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main.py`:

```python
class TestReplBanner(unittest.TestCase):
    def test_verify_line_describes_discovery_not_compileall(self):
        self.assertIn("run this project's test/check command (discovered, confirmed)",
                      main.BANNER)
        self.assertNotIn("compile all Python files", main.BANNER)
```

Because `main()` is a long interactive loop, exercise the two REPL behaviours through it with a scripted stdin:

```python
class TestReplVerifyAndYesNoNoop(unittest.TestCase):
    def _run_repl(self, script: str, verify_side_effect):
        """Drives main()'s REPL with a canned script, stubbing out every
        network/subprocess dependency so nothing but the branch under test
        actually happens."""
        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("sys.stdin", io.StringIO(script)), \
                 mock.patch("main.Path.cwd", return_value=Path(root)), \
                 mock.patch("ui._enabled", return_value=False), \
                 mock.patch("main.OllamaClient") as llm, \
                 mock.patch("main.CCEClient") as cce, \
                 mock.patch("main.websearch.is_online", return_value=False), \
                 mock.patch("main.build_tree", return_value="(tree)"), \
                 mock.patch("main.load_skills", return_value=[]), \
                 mock.patch("main.call_run_turn", create=True), \
                 mock.patch("verification.verify_project",
                            side_effect=verify_side_effect) as verify_project, \
                 contextlib.redirect_stdout(buffer):
                llm.return_value.is_up.return_value = True
                cce.return_value.start.return_value = False
                cce.return_value.available = False
                main.main()
        return verify_project, buffer.getvalue()

    def test_verify_command_uses_discovery_and_makes_no_model_call(self):
        outcome = mock.Mock(ran=True, skip_reason="")
        verify_project, _ = self._run_repl("/verify\n/quit\n", [outcome])
        verify_project.assert_called_once()
        args = verify_project.call_args[0]
        self.assertEqual(args[1], [])   # no changed paths -> plain table order

    def test_unsupported_project_is_reported_as_a_warning(self):
        outcome = mock.Mock(ran=False, skip_reason="no verification command for this project")
        _, printed = self._run_repl("/verify\n/quit\n", [outcome])
        self.assertIn("no verification command for this project", printed)

    def test_a_stray_yes_line_is_a_logged_no_op(self):
        verify_project, printed = self._run_repl("y\nn\nYES\n/quit\n", [])
        verify_project.assert_not_called()
        self.assertEqual(printed.count("nada a confirmar agora -- linha ignorada"), 3)
```

If driving `main()` end-to-end proves too brittle in practice, keep `TestReplBanner` and replace `TestReplVerifyAndYesNoNoop` with direct tests of two tiny extracted helpers (`main._is_stray_confirmation(line: str) -> bool` and `main._run_verify_command(project_root, verify_cfg) -> None`) called from the REPL — but extract them **only** if needed, and update this task's "Produces" block accordingly.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_main.TestReplBanner tests.test_main.TestReplVerifyAndYesNoNoop -v`
Expected: FAIL — the BANNER assertion fails on the current `compile all Python files in this project (confirmed)` text; `/verify` currently calls `execution.apply_run`, so `verify_project` is never called; and a stray `y` is currently sent to the model as a full turn.

- [ ] **Step 3: Write the implementation**

In `BANNER`, replace the `/verify` line:

```
  /verify                run this project's test/check command (discovered, confirmed)
```

Replace the `/verify` branch:

```python
            if line == "/verify":
                # A REPL command, not a turn: discovery happens inside
                # verify_project and this path never calls the model.
                outcome = verification.verify_project(project_root, [], verify_cfg)
                if not outcome.ran:
                    ui.warn(outcome.skip_reason)
                continue
```

Immediately after the `if not line: continue` guard:

```python
            if line.lower() in ("y", "n", "yes", "no"):
                # A spare confirmation line in a piped script would otherwise
                # be sent to the model as a full multi-minute phantom turn.
                ui.info("nada a confirmar agora -- linha ignorada")
                continue
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_main -v`
Expected: PASS.

- [ ] **Step 5: Run the full fast suite and py_compile**

```bash
python3 -m unittest discover tests
python3 -m py_compile main.py
```

- [ ] **Step 6: Update the docs this task's behaviour changes**

Add to `README.md`, immediately after the "Git safety net" section:

```markdown
## Verification and repair

After a turn actually changed a file, localcoder runs the project's own
check command and, if it fails, gives the model exactly one attempt to fix
it. Nothing here involves an extra model call to *decide* anything --
discovery is plain filesystem checks (`verification.py`).

Discovery order (first match wins, and any candidate whose executable is
missing from `PATH` is dropped):

| Detected by | Command |
|---|---|
| `pytest.ini`, `[tool.pytest` in `pyproject.toml`, or `[tool:pytest]` in `setup.cfg` | `python -m pytest -q -x` |
| `tests/test_*.py` | `python -m unittest discover -q -s tests -t .` |
| top-level `test_*.py` | `python -m unittest discover -q` |
| `package.json` with a real `scripts.test` | `npm test --silent` |
| `Cargo.toml` | `cargo test --quiet` |
| `go.mod` | `go test ./...` |
| any `*.py` | `python -m compileall -q .` |
| nothing above | nothing runs; localcoder says so and moves on |

Before that order is applied, candidates are re-sorted by what the turn
actually changed, so editing `src/lib.rs` in a polyglot repo picks `cargo`
and editing a README picks nothing new.

The rules that bound the cost:

- **Every single verification execution needs its own y/N**, showing the
  exact command. There is no once-per-session approval.
- Commands run as `argv` with `shell=False`; no model output ever becomes an
  argv element. A timeout (default 180s) kills the whole process group.
- **Passing verification costs zero extra model calls.** Exit code 5 from
  `unittest`/`pytest` means "no tests", not failure. A missing toolchain is
  reported, never treated as a bug to repair.
- A failure or timeout produces **exactly one** repair call, then at most one
  final verification whose result is shown to you and never fed back to the
  model. The absolute bound per turn is 4 model calls and 2 verification
  executions.
- `/verify` runs the same discovery on demand and makes **no** model call at
  all.

Config keys (`config.json`): `verify_after_change` (default `true` -- set it
to `false` on slow hardware), `verify_timeout_s` (default `180`), and
`verify_command` (default `null`; a list of strings like
`["make", "check"]` overrides discovery entirely). The repair bound itself
is deliberately not configurable.
```

In `docs/BACKLOG.md`, at the top of **## Done**, add:

```markdown
- **Slice B: deterministic verification -> repair** (`verification.py`,
  `execution.py`, `config.py`, `main.py`). After a mutation, localcoder runs
  the project's own check command -- discovered with filesystem checks only
  (pytest / unittest / npm / cargo / go / `compileall` fallback), re-sorted
  by the language of what actually changed -- as `argv` with `shell=False`,
  behind its own y/N per execution. Passing costs zero extra model calls;
  failing or timing out costs exactly one repair call followed by at most one
  final verification that can never recurse. `execution.py` gained a
  structured `CommandResult`/`Status` and a process-group kill on timeout;
  `/verify` uses discovery instead of a hardcoded `compileall`; a stray `y`
  at the REPL is a logged no-op instead of a phantom turn. See
  `docs/superpowers/specs/2026-09-04-safe-verification-repair-design.md`.
```

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py README.md docs/BACKLOG.md
git commit -m "feat: /verify uses discovery; stray y/n at the REPL is a no-op"
```

---

## Task 19: Live end-to-end case + final whole-phase verification

**Model:** claude-opus-5-thinking-high (live test + integration)

**Spec:** §10 (`tests/test_live.py`, Acceptance criteria)

**Files:**
- Modify: `tests/test_live.py` — one new case, same `LOCALCODER_LIVE_TESTS=1` gate.

**Interfaces:**
- Consumes: everything from Tasks 2-18.
- Produces: `tests/test_live.TestLiveEndToEnd.test_verification_repairs_a_failing_test` — the real-model proof that the single-repair bound and the EOF handling hold end to end.

- [ ] **Step 1: Write the live test**

Append to `tests/test_live.py`'s `TestLiveEndToEnd` class:

```python
    def test_verification_repairs_a_failing_test(self):
        """Real model, real Ollama, real verification: localcoder must write
        a fix, run the discovered unittest command behind a y/N, and -- if
        the first attempt fails -- repair exactly once, never twice."""
        with tempfile.TemporaryDirectory() as project:
            Path(project, "calc.py").write_text("def divide(a, b):\n    return a / b\n")
            Path(project, "tests").mkdir()
            Path(project, "tests", "test_calc.py").write_text(
                "import unittest\n"
                "\n"
                "from calc import divide\n"
                "\n"
                "\n"
                "class TestDivide(unittest.TestCase):\n"
                "    def test_by_zero_raises_value_error(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            divide(1, 0)\n"
                "\n"
                "    def test_ordinary_division(self):\n"
                "        self.assertEqual(divide(4, 2), 2)\n"
            )

            script = (
                "/files calc.py tests/test_calc.py\n"
                "Make tests/test_calc.py pass by fixing calc.py.\n"
                "y\n"   # apply the write/edit
                "y\n"   # run verification
                "y\n"   # possible repair edit
                "y\n"   # final verification
                "/quit\n"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "main.py")],
                cwd=project, input=script, capture_output=True, text=True, timeout=900,
            )
            combined = result.stdout + result.stderr

            # 1. Surplus `y` lines are now no-ops and a missing one declines
            #    cleanly, so this also regression-tests the old EOF crash.
            self.assertEqual(result.returncode, 0, combined)

            # 2. Real behaviour, not a grep of the source: the resulting code
            #    must actually pass its own suite.
            verify = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=project, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

            # 3. Verification really ran, using the discovered command.
            self.assertIn("run verification", combined)
            self.assertIn("unittest discover", combined)

            # 4. The single-repair bound holds against a real model.
            self.assertLessEqual(combined.count("VERIFICATION FAILED"), 1)
```

- [ ] **Step 2: Confirm the live test is correctly gated and does not run by default**

```bash
python3 -m unittest discover tests
```

Expected: green, and the live tests report as skipped (`set LOCALCODER_LIVE_TESTS=1 to run`). Total runtime still a few seconds.

- [ ] **Step 3: Run the full fast suite one final time**

Run: `python3 -m unittest discover tests -v`
Expected: PASS, every test, no skips other than `tests.test_live`.

- [ ] **Step 4: `py_compile` every module this phase created or touched**

```bash
python3 -m py_compile pathpolicy.py textfile.py verification.py \
                      actions.py gitsafety.py execution.py ui.py config.py main.py
```

Expected: silent (exit 0).

- [ ] **Step 5: Run the forbidden-pattern searches**

```bash
rg -n "git add -A"        --glob '!docs/**' --glob '!tests/**' .
rg -n "reset --hard"      --glob '!docs/**' .
rg -n 'errors="replace"'  actions.py textfile.py pathpolicy.py
rg -n "TODO|TBD|FIXME|XXX" pathpolicy.py textfile.py verification.py
```

Expected: **all four find nothing.** (`main.py:90`'s read-side `errors="replace"` fallback is out of scope and is not searched.)

- [ ] **Step 6: Confirm Ollama is up before the live run**

```bash
systemctl --user status ollama-tuned --no-pager || curl -s http://127.0.0.1:11434/api/tags | head -c 200
ollama list | grep qwen2.5-coder
```

Expected: the service is active (or the API answers) and `qwen2.5-coder:7b` is present. If not, start it (`systemctl --user start ollama-tuned`) and wait before continuing.

- [ ] **Step 7: Run the live test — generous timeout, no premature kill**

```bash
LOCALCODER_LIVE_TESTS=1 python3 -m unittest tests.test_live -v
```

**This is CPU-only inference: it will take many minutes — plausibly 20-40 for all three live cases on the reference machine, and the new case alone can take 15+.** Do not wrap it in an external `timeout`, do not background-and-kill it, and do not conclude it is hung from silence alone: `docs/LESSONS_LEARNED.md`'s "Piped stdout + a hard `timeout` can silently swallow real output" and "A 'hung' turn was actually slow cold prefill" both describe exactly this trap. Poll for completion with a long wait window and let it finish.

Expected: PASS, all three live cases including `test_verification_repairs_a_failing_test`.

If it fails, capture the full stdout/stderr and diagnose before changing anything — a live failure is usually either the model producing a differently-shaped block or a confirmation-order mismatch in the script, not a bug in the phase's Python.

- [ ] **Step 8: Commit**

```bash
git add tests/test_live.py
git commit -m "test: live end-to-end verification and single-repair case"
```

---

## Task 20: Final whole-branch review

**Model:** claude-opus-5-thinking-high (final whole-branch review)

**Files:**
- Modify: none. **This task is read-only.** If it finds a defect, it reports it; the fix is a new task with its own TDD cycle and its own reviewer.

**Interfaces:**
- Consumes: the full branch diff.
- Produces: a written verdict — approved, or a list of defects with file:line references.

- [ ] **Step 1: Read the whole branch diff against the merge base**

```bash
git log --oneline $(git merge-base HEAD main)..HEAD
git diff $(git merge-base HEAD main)..HEAD --stat
git diff $(git merge-base HEAD main)..HEAD
```

(If `main` is not the base branch, use whatever the branch was cut from; `git log --oneline -20` makes that obvious.)

- [ ] **Step 2: Check every security invariant from spec §8 against the code**

Confirm each of the ten invariants is enforced *in code*, not just tested:
1. one path gate for write/edit/delete; 2. y/N on every mutation, run, fetch, search and verification execution; 3. EOF is "no"; 4. model strings never become argv, argv never becomes a shell string, model strings still hit `is_denied`; 5. no model output in a verification argv; 6. no `git add -A`; 7. no `reset --hard`, no history rewrite, only `localcoder: ` commits; 8. process-group kill on timeout; 9. credential content never enters context; 10. CCE still optional everywhere.

- [ ] **Step 3: Check the model-call bound table (spec §7.8) against `run_turn`**

Confirm the repair phase is straight-line code with no loop around it, that `_apply_blocks`'s return value is discarded on the repair call, and that the final verification's result is never appended to any prompt. Confirm `MAX_REPAIR_HOPS = 1` is a `main.py` constant and not a config key.

- [ ] **Step 4: Check the non-goals were respected**

```bash
rg -n "requirements.txt|pip install|import requests|import numpy" --glob '!docs/**' .
git diff $(git merge-base HEAD main)..HEAD -- llm/prompts.py
rg -n "BASE_SYSTEM_PROMPT" --glob '!docs/**' .
```

Expected: no pip dependency of any kind; **`llm/prompts.py` shows no diff in this phase** beyond the baseline commit from Task 1 (spec §7.5 — the cached prefix must stay byte-identical).

- [ ] **Step 5: Re-run everything one last time**

```bash
python3 -m unittest discover tests
python3 -m py_compile pathpolicy.py textfile.py verification.py actions.py \
                      gitsafety.py execution.py ui.py config.py main.py
```

- [ ] **Step 6: Confirm the docs match the code**

Read `README.md`'s Actions table, "Git safety net" and "Verification and repair" sections, `docs/BACKLOG.md`'s two new "Done" entries, and `docs/LESSONS_LEARNED.md`'s two new sections. Every behaviour they describe must actually exist in the branch. Report any drift.

- [ ] **Step 7: Write the verdict**

Report: approved, or a numbered list of defects with `file:line` and the spec section each one violates.

---

## Model Assignment and Orchestration

### Assignment rationale

| Model | Tasks | Why |
|---|---|---|
| `composer-2.5-fast` | 2, 6, 9, 12, 13, 15 | Complete-spec, 1-2 file mechanical tasks: a new leaf module plus its test file, or a constant block plus its test. The implementation is fully specified in the task, with no cross-file judgement required. |
| `gpt-5.6-sol-medium` | 1, 3, 4, 5, 7, 8, 10, 11, 14, 18 | Multi-file integration, plus everything touching Git or process execution — `git revert` semantics, path-scoped staging, process-group kills, and the `run_turn`/`README`/`BACKLOG` fan-out. |
| `claude-opus-5-thinking-high` | 16, 17, 19, 20 | The `run_turn` lifecycle, KV-context threading, and the repair phase's structural bound; the live Ollama test; and the final whole-branch review. |

### Execution order

**All implementation tasks are strictly sequential: 1 → 2 → 3 → … → 20.** Every task after Task 2 shares at least one file or interface with a predecessor:

- Tasks 3, 7, 8, 10 all rewrite parts of `actions.py`.
- Tasks 4 and 5 both rewrite `gitsafety.py`, and Task 4 also edits `actions.py`.
- Tasks 12, 13, 14 all append to the same new `verification.py`.
- Tasks 10, 16, 17, 18 all edit `main.py`.

Never run two implementers against the same worktree at the same time. Do not "parallelize" Slice A and Slice B: **Task 11 must not start until Task 10's checkpoint (full suite green, both forbidden-pattern searches empty) passes.**

### Per-task protocol

For each task N:

1. **Dispatch a fresh implementer** with the assigned model. Give it the task's full text plus the Global Constraints section and the spec path. A fresh implementer sees only its own task, which is why every task's **Interfaces** block spells out the exact names and types its neighbours use.
2. The implementer completes every checkbox in order, ending with a commit.
3. **Dispatch a separate reviewer** — a different agent, never the implementer continuing. The reviewer is read-only: it runs `git show HEAD`, `python3 -m unittest discover tests`, and the task's own `py_compile` line, and checks the diff against the task's Files/Interfaces blocks and the Global Constraints.
4. If the reviewer rejects, dispatch a **new** implementer with the review findings. Do not resume the original.
5. Only after a task is approved does task N+1 begin.

Read-only reviews are the **only** work that may overlap: reviewing task N while nothing is being implemented is fine, but a review must not overlap with an implementer touching the same files.

### The live test's timing budget

Task 19 Step 7 is the only step that can take tens of minutes. Give it a wait window measured in tens of minutes, poll patiently, and do not kill it on silence — the reference machine is a CPU-only 2017 dual-core i5 and `docs/LESSONS_LEARNED.md` documents two separate incidents where an impatient kill produced a misleading diagnosis (an orphaned process and swallowed output).

---

## Self-Review

Run after writing the plan, before dispatching Task 1. Recorded here for the record.

### 1. Spec coverage

| Spec section | Requirement | Task |
|---|---|---|
| §1 goal 1 | No mutation of `.git/**` or credential files; `.gitignore`/`.github/` writable | 2, 3 |
| §1 goal 2 | Path-scoped staging, never `git add -A` | 4 |
| §1 goal 3 | Non-destructive `/undo` | 5 |
| §1 goal 4 | CRLF/BOM preserved, non-UTF-8 refused, diff on overwrite | 6, 7, 8 |
| §1 goal 5 | No crash on FS/path error, EOF, or timeout; no orphaned process group | 8, 9, 11 |
| §1 goal 6 | Malformed `edit` gets structured feedback | 10 |
| §1 goal 7 | Deterministic discovery incl. fallback and "unsupported" | 12 |
| §1 goal 8 | `argv` + `shell=False`; y/N per execution, no blanket approval | 11, 14 |
| §1 goal 9 | Pass = 0 model calls; fail = exactly 1 repair + 1 non-recursing final verification | 17 |
| §1 goal 10 | `/verify` uses discovery | 18 |
| §2 non-goals | No pip, no provider abstraction, no index/telemetry, no new blocks, prompt unchanged, no sandboxing, one repair hop | Global Constraints + Task 20 Step 4 |
| §6.1 | `PathDecision`, `resolve_for_mutation`, five ordered checks, exact strings | 2 |
| §6.2 | `TextFile`, `detect_eol`, `read`, `write`, `to_eol` | 6 |
| §6.3 | `MalformedEdit`/`extract_malformed_edits`; rewritten `apply_write`/`apply_edit`/`apply_delete` | 3, 7, 8, 10 |
| §6.4 | `commit_change(root, msg, paths)`; revert-based `undo_last` with all 8 steps | 4, 5 |
| §6.5 | `Status`, `CommandResult`, `run_shell`, `run_argv`, killpg, `apply_run` wrapper | 11 |
| §6.6 | `VerifyConfig`/`VerificationCommand`/`VerificationOutcome`, `discover` + promotion, `verify_project`, `strip_ansi`, `extract_failure_context`, `failure_feedback` | 12, 13, 14 |
| §6.7 | Three config keys + `verify_command` validation; `MAX_REPAIR_HOPS` not configurable | 15, 17 |
| §6.8 | `_append_result`, `_apply_blocks`, four turn locals, straight-line repair, `/verify`, BANNER, y/n no-op | 16, 17, 18 |
| §6.9 | `ui.confirm` EOF | 9 |
| §7.1-§7.9 | All resolved decisions | mapped above; §7.8's bound table is asserted in Task 17 and re-checked in Task 20 Step 3 |
| §8.1-§8.10 | Ten security invariants | enforced across 2-17; audited in Task 20 Step 2 |
| §9 | Every error-handling row | 3, 5, 7, 8, 11, 14, 17 |
| §10 | Every named test file and case | 2, 5, 6, 7, 8, 10, 11, 12, 13, 14, 17, 19 |
| §10 acceptance | Suite green and fast, `rg` checks, `py_compile`, docs updated in-commit | 19 Steps 3-5; docs folded into 3, 5, 7, 10, 14, 18 |
| §11 rollout | Slice A fully before Slice B | Task 10 checkpoint + Orchestration section |
| §12 risks | Mitigations (per-execution y/N, timeout, killpg, kill switch, one-hop bound) | 11, 14, 15, 17 |

No gaps found. Note one deliberate divergence from §11: the spec groups docs into steps 5 and 10, but this plan folds each README/LESSONS_LEARNED change into the task whose behaviour changes it describes (Tasks 3, 5, 7, 10, 14, 18), which satisfies §10's "updated in the same commits, not afterwards" more strictly than a separate docs step would.

### 2. Placeholder scan

Searched for `TBD`, `TODO`, `FIXME`, "implement later", "add appropriate error handling", "similar to Task N", and "write tests for the above". None present. Every code step carries a real code block; every test step carries real test code with real assertions; every "run this" step names the exact command and the exact expected result. The two places using `...unchanged...` (Tasks 3 and 8) point at a specific, currently-existing block of `actions.py` identified by line number in the Files section, and the surrounding replacement code is given in full.

One conditional branch is documented rather than left open: Task 18's Step 1 gives a concrete fallback (two named helpers, `_is_stray_confirmation` and `_run_verify_command`) if driving `main()` end-to-end proves brittle, with instructions to update the Interfaces block if that path is taken.

### 3. Type and signature consistency

Checked every name that crosses a task boundary:

- `PathDecision` fields `ok`/`path`/`relpath`/`reason`/`suggestion` — defined in Task 2, consumed identically in Tasks 3, 7, 8.
- `textfile.read/write/to_eol/detect_eol`, `TextFile.text/eol/bom/mixed_eol` — defined in Task 6, consumed identically in Tasks 7, 8. `write` is keyword-only for `bom` in both the definition and every call.
- `gitsafety.commit_change(project_root, message, paths)` — three arguments everywhere from Task 4 onward; Task 3 still uses the two-argument form and Task 4 updates all three call sites in the same commit.
- `execution.Status` members and `CommandResult` fields — defined in Task 11, constructed in Task 14 (`DECLINED`) and Task 12's test with the same field names and order.
- `VerificationOutcome(ran, command, result, skip_reason)` — positional construction in Tasks 12, 14 and the tests all match.
- `verification.discover(project_root, changed_paths, override_argv=None)` and `verify_project(project_root, changed_paths, cfg, *, confirm=True)` — same names in Tasks 12, 14, 17, 18.
- `failure_feedback(outcome)` — single argument, defined in Task 13, called in Task 17.
- `_append_result(task, current_task, results, max_total_context_chars, num_ctx, kv_context) -> (str, str, list[int] | None)` — same six parameters and same 3-tuple in Task 16's definition, Task 16's tests, and Task 17's two call sites.
- `_apply_blocks(output, project_root, cce, mutated) -> list[str]` — same four parameters in Task 16 and both Task 17 call sites.
- `run_turn(..., initial_kv_context=None, verify=None)` — the new parameter is last, so Task 16's and Task 10's existing positional calls stay valid; asserted by `test_verify_none_reproduces_todays_behaviour`.
- `MAX_REPAIR_HOPS` — one spelling, `main.py`, asserted in Task 17.

No mismatches found.
