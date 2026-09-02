"""Coverage for the `context` (KV-cache reuse) plumbing added to
OllamaClient -- this had zero direct unit coverage before, only exercised
indirectly through the live end-to-end test. Uses OllamaClient's own
_payload() directly (no network) and constructs GenerationResult/streaming
bodies by hand to check parsing, matching TestUsageFromChunk's existing
style in tests/test_main.py.
"""
import json
import unittest
from unittest import mock

from llm.ollama_client import GenerationResult, OllamaClient, OllamaError, Usage


class TestPayloadContext(unittest.TestCase):
    def setUp(self):
        self.client = OllamaClient("http://127.0.0.1:11434", "qwen2.5-coder:7b")

    def test_context_key_omitted_when_none(self):
        payload = self.client._payload("prompt", "system", think=False, stream=False, context=None)
        self.assertNotIn("context", payload)

    def test_context_key_included_when_given(self):
        payload = self.client._payload("prompt", "system", think=False, stream=False, context=[1, 2, 3])
        self.assertEqual(payload["context"], [1, 2, 3])

    def test_context_defaults_to_omitted(self):
        """The pre-existing 4-positional-arg call shape (no context) must
        keep working unchanged -- this is what every call site used before
        this feature existed."""
        payload = self.client._payload("prompt", "system", False, False)
        self.assertNotIn("context", payload)


class TestPayloadNumBatchNumThread(unittest.TestCase):
    """num_batch/num_thread are per-request `options` fields (verified
    against the actual Ollama binary -- no such env var exists), not env
    vars like OLLAMA_NUM_PARALLEL -- confirm they land in the right place
    and stay opt-in (None omits them, matching Ollama's own default)."""

    def test_omitted_from_options_when_none(self):
        client = OllamaClient("http://127.0.0.1:11434", "qwen2.5-coder:7b")
        options = client._payload("p", None, False, False)["options"]
        self.assertNotIn("num_batch", options)
        self.assertNotIn("num_thread", options)

    def test_included_in_options_when_set(self):
        client = OllamaClient(
            "http://127.0.0.1:11434", "qwen2.5-coder:7b", num_batch=2048, num_thread=2
        )
        options = client._payload("p", None, False, False)["options"]
        self.assertEqual(options["num_batch"], 2048)
        self.assertEqual(options["num_thread"], 2)
        self.assertEqual(options["num_ctx"], client.num_ctx)  # untouched, still present


class TestGenerateContext(unittest.TestCase):
    def setUp(self):
        self.client = OllamaClient("http://127.0.0.1:11434", "qwen2.5-coder:7b")

    def _fake_response(self, body: dict):
        payload = json.dumps(body).encode()

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return payload

        return _Resp()

    def test_generate_returns_context_from_body(self):
        body = {"response": "hi", "done": True, "context": [10, 20, 30]}
        with mock.patch("json.load", return_value=body), mock.patch.object(
            self.client, "_request", return_value=self._fake_response(body)
        ):
            result = self.client.generate("prompt")
        self.assertIsInstance(result, GenerationResult)
        self.assertEqual(result.context, [10, 20, 30])

    def test_generate_context_none_when_absent(self):
        body = {"response": "hi", "done": True}
        with mock.patch("json.load", return_value=body), mock.patch.object(
            self.client, "_request", return_value=self._fake_response(body)
        ):
            result = self.client.generate("prompt")
        self.assertIsNone(result.context)

    def test_generate_still_raises_on_ollama_error_body(self):
        body = {"error": "model not found"}
        with mock.patch("json.load", return_value=body), mock.patch.object(
            self.client, "_request", return_value=self._fake_response(body)
        ):
            with self.assertRaises(OllamaError):
                self.client.generate("prompt")


class TestGenerationResultDefaults(unittest.TestCase):
    def test_context_defaults_to_none(self):
        """Older call sites that only pass text/usage (none currently exist
        in this codebase, but the dataclass default must stay safe) don't
        need to know about `context`."""
        result = GenerationResult(text="hi", usage=Usage.from_chunk({}))
        self.assertIsNone(result.context)


if __name__ == "__main__":
    unittest.main()
