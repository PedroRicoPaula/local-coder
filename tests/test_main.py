import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from context.truncate import truncate_text
from llm.ollama_client import Usage


class _FakeCCE:
    def __init__(self, available=False, compress_result=None, symbol_result=None):
        self._available = available
        self._compress_result = compress_result
        self._symbol_result = symbol_result

    @property
    def available(self):
        return self._available

    def compress_file(self, path, task_description=""):
        return self._compress_result

    def get_symbol(self, path, symbol):
        return self._symbol_result


class _FakeAgent:
    """Records every run_stream() call as (task, context, kv_context) and
    replays one canned chunk-stream per call, in call order -- lets
    run_turn's hop-loop kv_context threading be tested without a real
    Ollama connection."""

    def __init__(self, chunk_streams):
        self._chunk_streams = list(chunk_streams)
        self.calls: list[tuple[str, str, list[int] | None]] = []

    def run_stream(self, task, context="", kv_context=None):
        self.calls.append((task, context, kv_context))
        return iter(self._chunk_streams.pop(0))


class TestAssembleFileContext(unittest.TestCase):
    def test_raw_read_when_cce_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("print('hi')\n")
            cce = _FakeCCE(available=False)
            context, warnings = main.assemble_file_context(cce, root, ["a.py"], budget_chars=1000)
            self.assertIn("print('hi')", context)
            self.assertEqual(warnings, [])

    def test_cce_compression_used_when_available(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("print('hi')\n")
            cce = _FakeCCE(available=True, compress_result="COMPRESSED")
            context, warnings = main.assemble_file_context(cce, root, ["a.py"], budget_chars=1000)
            self.assertIn("COMPRESSED", context)
            self.assertEqual(warnings, [])

    def test_denied_path_produces_warning_and_is_skipped(self):
        cce = _FakeCCE(available=False)
        context, warnings = main.assemble_file_context(cce, "/tmp", [".env"], budget_chars=1000)
        self.assertEqual(context, "")
        self.assertEqual(len(warnings), 1)
        self.assertIn(".env", warnings[0])

    def test_truncates_when_over_budget(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "big.py").write_text("x" * 1000)
            cce = _FakeCCE(available=False)
            context, warnings = main.assemble_file_context(cce, root, ["big.py"], budget_chars=300)
            self.assertIn("truncated to fit budget", context)
            self.assertTrue(any("truncated" in w for w in warnings))

    def test_skips_when_budget_already_exhausted(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("x" * 50)
            (Path(root) / "b.py").write_text("y" * 50)
            cce = _FakeCCE(available=False)
            context, warnings = main.assemble_file_context(cce, root, ["a.py", "b.py"], budget_chars=60)
            self.assertTrue(any("skipped entirely" in w for w in warnings))


class TestUsageFromChunk(unittest.TestCase):
    def test_full_chunk(self):
        chunk = {
            "done": True,
            "prompt_eval_count": 100,
            "eval_count": 50,
            "prompt_eval_duration": 1_000_000_000,
            "eval_duration": 2_000_000_000,
            "load_duration": 500_000_000,
            "total_duration": 3_500_000_000,
        }
        usage = Usage.from_chunk(chunk)
        self.assertEqual(usage.total_tokens, 150)

    def test_missing_fields_give_none_total(self):
        usage = Usage.from_chunk({"done": True})
        self.assertIsNone(usage.total_tokens)


class TestStreamAndPrint(unittest.TestCase):
    def test_accumulates_response_and_returns_usage(self):
        chunks = [
            {"response": "hi", "done": False},
            {"response": " there", "done": True, "prompt_eval_count": 10, "eval_count": 5, "context": [1, 2, 3]},
        ]
        with mock.patch("ui._enabled", return_value=False):
            text, usage, kv_context = main.stream_and_print(iter(chunks))
        self.assertEqual(text, "hi there")
        self.assertEqual(usage.prompt_eval_count, 10)
        self.assertEqual(usage.eval_count, 5)
        self.assertEqual(kv_context, [1, 2, 3])

    def test_empty_stream_returns_empty_text_and_no_usage(self):
        with mock.patch("ui._enabled", return_value=False):
            text, usage, kv_context = main.stream_and_print(iter([]))
        self.assertEqual(text, "")
        self.assertIsNone(usage)
        self.assertIsNone(kv_context)

    def test_done_chunk_without_context_yields_none(self):
        """Older Ollama versions (or an error path) might not include
        `context` in the final chunk -- must not crash, must not fabricate
        a value."""
        chunks = [{"response": "ok", "done": True, "prompt_eval_count": 1, "eval_count": 1}]
        with mock.patch("ui._enabled", return_value=False):
            _, _, kv_context = main.stream_and_print(iter(chunks))
        self.assertIsNone(kv_context)


def _symbol_chunk(context: list[int] | None) -> dict:
    chunk = {
        "response": "```symbol:a.py#foo\n```",
        "done": True, "prompt_eval_count": 1, "eval_count": 1,
    }
    if context is not None:
        chunk["context"] = context
    return chunk


def _final_chunk(context: list[int] | None) -> dict:
    chunk = {"response": "all done", "done": True, "prompt_eval_count": 1, "eval_count": 1}
    if context is not None:
        chunk["context"] = context
    return chunk


class TestRunTurnKvContextThreading(unittest.TestCase):
    """run_turn's hop loop must (1) accept a caller-supplied kv_context to
    seed hop 0, (2) reuse whatever hop 0 returns on later hops by sending
    only the new follow-up delta (not the whole accumulated task -- Ollama
    already has that cached), and (3) fall back cleanly to today's
    behavior (full resend, no context) whenever no cached context is
    available, including right after the budget-exceeded history-drop
    branch invalidates one."""

    def test_initial_kv_context_seeds_hop_zero_but_full_task_is_still_sent(self):
        agent = _FakeAgent([[_final_chunk([9, 9])]])
        cce = _FakeCCE(available=False)
        with mock.patch("ui._enabled", return_value=False):
            final_kv = main.run_turn(
                agent, "task", "", "/tmp", cce,
                num_ctx=8192, max_total_context_chars=100_000,
                initial_kv_context=[1, 2, 3],
            )
        task, ctx, kv = agent.calls[0]
        self.assertEqual(task, "task")
        self.assertEqual(kv, [1, 2, 3])
        self.assertEqual(final_kv, [9, 9])

    def test_followup_hop_sends_delta_only_and_reuses_returned_context(self):
        agent = _FakeAgent([[_symbol_chunk([100, 101])], [_final_chunk([200, 201])]])
        cce = _FakeCCE(available=True, symbol_result="def foo(): ...")
        with mock.patch("ui._enabled", return_value=False):
            final_kv = main.run_turn(
                agent, "do the thing", "FILE CONTEXT", "/tmp", cce,
                num_ctx=8192, max_total_context_chars=100_000,
            )
        self.assertEqual(len(agent.calls), 2)
        hop0_task, hop0_ctx, hop0_kv = agent.calls[0]
        hop1_task, hop1_ctx, hop1_kv = agent.calls[1]
        self.assertEqual(hop0_task, "do the thing")
        self.assertEqual(hop0_ctx, "FILE CONTEXT")
        self.assertIsNone(hop0_kv)
        self.assertEqual(hop1_kv, [100, 101])
        self.assertEqual(hop1_ctx, "")  # not resent -- already covered by the cached prefix
        self.assertIn("RESULT OF YOUR LAST ACTION", hop1_task)
        self.assertNotIn("do the thing", hop1_task)  # delta only, original task not repeated
        self.assertEqual(final_kv, [200, 201])

    def test_falls_back_to_full_resend_when_ollama_never_returns_context(self):
        agent = _FakeAgent([[_symbol_chunk(None)], [_final_chunk(None)]])
        cce = _FakeCCE(available=True, symbol_result="def foo(): ...")
        with mock.patch("ui._enabled", return_value=False):
            final_kv = main.run_turn(
                agent, "do the thing", "FILE CONTEXT", "/tmp", cce,
                num_ctx=8192, max_total_context_chars=100_000,
            )
        hop1_task, hop1_ctx, hop1_kv = agent.calls[1]
        self.assertIsNone(hop1_kv)
        self.assertEqual(hop1_ctx, "FILE CONTEXT")  # resent, same as pre-existing behavior
        self.assertIn("do the thing", hop1_task)      # full accumulated text, not just the delta
        self.assertIsNone(final_kv)

    def test_history_drop_branch_also_drops_the_cached_kv_context(self):
        """Once the accumulated follow-up history is dropped for exceeding
        the char budget, a cached kv_context from before the drop no
        longer matches what current_task represents -- must fall back to
        an uncached resend on the next hop rather than send a delta on top
        of a now-stale prefix."""
        symbol_result = "x" * 40
        appended_len = len(
            "\n\n--- RESULT OF YOUR LAST ACTION ---\n"
            + truncate_text(symbol_result)
            + "\n--- CONTINUE THE TASK ABOVE, USING THAT RESULT ---"
        )
        # Fits exactly one hop's growth, not two -- forces the drop-history
        # branch to trigger between hop 1 and hop 2, not hop 0 and hop 1.
        budget = len("task") + appended_len + 20

        agent = _FakeAgent([
            [_symbol_chunk([1, 1])],
            [_symbol_chunk([2, 2])],
            [_final_chunk([3, 3])],
        ])
        cce = _FakeCCE(available=True, symbol_result=symbol_result)
        with mock.patch("ui._enabled", return_value=False):
            main.run_turn(
                agent, "task", "FILECTX", "/tmp", cce,
                num_ctx=8192, max_total_context_chars=budget,
                initial_kv_context=[9, 9],
            )
        self.assertEqual(len(agent.calls), 3)
        self.assertEqual(agent.calls[0][2], [9, 9])          # hop 0: seeded as requested
        self.assertEqual(agent.calls[1][2], [1, 1])          # hop 1: reused hop 0's context, fit the budget
        self.assertEqual(agent.calls[1][1], "")              # hop 1: delta only
        self.assertIsNone(agent.calls[2][2])                 # hop 2: budget blew -- cache dropped
        self.assertEqual(agent.calls[2][1], "FILECTX")       # hop 2: fallback resends full context


class TestResolveModelConfig(unittest.TestCase):
    def _cfg(self):
        return {"model": "qwen2.5-coder:7b", "model_profiles": {
            "fast": {"model": "qwen3:4b", "num_ctx": 8192},
            "quality": {"model": "qwen2.5-coder:7b", "num_ctx": 8192},
        }}

    def test_explicit_model_flag_always_wins(self):
        args = main.parse_args(["--model", "custom:1b", "--profile", "fast"])
        with mock.patch("config.explicit_model_in_config_file", return_value=None):
            cfg = main.resolve_model_config(self._cfg(), args)
        self.assertEqual(cfg["model"], "custom:1b")

    def test_profile_applies_when_no_explicit_config_model(self):
        args = main.parse_args(["--profile", "fast"])
        with mock.patch("config.explicit_model_in_config_file", return_value=None):
            cfg = main.resolve_model_config(self._cfg(), args)
        self.assertEqual(cfg["model"], "qwen3:4b")

    def test_config_file_explicit_model_beats_profile(self):
        args = main.parse_args(["--profile", "fast"])
        with mock.patch("config.explicit_model_in_config_file", return_value="qwen2.5-coder:7b"):
            cfg = main.resolve_model_config(self._cfg(), args)
        self.assertEqual(cfg["model"], "qwen2.5-coder:7b")  # untouched -- profile ignored

    def test_unknown_profile_leaves_model_untouched(self):
        args = main.parse_args(["--profile", "quality"])
        base = self._cfg()
        del base["model_profiles"]["quality"]
        with mock.patch("config.explicit_model_in_config_file", return_value=None):
            cfg = main.resolve_model_config(base, args)
        self.assertEqual(cfg["model"], "qwen2.5-coder:7b")  # DEFAULTS value, unchanged

    def test_no_flags_leaves_config_untouched(self):
        args = main.parse_args([])
        cfg = main.resolve_model_config(self._cfg(), args)
        self.assertEqual(cfg["model"], "qwen2.5-coder:7b")


if __name__ == "__main__":
    unittest.main()
