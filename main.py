#!/usr/bin/env python3
"""localcoder -- a local coding CLI on top of Ollama + qwen2.5-coder, with
the Context-Compress-Engine doing context compression before anything
reaches the model. No pip dependencies -- stdlib only, so it runs anywhere
Python 3.10+ runs. Offline except for two opt-in actions, `fetch` and
`search` (see README's "Why fetch breaks the offline claim").
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions
import config
import execution
import gitsafety
import ui
import webfetch
import websearch
from agents.base import Agent
from agents.coder import CoderAgent
from agents.registry import AgentRegistry
from config import load_config
from context.cce_client import CCEClient
from context.denylist import is_denied
from context.tree import build_tree, list_source_files
from context.truncate import truncate_text
from knowledge.loader import load_skills
from llm.ollama_client import OllamaClient, OllamaError, Usage
from llm.prompts import build_user_prompt

MAX_FOLLOWUP_TURNS = 2
NEAR_LIMIT_FRACTION = 0.98  # prompt_eval_count / num_ctx above this -> warn

BANNER = """localcoder -- local, offline coding assistant (qwen2.5-coder via Ollama)
Type an instruction, or one of:
  /files <a.py> <b.py>   pin specific files as context for the next turn
  /agent <name> <task>   run a sub-agent once (test, refactor)
  /agents                list available sub-agents
  /search <query>        search the web (only if online), shown here directly
  /undo                  revert the last change localcoder committed (git repos only)
  /tree                  reprint the project tree
  /quit                  exit
