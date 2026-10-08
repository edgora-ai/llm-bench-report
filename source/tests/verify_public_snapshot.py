#!/usr/bin/env python3
"""Verify an actual generated read-only report, never a synthetic fixture.

Run --help first. Requires native Python Playwright and Chromium (the trusted
llm-bench-runtime:v4 image includes both). Mount this script, web/app.js and the
snapshot read-only; mount only --output writable. Local mode needs no network.
Live mode really fetches the HTTPS report and audits its bytes before rendering;
only target-host GET/static requests may leave the trusted report viewer.
This is report-viewer inspection, NOT model_eval or a benchmark renderer.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
NEW_CASES = {
    "6c3f48c1c35747369efe292af88de884": "gpt-6-sol",
    "42ec6adce94640f2a79659c7446ea3d3": "gpt-6.1-sol",
}
SAFE_MEDIA = re.compile(r"^data:(image/(?:png|jpeg|webp|gif)|video/(?:webm|mp4));base64,[A-Za-z0-9+/=\r\n]+$", re.I)
MEDIA_PATH = re.compile(r"\.(png|jpe?g|webp|gif|webm|mp4)$", re.I)
PASS = {"pass", "passed", "ok"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--snapshot", type=Path, help="Existing real generated HTML; never rebuilt by this verifier")
    target.add_argument("--url", help="Live HTTPS report on edgora-ai.github.io (actual network fetch, no credentials)")
    parser.add_argument("--output", type=Path, required=True, help="JSON and screenshots directory outside the project, e.g. /tmp/report-verification")
    parser.add_argument("--expected-runs", type=int, default=58)
    parser.add_argument("--expected-reviews", type=int, default=14)
    parser.add_argument("--expected-failed", type=int, default=21, help="Preserved failed runs, not silently dropped")
    parser.add_argument("--date", default="2026-10-08", help="Exact gallery/date coverage and case metadata date")
    parser.add_argument("--case-id", action="append", help="Override case IDs ONLY for explicit older-snapshot development; one or two IDs")
    parser.add_argument("--viewer-script", type=Path, default=ROOT / "web/app.js", help="Trusted report viewer source; inline executable code must match exactly")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    args = parser.parse_args(argv)
    require(args.expected_runs > 0 and args.expected_reviews >= 0 and args.expected_failed >= 0, "Invalid expected counts")
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date), "Date must be YYYY-MM-DD")
    if args.case_id:
        require(1 <= len(args.case_id) <= 2 and len(set(args.case_id)) == len(args.case_id), "Development mode requires one or two distinct --case-id values")
        require(all(re.fullmatch(r"[a-f0-9]{32}", value) for value in args.case_id), "Invalid development run ID")
    if args.url:
        url = urlsplit(args.url)
        require(url.scheme == "https" and url.hostname == "edgora-ai.github.io" and url.port in (None, 443)
                and not url.username and not url.password and url.path.startswith("/llm-bench-report/"), "Only the approved public Pages report URL is allowed")
    else:
        args.snapshot = args.snapshot.resolve(strict=True)
        require(args.snapshot.is_file() and args.snapshot.suffix.lower() == ".html", "Snapshot must be an existing HTML file")
    args.output = args.output.resolve()
    require(not args.output.is_relative_to(ROOT.resolve()), "Output must be outside the project")
    require(args.output != Path("/"), "Output must be a dedicated directory")
    return args


class ReportHTML(HTMLParser):
    """Check executable tags before loading a report; payload strings are data."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.current = None
        self.unsafe = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"iframe", "object", "embed", "base"}:
            self.unsafe.append(tag)
        if any(name.lower().startswith("on") for name in attrs):
            self.unsafe.append("inline event handler")
        if "srcdoc" in attrs:
            self.unsafe.append("srcdoc")
        for key in ("href", "src", "action", "formaction"):
            value = attrs.get(key, "")
            if value.strip().lower().startswith("javascript:"):
                self.unsafe.append("javascript URL")
        if tag == "script":
            if attrs.get("src") or attrs.get("type", "") not in ("", "text/javascript"):
                self.unsafe.append("external/non-viewer script")
            self.current = []
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.unsafe.append("external stylesheet")
        if tag in {"img", "video", "source"}:
            if not SAFE_MEDIA.fullmatch(attrs.get("src", "")):
                self.unsafe.append("non-data media")

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def audit_html(html, trusted_script):
    parsed = ReportHTML()
    parsed.feed(html)
    parsed.close()
    require(not parsed.unsafe, "Unsafe report markup: " + ", ".join(sorted(set(parsed.unsafe))))
    require(len(parsed.scripts) == 2, "Report must contain only snapshot JSON and trusted viewer code")
    match = re.fullmatch(r"window\.BENCH_SNAPSHOT=(\{.*\});", parsed.scripts[0], re.S)
    require(match is not None and "<" not in parsed.scripts[0], "Snapshot JSON must be safely escaped, without embedded model markup")
    require(parsed.scripts[1] == trusted_script, "Inline executable code does not match trusted web/app.js")
    data = json.loads(match.group(1))
    require(isinstance(data.get("runs"), list) and isinstance(data.get("evidence"), dict), "Snapshot data shape is invalid")
    for key, value in data["evidence"].items():
        require(isinstance(key, str) and isinstance(value, str) and SAFE_MEDIA.fullmatch(value), "Evidence must contain only allowlisted raster/video data URIs")
    def walk(value):
        if isinstance(value, dict):
            require(not {"archive_dir", "cli_command", "usage_raw", "api_key", "access_token", "authorization"}.intersection(value), "Private fields found in public data")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(data)
    require(not any(marker in match.group(1) for marker in ("/home/ubuntu/", "/workspace", "127.0.0.1")), "Public data contains private host details")
    return data


