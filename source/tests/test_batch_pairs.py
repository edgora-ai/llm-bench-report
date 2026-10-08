"""Tests for resuming a batch from an exact pair list.

A crashed batch leaves a queue of specific (tool, model, task) combinations.
Restarting it must run those and nothing else: the product of models and tasks
would re-generate combinations that already completed, and a benchmark whose
unit is "one session per model per task" would silently gain unplanned
samples. These tests drive the real run_batch with its I/O stubbed, so they
fail if the queue rule itself changes.
"""

import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bench import jobs, runner

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TMP = Path(tempfile.mkdtemp())

TASKS = [{"id": "crocodile", "prompt_version": "standard-v1"},
         {"id": "blackhole", "prompt_version": "standard-v1"}]


def run_batch_with_pairs(pairs, concurrency=1):
    """Invoke the real run_batch, returning the batch it built.

    Everything that costs money or touches disk is stubbed: preflight opens
    containers, run_one launches model sessions, and the manifest is written
    to a batch directory. The queue rule under test is none of that.
    """
    config = {"paths": {"database": "data/bench.sqlite3", "models_cache": "data/discovery/latest-models.json"}}
    sealed = {}

    def fake_run_one(root, cfg, store, provider, catalogue, facts, batch_id, purpose, tool, model, task, *a, **k):
        run = {"id": f"run-{tool}-{model}-{task['id']}", "status": "generated",
               "archive_dir": f"runs/2026-10-01/run-{tool}-{model}-{task['id']}"}
        sealed.setdefault("queue", []).append((tool, model, task["id"]))
        return run

    with mock.patch.object(runner, "preflight", return_value={"image_id": "img", "source_hashes": {}}), \
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
         mock.patch.object(runner, "evaluate_archive", return_value={"evaluation": {"status": "completed"}}):
        # Shuffling would hide the call order but not the membership, so the
        # queue is inspected by set of triples rather than by position.
        batch = runner.run_batch(PROJECT_ROOT, config, "benchmark", tasks=TASKS,
                                 seed=7, concurrency=concurrency, pairs=pairs)
    return batch, sealed["queue"]


class PairQueueTests(unittest.TestCase):
    def test_a_pair_list_is_the_queue_verbatim(self):
        pairs = [("claude-code", "gpt-6-luna", "blackhole"),
                 ("opencode", "opencode/big-pickle", "crocodile"),
                 ("claude-code", "kimi-k3-256k", "crocodile")]
        batch, ran = run_batch_with_pairs(pairs)
        # The batch shuffles the queue under its saved seed, so membership is
        # what a resume must preserve -- not the caller's ordering.
        self.assertEqual({(q["tool"], q["model"], q["task_id"]) for q in batch["queue"]}, set(pairs))
        self.assertEqual(len(batch["queue"]), 3, "a named pair was dropped or duplicated")
        self.assertEqual(len(ran), 3, "not every named pair produced a session")

    def test_pairs_are_not_expanded_into_the_product(self):
        # Three named pairs over two tasks would become six runs under the
        # product rule, re-running work the crashed batch already completed.
        pairs = [("claude-code", "m1", "crocodile"),
                 ("claude-code", "m2", "crocodile"),
                 ("claude-code", "m3", "blackhole")]
        batch, ran = run_batch_with_pairs(pairs)
        self.assertEqual(len(batch["queue"]), 3, "pairs were expanded into a model x task product")
        self.assertEqual(len(ran), 3, "an unplanned sample was generated")

    def test_no_pairs_keeps_the_full_product(self):
        # The recovery path must not change ordinary batches.
        models = [("claude-code", "m1"), ("opencode", "m2")]
        config = {"paths": {"database": "d", "models_cache": "c"}}
        with mock.patch.object(runner, "preflight", return_value={"image_id": "img"}), \
             mock.patch.object(runner, "connections", return_value={"claude-code": object(), "opencode": object()}), \
             mock.patch.object(runner, "discover_free", return_value=[]), \
             mock.patch.object(runner, "Store"), \
             mock.patch.object(runner.Path, "mkdir"), \
             mock.patch.object(runner.Path, "read_text", return_value=json.dumps({"opencode": {}})), \
             mock.patch.object(runner.os, "chmod"), \
             mock.patch.object(runner, "archive_policy"), \
             mock.patch.object(runner, "write_json"), \
             mock.patch.object(runner, "image_id", return_value="img"), \
             mock.patch.object(runner, "run_one", return_value={"id": "r", "status": "generated", "archive_dir": "a"}), \
             mock.patch.object(runner, "evaluate_archive", return_value={"evaluation": {}}):
            batch = runner.run_batch(PROJECT_ROOT, config, "benchmark", models=models, tasks=TASKS, seed=7, concurrency=1)
        self.assertEqual(len(batch["queue"]), 4, "the default model x task product changed")

    def test_an_unknown_task_id_is_refused_before_anything_runs(self):
        with self.assertRaises(ValueError) as raised:
            run_batch_with_pairs([("claude-code", "m", "does-not-exist")])
        self.assertIn("Unknown task id", str(raised.exception))

    def test_an_unknown_tool_is_refused_before_anything_runs(self):
        with self.assertRaises(ValueError) as raised:
            run_batch_with_pairs([("telepathy", "m", "crocodile")])
        self.assertIn("Unknown tool", str(raised.exception))


