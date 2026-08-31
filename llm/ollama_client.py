"""HTTP client for a local Ollama server. Stdlib only (urllib) -- this
project has zero pip dependencies by design, so it runs on a bare Python
install with no internet access at all, install or run time.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str, timeout_s: int = 300, num_ctx: int = 8192):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx

    def is_up(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.host}/api/version")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def generate(self, prompt: str, system: str | None = None, think: bool = False) -> str:
        """Single-shot completion. No chat history, no tool-calling --
        deliberately simple, since tool-call schema reliability from
        qwen2.5-coder over Ollama was found to be inconsistent in practice.
        File edits are driven by parsing fenced blocks (see actions.py)
        instead of a JSON tool-call contract.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system or "",
            "think": think,
            "stream": False,
            "options": {"num_ctx": self.num_ctx},
        }
        return self._post("/api/generate", payload, response_key="response")

    def _post(self, path: str, payload: dict, response_key: str) -> str:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.host}{path}", data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.load(resp)
        except urllib.error.URLError as e:
            raise OllamaError(
                f"Could not reach Ollama at {self.host} (is 'ollama serve' running?): {e}"
            ) from e
        except TimeoutError as e:
            raise OllamaError(
                f"Ollama took longer than {self.timeout_s}s to respond. "
                "CPU-only inference is slow -- try a shorter prompt or raise request_timeout_s."
            ) from e
        if "error" in body:
            raise OllamaError(f"Ollama error: {body['error']}")
        return body.get(response_key, "")
