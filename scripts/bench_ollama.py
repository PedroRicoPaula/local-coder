#!/usr/bin/env python3
"""Benchmarks one or more pulled Ollama models against a fixed small prompt
set, using OllamaClient directly (no duplicate HTTP logic -- same client
main.py uses). Reports the real prompt_eval/eval counts and durations
Ollama already returns, instead of guessing from published benchmarks.

Used for two things this project needs measured, not assumed:
  1. Whether an Ollama tuning change (num_batch, num_thread, ...) actually
     helps on THIS machine -- run once before, once after, compare.
  2. Whether a candidate "fast" profile model (e.g. qwen3:4b) is actually
     faster here, and roughly how much -- before promoting it in
     config.py's model_profiles.

Deliberately not a general-purpose benchmark suite: two representative
prompts, no statistics beyond what Ollama already reports per call. Log
real results in docs/BENCHMARKS.md by hand -- this script prints, it
doesn't write files, so a "no measurable difference" result still requires
someone to look at the output and note it down (matching this project's
existing docs/LESSONS_LEARNED.md discipline of writing down what was
actually measured).

Usage:
  python3 scripts/bench_ollama.py                      # benchmark cfg["model"]
  python3 scripts/bench_ollama.py qwen3:4b qwen3:8b     # benchmark specific models
  python3 scripts/bench_ollama.py --all-pulled          # benchmark everything `ollama list` shows
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from llm.ollama_client import OllamaClient, OllamaError

PROMPTS = {
    "short": "In one sentence, what does the Python `zip()` builtin do?",
    "code": (
        "Write a Python function `is_prime(n: int) -> bool` that correctly "
        "handles n <= 1. No explanation, just the function."
    ),
}


def list_pulled_models(host: str) -> list[str]:
    req = urllib.request.Request(f"{host.rstrip('/')}/api/tags")
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.load(resp)
    return [m["name"] for m in body.get("models", [])]


def bench_one(client: OllamaClient, model: str, prompt_name: str, prompt: str) -> None:
    client.model = model
    t0 = time.monotonic()
    try:
        result = client.generate(prompt, think=False)
    except OllamaError as e:
        print(f"  [{prompt_name}] FAILED: {e}")
        return
    wall = time.monotonic() - t0
    u = result.usage
    if u is None or u.total_tokens is None:
        print(f"  [{prompt_name}] no usage stats returned (wall={wall:.1f}s)")
        return

    def s(ns):
        return f"{ns / 1e9:.1f}s" if ns is not None else "?"

    print(
        f"  [{prompt_name}] wall={wall:.1f}s  "
        f"prompt_eval={u.prompt_eval_count}tok/{s(u.prompt_eval_duration)}  "
        f"eval={u.eval_count}tok/{s(u.eval_duration)}  "
        f"load={s(u.load_duration)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="*", help="model names to benchmark (default: current config's model)")
    parser.add_argument("--all-pulled", action="store_true", help="benchmark every model `ollama list` shows")
    args = parser.parse_args()

    cfg = load_config()
    client = OllamaClient(cfg["ollama_host"], cfg["model"], cfg["request_timeout_s"], cfg["num_ctx"],
                           num_batch=cfg.get("num_batch"), num_thread=cfg.get("num_thread"))
    if not client.is_up():
        print(f"Ollama is not reachable at {cfg['ollama_host']} -- start it first.")
        sys.exit(1)

    if args.all_pulled:
        models = list_pulled_models(cfg["ollama_host"])
    elif args.models:
        models = args.models
    else:
        models = [cfg["model"]]

    print(f"Benchmarking {len(models)} model(s) against {len(PROMPTS)} prompt(s).")
    print("First call per model pays cold load + prefill -- that's expected, not a bug.\n")

    for model in models:
        print(f"=== {model} ===")
        for name, prompt in PROMPTS.items():
            bench_one(client, model, name, prompt)
        print()


if __name__ == "__main__":
    main()
