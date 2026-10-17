#!/usr/bin/env python3
"""Derive a progressive static gallery from a verified v2 bundle, without rebuilding it.

Only the public entrypoint is rendered. The complete viewer, runtime, offline
report, media and reviewed original packages retain their exact source bytes.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import gzip
import math
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
from make_repairs import build_repairs, validate_repairs
from make_snapshot import ROOT

FORMAT = site.FORMAT_V3
VERSION = 3
SEED_MARKER = "window.BENCH_GALLERY="
PUBLIC_FILES = ("web/public-gallery.html", "web/public-gallery.css", "web/public-gallery.js")
MARKERS = ("PUBLIC_CSS", "PUBLIC_JS", "GALLERY_SEED", "TASK_TABS", "GALLERY_CARDS",
           "GALLERY_COUNT", "GALLERY_TOTAL")
# Entry-point budgets for the light gallery. Raised on 2026-10-15 at the
# operator's explicit instruction after the entry grew to 91,951 decoded /
# 26,067 gzip when two more reviewed samples added a second score column to the
# default cards. The decoded budget is unchanged in spirit; both ceilings are
# doubled, and the measured size still sits far below them.
DECODED_BUDGET = 200 * 1024
GZIP_BUDGET = 50 * 1024
SCORE_ENCODING = "scope-index-v1"
SCORE_LABELS = {"compliance": "指令遵循", "recognizability": "辨识度", "motion": "运动表现",
                "visual_detail": "视觉细节", "interaction": "交互表现", "completeness": "完整性"}


def valid_number(value):
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False  # Outside the browser's finite numeric range.


def valid_score(value):
    return value if valid_number(value) and value <= 4 else None


def score_scope(run, review):
    """Provenance labels are heuristics, never reviewer identity authentication."""
    rubric = (run.get("conditions") or {}).get("rubric_version")
    rubric = rubric if isinstance(rubric, str) and rubric and rubric != "unknown" else None
    # A declared conflict is not permission to infer a protocol from today's task.
    declarations = [review]
    declarations += [review[key] for key in ("conditions", "source") if isinstance(review.get(key), dict)]
    if any("rubric_version" in item and item["rubric_version"] != rubric for item in declarations):
        rubric = None
    if "run_id" in review and review["run_id"] != run.get("id"):
        rubric = None
    reviewer = review.get("reviewer")
    reviewer = reviewer if isinstance(reviewer, str) and reviewer.strip(site.SCORE_REVIEWER_WHITESPACE) else None
    blind = review.get("blind") if type(review.get("blind")) is bool else None
    provenance = "ai-suggestion" if blind is False and site.SCORE_AI_SUGGESTION.search(reviewer or "") else "label-unverified"
    return [rubric, provenance, blind, reviewer]


def scope_key(scope):
    return json.dumps(scope, ensure_ascii=False, separators=(",", ":"))


def scope_rankable(scope):
    return (isinstance(scope, list) and len(scope) == 4 and scope[0] == "visual-v1"
            and scope[1] in ("ai-suggestion", "label-unverified") and type(scope[2]) is bool
            and isinstance(scope[3], str) and bool(scope[3].strip(site.SCORE_REVIEWER_WHITESPACE)))


def review_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", value):
        return -math.inf
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
    except ValueError:
        return -math.inf


def score_summaries(run):
    """Latest timestamp / available ID / source index within each exact scope.

    No best-score selection, no backfill, and no transfer to a newer run.
    Keep original reviews immutable; only the six validated numbers are projected.
    """
    selected = {}
    for index, review in enumerate(run.get("reviews") or []):
        if not isinstance(review, dict):
            continue
        key = scope_key(score_scope(run, review))
        stamp = review.get("created_at") if isinstance(review.get("created_at"), str) else None
        review_id = review.get("id")
        if not (isinstance(review_id, str) or valid_number(review_id)):
            review_id = None
        tie_id = str(int(review_id)) if valid_number(review_id) and review_id == int(review_id) else str(review_id) if review_id is not None else ""
        order = (review_timestamp(stamp), tie_id, index)
        values = review.get("scores") if isinstance(review.get("scores"), dict) else {}
        summary = {"scope": key, "created_at": stamp, "id": review_id, "index": index,
                   "values": [valid_score(values.get(dimension)) for dimension in SCORE_LABELS]}
        if key not in selected or order > selected[key][0]:
            selected[key] = (order, summary)
    return [selected[key][1] for key in sorted(selected, key=lambda key: key.encode("utf-16-be", "surrogatepass"))]


def score_scopes(runs):
    scopes = {scope_key(score_scope(run, review)): score_scope(run, review)
              for run in runs for review in run.get("reviews") or [] if isinstance(review, dict)}
    return [scopes[key] for key in sorted(scopes, key=lambda key: key.encode("utf-16-be", "surrogatepass"))]


def decode_scores(seed):
    """Strict versioned wire projection. ID/date are intentionally in full detail only."""
    site._require(seed.get("score_encoding") == SCORE_ENCODING, "unknown gallery score encoding")
    scopes = seed.get("score_scopes")
    site._require(isinstance(scopes, list), "invalid gallery scope table")
    keys = []
    for scope in scopes:
        site._require(isinstance(scope, list) and len(scope) == 4
                      and (scope[0] is None or isinstance(scope[0], str) and scope[0] not in ("", "unknown"))
                      and scope[1] in ("ai-suggestion", "label-unverified")
                      and (scope[2] is None or type(scope[2]) is bool)
                      and (scope[3] is None or isinstance(scope[3], str) and scope[3].strip(site.SCORE_REVIEWER_WHITESPACE)),
                      "invalid gallery scope shape")
        provenance = "ai-suggestion" if scope[2] is False and site.SCORE_AI_SUGGESTION.search(scope[3] or "") else "label-unverified"
        site._require(scope[1] == provenance, "conflicting gallery scope provenance")
        keys.append(scope_key(scope))
    site._require(keys == sorted(set(keys), key=lambda key: key.encode("utf-16-be", "surrogatepass")),
                  "duplicate or unordered gallery scope table")
    site._require(isinstance(seed.get("runs"), list), "invalid gallery score rows")
    rows = []
    for row in seed["runs"]:
        site._require(isinstance(row, dict), "invalid gallery score row")
        summaries, previous = [], -1
        if "scores" in row:
            site._require(isinstance(row["scores"], list) and row["scores"], "invalid gallery score entries")
            for entry in row["scores"]:
                site._require(isinstance(entry, list) and len(entry) == 3, "invalid gallery score entry shape")
                scope_index, review_index, values = entry
                site._require(type(scope_index) is int and previous < scope_index < len(keys)
                              and type(review_index) is int and 0 <= review_index <= 9007199254740991,
                              "invalid gallery score reference")
                site._require(isinstance(values, list) and len(values) == 6
                              and all(value is None or valid_score(value) is not None for value in values),
                              "invalid gallery score values")
                summaries.append({"scope": keys[scope_index], "index": review_index, "values": values[:]})
                previous = scope_index
        rows.append({**row, **({"scores": summaries} if "scores" in row else {})})
    return rows


def default_score_source(scopes, score_source=None):
    # Explicit build preference is an exact scope, not a reviewer-name heuristic.
    if score_source is not None:
        site._require(isinstance(score_source, str) and (score_source == "*" or
                      score_source in {scope_key(scope) for scope in scopes} or not scopes and score_source == ""),
                      "preferred score source is not an exact available scope")
        return score_source
    preferred = next((scope for scope in scopes if scope_rankable(scope) and scope[1] == "ai-suggestion" and scope[2] is False), None)
    fallback = next((scope for scope in scopes if scope_rankable(scope)), None)
    if preferred is not None or fallback is not None:
        # Several independent sources exist. Showing one by default would hide
        # every other source behind a filter, so list each source separately.
        return "*"
    return ""


def scope_label(scope):
    return " · ".join([scope[0] or "协议 unknown", short_scope(scope), scope[3] or "Reviewer unknown"])


def meter(run):
    metrics = run.get("metrics") or {}
    evaluation = run.get("evaluation") or {}
    cost_source = metrics.get("cost_source")
    return [run.get("duration_ms") if valid_number(run.get("duration_ms")) else None,
            metrics.get("cost_usd") if valid_number(metrics.get("cost_usd")) else None,
            cost_source if isinstance(cost_source, str) and cost_source else None,
            metrics.get("total_tokens") if valid_number(metrics.get("total_tokens")) else None,
            evaluation.get("duration_ms") if valid_number(evaluation.get("duration_ms")) else None]


def meter_text(values):
    duration, cost, source, tokens, evaluation = values
    def seconds(value):
        return ("未上报" if not valid_number(value) else "0 秒" if value == 0 else
                "<1 秒" if value < 1000 else f"约 {math.floor(value / 1000 + .5)} 秒（approx）")
    cost = ("未上报" if not valid_number(cost) else "USD 0.00" if cost == 0 else
            "< USD 0.01" if cost < .01 else f"约 USD {cost:.2f}（approx）")
    origin = "CLI 未核验，非账单" if source == "cli_reported_unverified" else "来源未核实" if source else "来源未声明"
    total = str(int(tokens)) if valid_number(tokens) and tokens == int(tokens) else str(tokens) if valid_number(tokens) else "未上报"
    return f"生成会话 {seconds(duration)}；费用 {cost} · {origin}；总 Token {total}；评估 {seconds(evaluation)}"


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
    keys = list(dict.fromkeys(run_id + "/" + path for path in declared
                             if isinstance(path, str) and re.search(r"\.(png|jpe?g|webp|gif|webm|mp4)$", path, re.I)))
    included = [key for key in keys if key in data["evidence"]]
    desktop = next((key for key in keys if key.rsplit("/", 1)[-1] == "desktop.png"), None)
    gaps = []
    if not keys:
        gaps.append("no_media_registered")
    if len(included) < len(keys):
        gaps.append("media_not_included")
    if keys and not desktop:
        gaps.append("no_desktop_capture")
    elif desktop and desktop not in included:
        gaps.append("desktop_not_included")
    elif desktop and desktop not in data["thumbnails"]:
        gaps.append("desktop_no_thumbnail")
    full = data["evidence"].get(desktop)
    thumb = data["thumbnails"].get(desktop)
    image = None
    if full and thumb:
        info = data["assets"][full]
        if info["mime"].startswith("image/"):
            image = {"thumb": thumb, "full": full, "width": info["width"],
                     "height": info["height"], "label": "desktop.png"}
    summaries = score_summaries(run)
    return {**({"scores": summaries} if summaries else {}), "meter": meter(run),
            **({"termination": evaluation["generation_termination_reason"]}
               if str(run.get("prompt_version") or "").startswith("standalone-svg-")
               and evaluation.get("generation_termination_reason") in {"length", "stop"} else {}),
            "id": run_id, "model": run.get("model") or "unknown",
            "tool": run.get("tool") or "unknown", "task_id": run.get("task_id"),
            "purpose": run.get("purpose") or "unknown", "prompt_version": run.get("prompt_version"),
            "date": str(run.get("date") or run.get("started_at") or "")[:10] or None,
            "started_at": run.get("started_at"),
            "status": run.get("generation_status") or run.get("status") or "unknown",
            "entry_status": entrypoint_status(run),
            "evaluation_status": (run.get("evaluation") or {}).get("status") or "unknown",
            "registered_media": bool(included), "media_counts": [len(keys), len(included)],
            **({"media_gaps": gaps} if gaps else {}),
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


def project_gallery(data, full_bytes, runtime_bytes, viewer_bytes, *, score_source=None, repairs=None):
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
    scopes = score_scopes(data["runs"])
    scope_indices = {scope_key(scope): index for index, scope in enumerate(scopes)}
    # Wire references identify an exact raw review on its own run; never a merged score.
    # Keep score_summaries/project_runs unchanged for full metadata callers.
    runs = [{**row, **({"scores": [[scope_indices[item["scope"]], item["index"], item["values"]]
                                  for item in row["scores"]]} if "scores" in row else {})} for row in runs]
    seed = {"version": VERSION, "format": FORMAT, "score_encoding": SCORE_ENCODING,
            "tasks": ordered_tasks, "runs": runs, "score_scopes": scopes,
            "defaults": {"task_id": ordered_tasks[0]["id"] if ordered_tasks else "", "purpose": "benchmark",
                         "page_size": 2, "mobile_page_size": 1, "score_source": default_score_source(scopes, score_source)},
            **({"preferred_score_source": score_source} if score_source is not None else {}),
            "counts": {"runs": len(runs), "full_runs": len(data["runs"]), "benchmark": sum(run.get("purpose") == "benchmark" for run in runs),
                       "full_benchmark": sum(run.get("purpose") == "benchmark" for run in data["runs"]),
                       "reviews": sum(len(run.get("reviews") or []) for run in data["runs"])},
            "full": full, "runtime": _descriptor(runtime_bytes, "js"), "offline": copy.deepcopy(data["offline"])}
    if repairs:
        site._require(set(repairs) <= {row["id"] for row in runs}, "repair is not a latest gallery run")
        seed["repairs"] = copy.deepcopy(repairs)
    site._public_data(seed)
    decode_scores(seed)
    return seed


def initial_runs(seed):
    defaults = seed["defaults"]
    return [run for run in decode_scores(seed) if run["task_id"] == defaults["task_id"]
            and run["purpose"] == defaults["purpose"] and run["registered_media"]]


MEDIA_GAPS = {"no_media_registered": "无媒体登记", "media_not_included": "部分媒体未公开",
              "no_desktop_capture": "未登记桌面首图", "desktop_not_included": "桌面首图未公开",
              "desktop_no_thumbnail": "桌面缩略图缺失"}


def media_text(run):
    registered, included = run["media_counts"]
    facts = f"媒体登记 {registered} · 公开包含 {included}（非加载结果）"
    gaps = [MEDIA_GAPS[key] for key in run.get("media_gaps", []) if key in MEDIA_GAPS]
    if run["original"]["status"] == "no_entrypoint":
        gaps.insert(0, "未交付，没有可展示 SVG/HTML")
    if str(run.get("prompt_version") or "").startswith("standalone-svg-"):
        gaps.insert(0, "SVG 补测" + (" · length 截断" if run.get("termination") == "length" else ""))
    return facts + (" · " + " · ".join(gaps) if gaps else "")


def short_scope(scope):
    if not scope:
        return "未评审 · 不借用历史评分"
    if scope[1] == "ai-suggestion":
        # Two AI scopes must stay distinguishable; derive the label from the
        # recorded reviewer string instead of inventing a review date.
        return "AI 复核 · score-first-v1" if "score-first" in str(scope[3] or "") else "AI 建议 · 待复核"
    return ("盲评" if scope[2] is True else "非盲评" if scope[2] is False else "盲评未声明") + " · 来源标签未核实"


def render_repair(run_id, repair):
    """Derived previews never borrow the original-work label or historical scores."""
    e = lambda value: escape(str(value), quote=True)
    modes = ''.join('<button type="button" data-preview-mode="' + mode + '" data-preview-run="' + e(run_id)
                    + '" aria-pressed="' + ('true' if mode == 'repair' else 'false') + '">' + label + '</button>'
                    for mode, label in (("repair", "修复版"), ("original", "原始交付"), ("static", "历史截图")))
    return ('<aside class="work-repair"><p class="repair-label">修复版，非原始交付 · 不用于评分；历史评分仍针对原始交付</p>'
            '<div class="preview-modes" role="group" aria-label="预览版本">' + modes + '</div>'
            '<details><summary>固定修复说明 / 源与结果 SHA-256</summary><p>' + e(repair["recipe_id"]) + ' · '
            + e(repair["review"]["reason"]) + '</p><p class="repair-hash">源 SHA-256：' + e(repair["source_sha256"])
            + '</p><p class="repair-hash">结果 SHA-256：' + e(repair["result_sha256"])
            + '</p><p>完整查看器和离线版仅含原始交付，不含修复版。</p></details></aside>')


def render_card(run, score_source="", repair=None):
    """Server markup and bootstrap share six scores, exact scope and meter facts."""
    e = lambda value: escape(str(value if value is not None else "unknown"), quote=True)
    run_id, image = e(run["id"]), run["image"]
    summaries = run.get("scores", [])
    selected = summaries if score_source == "*" else [next((item for item in summaries if item["scope"] == score_source), None)]
    score, sources = '', ''
    for index, summary in enumerate(selected or [None]):
        scope = json.loads(summary["scope"]) if summary else json.loads(score_source) if score_source not in ("", "*") else None
        valid = scope_rankable(scope)
        score += '<div class="work-score" data-score-scope="' + e(summary["scope"] if summary else score_source) + '"><dl class="score-sheet">'
        for key_index, (key, label) in enumerate(SCORE_LABELS.items()):
            value = valid_score(summary["values"][key_index]) if summary and valid else None
            missing = ("未评审" if not summaries else "本来源无记录") if not summary else "协议未核实" if not valid else "分项缺失"
            figure = str(int(value)) if value is not None and value == int(value) else str(value)
            score += '<div class="score-cell" data-dimension="' + key + '"><dt>' + label + '</dt><dd>'
            score += ('<strong>' + e(figure) + '</strong><span>/4</span>') if value is not None else '<span class="score-unknown">' + missing + '</span>'
            score += '</dd></div>'
        score += '</dl><p class="score-caption">' + (f'口径 {index + 1} · ' if score_source == "*" else '') + e(short_scope(scope)) + '</p></div>'
        if summary:
            sources += '<p>' + e(scope_label(scope)) + ' · 原序 ' + e(summary["index"]) + ' · 评审 ID / 日期见详情</p>'
    cover = ('<img class="work-image" data-src="' + e(image["thumb"]) + '" width="' + e(image["width"])
             + '" height="' + e(image["height"]) + '" alt="' + e(run["model"] + ' · ' + run["id"] + ' · ' + image["label"])
             + '" decoding="async">') if image else '<p class="work-empty">' + e(media_text(run)) + '</p>'
    cover_style = ' style="aspect-ratio:' + e(image["width"]) + '/' + e(image["height"]) + '"' if image else ''
    descriptor = repair["descriptor"] if repair else run["original"]
    disabled = '' if descriptor["status"] in {"ready", "missing_dependencies"} else ' disabled'
    zoom_disabled = '' if image or not disabled else ' disabled'
    zoom_label = '放大修复版' if repair else '放大原作' if not disabled else '放大回退'
    start_label = '运行修复版' if repair else '运行原作'
    model = run["model"][9:] if run["model"].startswith("opencode/") else run["model"]
    timeline_button = ('<button type="button" data-model-profile="' + e(run["model"]) + '" data-model-task="' + e(run["task_id"])
                       + '">历次作品 ↗</button>') if int(run.get("history_count") or 0) > 0 else ''
    return ('<article class="evidence-card' + ('' if run["registered_media"] else ' no-registered-media') + '" data-run-id="' + run_id + '" data-model-route="' + e(run["model"]) + '">'
            '<div class="work-heading"><h2 class="work-name" title="' + e(run["model"]) + '" aria-label="' + e(run["model"]) + '">' + e(model) + '</h2></div>'
            + score + (render_repair(run["id"], repair) if repair else '')
            + '<div class="work-cover' + ('' if image else ' empty-cover') + '"' + cover_style + '>' + cover + '</div>'
            '<div class="work-actions"><button type="button" data-run-original="' + run_id + '"' + disabled + '>' + start_label + '</button>'
            '<button type="button" data-select-run="' + run_id + '" aria-pressed="false">加入对比</button>'
            '<button type="button" data-zoom-run="' + run_id + '"' + zoom_disabled + '>' + zoom_label + '</button>'
            '<button type="button" data-detail-run="' + run_id + '">详情 ↗</button>' + timeline_button + '</div>'
            '<details class="work-facts"><summary>计量 / 条件 / 来源 · 历史 ' + e(int(run.get("history_count") or 0) + 1) + ' 次</summary>'
            '<p class="work-metrics">' + e(meter_text(run["meter"])) + '</p><p class="work-meta">' + e(run["tool"]) + ' · ' + e(run["date"]) + ' · ' + e(run["task_id"]) + ' · ' + e(run["purpose"]) + ' · ' + e(run["prompt_version"]) + '</p>'
            '<p class="work-status">会话 ' + e(run["status"]) + ' · 入口 ' + e(run["entry_status"]) + ' · 评估 ' + e(run["evaluation_status"]) + '</p>'
            '<p>' + e(media_text(run)) + '</p>' + sources + '<button type="button" data-model-profile="' + e(run["model"]) + '" data-model-task="' + e(run["task_id"]) + '">作品时间轴 ↗</button></details></article>')


def render_gallery(seed, root=ROOT):
    template, css, bootstrap = [site.read_asset_safe(root, path).decode("utf-8") for path in PUBLIC_FILES]
    for marker in MARKERS:
        site._require(template.count("{{" + marker + "}}") == 1, "expected exactly one template marker: " + marker)
    site._require(not re.search(r"</(?:script|style)", css + bootstrap, re.I), "public inline asset closes its container")
    included = initial_runs(seed)
    tabs = ''.join('<button type="button" role="tab" data-task-id="' + escape(task["id"], quote=True)
                   + '" aria-selected="' + ('true' if task["id"] == seed["defaults"]["task_id"] else 'false')
                   + '" title="' + escape(task["name"], quote=True) + '">'
                   + escape({"crocodile": "鳄鱼", "blackhole": "黑洞"}.get(task["id"], task["name"]), quote=True) + '</button>' for task in seed["tasks"])
    tabs += '<button type="button" role="tab" data-task-id="" aria-selected="false">全部任务</button>'
    # Group each row's context before its unique identity in the wire document.
    wire = {**seed, "runs": [{**{key: value for key, value in row.items() if key not in ("model", "id")},
                             "model": row["model"], "id": row["id"]} for row in seed["runs"]]}
    encoded = json.dumps(wire, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    defaults = seed["defaults"]
    filtered = [run for run in seed["runs"] if run["task_id"] == defaults["task_id"] and run["purpose"] == defaults["purpose"]]
    values = {"PUBLIC_CSS": css, "PUBLIC_JS": bootstrap, "GALLERY_SEED": encoded,
              "TASK_TABS": tabs, "GALLERY_CARDS": ''.join(render_card(run, defaults["score_source"], seed.get("repairs", {}).get(run["id"]))
                                                           for run in included[:defaults["page_size"]]),
              "GALLERY_COUNT": f"{len(filtered)} 最新 · 无媒体 {len(filtered) - len(included)}",
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
    decode_scores(seed)
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
    data = site.audit_snapshot(payloads[full_path].decode("utf-8"), viewer_script, static=True, preview_runtime=runtime)
    repairs = seed.get("repairs", {})
    repair_paths = validate_repairs(repairs, payloads, data["originals"])
    site._require(repair_paths == {path for path, info in manifest["files"].items() if info["role"] == "repair"},
                  "unreferenced or mismatched repair roles")
    legacy_files = {path: info for path, info in manifest["files"].items()
                    if path not in {"index.html", full_path, runtime_path} | repair_paths}
    legacy_files["index.html"] = {**manifest["files"][full_path], "role": "index"}
    legacy = site.validate_manifest({"version": 2, "format": FORMAT_V2, "files": legacy_files,
                                   "stats": {**manifest["stats"], "index_bytes": len(payloads[full_path])}})
    legacy_payloads = {path: payloads[full_path] if path == "index.html" else payloads[path] for path in legacy_files}
    site._verify_site_payloads(legacy, legacy_payloads, viewer_script, preview_runtime=runtime)
    expected = project_gallery(data, payloads[full_path], trusted_runtime, site._read(viewer_script),
                               score_source=seed.get("preferred_score_source"), repairs=repairs)
    # Canonical JSON comparison distinguishes booleans from integers (True == 1).
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    site._require(canonical(seed) == canonical(expected), "gallery projection differs from full v2 data")
    site._require(seed["full"]["path"] == full_path and seed["runtime"]["path"] == runtime_path,
                  "unreferenced or mismatched viewer roles")
    site._require(payloads["index.html"] == render_gallery(expected, root), "gallery differs from trusted template/assets/server cards")
    site._require(len(payloads["index.html"]) <= DECODED_BUDGET, "gallery decoded entrypoint budget exceeded")
    site._require(len(gzip.compress(payloads["index.html"], compresslevel=6, mtime=0)) <= GZIP_BUDGET, "gallery gzip entrypoint budget exceeded")
    if baseline_site is not None:
        original_manifest, original_payloads = _captured_v2(baseline_site, viewer_script, runtime)
        site._require(legacy == original_manifest and legacy_payloads == original_payloads,
                      "gallery v2 bytes/metadata differ from independently trusted baseline")
    return manifest


def build_gallery(source, destination, input_viewer, *, input_runtime=None, root=ROOT, score_source=None,
                  archive_root=None, repair_audit=None):
    source, root = Path(source), Path(root)
    viewer = Path(input_viewer)
    runtime = Path(input_runtime) if input_runtime is not None else viewer.with_name("preview-runtime.js")
    manifest, captured = _captured_v2(source, viewer, runtime)
    runtime_bytes, viewer_bytes = site._read(runtime), site._read(viewer)
    data = site.audit_snapshot(captured["index.html"].decode("utf-8"), viewer, static=True, preview_runtime=runtime)
    site._require((archive_root is None) == (repair_audit is None), "repairs require both archive root and independent audit")
    repairs, repair_payloads = build_repairs(data["runs"], archive_root, repair_audit) if repair_audit is not None else ({}, {})
    repair_paths = validate_repairs(repairs, repair_payloads, data["originals"])
    site._require(repair_paths == set(repair_payloads) and not repair_paths.intersection(captured), "repair asset collision")
    seed = project_gallery(data, captured["index.html"], runtime_bytes, viewer_bytes,
                           score_source=score_source, repairs=repairs)
    payloads = {path: raw for path, raw in captured.items() if path != "index.html"}
    payloads.update(repair_payloads)
    payloads[seed["full"]["path"]] = captured["index.html"]
    payloads[seed["runtime"]["path"]] = runtime_bytes
    payloads["index.html"] = render_gallery(seed, root)
    files = {path: info for path, info in manifest["files"].items() if path != "index.html"}
    files.update({path: site._file_info(raw, "application/json", "repair") for path, raw in repair_payloads.items()})
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
    parser.add_argument("--archive-root", type=Path, help="read-only archives for fixed repair recipes; requires --repair-audit")
    parser.add_argument("--repair-audit", type=Path, help="independent full-source review of derived previews, not original review")
    parser.add_argument("--score-source", help="preferred exact canonical JSON scope (or * for separate scopes); omitted keeps legacy default")
    args = parser.parse_args(argv)
    try:
        stats = build_gallery(args.source, args.destination, args.input_viewer,
                              input_runtime=args.input_runtime, root=args.root, score_source=args.score_source,
                              archive_root=args.archive_root, repair_audit=args.repair_audit)
    except (RuntimeError, OSError, UnicodeError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps({"destination": str(args.destination), "version": VERSION, "format": FORMAT, **stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
