"""Offline dry-run of the whole AGY run flow with a fake process.

Every case runs in a throwaway project root with its own SQLite database, so no
model is called and no real archive or database is touched. The point is to
prove that the manifest this runner freezes actually survives storage's
immutability rules and the seal transition, which is where the real failures were.
"""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, "/home/ubuntu/llm-bench")
sys.path.insert(0, "/home/ubuntu/llm-bench/tests")
import bench.agy_runner as agy_runner  # noqa: E402
import bench.runner as runner_module  # noqa: E402
from bench.storage import Store  # noqa: E402

INIT = {"event": "init", "conversation_id": "c1",
        "init": {"model": "gemini-3.8-flash-high", "cwd": "/workspace",
                 "permission_mode": "always-proceed", "tools": []}}
RESULT = {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "done\n",
        "duration_seconds": 1.5, "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 2,
        "thinking_tokens": 1, "cache_read_tokens": 0, "total_tokens": 12}}}
TRUNCATED = {"event": "step_update", "step_update": {"state": "D"}}


class FakeProcess:
    """Mimics only what run_agy actually calls on a Popen."""

    def __init__(self, script, argv, cwd, returncode=0, stderr_lines=("agy stderr line\n",)):
        self.argv = argv
        self.cwd = cwd
        self.returncode = returncode
        self.stdout = iter([json.dumps(line) + "\n" for line in script])
        self.stderr = iter(list(stderr_lines))

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class AgyFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bench-agy-flow-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "bench/tasks").mkdir(parents=True)
        (self.root / "runtime").mkdir()
        (self.root / "data").mkdir()
        (self.root / "bench/egress.py").write_bytes(
            Path("/home/ubuntu/llm-bench/bench/egress.py").read_bytes())
        for name in ("blackhole.json", "crocodile.json"):
            shutil.copyfile(f"/home/ubuntu/llm-bench/bench/tasks/{name}", self.root / "bench/tasks" / name)
        self.db = self.root / "data/bench.sqlite3"
        with Store(self.db, self.root).connection():
            pass

    def run_flow(self, script, *, exit_code=0, writes=(), log=None, stderr_lines=("agy stderr line\n",)):
        """Run the real flow with a fake process and a stubbed external evaluation."""
        captured = {}

        def fake_popen(argv, **kwargs):
            if "cwd" not in kwargs:
                # Not the model session (for example the --version probe).
                return real_popen(argv, **kwargs)
            captured["argv"] = argv
            captured["cwd"] = kwargs["cwd"]
            captured["env"] = kwargs["env"]
            for name, content in writes:
                Path(kwargs["cwd"], name).write_text(content)
            if log is not None:
                path = Path(argv[argv.index("--log-file") + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(log)
            return FakeProcess(script, argv, kwargs["cwd"], exit_code, stderr_lines)

        def fake_evaluate(config, store, run_dir, task_id, run_id):
            # Skip only the Docker evaluation; keep the real seal transition so
            # artifact verification and checksums.json are actually exercised.
            manifest = json.loads((run_dir / "manifest.json").read_text())
            captured["evaluated"] = task_id
            if manifest["status"] == "generated":
                manifest["status"] = manifest["generation_status"]
            return runner_module.seal(store, run_dir, manifest)

        real_popen = subprocess.Popen
        real_evaluate = runner_module.evaluate_archive
        real_stop = agy_runner.stop_process_group
        subprocess.Popen = fake_popen
        runner_module.evaluate_archive = fake_evaluate
        agy_runner.stop_process_group = lambda process: captured.setdefault("stopped", True)
        try:
            manifest = agy_runner.run_agy(self.root, "benchmark", "blackhole", "gemini-3.8-flash-high")
        finally:
            subprocess.Popen = real_popen
            runner_module.evaluate_archive = real_evaluate
            agy_runner.stop_process_group = real_stop
        captured["manifest"] = manifest
        # The runner dates each run with today's UTC date; derive it from the
        # manifest instead of hardcoding a calendar day.
        captured["run_dir"] = self.root / manifest["archive_dir"]
        return captured

    def stored(self, run_id):
        con = sqlite3.connect(self.db)
        try:
            row = con.execute("SELECT manifest_json FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row else None

    def test_completed_session_is_stored_sealed_and_consistent_everywhere(self):
        result = self.run_flow([INIT, RESULT], writes=[("index.html", "<html>run</html>")])
        manifest = result["manifest"]
        run_dir = result["run_dir"]
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["generation_status"], "completed")
        self.assertEqual(result["evaluated"], "blackhole")
        self.assertTrue(result["stopped"])
        argv = result["argv"]
        self.assertEqual(argv[0:2], ["/home/ubuntu/.local/bin/agy", "-p"])
        self.assertEqual(argv[2], json.loads(
            (self.root / "bench/tasks/blackhole.json").read_text())["prompt"])
        self.assertNotIn("<PROMPT>", argv)
        for forbidden in ("--continue", "-c", "--conversation", "--effort", "--remote-control"):
            self.assertNotIn(forbidden, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertEqual(Path(result["cwd"]).name, "output")
        self.assertEqual(manifest["reported_models"], ["gemini-3.8-flash-high"])
        self.assertEqual(manifest["metrics"]["input_tokens"], 10)
        self.assertEqual(manifest["metrics"]["output_tokens"], 2)
        self.assertEqual(manifest["metrics"]["reasoning_tokens"], 1)
        self.assertEqual(manifest["metrics"]["total_tokens"], 12)
        self.assertEqual(manifest["metrics"]["num_turns"], 2)
        self.assertEqual(manifest["metrics"]["api_duration_ms"], 1500)
        self.assertIsNone(manifest["metrics"]["cost_usd"])
        self.assertEqual(manifest["agy_facts"]["stdout_non_json_lines"], 0)
        self.assertEqual(manifest["agy_facts"]["agy_project_residue"], [])
        self.assertEqual(manifest["agy_facts"]["agy_cwd"], "output")
        self.assertEqual(manifest["agy_facts"]["agy_project_dir"], "agy-project")
        # No absolute host path may be recorded anywhere in the manifest. The
        # canonical archive_dir is the one required run-identity field; every
        # other location must stay run-relative.
        blob = json.dumps({key: value for key, value in manifest.items()
                           if key not in {"archive_dir", "id"}}, ensure_ascii=False)
        for forbidden in (str(self.root), "runs/2026", "/home/", "/workspace", "127.0.0.1"):
            self.assertNotIn(forbidden, blob)
        self.assertRegex(manifest["archive_dir"], rf"^runs/\d{{4}}-\d{{2}}-\d{{2}}/{manifest['id']}$")
        self.assertEqual(manifest["condition_fingerprint"], runner_module.digest(manifest["conditions"]))
        self.assertEqual((run_dir / "final.txt").read_text(), "done\n")
        self.assertFalse((run_dir / "agy-project").exists())
        paths = [a["path"] for a in manifest["generation_artifacts"]]
        self.assertIn("output/index.html", paths)
        self.assertNotIn("agy-project/anything", paths)
        self.assertEqual(json.loads((run_dir / "checksums.json").read_text()),
                         {a["path"]: a["sha256"] for a in manifest["generation_artifacts"]})
        stored = self.stored(manifest["id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["condition_fingerprint"], manifest["condition_fingerprint"])
        self.assertEqual(stored["conditions"], manifest["conditions"])

    def test_truncated_stream_never_counts_as_completed(self):
        result = self.run_flow([INIT, TRUNCATED], writes=[("index.html", "<html>run</html>")])
        manifest = result["manifest"]
        self.assertNotEqual(manifest["generation_status"], "completed")
        self.assertIn(manifest["generation_status"], {"failed", "blocked"})
        self.assertIsNotNone(manifest["error"])
        self.assertEqual(self.stored(manifest["id"])["generation_status"], manifest["generation_status"])

    def test_nonzero_exit_is_not_completed_even_with_a_success_status(self):
        result = self.run_flow([INIT, RESULT], exit_code=3, writes=[("index.html", "<html>run</html>")])
        manifest = result["manifest"]
        self.assertNotEqual(manifest["generation_status"], "completed")
        self.assertIsNotNone(manifest["error"])

    def test_non_json_stdout_lines_are_counted_not_silently_dropped(self):
        real_popen = subprocess.Popen

        def fake_popen(argv, **kwargs):
            if "cwd" not in kwargs:
                return real_popen(argv, **kwargs)
            Path(kwargs["cwd"], "index.html").write_text("<html>run</html>")
            process = FakeProcess([INIT, RESULT], argv, kwargs["cwd"])
            process.stdout = iter([json.dumps(INIT) + "\n", "warning: not json\n", json.dumps(RESULT) + "\n"])
            return process

        def fake_evaluate(config, store, run_dir, task_id, run_id):
            manifest = json.loads((run_dir / "manifest.json").read_text())
            manifest["status"] = manifest["generation_status"]
            manifest["artifacts"] = manifest["generation_artifacts"]
            return manifest

        real_evaluate = runner_module.evaluate_archive
        real_stop = agy_runner.stop_process_group
        subprocess.Popen = fake_popen
        runner_module.evaluate_archive = fake_evaluate
        agy_runner.stop_process_group = lambda process: None
        try:
            manifest = agy_runner.run_agy(self.root, "benchmark", "blackhole", "gemini-3.8-flash-high")
        finally:
            subprocess.Popen = real_popen
            runner_module.evaluate_archive = real_evaluate
            agy_runner.stop_process_group = real_stop
        self.assertEqual(manifest["generation_status"], "completed")
        self.assertEqual(manifest["agy_facts"]["stdout_non_json_lines"], 1)
        run_dir = self.root / manifest["archive_dir"]
        self.assertIn("not json", (run_dir / "stdout.jsonl").read_text())

    def test_cli_log_is_sanitized_before_it_is_hashed(self):
        log = "start\nkey sk-" + "abcdefghijklmnopqrstuvwxyz0123456789\nend\n"
        result = self.run_flow([INIT, RESULT], writes=[("index.html", "<html>run</html>")], log=log)
        run_dir = result["run_dir"]
        text = (run_dir / "agy-cli.log").read_text()
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", text)
        hashed = {a["path"]: a["sha256"] for a in result["manifest"]["generation_artifacts"]}
        self.assertIn("agy-cli.log", hashed)
        import hashlib
        self.assertEqual(hashlib.sha256((run_dir / "agy-cli.log").read_bytes()).hexdigest(), hashed["agy-cli.log"])

    def test_unknown_model_is_refused_before_any_directory_or_process(self):
        before = sorted(p.name for p in self.root.iterdir())
        with self.assertRaises(ValueError):
            agy_runner.run_agy(self.root, "benchmark", "blackhole", "gemin-3.8-flash")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        self.assertFalse((self.root / "runs").exists())


if __name__ == "__main__":
    unittest.main()