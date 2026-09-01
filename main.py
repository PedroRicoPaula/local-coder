#!/usr/bin/env python3
"""localcoder -- a local coding CLI on top of Ollama + qwen2.5-coder, with
the Context-Compress-Engine doing context compression before anything
reaches the model. No pip dependencies -- stdlib only, so it runs anywhere
Python 3.10+ runs. Offline except for the one opt-in ```fetch action (see
README's "Why fetch breaks the offline claim").
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions
import execution
import gitsafety
import webfetch
from agents.base import Agent
from agents.coder import CoderAgent
from agents.registry import AgentRegistry
from config import load_config
from context.cce_client import CCEClient
from context.denylist import is_denied
from context.tree import build_tree, list_source_files
from knowledge.loader import load_skills
from llm.ollama_client import OllamaClient, OllamaError
from llm.prompts import build_user_prompt

MAX_FOLLOWUP_TURNS = 2

BANNER = """localcoder -- local, offline coding assistant (qwen2.5-coder via Ollama)
Type an instruction, or one of:
  /files <a.py> <b.py>   pin specific files as context for the next turn
  /agent <name> <task>   run a sub-agent once (test, refactor)
  /agents                list available sub-agents
  /undo                  revert the last change localcoder committed (git repos only)
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


def stream_and_print(chunks: Iterator[dict]) -> str:
    """Prints `thinking`/`response` fragments live as Ollama emits them and
    returns the accumulated response text. This is the direct answer to
    "I can't tell if it's working": raw tokens appear as they're generated,
    including the literal fenced action blocks -- noisier than the old
    clean post-hoc summary, but undeniable proof of life on hardware where
    a turn can take minutes. Note: qwen2.5-coder's template has no extended-
    thinking branch, so `thinking` fragments will likely never appear for
    this model -- harmless to check for, just don't expect to see them.
    """
    response_parts: list[str] = []
    thinking_started = False
    response_started = False
    for chunk in chunks:
        thinking = chunk.get("thinking") or ""
        if thinking:
            if not thinking_started:
                print("\n[thinking] ", end="", flush=True)
                thinking_started = True
            print(thinking, end="", flush=True)
        response = chunk.get("response") or ""
        if response:
            if not response_started:
                print("\n\n" if thinking_started else "\n", end="")
                response_started = True
            print(response, end="", flush=True)
            response_parts.append(response)
        if chunk.get("done"):
            break
    print()
    return "".join(response_parts)


def run_turn(agent: Agent, task: str, context: str, project_root: str) -> None:
    """Streams a response, applies every action block it contains, and --
    only if a ```run or ```fetch actually produced output -- feeds that
    back for up to MAX_FOLLOWUP_TURNS more turns. write/delete/shell never
    trigger a follow-up: their confirmation message is context enough."""
    current_task = task
    for hop in range(MAX_FOLLOWUP_TURNS + 1):
        try:
            output = stream_and_print(agent.run_stream(current_task, context))
        except OllamaError as e:
            print(f"[localcoder] {e}")
            return

        for write in actions.extract_writes(output):
            actions.apply_write(project_root, write)
        for path in actions.extract_deletes(output):
            actions.apply_delete(project_root, path)
        for cmd in actions.extract_shell_suggestions(output):
            print(f"\n[suggested command -- not run automatically]\n  $ {cmd}")

        action_results: list[str] = []
        for cmd in actions.extract_runs(output):
            result = execution.apply_run(project_root, cmd)
            if result:
                action_results.append(result)
        for url in actions.extract_fetches(output):
            result = webfetch.apply_fetch(url)
            if result:
                action_results.append(result)

        if not action_results:
            return
        if hop >= MAX_FOLLOWUP_TURNS:
            print(f"[localcoder] follow-up limit reached ({MAX_FOLLOWUP_TURNS}) -- stopping here")
            return

        current_task = (
            f"{current_task}\n\n--- RESULT OF YOUR LAST ACTION ---\n"
            + "\n\n".join(action_results)
            + "\n--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
        )


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

    if gitsafety.is_git_repo(project_root):
        print("[localcoder] git repo detected: confirmed writes/deletes are auto-committed; /undo reverts the last one")

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
            if line == "/undo":
                ok, message = gitsafety.undo_last(project_root)
                print(f"[localcoder] {message}")
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
                run_turn(agent, task, context, project_root)
                continue

            # Default: main coder turn.
            files_for_context = pinned_files or list_source_files(project_root)[:5]
            file_context = assemble_file_context(cce, project_root, files_for_context, cfg["max_total_context_chars"])
            prompt = build_user_prompt(line, tree, file_context)
            run_turn(coder, prompt, "", project_root)
    except KeyboardInterrupt:
        # Ctrl-C is the most likely way anyone exits this, and it's just as
        # likely to land mid-generation (a multi-minute CPU wait) as at the
        # prompt -- this catches it everywhere in the loop, not just input().
        print("\n[localcoder] interrupted, exiting")
    finally:
        cce.stop()


if __name__ == "__main__":
    main()
