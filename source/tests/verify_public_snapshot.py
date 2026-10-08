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
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from statistics import median
from urllib.parse import urljoin, urlsplit, unquote

ROOT = Path(__file__).resolve().parents[1]
NEW_CASES = {
    "6c3f48c1c35747369efe292af88de884": "gpt-6-sol",
    "42ec6adce94640f2a79659c7446ea3d3": "gpt-6.1-sol",
}
SAFE_MEDIA = re.compile(r"^data:(image/(?:png|jpeg|webp|gif)|video/(?:webm|mp4));base64,[A-Za-z0-9+/=\r\n]+$", re.I)
MEDIA_PATH = re.compile(r"\.(png|jpe?g|webp|gif|webm|mp4)$", re.I)
PASS = {"pass", "passed", "ok"}
STATIC_MEDIA = re.compile(r"^media/([a-f0-9]{64})\.(jpg|png|webp|gif|webm|mp4)$")
# Fixed throttling applies to real browser HTTP, not route.fulfilled synthetic bytes.
NETWORK_PROFILE = {"latency": 40, "downloadThroughput": 20_000_000 / 8,
                   "uploadThroughput": 5_000_000 / 8, "offline": False}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--snapshot", type=Path, help="Existing real generated HTML; never rebuilt by this verifier")
    target.add_argument("--url", help="Live HTTPS report on edgora-ai.github.io (actual network fetch, no credentials)")
    target.add_argument("--site", type=Path, help="Existing static-media-v1 directory; served read-only by an ephemeral local HTTP server")
    parser.add_argument("--baseline-snapshot", type=Path, help="Frozen old inline HTML for measured cold-load comparison, not a replacement fixture")
    parser.add_argument("--baseline-viewer-script", type=Path, help="Exact trusted old viewer source for --baseline-snapshot")
    parser.add_argument("--timing-repeats", type=int, default=3, help="Cold HTTP runs per version under the fixed 20 Mbit/s, 40ms profile")
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
    elif args.site:
        args.site = args.site.resolve(strict=True)
        require(args.site.is_dir() and (args.site / 'index.html').is_file(), "Site must contain an existing generated index.html")
    else:
        args.snapshot = args.snapshot.resolve(strict=True)
        require(args.snapshot.is_file() and args.snapshot.suffix.lower() == ".html", "Snapshot must be an existing HTML file")
    require(1 <= args.timing_repeats <= 5, "Timing repeats must be between one and five")
    require(bool(args.baseline_snapshot) == bool(args.baseline_viewer_script), "Baseline HTML and its trusted old viewer must be provided together")
    if args.baseline_snapshot:
        args.baseline_snapshot = args.baseline_snapshot.resolve(strict=True)
        args.baseline_viewer_script = args.baseline_viewer_script.resolve(strict=True)
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
    static = data.get('format') == 'static-media-v1'
    require(data.get('format') in (None, 'static-media-v1'), "Unsupported public report format")
    assets = data.get('assets', {})
    if static:
        require(isinstance(assets, dict) and assets, "Static report needs its media manifest")
        for path, asset in assets.items():
            match_path = STATIC_MEDIA.fullmatch(path)
            require(match_path and asset.get('sha256') == match_path[1], "Invalid content-addressed media path")
            require(asset.get('mime') == {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp',
                                         'gif': 'image/gif', 'webm': 'video/webm', 'mp4': 'video/mp4'}[match_path[2]], "Media MIME mismatch")
            require(isinstance(asset.get('size'), int) and asset['size'] > 0 and asset.get('role') in
                    {'evidence', 'thumbnail', 'evidence+thumbnail'}, "Invalid asset size or role")
        offline = data.get('offline', {})
        require(offline.get('path') == 'offline.html' and re.fullmatch(r'[a-f0-9]{64}', offline.get('sha256', ''))
                and isinstance(offline.get('size'), int) and offline['size'] > 0, "Invalid managed offline descriptor")
    for key, value in data['evidence'].items():
        require(isinstance(key, str) and isinstance(value, str), "Invalid evidence mapping")
        if static:
            require(value in assets and 'evidence' in assets[value]['role'], "Evidence path absent from exact asset manifest")
        else:
            require(SAFE_MEDIA.fullmatch(value), "Inline evidence must be raster/video data URIs")
    for key, value in data.get('thumbnails', {}).items():
        require(static and key in data['evidence'] and value in assets and assets[value]['mime'].startswith('image/')
                and 'thumbnail' in assets[value]['role'], "Thumbnail must name an exact declared raster asset")
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


