# Backlog

Tracks what's done, what's open, and what's deliberately deferred (with the
reasoning), so decisions don't get re-litigated from scratch. Update this
alongside any non-trivial change -- move finished items to "Done", add new
findings to "Open", and only remove a "Deferred" item once its stated
trigger condition actually happens.

## Open

- **Ollama does not cancel a request server-side when the client
  disconnects mid-computation.** Confirmed directly (2026-09-01, see
  `docs/LESSONS_LEARNED.md`): killing/interrupting `main.py` while a prompt
  is being prefilled or generated leaves the underlying `llama-server`
  process running at ~150-200% CPU until it finishes on its own, and since
  `OLLAMA_NUM_PARALLEL=1` every subsequent request queues behind it --
  making an unrelated, later "simple" prompt look hung when it's actually
  just waiting its turn behind an orphaned computation from an earlier
  interrupted session. This is Ollama/llama.cpp behavior, not something
  fixable from the Python client beyond closing the socket (which already
  happens automatically on process exit). Mitigation for now:
  document it (done -- see README "Troubleshooting") so a slow turn can be
  diagnosed (`ps aux | grep llama-server` / `ollama ps`) instead of assumed
  broken. Revisit if a future Ollama version exposes a cancel endpoint or
  reliably honors client disconnects.
- **Consider reusing Ollama's `context` array across hops/turns within a
  session** to avoid re-prefilling the fixed system prompt from scratch
  every time. Measured (2026-09-01): cold prefill of just the ~730-token
  system prompt took ~290s on the reference CPU-only hardware -- generation
  itself is comparatively fast per token but prefill dominates wall-clock
  time for short answers. This conflicts with the project's deliberate "no
  conversation memory, fresh context each turn" simplicity (see README);
  worth a real design discussion (what to cache, how to bound it, whether
  it still holds for the "no conversation memory" turn model) before
  building, not a speculative change.

## Done

- **Visual overhaul + liveness feedback** (`ui.py`): hand-rolled ANSI
  colors, a spinner with elapsed-time counter covering the pre-first-token
  gap, migrated ~47 `print()` call sites to `ui.info/success/warn/error/sub`.
- **Real token/context-limit visibility**: `Usage`/`GenerationResult`
  capture Ollama's own `prompt_eval_count`/`eval_count`/durations (was
  previously read off the wire and discarded); a colored per-turn usage bar
  and a near-`num_ctx` warning were added.
- **Reconciled context budgets**: `max_total_context_chars` is now derived
  from `num_ctx` (`config.derive_max_context_chars`) instead of an
  unrelated flat default that could exceed the real token window.
- **Web search**: `websearch.py`, DuckDuckGo HTML scraping, no API key,
  `is_online()` pre-check, `` ```search `` action block + `/search` command.
  Verified against the live internet, not just synthetic HTML.
- **CCE liveness + `get_symbol` wiring**: `CCEClient.is_alive()` re-checked
  per turn (was only checked once at startup); `` ```symbol `` action block
  wires the previously-implemented-but-unused `get_symbol` into the
  follow-up loop; `context/truncate.py` heuristically truncates
  `run`/`fetch`/`search` output that CCE cannot compress.
- **Anti-hallucination transparency**: file truncation/skip events during
  context assembly are now surfaced as visible warnings (were previously
  inline markers only the model would see).
- **System prompt trimmed** (`llm/prompts.py`'s `BASE_SYSTEM_PROMPT`,
  2336 -> 1403 chars, ~40% cut): same action-block instructions and the
  critical four-backtick nested-fence example preserved, prose tightened.
  Directly reduces the fixed per-turn prefill cost every turn pays (see
  "Ollama does not cancel..." below for why prefill cost matters so much on
  this hardware). Verified against `tests.test_live` after trimming --
  still correctly produces a working fix, not just shorter text.

## Deferred (with reasoning -- also documented in README.md)

- **Multi-context / subagent chunking**: splitting a large task across
  multiple model calls. No metered per-token cost on local hardware to
  justify the added complexity yet -- revisit only once a real task
  actually hits the context limit in practice.
- **Conversation memory across turns**: each turn is a fresh `generate()`
  call. Simple and predictable; adding memory is a bigger design decision
  (what to keep, how to bound it) not worth taking on speculatively.
- **Orchestrated multi-agent chaining** (coder -> test -> refactor
  automatically): `agents/registry.py` supports running one sub-agent at a
  time via `/agent`, but no automatic chaining exists. Deferred until a
  concrete second MCP tool or workflow need is identified.
