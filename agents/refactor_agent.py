"""Stub sub-agent: refactoring. See test_agent.py for the shape this follows."""
from agents.base import Agent

SYSTEM_PROMPT = """You are a refactoring specialist. Given a file, propose the
smallest change that improves clarity, removes duplication, or fixes an
obvious inefficiency -- never a rewrite unless asked. Use the ```write:path
convention for the changed file. State what you changed and why in one or
two lines, nothing more."""


class RefactorAgent(Agent):
    name = "refactor"
    description = "Proposes small, targeted refactors to a given file."

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT
