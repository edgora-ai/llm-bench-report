"""Deterministic contract tests for the local AGY adapter; never call the model."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench.adapters import agy  # noqa: E402
import bench.agy_runner as agy_runner  # noqa: E402


def stream(events):
    return [json.loads(line) for line in "\n".join(json.dumps(event) for event in events).splitlines()]


INIT = {"event": "init", "conversation_id": "c1",
        "init": {"model": "gemini-3.8-flash-high", "cwd": "/tmp/run/output",
                 "permission_mode": "always-proceed", "tools": ["read_file"]}}
RESULT = {"event": "result", "result": {
    "conversation_id": "c1", "status": "SUCCESS", "response": "done\n", "duration_seconds": 12.5,
    "num_turns": 3, "usage": {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 8,
                              "cache_read_tokens": 5, "total_tokens": 125}}}


class AgyCommandTests(unittest.TestCase):
    def test_command_is_prompt_attached_measured_and_isolated(self):
        argv = agy.command("gemini-3.8-flash-high", None, project="/tmp/p", log_file="/tmp/l.log", print_timeout="3h")
        self.assertEqual(argv[0:2], [agy.CLI, "-p"])
        self.assertEqual(argv[2], "<PROMPT>")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertNotIn("--continue", argv)
        self.assertNotIn("-c", argv)
        self.assertNotIn("--conversation", argv)
        self.assertNotIn("--remote-control", argv)
        self.assertEqual(argv[argv.index("--project") + 1], "/tmp/p")
        self.assertEqual(argv[argv.index("--log-file") + 1], "/tmp/l.log")
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "3h")
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--effort", argv)
        effort_argv = agy.command("gemini-3.8-flash-high", "high", project="/tmp/p", log_file="/tmp/l")
        self.assertEqual(effort_argv[effort_argv.index("--effort") + 1], "high")

    def test_command_rejects_unknown_models_and_projects(self):
        for model in ("gemin-3.8-flash", "opencode/gemini-3.8-flash-high", "gemini-3.8-flash-high\n"):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    agy.command(model, None, project="/tmp/p", log_file="/tmp/l")
        with self.assertRaises(ValueError):
            agy.command("gemini-3.8-flash-high", "maximum-typo", project="/tmp/p", log_file="/tmp/l")
        with self.assertRaises(ValueError):
            agy.command("gemini-3.8-flash-high", None, project="", log_file="/tmp/l")


class AgySummarizeTests(unittest.TestCase):
    def test_stream_json_terminal_success_is_lossless(self):
        summary = agy.summarize(stream([INIT, {"event": "step_update", "step_update": {"state": "D"}}, RESULT]))
        self.assertEqual(summary["result_status"], "completed")
        self.assertEqual(summary["final_text"], "done\n")
        self.assertEqual(summary["reported_models"], ["gemini-3.8-flash-high"])
        self.assertEqual(summary["metrics"]["input_tokens"], 100)
        self.assertEqual(summary["metrics"]["output_tokens"], 20)
        self.assertEqual(summary["metrics"]["reasoning_tokens"], 8)
        self.assertEqual(summary["metrics"]["cache_read_tokens"], 5)
        self.assertEqual(summary["metrics"]["total_tokens"], 125)
        self.assertEqual(summary["metrics"]["num_turns"], 3)
        self.assertEqual(summary["metrics"]["api_duration_ms"], 12500)
        self.assertIsNone(summary["metrics"]["cost_usd"])
        self.assertIsNone(summary["metrics"]["cost_source"])
        self.assertEqual(summary["usage_raw"]["permission_mode"], "always-proceed")

    def test_missing_usage_stays_null_and_never_zero(self):
        result = {"event": "result", "result": {"status": "SUCCESS", "response": "x"}}
        summary = agy.summarize(stream([INIT, result]))
        self.assertEqual(summary["result_status"], "completed")
        for key in ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "reasoning_tokens", "cost_usd"):
            self.assertIsNone(summary["metrics"][key], key)
        self.assertIsNone(summary["metrics"]["num_turns"])

    def test_failed_and_unknown_terminals_never_count_as_completed(self):
        failed = agy.summarize(stream([INIT, {"event": "result", "result": {"status": "FAILED", "response": ""}}]))
        self.assertEqual(failed["result_status"], "failed")
        self.assertIn("FAILED", failed["error"])
        unknown = agy.summarize(stream([INIT, {"event": "step_update"}]))
        self.assertEqual(unknown["result_status"], "unknown")
        self.assertIsNotNone(unknown["error"])
        self.assertEqual(unknown["reported_models"], ["gemini-3.8-flash-high"])
        empty = agy.summarize([])
        self.assertEqual(empty["result_status"], "unknown")
        self.assertIsNotNone(empty["error"])

    def test_error_events_fail_and_requested_model_is_never_fabricated(self):
        summary = agy.summarize(stream([{"event": "error", "message": "boom"}]))
        self.assertEqual(summary["result_status"], "failed")
        self.assertEqual(summary["error"], "boom")
        self.assertEqual(summary["reported_models"], [])
        non_dict = agy.summarize([1, "x", None, INIT])
        self.assertEqual(non_dict["result_status"], "unknown")


class AgyRunnerTests(unittest.TestCase):
    def test_sanitize_redacts_env_secrets_and_common_credential_shapes(self):
        import os
        # Compose credential-shaped samples so the repository's own source-export
        # scanner never sees a literal secret signature in this file.
        shaped = {
            "env": "super" + "secret-value-1234",
            "extra": "another-" + "secret-token",
            "sk": "sk-" + "abcdefghijklmnopqrstuvwx",
            "gh": "ghp_" + "abcdefghijklmnopqrstuvwxyz012345",
            "google": "AIza" + "Abcdefghijklmnopqrstuvwxyz012345",
            "bearer": "Bearer " + "abcdefghijklmnopqrstuvwxyz012345",
            "key": ("-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEabc\n"
                    + "-----END RSA " + "PRIVATE KEY-----"),
        }
        os.environ["OPENCODE_API_KEY"] = shaped["env"]
        try:
            sanitize = agy_runner.sanitizing_text([shaped["extra"]])
            auth_line = "Auth" + "orization: " + shaped["bearer"]
            text = "\n".join(["key " + shaped["env"], "tok " + shaped["extra"], shaped["sk"], shaped["gh"],
                              shaped["google"], auth_line, shaped["key"]])
            out = sanitize(text)
            bodies = {"env": shaped["env"], "extra": shaped["extra"], "sk": shaped["sk"][3:],
                      "gh": shaped["gh"][4:], "google": shaped["google"][4:],
                      "bearer": shaped["bearer"][len("Bearer "):], "key": "MIIEabc"}
            for label, body in bodies.items():
                with self.subTest(label=label):
                    self.assertNotIn(body, out)
            self.assertIn("[REDACTED]", out)
        finally:
            os.environ.pop("OPENCODE_API_KEY", None)

    def test_session_environment_excludes_provider_secrets(self):
        import os
        os.environ["GEMINI_API_KEY"] = "gemini-" + "secret-value-123"
        try:
            env = agy_runner.session_environment()
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertIn("PATH", env)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

    def test_storage_accepts_agy_and_manifest_records_isolation_truthfully(self):
        from bench.storage import _manifest
        manifest = {
            "id": "a" * 32, "date": "2026-10-14", "status": "generated", "purpose": "benchmark", "tool": "agy",
            "model": "gemini-3.8-flash-high", "task_id": "blackhole", "task_name": "黑洞", "prompt_version": "standard-v1",
            "archive_dir": "runs/2026-10-14/" + "a" * 32, "metrics": {}, "artifacts": [], "evaluation": {"evidence": []},
        }
        self.assertEqual(_manifest(manifest)["tool"], "agy")
        manifest["tool"] = "gemin-3.8-flash"
        with self.assertRaises(ValueError):
            _manifest(manifest)

    def test_manifest_conditions_state_local_strategy_and_unknown_effort(self):
        task = {"id": "blackhole", "name": "黑洞", "version": "standard-v1", "rubric_version": "visual-v1", "prompt": "p"}
        manifest, cli = agy_runner.build_manifest(Path(__file__).resolve().parent.parent, task, "benchmark",
                                                   "gemini-3.8-flash-high", "b" * 32, "2026-10-14", "batch", "p")
        conditions = manifest["conditions"]
        self.assertEqual(conditions["isolation"], agy_runner.STRATEGY)
        self.assertNotEqual(conditions["isolation"], "docker-network-none-unix-inference-bridge-v1")
        self.assertEqual(conditions["tool"], "agy")
        self.assertEqual(conditions["reasoning_effort_requested"], "high")
        self.assertIsNone(conditions["reasoning_effort_applied"])
        self.assertIsNone(conditions["image_id"])
        self.assertIsNone(conditions["model_directory_sha256"])
        self.assertEqual(manifest["reported_models"], [])
        self.assertNotIn("--effort", cli)
        self.assertEqual(manifest["status"], "running")
        self.assertEqual(manifest["evaluation"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()