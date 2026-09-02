#!/usr/bin/env bash
# Starts Ollama with settings tuned for this machine: ~19GB RAM, CPU-only
# (2 physical / 4 logical cores), one model used at a time. Optional --
# plain `ollama serve` still works, this just avoids paying for headroom
# this hardware doesn't have and picks settings CPU-only inference actually
# benefits from.
#
# OLLAMA_KV_CACHE_TYPE=q8_0  -- roughly halves KV-cache memory (needs flash
#                                attention, forced on below).
# OLLAMA_KEEP_ALIVE=30m       -- stay warm across a work session instead of
#                                paying a ~15-20s reload every idle gap.
# OLLAMA_MAX_LOADED_MODELS=1  -- never hold two models in RAM at once.
# OLLAMA_NUM_PARALLEL=1       -- one request at a time; this CPU can't
#                                usefully serve concurrent requests anyway.
# OLLAMA_FLASH_ATTENTION=1    -- force on rather than trust auto-detection
#                                on a 2017 CPU.
#
# Batch size and thread count (num_batch/num_thread) are NOT server-wide
# env vars in this Ollama version (0.33.2) -- verified directly against
# /usr/bin/ollama: no OLLAMA_NUM_BATCH/OLLAMA_NUM_THREAD env var exists in
# the binary at all (confirmed via `strings` + the server's own logged
# startup config, which doesn't mention either). They're per-request
# `options` fields instead, the same mechanism num_ctx already uses -- see
# llm/ollama_client.py's OllamaClient (num_batch/num_thread constructor
# args, config.json's "num_batch"/"num_thread" keys), not this script.
export OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_KEEP_ALIVE=30m
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_FLASH_ATTENTION=1
exec ollama serve
