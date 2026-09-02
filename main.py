#!/usr/bin/env python3
"""localcoder -- a local coding CLI on top of Ollama + qwen2.5-coder, with
the Context-Compress-Engine doing context compression before anything
reaches the model. No pip dependencies -- stdlib only, so it runs anywhere
Python 3.10+ runs. Offline except for two opt-in actions, `fetch` and
`search` (see README's "Why fetch breaks the offline claim").
"""
from __future__ import annotations

import argparse
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
from llm import busy
from llm.ollama_client import OllamaClient, OllamaError, Usage
from llm.prompts import build_user_prompt

MAX_FOLLOWUP_TURNS = 2
NEAR_LIMIT_FRACTION = 0.98  # prompt_eval_count / num_ctx above this -> warn

BANNER = """localcoder -- local, offline coding assistant (qwen2.5-coder via Ollama)
Type an instruction, or one of:
  /files <a.py> <b.py>   pin specific files as context for the next turn
  /agent <name> <task>   run a sub-agent once (test, refactor)
  /agents                list available sub-agents
  /model <name>          switch model for this session (e.g. qwen3:4b)
  /search <query>        search the web (only if online), shown here directly
  /undo                  revert the last change localcoder committed (git repos only)
  /tree                  reprint the project tree
  /quit                  exit
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="localcoder -- local, offline coding assistant")
    parser.add_argument(
        "--profile", choices=["fast", "quality"],
        help="model profile from config.json's model_profiles (ignored if config.json pins an explicit model)",
    )
    parser.add_argument("--model", help="explicit model name -- overrides --profile and config.json")
    return parser.parse_args(argv)


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


def stream_and_print(chunks: Iterator[dict]) -> tuple[str, Usage | None, list[int] | None]:
    """Prints `thinking`/`response` fragments live as Ollama emits them and
    returns (accumulated response text, real usage stats, and the KV
    context array) from the final chunk. A spinner covers the silent gap
    before the first fragment arrives -- connection + prompt prefill,
    exactly the "is it hung?" window on CPU-only hardware where a turn can
    take minutes. Note: qwen2.5-coder's template has no extended-thinking
    branch, so `thinking` fragments will likely never appear for this
    model -- harmless to check for, just don't expect to see them.
    """
    response_parts: list[str] = []
    thinking_started = False
    response_started = False
    usage: Usage | None = None
    kv_context: list[int] | None = None
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
                kv_context = chunk.get("context")
                break
    finally:
        spinner.stop()
    print()
    return "".join(response_parts), usage, kv_context


def run_turn(
    agent: Agent,
    task: str,
    context: str,
    project_root: str,
    cce: CCEClient,
    num_ctx: int,
    max_total_context_chars: int,
    initial_kv_context: list[int] | None = None,
) -> list[int] | None:
    """Streams a response, applies every action block it contains, and --
    only if a ```run/```fetch/```search/```symbol actually produced output --
    feeds that back for up to MAX_FOLLOWUP_TURNS more turns. write/delete
    never trigger a follow-up: their confirmation message is context enough.
    Each hop's action results are truncated (context/truncate.py -- CCE has
    no generic text-compression tool, only file/symbol-shaped ones) and the
    accumulated follow-up context is capped against max_total_context_chars,
    dropping older hop results (keeping the most recent) rather than
    growing unboundedly across hops.

    `initial_kv_context` seeds this turn with a KV-cache token array from a
    previous call (see llm/ollama_client.py's generate() docstring) --
    `None` means prefill from scratch, matching the pre-existing behavior.
    Regardless of what's passed in, once hop 0 completes, every later hop
    within *this* call reuses hop 0's returned context automatically: the
    follow-up prompt sent on hop >=1 is then just the new
    "RESULT OF YOUR LAST ACTION" delta, not the whole accumulated task text
    (which is already covered by the cached prefix -- resending it too
    would re-prefill text Ollama already has and could duplicate it ahead
    of the new tokens). `current_task` keeps growing/truncating exactly as
    before regardless, since it's still what num_ctx budget bookkeeping and
    the no-cache fallback path use.

    Returns the final KV context array (or None) so the caller can offer it
    to the *next* run_turn() call, if it wants to."""
    current_task = task
    hop_kv_context = initial_kv_context
    for hop in range(MAX_FOLLOWUP_TURNS + 1):
        if hop == 0:
            # Start of a new task: current_task IS the new content (there's
            # no "delta" yet, even if initial_kv_context carries a cached
            # prefix from a previous turn) -- always send it in full, just
            # let Ollama skip re-prefilling whatever initial_kv_context
            # already covers.
            prompt_to_send, context_to_send = current_task, context
        elif hop_kv_context is not None:
            # A later hop within this same call: hop_kv_context now covers
            # everything up to and including the previous hop's response,
            # so only the new follow-up delta needs sending.
            prompt_to_send, context_to_send = appended, ""
        else:
            # No cache available (e.g. an Ollama version that never
            # returned a context array) -- fall back to resending
            # everything, same as before this feature existed.
            prompt_to_send, context_to_send = current_task, context
        try:
            output, usage, new_kv_context = stream_and_print(
                agent.run_stream(prompt_to_send, context_to_send, kv_context=hop_kv_context)
            )
        except OllamaError as e:
            ui.error(str(e))
            return hop_kv_context
        hop_kv_context = new_kv_context

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
            return hop_kv_context
        if hop >= MAX_FOLLOWUP_TURNS:
            ui.info(f"follow-up limit reached ({MAX_FOLLOWUP_TURNS}) -- stopping here")
            return hop_kv_context

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
            # The cached prefix (hop_kv_context) covers the *dropped* history
            # too -- once older hops are discarded, "resend just the delta"
            # no longer means the same thing (the delta would land on top of
            # a prefix that includes text we just chose to drop from
            # `current_task`). Fall back to a clean, uncached resend of the
            # freshly-rebuilt current_task next hop rather than risk sending
            # a confusing/duplicated prompt.
            hop_kv_context = None
        else:
            current_task += appended
    return hop_kv_context


def resolve_model_config(cfg: dict, args: argparse.Namespace) -> dict:
    """Applies --model/--profile precedence on top of an already-loaded
    config: an explicit --model always wins; --profile is ignored (with a
    message, not silently) if config.json itself pins a model, since an
    explicit user choice there always beats a profile; an unknown profile
    name is warned about and otherwise ignored. Returns a new dict rather
    than mutating `cfg` in place, so it's cheap to call from a test with a
    plain dict and no real config.json on disk."""
    cfg = dict(cfg)
    if args.model:
        cfg["model"] = args.model
    elif args.profile:
        if config.explicit_model_in_config_file():
            ui.info(f"config.json define um modelo explícito -- a ignorar --profile '{args.profile}'")
        else:
            profile = cfg.get("model_profiles", {}).get(args.profile)
            if profile:
                cfg.update(profile)
            else:
                ui.warn(f"perfil '{args.profile}' desconhecido -- a usar o modelo de config.json")
    return cfg


def main() -> None:
    args = parse_args()
    cfg = resolve_model_config(load_config(), args)
    project_root = str(Path.cwd())

    llm = OllamaClient(
        cfg["ollama_host"], cfg["model"], cfg["request_timeout_s"], cfg["num_ctx"],
        num_batch=cfg.get("num_batch"), num_thread=cfg.get("num_thread"),
    )
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
    reuse_across_turns = bool(cfg.get("reuse_context_across_turns", True))
    session_kv_context: list[int] | None = None
    kv_reuse_announced = False

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

    def call_run_turn(agent, task, ctx, initial_kv_context=None):
        """Wraps run_turn with the orphaned-generation check + advisory
        lock from llm/busy.py. Kept out of run_turn itself so that function
        stays a pure, easily-testable unit (no filesystem lock I/O to mock
        in tests/test_main.py)."""
        stale_pid = busy.check_stale_lock()
        if stale_pid is not None:
            running = busy.list_running(cfg["ollama_host"])
            if running:
                ui.warn(
                    f"o Ollama pode ainda estar a processar um pedido órfão de uma sessão "
                    f"anterior (pid {stale_pid} já não existe, mas continua carregado: "
                    f"{', '.join(running)}) -- este pedido pode ficar em fila atrás dele; "
                    "ver 'ps aux | grep llama-server'"
                )
        with busy.held():
            return run_turn(
                agent, task, ctx, project_root, cce, cfg["num_ctx"], cfg["max_total_context_chars"],
                initial_kv_context=initial_kv_context,
            )

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
                session_kv_context = None  # tree text is part of what's cached; it just changed
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
                session_kv_context = None  # pinned files change what's cached too
                ui.info(f"pinned: {', '.join(pinned_files) or '(none)'}")
                continue
            if line.startswith("/model "):
                llm.model = line.removeprefix("/model ").strip()
                session_kv_context = None  # a different model invalidates any cached KV state
                ui.info(f"modelo alterado para '{llm.model}' (contexto de sessão reiniciado)")
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
                # A sub-agent has its own system prompt, so it can't share the
                # main coder's cached kv_context -- and running it invalidates
                # that cache server-side too (a different request just went
                # through Ollama's single active slot), so drop it here.
                call_run_turn(agent, task, context)
                session_kv_context = None
                continue

            # Default: main coder turn.
            check_cce_alive()
            files_for_context = pinned_files or list_source_files(project_root)[:5]
            file_context = build_context(files_for_context, line)
            prompt = build_user_prompt(line, tree, file_context)
            kv_in = session_kv_context if reuse_across_turns else None
            session_kv_context = call_run_turn(coder, prompt, "", initial_kv_context=kv_in)
            if not reuse_across_turns:
                session_kv_context = None
            elif session_kv_context is not None and not kv_reuse_announced:
                ui.info(
                    "reaproveitamento de contexto Ollama ativo -- turnos seguintes nesta "
                    "sessão devem pré-processar mais depressa"
                )
                kv_reuse_announced = True
    except KeyboardInterrupt:
        # Ctrl-C is the most likely way anyone exits this, and it's just as
        # likely to land mid-generation (a multi-minute CPU wait) as at the
        # prompt -- this catches it everywhere in the loop, not just input().
        ui.warn("interrupted, exiting")
    finally:
        cce.stop()


if __name__ == "__main__":
    main()
