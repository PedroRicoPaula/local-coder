#!/usr/bin/env python3
"""localcoder -- a 100% local, offline coding CLI on top of Ollama +
qwen2.5-coder, with the Context-Compress-Engine doing context compression
before anything reaches the model.

No network access at any point after models are pulled. No pip
dependencies -- stdlib only, so it runs anywhere Python 3.10+ runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions
from agents.coder import CoderAgent
from agents.registry import AgentRegistry
from config import load_config
from context.cce_client import CCEClient
from context.denylist import is_denied
from context.tree import build_tree, list_source_files
from knowledge.loader import load_skills
from llm.ollama_client import OllamaClient, OllamaError
from llm.prompts import build_user_prompt

BANNER = """localcoder -- local, offline coding assistant (qwen2.5-coder via Ollama)
Type an instruction, or one of:
  /files <a.py> <b.py>   pin specific files as context for the next turn
  /agent <name> <task>   run a sub-agent once (test, refactor)
  /agents                list available sub-agents
  /tree                  reprint the project tree
  /quit                  exit
"""


def assemble_file_context(cce: CCEClient, project_root: str, paths: list[str], budget_chars: int) -> str:
    parts = []
    used = 0
    for path in paths:
        if is_denied(path):
            print(f"  [localcoder] refusing to send {path} to the model (looks like a credential/key file)")
            continue
        compressed = cce.compress_file(path, "") if cce.available else None
        if compressed is None:
            try:
                compressed = Path(project_root, path).read_text(errors="replace")
            except OSError as e:
                parts.append(f"### {path}\n(could not read: {e})\n")
                continue
        if used + len(compressed) > budget_chars:
            remaining = budget_chars - used
            if remaining > 200:
                compressed = compressed[:remaining] + "\n...(truncated to fit budget)"
            else:
                parts.append(f"### {path}\n(skipped -- context budget exhausted)\n")
                continue
        parts.append(f"### {path}\n{compressed}\n")
        used += len(compressed)
        if used >= budget_chars:
            break
    return "\n".join(parts)


def main() -> None:
    cfg = load_config()
    project_root = str(Path.cwd())

    llm = OllamaClient(cfg["ollama_host"], cfg["model"], cfg["request_timeout_s"], cfg["num_ctx"])
    if not llm.is_up():
        print(f"[localcoder] Ollama is not reachable at {cfg['ollama_host']}.")
        print("Start it with: ollama serve")
        sys.exit(1)

    cce = CCEClient(cfg["cce_binary"], project_root)
    if cce.start():
        print(f"[localcoder] context-compress-engine: connected ({cfg['cce_binary']})")
    else:
        print("[localcoder] context-compress-engine: not available, falling back to raw file reads")
        print(f"             (expected binary at {cfg['cce_binary']} -- build it with 'cargo build --release' in the CCE repo)")

    knowledge = load_skills(cfg["skills_dir"])
    if knowledge:
        print(f"[localcoder] loaded {len(knowledge)} skill file(s) from {cfg['skills_dir']}")

    coder = CoderAgent(llm, knowledge)
    sub_agents = AgentRegistry(llm)

    print(BANNER)
    tree = build_tree(project_root, cfg["max_tree_entries"])
    pinned_files: list[str] = []

    try:
        while True:
            try:
                line = input("\n> ").strip()
            except EOFError:
                break
            if not line:
                continue

            if line in ("/quit", "/exit"):
                break
            if line == "/tree":
                tree = build_tree(project_root, cfg["max_tree_entries"])
                print(tree)
                continue
            if line == "/agents":
                for n in sub_agents.names():
                    print(f"  {n}: {sub_agents.get(n).description}")
                continue
            if line.startswith("/files "):
                pinned_files = line.removeprefix("/files ").split()
                print(f"[localcoder] pinned: {', '.join(pinned_files) or '(none)'}")
                continue
            if line.startswith("/agent "):
                rest = line.removeprefix("/agent ").strip()
                if " " not in rest:
                    print("usage: /agent <name> <task>")
                    continue
                agent_name, task = rest.split(" ", 1)
                agent = sub_agents.get(agent_name)
                if agent is None:
                    print(f"unknown agent '{agent_name}'. try: {', '.join(sub_agents.names())}")
                    continue
                context = assemble_file_context(cce, project_root, pinned_files, cfg["max_total_context_chars"])
                print(f"[localcoder] running '{agent_name}' agent (this can take a while on CPU)...")
                try:
                    output = agent.run(task, context)
                except OllamaError as e:
                    print(f"[localcoder] {e}")
                    continue
                handle_response(project_root, output)
                continue

            # Default: main coder turn.
            files_for_context = pinned_files or list_source_files(project_root)[:5]
            file_context = assemble_file_context(cce, project_root, files_for_context, cfg["max_total_context_chars"])
            prompt = build_user_prompt(line, tree, file_context)
            print("[localcoder] thinking (CPU-only inference: this can take from tens of seconds to several minutes)...")
            try:
                output = coder.run(prompt)
            except OllamaError as e:
                print(f"[localcoder] {e}")
                continue
            handle_response(project_root, output)
    except KeyboardInterrupt:
        # Ctrl-C is the most likely way anyone exits this, and it's just as
        # likely to land mid-generation (a multi-minute CPU wait) as at the
        # prompt -- this catches it everywhere in the loop, not just input().
        print("\n[localcoder] interrupted, exiting")
    finally:
        cce.stop()


def handle_response(project_root: str, output: str) -> None:
    prose = actions.strip_action_blocks(output)
    if prose:
        print(f"\n{prose}")

    writes = actions.extract_writes(output)
    for write in writes:
        actions.apply_write(project_root, write)

    for cmd in actions.extract_shell_suggestions(output):
        print(f"\n[suggested command -- not run automatically]\n  $ {cmd}")


if __name__ == "__main__":
    main()