class PairParsingTests(unittest.TestCase):
    def test_pairs_parse_into_triples(self):
        from bench.cli import parse_pairs
        self.assertEqual(parse_pairs(["claude-code,gpt-6-luna,blackhole"]),
                         [("claude-code", "gpt-6-luna", "blackhole")])

    def test_a_malformed_pair_is_refused(self):
        from bench.cli import parse_pairs
        for bad in ("claude-code,gpt-6-luna", "claude-code,gpt-6-luna,blackhole,extra", "claude-code,,blackhole"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_pairs([bad])

    def test_an_unknown_tool_in_a_pair_is_refused(self):
        from bench.cli import parse_pairs
        with self.assertRaises(ValueError):
            parse_pairs(["telepathy,m,crocodile"])

    def test_no_pairs_means_no_explicit_queue(self):
        from bench.cli import parse_pairs
        self.assertEqual(parse_pairs(None), [])
        self.assertEqual(parse_pairs([]), [])


class WorkerForwardingTests(unittest.TestCase):
    def _command(self, **kwargs):
        """Capture the worker command line jobs.start assembles.

        The manifest is written for real, so the stub only replaces the child
        process: a MagicMock pid would not be serialisable into job.json.
        """
        shutil.copytree(PROJECT_ROOT / "bench/tasks", TMP / "bench/tasks", dirs_exist_ok=True)
        popen = mock.MagicMock()
        popen.pid = 424242
        with mock.patch.object(jobs.subprocess, "Popen", return_value=popen) as patched:
            jobs.start(str(TMP), purpose="benchmark", concurrency=4, **kwargs)
        return patched.call_args[0][0]

    def test_start_forwards_every_pair_to_the_worker(self):
        pairs = [("claude-code", "gpt-6-luna", "blackhole"),
                 ("opencode", "opencode/big-pickle", "crocodile")]
        command = self._command(pairs=pairs)
        for tool, model, task in pairs:
            self.assertIn(f"{tool},{model},{task}", command,
                          "an exact queue entry never reached the worker")

    def test_start_forwards_pairs_given_as_cli_strings(self):
        # This is the shape the CLI actually passes, since argparse hands back
        # the raw "tool,model,task" text. A launcher that only understood
        # tuples would fail on every real invocation.
        command = self._command(pairs=["claude-code,gpt-6-luna,blackhole",
                                       "opencode,opencode/big-pickle,crocodile"])
        self.assertIn("claude-code,gpt-6-luna,blackhole", command)
        self.assertIn("opencode,opencode/big-pickle,crocodile", command)

    def test_start_without_pairs_sends_no_pair_flag(self):
        self.assertNotIn("--pair", self._command())


if __name__ == "__main__":
    unittest.main()
