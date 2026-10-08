"""Verify archive bytes, SQL/API readback and recovery using a temporary index.

Initializes the production Store schema and creates a temporary recovery DB;
this is an execution-stage verifier, not a strictly read-only planning tool.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from bench.cli import ROOT, load_config
from bench.storage import Store
from bench.runner import artifact_index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-api", action="store_true")
    args = parser.parse_args()
    config = load_config()
    database = ROOT / config["paths"]["database"]
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT id,status,manifest_json FROM runs ORDER BY id").fetchall()
        reviews = db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    manifests = {run_id: json.loads(raw) for run_id, _, raw in rows}
    finalized = {key: value for key, value in manifests.items() if value["status"] not in {"running", "queued"}}
    store = Store(database, ROOT)
    for run_id, manifest in finalized.items():
        archived = json.loads((ROOT / manifest["archive_dir"] / "manifest.json").read_text())
        assert archived == manifest, f"SQL/archive mismatch: {run_id}"
        directory = ROOT / manifest["archive_dir"]
        actual = {a["path"]: a["sha256"] for a in artifact_index(directory)}
        expected = {a["path"]: a["sha256"] for a in manifest["artifacts"]}
        assert actual == expected, f"Artifact byte mismatch: {run_id}"
        assert json.loads((directory / "checksums.json").read_text()) == expected, f"Checksum map mismatch: {run_id}"
        with store.connection() as db:
            indexed = db.execute("SELECT fulljson FROM runs_search WHERE id=?", (run_id,)).fetchall()
            assert len(indexed) == 1 and json.loads(indexed[0][0]) == manifest, f"FTS mismatch: {run_id}"
        returned = store.get_run(run_id)
        assert {key: value for key, value in returned.items() if key != "reviews"} == manifest
        hits = store.list_runs({"q": run_id})
        hit_ids = [run["id"] for run in hits]
        assert run_id in hit_ids and len(hit_ids) == len(set(hit_ids)), f"Search mismatch: {run_id}"
        # Literal full-JSON search also finds children whose retry_of is this ID.
    with tempfile.TemporaryDirectory(prefix="bench-recovery-") as tmp:
        recovered = Store(Path(tmp) / "recovered.sqlite3", ROOT)
        recovery = recovered.rebuild()
        assert recovery["errors"] == 0, recovery
        for run_id in finalized:
            assert recovered.get_run(run_id) == store.get_run(run_id), f"Recovery mismatch: {run_id}"
        with recovered.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == reviews
    api = {}
    if not args.skip_api:
        host, port = config["server"]["host"], config["server"]["port"]
        base = f"http://{host}:{port}"
        token = (ROOT / config["paths"]["token_file"]).read_text().strip()
        for path in ["/", "/api/health"]:
            with urllib.request.urlopen(base + path) as response:
                api[path] = response.status
                assert response.status == 200
        try:
            urllib.request.urlopen(base + "/api/runs")
        except urllib.error.HTTPError as error:
            assert error.code == 401
            api["private_without_auth"] = error.code
        else:
            raise AssertionError("Private API accepted unauthenticated request")
        for run_id, manifest in finalized.items():
            request = urllib.request.Request(base + "/api/runs/" + run_id, headers={"x-api-key": token})
            with urllib.request.urlopen(request) as response:
                returned = json.load(response)["run"]
                assert {key: value for key, value in returned.items() if key != "reviews"} == manifest
                api["private_with_auth"] = response.status
            request = urllib.request.Request(base + "/api/runs?" + urllib.parse.urlencode({"q": run_id}), headers={"x-api-key": token})
            with urllib.request.urlopen(request) as response:
                hit_ids = [run["id"] for run in json.load(response)["runs"]]
                assert run_id in hit_ids and len(hit_ids) == len(set(hit_ids))
    print(json.dumps({"sql_count": len(rows), "statuses": dict(Counter(row[1] for row in rows)), "finalized_verified": len(finalized), "reviews": reviews, "active_not_verified": len(rows) - len(finalized), "recovery": recovery, "api": api}, ensure_ascii=False))


if __name__ == "__main__":
    main()
