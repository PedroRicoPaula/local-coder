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
import verification
import webfetch
import websearch
from agents.base import Agent
from agents.coder import CoderAgent
from agents.registry import AgentRegistry
from config import load_config
from context.cce_client import CCEClient
from context.denylist import is_denied
from context.relevance import ScoredFile, format_selection, select_files_for_task
from context.tree import build_tree
from context.truncate import truncate_text
from knowledge.loader import load_skills
from llm import busy
from llm.ollama_client import OllamaClient, OllamaError, Usage
from llm.prompts import build_user_prompt

MAX_FOLLOWUP_TURNS = 2
MAX_REPAIR_HOPS = 1  # NOT configurable: this is the one bound preventing an
                     # unbounded fix/verify loop, and there is no evidence a
                     # second hop helps a 7B model.
NEAR_LIMIT_FRACTION = 0.98  # prompt_eval_count / num_ctx above this -> warn

BANNER = """localcoder -- local, offline coding assistant (qwen2.5-coder via Ollama)
Type an instruction, or one of:
  /files <a.py> <b.py>   pin specific files as context for the next turn
  /context               show how the last turn's file context was chosen
  /why                   same as /context (scores + budget)
  /verify                compile all Python files in this project (confirmed)
  /agent <name> <task>   run a sub-agent once (test, refactor)
  /agents                list available sub-agents
  /model <name>          switch model for this session (e.g. qwen3:4b)
  /search <query>        search the web (only if online), shown here directly
  /undo                  revert the last change localcoder committed (git repos only)
  /tree                  reprint the project tree
  /help                  reprint this list
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


def _append_result(
    task: str,
    current_task: str,
    results: list[str],
    max_total_context_chars: int,
    num_ctx: int,
    kv_context: list[int] | None,
) -> tuple[str, str, list[int] | None]:
    """Returns (new current_task, delta to send, kv_context or None).

    Exactly the pre-existing hop-budget logic, extracted: append when it
    fits; otherwise warn, rebuild current_task from `task` plus the most
    recent result only, and drop the cached kv_context because it covers
    text we just dropped. `delta` is only meaningful when the returned
    kv_context survives; the caller sends `current_task` instead whenever it
    comes back None."""
    appended = (
        "\n\n--- RESULT OF YOUR LAST ACTION ---\n"
        + "\n\n".join(results)
        + "\n--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
    )
    if len(current_task) + len(appended) > max_total_context_chars:
        ui.warn(
            f"contexto acumulado dos follow-ups excede o orçamento seguro em tokens "
            f"({max_total_context_chars} chars, derivado de num_ctx={num_ctx}) -- "
            f"a descartar histórico mais antigo, mantendo só o resultado mais recente"
        )
        rebuilt = (
            f"{task}\n\n--- RESULT OF YOUR LAST ACTION ---\n"
            f"{truncate_text(results[-1], max_chars=max_total_context_chars // 2)}\n"
            "--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
        )
        return rebuilt, appended, None
    return current_task + appended, appended, kv_context


def _apply_blocks(output: str, project_root: str, cce: CCEClient,
                  mutated: list[str]) -> list[str]:
    """Applies every block in `output` in today's order (writes, deletes,
    shell suggestions, edits, malformed edits, runs, fetches, searches,
    symbols), appends each successfully mutated relative path to `mutated`,
    and returns the truncated action results that would feed a follow-up
    hop."""
    def track(path: str) -> None:
        if path not in mutated:
            mutated.append(path)

    for write in actions.extract_writes(output):
        if actions.apply_write(project_root, write):
            track(write.path)
    for path in actions.extract_deletes(output):
        if actions.apply_delete(project_root, path):
            track(path)
    for cmd in actions.extract_shell_suggestions(output):
        ui.info(f"suggested command -- not run automatically:\n  $ {cmd}")

    action_results: list[str] = []
    for edit in actions.extract_edits(output):
        result = actions.apply_edit(project_root, edit)
        if result.ok:
            track(edit.path)
        if result.error:
            action_results.append(truncate_text(result.error))
    for bad in actions.extract_malformed_edits(output):
        action_results.append(truncate_text(actions.format_action_error(
            action="edit",
            reason="edit block is not a valid SEARCH/REPLACE pair",
            path=bad.path,
            suggestion=(
                "use exactly:\n<<<<<<< SEARCH\n<the old lines>\n=======\n"
                "<the new lines>\n>>>>>>> REPLACE"
            ),
        )))
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
            truncate_text(snippet) if snippet
            else f"get_symbol: símbolo `{symbol}` não encontrado em {path}"
        )
    return action_results


def build_verify_config(cfg: dict) -> verification.VerifyConfig:
    override = cfg.get("verify_command")
    if override is not None and not (
        isinstance(override, list) and override
        and all(isinstance(part, str) for part in override)
    ):
        ui.warn("verify_command must be a list of strings -- ignoring it")
        override = None
    return verification.VerifyConfig(
        enabled=bool(cfg.get("verify_after_change", True)),
        timeout_s=int(cfg.get("verify_timeout_s", 180)),
        override_argv=list(override) if override else None,
    )


def run_turn(
    agent: Agent,
    task: str,
    context: str,
    project_root: str,
    cce: CCEClient,
    num_ctx: int,
    max_total_context_chars: int,
    initial_kv_context: list[int] | None = None,
    verify: verification.VerifyConfig | None = None,
) -> list[int] | None:
    """Streams a response, applies every action block it contains, and --
    only if a ```run/```fetch/```search/```symbol produced output, or an
    ```edit failed in a way the model can fix -- feeds that back for up to
    MAX_FOLLOWUP_TURNS more turns. Successful write/edit/delete never
    trigger a follow-up: their confirmation message is context enough.
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
    to the *next* run_turn() call, if it wants to.

    `verify` enables the post-mutation verification phase; None means "no
    verification", which is exactly the pre-existing behaviour. Verification
    runs once, after the exploration loop -- never between hops, because
    running the suite mid-change tests a half-finished edit and multiplies
    the cost on hardware where each execution is real wall time."""
    current_task = task
    hop_kv_context = initial_kv_context
    mutated: list[str] = []
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

        action_results = _apply_blocks(output, project_root, cce, mutated)
        if not action_results:
            break
        if hop >= MAX_FOLLOWUP_TURNS:
            ui.info(f"follow-up limit reached ({MAX_FOLLOWUP_TURNS}) -- stopping here")
            break
        current_task, appended, hop_kv_context = _append_result(
            task, current_task, action_results, max_total_context_chars, num_ctx, hop_kv_context,
        )

    if verify is None or not mutated:
        return hop_kv_context

    outcome = verification.verify_project(project_root, mutated, verify)
    if not outcome.needs_repair:
        return hop_kv_context

    current_task, delta, hop_kv_context = _append_result(
        task, current_task, [verification.failure_feedback(outcome)],
        max_total_context_chars, num_ctx, hop_kv_context,
    )
    prompt, ctx = (delta, "") if hop_kv_context is not None else (current_task, context)
    try:
        output, usage, hop_kv_context = stream_and_print(
            agent.run_stream(prompt, ctx, kv_context=hop_kv_context)
        )
    except OllamaError as e:
        ui.error(str(e))
        return hop_kv_context
    print_usage_summary(usage, num_ctx)

    repair_mutated: list[str] = []
    # Every repair block is still applied and still individually confirmed
    # (a ```run in the repair response executes if the human says yes), but
    # the returned action results are DISCARDED -- that is what makes the
    # model-call bound structural rather than arithmetic.
    _apply_blocks(output, project_root, cce, repair_mutated)
    if repair_mutated:
        # Reported to the human by verify_project; never fed back anywhere.
        verification.verify_project(project_root, repair_mutated, verify)
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

    verify_cfg = build_verify_config(cfg)

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
    last_selection: list[ScoredFile] = []
    last_context_used_chars = 0
    last_selection_pinned = False
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
        nonlocal last_context_used_chars
        context, warnings = assemble_file_context(
            cce, project_root, paths, cfg["max_total_context_chars"], task_description
        )
        last_context_used_chars = len(context)
        for w in warnings:
            ui.warn(w)
        if warnings:
            ui.warn(
                f"{len(warnings)} ficheiro(s) não couberam por completo no contexto deste "
                "turno -- a resposta pode assentar em conteúdo incompleto"
            )
        return context

    def print_context_report() -> None:
        print(
            format_selection(
                last_selection,
                budget_chars=cfg["max_total_context_chars"],
                used_chars=last_context_used_chars,
                num_ctx=cfg["num_ctx"],
                pinned=last_selection_pinned,
            )
        )

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
                initial_kv_context=initial_kv_context, verify=verify_cfg,
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
            if line in ("/help", "/?"):
                print(BANNER)
                continue
            if line in ("/context", "/why"):
                print_context_report()
                continue
            if line == "/verify":
                execution.apply_run(project_root, "python3 -m compileall -q .")
                continue
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
                last_selection = [ScoredFile(p, 1.0) for p in pinned_files]
                last_selection_pinned = bool(pinned_files)
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
                last_selection = [ScoredFile(p, 1.0) for p in pinned_files]
                last_selection_pinned = bool(pinned_files)
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
            if pinned_files:
                last_selection = [ScoredFile(p, 1.0) for p in pinned_files]
                last_selection_pinned = True
                files_for_context = pinned_files
            else:
                last_selection = select_files_for_task(project_root, line, limit=5)
                last_selection_pinned = False
                files_for_context = [item.path for item in last_selection]
            if files_for_context:
                ui.info("contexto: " + ", ".join(files_for_context))
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
