"""Closed-loop storage and real TCP HTTP regression tests; no models invoked."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import hashlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from bench.server import COOKIE_NAME, make_server, serve
from bench.storage import METRICS, RUBRIC, RunConflict, Store, summary


def manifest(run_id="fixture-1", status="completed", cost=None, **overrides):
    result = {
        "id": run_id, "batch_id": "fixture-batch", "date": "2026-09-30", "purpose": "fixture",
        "tool": "claude-code", "model": "fixture-model", "task_id": "landing", "task_name": "Landing page",
        "prompt_version": "v1", "condition_fingerprint": "fixture-fingerprint",
        "started_at": "2026-09-30T00:00:00Z", "finished_at": "2026-09-30T00:00:01Z",
        "status": status, "error": None, "duration_ms": 1000,
        "metrics": {"input_tokens": 10, "output_tokens": 20, "cache_read_tokens": None,
                    "cache_write_tokens": None, "reasoning_tokens": None, "total_tokens": 30,
                    "cost_usd": cost, "cost_source": None, "api_duration_ms": 900, "num_turns": 1},
        "artifacts": [], "checks": [{"name": "fixture", "status": "pass", "detail": "safe"}],
        "evaluation": {"status": "unknown", "evidence": []},
        "archive_dir": f"runs/2026-09-30/{run_id}", "reviews": [],
        "extra": {"unicode": "完整内容", "needle": "searchneedle", "nested": [1, {"detail": "preserved"}]},
    }
    result.update(overrides)
    return result


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "index.sqlite"
        self.store = Store(self.db_path, self.root)

    def archive(self, data):
        directory = self.root / data["archive_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return directory

    def test_parent_id_search_returns_parent_and_retry_child(self):
        parent = manifest("parent-proof")
        child = manifest("child-proof", attempt=1, retry_of="parent-proof", retry_reason="stream disconnected")
        self.store.upsert_run(parent)
        self.store.upsert_run(child)
        with sqlite3.connect(self.db_path) as db:
            stored = json.loads(db.execute("SELECT manifest_json FROM runs WHERE id=?", (child["id"],)).fetchone()[0])
        self.assertEqual(stored["retry_of"], parent["id"])
        self.assertEqual(self.store.get_run(child["id"])["attempt"], 1)
        self.assertEqual({r["id"] for r in self.store.list_runs({"q": parent["id"]})}, {parent["id"], child["id"]})

    def test_full_json_sql_readback_and_search(self):
        data = manifest()
        self.store.upsert_run(data)
        with sqlite3.connect(self.db_path) as db:
            stored = json.loads(db.execute("SELECT manifest_json FROM runs WHERE id=?", (data["id"],)).fetchone()[0])
            self.assertEqual(stored, data)
            self.assertEqual(db.execute("SELECT tool,model,status FROM runs").fetchone(), ("claude-code", "fixture-model", "completed"))
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs_search WHERE runs_search MATCH 'searchneedle'").fetchone()[0], 1)
        self.assertEqual(self.store.get_run(data["id"]), data)
        self.assertEqual(self.store.list_runs({"q": "searchneedle"}), [data])
        self.assertEqual(self.store.list_runs({"q": "完整内容"}), [data])
        self.assertEqual(self.store.list_runs({"tool": "opencode"}), [])
        self.assertIsNone(self.store.get_run("missing"))
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        data["extra"]["nested"].append("caller mutation")
        self.assertNotEqual(self.store.get_run(data["id"]), data)

    def test_unknown_cost_and_known_subtotal_coverage(self):
        for data in (manifest("unknown"), manifest("known", cost=1.25), manifest("free", cost=0)):
            self.store.upsert_run(data)
        runs = self.store.list_runs()
        self.assertIsNone(self.store.get_run("unknown")["metrics"]["cost_usd"])
        aggregate = summary(runs)
        self.assertEqual(aggregate["cost_usd_known_subtotal"], 1.25)
        self.assertEqual(aggregate["cost_known_count"], 2)
        self.assertEqual(aggregate["cost_unknown_count"], 1)
        self.assertAlmostEqual(aggregate["cost_coverage"], 2 / 3)
        self.assertIsNone(summary([self.store.get_run("unknown")])["metrics"]["cost_usd"])
        self.assertIsNone(summary([])["cost_usd_known_subtotal"])
        self.assertEqual(summary([])["cost_coverage"], 0)

    def test_all_filters_share_same_slice(self):
        self.store.upsert_run(manifest())
        self.store.upsert_run(manifest("other", tool="opencode", model="other", task_id="game", prompt_version="v2", purpose="smoke", status="failed"))
        filters = {"q": "searchneedle", "tool": "claude-code", "model": "fixture-model", "task_id": "landing", "prompt_version": "v1", "purpose": "fixture", "status": "completed", "date_from": "2026-09-30", "date_to": "2026-09-30"}
        self.assertEqual([r["id"] for r in self.store.list_runs(filters)], ["fixture-1"])
        self.assertEqual(self.store.list_runs({"date_to": "2026-09-29"}), [])
        with self.assertRaises(ValueError):
            self.store.list_runs({"date_from": "invalid"})

    def test_q_matches_whole_substring_not_tokens_across_fields(self):
        for index in range(1, 9):
            self.store.upsert_run(manifest(f"fixture-only-{index:02d}", task_name="Only fixture", started_at="2026-09-30T10:01:00Z"))
        self.assertEqual([run["id"] for run in self.store.list_runs({"q": "fixture-only-01"})], ["fixture-only-01"])
        self.assertEqual([run["id"] for run in self.store.list_runs({"q": "TURE-ONLY-01"})], ["fixture-only-01"])
        self.assertEqual(len(self.store.list_runs({"q": "Only fixture"})), 8)
        self.assertEqual(self.store.list_runs({"q": "fixture unrelated 01"}), [])

    def test_a_finished_session_may_replace_its_own_placeholder(self):
        # A run is indexed while the CLI is still working, so the record then
        # holds no result: empty metrics, no duration. Those placeholders are
        # the absence of a measurement, not an early claim about one, and the
        # session's real values arrive when it exits. Treating that write as
        # tampering aborted a live batch mid-run.
        running = manifest(status="running")
        running["metrics"] = {}
        running["duration_ms"] = None
        running["first_model_event_ms"] = None
        running["usage_raw"] = None
        running["final_response"] = None
        self.store.upsert_run(running)

        finished = dict(running)
        finished["status"] = "generated"
        finished["metrics"] = dict(manifest()["metrics"], cost_usd=0.42, num_turns=17)
        finished["duration_ms"] = 812345
        finished["first_model_event_ms"] = 4928
        finished["usage_raw"] = {"steps": []}
        finished["final_response"] = "built the page"
        self.store.upsert_run(finished)

        stored = self.store.get_run(running["id"])
        self.assertEqual(stored["status"], "generated")
        self.assertEqual(stored["metrics"]["cost_usd"], 0.42)
        self.assertEqual(stored["duration_ms"], 812345)

    def test_evaluating_a_sealed_generation_is_not_a_revision(self):
        # A sealed run is still awaiting evaluation, and evaluation is what
        # finalises the archive: it adds source_bytes/source_file_count to the
        # metrics, writes the artifacts and checks, and advances the status.
        # Refusing that made the evaluator look like tampering and rejected
        # every sample of a batch, leaving none of them with any evidence.
        sealed = manifest(status="generated")
        sealed["generation_status"] = "completed"
        sealed["evaluation"] = {"status": "pending", "evidence": []}
        sealed["checks"] = []
        sealed["artifacts"] = []
        sealed["metrics"] = {k: v for k, v in sealed["metrics"].items()
                             if k not in ("source_bytes", "source_file_count")}
        self.store.upsert_run(sealed)

        evaluated = dict(sealed)
        evaluated["status"] = "completed"
        evaluated["archive_status"] = "complete"
        evaluated["metrics"] = dict(sealed["metrics"], source_bytes=4096, source_file_count=2)
        evaluated["artifacts"] = [{"path": "evidence/desktop.png", "size": 10, "sha256": "0" * 64}]
        evaluated["evaluation"] = {"status": "completed", "checks": [
            {"name": "entrypoint", "status": "pass", "detail": "index.html exists"}], "evidence": ["evidence/desktop.png"]}
        evaluated["checks"] = evaluated["evaluation"]["checks"]
        self.store.upsert_run(evaluated)

        stored = self.store.get_run(sealed["id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["metrics"]["source_bytes"], 4096)
        self.assertEqual(stored["evaluation"]["status"], "completed")

    def test_a_finished_result_still_cannot_be_revised(self):
        # The counterpart: once the session has ended, its measurements are a
        # fact about what the model did. Relaxing the placeholder case must not
        # open a way to restate a completed run's cost or duration. The run
        # is sealed even in "generated"; evaluation may change only derived
        # source metrics, never session cost, usage or timing.
        data = manifest(status="completed")
        data["metrics"] = dict(data["metrics"], cost_usd=1.25, num_turns=42)
        data["duration_ms"] = 5000
        self.store.upsert_run(data)
        for field, value in (("metrics", dict(data["metrics"], cost_usd=0.01)),
                             ("duration_ms", 1),
                             ("final_response", "rewritten"),
                             ("usage_raw", {"steps": [{"input": 1}]}),
                             ("first_model_event_ms", 1)):
            with self.assertRaises(RunConflict, msg=field):
                self.store.upsert_run(dict(data, **{field: value}))

    def test_identity_is_immutable_even_from_a_placeholder(self):
        # Allowing the placeholder to fill in must not allow a run to be
        # relabelled as a different model or task on the way to its result.
        running = manifest(status="running")
        running["metrics"] = {}
        self.store.upsert_run(running)
        for field, value in (("model", "swapped"), ("task_id", "blackhole"),
                             ("condition_fingerprint", "other"), ("prompt_version", "v2"),
                             ("batch_id", "other-batch"), ("started_at", "2026-10-02T00:00:00+00:00")):
            with self.assertRaises(RunConflict, msg=field):
                self.store.upsert_run(dict(running, status="generated", **{field: value}))

    def test_a_reevaluated_run_can_replace_an_infrastructure_error(self):
        # The evaluator runs after the session and can fail on its own. When a
        # fixed evaluator produces a real result, the index has to follow the
        # archive -- refusing the update left the database permanently
        # disagreeing with the authoritative file on disk, so the run looked
        # unevaluated everywhere it was read.
        broken = manifest(status="completed")
        broken["evaluation"] = {"status": "infrastructure_error",
                                "error": "TimeoutError: Page.screenshot", "checks": [], "evidence": []}
        broken["checks"] = []
        self.store.upsert_run(broken)
        self.assertEqual(self.store.get_run(broken["id"])["evaluation"]["status"], "infrastructure_error")

        fixed = dict(broken)
        fixed["evaluation"] = {"status": "completed", "checks": [
            {"name": "entrypoint", "status": "pass", "detail": "index.html exists"}], "evidence": ["evidence/desktop.png"]}
        fixed["checks"] = fixed["evaluation"]["checks"]
        self.store.upsert_run(fixed)
        stored = self.store.get_run(broken["id"])
        self.assertEqual(stored["evaluation"]["status"], "completed")
        self.assertEqual(stored["evaluation"]["evidence"], ["evidence/desktop.png"])
        # The generation result must be untouched by the re-evaluation.
        self.assertEqual(stored["metrics"], broken["metrics"])
        self.assertEqual(stored["duration_ms"], broken["duration_ms"])

    def test_a_reevaluation_cannot_launder_a_generation_fact(self):
        data = manifest(status="failed", error="provider 429")
        data["metrics"] = dict(data["metrics"], cost_usd=1.25, num_turns=42)
        self.store.upsert_run(data)
        # Opening the evaluation must not become a way to restate what the
        # session cost or produced.
        for field, value in (("error", "recovered"), ("metrics", dict(data["metrics"], cost_usd=0.0)),
                             ("duration_ms", 1), ("model", "swapped")):
            with self.assertRaises(RunConflict, msg=field):
                self.store.upsert_run(dict(data, evaluation={"status": "completed", "checks": [], "evidence": []}, **{field: value}))

    def test_import_idempotence_immutable_final_and_identity(self):
        data = manifest(status="queued")
        self.store.upsert_run(data)
        data["status"] = "running"
        self.store.upsert_run(data)
        with self.assertRaises(RunConflict):
            self.store.upsert_run(dict(data, model="changed"))
        with self.assertRaises(RunConflict):
            self.store.upsert_run(dict(data, status="queued"))
        data["status"] = "completed"
        self.store.upsert_run(data)
        self.store.upsert_run(data)
        with self.assertRaises(RunConflict):
            self.store.upsert_run(dict(data, error="changed"))
        self.assertEqual(self.store.get_run(data["id"]), data)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs_search").fetchone()[0], 1)

    def assert_persisted(self, data):
        """Check the actual row/index, complete readback and searchable snapshot."""
        with sqlite3.connect(self.db_path) as db:
            raw = db.execute("SELECT manifest_json FROM runs WHERE id=?", (data["id"],)).fetchone()[0]
            self.assertEqual(json.loads(raw), data)
            indexed = db.execute("SELECT fulljson FROM runs_search WHERE id=?", (data["id"],)).fetchall()
            self.assertEqual([json.loads(row[0]) for row in indexed], [data])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs_search WHERE id=? AND runs_search MATCH 'searchneedle'", (data["id"],)).fetchone()[0], 1)
        self.assertEqual(self.store.get_run(data["id"]), data)
        self.assertEqual(self.store.list_runs({"q": data["id"]}), [data])

    def test_prewrite_validation_is_detached_and_has_no_side_effects(self):
        data = manifest(status="running", metrics={}, duration_ms=None)
        normalized = self.store.validate_run(data)
        self.assertEqual(normalized, data)
        normalized["extra"]["nested"].append("detached")
        self.assertNotEqual(normalized, data)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs_search").fetchone()[0], 0)
        self.assertFalse((self.root / "runs").exists())
        self.store.upsert_run(data)
        sealed = dict(data, status="generated", generation_status="completed", duration_ms=500)
        self.assertEqual(self.store.validate_run(sealed), sealed)
        self.assert_persisted(data)
        self.store.upsert_run(sealed)
        # Validation is not a write reservation: upsert must check the new row.
        stale = dict(data, status="completed", duration_ms=10)
        with self.assertRaises(RunConflict):
            self.store.upsert_run(stale)
        with self.assertRaises(RunConflict):
            self.store.validate_run(stale)
        with self.assertRaises(ValueError):
            self.store.validate_run(dict(sealed, metrics={"cost_usd": -1}))
        self.assert_persisted(sealed)

    def test_full_identity_conditions_and_lineage_frozen_from_creation(self):
        data = manifest(status="running", metrics={}, prompt_hash="prompt-original",
                        conditions={"effort": {"requested": "high", "applied": "high"}},
                        requested_model="fixture-model", attempt=1, retry_of="original-run",
                        retry_reason="provider-unavailable", lineage={"parent": "original-run"})
        self.store.upsert_run(data)
        for field, value in (("prompt_hash", "changed"),
                             ("conditions", {"effort": {"requested": "high", "applied": "low"}}),
                             ("requested_model", "other"), ("attempt", 2), ("retry_of", "other"),
                             ("retry_reason", "changed"), ("lineage", {"parent": "other"}),
                             ("extra", {"needle": "laundered"})):
            for target in ("running", "generated", "completed"):
                for remove in (False, True):
                    changed = deepcopy(data)
                    changed["status"] = target
                    if remove:
                        changed.pop(field)
                    else:
                        changed[field] = value
                    with self.subTest(field=field, target=target, remove=remove):
                        with self.assertRaises(RunConflict):
                            self.store.validate_run(changed)
                        with self.assertRaises(RunConflict):
                            self.store.upsert_run(changed)
                        self.assert_persisted(data)
        with self.assertRaises(RunConflict):
            self.store.upsert_run(dict(data, new_identity_field="cannot be retrofitted"))
        # Optional lineage cannot be added later even if it was absent/null.
        plain = manifest("plain", status="running")
        self.store.upsert_run(plain)
        for field in ("prompt_hash", "conditions", "attempt", "retry_of", "retry_reason"):
            with self.subTest(added=field), self.assertRaises(RunConflict):
                self.store.upsert_run(dict(plain, **{field: None}))
        self.assert_persisted(plain)

    def test_placeholders_complete_but_all_sealed_generation_facts_freeze(self):
        data = manifest(status="queued", metrics={}, duration_ms=None, finished_at=None)
        self.store.upsert_run(data)
        running = dict(data, status="running")
        self.store.upsert_run(running)
        progress = dict(running, first_model_event_ms=2)
        self.store.upsert_run(progress)  # Forward progress need not change status.
        sealed = dict(progress, status="generated", generation_status="failed",
                      finished_at="2026-09-30T00:00:05Z", generation_finished_at="2026-09-30T00:00:05Z",
                      duration_ms=5000, exit_code=1, error="provider 429", error_category="provider",
                      final_response="delivered", reported_models=["fixture-model"],
                      backend_weights_version="weights1", cli_command=["fixture", "--effort", "high"],
                      usage_raw={"steps": [{"cost": 1.25}]},
                      metrics=dict(manifest()["metrics"], cost_usd=1.25, cost_source="provider", custom_usage=17))
        self.store.upsert_run(sealed)
        for field, value in (("duration_ms", 1), ("first_model_event_ms", 1),
                             ("exit_code", 0), ("error", None), ("error_category", "evaluation"),
                             ("final_response", "rewritten"), ("reported_models", []),
                             ("backend_weights_version", "weights2"), ("cli_command", ["other"]),
                             ("usage_raw", {}), ("finished_at", "2026-10-01T00:00:00Z"),
                             ("generation_finished_at", "2026-10-01T00:00:00Z")):
            for target in ("generated", "failed"):
                for remove in (False, True):
                    changed = deepcopy(sealed)
                    changed.update(status=target, evaluation={"status": "completed", "evidence": []})
                    if remove:
                        changed.pop(field)
                    else:
                        changed[field] = value
                    with self.subTest(field=field, target=target, remove=remove):
                        with self.assertRaises(RunConflict):
                            self.store.upsert_run(changed)
                        self.assert_persisted(sealed)
        for key in ("cost_usd", "num_turns", "cost_source", "custom_usage"):
            for remove in (False, True):
                changed = deepcopy(sealed)
                if remove:
                    changed["metrics"].pop(key)
                else:
                    changed["metrics"][key] = 0 if key != "cost_source" else "other"
                with self.subTest(metric=key, remove=remove), self.assertRaises(RunConflict):
                    self.store.upsert_run(changed)
                self.assert_persisted(sealed)
        changed = deepcopy(sealed)
        changed["metrics"]["invented_usage"] = 1
        with self.assertRaises(RunConflict):
            self.store.upsert_run(changed)
        self.assert_persisted(sealed)

    def test_status_and_generation_verdict_cannot_be_laundered(self):
        finals = ("completed", "failed", "interrupted", "unavailable", "blocked")
        for verdict in finals:
            data = manifest("sealed-" + verdict, status="generated", generation_status=verdict)
            self.store.upsert_run(data)
            for status in ("queued", "running") + tuple(v for v in finals if v != verdict):
                with self.subTest(verdict=verdict, status=status), self.assertRaises(RunConflict):
                    self.store.upsert_run(dict(data, status=status))
            for status in ("generated", verdict):
                other = next(v for v in finals if v != verdict)
                with self.subTest(verdict=verdict, replacement=other), self.assertRaises(RunConflict):
                    self.store.upsert_run(dict(data, status=status, generation_status=other))
            final = dict(data, status=verdict)
            self.store.upsert_run(final)
            for status in ("generated", "queued", "running") + tuple(v for v in finals if v != verdict):
                changed = dict(final, status=status)
                changed.pop("generation_status")
                with self.subTest(verdict=verdict, target=status), self.assertRaises(RunConflict):
                    self.store.upsert_run(changed)
            removed = dict(final)
            removed.pop("generation_status")
            with self.assertRaises(RunConflict):
                self.store.upsert_run(removed)
            self.assert_persisted(final)
        # Old evaluate_archive consumed generation_status; matching promotion is safe.
        legacy = manifest("legacy-generated", status="generated", generation_status="failed")
        self.store.upsert_run(legacy)
        promoted = dict(legacy, status="failed")
        promoted.pop("generation_status")
        self.store.upsert_run(promoted)
        self.assert_persisted(promoted)
        fallback = manifest("missing-verdict", status="generated")
        self.store.upsert_run(fallback)
        self.store.upsert_run(dict(fallback, status="completed"))
        with self.assertRaises(RunConflict):
            self.store.upsert_run(dict(fallback, status="failed"))

    def test_generation_inventory_frozen_and_derived_evaluation_whitelisted(self):
        generation = [{"path": "output/index.html", "size": 25, "sha256": "a" * 64},
                      {"path": "stdout.jsonl", "size": 10, "sha256": "b" * 64}]
        sealed = manifest(status="generated", generation_status="completed", generation_artifacts=generation)
        self.store.upsert_run(sealed)
        evaluated = deepcopy(sealed)
        evaluated.update(status="completed", archive_status="complete", evaluation_finished_at="2026-09-30T00:01:00Z",
                         evaluation={"status": "completed", "evidence": ["evidence/desktop.png"]},
                         checks=[{"name": "entrypoint", "status": "pass"}],
                         artifacts=deepcopy(generation) + [{"path": "evidence/desktop.png", "size": 10, "sha256": "c" * 64}])
        evaluated["metrics"].update(source_bytes=25, source_file_count=1)
        self.assertEqual(self.store.validate_run(evaluated), evaluated)
        self.store.upsert_run(evaluated)
        self.assert_persisted(evaluated)
        reevaluated = deepcopy(evaluated)
        reevaluated.update(evaluation_finished_at="2026-09-30T00:02:00Z", archive_status="integrity_error",
                           evaluation={"status": "infrastructure_error", "evidence": []}, checks=[],
                           artifacts=list(reversed(generation)) + [{"path": "evaluation-stderr.txt", "size": 40}])
        reevaluated["metrics"].update(source_bytes=50, source_file_count=2)
        self.store.upsert_run(reevaluated)
        self.assert_persisted(reevaluated)
        for field in ("generation_artifacts", "artifacts"):
            for key, value in (("sha256", "tampered"), ("size", 0), ("path", "output/replaced.html")):
                changed = deepcopy(reevaluated)
                changed[field][0][key] = value
                with self.subTest(field=field, key=key), self.assertRaises(RunConflict):
                    self.store.upsert_run(changed)
                self.assert_persisted(reevaluated)
        for field in ("generation_artifacts", "artifacts"):
            changed = deepcopy(reevaluated)
            changed[field] = []
            with self.subTest(removed=field), self.assertRaises(RunConflict):
                self.store.upsert_run(changed)
        changed = dict(reevaluated, evaluation_notes="not a whitelisted top-level field")
        with self.assertRaises(RunConflict):
            self.store.upsert_run(changed)
        for field in ("generation_artifacts", "artifacts"):
            changed = deepcopy(reevaluated)
            tampered = dict(changed[field][0], sha256="concealed by duplicate path")
            changed[field].insert(0, tampered)
            with self.subTest(duplicate=field), self.assertRaises(ValueError):
                self.store.upsert_run(changed)
        self.assert_persisted(reevaluated)

    def test_archive_error_is_derived_without_revising_generation_error(self):
        running = manifest(status="running", metrics={}, error=None)
        self.store.upsert_run(running)
        generated = dict(running, status="generated", generation_status="completed",
                         archive_status="integrity_error", archive_error="Unsafe output file")
        self.store.upsert_run(self.store.validate_run(generated))
        self.assert_persisted(generated)
        final = dict(generated, status="completed", archive_error="Output checksum mismatch")
        self.store.upsert_run(self.store.validate_run(final))
        self.assert_persisted(final)
        for generation_error in ("archive failure", "provider failure"):
            with self.subTest(error=generation_error), self.assertRaises(RunConflict):
                self.store.upsert_run(dict(final, error=generation_error, archive_error="updated"))
        self.assert_persisted(final)
        recovered = dict(final, archive_status="complete", evaluation={"status": "completed", "evidence": []})
        recovered.pop("archive_error")
        self.store.upsert_run(self.store.validate_run(recovered))
        self.assert_persisted(recovered)
        self.assertIsNone(recovered["error"])

    def test_rebuild_skips_conflicts_without_laundering_or_rewriting_archive(self):
        data = manifest(artifacts=[{"path": "output/index.html", "size": 25, "sha256": "a" * 64}])
        directory = self.archive(data)
        self.store.upsert_run(data)
        self.assertEqual(self.store.rebuild(), {"runs": 1, "reviews": 0, "errors": 0, "skipped": 0})
        for changed in (dict(data, status="generated"), dict(data, status="failed"),
                        dict(data, conditions={"effort": "low"}),
                        dict(data, artifacts=[{"path": "output/index.html", "size": 25, "sha256": "changed"}])):
            self.archive(changed)
            before = (directory / "manifest.json").read_bytes()
            self.assertEqual(self.store.rebuild(), {"runs": 0, "reviews": 0, "errors": 0, "skipped": 1})
            self.assertEqual((directory / "manifest.json").read_bytes(), before)
            self.assert_persisted(data)
        reevaluated = deepcopy(data)
        reevaluated.update(evaluation={"status": "completed", "evidence": ["evidence/new.png"]}, checks=[])
        reevaluated["artifacts"].append({"path": "evidence/new.png", "size": 10})
        reevaluated["metrics"]["source_bytes"] = 25
        self.archive(reevaluated)
        self.assertEqual(self.store.rebuild(), {"runs": 1, "reviews": 0, "errors": 0, "skipped": 0})
        self.assert_persisted(reevaluated)

    def test_historical_manifests_reindex_identically_and_stay_unchanged(self):
        source = Path(__file__).resolve().parent.parent
        paths = sorted((source / "runs").rglob("manifest.json"))
        if not paths:
            self.skipTest("No archived integration samples")
        before = {path: path.read_bytes() for path in paths}
        # Copy only manifests into the temporary root: no production Store/DB writes.
        records = [json.loads(raw) for raw in before.values()]
        for data in records:
            self.archive(data)
        for _ in range(2):
            self.assertEqual(self.store.rebuild(), {"runs": len(records), "reviews": 0, "errors": 0, "skipped": 0})
            for data in records:
                self.assertEqual(self.store.validate_run(data), data)
                self.store.upsert_run(data)
                with sqlite3.connect(self.db_path) as db:
                    self.assertEqual(json.loads(db.execute("SELECT manifest_json FROM runs WHERE id=?", (data["id"],)).fetchone()[0]), data)
                self.assertEqual(self.store.get_run(data["id"]), dict(data, reviews=[]))
                hits = self.store.list_runs({"q": data["id"]})
                self.assertEqual([r for r in hits if r["id"] == data["id"]], [dict(data, reviews=[])])
        for data in records:
            reevaluated = deepcopy(data)
            reevaluated.update(evaluation={"status": "completed", "evidence": ["evidence/recheck.png"]},
                               checks=[{"name": "recheck", "status": "pass"}],
                               evaluation_finished_at="2026-10-08T00:00:00Z")
            reevaluated["artifacts"] = [a for a in data["artifacts"]
                                       if not a["path"].startswith("evidence/")
                                       and a["path"] not in ("evaluation-stdout.txt", "evaluation-stderr.txt")]
            reevaluated["artifacts"].append({"path": "evidence/recheck.png", "size": 10})
            reevaluated["metrics"].update(source_bytes=123, source_file_count=1)
            self.store.upsert_run(reevaluated)
            with sqlite3.connect(self.db_path) as db:
                self.assertEqual(json.loads(db.execute("SELECT manifest_json FROM runs WHERE id=?", (data["id"],)).fetchone()[0]), reevaluated)
            self.assertEqual(self.store.get_run(data["id"]), dict(reevaluated, reviews=[]))
            hits = self.store.list_runs({"q": data["id"]})
            self.assertEqual([r for r in hits if r["id"] == data["id"]], [dict(reevaluated, reviews=[])])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_reviews_append_sql_readback_and_rebuild(self):
        data = manifest()
        directory = self.archive(data)
        self.store.upsert_run(data)
        first = self.store.add_review(data["id"], {"reviewer": "Alice", "note": "", "scores": {"motion": 3, "compliance": 4}})
        second = self.store.add_review(data["id"], {"reviewer": "Bob", "note": "Partial", "scores": {"interaction": 1.5}})
        self.assertIsNone(first["scores"]["interaction"])
        self.assertEqual(set(first["scores"]), set(RUBRIC))
        with sqlite3.connect(self.db_path) as db:
            reviews = [json.loads(row[0]) for row in db.execute("SELECT review_json FROM reviews ORDER BY created_at,id")]
            self.assertEqual(reviews, [first, second])
        self.assertEqual(self.store.get_run(data["id"])["reviews"], [first, second])
        files = list((directory / "reviews").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual(json.loads((directory / "reviews" / (first["id"] + ".json")).read_text()), first)
        self.assertEqual(json.loads((directory / "manifest.json").read_text()), data)
        rebuilt = Store(self.root / "rebuilt.sqlite", self.root)
        for _ in range(2):
            counts = rebuilt.rebuild()
            self.assertEqual(counts, {"runs": 1, "reviews": 2, "errors": 0, "skipped": 0})
            self.assertEqual(rebuilt.get_run(data["id"])["reviews"], [first, second])
            self.assertEqual(len(rebuilt.list_runs({"q": "searchneedle"})), 1)
        self.assertEqual(len(rebuilt.get_run(data["id"])["reviews"]), 2)

    def test_rebuild_corrupt_and_symlink_manifests(self):
        self.archive(manifest())
        bad = self.root / "runs/2026-09-30/bad"
        bad.mkdir(parents=True)
        (bad / "manifest.json").write_text("invalid json")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text(json.dumps(manifest("outside")))
        (self.root / "runs/2026-09-30/link").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.store.rebuild(), {"runs": 1, "reviews": 0, "errors": 1, "skipped": 0})
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_unsafe_ids_paths_and_score_validation(self):
        for run_id in ("../bad", "/abs", "a/b", "a\\b", "a..b", "", "nul\x00"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                self.store.get_run(run_id)
        for path in ("/etc/passwd", "../outside", "a/../b", "a\\b", "a\x00b", "a//b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.upsert_run(manifest(artifacts=[{"path": path}]))
        with self.assertRaises(ValueError):
            self.store.upsert_run(manifest(archive_dir="outside"))
        self.store.upsert_run(manifest())
        for payload in ({"note": "", "scores": {}}, {"reviewer": "Alice", "scores": {}}, {"reviewer": "Alice", "note": "", "scores": {"wrong": 3}}, {"reviewer": "Alice", "note": "", "scores": {"motion": True}}, {"reviewer": "Alice", "note": "", "scores": {"motion": 5}}, {"reviewer": "Alice", "note": "", "scores": {"motion": -1}}, {"reviewer": "Alice", "note": "", "scores": {"motion": float("nan")}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.store.add_review("fixture-1", payload)
        self.assertEqual(self.store.get_run("fixture-1")["reviews"], [])

    def test_thread_independent_connections(self):
        self.store.upsert_run(manifest())
        def worker(index):
            self.store.add_review("fixture-1", {"reviewer": str(index), "note": "", "scores": {"motion": index % 5}})
            return self.store.get_run("fixture-1")["id"]
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(list(executor.map(worker, range(12))), ["fixture-1"] * 12)
        self.assertEqual(len(self.store.get_run("fixture-1")["reviews"]), 12)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "web").mkdir()
        for name, value in (("index.html", "<h1>Fixture dashboard</h1>"), ("app.js", "console.log('fixture')"), ("styles.css", "body { margin: 0; }")):
            (self.root / "web" / name).write_text(value)
        (self.root / "bench/tasks").mkdir(parents=True)
        (self.root / "bench/tasks/landing.json").write_text('{"id":"landing","name":"Landing page"}')
        self.db_path = self.root / "index.sqlite"
        self.token = "unit-test-secret"
        self.server = make_server(self.root, self.db_path, "127.0.0.1", 0, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.origin = f"http://{self.host}:{self.port}"
        self.store = self.server.store
        self.data = manifest()
        self.archive = self.root / self.data["archive_dir"]
        self.archive.mkdir(parents=True)
        for path, content in (("output/index.html", b"<script>dangerous()</script>"), ("evidence/frame.png", b"\x89PNG\r\nfixture"), ("output/movie.webm", b"0123456789"), ("output/icon.svg", b"<svg onload='dangerous()'/>"), ("events.jsonl", b'{"fixture":"event"}\n')):
            file = self.archive / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
            self.data["artifacts"].append({"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "kind": "fixture"})
        self.store.upsert_run(self.data)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        conn = HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            result = (response.status, dict(response.getheaders()), response.read())
            return result
        finally:
            conn.close()

    def api(self, path, method="GET", payload=None, headers=None):
        headers = dict(headers or {})
        if payload is not None:
            headers.setdefault("Content-Type", "application/json")
        status, response_headers, body = self.request(path, method, json.dumps(payload) if payload is not None else None, headers)
        return status, response_headers, json.loads(body) if body else None

    def auth(self):
        return {"Authorization": "Bearer " + self.token}

    def login(self):
        status, headers, body = self.api("/api/login", "POST", {"token": self.token}, {"Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"authenticated": True})
        return headers["Set-Cookie"]

    def file_url(self, path):
        return "/api/runs/fixture-1/file?path=" + quote(path, safe="")

    def test_public_and_protected_get_head_security_headers(self):
        for method in ("GET", "HEAD"):
            for path in ("/", "/api/health", "/app.js", "/styles.css"):
                with self.subTest(method=method, path=path):
                    status, headers, body = self.request(path, method)
                    self.assertEqual(status, 200)
                    self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                    self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                    if method == "HEAD":
                        self.assertEqual(body, b"")
            for path in ("/api/runs", "/api/runs/fixture-1", "/api/options", "/api/tasks", "/api/export", self.file_url("output/index.html")):
                self.assertEqual(self.request(path, method)[0], 401)
        for headers in (self.auth(), {"x-api-key": self.token}):
            status, _, body = self.api("/api/runs", headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(body["runs"], [self.data])
        status, _, body = self.api("/api/runs", headers={"Authorization": "Bearer wrong"})
        self.assertEqual((status, body), (401, {"error": "Authentication required"}))

    def test_cookie_login_read_logout_and_expiry(self):
        cookie = self.login()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=28800", cookie)
        self.assertNotIn(self.token, cookie)
        cookie = cookie.split(";", 1)[0]
        headers = {"Cookie": cookie}
        self.assertEqual(self.api("/api/runs/fixture-1", headers=headers)[0], 200)
        self.assertEqual(self.api("/api/logout", "POST", {}, headers)[0], 403)
        headers["Origin"] = self.origin
        status, response_headers, _ = self.api("/api/logout", "POST", {}, headers)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", response_headers["Set-Cookie"])
        self.assertEqual(self.api("/api/runs", headers=headers)[0], 401)
        expired = self.login().split(";", 1)[0]
        session = expired.split("=", 1)[1]
        with self.server._session_lock:
            self.server._sessions[session] = time.monotonic() - 1
        self.assertEqual(self.api("/api/runs", headers={"Cookie": expired})[0], 401)
        # The token itself must never work as a session cookie.
        self.assertEqual(self.api("/api/runs", headers={"Cookie": f"{COOKIE_NAME}={self.token}"})[0], 401)

    def test_csrf_json_content_type_and_login_failure(self):
        self.assertEqual(self.api("/api/login", "POST", {"token": self.token})[0], 403)
        self.assertEqual(self.api("/api/login", "POST", {"token": "wrong"}, {"Origin": self.origin})[0], 401)
        self.assertEqual(self.api("/api/login", "POST", {"token": self.token}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/login", "POST", '{}', {"Origin": self.origin, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/login", "POST", '{', {"Origin": self.origin, "Content-Type": "application/json"})[0], 400)
        cookie = self.login().split(";", 1)[0]
        payload = {"reviewer": "Alice", "note": "", "scores": {"motion": 2}}
        self.assertEqual(self.api("/api/runs/fixture-1/reviews", "POST", payload, {"Cookie": cookie})[0], 403)
        self.assertEqual(self.api("/api/runs/fixture-1/reviews", "POST", payload, {**self.auth(), "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.api("/api/runs/fixture-1/reviews", "POST", payload, {"Cookie": cookie, "Origin": self.origin})[0], 201)
        # CLI credentials without Origin are allowed; provided malicious Origin is not.
        self.assertEqual(self.api("/api/runs/fixture-1/reviews", "POST", payload, self.auth())[0], 201)

    def test_review_validation_and_real_persistence(self):
        endpoint = "/api/runs/fixture-1/reviews"
        for score in (5, -1, True, "4"):
            status, _, _ = self.api(endpoint, "POST", {"reviewer": "Alice", "note": "", "scores": {"motion": score}}, self.auth())
            self.assertEqual(status, 400)
        status, _, body = self.api(endpoint, "POST", {"reviewer": "Alice", "note": "", "scores": {"motion": 4}}, {"x-api-key": self.token})
        self.assertEqual(status, 201)
        review = body["review"]
        with sqlite3.connect(self.db_path) as db:
            stored = json.loads(db.execute("SELECT review_json FROM reviews WHERE id=?", (review["id"],)).fetchone()[0])
            self.assertEqual(stored, review)
        self.assertEqual(json.loads((self.archive / "reviews" / (review["id"] + ".json")).read_text()), review)
        status, _, body = self.api("/api/runs/fixture-1", headers=self.auth())
        self.assertEqual(body["run"]["reviews"], [review])
        self.assertEqual(self.api("/api/runs/missing/reviews", "POST", {"reviewer": "Alice", "note": "", "scores": {}}, self.auth())[0], 404)

    def test_options_tasks_and_unknown_run(self):
        status, _, options = self.api("/api/options", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(options["tasks"], [{"id": "landing", "name": "Landing page"}])
        self.assertEqual(options["tools"], ["claude-code"])
        self.assertEqual(options["dates"], ["2026-09-30"])
        self.assertEqual(self.api("/api/tasks", headers=self.auth())[2], {"tasks": [{"id": "landing", "name": "Landing page"}]})
        self.assertEqual(self.api("/api/runs/missing", headers=self.auth())[0], 404)
        self.assertEqual(self.api("/api/runs?tool=x&tool=y", headers=self.auth())[0], 400)

    def test_export_json_csv_filter_slice_and_unknown_cost(self):
        self.store.upsert_run(manifest("other", tool="opencode", cost=1.5, task_name="=2+2"))
        status, headers, exported = self.api("/api/export?format=json&tool=claude-code", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        listed = self.api("/api/runs?tool=claude-code", headers=self.auth())[2]
        self.assertEqual(exported, listed)
        self.assertEqual(exported["summary"]["count"], 1)
        self.assertIsNone(exported["summary"]["cost_usd_known_subtotal"])
        status, headers, body = self.request("/api/export?format=csv&tool=claude-code", headers=self.auth())
        self.assertEqual(status, 200)
        rows = list(csv.DictReader(io.StringIO(body.decode())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_usd"], "")
        self.assertEqual(rows[0]["input_tokens"], "10")
        self.assertTrue(set(METRICS).issubset(rows[0]))
        _, _, body = self.request("/api/export?format=csv&tool=opencode", headers=self.auth())
        self.assertEqual(list(csv.DictReader(io.StringIO(body.decode())))[0]["task_name"], "'=2+2")
        self.assertEqual(self.api("/api/export?format=invalid", headers=self.auth())[0], 400)

    def test_artifact_types_range_and_head(self):
        for path in ("output/index.html", "output/icon.svg", "events.jsonl"):
            status, headers, _ = self.request(self.file_url(path), headers=self.auth())
            self.assertEqual(status, 200)
            self.assertIn("attachment", headers["Content-Disposition"])
            if path.endswith((".html", ".svg")):
                self.assertEqual(headers["Content-Type"], "application/octet-stream")
        status, headers, _ = self.request(self.file_url("evidence/frame.png"), headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertNotIn("Content-Disposition", headers)
        for range_header, expected in (("bytes=2-5", b"2345"), ("bytes=7-", b"789"), ("bytes=-3", b"789")):
            status, headers, body = self.request(self.file_url("output/movie.webm"), headers={**self.auth(), "Range": range_header})
            self.assertEqual(status, 206)
            self.assertEqual(body, expected)
            self.assertEqual(headers["Accept-Ranges"], "bytes")
            self.assertIn("Content-Range", headers)
        status, headers, body = self.request(self.file_url("output/movie.webm"), "HEAD", headers={**self.auth(), "Range": "bytes=2-5"})
        self.assertEqual((status, headers["Content-Length"], body), (206, "4", b""))
        for invalid in ("bytes=99-100", "bytes=5-2", "bytes=-0", "bytes=0-1,2-3", "bytes=-", "items=0-1"):
            self.assertEqual(self.request(self.file_url("output/movie.webm"), headers={**self.auth(), "Range": invalid})[0], 416)

    def test_path_traversal_symlink_and_whitelist(self):
        secret = self.root / "secret.txt"
        secret.write_text("not-readable")
        for path in ("../manifest.json", "/etc/passwd", "output/../../secret.txt", "output\\index.html", "nul\x00byte", "reviews/x.json", "manifest.json", "not-listed.txt"):
            status, _, body = self.request(self.file_url(path), headers=self.auth())
            self.assertIn(status, (400, 403, 404))
            self.assertNotIn(b"not-readable", body)
        target = self.archive / "output/index.html"
        target.unlink()
        target.symlink_to(secret)
        self.assertIn(self.request(self.file_url("output/index.html"), headers=self.auth())[0], (400, 403, 404))
        png = self.archive / "evidence/frame.png"
        png.unlink()
        (self.archive / "evidence").rmdir()
        (self.archive / "evidence").symlink_to(self.root, target_is_directory=True)
        (self.root / "frame.png").write_text("not-readable")
        self.assertIn(self.request(self.file_url("evidence/frame.png"), headers=self.auth())[0], (400, 403, 404))
        # Public static file routes are no-follow too.
        (self.root / "web/app.js").unlink()
        (self.root / "web/app.js").symlink_to(secret)
        self.assertIn(self.request("/app.js")[0], (400, 403, 404))
        self.assertIn(self.api("/api/runs/%2e%2e", headers=self.auth())[0], (400, 404))

    def test_no_token_startup_rejected(self):
        for token in ("", " ", None):
            with self.subTest(token=token), self.assertRaises(ValueError):
                make_server(self.root, self.db_path, "127.0.0.1", 0, token)
        with self.assertRaises(ValueError):
            serve(self.root, self.db_path, "127.0.0.1", 0, "")


if __name__ == "__main__":
    unittest.main()
