"""HTTP client for a local Ollama server. Stdlib only (urllib) -- this
project has zero pip dependencies by design, so it runs on a bare Python
install with no internet access at all, install or run time.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


class OllamaError(RuntimeError):
    pass


@dataclass
class Usage:
    """Ollama reports real token counts and timings on the final chunk of
    every request (streaming or not) -- this used to be read off the wire
    and thrown away. `None` fields mean the value wasn't present (an older
    Ollama version, or a response that errored before completion), never a
    misleading 0."""
    prompt_eval_count: int | None
    eval_count: int | None
    prompt_eval_duration: int | None  # nanoseconds
    eval_duration: int | None         # nanoseconds
    load_duration: int | None         # nanoseconds
    total_duration: int | None        # nanoseconds

    @classmethod
    def from_chunk(cls, chunk: dict) -> "Usage":
        return cls(
            prompt_eval_count=chunk.get("prompt_eval_count"),
            eval_count=chunk.get("eval_count"),
            prompt_eval_duration=chunk.get("prompt_eval_duration"),
            eval_duration=chunk.get("eval_duration"),
            load_duration=chunk.get("load_duration"),
            total_duration=chunk.get("total_duration"),
        )

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_eval_count is None or self.eval_count is None:
            return None
        return self.prompt_eval_count + self.eval_count


@dataclass
class GenerationResult:
    text: str
    usage: Usage | None
    context: list[int] | None = None


class OllamaClient:
    def __init__(
        self,
        host: str,
        model: str,
        timeout_s: int = 300,
        num_ctx: int = 8192,
        num_batch: int | None = None,
        num_thread: int | None = None,
    ):
        """`num_batch`/`num_thread` are per-request `options` fields, same
        as `num_ctx` -- NOT server-wide env vars. Verified directly against
        this project's own Ollama install (0.33.2): no OLLAMA_NUM_BATCH or
        OLLAMA_NUM_THREAD env var exists in the binary at all (checked via
        `strings` and the server's own logged startup config). `None`
        (the default) omits the field from the request entirely, letting
        Ollama use its own default -- matching this client's existing
        "None means don't touch it" convention for optional fields."""
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx
        self.num_batch = num_batch
        self.num_thread = num_thread

    def is_up(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.host}/api/version")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        think: bool = False,
        context: list[int] | None = None,
    ) -> GenerationResult:
        """Single-shot, non-streaming completion. Used by sub-agents that
        don't drive a live terminal display (agents/*.py) -- no reason to
        pay the streaming bookkeeping cost when nothing is watching.

        `context` is the token-ID array Ollama returned from a *previous*
        call -- passing it back is the documented workaround for Ollama's
        CPU backend otherwise never reusing KV cache across calls (see
        ollama/ollama#14780). `None` means "no cached prefix, prefill from
        scratch", which is today's behavior unchanged."""
        payload = self._payload(prompt, system, think, stream=False, context=context)
        with self._request("/api/generate", payload) as resp:
            body = json.load(resp)
        if "error" in body:
            raise OllamaError(f"Ollama error: {body['error']}")
        return GenerationResult(
            text=body.get("response", ""),
            usage=Usage.from_chunk(body),
            context=body.get("context"),
        )

    def generate_stream(
        self,
        prompt: str,
        system: str | None = None,
        think: bool = False,
        context: list[int] | None = None,
    ) -> Iterator[dict]:
        """Streaming completion for the main REPL loop: yields each decoded
        JSON chunk as Ollama emits it, so the caller can print `thinking`/
        `response` fragments live instead of sitting on a silent multi-
        minute wait -- see main.py. No tool-calling here either, same
        reasoning as generate(): file/command/fetch actions are driven by
        parsing fenced blocks out of the complete text once streaming ends
        (actions.py), not a JSON tool-call contract.

        `think` defaults to False and should stay that way for
        qwen2.5-coder: verified directly against this Ollama install that
        it doesn't just ignore `think: true` for a model with no
        extended-thinking template -- it rejects the request outright with
        HTTP 400 ("does not support thinking"). The live value of streaming
        here is seeing response tokens arrive as they're generated; it was
        never going to be a distinct reasoning trace for this model.

        `context`: see generate()'s docstring. The final chunk (`done:
        true`) carries the new context array to pass into the *next* call
        -- the caller (main.py's stream_and_print) is responsible for
        capturing it off that chunk, same as it already does for `Usage`.
        """
        payload = self._payload(prompt, system, think, stream=True, context=context)
        with self._request("/api/generate", payload) as resp:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise OllamaError(f"Ollama error: {chunk['error']}")
                yield chunk

    def _payload(
        self,
        prompt: str,
        system: str | None,
        think: bool,
        stream: bool,
        context: list[int] | None = None,
    ) -> dict:
        options = {"num_ctx": self.num_ctx}
        if self.num_batch is not None:
            options["num_batch"] = self.num_batch
        if self.num_thread is not None:
            options["num_thread"] = self.num_thread
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system or "",
            "think": think,
            "stream": stream,
            "options": options,
        }
        if context is not None:
            payload["context"] = context
        return payload

    @contextmanager
    def _request(self, path: str, payload: dict):
        """Opens the connection and hands back the live response object for
        the caller to read (once, or line by line) -- wraps both the
        connect and the read in the same error handling, since a read-phase
        failure (the far more likely one: the model is mid-generation when
        the connection dies) needs the same clean-error treatment as a
        connect-phase one.
        """
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.host}{path}", data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                yield resp
        except urllib.error.HTTPError as e:
            # A response DID come back -- Ollama is up and reachable, it
            # rejected this specific request (e.g. "does not support
            # thinking" for a model with no extended-thinking template).
            # HTTPError is a URLError subclass, so this must be caught
            # first or the generic "is Ollama running?" branch below
            # swallows it and hides the actual, fixable reason.
            body = e.read().decode(errors="replace")
            raise OllamaError(f"Ollama rejected the request ({e.code}): {body}") from e
        except urllib.error.URLError as e:
            raise OllamaError(
                f"Could not reach Ollama at {self.host} (is 'ollama serve' running?): {e}"
            ) from e
        except TimeoutError as e:
            raise OllamaError(
                f"Ollama took longer than {self.timeout_s}s to respond. "
                "CPU-only inference is slow -- try a shorter prompt or raise request_timeout_s."
            ) from e
        except (OSError, ValueError) as e:
            # Catches the rest of the ways a long-lived local connection can
            # die mid-response (connection reset, Ollama killed or OOM'd
            # while generating) or come back malformed. Without this, either
            # crashes the whole REPL loop instead of reporting one failed turn.
            raise OllamaError(f"Lost connection to Ollama mid-request: {e}") from e
