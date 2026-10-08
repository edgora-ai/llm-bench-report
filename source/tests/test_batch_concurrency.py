"""Exercise the real scheduler, durable ledger, cancellation and sealing paths."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from bench import runner


class BatchSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        cache = self.root / "models.json"
        cache.write_text('{"opencode": {}}')
        self.config = {"paths": {"database": "index.sqlite", "models_cache": str(cache)}}
        self.task = {"id": "crocodile", "prompt_version": "standard-v1"}
        self.runs = {}
        self.evaluated = []
        for name, value in {
            "preflight": {"image_id": "img"}, "policy_hashes": {},
            "connections": {"claude-code": object()}, "discover_free": [], "image_id": "img",
        }.items():
            patch = self.stack.enter_context(mock.patch.object(runner, name, return_value=value))
            setattr(self, name + "_mock", patch)
        self.stack.enter_context(mock.patch.object(runner, "archive_policy"))
        self.stack.enter_context(mock.patch.object(runner, "Store"))
        self.worker = self.stack.enter_context(mock.patch.object(runner, "run_one", side_effect=self.generate))
        self.evaluator = self.stack.enter_context(mock.patch.object(runner, "evaluate_archive", side_effect=self.evaluate))

    def generate(self, root, config, store, provider, catalogue, facts, batch_id, purpose, tool, model, task, defer, **metadata):
        run_id = metadata["run_id"]
        run = {"id": run_id, "status": "generated", "generation_status": "completed",
               "archive_dir": f"runs/fixture/{run_id}", "model": model, "task_id": task["id"],
               "attempt": metadata["attempt"], "retry_of": metadata["retry_of"]}
        self.runs[run_id] = run
        return run

    def evaluate(self, config, store, archive, task_id, run_id):
        self.evaluated.append(run_id)
        return {**self.runs[run_id], "status": self.runs[run_id]["generation_status"],
                "evaluation": {"status": "completed", "checks": [], "evidence": []}}

    def batch(self, count=2, concurrency=2, **kwargs):
        return runner.run_batch(self.root, self.config, tasks=[self.task], seed=7, concurrency=concurrency,
                                pairs=[("claude-code", f"m{i}", "crocodile") for i in range(count)], **kwargs)

    def saved(self):
        paths = list((self.root / "data/batches").glob("*/batch.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_capacity_and_every_completed_run_recorded_once(self):
        original_write = runner.write_json
        observed = []

        def write(path, value):
            if Path(path).name == "batch.json":
                observed.append(sum(entry["status"] == "running" for entry in value["queue"]))
            original_write(path, value)

        with mock.patch.object(runner, "write_json", side_effect=write):
            result = self.batch(count=8, concurrency=4)
        saved = self.saved()
        self.assertLessEqual(max(observed), 4)
        self.assertEqual(len(result["run_ids"]), 8)
        self.assertEqual(set(result["run_ids"]), set(self.evaluated))
        self.assertEqual(saved, result)
        self.assertTrue(all(e["status"] == "completed" for e in saved["queue"]))
        self.assertEqual(len(self.evaluated), len(set(self.evaluated)))

    def test_serial_capacity_and_pinned_image(self):
        result = self.batch(count=4, concurrency=1)
        self.assertEqual(len(result["run_ids"]), 4)
        self.assertEqual(result["status"], "completed")
        for call in self.worker.call_args_list:
            self.assertEqual(call.args[1]["runtime"]["image"], "img")
        for call in self.evaluator.call_args_list:
            self.assertEqual(call.args[0]["runtime"]["image"], "img")

    def test_raising_worker_does_not_lose_successful_sibling(self):
        barrier = threading.Barrier(2)

        def worker(*args, **kwargs):
            barrier.wait(timeout=5)
            if args[9] == "m0":
                raise RuntimeError("fixture worker failed")
            return self.generate(*args, **kwargs)

        self.worker.side_effect = worker
        with self.assertRaisesRegex(RuntimeError, "fixture worker failed"):
            self.batch()
        saved = self.saved()
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(len(saved["run_ids"]), 1)
        self.assertEqual(self.evaluated, saved["run_ids"])
        self.assertEqual(sum("worker_error" in e for e in saved["queue"]), 1)

    def test_freeze_failure_preserves_generated_sibling_without_changed_policy_evaluation(self):
        self.policy_hashes_mock.side_effect = [{}, {}, {"changed": "hash"}, {"changed": "hash"}]
        with self.assertRaisesRegex(RuntimeError, "source policy changed"):
            self.batch(count=3)
        saved = self.saved()
        self.assertEqual(len(saved["run_ids"]), 1)
        self.assertEqual(saved["queue"][0]["status"], "generated")
        self.assertIn("evaluation_error", saved["queue"][0])
        self.assertEqual(self.evaluated, [])
        self.assertEqual(self.worker.call_count, 1)

    def test_keyboard_interrupt_cancels_futures_not_integer_indices(self):
        started = threading.Event()
        original_wait = runner.wait
        calls = 0

        def worker(*args, **kwargs):
            started.set()
            kwargs["cancel_event"].wait(timeout=5)
            return self.generate(*args, **kwargs)

        def interrupt_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertTrue(started.wait(timeout=5))
                raise KeyboardInterrupt
            return original_wait(*args, **kwargs)

        self.worker.side_effect = worker
        with mock.patch.object(runner, "wait", side_effect=interrupt_once), \
             mock.patch.object(runner.subprocess, "run") as stop:
            with self.assertRaises(KeyboardInterrupt):
                self.batch()
        saved = self.saved()
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(set(saved["run_ids"]), set(self.evaluated))
        expected = {"llm-bench-" + e["run_id"] for e in saved["queue"]}
        self.assertTrue(stop.called)
        self.assertTrue(all(call.args[0][-1] in expected for call in stop.call_args_list))

    def test_evaluation_exception_does_not_stop_other_archive_evaluation(self):
        def evaluate(*args):
            if not self.evaluated:
                self.evaluated.append(args[-1])
                raise OSError("fixture evaluation failed")
            return self.evaluate(*args)

        self.evaluator.side_effect = evaluate
        with self.assertRaisesRegex(OSError, "fixture evaluation failed"):
            self.batch()
        saved = self.saved()
        self.assertEqual(len(self.evaluated), 2)
        self.assertEqual(len(saved["run_ids"]), 2)
        self.assertEqual(sum(e["status"] == "completed" for e in saved["queue"]), 1)

    def test_invalid_concurrency_does_not_preflight_or_submit(self):
        for concurrency in (0, -1, True):
            with self.assertRaises(ValueError):
                self.batch(concurrency=concurrency)
        self.preflight_mock.assert_not_called()
        self.worker.assert_not_called()

    def test_cross_batch_retry_is_already_attempt_one(self):
        from bench import recovery
        entry = {"tool": "claude-code", "model": "old", "task_id": "crocodile",
                 "attempt": 1, "retry_of": "old-parent", "retry_reason": "stream disconnected"}
        generate = self.generate

        def worker(*args, **kwargs):
            run = generate(*args, **kwargs)
            run.update({"generation_status": "failed", "error_category": "transport_interrupted", "error": "stream disconnected"})
            return run

        self.worker.side_effect = worker
        with mock.patch.object(recovery, "resolve_retries", return_value=[entry]):
            batch = runner.run_batch(self.root, self.config, tasks=[self.task], concurrency=4,
                                     pairs=[], retry_runs=["old-parent"])
        self.assertEqual(self.worker.call_count, 1)
        self.assertEqual(batch["queue"][0]["attempt"], 1)
        self.assertEqual(batch["queue"][0]["retry_of"], "old-parent")
        self.assertEqual(next(iter(self.runs.values()))["retry_of"], "old-parent")


class AtomicWriteTests(unittest.TestCase):
    def test_concurrent_writes_leave_complete_json_and_no_temporaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.json"
            barrier = threading.Barrier(8)
            errors = []

            def writer(index):
                try:
                    barrier.wait(timeout=5)
                    for n in range(10):
                        runner.write_json(path, {"run_ids": [f"run{index}-{n}-{j}" for j in range(50)]})
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(json.loads(path.read_text())["run_ids"]), 50)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
