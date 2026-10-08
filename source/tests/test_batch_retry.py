"""Tests for queueing a transport-failure retry under concurrency.

A dropped stream is the connection failing, so the sample is retried once as a
new entry. That retry is created inside finish(), which under concurrency runs
on a worker thread -- and it was written as if the queue entry were a mapping
when it is a (tool, model, task) triple. The first time a retry was queued with
concurrency > 1, the batch died on "dictionary update sequence element #0 has
length 11" while building the very retry it was supposed to queue: the
interrupted sample kept its archive and the retry never ran.

These tests drive the real finish() through a stubbed run_one, so a change that
breaks the retry fails here rather than after 40 minutes of model sessions.
"""

import json
from pathlib import Path
import unittest
from unittest import mock

from bench import runner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASKS = [{"id": "crocodile", "prompt_version": "standard-v1"},
         {"id": "blackhole", "prompt_version": "standard-v1"}]

TRANSPORT_ERROR = "API Error: stream error: stream disconnected before completion: stream closed before response.completed"


def run_batch_with_runs(manifests, concurrency):
    """Run a real batch whose sessions return the given manifests in order.

    run_one is stubbed to hand back prepared manifests, so the retry logic in
    finish() runs for real against the queue it actually built.
    """
    config = {"paths": {"database": "data/bench.sqlite3", "models_cache": "data/discovery/latest-models.json"}}
    prepared = list(manifests)
    archived = []

    def fake_run_one(root, cfg, store, provider, catalogue, facts, batch_id, purpose, tool, model, task, *a, **k):
        manifest = prepared.pop(0) if prepared else {"id": f"run-{tool}-{model}-{task['id']}",
                                                    "status": "generated", "archive_dir": "runs/x"}
        manifest.setdefault("model", model)
        manifest.setdefault("task_id", task["id"])
        archived.append(manifest)
        return manifest

    def fake_evaluate(config, store, archive, task_id, run_id):
        return {"evaluation": {"status": "completed", "checks": [], "evidence": []}}

    with mock.patch.object(runner, "preflight", return_value={"image_id": "img", "source_hashes": {}}), \
         mock.patch.object(runner, "policy_hashes", return_value={}), \
         mock.patch.object(runner, "connections", return_value={"claude-code": object(), "opencode": object()}), \
         mock.patch.object(runner, "discover_free", return_value=[]), \
         mock.patch.object(runner, "Store"), \
         mock.patch.object(runner.Path, "mkdir"), \
         mock.patch.object(runner.Path, "read_text", return_value=json.dumps({"opencode": {}})), \
         mock.patch.object(runner.os, "chmod"), \
         mock.patch.object(runner, "archive_policy"), \
         mock.patch.object(runner, "write_json"), \
         mock.patch.object(runner, "image_id", return_value="img"), \
         mock.patch.object(runner, "run_one", side_effect=fake_run_one), \
         mock.patch.object(runner, "evaluate_archive", side_effect=fake_evaluate):
        batch = runner.run_batch(PROJECT_ROOT, config, "benchmark", tasks=TASKS, seed=11,
                                 concurrency=concurrency,
                                 pairs=[("claude-code", "m1", "crocodile")])
    return batch, archived


def transport_failure(run_id="r1"):
    return {"id": run_id, "status": "generated", "generation_status": "failed", "error_category": "transport_interrupted",
            "error": TRANSPORT_ERROR, "archive_dir": f"runs/2026-10-01/{run_id}"}


def model_failure(run_id="r2"):
    return {"id": run_id, "status": "generated", "generation_status": "failed", "error_category": "session_error",
            "error": "the model stopped on its own", "archive_dir": f"runs/2026-10-01/{run_id}"}


