"""Base class for all agents. An agent is just a specialized system prompt
plus (optionally) its own set of MCP tools -- the LLM call itself is always
the same OllamaClient.generate(). This keeps every agent cheap: no separate
model, no separate process, just a different framing of the same local model.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from llm.ollama_client import GenerationResult, OllamaClient


class Agent(ABC):
    name: str = "agent"
    description: str = ""

    def __init__(self, llm: OllamaClient):
        self.llm = llm

    @abstractmethod
    def system_prompt(self) -> str:
        ...

    def run(self, task: str, context: str = "", kv_context: list[int] | None = None) -> GenerationResult:
        return self.llm.generate(
            self._prompt(task, context), system=self._system_for(kv_context), context=kv_context
        )

    def run_stream(self, task: str, context: str = "", kv_context: list[int] | None = None) -> Iterator[dict]:
        return self.llm.generate_stream(
            self._prompt(task, context), system=self._system_for(kv_context), context=kv_context
        )

    def _system_for(self, kv_context: list[int] | None) -> str | None:
        """Omit `system` once a cached `kv_context` is being reused -- it's
        already baked into that context from the call that produced it.
        Resending it on top would either duplicate the system block ahead
        of the new prompt tokens or break the prefix match Ollama relies on
        to skip re-evaluating cached tokens, defeating the whole point."""
        return self.system_prompt() if kv_context is None else None

    def _prompt(self, task: str, context: str) -> str:
        return f"{context}\n\nTASK:\n{task}" if context else task
