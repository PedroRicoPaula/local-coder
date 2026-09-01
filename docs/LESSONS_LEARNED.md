# Lessons learned

Concrete bugs, near-misses, and testing gotchas hit during development,
with what actually explained or fixed them -- so the same investigation
doesn't have to happen twice. Add an entry whenever a bug turns out to have
a non-obvious cause, or a test setup turns out to hide a real failure mode.

## Piped stdout + a hard `timeout` can silently swallow real output

**Symptom**: an end-to-end smoke test (`printf ... | timeout 280 python3
main.py | tee out.txt`) produced a startup banner and nothing else -- no
sign the model ever responded, even though the same instruction worked
seconds later with a longer timeout.

**Cause**: Python fully block-buffers stdout when it isn't a TTY (piped
into `tee`), rather than line-buffering. If `timeout` kills the process
with SIGTERM before a buffer flushes, whatever was sitting in that buffer
is lost -- it was never actually a hang, just unflushed output plus a
timeout that was too short for CPU-only inference (see
`docs/BACKLOG.md`'s "genuine CPU-only prefill+generation time" note).

**Fix / takeaway**: when reproducing a "no output" report through a piped
command, set `PYTHONUNBUFFERED=1` (or redirect straight to a file instead
of `tee`) and use a generous timeout before concluding the program hung.
Also: `ui.py`'s own `print()` calls now pass `flush=True` explicitly, so
CLI chrome messages (spinner start line, info/warn/error/sub) can't get
stuck in a buffer even in a non-TTY context like a log file or CI run.

## CCE's real tool surface had to be confirmed from its own source, not assumed

**Symptom**: a design question ("can CCE compress arbitrary shell/fetch
output, not just files?") could have been guessed either way.

**Cause/resolution**: read `Context-Compress-Engine`'s Rust source directly
(`tools/mod.rs`, `compress.rs`, `symbol.rs`) rather than inferring from this
repo's Python wrapper alone. It advertises exactly two tools --
`compress_file` and `get_symbol` -- both requiring a real file path
resolved under `CCE_ROOT`. No generic text-compression tool exists or is
planned.

**Takeaway**: when a design decision depends on an external tool/service's
real capabilities, check the other side's actual source/API surface (here,
via `mcp/client.py`'s `tools/list` response, exposed as
`CCEClient.tool_names`) instead of assuming from this repo's usage of it.
`CCEClient.tool_names` was added specifically so this can be checked live,
not just at review time.

## "Available" and "alive" are different questions for a subprocess client

**Symptom**: `CCEClient.available` only ever checked whether an MCP client
object had been constructed at startup -- it would keep reporting "yes"
even after the underlying `context-compressor-mcp` subprocess crashed mid
-session, silently falling back to raw file reads (via the already-caught
`MCPError`) with zero visibility to the user.

**Fix**: added `MCPClient.is_alive()` (`self._proc.poll() is None`) and
`CCEClient.is_alive()`, re-checked once per REPL turn, with a one-time
visible warning the first time a previously-alive CCE process is found
dead.

**Takeaway**: for any subprocess- or connection-backed client, "was this
ever set up" and "is this still working right now" are different
properties -- a long-running interactive session needs the second one
checked periodically, not just once at startup.

## A network liveness probe needs the same headers a real request would use

**Symptom risk (caught in review, not production)**: an early draft of
`websearch.is_online()` sent a bare request with no `User-Agent`. Some
endpoints (DuckDuckGo included) can respond differently to requests that
look like a bot versus ones that look like a real client, which could make
a working connection look "offline" for the wrong reason.

**Fix**: `is_online()` sends the same `User-Agent: localcoder/1.0` header
`apply_search`/`apply_fetch` use for the real request, so the probe fails
for the same reasons a real call would, not different ones.

## A "hung" turn was actually slow cold prefill, compounded by an orphaned process from killing an earlier one

**Symptom** (reported 2026-09-01): running localcoder against a real repo
and asking a simple question, the spinner animated with elapsed time
climbing but no response ever appeared, even after a long wait.

**Investigation** (all measured directly, not guessed -- see the commands
in this session's history):
1. A bare pty-based reproduction confirmed the spinner itself works
   correctly (animates, correctly stops on real content) -- ruled out a UI
   bug.
2. `ps aux` / `top` revealed an orphaned `llama-server` process consuming
   ~150-200% CPU for tens of minutes -- traced to an earlier test session
   whose client process had been killed (`timeout`, `kill -9`) while a
   request was mid-flight. Ollama does not cancel the underlying
   computation when the client disconnects, and `OLLAMA_NUM_PARALLEL=1`
   means every later request queues behind it.
3. After clearing the orphan and re-testing on a fully clean system, a
   *fresh* (cold KV-cache) call still took ~290s of `prompt_eval_duration`
   to prefill just ~780 tokens (the fixed system prompt + a tiny task) --
   roughly 2.6 tokens/sec, dramatically slower than this hardware's
   generation speed (~1.6 tokens/sec) would suggest, and far slower than an
   earlier *warm* call in the same session (853 prompt tokens in 1.6s).
   The gap is very likely Ollama/llama.cpp reusing cached KV state for a
   repeated identical prompt prefix in the warm case, and paying full cost
   with no cache hit in the cold case.

**Conclusion**: not a bug in `main.py`/`ui.py` -- the code was accurately
reporting real, if very slow, progress. Two real, separate contributors:
(a) killing a client mid-request orphans server-side computation that then
blocks the next request, and (b) *cold* prompt prefill on this specific
CPU-only hardware is far slower than generation, making the fixed system
prompt's size matter more than it would on faster hardware.

**Fix / takeaway**: trimmed `BASE_SYSTEM_PROMPT` by ~40% (see
`docs/BACKLOG.md`) to cut the fixed per-turn prefill cost proportionally.
Documented the orphaned-process gotcha in README's new "Troubleshooting"
section so it's diagnosable (`ps aux | grep llama-server`) instead of
mistaken for a hang. When reproducing a "no response" report on this
project in the future: check for an orphaned `llama-server` *first*
(`top`/`ps`) before assuming the current session's request is the problem,
and always let a live test either finish naturally or accept that killing
it early will leave server-side work running.

## Ollama already returns real token/timing stats -- check before estimating

**Symptom**: this project's only context-budget signal used to be a flat
character cap (`max_total_context_chars`), unrelated to `num_ctx` (the
model's actual token window), with no way to know after a call how close a
prompt actually came to the real limit.

**Cause**: Ollama's streaming and non-streaming `/api/generate` responses
already include `prompt_eval_count`, `eval_count`, and duration fields on
their final chunk -- `llm/ollama_client.py`'s `generate()`/`generate_stream()`
were reading `response`/`thinking`/`done`/`error` and discarding everything
else.

**Takeaway**: before building an estimate (a tokenizer, a chars-per-token
heuristic) for something an API call already returns as ground truth,
check the full response shape first. The heuristic (`config.py`'s
`CHARS_PER_TOKEN`) is still needed for *pre-flight* budgeting (before a
call is made), but post-call accounting should always prefer the real
number when the API provides one.