def evidence_paths(run):
    result = []
    for item in run.get("evaluation", {}).get("evidence", []):
        path = item if isinstance(item, str) else item.get("path", "")
        if (isinstance(path, str) and path and not path.startswith("/") and "\\" not in path
                and not any(part in {".", ".."} for part in path.split("/"))
                and not re.search(r"[\x00-\x1f]", path) and MEDIA_PATH.search(path) and path not in result):
            result.append(path)
    return result


def run_date(run):
    return str(run.get("date") or run.get("started_at") or "")[:10]


def validate_data(data, args, report):
    runs = data["runs"]
    ids = [run["id"] for run in runs]
    require(len(ids) == len(set(ids)), "Duplicate run IDs")
    require(len(runs) == args.expected_runs, f"Expected {args.expected_runs} runs, found {len(runs)}")
    reviews = [review for run in runs for review in run.get("reviews", [])]
    require(len(reviews) == args.expected_reviews, f"Expected {args.expected_reviews} reviews, found {len(reviews)}")
    require(len({review["id"] for review in reviews}) == len(reviews), "Duplicate review IDs")
    require(all(review.get("blind") is False and "AI" in review.get("reviewer", "")
                and "待" in review.get("reviewer", "") for review in reviews), "Legacy reviews must remain labeled nonblind AI suggestions awaiting human review")
    statuses = Counter(run["status"] for run in runs)
    require(statuses["failed"] == args.expected_failed, "Failed run history was changed or dropped")
    case_ids = args.case_id or list(NEW_CASES)
    cases = []
    for case_id in case_ids:
        run = next((run for run in runs if run["id"] == case_id), None)
        require(run is not None, "Required case missing: " + case_id)
        require(run["status"] == "completed" and run.get("evaluation", {}).get("status") == "completed", "Case is not generation/evaluation completed: " + case_id)
        checks = run.get("checks", [])
        require(len(checks) == 8 and len({check["name"] for check in checks}) == 8
                and all(check.get("status") in PASS for check in checks), "Case must have eight distinct passing checks: " + case_id)
        conditions = run.get("conditions", {})
        require(conditions.get("reasoning_effort_requested") == "max" and conditions.get("reasoning_effort_applied") == "max", "Case effort must actually be max: " + case_id)
        require(run.get("purpose") == "benchmark" and run_date(run) == args.date, "Case date/purpose mismatch: " + case_id)
        if not args.case_id:
            require(run.get("model") == NEW_CASES[case_id], "Unexpected new-case model: " + case_id)
            require(not run.get("reviews"), "New cases must not acquire invented reviews")
        for key in ("tool", "task_id", "prompt_version", "condition_fingerprint", "started_at", "finished_at"):
            require(bool(run.get(key)), "Missing case metadata: " + key)
        require(re.fullmatch(r"[a-f0-9]{64}", run.get("prompt_hash", "")), "Missing/invalid prompt hash")
        require(isinstance(run.get("duration_ms"), (int, float)) and math.isfinite(run["duration_ms"]) and run["duration_ms"] > 0, "Invalid recorded duration")
        paths = evidence_paths(run)
        require(any(MEDIA_PATH.search(path) and not path.lower().endswith((".webm", ".mp4")) for path in paths), "Case has no image evidence")
        require(any(path.lower().endswith((".webm", ".mp4")) for path in paths), "Case has no video evidence")
        require(all(f'{case_id}/{path}' in data["evidence"] for path in paths), "Case evidence bytes are missing")
        cases.append(run)
    report.update(counts={"runs": len(runs), "reviews": len(reviews), "statuses": dict(sorted(statuses.items()))},
                  reviews_label="nonblind AI suggestions awaiting human review",
                  cases=[{"id": run["id"], "model": run["model"], "date": run_date(run), "status": run["status"],
                          "evaluation": run["evaluation"]["status"], "checks": len(run["checks"]), "effort": "max",
                          "task": run["task_id"], "purpose": run["purpose"]} for run in cases])
    return cases


