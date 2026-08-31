from agents.base import Agent
from agents.refactor_agent import RefactorAgent
from agents.test_agent import TestAgent


class AgentRegistry:
    """Named agents beyond the default coder (which main.py wires directly,
    since it needs the knowledge snippets). Extend by adding a module under
    agents/ and registering it here."""

    def __init__(self, llm):
        self._agents: dict[str, Agent] = {
            "test": TestAgent(llm),
            "refactor": RefactorAgent(llm),
        }

    def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def names(self) -> list[str]:
        return list(self._agents.keys())
