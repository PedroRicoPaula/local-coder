# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run the full test suite (fast, no Ollama needed, ~0.5s):
```
python3 -m unittest discover tests
```

Run a single test file or a single test case:
```
python3 -m unittest tests.test_actions
python3 -m unittest tests.test_actions.TestExtraction.test_extract_write_basic
```

Run the one true end-to-end test (spawns `main.py` against a real running
Ollama and checks the resulting code actually works; slow -- CPU-only
inference takes minutes -- skipped by default):
```
LOCALCODER_LIVE_TESTS=1 python3 -m unittest tests.test_live
```

Run the CLI itself, from inside the target project you want it to operate
on (not from this repo):
```
python3 /path/to/localcoder/main.py
```

There is no build step, no linter, and no `pip install` -- this project is
stdlib-only Python 3.10+ by design (see "Zero pip dependencies" below).
`py_compile` is the closest thing to a lint check:
```
python3 -m py_compile <file>.py
```

## Architecture

**Zero pip dependencies is a load-bearing design constraint, not a
preference.** Every module (`ui.py`'s ANSI styling, `websearch.py`'s
DuckDuckGo scraping, `webfetch.py`'s HTML-to-text extraction, `mcp/client.py`'s
JSON-RPC transport) is hand-rolled against the stdlib. Before reaching for a
library, check whether the stdlib already covers it here -- it usually does,
on purpose.

**No JSON tool-calling.** `qwen2.5-coder:7b` was measured directly against
this Ollama install to not reliably emit Ollama's structured `tool_calls`
schema. Instead, the model is instructed (`llm/prompts.py`'s
`BASE_SYSTEM_PROMPT`) to emit plain-text fenced blocks (` ```write:path `,
` ```delete:path `, ` ```run `, ` ```fetch:url `, ` ```search:query `,
` ```symbol:path#name `, ` ```shell `), parsed deterministically by regex in
`actions.py` (variable-length fence: 3+ backticks, closed by the same
count). Any new agent capability should follow this same convention (new
block regex in `actions.py` + documented in `llm/prompts.py` + a handler in
`main.py`'s `run_turn`), not a tool-schema.

**The turn lifecycle spans several files working together**, orchestrated by
`main.py`:
1. `context/tree.py` + `context/cce_client.py` (or a raw-read fallback) +
   `knowledge/loader.py` assemble context under a char budget
   (`assemble_file_context` in `main.py`), returning both the context text
   and any truncation/skip warnings to surface to the human.
2. `llm/ollama_client.py`'s `generate_stream()` streams the response;
   `main.py`'s `stream_and_print()` displays it live with a spinner covering
   the pre-first-token gap (connection + prompt prefill), and returns real
   token/timing stats (`Usage`) that Ollama reports on its final chunk --
   don't reintroduce a char-based token estimate where this is available.
3. `actions.py` extracts every fenced block from the complete response;
   `main.py`'s `run_turn()` applies each one via its own module (`actions.py`
   for write/delete, `execution.py` for run, `webfetch.py` for fetch,
   `websearch.py` for search, `context/cce_client.py`'s `get_symbol` for
   symbol).
4. If `run`/`fetch`/`search`/`symbol` produced output, it's truncated
   (`context/truncate.py` -- CCE only compresses whole files/symbols, never
   arbitrary text, confirmed against its own Rust source) and fed back for
   up to `MAX_FOLLOWUP_TURNS` (2) more automatic hops, capped against the
   same context budget so accumulated hop results can't grow unboundedly
   (older hops are dropped before newer ones, never the reverse).

**CCE (Context-Compress-Engine) is an external Rust binary/repo**, not
vendored here -- `context/cce_client.py` is a thin MCP client wrapper
(`mcp/client.py`'s generic stdio JSON-RPC transport, reusable for any future
MCP server) around it. It exposes exactly two tools, `compress_file` and
`get_symbol` -- no generic text-compression tool exists or is planned, so
don't assume one when extending CCE usage. It must always degrade gracefully
to raw file reads when unavailable or when the subprocess dies mid-session
(`CCEClient.is_alive()`, re-checked once per turn) -- CCE failing is never
allowed to crash the CLI.

**Two context budgets used to be unrelated and are now reconciled**:
`num_ctx` (the real token window sent to Ollama) and
`max_total_context_chars` (`config.py`, a char cap on file-context assembly
only). `config.derive_max_context_chars()` derives the latter from the
former (`CHARS_PER_TOKEN`, `RESERVED_FRACTION`) whenever the user hasn't set
it explicitly in `config.json` -- don't reintroduce a flat,
num_ctx-independent default.

**Every human-facing message goes through `ui.py`**, not bare `print()` --
hand-rolled ANSI (`info`/`success`/`warn`/`error`/`sub`/`confirm`),
degrading to plain text under `NO_COLOR` or a non-TTY stdout. New
user-facing output should use these, not raw `print()`, to stay visually
consistent and to keep piped/test output deterministic. The model's own
streamed text is deliberately never styled (only the CLI's own chrome is),
so it can't collide with an escape-like sequence the model happens to emit.

**Every write/delete/run/fetch/search requires an explicit y/N confirmation**
(`ui.confirm`) and, for write/delete inside a git repo, is auto-committed
with a `localcoder: ` prefix (`gitsafety.py`) so `/undo` can revert it --
but only commits carrying that prefix, so a user's own work is never at
risk. `execution.py`'s command denylist and `context/denylist.py`'s
credential-file denylist are mitigation, not the real gate -- the
confirmation prompt is the actual gate, per OWASP's AI Agent Security
Cheat Sheet guidance for agentic CLIs.

**This hardware is often the actual bottleneck, not the code.** The
reference machine is CPU-only (2017 dual-core i5 class); a turn can take
tens of seconds to several minutes, worse with more context.
`scripts/ollama-serve-tuned.sh` + `ollama-tuned.service` hold the current
Ollama tuning (`num_ctx`, KV cache quantization, flash attention,
keep-alive). Before treating a slow turn as a bug, check how much context is
actually being sent (the per-turn token-usage bar) and compare against
`docs/LESSONS_LEARNED.md` and `docs/BACKLOG.md` for known performance
findings.

## Project docs

- `docs/BACKLOG.md` -- open items, known issues, and things deliberately
  deferred (with the reasoning for deferring them).
- `docs/LESSONS_LEARNED.md` -- concrete bugs/testing gotchas hit during
  development and what fixed or explained them, so they don't get
  re-investigated from scratch.
- `README.md` -- user-facing docs (what the CLI does, how to run it, the
  action-block table, security model).

Keep these updated as part of any non-trivial change, not as an afterthought
at the end of a session.