def overflow(page, label, report):
    result = page.evaluate("""() => ({width:innerWidth, documentWidth:document.documentElement.scrollWidth,
        dialogs:[...document.querySelectorAll('dialog[open]')].map(d=>({width:d.clientWidth,scrollWidth:d.scrollWidth}))})""")
    okay = result["documentWidth"] <= result["width"] and all(item["scrollWidth"] <= item["width"] + 1 for item in result["dialogs"])
    report["overflow_checks"].append({"view": label, **result, "pass": okay})
    require(okay, "Horizontal overflow: " + label)


def check_gallery(page, runs, date, report, expect):
    page.locator('[data-view="gallery"]').click()
    for purpose in ("", "benchmark"):
        page.locator('[name="purpose"]').select_option(purpose)
        for dated in (False, True):
            page.locator('[name="date_from"]').fill(date if dated else "")
            page.locator('[name="date_to"]').fill(date if dated else "")
            expected = [run for run in runs if (not purpose or run.get("purpose") == purpose)
                        and (not dated or run_date(run) == date)]
            expect(page.locator('#result-label')).to_contain_text(f'/ {len(expected)} 运行')
            covered = [run for run in expected if evidence_paths(run)]
            cards = page.locator('.evidence-card')
            expect(cards).to_have_count(len(covered))
            rendered = cards.evaluate_all("""cards=>cards.map(c=>({model:c.querySelector('h3').textContent,
                caption:c.querySelector('.caption p').textContent,alt:c.querySelector('img')?.alt,
                label:c.querySelector('.caption button').textContent,placeholder:!!c.querySelector('.media-frame p')}))""")
            for run, card in zip(covered, rendered):
                require(card["model"] == run["model"] and run_date(run) in card["caption"], "Gallery metadata/order mismatch")
                require(card["label"] == f'查看 {len(evidence_paths(run))} 份证据与详情', "Gallery evidence label mismatch")
                if card.get("alt"):
                    require(run["id"] in card["alt"], "Gallery image run ID mismatch")
            report["gallery_filters"].append({"purpose": purpose or "all", "date": date if dated else "all",
                                              "runs": len(expected), "cards": len(covered),
                                              "placeholders": sum(card["placeholder"] for card in rendered)})
    page.locator('#reset-filters').click()


def check_all_images(page, report):
    # Decode every embedded real image, not just lazy thumbnails in the viewport.
    labels = page.evaluate("Object.keys(window.BENCH_SNAPSHOT.evidence).filter(k=>window.BENCH_SNAPSHOT.evidence[k].startsWith('data:image/'))")
    checked = []
    for offset in range(0, len(labels), 8):
        results = page.evaluate("""keys=>Promise.all(keys.map(async label=>{const image=new Image();
            image.src=window.BENCH_SNAPSHOT.evidence[label];
            try {await image.decode(); return {label,width:image.naturalWidth,height:image.naturalHeight,pass:image.naturalWidth>0&&image.naturalHeight>0};}
            catch(error){return {label,pass:false,error:String(error).slice(0,160)};}}))""", labels[offset:offset + 8])
        checked.extend(results)
    report["media"]["images"] = {"embedded": len(labels), "decoded": sum(item["pass"] for item in checked),
                                    "labels": labels, "failures": [item for item in checked if not item["pass"]]}
    require(bool(labels) and all(item["pass"] for item in checked), "One or more embedded image bytes do not decode")