@contextmanager
def serve_report(html_path, data):
    """Serve only the already-audited entry, declared media, and offline file."""
    html_path = html_path.resolve(strict=True)
    root = html_path.parent
    files = {'index.html': {'mime': 'text/html', 'path': html_path}}
    for name, meta in data.get('assets', {}).items():
        path = root / name
        require(not path.is_symlink() and not path.parent.is_symlink() and path.resolve().is_relative_to(root), "Unsafe local media path")
        files[name] = {**meta, 'path': path}
    if data.get('offline'):
        files['offline.html'] = {**data['offline'], 'mime': 'text/html', 'path': root / 'offline.html'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = unquote(urlsplit(self.path).path).lstrip('/') or 'index.html'
            item = files.get(name)
            if not item or not item['path'].is_file() or item['path'].is_symlink():
                self.send_error(404)
                return
            body = item['path'].read_bytes()
            if item.get('sha256') and hashlib.sha256(body).hexdigest() != item['sha256']:
                self.send_error(409, 'Declared asset hash mismatch')
                return
            self.send_response(200)
            self.send_header('Content-Type', item['mime'])
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Report-SHA256', hashlib.sha256(body).hexdigest())
            if name == 'offline.html':
                self.send_header('Content-Disposition', 'attachment; filename="offline.html"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Browsers cancel media when a dialog closes; no hidden retry.
                self.close_connection = True
        def log_message(self, format, *args):
            return None  # Expected HTTP access logging is omitted, never errors.
    server = ThreadingHTTPServer((os.environ.get('BENCH_VERIFY_BIND', '127.0.0.1'), 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://{server.server_address[0]}:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class BrowserGuard:
    """Exact asset lookup, read-only browser boundaries, and per-phase traffic."""
    def __init__(self, context, root_url, data, report):
        self.context, self.root_url, self.data, self.report = context, root_url, data, report
        self.phase = 'cold'
        self.offline_allowed = False
        self.assets = {urljoin(root_url, name): meta for name, meta in data.get('assets', {}).items()}
        self.traffic = []
        self.failures = []
        context.route('**/*', self.route)
        context.add_init_script("""(() => {window.__viewerForbidden=[];
            const stop=kind=>{window.__viewerForbidden.push(kind);throw new Error('Report viewer forbids '+kind);};
            window.fetch=()=>stop('fetch'); XMLHttpRequest.prototype.open=()=>stop('XMLHttpRequest');
            window.WebSocket=function(){stop('WebSocket')};window.EventSource=function(){stop('EventSource')};
            navigator.sendBeacon=()=>stop('sendBeacon');window.open=()=>stop('window.open');})();""")
        context.on('requestfinished', self.finished)

    def attach(self, page):
        self.session = self.context.new_cdp_session(page)
        self.session.send('Network.enable')
        page.on('pageerror', lambda error: self.report['javascript_errors'].append(str(error)[:300]))
        page.on('console', lambda message: self.report['console_errors'].append(message.text[:300]) if message.type == 'error' else None)
        page.on('download', lambda download: self.report['forbidden_requests'].append({'type': 'unexpected download'}) if not self.offline_allowed else None)

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        kind = request.resource_type
        if url.scheme == 'data':
            route.continue_()
            return
        offline = self.data.get('offline', {})
        allowed_offline = self.offline_allowed and offline and request.url == urljoin(self.root_url, offline['path'])
        allowed = request.method == 'GET' and (
            (request.url == self.root_url and kind == 'document') or
            (request.url in self.assets and kind in {'image', 'media'}) or allowed_offline)
        if not allowed:
            self.report['forbidden_requests'].append({'url': request.url, 'method': request.method, 'type': kind, 'phase': self.phase})
            route.abort()
            return
        # Never route.fulfill actual report/media: the browser's real HTTP stack
        # must be measured under the same CDP throttling for old and new pages.
        route.continue_()

    def finished(self, request):
        if urlsplit(request.url).scheme == 'data':
            return  # Inline bytes were already counted in the entry document.
        response = request.response()
        if response is None:
            return
        item = {'url': request.url, 'type': request.resource_type, 'phase': self.phase, 'status': response.status}
        try:
            item['transfer_bytes'] = request.sizes()['responseBodySize'] + request.sizes()['responseHeadersSize']
            asset = self.assets.get(request.url)
            if asset:
                item['role'] = asset['role']
                item['mime'] = response.headers.get('content-type', '').split(';')[0]
                require(response.status in (200, 206), 'Static asset HTTP failure')
                require(item['mime'] == asset['mime'], 'Static asset MIME mismatch')
                if request.resource_type == 'image' and response.status == 200:
                    body = response.body()
                    require(len(body) == asset['size'] and hashlib.sha256(body).hexdigest() == asset['sha256'], 'Image bytes mismatch manifest')
            elif request.url == self.root_url:
                require(response.status == 200, 'Root document HTTP failure')
                if urlsplit(self.root_url).scheme == 'http':
                    # Only --site/--snapshot can use HTTP, through our own server.
                    # Hash the actual served bytes server-side and require the
                    # browser's completed byte count: large inline baselines
                    # exceed Chromium's inspector response-body retention.
                    require(request.sizes()['responseBodySize'] == int(response.headers['content-length']), 'Local document transfer incomplete')
                    item['source_sha256'] = response.headers['x-report-sha256']
                    item['source_verified_by'] = 'owned HTTP server body hash and completed browser byte count'
                else:
                    item['source_sha256'] = hashlib.sha256(response.body()).hexdigest()
                    item['source_verified_by'] = 'actual HTTPS response body hash'
        except Exception as error:
            self.failures.append(str(error)[:250])
        self.traffic.append(item)

    def finish(self, page):
        self.report['forbidden_requests'].extend({'type': kind} for kind in page.evaluate('window.__viewerForbidden'))
        require(not self.failures, '; '.join(self.failures))
        require(not self.report['forbidden_requests'], 'Network/API boundary violation')
        require(not self.report['javascript_errors'] and not self.report['console_errors'], 'Viewer JavaScript/console errors')
        require(page.locator('iframe,object,embed').count() == 0, 'Unsafe model embedding')


def cold_load(browser, url, data, args, report, name, enforce_budget, viewport=None):
    viewport = viewport or {'width': 1440, 'height': 900}
    context = browser.new_context(viewport=viewport, color_scheme='light', service_workers='block')
    context.set_default_timeout(args.timeout_ms)
    page = context.new_page()
    guard = BrowserGuard(context, url, data, report)
    guard.attach(page)
    session = guard.session
    session.send('Network.setCacheDisabled', {'cacheDisabled': True})
    session.send('Network.emulateNetworkConditions', NETWORK_PROFILE)
    begin = time.perf_counter()
    page.goto(url, wait_until='domcontentloaded', timeout=180000)
    page.wait_for_function("document.querySelector('#workspace')?.getAttribute('aria-busy')==='false' && document.querySelector('#result-label')?.textContent.includes('运行')")
    readable_ms = (time.perf_counter() - begin) * 1000
    # Check enabled visible controls without trial-click scrolling, which would
    # change the initial viewport and contaminate thumbnail/request budgets.
    require(page.locator('[data-view="gallery"]').is_visible() and page.locator('[data-view="gallery"]').is_enabled(), 'Viewer controls not interactive')
    interactive_ms = (time.perf_counter() - begin) * 1000
    page.wait_for_load_state('networkidle', timeout=180000)
    finished_ms = (time.perf_counter() - begin) * 1000
    requests = list(guard.traffic)
    root = [row for row in requests if row['url'] == url]
    require(len(root) == 1, 'Cold run must make exactly one root document request')
    active_task = page.locator('[name="task_id"]').input_value()
    first_images = page.locator('.evidence-card img').evaluate_all("items=>items.filter(i=>{const b=i.getBoundingClientRect();return b.top<innerHeight&&b.bottom>0&&b.left<innerWidth&&b.right>0&&i.complete&&i.naturalWidth>0}).map(i=>({run_id:i.closest('[data-run-id]')?.dataset.runId,top:i.getBoundingClientRect().top,height:i.getBoundingClientRect().height,width:i.getBoundingClientRect().width}))")
    for image in first_images:
        image['model'] = next((run.get('model') for run in data['runs'] if run['id'] == image.get('run_id')), None)
    first_controls = page.locator('#task-tabs button, [data-view="gallery"]').evaluate_all("items=>items.filter(i=>{const b=i.getBoundingClientRect();return b.top<innerHeight&&b.bottom>0&&!i.disabled}).map(i=>({label:i.textContent,top:i.getBoundingClientRect().top,bottom:i.getBoundingClientRect().bottom}))")
    if enforce_budget:
        if viewport['width'] > 600:
            require(bool(first_images), 'Default desktop first viewport must show an actually loaded work image')
            if not urlsplit(url).query:
                require(len({image['model'] for image in first_images if image['model']}) >= 2, 'Default desktop must expose two different-model works in the first viewport')
        else:
            require(bool(first_controls), 'Phone first viewport lacks an actionable task/workspace entry')
            require(page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'Cold phone viewport overflows')
        require(report['source']['html_bytes'] <= 1024 * 1024, 'Static entry exceeds 1 MiB budget')
        require(sum(row['transfer_bytes'] for row in requests) <= 1536 * 1024, 'Cold first viewport exceeds 1.5 MiB transfer budget')
        require(not any(row['type'] == 'media' for row in requests), 'Video loaded before action')
        require(all('thumbnail' in row.get('role', '') for row in requests if row['type'] == 'image'), 'Full image loaded before action')
        allowed_thumbs = {urljoin(url, path) for key, path in data.get('thumbnails', {}).items()
                          if any(run['id'] == key.split('/')[0] and (not active_task or run.get('task_id') == active_task) for run in data['runs'])}
        require(all(row['url'] in allowed_thumbs for row in requests if row['type'] == 'image'), 'Off-task media loaded in cold viewport')
        require(page.locator('video[src],video source[src]').count() == 0, 'Video src attached before action')
    guard.finish(page)
    metrics = {'version': name, 'readable_ms': round(readable_ms, 1), 'interactive_ms': round(interactive_ms, 1),
               'settled_ms': round(finished_ms, 1), 'transfer_bytes': sum(row['transfer_bytes'] for row in requests),
               'requests': len(requests), 'image_requests': sum(row['type'] == 'image' for row in requests),
               'video_requests': sum(row['type'] == 'media' for row in requests), 'active_task': active_task,
               'source_sha256': root[0]['source_sha256'], 'source_verified_by': root[0]['source_verified_by'],
               'viewport': viewport, 'first_viewport_images': first_images, 'first_viewport_controls': first_controls}
    context.close()
    return metrics


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
    labels = page.evaluate("Object.keys(window.BENCH_SNAPSHOT.evidence).filter(k=>/\\.(png|jpe?g|webp|gif)$/i.test(k))")
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


def settle_theme(page):
    page.evaluate("""async()=>{getComputedStyle(document.body).backgroundColor;
        await Promise.allSettled(document.getAnimations().filter(a=>a.playState==='running'&&a.effect.getTiming().iterations!==Infinity).map(a=>a.finished));
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));}""")


def set_theme(page, theme, expect):
    # The untouched viewer initially follows the system without data-theme.
    # Use the actual theme button, including its implicit-light first state.
    if not page.locator('html').get_attribute('data-theme'):
        page.locator('#theme-toggle').click()
    if page.locator('html').get_attribute('data-theme') != theme:
        page.locator('#theme-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-theme', theme)
    settle_theme(page)


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


def all_filters(page):
    # More-filter disclosure may be collapsed in the visual-first workspace.
    page.locator('#filters details').evaluate_all('items=>items.forEach(d=>d.open=true)')


def open_run(page, run_id, expect):
    page.locator('[data-view="evidence"]').click()
    all_filters(page)
    page.locator('[name="task_id"]').select_option('')
    page.locator('[name="purpose"]').select_option('')
    page.locator('[name="q"]').fill(run_id)
    button = page.locator('.run-name button').filter(has_text=re.compile('^' + run_id + '$'))
    expect(button).to_have_count(1)
    button.click()
    expect(page.locator('#detail-dialog')).to_be_visible()
    expect(page.locator('#detail-title')).to_have_text(run_id)


def new_gallery_checks(page, data, args, report, expect):
    page.locator('[data-view="gallery"]').click()
    all_filters(page)
    page.locator('[name="task_id"]').select_option('')
    for purpose in ('', 'benchmark'):
        page.locator('[name="purpose"]').select_option(purpose)
        for dated in (False, True):
            page.locator('[name="date_from"]').fill(args.date if dated else '')
            page.locator('[name="date_to"]').fill(args.date if dated else '')
            runs = [run for run in data['runs'] if (not purpose or run.get('purpose') == purpose)
                    and (not dated or run_date(run) == args.date)]
            cards = page.locator('.evidence-card[data-run-id]')
            expect(cards).to_have_count(len(runs))
            require(set(cards.evaluate_all('cards=>cards.map(c=>c.dataset.runId)')) == {run['id'] for run in runs}, 'Gallery must preserve every filtered attempt including failures')
            report['gallery_filters'].append({'purpose': purpose or 'all', 'date': args.date if dated else 'all',
                                              'runs': len(runs), 'cards': cards.count(),
                                              'without_registered_media': sum(not evidence_paths(run) for run in runs)})
    page.locator('#reset-filters').click()
    for task in data['tasks']:
        task_id = task['id']
        page.locator(f'#task-tabs [data-task-id="{task_id}"]').click()
        expect(page.locator('[name="task_id"]')).to_have_value(task_id)
        expected = [run for run in data['runs'] if run.get('task_id') == task_id and run.get('purpose') == 'benchmark']
        expect(page.locator('.evidence-card[data-run-id]')).to_have_count(len(expected))
        require(page.locator('#task-context').inner_text().strip(), 'Task requirements/context missing')
    page.locator('#reset-filters').click()


def new_detail_checks(page, cases, args, report, expect):
    for width, height, size in ((1440, 900, 'desktop'), (400, 850, 'phone')):
        page.set_viewport_size({'width': width, 'height': height})
        for theme in ('light', 'dark'):
            set_theme(page, theme, expect)
            for run in cases:
                open_run(page, run['id'], expect)
                for text in (run['model'], run['task_id'], args.date, 'benchmark', 'max', 'completed'):
                    expect(page.locator('#detail-content')).to_contain_text(text)
                expect(page.locator('#detail-content')).to_contain_text('不包含、不请求文本日志')
                require(not page.locator('#detail-content video[src]').count(), 'Detail opened video without explicit playback')
                overflow(page, f'detail-{run["id"]}-{size}-{theme}', report)
                name = f'case-{run["id"]}-{size}-{theme}-details.png'
                page.screenshot(path=str(args.output / name))
                report['screenshots'].append(name)
                page.locator('[data-close="detail-dialog"]').click()
    page.locator('#reset-filters').click()


def compare_checks(page, cases, args, report, expect):
    if len(cases) < 2:
        report['comparison'] = {'skipped': 'Explicit single-case development mode'}
        return
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.locator('[data-view="gallery"]').click()
    page.locator('#reset-filters').click()
    page.locator(f'#task-tabs [data-task-id="{cases[0]["task_id"]}"]').click()
    page.locator(f'.evidence-card [data-select-run="{cases[0]["id"]}"]').click()
    page.locator('#mixed-comparison').check()
    for run in cases[1:]:
        page.locator(f'.evidence-card [data-select-run="{run["id"]}"]').click()
    expect(page.locator('#selection-label')).to_contain_text('2')
    page.locator('#compare-button').click()
    expect(page.locator('#compare-dialog')).to_be_visible()
    expect(page.locator('#compare-condition-warning')).to_be_visible()
    require(not page.locator('#compare-content video[src]').count(), 'Compare opened video without action')
    report['comparison'] = {'case_ids': [run['id'] for run in cases], 'stages': []}
    for width, height, size in ((1440, 900, 'desktop'), (400, 850, 'phone')):
        page.set_viewport_size({'width': width, 'height': height})
        for theme in ('light', 'dark'):
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
            settle_theme(page)
            page.locator('#compare-stage').select_option('desktop')
            for index in (range(2) if size == 'phone' else range(1)):
                if size == 'phone':
                    page.locator(f'#compare-ab [data-compare-index="{index}"]').click()
                images = page.locator('#compare-content img:visible')
                require(images.count() > 0, 'Comparison has no visible image')
                images.first.scroll_into_view_if_needed()
                page.wait_for_function("[...document.querySelectorAll('#compare-content img')].filter(i=>i.getClientRects().length).every(i=>i.complete&&i.naturalWidth>0)")
                overflow(page, f'compare-{size}-{theme}-{index}', report)
                name = f'comparison-{size}-{theme}-{index}.png'
                page.screenshot(path=str(args.output / name))
                report['screenshots'].append(name)
                page.locator('#compare-content [data-open-stage]:visible').first.click()
                expect(page.locator('#zoom-dialog')).to_be_visible()
                page.wait_for_function("[...document.querySelectorAll('#zoom-content img')].some(i=>i.complete&&i.naturalWidth>0)")
                overflow(page, f'zoom-{size}-{theme}-{index}', report)
                page.locator('[data-close="zoom-dialog"]').click()
    page.set_viewport_size({'width': 1440, 'height': 900})
    for stage in ('later', 'final', 'mobile'):
        page.locator('#compare-stage').select_option(stage)
        page.wait_for_function("[...document.querySelectorAll('#compare-content img')].every(i=>i.complete&&i.naturalWidth>0)")
        require(page.locator('#compare-content img').count() == 2, 'Selected case stage missing: ' + stage)
        report['comparison']['stages'].append(stage)
    page.locator('#compare-stage').select_option('video')
    require(page.locator('#compare-content video[src]').count() == 0, 'Video stage assigned src before play')
    page.locator('#play-videos').click()
    page.wait_for_function("[...document.querySelectorAll('#compare-content video')].length===2&&[...document.querySelectorAll('#compare-content video')].every(v=>v.readyState>=2&&v.videoWidth>0&&v.currentTime>0&&!v.paused&&!v.error)")
    videos = page.locator('#compare-content video').evaluate_all("vs=>vs.map(v=>({label:v.getAttribute('aria-label'),width:v.videoWidth,height:v.videoHeight,duration:v.duration,currentTime:v.currentTime,readyState:v.readyState,played:!v.paused}))")
    for run, video in zip(cases, videos):
        report['media']['videos_played'].append({**video, 'run_id': run['id']})
    page.locator('#pause-videos').click()
    require(page.locator('#compare-content video').evaluate_all('vs=>vs.every(v=>v.paused)'), 'Pause did not pause all selected videos')
    page.locator('#restart-videos').click()
    page.wait_for_function("[...document.querySelectorAll('#compare-content video')].every(v=>!v.paused&&v.currentTime<2)")
    page.locator('[data-close="compare-dialog"]').click()
    require(page.locator('#compare-content video[src]').count() == 0, 'Closed comparison retained video source')
    page.locator('#clear-selection').click()


def failed_and_review_checks(page, data, report, expect, new_ui):
    page.locator('[data-view="evidence"]').click()
    all_filters(page)
    page.locator('#reset-filters').click()
    page.locator('[name="task_id"]').select_option('')
    page.locator('[name="purpose"]').select_option('')
    page.locator('[name="status"]').select_option('failed')
    failed_runs = [run for run in data['runs'] if run['status'] == 'failed']
    expect(page.locator('.run-table tbody tr')).to_have_count(len(failed_runs))
    rows = []
    for run in failed_runs:
        row = page.locator('.run-table tbody tr').filter(has=page.get_by_role('button', name=run['id'], exact=True))
        text = row.inner_text()
        require('失败' in text, 'Failed generation status lost')
        checks, evaluation = run.get('checks', []), run.get('evaluation', {}).get('status')
        label = '未验收' if not checks or (evaluation and evaluation != 'completed') else f'{sum(c.get("status") in PASS for c in checks)}/{len(checks)} 通过'
        require(label in text, 'Failed generation/evaluation facts conflated: ' + run['id'])
        rows.append({'id': run['id'], 'generation': 'failed', 'evaluation': evaluation, 'checks_label': label})
    report['failed_generation_rows'] = rows
    page.locator('#reset-filters').click()
    reviewed = next((run for run in data['runs'] if run.get('reviews')), None)
    if reviewed:
        open_run(page, reviewed['id'], expect)
        # Records can be in a collapsed detail disclosure; text remains intact.
        expect(page.locator('.review-list li')).to_have_count(len(reviewed['reviews']))
        for label in ('非盲评', 'AI 建议评分', '待操作员复核'):
            expect(page.locator('.review-list')).to_contain_text(label)
        page.locator('[data-close="detail-dialog"]').click()
    page.locator('[data-view="blind"]').click()
    expect(page.locator('#view-content')).to_contain_text('写入已禁用')
    require(page.locator('#view-content input, #view-content textarea').count() == 0, 'Writable public reviews')
    report['readonly_controls'] = 'pass'


def explicit_filter_compatibility(browser, url, data, args, report):
    if data.get('format') != 'static-media-v1':
        return
    from playwright.sync_api import expect
    filtered = url.split('#')[0].split('?')[0] + '?purpose=benchmark'
    expected = sum(run.get('purpose') == 'benchmark' for run in data['runs'])
    for fragment in ('', '#evidence'):
        context = browser.new_context(service_workers='block')
        page = context.new_page()
        guard = BrowserGuard(context, filtered, data, report)
        guard.attach(page)
        page.goto(filtered + fragment, wait_until='networkidle')
        expect(page.locator('[name="task_id"]')).to_have_value('')
        expect(page.locator('#result-label')).to_contain_text(f'{expected} 运行')
        if fragment:
            expect(page.locator('.run-table tbody tr')).to_have_count(expected)
        else:
            expect(page.locator('.evidence-card[data-run-id]')).to_have_count(expected)
        guard.finish(page)
        context.close()
    report['explicit_filter_compatibility'] = {'purpose_benchmark': 'all tasks preserved', 'legacy_evidence_hash': 'pass', 'runs': expected}


def offline_download_check(browser, url, data, args, report):
    if not data.get('offline'):
        return
    context = browser.new_context(accept_downloads=True, service_workers='block')
    page = context.new_page()
    guard = BrowserGuard(context, url, data, report)
    guard.attach(page)
    page.goto(url, wait_until='networkidle')
    guard.phase = 'explicit-offline-download'
    guard.offline_allowed = True
    with page.expect_download(timeout=120000) as pending:
        page.locator('#offline-download').click()
    path = args.output / 'downloaded-offline.html'
    pending.value.save_as(path)
    require(len(path.read_bytes()) == data['offline']['size'] and hashlib.sha256(path.read_bytes()).hexdigest() == data['offline']['sha256'], 'Explicit offline download hash/size mismatch')
    require(page.url.split('#')[0].split('?')[0] == url.split('?')[0], 'Offline download navigated the report')
    guard.finish(page)
    report['offline_download'] = {'sha256': data['offline']['sha256'], 'size': data['offline']['size'], 'pass': True}
    context.close()


def browser_suite(browser, url, data, args, report):
    from playwright.sync_api import expect
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='light', service_workers='block')
    context.set_default_timeout(args.timeout_ms)
    page = context.new_page()
    guard = BrowserGuard(context, url, data, report)
    guard.attach(page)
    try:
        page.goto(url, wait_until='networkidle', timeout=180000)
        cases = validate_data(data, args, report)
        expect(page.locator('#mode-label')).to_have_text('脱敏快照 · 只读')
        for selector in ('#auth-button', '#refresh-button', '[data-export="json"]', '[data-export="csv"]'):
            expect(page.locator(selector)).to_be_disabled()
        expect(page.locator('#auth-dialog')).not_to_be_visible()
        new_ui = page.locator('#task-tabs').count() > 0
        guard.phase = 'explicit-ui-interactions'
        if new_ui:
            new_gallery_checks(page, data, args, report, expect)
            for width, height, size in ((1440, 900, 'desktop'), (400, 850, 'phone')):
                page.set_viewport_size({'width': width, 'height': height})
                for theme in ('light', 'dark'):
                    set_theme(page, theme, expect)
                    for view in ('gallery', 'matrix', 'batch', 'trend', 'evidence', 'blind'):
                        page.locator(f'[data-view="{view}"]').click()
                        overflow(page, f'{view}-{size}-{theme}', report)
                        if view == 'gallery':
                            name = f'gallery-{size}-{theme}.png'
                            page.screenshot(path=str(args.output / name))
                            report['screenshots'].append(name)
            new_detail_checks(page, cases, args, report, expect)
            compare_checks(page, cases, args, report, expect)
        else:
            check_gallery(page, data['runs'], args.date, report, expect)
            view_matrix(page, args, report, expect)
            detail_checks(page, cases, args, report, expect)
        failed_and_review_checks(page, data, report, expect, new_ui)
        # Bulk decoding is deliberately last; it cannot contaminate cold budgets.
        guard.phase = 'explicit-all-image-audit'
        check_all_images(page, report)
        report['media']['embedded_videos'] = sum(key.lower().endswith(('.webm', '.mp4')) for key in data['evidence'])
        report['media']['video_scope'] = 'Decoded and played selected-case videos; remaining video assets counted only'
        guard.finish(page)
        report['network'] = {'requests': len(guard.traffic), 'verified_image_responses': sum(row['type'] == 'image' for row in guard.traffic),
                             'api_or_external_requests': 0, 'phases': dict(Counter(row['phase'] for row in guard.traffic))}
    finally:
        context.close()


def verify(args, report):
    from playwright.sync_api import sync_playwright
    trusted = args.viewer_script.resolve(strict=True).read_text(encoding='utf-8')
    with sync_playwright() as playwright:
        # Preflight uses a separate request client, never report-page fetch/XHR.
        # Remote documents are audited before any browser is allowed to execute.
        if args.url:
            client = playwright.request.new_context()
            try:
                response = client.get(args.url, max_redirects=0, timeout=120000)
                require(response.status == 200 and 'text/html' in response.headers.get('content-type', ''), 'Live report did not return HTTP200 HTML')
                html_bytes = response.body()
                report['source'].update(http_status=response.status, actual_network_fetch=True)
            finally:
                client.dispose()
            html_path = None
        else:
            html_path = args.site / 'index.html' if args.site else args.snapshot
            html_bytes = html_path.read_bytes()
        data = audit_html(html_bytes.decode('utf-8'), trusted)
        report['source'].update(sha256=hashlib.sha256(html_bytes).hexdigest(), html_bytes=len(html_bytes), format=data.get('format', 'inline'))
        validate_data(data, args, report)
        browser = playwright.chromium.launch(headless=True, args=['--autoplay-policy=no-user-gesture-required'])
        try:
            @contextmanager
            def target():
                if args.url:
                    yield args.url
                else:
                    with serve_report(html_path, data) as local_url:
                        yield local_url
            with target() as url:
                metrics = []
                for _ in range(args.timing_repeats):
                    sample = cold_load(browser, url, data, args, report, 'current', data.get('format') == 'static-media-v1')
                    require(sample['source_sha256'] == report['source']['sha256'], 'Browser received a different document than audited preflight')
                    metrics.append(sample)
                report['cold_load'] = {'profile': NETWORK_PROFILE, 'viewport': {'width': 1440, 'height': 900},
                                       'samples': metrics, 'median_readable_ms': median(m['readable_ms'] for m in metrics),
                                       'median_interactive_ms': median(m['interactive_ms'] for m in metrics)}
                if args.baseline_snapshot:
                    old_html = args.baseline_snapshot.read_text(encoding='utf-8')
                    old_data = audit_html(old_html, args.baseline_viewer_script.read_text(encoding='utf-8'))
                    # Metadata equivalence is checked independently from media delivery.
                    require(old_data['runs'] == data['runs'] and old_data['tasks'] == data['tasks'], 'Baseline/current metadata changed')
                    with serve_report(args.baseline_snapshot, old_data) as old_url:
                        old_metrics = [cold_load(browser, old_url, old_data, args, report, 'baseline', False)
                                       for _ in range(args.timing_repeats)]
                    report['cold_load']['baseline_samples'] = old_metrics
                    report['cold_load']['baseline_median_readable_ms'] = median(m['readable_ms'] for m in old_metrics)
                    report['cold_load']['baseline_median_interactive_ms'] = median(m['interactive_ms'] for m in old_metrics)
                if data.get('format') == 'static-media-v1':
                    report['cold_load']['phone_sample'] = cold_load(browser, url, data, args, report, 'current-phone', True,
                                                                    {'width': 400, 'height': 850})
                browser_suite(browser, url, data, args, report)
                explicit_filter_compatibility(browser, url, data, args, report)
                offline_download_check(browser, url, data, args, report)
        finally:
            browser.close()


def main(argv=None):
    args = arguments(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "scope": "trusted_report_viewer_not_model_eval",
              "development_mode": bool(args.case_id) or args.expected_runs != 58,
              "source": {"mode": "live" if args.url else "site" if args.site else "local", "target": args.url or str(args.site or args.snapshot)},
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
