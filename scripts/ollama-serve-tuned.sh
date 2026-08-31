#!/usr/bin/env bash
# Starts Ollama with settings tuned for this machine: 11GB RAM, CPU-only,
# one model used at a time. Optional -- plain `ollama serve` still works,
# this just avoids paying for headroom this hardware doesn't have.
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
export OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_KEEP_ALIVE=30m
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_FLASH_ATTENTION=1
exec ollama serve