def set_theme(page, theme, expect):
    # The untouched viewer initially follows the system without data-theme.
    # Use the actual theme button, including its implicit-light first state.
    if not page.locator('html').get_attribute('data-theme'):
        page.locator('#theme-toggle').click()
    if page.locator('html').get_attribute('data-theme') != theme:
        page.locator('#theme-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-theme', theme)


def view_matrix(page, args, report, expect):
    for width, height, size in ((1440, 900, "desktop"), (400, 850, "phone")):
        page.set_viewport_size({"width": width, "height": height})
        for theme in ("light", "dark"):
            set_theme(page, theme, expect)
            for view in ('batch', 'gallery', 'trend', 'evidence', 'blind'):
                page.locator(f'[data-view="{view}"]').click()
                overflow(page, f'{view}-{size}-{theme}', report)
                if view == 'gallery':
                    name = f'gallery-{size}-{theme}.png'
                    page.screenshot(path=str(args.output / name))
                    report['screenshots'].append(name)
    page.locator('[data-view="batch"]').click()


def detail_checks(page, cases, args, report, expect):
    page.locator('[data-view="batch"]').click()
    for width, height, size in ((1440, 900, "desktop"), (400, 850, "phone")):
        page.set_viewport_size({"width": width, "height": height})
        for theme in ("light", "dark"):
            set_theme(page, theme, expect)
            overflow(page, f'batch-{size}-{theme}', report)
            for run in cases:
                # Real rendered run-name button opens the actual report dialog.
                page.locator('.run-name button').filter(has_text=re.compile('^' + run["id"] + '$')).click()
                expect(page.locator('#detail-dialog')).to_be_visible()
                expect(page.locator('#detail-title')).to_have_text(run["id"])
                for text in (run["model"], run["task_id"], args.date, "benchmark", "max（已设为该模型最高档）", "Evaluation: completed"):
                    expect(page.locator('#detail-content')).to_contain_text(text)
                checks_block = page.locator('.detail-block').filter(has=page.get_by_role('heading', name='Checks / Evaluation', exact=True))
                expect(checks_block.locator('.chip')).to_have_count(8)
                expect(page.locator('#detail-content')).to_contain_text('只读脱敏快照不包含、不请求文本日志。')
                if not run.get("reviews"):
                    expect(page.locator('#detail-content')).to_contain_text('暂无评分；不会推算或自动补齐。')
                require(page.locator('iframe,object,embed').count() == 0, "Model HTML embedded in detail")
                page.locator('#detail-content img').evaluate_all("images=>images.forEach(i=>i.loading='eager')")
                page.wait_for_function("[...document.querySelectorAll('#detail-content img')].every(i=>i.complete&&i.naturalWidth>0)")
                overflow(page, f'detail-{run["id"]}-{size}-{theme}', report)
                prefix = f'case-{run["id"]}-{size}-{theme}'
                page.locator('#detail-dialog').evaluate('(d)=>d.scrollTop=0')
                page.screenshot(path=str(args.output / f'{prefix}-metadata.png'))
                page.locator('#detail-content img').first.scroll_into_view_if_needed()
                page.screenshot(path=str(args.output / f'{prefix}-evidence.png'))
                report["screenshots"].extend([f'{prefix}-metadata.png', f'{prefix}-evidence.png'])
                if size == "desktop" and theme == "light":
                    for index in range(page.locator('#detail-content video').count()):
                        video = page.locator('#detail-content video').nth(index)
                        video.evaluate("async v=>{v.muted=true;v.preload='auto';await v.play();}")
                        page.wait_for_function("index=>{const v=document.querySelectorAll('#detail-content video')[index];return v.readyState>=2&&v.videoWidth>0&&v.currentTime>0&&!v.paused&&!v.error}", arg=index)
                        result = video.evaluate("v=>({label:v.getAttribute('aria-label'),width:v.videoWidth,height:v.videoHeight,duration:v.duration,currentTime:v.currentTime,readyState:v.readyState,played:!v.paused})")
                        result["run_id"] = run["id"]
                        report["media"]["videos_played"].append(result)
                        video.evaluate('v=>v.pause()')
                page.locator('[data-close="detail-dialog"]').click()


