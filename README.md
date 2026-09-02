# localcoder

A 100% local, offline coding CLI: `qwen2.5-coder:7b` via Ollama, with the
[Context-Compress-Engine](https://github.com/PedroRicoPaula/Context-Compress-Engine)
doing context compression before anything reaches the model. Zero pip
dependencies (stdlib only) — nothing here needs internet access at run time.

## Quick start (Omarchy / any Linux with Ollama installed)

```bash
git clone https://github.com/PedroRicoPaula/local-coder.git
cd local-coder

# 1. Ollama must already be installed (https://ollama.com) and the model
# pulled -- install.sh doesn't do either, it only configures what's there.
ollama pull qwen2.5-coder:7b     # skip if you already have it

# 2. Detects this machine's CPU/RAM/GPU, writes a config.json tuned for it,
# installs scripts/ollama-serve-tuned.sh as a systemd --user service (see
# "Ollama tuning" below for why a service, not a backgrounded script), and
# puts a `localcoder` launcher on PATH. Safe to re-run -- never overwrites
# an existing config.json. Linux + systemd only (see docs/BACKLOG.md); on
# anything else, run the steps inside scripts/install.sh by hand.
./scripts/install.sh

# 3. Build the CCE binary once (optional but recommended -- see below)
git clone https://github.com/PedroRicoPaula/Context-Compress-Engine.git ../Context-Compress-Engine
cd ../Context-Compress-Engine
cargo build --release
cp target/release/context-compressor-mcp ~/.cargo/bin/
cd -

# 4. Run localcoder from inside the project you want to work on
cd /path/to/your/project
localcoder
```

## What it does each turn

1. Builds a compact tree of the current directory (`.gitignore`-aware).
2. Compresses whichever files are in context through CCE (heuristic,
   near-instant, no LLM involved in this step) — falls back to raw file
   reads if the CCE binary isn't built yet. Any file that gets truncated or
   skipped to fit the context budget prints a visible warning (not just an
   inline marker only the model would see) -- see "Token usage & context
   budget" below.
3. Injects any `.md` rule files from `skills/` into the system prompt.
4. Streams the response from Ollama live, token by token, instead of
   sitting on a silent wait. A spinner covers the gap before the first
   token too -- connection + prompt prefill, the part that used to be pure
   silence -- and each response ends with a colored token-usage bar (see
   "Streaming" and "Token usage & context budget" below).
5. Parses the complete reply for action blocks (write, delete, run, fetch,
   search, symbol, or a display-only shell suggestion) and applies each
   one, individually confirmed -- see "Actions" below.
6. If a `run`, `fetch`, `search`, or `symbol` produced output, feeds it back
   for up to two more automatic turns so the model can act on what it
   learned (install a dependency, then use it; read a page, then write
   against it; look up a search result, then use it; ask for an elided
   function body, then read it) -- bounded, not an open-ended agent loop,
   because each hop costs real CPU-minutes. Each hop's output is truncated
   heuristically (CCE has no generic text-compression tool, only
   file/symbol-shaped ones) and the accumulated follow-up context is capped
   against the same budget, dropping older hop results before newer ones
   rather than growing unboundedly.

## Streaming

`llm/ollama_client.py`'s `generate_stream()` uses Ollama's native
`stream: true` and prints fragments as they arrive (`main.py`'s
`stream_and_print`). One thing this surfaced directly: `think: true`
doesn't just get ignored by a model with no extended-thinking template --
Ollama rejects the request outright with HTTP 400 ("does not support
thinking"). `qwen2.5-coder` has no `<think>` branch in its chat template
(unlike `qwen3`), so `think` stays `False` here; the value of streaming for
this model is watching real response tokens arrive, not a distinct
reasoning trace.

Before the first fragment arrives -- the connection plus Ollama's prompt
prefill, which on this hardware can be the slowest part of a turn -- a
spinner (`ui.Spinner`) animates with an elapsed-time counter, so the silent
"is it hung?" gap that used to exist between hitting Enter and the first
token is now visibly alive. It stops the instant real content starts
streaming.

## Context reuse (skipping repeated prefill)

Every turn used to re-prefill the full system prompt (and tree, if
`/files`/`/tree` hadn't run since) from zero, even though that text is
identical turn to turn -- measured at ~290s just for the system prompt on
this hardware (`docs/LESSONS_LEARNED.md`). Ollama has a confirmed CPU
backend bug where KV cache is otherwise never reused across `/api/generate`
calls unless the caller passes back the `context` token array a previous
call returned (ollama/ollama#14780) -- so that's what `main.py`/
`llm/ollama_client.py` now do, in two places:

- **Within one turn's follow-up hops** (`run_turn`'s loop, up to
  `MAX_FOLLOWUP_TURNS`): always on, no design tradeoff -- hop 2 only sends
  the new "RESULT OF YOUR LAST ACTION" delta, not the whole accumulated
  task text, since that's already covered by hop 1's cached context.
- **Across turns in the same REPL session**: opt-out via
  `"reuse_context_across_turns": false` in `config.json` (default `true`).
  Reset automatically on `/tree`, `/files`, `/model`, and `/agent
  <name>` — anything that changes what's cached or switches to a different
  system prompt invalidates the old context array, so it's dropped rather
  than resent stale.

Either way, `system` is omitted (not resent) on any call carrying a cached
`context` -- it's already baked in from the call that produced it;
resending it on top risks duplicating it ahead of the new prompt tokens or
breaking the prefix match the whole optimization depends on
(`agents/base.py`'s `_system_for`). See `docs/BENCHMARKS.md` for this
machine's own measured before/after numbers, not just the theory.

## Terminal styling

`ui.py` is a hand-rolled ANSI styling module -- no `rich`/`colorama`, stdlib
only, matching this project's zero-pip-dependency design. It degrades to
plain, uncolored text automatically when `NO_COLOR` is set or stdout isn't
a real terminal (piped output, tests), so nothing here can corrupt a
redirected or non-interactive run. Only the CLI's own messages (info,
warnings, errors, the token-usage bar) get color; the model's own streamed
text is never styled, so it can't collide with a literal escape-like
sequence the model happens to emit.

## Token usage & context budget

Ollama reports real token counts and timings on the final chunk of every
request (`prompt_eval_count`, `eval_count`, plus load/prompt-eval/eval
durations) -- `llm/ollama_client.py`'s `Usage` dataclass captures them
instead of discarding them, and every turn ends with a colored bar:

```
[############------------] 3600/8192 tokens (44%) -- prompt 3120 + resposta 480
```

Green under 60% of `num_ctx`, yellow 60-85%, red above 85%. If a turn's
`prompt_eval_count` gets within ~2% of `num_ctx`, a warning prints directly:
Ollama may have silently dropped the oldest part of the prompt to fit,
meaning that turn's answer could be based on truncated context -- this is
the most direct anti-hallucination signal this project has, since it comes
from Ollama's own accounting rather than a guess.

Two previously unrelated budgets are now reconciled: `num_ctx` (the real
token window sent to Ollama) and `max_total_context_chars` (the char cap on
file-context assembly). `config.py`'s `derive_max_context_chars()` derives
a safe char budget from `num_ctx` (`~3.2 chars/token`, `65%` of the window
reserved for file context, the rest held back for system prompt/tree/
skills/task text) whenever `max_total_context_chars` isn't set explicitly
in `config.json` -- at the default `num_ctx=8192` this derives ~17800
chars, well under the old flat `60000` default, which was already ~3.5x
past what the real token window could safely hold. An explicit override in
`config.json` is still respected as-is, but a startup warning fires if it
exceeds the derived safe cap.

## Actions

Seven fenced-block kinds, all sharing the same variable-length-fence
convention (`actions.py`):

| Block | Effect | Confirmed? | Fed back to the model? |
|---|---|---|---|
| ` ```write:path ` | create/replace a file | yes, y/N | no |
| ` ```delete:path ` | remove a file | yes, y/N | no |
| ` ```run ` | execute a shell command | yes, y/N, plus a denylist that refuses catastrophic patterns without even prompting | yes, up to 2 hops |
| ` ```fetch:url ` | fetch a web page as text | yes, y/N, http(s) only, skipped immediately if offline | yes, up to 2 hops |
| ` ```search:query ` | search the web (DuckDuckGo, best-effort) | yes, y/N, skipped immediately if offline | yes, up to 2 hops |
| ` ```symbol:path#name ` | ask CCE for one elided function/class's full body | no (read-only, local) | yes, up to 2 hops |
| ` ```shell ` | show a command, don't run it | no execution at all | no |

`run`, `fetch`, and `search` are the only things that touch anything
outside the project directory or the local machine (`fetch`/`search` are
also the only two that need the network -- see "Why fetch and search break
the offline claim" below). `execution.py`'s denylist (`rm -rf`, `mkfs`,
`dd if=`, a fork bomb pattern, `shutdown`/`reboot`, writing to a raw block
device, `sudo`) is mitigation, not a promise, same spirit as
`context/denylist.py` — everything *not* on that list still needs an
explicit y/N, which is the real gate, matching OWASP's AI Agent Security
Cheat Sheet guidance for agentic CLIs: never blanket-grant execution,
always require approval for anything with real-world effect.

## Web search

`websearch.py` scrapes DuckDuckGo's HTML-only endpoint
(`html.duckduckgo.com/html/`) with a hand-rolled stdlib `HTMLParser` --
no API key, matching this project's zero-pip-dependency and zero-setup
philosophy. `is_online()` is a cheap 2-second-timeout probe against that
one endpoint (not a general internet check) used to skip the confirmation
prompt entirely when a fetch/search is already known to be doomed, and to
print an online/offline line in the startup banner. This is explicitly
**best-effort, not a guarantee**: DuckDuckGo's markup or bot-detection
heuristics can change with no notice and silently break the scraper --
same spirit as `webfetch.py`'s HTML-to-text stripping and `security.py`'s
secret-pattern scan. `/search <query>` in the REPL runs the same search
without a confirmation prompt (you already typed the command) and prints
results directly to the terminal rather than feeding them into the model's
context automatically.

## Why `fetch` and `search` break the offline claim

Every other action stays local. `fetch`/`search` need a real HTTP request
to wherever the model (or you, via `/search`) points them, which is a
deliberate, confirmed exception to "100% offline" -- made because being
able to read a page or look something up was worth more than the purity of
the claim. Both are opt-in per call (never automatic, always a y/N with the
URL or query shown first when the model requests them), so nothing reaches
the network without a human seeing what's being sent and saying yes -- and
both are skipped outright, with no prompt at all, when localcoder detects
it's offline.

## Git safety net

If the current directory is a git repo, every confirmed `write`/`delete` is
auto-committed with a `localcoder: ` prefixed message (`gitsafety.py`).
`/undo` reverts the last commit -- but only if its message has that prefix,
so it can never discard a commit that was actually your own work, and it
refuses (rather than doing something more elaborate) if that commit happens
to be the repository's very first. Outside a git repo, the y/N prompt at
write/delete time is the only safety net there is -- `git init` first if you
want `/undo` available.

## Repo layout

```
main.py               entry point / REPL, run_turn's streaming + follow-up loop
ui.py                   hand-rolled ANSI styling: colors, spinner, token-usage bar
config.py, config.json    model, host, timeouts, budgets (max_total_context_chars
                          derived from num_ctx unless set explicitly)
context/
  tree.py                project tree walker
  cce_client.py            MCP client wrapping the CCE binary
  denylist.py               credential/key files, never sent as context
  truncate.py                heuristic head+tail truncation for text CCE can't compress
llm/
  ollama_client.py          HTTP client (urllib, stdlib only), generate + generate_stream,
                            Usage/GenerationResult (real token counts from Ollama),
                            context-array reuse, num_batch/num_thread options
  busy.py                    orphaned-generation detection (advisory lock + GET /api/ps)
  prompts.py                 system/user prompt assembly
knowledge/loader.py           loads skills/*.md into the system prompt
skills/                        <- put your own stack rules here as .md files
agents/
  base.py                    Agent base class (run + run_stream)
  coder.py                    the default agent driving the REPL
  test_agent.py, refactor_agent.py   narrow-purpose sub-agents (stubs)
  registry.py                 name -> agent lookup
mcp/
  client.py                  generic stdio MCP client (JSON-RPC), reusable
                              for any future MCP server, not just CCE
mcp.servers.json               MCP server list (context-compressor pre-wired)
actions.py                      parses all seven action blocks, applies write/delete
execution.py                     runs ```run blocks (denylist + confirm + capture)
webfetch.py                      fetches ```fetch blocks (confirm + HTML-to-text)
websearch.py                      DuckDuckGo HTML scrape for ```search blocks + /search
gitsafety.py                      auto-commit on confirmed changes, /undo
security.py                       secret-shaped-string scan on generated content
scripts/
  ollama-serve-tuned.sh              tuned Ollama launch script (source of truth --
                                      install.sh symlinks ~/.local/bin/ to this, not a copy)
  ollama-tuned.service                 systemd --user unit for the above
  detect_hardware.py                    CPU/RAM/GPU detection -> tier, stdlib only
  install.sh                              hardware-tiered setup: config.json, systemd
                                          service, `localcoder` launcher on PATH
  bench_ollama.py                         real tok/s + prefill benchmarking across models
docs/
  BACKLOG.md                              open/done/deferred work, with reasoning
  LESSONS_LEARNED.md                       concrete debugging post-mortems
  BENCHMARKS.md                             dated, real measurements (not guesses)
tests/                              unittest suite, see "Testing" below
```

## Why no JSON tool-calling

Ollama's OpenAI-compatible endpoint asks the model to wrap tool calls in
`<tool_call>...</tool_call>` tags so it can parse them into a structured
`tool_calls` array. Measured directly against this Ollama install:
`qwen2.5-coder:7b` reliably ignores that wrapper and emits the JSON as plain
text instead, which breaks structured tool-calling end to end. `qwen3`
models were more consistent about it, but since the spec asked for
`qwen2.5-coder:7b` specifically, this CLI sidesteps the whole problem: the
model is asked to emit ` ```write:path ` fenced blocks, which are parsed
with a plain regex (`actions.py`). No tool schema to get right, no silent
failure mode — either the block is there or it isn't, and you see the raw
text either way.

The parser supports a variable-length fence (` ```` ` for a file whose own
content has a ` ``` ` in it, e.g. a README) via a regex backreference, and
the system prompt tells the model to use it. Measured directly: `7b` doesn't
reliably follow that instruction either -- it still opens with three
backticks even when the content has its own fence inside. The parser itself
is correct (verified: a well-formed four-backtick block parses intact); what
isn't guaranteed is the model choosing to emit one. When it doesn't, the
write still comes out clean (whatever content came before the first inner
fence), it just won't contain what came after -- no corruption, just an
incomplete file. Same lesson as the tool-calling one above: a 7B model
follows a plain textual convention more reliably than a schema, but "more
reliably" isn't "always."

## Ollama tuning (`scripts/ollama-serve-tuned.sh` + `ollama-tuned.service`)

Measured on this machine: Intel i5-7200U, 2 cores/4 threads, no GPU -- that
CPU is the dominant, unfixable cost. RAM is a separate story: an earlier pass
assumed 11GB total and tuned accordingly (`num_ctx` capped at 4096, two
models deleted to save disk). Ollama's own startup log later showed the real
number -- `total="19.4 GiB" available="15.1 GiB"` -- an 11GB reading that was
either stale or measured under some constraint that no longer applies.
Flagging the discrepancy rather than quietly re-tuning around it: the table
below reflects the *measured* 19GB reality, not the original assumption.

**Run it as a service, not a backgrounded script.** `ollama-serve-tuned &`
in a terminal died the moment that terminal closed, and its `llama-server`
inference child orphaned instead of dying with it -- kept running, pegged at
high CPU, completely unreachable, until something noticed and killed it by
hand. `ollama-tuned.service` (installed via the Quick Start above) wraps the
same script in systemd --user with `Restart=on-failure` and
`KillMode=control-group`, so it survives a closed terminal and takes its
child down with it if it ever does die. Manage it with:

```bash
systemctl --user status ollama-tuned    # is it up?
systemctl --user restart ollama-tuned   # picked up a config change
systemctl --user stop ollama-tuned      # done for now
```

| Setting | Value | Why |
|---|---|---|
| `num_ctx` (in `config.json`) | 8192 | Ollama's own CPU-path default is a flat 4096 regardless of system RAM -- it doesn't auto-scale with RAM the way GPU/VRAM sizing does (confirmed in its own log: `default_num_ctx=4096` even with 15GB free). With 19GB actually available, doubling this buys real headroom for longer files/instructions without truncation, at an affordable RAM and prefill-time cost. |
| `request_timeout_s` (in `config.json`) | 600 | Raised from 300: a real task should get the time it needs to actually finish, not be cut off mid-response for the sake of a tighter budget this hardware doesn't need. |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Roughly halves KV-cache RAM; needs flash attention, which is on below. |
| `OLLAMA_FLASH_ATTENTION` | `1` | Forced rather than left on "auto" — auto-detection is a reasonable default in general, but on a 2017 CPU it's worth confirming rather than assuming. |
| `OLLAMA_KEEP_ALIVE` | `30m` | Avoids paying the ~15–20s cold-load cost between turns during a work session. |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Never hold two models in RAM — matches the earlier finding that swapping between models mid-session is what caused request pile-ups. |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time; this CPU cannot usefully serve concurrent ones anyway (measured: concurrent requests just queue and each pays the full serial cost -- running localcoder in two terminals at once means the second one waits for the first, not a bug, just serial hardware). |

`num_batch`/`num_thread` (`config.json`, defaults `2048`/`2`) are **not**
env vars, unlike everything in the table above -- verified directly against
this project's own Ollama 0.33.2 binary (`strings /usr/bin/ollama` shows no
`OLLAMA_NUM_BATCH`/`OLLAMA_NUM_THREAD` at all). They're per-request
`options` fields instead, the same mechanism `num_ctx` already uses
(`llm/ollama_client.py`'s `OllamaClient`) -- confirmed end-to-end by
inspecting the actual `llama-server` process Ollama spawns: `-b 2048 -ub
2048 -t 2`, matching the configured values exactly. `num_thread` is
machine-specific (this machine's 2 physical cores) -- override it in
`config.json` if you're not on this exact hardware, or set it to `null` to
let Ollama pick its own default. `scripts/install.sh` sets both
automatically based on `scripts/detect_hardware.py`'s reading of the
machine it's run on.

Two models beyond `qwen2.5-coder:7b` are worth keeping installed:
`qwen3:4b`/`qwen3:8b` are the only ones on this machine confirmed to emit
Ollama's structured `tool_calls` format reliably, if you ever build something
that needs that instead of the `` ```write `` convention. `qwen2.5-coder:14b`
and the redundant `qwen-claude:latest` alias were removed when RAM was
believed to be the binding constraint; worth reconsidering the 14b now that
the real RAM figure is known, though the CPU -- unchanged -- is still the
harder limit for a model that size.

## Model selection

`config.json`'s `model` key (if set) always wins. Otherwise, two profiles
exist in `config.py`'s `model_profiles` (`quality` = `qwen2.5-coder:7b`,
the default; `fast` = `qwen3:4b`) -- pick one at startup with
`--profile fast`/`--profile quality`, or override the model directly with
`--model <name>` (wins over `--profile` too). Switch mid-session with
`/model <name>`, which also resets any cached context (see "Context reuse"
above), since a different model invalidates it.

**`qwen3:4b` is not yet a verified `fast` default** -- measured directly
(`docs/BENCHMARKS.md`, 2026-09-02): its `think: false` request parameter
did not suppress reasoning output on this Ollama version, burning most of
a call's time on a hidden `<think>` block even for a trivial question. It
stays available as an opt-in profile, but promoting it as the automatic
choice on weak hardware needs that resolved first -- `qwen2.5-coder:7b`
has no thinking-mode branch at all, so this doesn't affect the default.

## Security

- **Credential files never enter the model's context.** `context/denylist.py`
  (ported from CCE's own `denylist.rs`, prefix-family matching so `.env.local`
  and `id_ecdsa` are caught, not just `.env` and `id_rsa`) is checked both for
  auto-selected files and anything passed to `/files` explicitly — a denied
  path is refused with a visible message, not silently dropped.
- **Every file write requires a y/N confirmation** (`actions.py`); nothing is
  written without it, and a write path is checked against the project root
  before that prompt even appears (no `../../etc/passwd` via a crafted
  `write:` block).
- **Shell commands are never executed**, only ever printed as a suggestion —
  matching the standard guidance for agentic CLIs (OWASP's AI Agent Security
  Cheat Sheet: allowlist tools, never grant blanket shell access, require
  approval for high-impact actions). There is no code path in this project
  that runs a shell command the model proposed.
- **`security.py` scans generated file content for secret-shaped strings**
  (AWS keys, PEM headers, `sk-`/`ghp_`-style tokens, `key = "..."` patterns)
  before the write confirmation prompt, and flags a match inline. This is a
  heuristic warning, not a filter — it will miss things and occasionally
  flag something harmless. It exists so a hallucinated or copied credential
  gets a human's eyes on it before disk, not to replace that judgment.

None of this defends against a determined attacker; it defends against the
ordinary failure modes of pointing a local model at your files — it echoing
something it shouldn't have seen, or a fenced block landing somewhere you
didn't mean.

## Testing

```bash
python3 -m unittest discover tests
```

Pure-logic tests (denylist, secret-pattern scan, action-block parsing
including the nested-fence case, tree sorting/filtering, config merging,
the command denylist, git commit/undo) run in well under a second, no
Ollama or network needed. The one true end-to-end test is opt-in and slow
for the same reason everything on this hardware is slow:

```bash
LOCALCODER_LIVE_TESTS=1 python3 -m unittest tests.test_live
```

It spawns `main.py` for real against a real running Ollama, feeds it an
actual bug (`ZeroDivisionError` → should become a clear `ValueError`), and
checks the *behavior* of the resulting code (imports it and calls the
function) rather than grepping the source text for a particular phrasing.

## Deferred: multi-context / subagent chunking

Explicitly not built: splitting a large task across multiple model calls
(one per chunk of files, then a synthesis call to combine them) or routing
sub-tasks to a separate subagent to save time. On fully local, CPU-only
hardware there's no metered cost to save by doing this — no per-token
billing — so the only thing subagent chunking would trade is *more*
multi-minute calls for *maybe* better coverage of a task too big for one
context window. Deferred until a real task actually hits that limit,
matching CCE's own "gated on evidence, not guesses" approach to its V2.

## Multi-agent / MCP: what's real vs. scaffold

- **Real and working:** `mcp/client.py` is a generic stdio MCP client;
  `context/cce_client.py` uses it against the CCE binary today, including
  `get_symbol` (wired to the ```symbol action block, see "Actions" above --
  no longer implemented-but-unused). `agents/registry.py` + `test`/
  `refactor` agents work now via `/agent test <task>` — same model,
  narrower system prompt.
- **Scaffold, not wired up:** there is no orchestrator that automatically
  chains coder → test → refactor, and no Playwright/DB MCP server is
  connected. `mcp.servers.json` and `mcp/client.py` are shaped so adding one
  is "add an entry + a thin wrapper like `cce_client.py`", not a rewrite.
  This was deliberately left unbuilt rather than guessed at — wire it up
  once you know which second tool you actually want.

## Troubleshooting: a turn that seems to take forever

The spinner (`ui.Spinner`, see "Streaming" above) is the ground truth for
"is it working" -- it only stops once real content starts arriving from
Ollama. If it's animating with elapsed time climbing, localcoder is not
hung; the question is what's actually slow. Measured directly (2026-09-01,
see `docs/LESSONS_LEARNED.md` for the full investigation):

1. **Cold prompt prefill is much slower than token generation on CPU-only
   hardware.** A *fresh* call (no matching cached prompt prefix) can spend
   several minutes just processing the system prompt + task before
   generating a single response token -- measured at roughly 2.6 tokens/sec
   prefill on the reference machine, versus ~1.6 tokens/sec generation.
   This is why `BASE_SYSTEM_PROMPT` (`llm/prompts.py`) is kept as short as
   it can be while still reliably teaching the action-block convention --
   every character in it is paid for on every single turn. A *warm* call
   (Ollama reusing a cached KV state for a repeated prompt prefix) can be
   dramatically faster for the same token count -- so turnaround time can
   vary a lot turn to turn on this hardware, and that's expected, not a
   regression.
2. **If a turn seems to hang far longer than the token-usage bar and
   elapsed counter would justify, check for an orphaned Ollama process
   first, before assuming something is broken:**
   ```bash
   ps aux | grep llama-server
   ```
   Ollama does not cancel the underlying computation when a client
   disconnects mid-request (Ctrl+C at the wrong moment, a killed process, a
   closed terminal) -- the `llama-server` subprocess keeps computing at
   ~150-200% CPU until it finishes on its own, and with `OLLAMA_NUM_PARALLEL=1`
   (see "Ollama tuning" below), *every later request queues behind it*,
   including an unrelated, trivial one in a brand-new session. localcoder
   now checks for exactly this before sending a request (`llm/busy.py`: a
   host-scoped advisory lock file, `~/.cache/localcoder/inflight.lock`,
   paired with `GET /api/ps`) and prints a warning naming it instead of
   just sitting on a silent spinner -- but the underlying wait is still
   real, this only makes it legible. If it happens, kill the orphaned
   process (`kill <pid>`) or `systemctl --user restart ollama-tuned` to
   clear it -- the next request will pay a fresh model-load cost
   (`OLLAMA_KEEP_ALIVE`, ~15-20s) but won't be stuck behind the old one.

## Limitations (measured on this hardware, not assumed)

- **This machine is CPU-only**: Intel i5-7200U, 2 cores/4 threads, no
  dedicated GPU. Expect tens of seconds to several minutes per turn, worse
  for longer prompts. `request_timeout_s` in `config.json` defaults to 300s
  for this reason. See "Ollama tuning" above for what's already been done to
  keep this from being worse than it has to be — there is no further config
  change that fixes a 2017 dual-core CPU.
- **No conversation memory across turns, still.** Each turn is a fresh
  `generate()` call with fresh context assembly — "no, the other file"
  won't work; be explicit each time, or use `/files` to pin what's
  relevant. What *did* change: turns within the same session now reuse
  Ollama's `context` token array from the previous turn purely to skip
  re-prefilling the (identical) system prompt from zero -- see "Context
  reuse" below. That's a prefill-speed optimization, not memory: the model
  still receives no summary of what you asked before, only a cached prefix
  it doesn't have to re-read.
- **Context budget is still a char cap** (`max_total_context_chars`), not an
  exact token count — no tokenizer library exists in this project by design
  (stdlib only). It's now *derived* from `num_ctx` (`config.py`'s
  `derive_max_context_chars`, ~3.2 chars/token, 65% of the window reserved
  for non-file-context text) instead of an unrelated flat guess, and the
  real per-turn token counts Ollama reports (`Usage`, see "Token usage &
  context budget" above) catch the cases where the char-based estimate
  undershoots or overshoots — but it's still an estimate going in, not a
  measurement, until the request actually comes back.
- **CCE cannot compress arbitrary text**, only files (`compress_file`) and
  named symbols (`get_symbol`) — confirmed directly against its Rust
  source. `run`/`fetch`/`search` output in the follow-up-hop loop uses a
  local heuristic head+tail truncation (`context/truncate.py`) instead,
  which is simpler and can't be as semantically aware as CCE's own
  outlining. Applying CCE to the project tree or to `skills/` content was
  considered and rejected: the tree is synthesized listing text (not a real
  file), and `skills/` is hand-curated prose already capped at 12000 chars
  total — running a pipeline meant for source code over either risks
  mangling content on purpose-written text for no measured benefit.
- **`is_online()` is checked once at startup**, not re-probed every turn
  (an extra network round trip per turn isn't worth it on hardware already
  bound by CPU inference time) — the banner reflects the state at launch,
  not necessarily mid-session changes in connectivity. `fetch`/`search`
  still each re-check before actually running, so a stale "online" banner
  can't cause a silent hang, only a slightly stale status line.
