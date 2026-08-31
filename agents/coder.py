from agents.base import Agent
from llm.prompts import build_system_prompt


class CoderAgent(Agent):
    """The default, general-purpose coding agent driving the main REPL."""
    name = "coder"
    description = "General coding: read, write, and modify files per instruction."

    def __init__(self, llm, knowledge_snippets: list[str]):
        super().__init__(llm)
        self._system_prompt = build_system_prompt(knowledge_snippets)

    def system_prompt(self) -> str:
        return self._system_prompt
