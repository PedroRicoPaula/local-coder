"""Central configuration. Reads config.json next to this file if present,
falls back to hardcoded defaults. No external dependencies.
"""
from __future__ import annotations

import json
from pathlib import Path

import ui

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"

CHARS_PER_TOKEN = 3.2      # Conservative: assumes MORE tokens per char than
                            # the ~4 chars/token typical of English prose,
                            # because code is denser (short identifiers,
                            # heavy punctuation). No tokenizer library exists
                            # in this project by design (stdlib only), so
                            # this stays a documented ratio, not a
                            # measurement -- the safe direction is to
                            # overestimate token cost, not underestimate it.
RESERVED_FRACTION = 0.35   # Fraction of num_ctx NOT available for file
                            # context -- reserved for system prompt, tree,
                            # skills, task text, and the model's own
                            # response.

DEFAULTS = {
    # Ollama
    "ollama_host": "http://127.0.0.1:11434",
    "model": "qwen2.5-coder:7b",
    "request_timeout_s": 600,      # CPU-only inference is slow; be generous.
                                    # Raised from 300s: a real task (not a
                                    # smoke test) can need more room to finish
                                    # rather than being cut off mid-response.
    "num_ctx": 8192,                # Ollama's own CPU default is a flat 4096
                                    # regardless of system RAM (confirmed in
                                    # its own startup log -- CPU inference
                                    # doesn't auto-scale with RAM the way GPU/
                                    # VRAM-based sizing does). Measured 19.4GB
                                    # total / ~15GB available on this machine,
                                    # not the 11GB originally assumed, so
                                    # doubling the default buys real headroom
                                    # for longer files/instructions without
                                    # truncation, at a real but affordable
                                    # RAM and prefill-time cost. Raise further
                                    # per-project if a task actually needs it.

    # Context-Compress-Engine (MCP stdio server). CCE_ROOT is set per-instance
    # at connect time (see context/cce_client.py), not read from here.
    "cce_binary": str(Path.home() / ".cargo" / "bin" / "context-compressor-mcp"),

    # Context assembly
    "max_tree_entries": 400,
    # NOTE: no "max_total_context_chars" default here -- it's derived from
    # num_ctx by load_config() below unless the user sets it explicitly in
    # config.json (see derive_max_context_chars).

    # Skills
    "skills_dir": str(ROOT / "skills"),

    # MCP servers available to the agent layer (beyond CCE)
    "mcp_servers_file": str(ROOT / "mcp.servers.json"),
}


def derive_max_context_chars(num_ctx: int) -> int:
    """Safe default for the file-context char budget, derived from the
    model's real token window (num_ctx) instead of a flat guess disconnected
    from it. At num_ctx=8192 this derives ~17800 chars -- previously a flat
    60000 was used regardless of num_ctx, already ~3.5x past what the real
    token window can safely hold before system prompt/tree/task text are
    even counted, a latent silent-truncation risk on Ollama's side."""
    return int(num_ctx * (1 - RESERVED_FRACTION) * CHARS_PER_TOKEN)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    user_cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text())
            cfg.update(user_cfg)
        except (json.JSONDecodeError, OSError) as e:
            ui.warn(f"could not read {CONFIG_FILE}: {e}")
    if "max_total_context_chars" not in user_cfg:
        # Not set by the user -- derive it from whatever num_ctx is in
        # effect (default or overridden) so the two budgets never drift
        # apart on their own. An explicit user value is respected as-is
        # (main.py warns at startup if it exceeds the derived safe cap,
        # rather than silently overriding an intentional choice).
        cfg["max_total_context_chars"] = derive_max_context_chars(cfg["num_ctx"])
    return cfg
