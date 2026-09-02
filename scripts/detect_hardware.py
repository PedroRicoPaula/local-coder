#!/usr/bin/env python3
"""Detects this Linux machine's CPU/RAM/GPU capability and buckets it into
a tier, so install.sh can write a config.json and Ollama tuning suited to
THIS machine instead of hand-copying settings tuned for a different one
(which is exactly how localcoder ended up working only on its original dev
machine -- see docs/BACKLOG.md). Stdlib only, matching the rest of this
project.

Structurally similar to what tools like whichllm/llmfit do (detect
hardware, pick a tier) -- the Ollama-tuning-per-tier part below is
project-specific glue, not copied from anywhere.

Usage: python3 scripts/detect_hardware.py   # prints JSON to stdout
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Hardware:
    physical_cores: int
    logical_cores: int
    ram_gb: float
    has_gpu: bool
    vram_gb: float | None
    tier: str  # "gpu" | "cpu-strong" | "cpu-weak"


def _physical_cores() -> int:
    """Counts unique (physical id, core id) pairs in /proc/cpuinfo --
    logical (hyperthreaded) siblings share a core id, so this correctly
    undercounts them relative to os.cpu_count(). Falls back to
    os.cpu_count() if /proc/cpuinfo is unreadable (non-Linux, restricted
    container) or doesn't expose these fields (unusual, but this must
    never raise -- a wrong tier guess is recoverable, a crash isn't)."""
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return os.cpu_count() or 1
    pairs: set[tuple[str, str]] = set()
    physical_id = "0"  # some CPUs omit "physical id" entirely (single-socket, always 0)
    for line in text.splitlines():
        if line.startswith("physical id"):
            physical_id = line.split(":", 1)[1].strip()
        elif line.startswith("core id"):
            pairs.add((physical_id, line.split(":", 1)[1].strip()))
    return len(pairs) or (os.cpu_count() or 1)


def _ram_gb() -> float:
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return 0.0
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            kb = int(line.split()[1])
            return kb / 1024 / 1024
    return 0.0


def _gpu() -> tuple[bool, float | None]:
    """Only recognizes an NVIDIA GPU with a working driver (nvidia-smi
    actually runs) -- an unsupported/driverless card (as found on this
    reference machine: an old GeForce 920M with no driver installed) is
    correctly reported as no usable GPU, since that's what Ollama itself
    will see too."""
    if not shutil.which("nvidia-smi"):
        return False, None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if out.returncode != 0 or not out.stdout.strip():
        return False, None
    try:
        vram_mb = float(out.stdout.strip().splitlines()[0])
    except ValueError:
        return True, None
    return True, vram_mb / 1024


def _tier(physical_cores: int, ram_gb: float, has_gpu: bool) -> str:
    if has_gpu:
        return "gpu"
    if physical_cores >= 4 and ram_gb >= 16:
        return "cpu-strong"
    return "cpu-weak"


def detect() -> Hardware:
    physical = _physical_cores()
    logical = os.cpu_count() or physical
    ram = _ram_gb()
    has_gpu, vram = _gpu()
    return Hardware(
        physical_cores=physical,
        logical_cores=logical,
        ram_gb=round(ram, 1),
        has_gpu=has_gpu,
        vram_gb=round(vram, 1) if vram is not None else None,
        tier=_tier(physical, ram, has_gpu),
    )


if __name__ == "__main__":
    print(json.dumps(asdict(detect()), indent=2))
