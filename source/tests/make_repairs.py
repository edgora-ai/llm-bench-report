"""Two catalog-pinned repair previews, never originals or automatic approvals.

build_repairs accepts snapshot runs, a read-only runs root and an audit path/dict.
validate_repairs accepts the repair registry, external package bytes and the
snapshot's originals mapping; it needs no archives. Both APIs perform no writes.
CLI prepare emits HTML plus a non-include audit template into a NEW directory;
only a separately written, full-content assistant review can enable CLI build.
"""
from __future__ import annotations

import argparse
import base64
import copy
from pathlib import Path
import re
import sys

# Allow `python3 -I tests/make_repairs.py ...` without loading archive-side code.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_originals as original


CATALOG = {
    "111ded7301fc4cb68b2fe4fa30dd58f7": {
        "run_id": "111ded7301fc4cb68b2fe4fa30dd58f7",
        "date": "2026-10-10",
        "recipe_id": "nemotron-event-horizon-let-v1",
        "source_sha256": "bef9d0a9dee0236a9674d90d5811c15c5c60ada39a5beb3f75e7b91e6cf00df8",
        "operations": [{"op": "replace_exact", "old": "const eventHorizonRadius = 0;",
                        "new": "let eventHorizonRadius = 0;", "count": 1}],
        "result_sha256": "a857a31a6b987d0c8290c8128716a730a03baaca5715b395083429433bd6e784",
        "package_sha256": "bc12ce6085f42fb1fbe0325c0fb253511389538834e6d5a654200140ac763abc",
        "package_size": 34695,
    },
    "008c06189a844125af6b8e37c0b08a07": {
        "run_id": "008c06189a844125af6b8e37c0b08a07",
        "date": "2026-10-14",
        "recipe_id": "catjam-first-html-lines-1-177-v1",
        "source_sha256": "ffbfb56be15c02b50ab6c77e7d7ba2c32ec398604f97bf39be5ce39a363c761c",
        "operations": [{"op": "extract_lines", "first": 1, "last": 177}],
        "result_sha256": "2a89ff1878a335bb7853d496807388468d857cc3a8364b929baa13f1f3034205",
        "package_sha256": "e13cab5a8e015964548164a6756c7cf1c2523136a5fb08a32efc69598225d797",
        "package_size": 9726,
    },
}
RECORD_FIELDS = {"version", "recipe_id", "source_sha256", "operations",
                 "result_sha256", "review", "descriptor"}
REVIEW_FIELDS = {"reviewer", "decision", "reviewed_full", "reason", "recipe_id",
                 "source_sha256", "result_sha256", "package_sha256"}


def require(condition, message):
    if not condition:
        raise RuntimeError("Unsafe repair: " + message)


def _load(value):
    return original._site()._json(original._site()._read(value)) if isinstance(value, (str, Path)) else value


def _apply_operations(source, operations):
    """Byte-only operations: neither normalize line endings nor parse/choose HTML."""
    result = source
    for operation in operations:
        if operation["op"] == "replace_exact":
            old, new = operation["old"].encode("utf-8"), operation["new"].encode("utf-8")
            require(result.count(old) == operation["count"], "patch count")
            result = result.replace(old, new)
        elif operation["op"] == "extract_lines":
            # LF physical lines, retaining every original byte (including CRLF).
            # splitlines() would also split on other byte control characters.
            lines = result.split(b"\n")
            lines = [line + b"\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
            require(len(lines) >= operation["last"], "line range")
            result = b"".join(lines[operation["first"] - 1:operation["last"]])
        else:
            raise RuntimeError("Unsafe repair: unknown operation")
    return result


def apply_recipe(source, recipe):
    require(isinstance(source, bytes) and original.digest(source) == recipe["source_sha256"], "source hash")
    result = _apply_operations(source, recipe["operations"])
    require(original.digest(result) == recipe["result_sha256"], "result hash")
    return result


def _package(run_id, result):
    original.scan_source("index.html", result)
    adapter, missing = original.adaptation(result, {"index.html"})
    require(not missing and not adapter["patches"], "catalog repairs must be self-contained")
    raw = original.encode_json({
        "version": 1, "run_id": run_id, "entrypoint": "index.html",
        "files": {"index.html": {"mime": "text/html", "size": len(result),
                                  "sha256": original.digest(result),
                                  "base64": base64.b64encode(result).decode("ascii")}},
        "adaptation": adapter, "missing": [],
    })
    original.validate_original_package(raw, run_id)
    return raw


def _validate_review(review, recipe, *, included=False):
    require(isinstance(review, dict) and set(review) == REVIEW_FIELDS, "review schema")
    require(review["reviewer"] == "assistant" and type(review["reviewed_full"]) is bool,
            "assistant full-content review")
    require(isinstance(review["decision"], str) and review["decision"] in {"include", "withhold"}, "review decision")
    require(isinstance(review["reason"], str) and bool(review["reason"].strip()), "review reason")
    original._site()._public_text(review["reason"])
    for field in ("recipe_id", "source_sha256", "result_sha256", "package_sha256"):
        require(review[field] == recipe[field], "review binding: " + field)
    if included or review["decision"] == "include":
        require(review["decision"] == "include" and review["reviewed_full"] is True, "include requires full review")


def _audit(value):
    value = _load(value)
    require(isinstance(value, dict) and set(value) == {"version", "repairs"}
            and type(value["version"]) is int and value["version"] == 1
            and isinstance(value["repairs"], dict), "audit schema")
    for run_id, review in value["repairs"].items():
        require(isinstance(run_id, str) and run_id in CATALOG, "unknown audit run")
        _validate_review(review, CATALOG[run_id])
    return value


def _candidates(runs, archive_root):
    require(isinstance(runs, list), "runs schema")
    seen = set()
    for run in runs:
        require(isinstance(run, dict), "run schema")
        run_id = original.relative(run.get("id"))
        date = original.relative(run.get("date"))
        require("/" not in run_id and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), "archive locator")
        require(run_id not in seen, "duplicate run")
        seen.add(run_id)
        if run_id not in CATALOG:
            continue  # No inference or repair for unknown/empty deliveries.
        recipe = CATALOG[run_id]
        require(date == recipe["date"], "catalog run/date binding")
        # Reuse the exact original archive reader: manifest, checksums, generation
        # registrations, canonical paths, bounded reads and no-follow directories.
        descriptors, _ = original.build_originals(
            [run], archive_root, {"version": 1, "reviewer": "assistant", "files": {}})
        require(descriptors[run_id]["entrypoint"] == "index.html"
                and descriptors[run_id]["entry_sha256"] == recipe["source_sha256"], "source hash/entry binding")
        directory = Path(archive_root) / date / run_id
        # Require the generation registration too, rather than relying on the
        # legacy reader's compatibility allowance for archives without this field.
        manifest = original._site()._json(original._site().read_asset_safe(directory, "manifest.json"))
        generation = manifest.get("generation_artifacts")
        require(isinstance(generation, list), "generation artifact schema")
        matching = [item for item in generation if isinstance(item, dict) and item.get("path") == "output/index.html"]
        source = original._site().read_asset_safe(directory, "output/index.html", max_bytes=original.MAX_ORIGINAL_BYTES)
        require(len(matching) == 1 and matching[0].get("kind") == "output"
                and type(matching[0].get("size")) is int and matching[0]["size"] == len(source)
                and matching[0].get("sha256") == original.digest(source), "generation artifact mismatch")
        result = apply_recipe(source, recipe)
        raw = _package(run_id, result)
        require(original.digest(raw) == recipe["package_sha256"] and len(raw) == recipe["package_size"], "catalog package binding")
        descriptor = {"status": "ready", "entrypoint": "index.html", "entry_sha256": recipe["result_sha256"],
                      "missing": [], "policy": "opaque-srcdoc-v1",
                      "package": {"sha256": original.digest(raw), "size": len(raw),
                                  "path": "originals/" + original.digest(raw) + ".json"}}
        original.validate_original_descriptor(descriptor, "external")
        record = {"version": 1, "recipe_id": recipe["recipe_id"], "source_sha256": recipe["source_sha256"],
                  "operations": copy.deepcopy(recipe["operations"]), "result_sha256": recipe["result_sha256"],
                  "descriptor": descriptor}
        yield run_id, record, raw, result, descriptors[run_id]


def build_repairs(runs, archive_root, audit):
    """Return (repairs_by_run_id, payloads_by_originals_path), without writes.

    audit = {"version": 1, "repairs": {run_id: review}}. Missing or withhold
    reviews yield no registry entry. Invalid/stale reviews fail closed.
    """
    audit = _audit(audit)
    repairs, payloads, sources = {}, {}, {}
    for run_id, record, raw, _, source_descriptor in _candidates(runs, archive_root):
        review = audit["repairs"].get(run_id)
        if review is None or review["decision"] != "include":
            continue
        record["review"] = copy.deepcopy(review)
        repairs[run_id] = record
        payloads[record["descriptor"]["package"]["path"]] = raw
        sources[run_id] = source_descriptor
    validate_repairs(repairs, payloads, sources)
    return repairs, payloads


def validate_repairs(repairs, payloads, originals):
    """Validate external repair packages, returning their verified path set.

    originals is the snapshot's run-id -> SDK descriptor mapping, external or
    inline. Payloads may also contain unrelated original/media files; only paths
    returned here are authorized as repairs. No archive access or metadata trust.
    """
    require(isinstance(repairs, dict) and isinstance(payloads, dict) and isinstance(originals, dict), "registry schema")
    paths = set()
    for run_id, record in repairs.items():
        require(isinstance(run_id, str) and run_id in CATALOG, "unknown repair run")
        recipe = CATALOG[run_id]
        require(isinstance(record, dict) and set(record) == RECORD_FIELDS
                and type(record["version"]) is int and record["version"] == 1, "record schema")
        for field in ("recipe_id", "source_sha256", "result_sha256"):
            require(record[field] == recipe[field], "catalog binding: " + field)
        try:
            operations_match = original.encode_json(record["operations"]) == original.encode_json(recipe["operations"])
        except (TypeError, ValueError, RecursionError) as error:
            raise RuntimeError("Unsafe repair: operations schema") from error
        require(operations_match, "catalog operations")
        require(run_id in originals, "original run binding")
        source = originals[run_id]
        transport = "inline" if isinstance(source, dict) and isinstance(source.get("package"), dict) and "base64" in source["package"] else "external"
        original.validate_original_descriptor(source, transport)
        require(source["entrypoint"] == "index.html" and source["entry_sha256"] == recipe["source_sha256"], "original entry/source binding")
        _validate_review(record["review"], recipe, included=True)
        descriptor = original.validate_original_descriptor(record["descriptor"], "external")
        require(descriptor["status"] == "ready" and descriptor["entrypoint"] == "index.html"
                and descriptor["entry_sha256"] == recipe["result_sha256"] and descriptor["missing"] == [], "repair descriptor binding")
        info = descriptor["package"]
        require(info["sha256"] == recipe["package_sha256"] and info["size"] == recipe["package_size"], "catalog package binding")
        require(info["path"] in payloads, "missing repair package")
        raw = payloads[info["path"]]
        package = original.validate_original_package(raw, run_id, descriptor)
        require(set(package["files"]) == {"index.html"} and package["files"]["index.html"]["sha256"] == recipe["result_sha256"], "repair result binding")
        paths.add(info["path"])
    return paths


def prepare_repairs(runs, archive_root, destination):
    """Write pending repaired HTML and audit.json to a new directory, no approval."""
    candidates = list(_candidates(runs, archive_root))
    audit = {"version": 1, "repairs": {}}
    files = {}
    for run_id, record, _, result, _ in candidates:
        recipe = CATALOG[run_id]
        review = {key: recipe[key] for key in ("recipe_id", "source_sha256", "result_sha256", "package_sha256")}
        review.update(reviewer="assistant", decision="withhold", reviewed_full=False,
                      reason="待独立全文审核；修复预览不是原作。")
        audit["repairs"][run_id] = review
        files[run_id + "/index.html"] = result
    _audit(audit)
    files["audit.json"] = original.encode_json(audit)
    _write_new(destination, files)
    return audit


def _write_new(destination, files):
    destination = Path(destination).absolute()
    # Never overwrite an existing destination; reject symlink parents.
    with original._site()._directory(destination.parent):
        destination.mkdir()
    for path, raw in files.items():
        original.relative(path)
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(raw)
        require(original._site().read_asset_safe(destination, path) == raw, "output readback")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "build"))
    parser.add_argument("--runs", type=Path, required=True, help="v2 snapshot JSON (with runs) or its runs array JSON")
    parser.add_argument("--archive-root", type=Path, required=True, help="read-only runs directory")
    parser.add_argument("--destination", type=Path, required=True, help="new output directory; parent must exist")
    parser.add_argument("--audit", type=Path, help="separately written full review JSON, required for build")
    args = parser.parse_args(argv)
    if args.command == "build" and args.audit is None:
        parser.error("build requires --audit; prepare never approves")
    if args.command == "prepare" and args.audit is not None:
        parser.error("prepare does not accept approvals")
    try:
        data = _load(args.runs)
        runs = data.get("runs") if isinstance(data, dict) else data
        if args.command == "prepare":
            audit = prepare_repairs(runs, args.archive_root, args.destination)
            print("Prepared", len(audit["repairs"]), "pending repairs; no approvals")
        else:
            repairs, payloads = build_repairs(runs, args.archive_root, args.audit)
            _write_new(args.destination, {**payloads, "repairs.json": original.encode_json(repairs)})
            print("Built", len(repairs), "reviewed repair packages")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