def verify(args, report):
    # Import only after argument parsing: --help works without host Playwright.
    from playwright.sync_api import expect, sync_playwright
    trusted = args.viewer_script.resolve(strict=True).read_text(encoding="utf-8")
    local_html = args.snapshot.read_text(encoding="utf-8") if args.snapshot else None
    document = {"data": audit_html(local_html, trusted) if local_html is not None else None}
    root_url = args.url or args.snapshot.as_uri() + '?purpose=benchmark'
    target_host = urlsplit(root_url).hostname
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--autoplay-policy=no-user-gesture-required'])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="light", service_workers="block", accept_downloads=False)
        context.set_default_timeout(args.timeout_ms)
        page = context.new_page()
        page.on('pageerror', lambda error: report["javascript_errors"].append(str(error)[:300]))
        page.on('console', lambda message: report["console_errors"].append(message.text[:300]) if message.type == 'error' else None)
        page.on('download', lambda download: report["forbidden_requests"].append({"type": "download"}))
        context.add_init_script("""(() => {window.__viewerForbidden=[];
            const stop=kind=>{window.__viewerForbidden.push(kind);throw new Error('Report viewer forbids '+kind);};
            window.fetch=()=>stop('fetch'); XMLHttpRequest.prototype.open=()=>stop('XMLHttpRequest');
            window.WebSocket=function(){stop('WebSocket')};window.EventSource=function(){stop('EventSource')};
            navigator.sendBeacon=()=>stop('sendBeacon');window.open=()=>stop('window.open');})();""")
        def route_handler(route):
            request = route.request
            url = urlsplit(request.url)
            if url.scheme in {"data", "blob"}:
                route.continue_()
                return
            api = bool(re.search(r'/(?:api|graphql)(?:/|$)', url.path, re.I)) or request.resource_type in {"fetch", "xhr", "websocket", "eventsource"} or request.method != "GET"
            allowed = (url.scheme == "https" and url.hostname == target_host and url.port in (None, 443)
                       and not url.username and not url.password) if args.url else request.url == root_url
            if api or not allowed or (request.resource_type == "document" and request.url != root_url):
                report["forbidden_requests"].append({"url": request.url, "method": request.method, "type": request.resource_type, "api": api})
                route.abort()
                return
            if args.url and request.resource_type == 'document':
                try:
                    response = route.fetch(max_redirects=0)
                    require(response.status == 200, f'Live report HTTP {response.status}')
                    require('text/html' in response.headers.get('content-type', ''), "Live response is not HTML")
                    body = response.body()
                    document["data"] = audit_html(body.decode('utf-8'), trusted)
                    report["source"].update(http_status=response.status, sha256=hashlib.sha256(body).hexdigest(), actual_network_fetch=True)
                    route.fulfill(response=response, body=body)
                except Exception as error:
                    document["error"] = str(error)[:300]
                    route.abort()
            else:
                route.continue_()
        context.route('**/*', route_handler)
        try:
            page.goto(root_url, wait_until='networkidle', timeout=max(120000, args.timeout_ms))
            require("error" not in document, document.get("error", ""))
            require(document["data"] is not None, "No actual snapshot document fetched")
            data = document["data"]
            cases = validate_data(data, args, report)
            expect(page.locator('#mode-label')).to_have_text('脱敏快照 · 只读')
            for selector in ('#auth-button', '#refresh-button', '[data-export="json"]', '[data-export="csv"]'):
                expect(page.locator(selector)).to_be_disabled()
            expect(page.locator('#auth-dialog')).not_to_be_visible()
            benchmark = [run for run in data["runs"] if run.get("purpose") == 'benchmark']
            expect(page.locator('#result-label')).to_contain_text(f'/ {len(benchmark)} 运行')
            expect(page.locator('#summary')).to_contain_text('unknown')
            check_gallery(page, data["runs"], args.date, report, expect)
            check_all_images(page, report)
            view_matrix(page, args, report, expect)
            detail_checks(page, cases, args, report, expect)
            page.locator('[name="status"]').select_option('failed')
            failed_runs = [run for run in benchmark if run['status'] == 'failed']
            expect(page.locator('.run-table tbody tr')).to_have_count(len(failed_runs))
            failed_rows = []
            for run in failed_runs:
                row = page.locator('.run-table tbody tr').filter(has=page.get_by_role('button', name=run['id'], exact=True))
                # CLI/session failure and page evaluation are independent facts.
                # An archived page may pass 8/8 while generation remains failed.
                expect(row.locator('td').nth(7)).to_contain_text('失败')
                checks = run.get('checks', [])
                evaluation = run.get('evaluation', {}).get('status')
                cell = row.locator('td').nth(8)
                if not checks or (evaluation and evaluation != 'completed'):
                    expect(cell).to_contain_text('未验收')
                else:
                    passed = sum(check.get('status') in PASS for check in checks)
                    expect(cell).to_have_text(f'{passed}/{len(checks)} 通过')
                failed_rows.append({'id': run['id'], 'generation': 'failed', 'evaluation': evaluation,
                                    'checks_label': cell.inner_text()})
            report['failed_generation_rows'] = failed_rows
            page.locator('#reset-filters').click()
            reviewed = next((run for run in benchmark if run.get('reviews')), None)
            if reviewed:
                page.locator('.run-name button').filter(has_text=re.compile('^' + reviewed['id'] + '$')).click()
                expect(page.locator('.review-list li')).to_have_count(len(reviewed['reviews']))
                expect(page.locator('.review-list')).to_contain_text('非盲评')
                expect(page.locator('.review-list')).to_contain_text('AI 建议评分')
                expect(page.locator('.review-list')).to_contain_text('待操作员复核')
                page.locator('[data-close="detail-dialog"]').click()
            page.locator('[data-view="blind"]').click()
            expect(page.locator('#view-content')).to_contain_text('盲评分写入已禁用')
            require(page.locator('#view-content input, #view-content textarea').count() == 0, "Writable review controls present")
            report['forbidden_requests'].extend({'type': kind} for kind in page.evaluate('window.__viewerForbidden'))
            require(not report['forbidden_requests'], "Network/API boundary violation")
            require(not report['javascript_errors'] and not report['console_errors'], "Viewer JavaScript/console errors")
            require(page.locator('iframe,object,embed').count() == 0, "Unsafe model embedding")
            report["media"]["embedded_videos"] = sum(value.startswith('data:video/') for value in data['evidence'].values())
            report["media"]["video_scope"] = "Decoded and played all videos in selected cases; other videos counted, not claimed decoded"
            report['readonly_controls'] = 'pass'
        finally:
            context.close()
            browser.close()


