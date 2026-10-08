"""No-model recovery validation against real temporary archives and job records."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bench import cli, jobs, recovery, runner

SOURCE = Path(__file__).resolve().parent.parent


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(SOURCE / "bench/tasks", self.root / "bench/tasks")
        self.task = json.loads((self.root / "bench/tasks/crocodile.json").read_text())
        self.parent = self.archive("parent1")

    def archive(self, run_id, model="fixture-model", **extra):
        folder = self.root / "runs/2026-10-08" / run_id
        folder.mkdir(parents=True)
        (folder / "task.json").write_text(json.dumps(self.task))
        (folder / "prompt.txt").write_text(self.task["prompt"])
        manifest = {"id": run_id, "date": "2026-10-08", "archive_dir": folder.relative_to(self.root).as_posix(),
                    "status": "failed", "archive_status": "complete", "tool": "claude-code", "model": model,
                    "purpose": "benchmark", "task_id": self.task["id"], "prompt_version": self.task["version"],
                    "prompt_hash": runner.digest(self.task["prompt"]),
                    "conditions": {"task_definition_sha256": runner.digest(self.task)},
                    # Historical parent category is accepted without being rewritten.
                    "error_category": "session_error", "error": "API Error: stream disconnected before completion",
                    "artifacts": runner.artifact_index(folder), **extra}
        (folder / "manifest.json").write_text(json.dumps(manifest))
        (folder / "checksums.json").write_text(json.dumps({a["path"]: a["sha256"] for a in manifest["artifacts"]}))
        return manifest

    def update_parent(self, **extra):
        self.parent.update(extra)
        (self.root / self.parent["archive_dir"] / "manifest.json").write_text(json.dumps(self.parent))

    def resolve(self, ids=None, **kwargs):
        return recovery.resolve_retries(self.root, {}, ids or ["parent1"], **kwargs)

    def test_resolves_exact_parent_and_lineage_without_rewriting_archive(self):
        folder = self.root / self.parent["archive_dir"]
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.assertEqual(self.resolve(), [{"tool": "claude-code", "model": "fixture-model", "task_id": "crocodile",
                                         "attempt": 1, "retry_of": "parent1", "retry_reason": self.parent["error"]}])
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_repeatable_retries_keep_ids_not_cross_product(self):
        second = self.archive("parent2", model="another-model")
        entries = self.resolve(["parent1", "parent2"])
        self.assertEqual([e["retry_of"] for e in entries], ["parent1", "parent2"])
        self.assertEqual([e["model"] for e in entries], [self.parent["model"], second["model"]])

    def test_bad_parent_states_and_nontransport_are_rejected(self):
        original = dict(self.parent)
        for fields in ({"status": "generated"}, {"status": "completed"}, {"archive_status": "integrity_error"},
                       {"error": "model gave up"}, {"attempt": 1}, {"retry_of": "older-parent"},
                       {"tool": "telepathy"}, {"prompt_version": "obsolete"}):
            with self.subTest(fields=fields):
                self.parent = dict(original)
                self.update_parent(**fields)
                with self.assertRaises(ValueError):
                    self.resolve()

    def test_checksums_and_artifact_bytes_are_both_verified(self):
        folder = self.root / self.parent["archive_dir"]
        checksums = (folder / "checksums.json").read_text()
        (folder / "checksums.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.resolve()
        (folder / "checksums.json").write_text(checksums)
        (folder / "prompt.txt").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.resolve()

    def test_symlink_parent_or_artifact_is_refused(self):
        folder = self.root / self.parent["archive_dir"]
        (folder / "prompt.txt").unlink()
        (folder / "prompt.txt").symlink_to(self.root / "bench/tasks/crocodile.json")
        with self.assertRaises(ValueError):
            self.resolve()

    def test_changed_current_task_is_rejected(self):
        task = dict(self.task, version="next-version")
        (self.root / "bench/tasks/crocodile.json").write_text(json.dumps(task))
        with self.assertRaisesRegex(ValueError, "task version"):
            self.resolve()

    def test_missing_ambiguous_and_duplicate_parent_ids_are_rejected(self):
        for ids in (["missing"], ["parent1", "parent1"], ["../parent1"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.resolve(ids)
        duplicate = self.root / "runs/2026-10-09/parent1"
        shutil.copytree(self.root / self.parent["archive_dir"], duplicate)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.resolve()

    def test_actual_child_rejects_but_queued_placeholder_does_not(self):
        batch_dir = self.root / "data/batches/old"
        batch_dir.mkdir(parents=True)
        (batch_dir / "batch.json").write_text(json.dumps({"queue": [
            {"retry_of": "parent1", "run_id": "parent1"}, {"retry_of": "parent1", "status": "queued"}]}))
        self.resolve()
        self.archive("child", model="child-model", retry_of="parent1", attempt=1)
        with self.assertRaisesRegex(ValueError, "actual child"):
            self.resolve()

    def test_historical_child_recorded_only_in_batch_is_rejected(self):
        child = self.archive("child", model="child-model")
        batch_dir = self.root / "data/batches/old"
        batch_dir.mkdir(parents=True)
        (batch_dir / "batch.json").write_text(json.dumps({"queue": [{"retry_of": "parent1", "run_id": child["id"]}]}))
        with self.assertRaisesRegex(ValueError, "actual child"):
            self.resolve()

    def test_live_claim_conflicts_self_is_excluded_and_terminal_claim_releases(self):
        with jobs.foreground_claim(self.root, ["parent1"]) as job_id:
            with self.assertRaisesRegex(ValueError, "active recovery claim"):
                self.resolve()
            self.resolve(job_id=job_id)
            jobs.write_state(self.root, job_id, "completed", batch_id="batch1")
            self.resolve()
        record = jobs.status(self.root, job_id)
        self.assertEqual(record["retry_runs"], ["parent1"])
        self.assertEqual(record["batch_id"], "batch1")
        self.assertEqual(record["status"], "completed")

    def test_pending_auto_retry_claim_in_live_original_job_is_rejected(self):
        batch_dir = self.root / "data/batches/original"
        batch_dir.mkdir(parents=True)
        (batch_dir / "batch.json").write_text(json.dumps({"id": "original", "queue": [
            {"run_id": "parent1", "retry_of": "parent1"}, {"retry_of": "parent1", "status": "queued"}]}))
        job_id = "e" * 32
        folder = self.root / "data/jobs" / job_id
        folder.mkdir(parents=True)
        jobs._write_json(folder / "job.json", {"id": job_id, "pid": os.getpid(), "foreground": True,
                        "retry_runs": [], "status": "running", **jobs._identity(os.getpid())})
        jobs.write_state(self.root, job_id, "running", batch_id="original")
        with self.assertRaisesRegex(ValueError, "active recovery claim"):
            self.resolve()
        jobs.write_state(self.root, job_id, "interrupted")
        self.resolve()  # A dead/terminal queued placeholder is still recoverable.

    def test_start_claim_is_saved_before_spawn_and_ids_are_forwarded(self):
        def spawn(command, **kwargs):
            job_id = command[command.index("--job-id") + 1]
            record = json.loads((self.root / "data/jobs" / job_id / "job.json").read_text())
            self.assertEqual(record["status"], "launching")
            self.assertEqual(record["retry_runs"], ["parent1"])
            self.assertEqual(command[command.index("--retry-run") + 1], "parent1")
            with self.assertRaisesRegex(ValueError, "active recovery claim"):
                self.resolve()
            return mock.Mock(pid=99999999)

        with mock.patch.object(jobs.subprocess, "Popen", side_effect=spawn):
            record = jobs.start(self.root, retry_runs=["parent1"])
        self.assertEqual(record["retry_runs"], ["parent1"])
        self.assertEqual(jobs.status(self.root, record["id"])["status"], "unexpected_exit")
        self.resolve()  # Dead workers do not permanently reserve a parent.

    def test_spawn_failure_releases_claim(self):
        with mock.patch.object(jobs.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            with self.assertRaises(OSError):
                jobs.start(self.root, retry_runs=["parent1"])
        self.resolve()

    def test_validation_rejects_conflicts_duplicates_tasks_tools_and_concurrency_prelaunch(self):
        bad = [dict(concurrency=0), dict(concurrency=-1), dict(concurrency=True),
               dict(tool="telepathy"), dict(models=["m"]), dict(task="missing"),
               dict(pairs=["claude-code,m,missing"]), dict(pairs=["claude-code,m,crocodile"] * 2),
               dict(pairs=["claude-code,m,crocodile"], task="crocodile"),
               dict(retry_runs=["parent1"], tool="claude-code"),
               dict(retry_runs=["parent1"], pairs=["claude-code,fixture-model,crocodile"]),
               dict(tool="claude-code", models=["m", "m"])]
        with mock.patch.object(jobs.subprocess, "Popen") as popen:
            for kwargs in bad:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    jobs.start(self.root, **kwargs)
            popen.assert_not_called()
        self.assertFalse((self.root / "data/jobs").exists())

    def test_cli_run_passes_exact_retry_ids_and_job_id_to_runner(self):
        batch = {"id": "fixture-batch", "status": "completed", "run_ids": ["child"]}
        with mock.patch.object(cli, "ROOT", self.root), mock.patch.object(cli, "load_config", return_value={}), \
             mock.patch.object(sys, "argv", ["bench", "run", "--retry-run", "parent1"]), \
             mock.patch.object(runner, "run_batch", return_value=batch) as run, \
             mock.patch.object(signal, "signal"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(run.call_args.kwargs["retry_runs"], ["parent1"])
        job_id = run.call_args.kwargs["job_id"]
        self.assertEqual(jobs.status(self.root, job_id)["status"], "completed")
        self.assertEqual(run.call_args.args[3], None)  # no default models
        self.assertEqual(run.call_args.args[7], None)  # no triple reinterpretation

    def test_cli_blocked_batch_records_failed_and_returns_nonzero(self):
        batch = {"id": "fixture-batch", "status": "blocked", "run_ids": ["child"]}
        with mock.patch.object(cli, "ROOT", self.root), mock.patch.object(cli, "load_config", return_value={}), \
             mock.patch.object(sys, "argv", ["bench", "run", "--retry-run", "parent1"]), \
             mock.patch.object(runner, "run_batch", return_value=batch) as run, \
             mock.patch.object(signal, "signal"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 1)
        job = jobs.status(self.root, run.call_args.kwargs["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["batch_status"], "blocked")
        self.assertEqual(job["batch_id"], "fixture-batch")

    def test_cli_validation_precedes_start_and_runner(self):
        cases = [["run", "--pair", "claude-code,m,crocodile", "--task", "crocodile"],
                 ["start", "--pair", "claude-code,m,missing"], ["run", "--concurrency", "0"],
                 ["start", "--retry-run", "parent1", "--model", "m", "--tool", "claude-code"]]
        with mock.patch.object(cli, "ROOT", self.root), mock.patch.object(cli, "load_config", return_value={}), \
             mock.patch.object(jobs, "start") as start, mock.patch.object(runner, "run_batch") as run, \
             contextlib.redirect_stderr(io.StringIO()):
            for args in cases:
                with self.subTest(args=args), mock.patch.object(sys, "argv", ["bench", *args]), self.assertRaises(SystemExit) as exc:
                    cli.main()
                self.assertEqual(exc.exception.code, 2)
            start.assert_not_called()
            run.assert_not_called()


class ScopedCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_id = "b" * 32
        self.folder = self.root / "data/jobs" / self.job_id
        self.folder.mkdir(parents=True)
        self.record = {"id": self.job_id, "pid": 4242, "process_alive": True, "status": "running", "batch_id": "batch1"}
        self.batch = self.root / "data/batches/batch1"
        self.batch.mkdir(parents=True)

    def write_queue(self, entries):
        (self.batch / "batch.json").write_text(json.dumps({"id": "batch1", "queue": entries}))

    def test_mismatched_container_name_is_refused_without_signal_or_docker(self):
        self.write_queue([{"run_id": "ours", "container_name": "llm-bench-someone-else"}])
        with mock.patch.object(jobs, "status", return_value=self.record), mock.patch.object(jobs.os, "kill") as kill, \
             mock.patch.object(jobs.subprocess, "run") as docker:
            with self.assertRaisesRegex(ValueError, "Container name"):
                jobs.stop(self.root, self.job_id)
            kill.assert_not_called()
            docker.assert_not_called()
        self.assertTrue((self.folder / "cancel.json").exists())

    def test_pid_identity_rechecked_before_signal(self):
        self.write_queue([])
        dead = dict(self.record, process_alive=False)
        with mock.patch.object(jobs, "status", side_effect=[self.record, dead]), \
             mock.patch.object(jobs.os, "kill") as kill, mock.patch.object(jobs.subprocess, "run") as docker:
            jobs.stop(self.root, self.job_id)
            kill.assert_not_called()
            docker.assert_not_called()

    def test_docker_failure_keeps_durable_cancellation(self):
        self.write_queue([{"run_id": "ours"}])
        result = mock.Mock(returncode=1, stdout="", stderr="fixture Docker failure")
        with mock.patch.object(jobs, "status", return_value=self.record), mock.patch.object(jobs.os, "kill"), \
             mock.patch.object(jobs.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "docker ps failed"):
                jobs.stop(self.root, self.job_id)
        self.assertTrue((self.folder / "cancel.json").exists())

    def test_stop_real_verified_worker_gets_sigint_and_unrelated_process_survives(self):
        # Real Python signal handling and /proc identity; no Docker or model call.
        worker = self.root / "worker.py"
        worker.write_text("import signal,time\nsignal.signal(signal.SIGINT, lambda *_: exit(130))\nprint('ready', flush=True)\nwhile True: time.sleep(0.1)\n")
        process = subprocess.Popen([sys.executable, str(worker), "bench.cli", "run", "--job-id", self.job_id],
                                   stdout=subprocess.PIPE, text=True)
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        self.addCleanup(lambda: unrelated.terminate() if unrelated.poll() is None else None)
        self.addCleanup(lambda: unrelated.wait(timeout=5))
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            record = dict(self.record, pid=process.pid, **jobs._identity(process.pid))
            jobs._write_json(self.folder / "job.json", record)
            self.write_queue([])
            with mock.patch.object(jobs.subprocess, "run") as docker:
                result = jobs.stop(self.root, self.job_id)
                docker.assert_not_called()
            self.assertTrue(result["cancellation_requested"])
            self.assertEqual(process.wait(timeout=5), 130)
            self.assertIsNone(unrelated.poll())
            self.assertTrue(jobs.status(self.root, self.job_id)["cancellation_requested"])
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()
            if unrelated.poll() is None:
                unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
