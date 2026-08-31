"""Stub sub-agent: writes/reviews tests. Same local model, narrower framing.
Not yet wired into an autonomous orchestrator -- invoke directly for now
(see main.py's `/agent test <instruction>`); real multi-agent handoff
(coder -> test -> refactor loop) is future work, deliberately not built
ahead of evidence that the single-agent flow needs it.
"""
from agents.base import Agent

SYSTEM_PROMPT = """You are a testing specialist. Given a file or function,
write focused unit tests for it -- edge cases first, happy path last. Use
the same ```write:path fenced-block convention as the main assistant to
emit test files. Do not modify production code."""


class TestAgent(Agent):
    name = "test"
    description = "Writes unit tests for the given file or function."

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT
