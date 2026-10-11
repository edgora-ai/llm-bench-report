#!/usr/bin/env python3
"""Derive a progressive static gallery from a verified v2 bundle, without rebuilding it.

Only the public entrypoint is rendered. The complete viewer, runtime, offline
report, media and reviewed original packages retain their exact source bytes.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
from html import escape
import json
from pathlib import Path
import re
import sys

# Permit an isolated (-I) CLI without importing from the input bundle or cwd.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_site as site
from make_originals import FORMAT_V2
from make_snapshot import ROOT

FORMAT = site.FORMAT_V3
VERSION = 3
SEED_MARKER = "window.BENCH_GALLERY="
PUBLIC_FILES = ("web/public-gallery.html", "web/public-gallery.css", "web/public-gallery.js")
MARKERS = ("PUBLIC_CSS", "PUBLIC_JS", "GALLERY_SEED", "TASK_TABS", "GALLERY_CARDS",
           "GALLERY_COUNT", "GALLERY_TOTAL")
GZIP_BUDGET = 25 * 1024


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _descriptor(raw, extension):
    sha = _digest(raw)
    return {"path": "viewer/" + sha + "." + extension, "sha256": sha, "size": len(raw)}


def entrypoint_status(run):
    """Keep app.js entrypointStatus semantics, including unfinished evaluations."""
    evaluation = run.get("evaluation") or {}
    if evaluation.get("status") and evaluation["status"] != "completed":
        return "unknown"
    check = next((item for item in run.get("checks") or [] if item.get("name") == "entrypoint"), None)
    if check is None:
        check = next((item for item in evaluation.get("checks") or [] if item.get("name") == "entrypoint"), None)
    status = (check or {}).get("status")
    return "pass" if status in {"pass", "passed", "ok"} else "fail" if status in {"fail", "failed"} else "unknown"


def project_row(run, data):
    """One projected attempt; history_count is stamped later."""
    for key in ("model", "tool", "task_id", "task_name", "purpose", "prompt_version", "date", "started_at", "status", "generation_status"):
        site._require(run.get(key) is None or isinstance(run[key], str), "invalid gallery run field: " + key)
    evaluation = run.get("evaluation") or {}
    site._require(evaluation.get("status") is None or isinstance(evaluation["status"], str), "invalid gallery evaluation status")
    for checks in (run.get("checks"), evaluation.get("checks")):
        site._require(checks is None or isinstance(checks, list), "invalid gallery checks")
        site._require(all(isinstance(item, dict) and (item.get("status") is None or isinstance(item["status"], str))
                          for item in checks or []), "invalid gallery check status")
    run_id = run["id"]
    declared = [(item.get("path") if isinstance(item, dict) else item)
                for item in (run.get("evaluation") or {}).get("evidence") or []]
    keys = [run_id + "/" + path for path in declared]
    desktop = next((key for key in keys if key.rsplit("/", 1)[-1] == "desktop.png"), None)
    full = data["evidence"].get(desktop)
    thumb = data["thumbnails"].get(desktop)
    image = None
    if full and thumb:
        info = data["assets"][full]
        if info["mime"].startswith("image/"):
            image = {"thumb": thumb, "full": full, "width": info["width"],
                     "height": info["height"], "label": "desktop.png"}
    return {"id": run_id, "model": run.get("model") or "unknown",
            "tool": run.get("tool") or "unknown", "task_id": run.get("task_id"),
            "purpose": run.get("purpose") or "unknown", "prompt_version": run.get("prompt_version"),
            "date": str(run.get("date") or run.get("started_at") or "")[:10] or None,
            "started_at": run.get("started_at"),
            "status": run.get("generation_status") or run.get("status") or "unknown",
            "entry_status": entrypoint_status(run),
            "evaluation_status": (run.get("evaluation") or {}).get("status") or "unknown",
            "registered_media": any(key in data["evidence"] for key in keys),
            "image": image, "original": copy.deepcopy(data["originals"][run_id]),
            "history_count": 0}


def project_runs(data):
    """Latest attempt per (tool, model, task_id); full history stays in viewer only."""
    site._require(all(re.fullmatch(r"[a-f0-9]{32}", run["id"]) for run in data["runs"])
                  and len({run["id"] for run in data["runs"]}) == len(data["runs"]),
                  "gallery requires unique 32hex run IDs")
    ordered = sorted(data["runs"], key=lambda run: (run.get("tool") or "unknown", run.get("model") or "unknown",
                                                    run.get("task_id") or "", run.get("started_at") or run.get("date") or "",
                                                    run["id"]))
    latest_index = {}
    for index, run in enumerate(ordered):
        key = (run.get("tool") or "unknown", run.get("model") or "unknown", run.get("task_id") or "")
        latest_index[key] = index
    rows = [project_row(run, data) for run in ordered]
    for key, index in latest_index.items():
        rows[index]["history_count"] = sum(1 for run in ordered
                                           if (run.get("tool") or "unknown", run.get("model") or "unknown",
                                               run.get("task_id") or "") == key) - 1
    selected = [rows[index] for index in sorted(latest_index.values())]
    selected.sort(key=lambda row: (row["tool"], row["model"], row["started_at"] or row["date"] or "", row["id"]))
    return selected


def project_gallery(data, full_bytes, runtime_bytes, viewer_bytes):
    """Latest-per-triple projection; never select best or invent scores."""
    site._require(data.get("format") == FORMAT_V2 and data.get("transport") == "external",
                  "gallery input must be verified external v2")
    tasks = {}
    for item in data.get("tasks") or []:
        task = {"id": item, "name": item} if isinstance(item, str) else item
        site._require(isinstance(task, dict) and isinstance(task.get("id"), str), "invalid gallery task")
        site._require(task.get("name") is None or isinstance(task["name"], str), "invalid gallery task name")
        tasks[task["id"]] = {"id": task["id"], "name": task.get("name") or task["id"]}
    ids = set()
    for run in data["runs"]:
        site._require(re.fullmatch(r"[a-f0-9]{32}", run["id"]) is not None and run["id"] not in ids,
                      "gallery requires unique 32hex run IDs")
        ids.add(run["id"])
        if run.get("task_id") and run["task_id"] not in tasks:
            tasks[run["task_id"]] = {"id": run["task_id"], "name": run.get("task_name") or run["task_id"]}
    runs = project_runs(data)
    ordered_tasks = sorted(tasks.values(), key=lambda task: (task["id"] != "crocodile", task["id"]))
    full = _descriptor(full_bytes, "html")
    full["script_sha256"] = _digest(runtime_bytes + b"\n" + viewer_bytes)
    seed = {"version": VERSION, "format": FORMAT, "tasks": ordered_tasks, "runs": runs,
            "defaults": {"task_id": ordered_tasks[0]["id"] if ordered_tasks else "", "purpose": "benchmark", "page_size": 8},
            "counts": {"runs": len(runs), "full_runs": len(data["runs"]), "benchmark": sum(run.get("purpose") == "benchmark" for run in runs),
                       "full_benchmark": sum(run.get("purpose") == "benchmark" for run in data["runs"]),
                       "reviews": sum(len(run.get("reviews") or []) for run in data["runs"])},
            "full": full, "runtime": _descriptor(runtime_bytes, "js"), "offline": copy.deepcopy(data["offline"])}
    site._public_data(seed)
    return seed


def initial_runs(seed):
    defaults = seed["defaults"]
    return [run for run in seed["runs"] if run["task_id"] == defaults["task_id"]
            and run["purpose"] == defaults["purpose"] and run["registered_media"]]


def render_card(run):
    """Server markup has the same action/status/lazy-image contract as bootstrap."""
    e = lambda value: escape(str(value if value is not None else "unknown"), quote=True)
    run_id = e(run["id"])
    image = run["image"]
    cover = ('<img class="work-image" data-src="' + e(image["thumb"]) + '" width="' + e(image["width"])
             + '" height="' + e(image["height"]) + '" alt="' + e(run["model"] + ' · ' + run["id"] + ' · ' + image["label"])
             + '" loading="lazy" decoding="async">') if image else '<p class="work-empty">本次未登记桌面首张截图；不替换为其他采样。</p>'
    disabled = '' if run["original"]["status"] in {"ready", "missing_dependencies"} else ' disabled'
    zoom_disabled = '' if image else ' disabled'
    history = int(run.get("history_count") or 0)
    history_badge = ('<span class="work-history-badge" title="历史尝试">历史 ' + e(history + 1) + ' 次 · 详情对照</span>'
                     if history else '<span class="work-history-badge" title="无历史尝试">最新</span>')
    score = '<span class="work-score-note">评分状态 · 详情</span>'
    return ('<article class="evidence-card" data-run-id="' + run_id + '">'
            '<div class="work-heading"><div class="work-identity"><h2 class="work-name">' + e(run["model"]) + '</h2>'
            '<p class="work-meta">' + e(run["tool"]) + ' · ' + e(run["date"]) + ' · ' + e(run["prompt_version"]) + '</p>'
            + history_badge + '</div>'
            '<span class="work-attempt" title="' + run_id + '">' + e(run["id"][:8]) + '</span></div>'
            '<div class="work-cover">' + cover + '</div>'
            '<div class="work-status"><span>会话 ' + e(run["status"]) + '</span><span>入口 ' + e(run["entry_status"])
            + '</span><span>评估 ' + e(run["evaluation_status"]) + '</span>' + score + '</div><div class="work-actions">'
            '<button type="button" class="primary" data-run-original="' + run_id + '"' + disabled + '>运行原作</button>'
            '<button type="button" data-select-run="' + run_id + '" aria-pressed="false">加入对比</button>'
            '<button type="button" data-zoom-run="' + run_id + '"' + zoom_disabled + '>放大</button>'
            '<button type="button" data-detail-run="' + run_id + '">详情 ↗</button>'
            '<button type="button" data-model-profile="' + e(run["model"]) + '">模型全史 ↗</button>'
            '</div></article>')


def render_gallery(seed, root=ROOT):
    template, css, bootstrap = [site.read_asset_safe(root, path).decode("utf-8") for path in PUBLIC_FILES]
    for marker in MARKERS:
        site._require(template.count("{{" + marker + "}}") == 1, "expected exactly one template marker: " + marker)
    site._require(not re.search(r"</(?:script|style)", css + bootstrap, re.I), "public inline asset closes its container")
    included = initial_runs(seed)
    tabs = ''.join('<button type="button" role="tab" data-task-id="' + escape(task["id"], quote=True)
                   + '" aria-selected="' + ('true' if task["id"] == seed["defaults"]["task_id"] else 'false')
                   + '"' + (' class="active"' if task["id"] == seed["defaults"]["task_id"] else '') + '>'
                   + escape(task["name"], quote=True) + '</button>' for task in seed["tasks"])
    tabs += '<button type="button" role="tab" data-task-id="" aria-selected="false">全部任务</button>'
    encoded = json.dumps(seed, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).replace("<", "\\u003c")
    defaults = seed["defaults"]
    filtered = [run for run in seed["runs"] if run["task_id"] == defaults["task_id"] and run["purpose"] == defaults["purpose"]]
    history_extra = sum(int(run.get("history_count") or 0) for run in filtered)
    values = {"PUBLIC_CSS": css, "PUBLIC_JS": bootstrap, "GALLERY_SEED": encoded,
              "TASK_TABS": tabs, "GALLERY_CARDS": ''.join(render_card(run) for run in included[:8]),
              "GALLERY_COUNT": f"{len(filtered)} 最新三元组 · 本页 {min(8, len(included))} 份媒体记录 · {len(filtered) - len(included)} 次无媒体 · 详情对照历史 {history_extra} 次",
              "GALLERY_TOTAL": str(seed["counts"]["full_runs"])}
    # One pass prevents a literal template marker in source data being expanded.
    result = re.sub(r"\{\{(" + '|'.join(MARKERS) + r")\}\}", lambda match: values[match.group(1)], template)
    return result.encode("utf-8")


def audit_gallery(raw, root=ROOT):
    """Read JSON only; exactly two scripts and a separately trusted bootstrap."""
    parsed = site._ReportHTML(static=True)
    parsed.feed(raw.decode("utf-8"))
    parsed.close()
    site._require(parsed.current is None and len(parsed.scripts) == 2, "gallery requires exactly two inline scripts")
    match = re.fullmatch(r"window\.BENCH_GALLERY=(\{.*\});", parsed.scripts[0], re.S)
    site._require(match is not None and '<' not in parsed.scripts[0], "gallery JSON must be safely escaped")
    bootstrap = site.read_asset_safe(root, PUBLIC_FILES[2]).decode("utf-8")
    site._require(parsed.scripts[1] == bootstrap, "untrusted gallery bootstrap")
    seed = site._json(match.group(1))
    site._require(isinstance(seed, dict) and type(seed.get("version")) is int and seed["version"] == VERSION
                  and seed.get("format") == FORMAT, "invalid gallery version/profile")
    site._public_data(seed)
    return seed


def _captured_v2(source, viewer, runtime):
    manifest = site.verify_site(source, viewer, preview_runtime=runtime)
    site._require(manifest["version"] == 2, "gallery source must be a verified v2 bundle")
    payloads = {path: site.read_asset_safe(source, path) for path in manifest["files"]}
    for path, raw in payloads.items():
        info = manifest["files"][path]
        site._require(len(raw) == info["size"] and _digest(raw) == info["sha256"], "v2 input changed after verification")
    return manifest, payloads


def verify_gallery(directory, manifest, viewer_script, *, preview_runtime=None, root=ROOT, baseline_site=None):
    """Verify exact inventory, trusted public assets and the FULL embedded v2 bundle."""
    files, directories = site._inventory(directory)
    expected_dirs = {path.split('/')[0] for path in manifest["files"] if '/' in path}
    site._require(files == set(manifest["files"]) | {site.MANIFEST} and directories == expected_dirs,
                  "missing or unmanaged gallery bundle files")
    payloads = {}
    for path, info in manifest["files"].items():
        raw = site.read_asset_safe(directory, path)
        site._require(len(raw) == info["size"] and _digest(raw) == info["sha256"], "asset hash/size mismatch: " + path)
        if site.MEDIA_PATH.fullmatch(path):
            actual = site._media_info(raw, info["mime"])
            site._require(all(actual.get(key) == value for key, value in info.items() if key != "role"
                              and (key not in {"width", "height"} or key in actual)), "media metadata mismatch: " + path)
        payloads[path] = raw
    seed = audit_gallery(payloads["index.html"], root)
    viewer_paths = [path for path, info in manifest["files"].items() if info["role"] == "viewer"]
    runtime_paths = [path for path, info in manifest["files"].items() if info["role"] == "runtime"]
    full_path, runtime_path = viewer_paths[0], runtime_paths[0]
    runtime = Path(preview_runtime) if preview_runtime is not None else Path(viewer_script).with_name("preview-runtime.js")
    trusted_runtime = site._read(runtime)
    site._require(payloads[runtime_path] == trusted_runtime, "untrusted gallery preview runtime")
    legacy_files = {path: info for path, info in manifest["files"].items()
                    if path not in {"index.html", full_path, runtime_path}}
    legacy_files["index.html"] = {**manifest["files"][full_path], "role": "index"}
    legacy = site.validate_manifest({"version": 2, "format": FORMAT_V2, "files": legacy_files,
                                   "stats": {**manifest["stats"], "index_bytes": len(payloads[full_path])}})
    legacy_payloads = {path: payloads[full_path] if path == "index.html" else payloads[path] for path in legacy_files}
    site._verify_site_payloads(legacy, legacy_payloads, viewer_script, preview_runtime=runtime)
    data = site.audit_snapshot(payloads[full_path].decode("utf-8"), viewer_script, static=True, preview_runtime=runtime)
    expected = project_gallery(data, payloads[full_path], trusted_runtime, site._read(viewer_script))
    # Canonical JSON comparison distinguishes booleans from integers (True == 1).
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    site._require(canonical(seed) == canonical(expected), "gallery projection differs from full v2 data")
    site._require(seed["full"]["path"] == full_path and seed["runtime"]["path"] == runtime_path,
                  "unreferenced or mismatched viewer roles")
    site._require(payloads["index.html"] == render_gallery(expected, root), "gallery differs from trusted template/assets/server cards")
    site._require(len(gzip.compress(payloads["index.html"], compresslevel=6, mtime=0)) <= GZIP_BUDGET, "gallery gzip entrypoint budget exceeded")
    if baseline_site is not None:
        original_manifest, original_payloads = _captured_v2(baseline_site, viewer_script, runtime)
        site._require(legacy == original_manifest and legacy_payloads == original_payloads,
                      "gallery v2 bytes/metadata differ from independently trusted baseline")
    return manifest


def build_gallery(source, destination, input_viewer, *, input_runtime=None, root=ROOT):
    source, root = Path(source), Path(root)
    viewer = Path(input_viewer)
    runtime = Path(input_runtime) if input_runtime is not None else viewer.with_name("preview-runtime.js")
    manifest, captured = _captured_v2(source, viewer, runtime)
    runtime_bytes, viewer_bytes = site._read(runtime), site._read(viewer)
    seed = project_gallery(site.audit_snapshot(captured["index.html"].decode("utf-8"), viewer, static=True,
                                              preview_runtime=runtime), captured["index.html"], runtime_bytes, viewer_bytes)
    payloads = {path: raw for path, raw in captured.items() if path != "index.html"}
    payloads[seed["full"]["path"]] = captured["index.html"]
    payloads[seed["runtime"]["path"]] = runtime_bytes
    payloads["index.html"] = render_gallery(seed, root)
    files = {path: info for path, info in manifest["files"].items() if path != "index.html"}
    files[seed["full"]["path"]] = site._file_info(captured["index.html"], "text/html", "viewer")
    files[seed["runtime"]["path"]] = site._file_info(runtime_bytes, "text/javascript", "runtime")
    files["index.html"] = site._file_info(payloads["index.html"], "text/html", "index")
    # Manifest stats still describe the full embedded v2 bundle (not latest-only
    # seed rows); only the progressive entrypoint size is updated.
    output = site.validate_manifest({"version": VERSION, "format": FORMAT, "files": dict(sorted(files.items())),
                                    "stats": {**manifest["stats"], "index_bytes": len(payloads["index.html"])}})
    payloads[site.MANIFEST] = (json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode("utf-8")
    return site._write_site_payloads(destination, payloads, output, viewer, preview_runtime=runtime, gallery_root=root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="independently verified v2 site directory; read-only")
    parser.add_argument("destination", type=Path, help="empty standalone v3 site directory (parent must exist)")
    parser.add_argument("--input-viewer", required=True, type=Path, help="independent trusted app.js matching v2 input")
    parser.add_argument("--input-runtime", required=True, type=Path, help="independent trusted preview-runtime.js matching v2 input")
    parser.add_argument("--root", type=Path, default=ROOT, help="project with trusted web/public-gallery.html/css/js")
    args = parser.parse_args(argv)
    try:
        stats = build_gallery(args.source, args.destination, args.input_viewer,
                              input_runtime=args.input_runtime, root=args.root)
    except (RuntimeError, OSError, UnicodeError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps({"destination": str(args.destination), "version": VERSION, "format": FORMAT, **stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
