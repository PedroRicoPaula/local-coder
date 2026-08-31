"""Thin wrapper around the Context-Compress-Engine MCP server.

Every file that goes into the model's context passes through here first.
CCE is local, deterministic, and near-instant (heuristic passes, no LLM) --
this is the compression step, not the Ollama call.
"""
from __future__ import annotations

from pathlib import Path

from mcp.client import MCPClient, MCPError


class CCEClient:
    def __init__(self, binary: str, project_root: str):
        self.binary = binary
        self.project_root = project_root
        self._client: MCPClient | None = None

    def start(self) -> bool:
        """Returns False (and leaves the caller to fall back to raw reads)
        if the CCE binary is missing -- this must never hard-fail the CLI."""
        if not Path(self.binary).exists():
            return False
        self._client = MCPClient(
            command=[self.binary],
            env={"CCE_ROOT": self.project_root},
            cwd=self.project_root,
        )
        try:
            self._client.start()
        except (MCPError, OSError):
            self._client = None
            return False
        return True

    def stop(self) -> None:
        if self._client:
            self._client.stop()

    @property
    def available(self) -> bool:
        return self._client is not None

    def compress_file(self, file_path: str, task_description: str = "") -> str | None:
        """Returns the compressed pack text, or None on any failure (caller
        should fall back to reading the raw file)."""
        if not self._client:
            return None
        try:
            result = self._client.call_tool(
                "compress_file",
                {"filePath": file_path, "taskDescription": task_description},
            )
            content = result.get("content", [])
            for block in content:
                if block.get("type") == "text":
                    return block["text"]
            return None
        except MCPError:
            return None

    def get_symbol(self, file_path: str, symbol: str) -> str | None:
        if not self._client:
            return None
        try:
            result = self._client.call_tool(
                "get_symbol", {"filePath": file_path, "symbol": symbol}
            )
            for block in result.get("content", []):
                if block.get("type") == "text":
                    return block["text"]
            return None
        except MCPError:
            return None
