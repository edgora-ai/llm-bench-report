import json
import os
import signal
import subprocess
from pathlib import Path
import shutil
import tempfile
import unittest

from bench import jobs


class JobTests(unittest.TestCase):
    def test_finite_worker_failure_and_private_log(self):
        # An intentionally incomplete fixture project fails preflight before
        # any Docker/model/provider request; it is not a benchmark sample.
        source = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(source / "bench", root / "bench", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "config").mkdir()
            shutil.copyfile(source / "config/bench.toml", root / "config/bench.toml")
            record = jobs.start(root, tool="opencode", models=["opencode/fixture-no-request"], task="crocodile", seed=17)
            exit_status = jobs._processes.pop(record["id"]).wait(timeout=15)
            self.assertNotEqual(exit_status, 0)
            state = jobs.status(root, record["id"])
            self.assertEqual(state["status"], "failed")
            self.assertFalse(state["process_alive"])
            log = (root / record["log"]).read_text()
            self.assertIn('"status": "failed"', log)
            self.assertNotIn('"event": "run_start"', log)
            self.assertEqual((root / "data/jobs" / record["id"]).stat().st_mode & 0o777, 0o700)
            with self.assertRaises(ValueError):
                jobs.stop(root, record["id"])

    def test_concurrency_flag_is_recorded_and_validated(self):
        source = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(source / "bench", root / "bench", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "config").mkdir()
            shutil.copyfile(source / "config/bench.toml", root / "config/bench.toml")
            record = jobs.start(root, tool="claude-code", models=["fixture-no-request"], task="crocodile", seed=5, concurrency=4)
            self.assertEqual(record["concurrency"], 4)
            self.assertIn("--concurrency", record["command"])
            self.assertEqual(record["command"][record["command"].index("--concurrency") + 1], "4")
            jobs._processes.pop(record["id"]).wait(timeout=15)

    def test_generated_status_is_not_final(self):
        # A generated session awaits evaluation; sealing it later must be allowed.
        from bench.storage import Store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3", root)
            manifest = {
                "id": "gen01", "date": "2026-09-30", "tool": "claude-code", "model": "fixture",
                "task_id": "crocodile", "task_name": "fixture", "purpose": "benchmark",
                "prompt_version": "standard-v1", "status": "generated", "generation_status": "completed",
                "archive_dir": "runs/2026-09-30/gen01", "metrics": {}, "artifacts": [],
            }
            store.upsert_run(manifest)
            evaluated = dict(manifest, status="completed", generation_status=None, checks=[{"name": "load", "status": "pass"}])
            evaluated.pop("generation_status")
            store.upsert_run(evaluated)
            self.assertEqual(store.get_run("gen01")["status"], "completed")
            with self.assertRaises(Exception):
                store.upsert_run(dict(evaluated, duration_ms=1))

    def test_stop_is_durable_graceful_and_scoped_to_its_batch(self):
        from unittest import mock
        listing = "llm-bench-aaa\nllm-bench-eval-aaa\nllm-bench-bbb\nllm-bench-eval-bbb\nother-container\n"
        job_id = "a" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "data/jobs" / job_id
            folder.mkdir(parents=True)
            batch = root / "data/batches/batch1"
            batch.mkdir(parents=True)
            (batch / "batch.json").write_text(json.dumps({"id": "batch1", "queue": [{"run_id": "aaa"}, {"status": "queued"}]}))
            record = {"id": job_id, "pid": 4242, "process_alive": True, "status": "running", "batch_id": "batch1"}
            calls = []

            def fake_run(command, **kwargs):
                self.assertTrue((folder / "cancel.json").exists())
                calls.append(command)
                return type("R", (), {"returncode": 0, "stdout": listing, "stderr": ""})()

            def fake_kill(pid, signum):
                self.assertTrue((folder / "cancel.json").exists())
                self.assertEqual((pid, signum), (4242, signal.SIGINT))

            with mock.patch.object(jobs, "status", return_value=record), \
                 mock.patch.object(jobs.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(jobs.os, "kill", side_effect=fake_kill):
                result = jobs.stop(root, job_id)
            self.assertEqual(result["stopped_containers"], ["llm-bench-aaa", "llm-bench-eval-aaa"])
            self.assertEqual(result["cleanup_errors"], [])
            stops = [command for command in calls if command[1] == "stop"]
            self.assertEqual([command[-1] for command in stops], result["stopped_containers"])
            self.assertEqual(json.loads((folder / "cancel.json").read_text())["id"], job_id)
            for command in stops:
                self.assertIn("--time", command)

    def test_reject_arbitrary_job_paths(self):
        with self.assertRaises(ValueError):
            jobs.status(Path("/tmp"), "../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
