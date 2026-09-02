# Backlog

Tracks what's done, what's open, and what's deliberately deferred (with the
reasoning), so decisions don't get re-litigated from scratch. Update this
alongside any non-trivial change -- move finished items to "Done", add new
findings to "Open", and only remove a "Deferred" item once its stated
trigger condition actually happens.

## Open

- **`qwen3:4b`'s `think: false` request parameter did not suppress
  reasoning output** on this Ollama version (0.33.2) -- measured directly
  (2026-09-02, `docs/BENCHMARKS.md`): a trivial "What is 2+2?" still
  produced a full `<think>...</think>` block, burning most of the call's
  wall time on hidden reasoning tokens. Not root-caused yet (model chat
  template vs. an Ollama bug, unclear). Blocks promoting `qwen3:4b` as an
  automatic `fast` profile default on weak hardware until resolved --
  `qwen2.5-coder:7b` is unaffected (no thinking-mode branch at all), so the
  actual default model is fine.

## Done

- **Ollama `context`-array reuse, within-turn and cross-turn**
  (`llm/ollama_client.py`, `agents/base.py`, `main.py`). Root cause
  confirmed via ollama/ollama#14780: the CPU backend never reuses KV cache
  across `/api/generate` calls unless the caller passes back the `context`
  array a previous call returned -- localcoder never did, by "fresh
  context every turn" design, so every turn re-prefilled the ~730-token
  system prompt from zero (measured ~290s, 2026-09-01). Now: (1) hop 2+ of
  a turn's follow-up loop always reuses hop 1's returned context and sends
  only the new delta, not the whole accumulated task text; (2) turns
  within the same REPL session optionally do the same
  (`reuse_context_across_turns` in config.json, default on), reset on
  `/tree`/`/files`/`/model`/`/agent` since those invalidate what's cached.
  `system` is omitted (not resent) on any context-carrying call --
  resending it risked duplicating it ahead of new tokens or breaking the
  prefix match the whole optimization depends on. This does **not** give
  the model conversation memory: bounded to "skip re-reading a prefix it
  already saw," which is why it doesn't conflict with the project's "fresh
  context each turn" design the way full memory would have. **Confirmed
  live** (2026-09-02, real localcoder session, real `qwen2.5-coder:7b`, see
  `docs/BENCHMARKS.md`): turn 2's prompt-eval time dropped 4.84x versus
  turn 1's cold prefill (354.9s -> 73.3s) despite having *more* tokens in
  context, not fewer -- the clear signature of cache reuse working, not
  just general noise. Total turn time dropped 3.95x (434.7s -> 110.0s).
- **Orphaned-generation detection** (`llm/busy.py`): pairs a host-scoped
  advisory lock file with `GET /api/ps` to warn explicitly ("Ollama may
  still be processing a request from an earlier session") instead of
  presenting a silent spinner indistinguishable from a normal slow turn.
  Does not fix the underlying Ollama/llama.cpp limitation below -- makes it
  legible instead.
- **`num_batch`/`num_thread` tuning, corrected from the original plan**:
  initially assumed these were `OLLAMA_NUM_BATCH`/`OLLAMA_NUM_THREAD` env
  vars (per several web write-ups) -- verified directly against this
  project's own Ollama 0.33.2 binary (`strings /usr/bin/ollama`) that
  neither env var exists at all. They're per-request `options` fields
  instead, the same mechanism `num_ctx` already uses. Confirmed working
  end-to-end by inspecting the actual `llama-server` process Ollama spawns:
  `-b 2048 -ub 2048 -t 2`, matching `config.json`'s `num_batch: 2048`,
  `num_thread: 2` exactly.
- **Hardware-tiered install** (`scripts/detect_hardware.py`,
  `scripts/install.sh`): detects physical cores/RAM/GPU, writes a
  `config.json` + Ollama tuning suited to the tier, symlinks (not copies)
  `~/.local/bin/ollama-serve-tuned` to the repo's script so it can't drift
  from what's actually installed the way it silently could before, and
  installs a real `localcoder` executable on PATH (replacing the
  bash-only `.bashrc` alias, which didn't work in non-interactive shells).
  Verified end-to-end on the reference machine: correctly detected
  `cpu-weak` (2 physical cores, 19.4GB RAM, no usable GPU), left the
  existing hand-written `config.json` untouched (idempotent), and the
  installed launcher runs cleanly.
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
  happens automatically on process exit) -- confirmed via web research
  (2026-09-02) to be a known, unsolved, industry-wide limitation, not
  something localcoder is uniquely missing. Mitigation: documented (see
  README "Troubleshooting") and now also actively detected and surfaced
  (`llm/busy.py`, above) instead of just documented after the fact. Revisit
  if a future Ollama version exposes a cancel endpoint or reliably honors
  client disconnects.
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
- **Conversation memory across turns**: each turn is still a fresh
  `generate()` call with no summary of prior turns handed to the model.
  Simple and predictable; adding real memory is a bigger design decision
  (what to keep, how to bound it) not worth taking on speculatively. Not
  the same thing as the context-array *prefill* reuse in "Done" above --
  that skips re-reading a cached prefix, it doesn't give the model any
  awareness of what was asked or answered before.
- **Orchestrated multi-agent chaining** (coder -> test -> refactor
  automatically): `agents/registry.py` supports running one sub-agent at a
  time via `/agent`, but no automatic chaining exists. Deferred until a
  concrete second MCP tool or workflow need is identified.
