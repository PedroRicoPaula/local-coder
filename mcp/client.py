"""Generic MCP client over stdio (JSON-RPC 2.0, line-delimited).

Reusable for any MCP server, not just CCE: spawn a subprocess, speak
JSON-RPC on its stdin/stdout, keep it alive across calls. This is the same
transport Claude Code itself uses, so any server that works there works here.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, command: list[str], env: dict | None = None, cwd: str | None = None):
        self.command = command
        self.env = env
        self.cwd = cwd
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()
        self.tools: list[dict] = []

    def start(self) -> None:
        import os
        full_env = os.environ.copy()
        if self.env:
            full_env.update(self.env)
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.cwd,
            env=full_env,
            text=True,
            bufsize=1,
        )
        self._call("initialize", {})
        result = self._call("tools/list", {})
        self.tools = result.get("tools", [])

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        return result

    def _call(self, method: str, params: dict) -> dict:
        if self._proc is None:
            raise MCPError("MCP server not started; call start() first")
        with self._lock:
            self._id += 1
            req_id = self._id
            request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(request) + "\n")
            self._proc.stdin.flush()

            assert self._proc.stdout is not None
            line = self._proc.stdout.readline()
            if not line:
                raise MCPError(f"MCP server closed stdout while calling {method}")
            response = json.loads(line)
            if "error" in response:
                raise MCPError(f"{method} failed: {response['error']}")
            return response.get("result", {})

    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def load_server_configs(path: str | Path) -> dict:
    """Reads a Claude-Code-style mcp.servers.json: {"mcpServers": {name: {command, args, env}}}.
    Expands a leading ~ in "command" so the file can be committed without
    baking in one person's home directory."""
    p = Path(path)
    if not p.exists():
        return {}
    servers = json.loads(p.read_text()).get("mcpServers", {})
    for server in servers.values():
        if "command" in server:
            server["command"] = str(Path(server["command"]).expanduser())
    return servers
