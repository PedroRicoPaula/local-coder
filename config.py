"""Central configuration. Reads config.json next to this file if present,
falls back to hardcoded defaults. No external dependencies.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"

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
    "max_total_context_chars": 60000,     # hard budget for the whole prompt

    # Skills
    "skills_dir": str(ROOT / "skills"),

    # MCP servers available to the agent layer (beyond CCE)
    "mcp_servers_file": str(ROOT / "mcp.servers.json"),
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text())
            cfg.update(user_cfg)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] warning: could not read {CONFIG_FILE}: {e}")
    return cfg
