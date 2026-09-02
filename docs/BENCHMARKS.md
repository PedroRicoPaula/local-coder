# Benchmarks

Dated, append-only log of real measurements taken on this project's
reference machine (2017 Kaby Lake-U, 2 physical / 4 logical cores, 19.4GB
RAM, no usable GPU -- an old NVIDIA 920M with no driver installed). Written
down so a "tried it, no measurable difference" result doesn't get
re-litigated from scratch later -- same discipline as
`docs/LESSONS_LEARNED.md`. Use `scripts/bench_ollama.py` to reproduce.

## 2026-09-02: Ollama `context`-array reuse -- does it actually skip re-prefill?

**Setup**: raw `/api/generate` calls (not through localcoder yet), model
`qwen3:4b`, tiny synthetic prompts ("What is 2+2?" / "Now what is 3+3?"),
`think: false` requested explicitly.

**Finding 1 -- `think: false` did not suppress Qwen3's reasoning.** Both
calls returned a full `<think>...</think>` block despite the request
explicitly setting `"think": false`. This burned 162 and 184 `eval_count`
tokens respectively to answer "4" and "6" -- almost all of each call's
~100-160s wall time was spent generating hidden reasoning, not the prefill
this test was actually trying to isolate. **Caveat for workstream C** (model
tiering): `qwen3:4b` is not a clean drop-in "fast" profile candidate as-is
if its thinking mode can't be reliably disabled via the standard API
parameter on this Ollama version (0.33.2) -- needs follow-up before
promoting it as the default `fast` profile. Not yet root-caused (could be
this specific model's chat template not honoring the flag, or an Ollama
bug) -- flagging as open, not fixed.

**Finding 2 -- context reuse helped, but a THIRD call revealed a
confound: Ollama also does its own automatic recent-prompt caching,
independent of the explicit `context` array.**

| call | what | prompt_eval_count | prompt_eval_duration | effective tok/s |
|---|---|---|---|---|
| 1 | cold, no context | 37 | 8.98s | 4.1 |
| 2 | context=call 1's array, system omitted, new prompt appended | 216 | 8.02s | 26.9 (nominal -- see caveat below) |
| 3 | **identical prompt to call 1**, sent fresh, no context at all | 37 | **0.48s** | 77.1 |

Call 3 is the surprise: the exact same 37-token prompt as call 1, sent with
**no `context` parameter at all**, evaluated ~18x faster than call 1's cold
run. This means llama-server (via `--context-shift`, visible in its process
args) is already doing automatic prefix-matching against whatever is
currently sitting in its own KV cache slot from the *immediately preceding*
request on this model -- regardless of whether the client explicitly
threads `context` back. Call 2's explicit-context approach still beat a
fully-cold rate (26.9 vs 4.1 tok/s nominal), suggesting it recovered
*partial* cache reuse, but nowhere near call 3's near-perfect-match rate --
plausibly because system-prompt-omission and the manual context splice
didn't align token-for-token with what the server actually had cached, or
because `prompt_eval_count=216` folding in the "logical" full-context
count (not just new tokens) makes the "26.9 tok/s" figure itself not a
clean apples-to-apples number. **Not fully disentangled** -- three data
points on a noisy CPU-only box with a heavy thinking-mode confound isn't
enough to separate "explicit context reuse helped" from "it happened to
ride along on Ollama's own automatic caching regardless." Treat the
localcoder-level, `qwen2.5-coder:7b` result below (no thinking-mode
confound, the actual production code path) as the real verdict, not this
synthetic test.

## 2026-09-02: A2 cross-turn reuse, real localcoder session, `qwen2.5-coder:7b` -- CONFIRMED

**Setup**: real `localcoder` CLI (not synthetic), real production model
(`qwen2.5-coder:7b`, no thinking-mode confound), real `BASE_SYSTEM_PROMPT` +
project tree + pinned file context, two consecutive turns in one REPL
session, `ollama-tuned.service` freshly restarted first to guarantee a cold
KV cache. Writes declined (`n`) at the confirmation prompt so this only
measures turn latency, no side effects.

| turn | prompt tokens | prompt-eval duration | eval (response) tokens | eval duration | load duration | **total** |
|---|---|---|---|---|---|---|
| 1 (cold) | 666 | **354.9s** | 28 | 19.4s | 60.4s | 434.7s |
| 2 (same session, A2 active) | 881 | **73.3s** | 47 | 36.6s | 0.0s (already warm) | 110.0s |

**This is the real result the whole plan was built to produce.** Turn 2's
prompt-eval time dropped **4.84x** (354.9s -> 73.3s) despite its
`prompt_eval_count` being *larger* than turn 1's (881 vs 666, since it's a
cumulative count including the cached prefix) -- if no caching had
occurred, turn 2 should have taken *longer* to prefill than turn 1, not a
fifth of the time. Total turn time dropped 3.95x (434.7s -> 110.0s). The
"reaproveitamento de contexto Ollama ativo" info line fired exactly once,
right after turn 1, confirming `main.py`'s one-time-announcement logic
worked as designed. `num_batch=2048`/`num_thread=2` (see the entry below)
were also active for this run -- both effects are stacked together here,
not isolated from each other, but the *shape* of the result (prefill time
shrinking while token count grows) is specifically the context-reuse
signature, not a generic speedup.

This closes the `docs/BACKLOG.md` item -- promoted to Done with these
numbers rather than the earlier synthetic/ambiguous qwen3:4b micro-benchmark
above, which is now superseded as the deciding evidence (kept above for the
`think:false` finding, which is still an open, separate issue).

## 2026-09-02: `num_batch`/`num_thread` via request `options` -- confirmed end-to-end

Corrected mid-implementation: the original plan assumed
`OLLAMA_NUM_BATCH`/`OLLAMA_NUM_THREAD` were server env vars (per several web
write-ups) -- `strings /usr/bin/ollama` showed neither exists in this
project's actual Ollama 0.33.2 binary at all. Redirected to the real
mechanism (per-request `options`, same as `num_ctx`) and confirmed directly
by inspecting the live `llama-server` process Ollama spawned for the run
above:

```
llama-server ... -b 2048 -ub 2048 -t 2 ...
```

Matches `config.py`'s `num_batch: 2048`, `num_thread: 2` exactly -- the
client is correctly reaching the actual inference process, not silently
doing nothing the way the original env-var approach would have.
