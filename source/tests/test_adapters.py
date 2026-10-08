"""Frozen protocol fixtures; no model calls or writes to benchmark runs."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from bench.adapters import claude_code, opencode
from bench.discovery import CLAUDE_MODELS, discover_free


CLAUDE_RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "result": "done",
    "usage": {"input_tokens": 100, "output_tokens": 20,
              "cache_read_input_tokens": 30, "cache_creation_input_tokens": 5,
              "reasoning_tokens": 7},
    "modelUsage": {"gpt-6.1-sol": {"inputTokens": 100, "outputTokens": 20},
                   "helper-model": {"inputTokens": 4, "outputTokens": 2}},
    "total_cost_usd": 0, "duration_ms": 2000, "duration_api_ms": 1700,
    "num_turns": 2, "permission_denials": [],
}
OPEN_STEP = {
    "type": "step_finish", "timestamp": 123, "sessionID": "sess_fixture",
    "part": {"id": "part_one", "messageID": "msg_one", "type": "step-finish",
             "reason": "stop", "tokens": {"total": 155, "input": 100,
                 "output": 20, "reasoning": 7, "cache": {"read": 30, "write": 5}},
             "cost": 0.25},
}
MODELS_CACHE = {"opencode": {"models": {
    "big-pickle": {"id": "big-pickle", "name": "Big Pickle",
                   "cost": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                   "limit": {"context": 200000, "output": 32000}},
    "plain-zero": {"cost": {"input": 0, "output": 0}},
    "old-free": {"status": "deprecated", "cost": {"input": 0, "output": 0}},
    "old-flag": {"deprecated": True, "cost": {"input": 0, "output": 0}},
    "paid-free": {"cost": {"input": 1, "output": 0}},
    "paid-cache": {"cost": {"input": 0, "output": 0, "cache_write": 1}},
    "paid-read": {"cost": {"input": 0, "output": 0, "cache_read": 1}},
    "unknown": {"cost": {"output": 0}},
    "null-cache": {"cost": {"input": 0, "output": 0, "cache_read": None}},
    "false-free": {"cost": {"input": False, "output": 0}},
}}}


class CommandTests(unittest.TestCase):
    def test_claude_exact_allowlist_and_isolation(self):
        for model in CLAUDE_MODELS:
            args = claude_code.command(model)
            self.assertEqual(args[0], "/usr/local/bin/claude")
            self.assertEqual(args[args.index("--model") + 1], model)
            expected = "Read,Write,Edit,Bash,Glob,Grep"
            self.assertEqual(args[args.index("--tools") + 1], expected)
            self.assertEqual(args[args.index("--allowedTools") + 1], expected)
            self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(args[args.index("--setting-sources") + 1], "")
            self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]), {"mcpServers": {}})
            settings = json.loads(args[args.index("--settings") + 1])
            self.assertEqual(settings["permissions"]["allow"], expected.split(","))
            self.assertEqual(set(settings["permissions"]["deny"]), {
                "Agent", "Task", "TaskOutput", "TaskStop", "WebFetch", "WebSearch",
                "Skill", "ToolSearch", "mcp__*"})
            self.assertTrue(settings["disableAllHooks"])
            self.assertFalse(settings["enableAllProjectMcpServers"])
            self.assertFalse(settings["sandbox"]["enabled"])
            self.assertTrue(settings["sandbox"]["autoAllowBashIfSandboxed"])
            self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
            for flag in ("-p", "--verbose", "--no-session-persistence", "--safe-mode",
                         "--restricted", "--strict-mcp-config"):
                self.assertIn(flag, args)
            self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
            for fragment in ("budget", "turn", "fallback", "dangerously"):
                self.assertFalse(any(fragment in arg for arg in args if arg.startswith("--")))

    def test_opencode_new_pure_stdin_session(self):
        self.assertEqual(opencode.command("opencode/big-pickle"), [
            "/usr/local/bin/opencode", "run", "--model", "opencode/big-pickle",
            "--format", "json", "--dir", "/workspace/output", "--pure",
            "--title", "llm-bench-fixed-session"])
        args = opencode.command("opencode/big-pickle")
        self.assertEqual(args[args.index("--title") + 1], "llm-bench-fixed-session")
        self.assertEqual(args.count("--title"), 1)
        for flag in ("-c", "-s", "--continue", "--session", "--attach"):
            self.assertNotIn(flag, args)

    def test_invalid_models(self):
        for model in ("", " ", "--help", None):
            with self.assertRaises(ValueError):
                claude_code.command(model)
        for model in ("plain", "/model", "provider/", "--bad/model", None):
            with self.assertRaises(ValueError):
                opencode.command(model)

    def test_target_model_ids(self):
        self.assertEqual(CLAUDE_MODELS, ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra",
                         "gpt-6-luna", "deepseek-flash", "glm-5.3-flash", "kimi-k3-256k"])


class ClaudeTests(unittest.TestCase):
    def test_empty_usage_is_null(self):
        for events in ([], [{"type": "result", "usage": {}}]):
            summary = claude_code.summarize(events)
            self.assertTrue(all(value is None for value in summary["metrics"].values()))
            self.assertEqual(summary["result_status"], "unknown")

    def test_final_cumulative_not_added_to_assistants_or_model_usage(self):
        events = [{"type": "assistant", "message": {"model": "gpt-6.1-sol",
                   "usage": {"input_tokens": 400, "output_tokens": 200}}},
                  copy.deepcopy(CLAUDE_RESULT), copy.deepcopy(CLAUDE_RESULT)]
        summary = claude_code.summarize(events)
        m = summary["metrics"]
        self.assertEqual(m["input_tokens"], 100)
        self.assertEqual(m["input_total"], 135)
        self.assertEqual(m["output_tokens"], 20)
        self.assertEqual(m["cache_read_tokens"], 30)
        self.assertEqual(m["cache_write_tokens"], 5)
        self.assertEqual(m["reasoning_tokens"], 7)
        self.assertEqual(m["total_tokens"], 155)
        self.assertEqual(m["num_turns"], 2)
        self.assertEqual(m["api_duration_ms"], 1700)
        self.assertEqual(summary["usage_raw"]["duration_ms"], 2000)
        self.assertEqual(summary["reported_models"], ["gpt-6.1-sol", "helper-model"])
        self.assertEqual(summary["result_status"], "completed")
        self.assertEqual(summary["final_text"], "done")

    def test_empty_result_does_not_use_assistant_usage(self):
        s = claude_code.summarize([{"type": "assistant", "message": {
            "usage": {"input_tokens": 10, "output_tokens": 2}}},
            {"type": "result", "subtype": "success", "usage": {}}])
        self.assertIsNone(s["metrics"]["input_tokens"])

    def test_zero_cost_unverified_and_missing_cost_null(self):
        s = claude_code.summarize([CLAUDE_RESULT])
        self.assertEqual(s["metrics"]["cost_usd"], 0)
        self.assertEqual(s["metrics"]["cost_source"], "cli_reported_unverified")
        result = copy.deepcopy(CLAUDE_RESULT)
        del result["total_cost_usd"]
        s = claude_code.summarize([result])
        self.assertIsNone(s["metrics"]["cost_usd"])
        self.assertIsNone(s["metrics"]["cost_source"])

    def test_fail_error_and_permission_metadata(self):
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "errors": ["authentication failed"], "permission_denials": [{"tool": "Agent"}]}
        s = claude_code.summarize([result])
        self.assertEqual(s["result_status"], "failed")
        self.assertEqual(s["error"], "authentication failed")
        self.assertEqual(s["usage_raw"]["permission_denials"], [{"tool": "Agent"}])

    def test_no_result_partial_and_final_text(self):
        s = claude_code.summarize([{"type": "assistant", "message": {"model": "reported",
            "usage": {"input_tokens": 4, "output_tokens": 2},
            "content": [{"type": "text", "text": "answer"}, {"type": "tool_use"}]}}])
        self.assertEqual(s["final_text"], "answer")
        self.assertEqual(s["metrics"]["total_tokens"], 6)
        self.assertIsNone(s["metrics"]["cache_read_tokens"])
        self.assertEqual(s["result_status"], "unknown")
        self.assertEqual(s["reported_models"], ["reported"])

    def test_error_event_overrides_success(self):
        s = claude_code.summarize([CLAUDE_RESULT, {"type": "error", "error": "failed"}])
        self.assertEqual(s["result_status"], "failed")
        self.assertEqual(s["error"], "failed")


class OpenCodeTests(unittest.TestCase):
    def test_empty_usage_is_null(self):
        self.assertTrue(all(v is None for v in opencode.summarize([])["metrics"].values()))
        s = opencode.summarize([{"type": "step_finish", "part": {"id": "empty", "tokens": {}}}])
        for key in ("input_tokens", "total_tokens", "cost_usd", "cost_source"):
            self.assertIsNone(s["metrics"][key])

    def test_cache_reasoning_and_provider_total(self):
        s = opencode.summarize([OPEN_STEP])
        m = s["metrics"]
        self.assertEqual((m["input_tokens"], m["input_total"], m["output_tokens"]), (100, 135, 20))
        self.assertEqual((m["cache_read_tokens"], m["cache_write_tokens"]), (30, 5))
        self.assertEqual(m["reasoning_tokens"], 7)
        self.assertEqual(m["total_tokens"], 155)
        self.assertEqual(m["cost_usd"], 0.25)
        self.assertEqual(s["result_status"], "completed")
        self.assertIsNone(m["api_duration_ms"])
        step = copy.deepcopy(OPEN_STEP)
        step["part"]["tokens"]["total"] = 162
        self.assertEqual(opencode.summarize([step])["metrics"]["total_tokens"], 162)

    def test_duplicate_steps_and_distinct_steps(self):
        second = copy.deepcopy(OPEN_STEP)
        second["part"]["id"] = "part_two"
        s = opencode.summarize([OPEN_STEP, copy.deepcopy(OPEN_STEP), second, second])
        self.assertEqual(s["metrics"]["total_tokens"], 310)
        self.assertEqual(s["metrics"]["cost_usd"], 0.5)
        self.assertEqual(s["metrics"]["num_turns"], 2)
        self.assertEqual(len(s["usage_raw"]["steps"]), 2)

    def test_missing_id_uses_message_and_index(self):
        step = copy.deepcopy(OPEN_STEP)
        del step["part"]["id"]
        step["part"]["index"] = 0
        second = copy.deepcopy(step)
        second["part"]["index"] = 1
        s = opencode.summarize([step, step, second])
        self.assertEqual(s["metrics"]["num_turns"], 2)
        del step["part"]["index"]
        self.assertEqual(opencode.summarize([step, step])["metrics"]["num_turns"], 2)

    def test_same_id_different_session_is_distinct(self):
        second = copy.deepcopy(OPEN_STEP)
        second["sessionID"] = "other"
        self.assertEqual(opencode.summarize([OPEN_STEP, second])["metrics"]["num_turns"], 2)

    def test_missing_cost_never_assumes_zero_or_partial_total(self):
        step = copy.deepcopy(OPEN_STEP)
        del step["part"]["cost"]
        s = opencode.summarize([step])
        self.assertIsNone(s["metrics"]["cost_usd"])
        step["part"]["id"] = "second"
        s = opencode.summarize([OPEN_STEP, step])
        self.assertIsNone(s["metrics"]["cost_usd"])
        self.assertIsNone(s["metrics"]["cost_source"])
        step["part"]["cost"] = 0
        self.assertEqual(opencode.summarize([step])["metrics"]["cost_usd"], 0)

    def test_total_fallback_does_not_double_count_reasoning(self):
        step = copy.deepcopy(OPEN_STEP)
        del step["part"]["tokens"]["total"]
        self.assertEqual(opencode.summarize([step])["metrics"]["total_tokens"], 155)

    def test_text_parts_updates_and_model_metadata(self):
        s = opencode.summarize([
            {"type": "text", "part": {"id": "text1", "text": "old"}},
            {"type": "text", "part": {"id": "text1", "text": "new"}},
            {"type": "text", "part": {"id": "text2", "text": "tail"}},
            {"type": "step_start", "model": {"providerID": "opencode", "modelID": "big-pickle"}},
            OPEN_STEP])
        self.assertEqual(s["final_text"], "new\ntail")
        self.assertEqual(s["reported_models"], ["opencode/big-pickle"])

    def test_error_event_and_report(self):
        for event in ({"type": "error", "error": {"name": "APIError", "data": {"message": "429"}}},
                      {"type": "report", "is_error": True, "error": "429"}):
            s = opencode.summarize([OPEN_STEP, event])
            self.assertEqual(s["result_status"], "failed")
            self.assertEqual(s["error"], "429")

    def test_nonterminal_step_is_unknown(self):
        step = copy.deepcopy(OPEN_STEP)
        step["part"]["reason"] = "tool-calls"
        self.assertEqual(opencode.summarize([step])["result_status"], "unknown")

    def test_summarize_does_not_mutate_fixtures(self):
        before = copy.deepcopy(OPEN_STEP)
        opencode.summarize([OPEN_STEP])
        self.assertEqual(OPEN_STEP, before)


class DiscoveryTests(unittest.TestCase):
    def discover(self, data):
        with tempfile.TemporaryDirectory(prefix="adapter-fixture-") as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            # Fixture write -> direct file read -> parser -> candidate lookup.
            self.assertEqual(json.loads(path.read_text()), data)
            return discover_free(path)

    def test_metadata_not_suffix_and_candidate_status(self):
        rows = self.discover(MODELS_CACHE)
        self.assertEqual([row["id"] for row in rows], ["opencode/big-pickle", "opencode/plain-zero"])
        self.assertEqual(rows[0]["model_id"], "big-pickle")
        self.assertTrue(all(row["id"].startswith("opencode/") for row in rows))
        for row in rows:
            self.assertEqual(opencode.command(row["id"])[3], row["id"])
        self.assertTrue(all(row["status"] == "candidate" for row in rows))
        self.assertEqual(rows[0]["label"], "Big Pickle")
        self.assertEqual(rows[0]["provider"], "opencode")
        self.assertEqual(rows[0]["source"], "models.json")
        self.assertEqual(rows[0]["limit"]["context"], 200000)

    def test_wrapped_and_list_cache_structures(self):
        for key in ("provider", "providers"):
            self.assertEqual(len(self.discover({key: MODELS_CACHE})), 2)
        data = {"provider": {"opencode": {"models": [
            {"id": "no-suffix", "cost": {"input": 0, "output": 0}}]}}}
        self.assertEqual(self.discover(data)[0]["id"], "opencode/no-suffix")

    def test_batch_queue_drops_withdrawn_models_but_keeps_observed_failures(self):
        # A model leaving the comparison set is an operator decision. One bad
        # session is not: kimi-k3-256k emits the same unrecognized_model line
        # and then completes normally, so that line must not remove a route.
        from bench.discovery import (EXCLUDED_CLAUDE_MODELS, OBSERVED_EMPTY_CLAUDE_MODELS,
                                     claude_model_queue)
        queue = claude_model_queue()
        self.assertTrue(queue, "queue must not be empty after filtering")
        for model in EXCLUDED_CLAUDE_MODELS:
            self.assertNotIn(model, queue)
        # Every excluded route carries the reason it was excluded.
        for model in EXCLUDED_CLAUDE_MODELS:
            self.assertIn(model, CLAUDE_MODELS, model)
        # The observation record is a note, not a filter: it must not be able
        # to remove a route on its own, or a client upgrade could never restore
        # one without someone editing the filter as well.
        self.assertTrue(OBSERVED_EMPTY_CLAUDE_MODELS)
        for model in OBSERVED_EMPTY_CLAUDE_MODELS:
            self.assertIsInstance(OBSERVED_EMPTY_CLAUDE_MODELS[model], str)
            self.assertTrue(OBSERVED_EMPTY_CLAUDE_MODELS[model].strip())
        # kimi-k3-256k has a full output record and must stay in the queue.
        self.assertIn("kimi-k3-256k", queue)
        self.assertEqual(queue, [m for m in CLAUDE_MODELS if m in set(queue)])

    def test_refreshed_cache_ids_are_command_compatible(self):
        path = Path(__file__).resolve().parents[1] / "data/discovery/latest-models.json"
        if not path.is_file():
            self.skipTest("main runner discovery snapshot is not present")
        rows = discover_free(path)
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row["id"].startswith("opencode/") for row in rows))
        for row in rows:
            self.assertEqual(row["id"], f"opencode/{row['model_id']}")
            self.assertEqual(opencode.command(row["id"])[3], row["id"])

    def test_missing_and_invalid_cache_is_visible(self):
        with tempfile.TemporaryDirectory(prefix="adapter-fixture-") as directory:
            path = Path(directory) / "models.json"
            with self.assertRaises(FileNotFoundError):
                discover_free(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                discover_free(path)
        with self.assertRaises(ValueError):
            self.discover({"opencode": {"models": "invalid"}})


if __name__ == "__main__":
    unittest.main()
