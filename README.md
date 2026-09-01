# localcoder

A 100% local, offline coding CLI: `qwen2.5-coder:7b` via Ollama, with the
[Context-Compress-Engine](https://github.com/PedroRicoPaula/Context-Compress-Engine)
doing context compression before anything reaches the model. Zero pip
dependencies (stdlib only) — nothing here needs internet access at run time.

## Quick start (Omarchy / any Linux with Ollama installed)

```bash
git clone https://github.com/PedroRicoPaula/local-coder.git
cd local-coder

# 1. Ollama must be running with the model already pulled.
# scripts/ollama-tuned.service runs scripts/ollama-serve-tuned.sh as a
# systemd --user service instead of a bare background job -- see "Ollama
# tuning" below for why: a script backgrounded with `&` in a terminal dies
# (and can orphan its inference child process, unreachable, still burning
# CPU) the moment that terminal closes. The service survives that and
# restarts itself on crash.
mkdir -p ~/.local/bin ~/.config/systemd/user
cp scripts/ollama-serve-tuned.sh ~/.local/bin/ollama-serve-tuned
chmod +x ~/.local/bin/ollama-serve-tuned
cp scripts/ollama-tuned.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ollama-tuned.service
ollama pull qwen2.5-coder:7b     # skip if you already have it

# 2. Build the CCE binary once (optional but recommended -- see below)
git clone https://github.com/PedroRicoPaula/Context-Compress-Engine.git ../Context-Compress-Engine
cd ../Context-Compress-Engine
cargo build --release
cp target/release/context-compressor-mcp ~/.cargo/bin/
cd -

# 3. Run localcoder from inside the project you want to work on
cd /path/to/your/project
python3 /path/to/local-coder/main.py
```

Add an alias for convenience (adjust the path to wherever you cloned this):

```bash
echo 'alias localcoder="python3 /path/to/local-coder/main.py"' >> ~/.bashrc
```

## What it does each turn

1. Builds a compact tree of the current directory (`.gitignore`-aware).
2. Compresses whichever files are in context through CCE (heuristic,
   near-instant, no LLM involved in this step) — falls back to raw file
   reads if the CCE binary isn't built yet.
3. Injects any `.md` rule files from `skills/` into the system prompt.
4. Sends one prompt to Ollama, generation only (no streaming, no chat
   history across turns yet — see Limitations).
5. Parses the reply for ` ```write:path ` fenced blocks and offers to write
   each file, with a diff-free confirm prompt.

## Repo layout

```
main.py               entry point / REPL
config.py, config.json    model, host, timeouts, budgets
context/
  tree.py                project tree walker
  cce_client.py            MCP client wrapping the CCE binary
llm/
  ollama_client.py          HTTP client (urllib, stdlib only)
  prompts.py                 system/user prompt assembly
knowledge/loader.py           loads skills/*.md into the system prompt
skills/                        <- put your own stack rules here as .md files
agents/
  base.py                    Agent base class
  coder.py                    the default agent driving the REPL
  test_agent.py, refactor_agent.py   narrow-purpose sub-agents (stubs)
  registry.py                 name -> agent lookup
mcp/
  client.py                  generic stdio MCP client (JSON-RPC), reusable
                              for any future MCP server, not just CCE
mcp.servers.json               MCP server list (context-compressor pre-wired)
actions.py                      parses ```write blocks, applies them to disk
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

Two models beyond `qwen2.5-coder:7b` are worth keeping installed:
`qwen3:4b`/`qwen3:8b` are the only ones on this machine confirmed to emit
Ollama's structured `tool_calls` format reliably, if you ever build something
that needs that instead of the `` ```write `` convention. `qwen2.5-coder:14b`
and the redundant `qwen-claude:latest` alias were removed when RAM was
believed to be the binding constraint; worth reconsidering the 14b now that
the real RAM figure is known, though the CPU -- unchanged -- is still the
harder limit for a model that size.

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

## Multi-agent / MCP: what's real vs. scaffold

- **Real and working:** `mcp/client.py` is a generic stdio MCP client;
  `context/cce_client.py` uses it against the CCE binary today.
  `agents/registry.py` + `test`/`refactor` agents work now via
  `/agent test <task>` — same model, narrower system prompt.
- **Scaffold, not wired up:** there is no orchestrator that automatically
  chains coder → test → refactor, and no Playwright/DB MCP server is
  connected. `mcp.servers.json` and `mcp/client.py` are shaped so adding one
  is "add an entry + a thin wrapper like `cce_client.py`", not a rewrite.
  This was deliberately left unbuilt rather than guessed at — wire it up
  once you know which second tool you actually want.

## Limitations (measured on this hardware, not assumed)

- **This machine is CPU-only**: Intel i5-7200U, 2 cores/4 threads, no
  dedicated GPU. Expect tens of seconds to several minutes per turn, worse
  for longer prompts. `request_timeout_s` in `config.json` defaults to 300s
  for this reason. See "Ollama tuning" above for what's already been done to
  keep this from being worse than it has to be — there is no further config
  change that fixes a 2017 dual-core CPU.
- **No conversation memory across turns yet.** Each turn is a fresh
  `generate()` call with fresh context assembly. Cheap and predictable, but
  it means "no, the other file" won't work — be explicit each time, or use
  `/files` to pin what's relevant before asking.
- **Context budget is a hard char cap** (`max_total_context_chars`), not a
  token count. It's a proxy, not exact — same caveat CCE's own docs note
  about byte-vs-token ratios.
- **`get_symbol` is implemented (`context/cce_client.py`) but never called.**
  Every file that reaches the model goes through `compress_file`'s outline
  mode only; if the outline elides a function body the task actually needs,
  there is currently no way for a turn to go get it back mid-response --
  that would need the model to ask for more and the CLI to feed it back in,
  i.e. a real follow-up loop, which conflicts with the single-shot
  `generate()` design this project deliberately chose over unreliable
  tool-calling (see "Why no JSON tool-calling" above). Noted rather than
  built speculatively: worth revisiting if outline-mode context turns out to
  be insufficient in practice, not before.
