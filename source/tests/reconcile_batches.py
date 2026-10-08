"""Reconcile closed batch ledgers from checksummed archives and worker events.

Run with PYTHONPATH=. python3 tests/reconcile_batches.py [--apply].
Original ledgers are retained in data/recovery/batch-ledgers; run archives never change.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path

from bench.cli import ROOT
from bench.jobs import status
from bench.runner import artifact_index, utcnow, write_json


def reconcile(root, apply=False, batch_ids=None):
    root = Path(root)
    runs = defaultdict(list)
    for path in (root / "runs").glob("*/*/manifest.json"):
        run = json.loads(path.read_text())
        if run.get("status") in {"running", "queued", "generated"}:
            continue
        runs[run.get("batch_id")].append((path, run))
    sources = defaultdict(list)
    active = set()
    for folder in (root / "data/jobs").glob("*"):
        if not folder.is_dir() or not (folder / "job.json").is_file():
            continue
        record = status(root, folder.name)
        if record["process_alive"] and record.get("batch_id"):
            active.add(record["batch_id"])
        log = folder / "worker.jsonl"
        if not log.is_file():
            continue
        batch_id = None
        for number, line in enumerate(log.read_text().splitlines(), 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "batch_start":
                batch_id = event.get("batch_id")
            if batch_id and event.get("event") in {"run_start", "run_generated", "run_end"}:
                sources[(batch_id, event.get("id"))].append({"worker_log": log.relative_to(root).as_posix(),
                    "line": number, "event": event["event"]})
    reports = []
    for path in sorted((root / "data/batches").glob("*/batch.json")):
        original = json.loads(path.read_text())
        batch_id = original["id"]
        if batch_id in active or (batch_ids and batch_id not in batch_ids):
            continue
        batch = json.loads(json.dumps(original))
        updated = []
        for manifest_path, run in runs.get(batch_id, []):
            evidence = sources.get((batch_id, run["id"]), [])
            if not evidence:
                raise ValueError(f"Cannot reconcile {run['id']}: no matching batch worker event")
            actual = {a["path"]: a["sha256"] for a in artifact_index(manifest_path.parent)}
            expected = {a["path"]: a["sha256"] for a in run["artifacts"]}
            if (run.get("archive_status") != "complete" or actual != expected
                    or json.loads((manifest_path.parent / "checksums.json").read_text()) != expected):
                raise ValueError(f"Cannot reconcile {run['id']}: archive failed integrity")
            entries = [entry for entry in batch["queue"] if entry.get("run_id") == run["id"]]
            if not entries:
                entries = [entry for entry in batch["queue"] if not entry.get("run_id") and
                           (entry["tool"], entry["model"], entry["task_id"]) ==
                           (run["tool"], run["model"], run["task_id"])]
            if len(entries) != 1:
                raise ValueError(f"Cannot reconcile {run['id']}: ambiguous queue slot")
            entry = entries[0]
            wanted = {"run_id": run["id"], "status": run["status"], "archive": run["archive_dir"],
                      "generation_status": run.get("generation_status", run["status"]),
                      "evaluation_status": run["evaluation"]["status"]}
            changed = any(entry.get(key) != value for key, value in wanted.items()) or run["id"] not in batch["run_ids"]
            if changed:
                entry.setdefault("recovered_original", json.loads(json.dumps(entry)))
                entry.update(wanted, recovered_from=evidence)
                if run["id"] not in batch["run_ids"]:
                    batch["run_ids"].append(run["id"])
                updated.append(run["id"])
        if not updated:
            continue
        # Reconciliation does not upgrade the batch's original outcome.
        batch["reconciled_at"] = utcnow()
        report = {"batch_id": batch_id, "original_status": original["status"],
                  "recovered_run_ids": updated, "run_count": len(batch["run_ids"])}
        if apply:
            backup = root / "data/recovery/batch-ledgers" / (batch_id + ".original.json")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                write_json(backup, original)
            write_json(path, batch)
        reports.append(report)
    return {"applied": apply, "batches": reports}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch", action="append", required=True, help="Closed batch ID to reconcile; repeatable")
    args = parser.parse_args()
    print(json.dumps(reconcile(ROOT, args.apply, args.batch), ensure_ascii=False))