def main(argv=None):
    args = arguments(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "scope": "trusted_report_viewer_not_model_eval",
              "development_mode": bool(args.case_id) or args.expected_runs != 58,
              "source": {"mode": "live" if args.url else "local", "target": args.url or str(args.snapshot)},
              "media": {"videos_played": []}, "console_errors": [], "javascript_errors": [],
              "forbidden_requests": [], "overflow_checks": [], "gallery_filters": [], "screenshots": []}
    if args.snapshot:
        report['source']['sha256'] = hashlib.sha256(args.snapshot.read_bytes()).hexdigest()
    try:
        verify(args, report)
        report["status"] = "pass"
    except Exception as error:
        report["status"] = "fail"
        report["failure"] = f'{type(error).__name__}: {str(error)[:500]}'
    report_path = args.output / 'verification.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Read back the actual on-disk result, rather than trusting a write return.
    require(json.loads(report_path.read_text(encoding='utf-8')) == report, "Verification JSON readback mismatch")
    concise = {key: report[key] for key in ('status', 'development_mode', 'counts', 'failure') if key in report}
    concise.update(output=str(report_path), screenshots=len(report['screenshots']),
                   images_decoded=report['media'].get('images', {}).get('decoded', 0),
                   videos_played=len(report['media']['videos_played']),
                   console_errors=report['console_errors'], javascript_errors=report['javascript_errors'],
                   external_requests=report['forbidden_requests'], overflow_checks=len(report['overflow_checks']))
    print(json.dumps(concise, ensure_ascii=False, allow_nan=False))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