"""


def assemble_file_context(
    cce: CCEClient,
    project_root: str,
    paths: list[str],
    budget_chars: int,
    task_description: str = "",
) -> tuple[str, list[str]]:
    """Returns (context text for the model, warnings for the human). The
    warnings used to be buried as inline markers only the model would see
    (`...(truncated to fit budget)`) -- now they're surfaced in the
    terminal too, since a truncated/skipped file is exactly the kind of
    thing that can make a response wrong without anyone noticing."""
    parts = []
    warnings: list[str] = []
    used = 0
    for path in paths:
        if is_denied(path):
            warnings.append(f"refusing to send {path} to the model (looks like a credential/key file)")
            continue
        compressed = cce.compress_file(path, task_description) if cce.available else None
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
                warnings.append(f"{path}: truncated to fit the {budget_chars}-char context budget")
            else:
                parts.append(f"### {path}\n(skipped -- context budget exhausted)\n")
                warnings.append(f"{path}: skipped entirely -- context budget exhausted")
                continue
        parts.append(f"### {path}\n{compressed}\n")
        used += len(compressed)
        if used >= budget_chars:
            break
    return "\n".join(parts), warnings


def format_durations(usage: Usage) -> str:
    def fmt(ns: int | None) -> str:
        return f"{ns / 1e9:.1f}s" if ns is not None else "?"

    return (
        f"took {fmt(usage.total_duration)} "
        f"(load {fmt(usage.load_duration)}, prompt-eval {fmt(usage.prompt_eval_duration)}, "
        f"eval {fmt(usage.eval_duration)})"
    )


def print_usage_summary(usage: Usage | None, num_ctx: int) -> None:
    if usage is None or usage.total_tokens is None:
        ui.sub(ui.dim("(uso de tokens indisponível para esta resposta)"))
        return
    ui.sub(ui.usage_bar(usage.prompt_eval_count or 0, usage.eval_count or 0, num_ctx))
    ui.sub(ui.dim(format_durations(usage)))
    if usage.prompt_eval_count is not None and usage.prompt_eval_count >= num_ctx * NEAR_LIMIT_FRACTION:
        ui.warn(
            "o prompt usou ~100% do num_ctx -- o Ollama pode ter descartado "
            "silenciosamente o início do prompt para caber; esta resposta pode "
            "assentar em contexto truncado. Considera subir num_ctx ou reduzir "
            "o contexto de ficheiros."
        )


def stream_and_print(chunks: Iterator[dict]) -> tuple[str, Usage | None]:
    """Prints `thinking`/`response` fragments live as Ollama emits them and
    returns (accumulated response text, real usage stats from the final
    chunk). A spinner covers the silent gap before the first fragment
    arrives -- connection + prompt prefill, exactly the "is it hung?" window
    on CPU-only hardware where a turn can take minutes. Note: qwen2.5-coder's
    template has no extended-thinking branch, so `thinking` fragments will
    likely never appear for this model -- harmless to check for, just don't
    expect to see them.
    """
    response_parts: list[str] = []
    thinking_started = False
    response_started = False
    usage: Usage | None = None
    spinner = ui.Spinner("a aguardar o modelo")
    spinner.start()
    try:
        for chunk in chunks:
            thinking = chunk.get("thinking") or ""
            response = chunk.get("response") or ""
            if thinking or response:
                spinner.stop()
            if thinking:
                if not thinking_started:
                    print("\n[thinking] ", end="", flush=True)
                    thinking_started = True
                print(thinking, end="", flush=True)
            if response:
                if not response_started:
                    print("\n\n" if thinking_started else "\n", end="")
                    response_started = True
                print(response, end="", flush=True)
                response_parts.append(response)
            if chunk.get("done"):
                usage = Usage.from_chunk(chunk)
                break
    finally:
        spinner.stop()
    print()
    return "".join(response_parts), usage


def run_turn(
    agent: Agent,
    task: str,
    context: str,
    project_root: str,
    cce: CCEClient,
    num_ctx: int,
    max_total_context_chars: int,
) -> None:
    """Streams a response, applies every action block it contains, and --
    only if a ```run/```fetch/```search/```symbol actually produced output --
    feeds that back for up to MAX_FOLLOWUP_TURNS more turns. write/delete
    never trigger a follow-up: their confirmation message is context enough.
    Each hop's action results are truncated (context/truncate.py -- CCE has
    no generic text-compression tool, only file/symbol-shaped ones) and the
    accumulated follow-up context is capped against max_total_context_chars,
    dropping older hop results (keeping the most recent) rather than
    growing unboundedly across hops."""
    current_task = task
    for hop in range(MAX_FOLLOWUP_TURNS + 1):
        try:
            output, usage = stream_and_print(agent.run_stream(current_task, context))
        except OllamaError as e:
            ui.error(str(e))
            return

        print_usage_summary(usage, num_ctx)

        for write in actions.extract_writes(output):
            actions.apply_write(project_root, write)
        for path in actions.extract_deletes(output):
            actions.apply_delete(project_root, path)
        for cmd in actions.extract_shell_suggestions(output):
            ui.info(f"suggested command -- not run automatically:\n  $ {cmd}")

        action_results: list[str] = []
        for cmd in actions.extract_runs(output):
            result = execution.apply_run(project_root, cmd)
            if result:
                action_results.append(truncate_text(result))
        for url in actions.extract_fetches(output):
            result = webfetch.apply_fetch(url)
            if result:
                action_results.append(truncate_text(result))
        for query in actions.extract_searches(output):
            result = websearch.apply_search(query)
            if result:
                action_results.append(truncate_text(result))
        for path, symbol in actions.extract_symbol_requests(output):
            if not cce.available:
                ui.warn(f"pedido get_symbol({path}, {symbol}) mas o CCE não está ligado -- a ignorar")
                continue
            snippet = cce.get_symbol(path, symbol)
            action_results.append(
                truncate_text(snippet) if snippet else f"get_symbol: símbolo `{symbol}` não encontrado em {path}"
            )

        if not action_results:
            return
        if hop >= MAX_FOLLOWUP_TURNS:
            ui.info(f"follow-up limit reached ({MAX_FOLLOWUP_TURNS}) -- stopping here")
            return

        appended = (
            f"\n\n--- RESULT OF YOUR LAST ACTION ---\n"
            + "\n\n".join(action_results)
            + "\n--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
        )
        if len(current_task) + len(appended) > max_total_context_chars:
            ui.warn(
                f"contexto acumulado dos follow-ups excede o orçamento seguro em tokens "
                f"({max_total_context_chars} chars, derivado de num_ctx={num_ctx}) -- "
                f"a descartar histórico mais antigo, mantendo só o resultado mais recente"
            )
            current_task = (
                f"{task}\n\n--- RESULT OF YOUR LAST ACTION ---\n"
                f"{truncate_text(action_results[-1], max_chars=max_total_context_chars // 2)}\n"
                "--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
            )
        else:
            current_task += appended


def main() -> None:
    cfg = load_config()
    project_root = str(Path.cwd())

    llm = OllamaClient(cfg["ollama_host"], cfg["model"], cfg["request_timeout_s"], cfg["num_ctx"])
    if not llm.is_up():
        ui.error(f"Ollama is not reachable at {cfg['ollama_host']}.")
        print("Start it with: ollama serve")
        sys.exit(1)

    derived_budget = config.derive_max_context_chars(cfg["num_ctx"])
    if cfg["max_total_context_chars"] > derived_budget:
        ui.warn(
            f"max_total_context_chars ({cfg['max_total_context_chars']}) pode exceder o que "
            f"num_ctx={cfg['num_ctx']} tokens aguenta em segurança (limite derivado: "
            f"{derived_budget} chars a ~{config.CHARS_PER_TOKEN} chars/token) -- o Ollama "
            f"pode descartar silenciosamente o início do prompt."
        )

    cce = CCEClient(cfg["cce_binary"], project_root)
    if cce.start():
        tools = ", ".join(cce.tool_names) or "nenhuma"
        ui.success(f"context-compress-engine: ligado ({cfg['cce_binary']}) -- tools: {tools}")
    else:
        ui.warn("context-compress-engine: indisponível, a usar leitura direta de ficheiros")
        print(
            f"             (esperava o binário em {cfg['cce_binary']} -- compila-o com "
            "'cargo build --release' no repo do CCE)"
        )

    if gitsafety.is_git_repo(project_root):
        ui.info("git repo detetado: escritas/eliminações confirmadas são auto-commitadas; /undo reverte a última")

    if websearch.is_online():
        ui.info("online: pesquisa web e fetch disponíveis")
    else:
        ui.warn("offline: pesquisa web e fetch vão falhar de imediato se tentados")

    knowledge = load_skills(cfg["skills_dir"])
    if knowledge:
        ui.info(f"carregados {len(knowledge)} ficheiro(s) de skills de {cfg['skills_dir']}")

    coder = CoderAgent(llm, knowledge)
    sub_agents = AgentRegistry(llm)

    print(BANNER)
    tree = build_tree(project_root, cfg["max_tree_entries"])
    pinned_files: list[str] = []
    cce_died_warned = False

    def check_cce_alive() -> None:
        nonlocal cce_died_warned
        if cce.available and not cce.is_alive() and not cce_died_warned:
            ui.warn(
                "o processo do context-compress-engine deixou de estar a correr -- "
                "a usar leitura direta de ficheiros pelo resto desta sessão"
            )
            cce_died_warned = True

    def build_context(paths: list[str], task_description: str) -> str:
        context, warnings = assemble_file_context(
            cce, project_root, paths, cfg["max_total_context_chars"], task_description
        )
        for w in warnings:
            ui.warn(w)
        if warnings:
            ui.warn(
                f"{len(warnings)} ficheiro(s) não couberam por completo no contexto deste "
                "turno -- a resposta pode assentar em conteúdo incompleto"
            )
        return context

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
                (ui.success if ok else ui.warn)(message)
                continue
            if line.startswith("/files "):
                pinned_files = line.removeprefix("/files ").split()
                ui.info(f"pinned: {', '.join(pinned_files) or '(none)'}")
                continue
            if line.startswith("/search "):
                query = line.removeprefix("/search ").strip()
                result = websearch.apply_search(query, confirm=False)
                if result:
                    print(result)
                continue
            if line.startswith("/agent "):
                rest = line.removeprefix("/agent ").strip()
                if " " not in rest:
                    ui.warn("usage: /agent <name> <task>")
                    continue
                agent_name, task = rest.split(" ", 1)
                agent = sub_agents.get(agent_name)
                if agent is None:
                    ui.warn(f"unknown agent '{agent_name}'. try: {', '.join(sub_agents.names())}")
                    continue
                check_cce_alive()
                context = build_context(pinned_files, task)
                run_turn(agent, task, context, project_root, cce, cfg["num_ctx"], cfg["max_total_context_chars"])
                continue

            # Default: main coder turn.
            check_cce_alive()
            files_for_context = pinned_files or list_source_files(project_root)[:5]
            file_context = build_context(files_for_context, line)
            prompt = build_user_prompt(line, tree, file_context)
            run_turn(coder, prompt, "", project_root, cce, cfg["num_ctx"], cfg["max_total_context_chars"])
    except KeyboardInterrupt:
        # Ctrl-C is the most likely way anyone exits this, and it's just as
        # likely to land mid-generation (a multi-minute CPU wait) as at the
        # prompt -- this catches it everywhere in the loop, not just input().
        ui.warn("interrupted, exiting")
    finally:
        cce.stop()


if __name__ == "__main__":
    main()