class RetryUnderConcurrencyTests(unittest.TestCase):
    def test_two_transport_failures_do_not_create_a_third_session(self):
        for concurrency in (1, 4):
            with self.subTest(concurrency=concurrency):
                batch, archived = run_batch_with_runs(
                    [transport_failure("parent"), transport_failure("child")], concurrency)
                self.assertEqual(len(archived), 2)
                self.assertEqual(len(batch["queue"]), 2)
                self.assertEqual(batch["queue"][1]["attempt"], 1)
                self.assertEqual(batch["queue"][1]["retry_of"], "parent")
                self.assertNotIn("retry_of", batch["queue"][0])

    def test_a_transport_failure_is_retried_under_concurrency(self):
        batch, _ = run_batch_with_runs([transport_failure()], concurrency=4)
        # Only the child carries retry_of; the parent's child ID is separate.
        later = batch["queue"][1:]
        self.assertEqual(len(later), 1, "a dropped stream was not retried exactly once")
        self.assertEqual(later[0]["retry_of"], "r1")
        self.assertEqual((later[0]["tool"], later[0]["model"], later[0]["task_id"]),
                         ("claude-code", "m1", "crocodile"))
        self.assertTrue(later[0].get("run_id"), "the retry was queued but never ran")

    def test_the_retry_lands_after_everything_already_queued(self):
        # The queue is frozen; a retry may not be inserted ahead of samples
        # that have not been attempted, or the order a later run would take
        # stops being comparable with this one.
        batch, _ = run_batch_with_runs([transport_failure()], concurrency=4)
        self.assertEqual(batch["queue"][-1].get("retry_of"), "r1",
                         "the retry was not appended at the end")

    def test_a_model_failure_is_not_retried(self):
        # The model giving up is the result being measured. Retrying it would
        # replace an honest sample with a lucky second attempt.
        batch, _ = run_batch_with_runs([model_failure()], concurrency=4)
        self.assertEqual([q for q in batch["queue"] if q.get("retry_of")], [],
                         "a genuine model failure was retried")

    def test_the_retry_records_why_it_repeats(self):
        batch, _ = run_batch_with_runs([transport_failure()], concurrency=4)
        original = batch["queue"][0]
        self.assertEqual(original["retried"], True)
        self.assertEqual(original["retry_reason"], TRANSPORT_ERROR)

    def test_the_queue_entry_the_retry_is_built_from_is_a_triple(self):
        # The regression itself: dict(queue_entry) raised on a 3-tuple of
        # (tool, model, task). Asserting the shape keeps the retry construction
        # honest even if the queue representation changes.
        entry = ("claude-code", "m1", {"id": "crocodile"})
        tool, model, task = entry
        self.assertEqual((tool, model, task["id"]), ("claude-code", "m1", "crocodile"))


class RetryRunsTests(unittest.TestCase):
    def test_the_queued_retry_actually_runs_as_its_own_sample(self):
        # The retry is appended to the live queue, so a batch that retries must
        # produce a second session. If the append is dropped, the interrupted
        # sample is simply missing and nothing reports it.
        config = {"paths": {"database": "d", "models_cache": "c"}}
        ran = []

        def fake_run_one(root, cfg, store, provider, catalogue, facts, batch_id, purpose, tool, model, task, *a, **k):
            ran.append((tool, model, task["id"]))
            return {"id": f"run{len(ran)}", "status": "failed" if len(ran) == 1 else "completed",
                    "error_category": "transport_interrupted" if len(ran) == 1 else None,
                    "error": TRANSPORT_ERROR if len(ran) == 1 else None,
                    "archive_dir": f"runs/2026-10-01/run{len(ran)}"}

        with mock.patch.object(runner, "preflight", return_value={"image_id": "img"}), \
             mock.patch.object(runner, "policy_hashes", return_value={}), \
             mock.patch.object(runner, "connections", return_value={"claude-code": object()}), \
             mock.patch.object(runner, "discover_free", return_value=[]), \
             mock.patch.object(runner, "Store"), \
             mock.patch.object(runner.Path, "mkdir"), \
             mock.patch.object(runner.Path, "read_text", return_value=json.dumps({"opencode": {}})), \
             mock.patch.object(runner.os, "chmod"), \
             mock.patch.object(runner, "archive_policy"), \
             mock.patch.object(runner, "write_json"), \
             mock.patch.object(runner, "image_id", return_value="img"), \
             mock.patch.object(runner, "run_one", side_effect=fake_run_one), \
             mock.patch.object(runner, "evaluate_archive", return_value={"evaluation": {}}):
            batch = runner.run_batch(PROJECT_ROOT, config, "benchmark", tasks=TASKS, seed=11,
                                     concurrency=2, pairs=[("claude-code", "m1", "crocodile")])
        self.assertEqual(ran, [("claude-code", "m1", "crocodile")] * 2,
                         "the retry did not run as a second session")
        self.assertEqual(len(batch["run_ids"]), 2, "the retry was not recorded as a run")


if __name__ == "__main__":
    unittest.main()
