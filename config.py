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
    "request_timeout_s": 300,      # CPU-only inference is slow; be generous.
    "num_ctx": 4096,                # Ollama's own auto-scaling default on an
                                    # 11GB box is ~4K (see OLLAMA_CONTEXT_LENGTH);
                                    # going above that trades RAM and prefill
                                    # time for headroom this hardware rarely
                                    # uses. Raise per-project if you actually
                                    # need it, don't raise it by default.

    # Context-Compress-Engine (MCP stdio server)
    "cce_binary": str(Path.home() / ".cargo" / "bin" / "context-compressor-mcp"),
    "cce_root": None,              # set at runtime to the project cwd

    # Context assembly
    "max_tree_entries": 400,
    "max_file_bytes_to_include": 20000,   # per file, after CCE compression
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
