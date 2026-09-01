"""Base class for all agents. An agent is just a specialized system prompt
plus (optionally) its own set of MCP tools -- the LLM call itself is always
the same OllamaClient.generate(). This keeps every agent cheap: no separate
model, no separate process, just a different framing of the same local model.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from llm.ollama_client import OllamaClient


class Agent(ABC):
    name: str = "agent"
    description: str = ""

    def __init__(self, llm: OllamaClient):
        self.llm = llm

    @abstractmethod
    def system_prompt(self) -> str:
        ...

    def run(self, task: str, context: str = "") -> str:
        return self.llm.generate(self._prompt(task, context), system=self.system_prompt())

    def run_stream(self, task: str, context: str = "") -> Iterator[dict]:
        return self.llm.generate_stream(self._prompt(task, context), system=self.system_prompt())

    def _prompt(self, task: str, context: str) -> str:
        return f"{context}\n\nTASK:\n{task}" if context else task
