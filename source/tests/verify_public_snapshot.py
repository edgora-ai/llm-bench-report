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
import gzip
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
    target.add_argument("--site", type=Path, help="Existing static-media-v1/v2 directory; served read-only by an ephemeral local HTTP server")
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
    parser.add_argument("--preview-runtime", type=Path, default=ROOT / "web/preview-runtime.js", help="Independently trusted runtime for exact v2 composition; unused for v1")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--performance-only", action="store_true", help="Bounded Chromium v2 real-HTTP gzip cold loads; skip original/media census and full regressions")
    parser.add_argument("--ux-only", action="store_true", help="Focused current-product search/history/options/mobile comparison regressions, no original census")
    parser.add_argument("--baseline-site", type=Path, help="Frozen external v2 site for paired performance/UX checks; needs its separately trusted --baseline-viewer-script")
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
    require(not (args.performance_only and args.ux_only), "Choose performance-only or ux-only")
    require(not (args.baseline_snapshot and args.baseline_site), "Choose one baseline format")
    require(bool(args.baseline_snapshot or args.baseline_site) == bool(args.baseline_viewer_script), "Baseline and its independently trusted viewer must be provided together")
    if args.performance_only or args.ux_only:
        require(args.site is not None and args.timing_repeats <= 3, "Focused modes need --site and at most three repeats")
        require(not args.baseline_snapshot, "Focused comparisons require frozen v2 --baseline-site, not legacy inline HTML")
    elif args.baseline_site:
        require(False, "--baseline-site is supported only by focused modes")
    if args.baseline_site:
        args.baseline_site = args.baseline_site.resolve(strict=True)
        require(args.baseline_site.is_dir() and (args.baseline_site / 'index.html').is_file(), "Baseline site must contain index.html")
        args.baseline_viewer_script = args.baseline_viewer_script.resolve(strict=True)
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


def audit_external_original_descriptors(data):
    """Descriptor-only focused audit; never import renderer/image-builder dependencies."""
    require(data.get('transport') == 'external', 'Focused audit requires external originals')
    originals = data.get('originals')
    require(isinstance(originals, dict) and set(originals) == {run['id'] for run in data['runs']}, 'Original descriptor coverage')
    sha = lambda value: isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value)
    for descriptor in originals.values():
        require(isinstance(descriptor, dict) and set(descriptor) == {'status', 'entrypoint', 'entry_sha256', 'missing', 'policy', 'package'}, 'Original descriptor schema')
        status = descriptor['status']
        require(status in {'ready', 'missing_dependencies', 'no_entrypoint', 'not_reviewed', 'withheld', 'unsupported'} and descriptor['policy'] == 'opaque-srcdoc-v1', 'Original status/policy')
        missing = descriptor['missing']
        require(isinstance(missing, list) and all(isinstance(path, str) and path and all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) and part not in {'.', '..'} for part in path.split('/')) for path in missing), 'Unsafe missing-dependency paths')
        require(missing == sorted(set(missing)), 'Noncanonical missing-dependency list')
        if status == 'no_entrypoint':
            require(descriptor['entrypoint'] is None and descriptor['entry_sha256'] is None and not missing, 'Absent entrypoint descriptor')
        else:
            require(descriptor['entrypoint'] == 'index.html' and sha(descriptor['entry_sha256']), 'Original entrypoint descriptor')
        ready = status in {'ready', 'missing_dependencies'}
        info = descriptor['package']
        require((info is not None) == ready, 'Original status/package mismatch')
        if ready:
            require((status == 'missing_dependencies') == bool(missing), 'Original missing-dependency status mismatch')
            require(isinstance(info, dict) and set(info) == {'sha256', 'size', 'path'} and sha(info['sha256']), 'Original package descriptor schema')
            require(type(info['size']) is int and 0 < info['size'] <= 4 * 1024 * 1024 and info['path'] == 'originals/' + info['sha256'] + '.json', 'Original package size/path')


def audit_html(html, trusted_script, trusted_runtime=None, descriptors_only=False):
    parsed = ReportHTML()
    parsed.feed(html)
    parsed.close()
    require(not parsed.unsafe, "Unsafe report markup: " + ", ".join(sorted(set(parsed.unsafe))))
    require(len(parsed.scripts) == 2, "Report must contain only snapshot JSON and trusted viewer code")
    match = re.fullmatch(r"window\.BENCH_SNAPSHOT=(\{.*\});", parsed.scripts[0], re.S)
    require(match is not None and "<" not in parsed.scripts[0], "Snapshot JSON must be safely escaped, without embedded model markup")
    data = json.loads(match.group(1))
    v2 = data.get('format') == 'static-media-v2'
    if v2:
        require(isinstance(trusted_runtime, str) and trusted_runtime, "v2 audit requires independently trusted preview-runtime.js")
        require(parsed.scripts[1] == trusted_runtime + '\n' + trusted_script, "Inline executable code differs from trusted runtime + viewer composition")
        if descriptors_only:
            audit_external_original_descriptors(data)
        else:
            # Import only this verifier's trusted sibling, never the report directory.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from make_originals import validate_originals
            validate_originals(data)
        require(set(data) <= {'runs', 'tasks', 'evidence', 'format', 'transport', 'originals', 'assets', 'thumbnails', 'offline'}, "Unknown v2 report fields")
    else:
        require(parsed.scripts[1] == trusted_script, "Inline executable code does not match trusted web/app.js")
        require(not {'transport', 'originals'}.intersection(data), "v2 fields in legacy report")
    require(isinstance(data.get("runs"), list) and isinstance(data.get("evidence"), dict), "Snapshot data shape is invalid")
    static = data.get('format') == 'static-media-v1' or (v2 and data['transport'] == 'external')
    require(data.get('format') in (None, 'static-media-v1', 'static-media-v2'), "Unsupported public report format")
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
    if v2 and not static:
        require(not {'assets', 'thumbnails', 'offline'}.intersection(data), "Inline v2 contains external media fields")
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
def serve_report(html_path, data, gzip_html=False):
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
    # Compress once, outside the measured request. Media is already compressed.
    entry_bytes = html_path.read_bytes() if gzip_html else None
    entry_gzip = gzip.compress(entry_bytes, compresslevel=6, mtime=0) if gzip_html else None
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
            if gzip_html and name == 'index.html' and body != entry_bytes:
                self.send_error(409, 'Audited document changed')
                return
            self.send_response(200)
            self.send_header('Content-Type', item['mime'])
            source_hash = hashlib.sha256(body).hexdigest()
            if gzip_html and name == 'index.html':
                self.send_header('Vary', 'Accept-Encoding')
                if 'gzip' in self.headers.get('Accept-Encoding', '').lower():
                    body = entry_gzip
                    self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Report-SHA256', source_hash)
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
    first_originals = []
    if data.get('format') == 'static-media-v2':
        first_originals = page.locator('.evidence-card [data-run-original]').evaluate_all("items=>items.filter(i=>{const b=i.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight&&b.left>=0&&b.right<=innerWidth&&!i.disabled&&i.textContent==='运行原作'}).map(i=>({run_id:i.dataset.runOriginal,top:i.getBoundingClientRect().top,bottom:i.getBoundingClientRect().bottom}))")
        require(bool(first_originals), 'v2 first viewport lacks a fully visible actionable original button')
        require(not any('/originals/' in row['url'] for row in requests), 'Original package fetched before explicit action')
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
               'viewport': viewport, 'first_viewport_images': first_images, 'first_viewport_controls': first_controls,
               'first_viewport_originals': first_originals}
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
    # Wait for the actual debounced filter commit before opening a dialog;
    # v2 correctly closes running previews/dialogs when filters later change.
    expect(page.locator('.run-table tbody tr')).to_have_count(1)
    button = page.locator('.run-name button').filter(has_text=re.compile('^' + run_id + '$'))
    expect(button).to_have_count(1)
    button.click()
    expect(page.locator('#detail-dialog')).to_be_visible()
    expect(page.locator('#detail-title')).to_have_text(run_id)


def expand_no_media_history(page):
    history = page.locator('#no-media-history')
    if history.count() and not history.evaluate('d=>d.open'):
        history.locator('summary').click()


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
            expect(page.locator('#result-label')).to_contain_text(f'{len(runs)} 运行')
            expand_no_media_history(page)
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
        expect(page.locator('#result-label')).to_contain_text(f'{len(expected)} 运行')
        expand_no_media_history(page)
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
    if data.get('format') not in {'static-media-v1', 'static-media-v2'}:
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
            expand_no_media_history(page)
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


PERFORMANCE_PROFILES = {
    'original20Mbps40ms': {'network': NETWORK_PROFILE, 'cpu': 1},
    'slow1.6Mbps150ms4xCPU': {'network': {'latency': 150, 'downloadThroughput': 1_600_000 / 8,
        'uploadThroughput': 750_000 / 8, 'offline': False}, 'cpu': 4},
}
PERFORMANCE_VIEWPORTS = {'desktop': {'width': 1440, 'height': 900}, 'phone': {'width': 400, 'height': 850}}
PERFORMANCE_OBSERVER = """(() => {
    const metrics=window.__loadMetrics={longtasks:[],readable:null,firstVisibleImage:null};
    new PerformanceObserver(list=>list.getEntries().forEach(e=>metrics.longtasks.push({start:e.startTime,duration:e.duration}))).observe({type:'longtask',buffered:true});
    const visible=node=>{if(!node||!node.getClientRects().length)return false;
        const r=node.getBoundingClientRect(),s=getComputedStyle(node);
        return r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0&&s.visibility!=='hidden'&&s.display!=='none'&&Number(s.opacity)>0;};
    const tick=()=>{
        if(metrics.readable===null&&document.querySelector('#workspace')?.getAttribute('aria-busy')==='false'&&
            visible(document.querySelector('#result-label'))&&document.querySelector('#result-label').textContent.includes('运行'))metrics.readable=performance.now();
        if(metrics.firstVisibleImage===null){const image=[...document.querySelectorAll('.evidence-card img')].find(i=>i.complete&&i.naturalWidth>0&&visible(i));
            if(image)metrics.firstVisibleImage={ms:performance.now(),src:image.currentSrc,run_id:image.closest('[data-run-id]')?.dataset.runId};}
        requestAnimationFrame(tick);
    };requestAnimationFrame(tick);
})();"""


def performance_summary(samples):
    keys = ('ttfb_ms', 'readable_ms', 'interactive_ms', 'first_actually_visible_image_ms', 'dom_nodes',
            'longtask_count', 'longtask_total_ms', 'transfer_bytes', 'filter_total_ms', 'filter_after_debounce_ms')
    result = {}
    for key in keys:
        values = [row[key] for row in samples if row.get(key) is not None]
        result[key] = {'median': round(median(values), 2) if values else None, 'observed': len(values)}
    return result


def performance_sample(browser, url, data, args, name, profile_name, viewport_name, repeat):
    """Real gzip HTTP + CDP throttling; never BrowserGuard routes or API monkeypatches."""
    profile = PERFORMANCE_PROFILES[profile_name]
    context = browser.new_context(viewport=PERFORMANCE_VIEWPORTS[viewport_name], color_scheme='light', service_workers='block')
    context.set_default_timeout(args.timeout_ms)
    errors, requests, document_responses = [], [], []
    try:
        context.add_init_script(PERFORMANCE_OBSERVER)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        page.on('request', lambda request: requests.append({'url': request.url, 'method': request.method, 'type': request.resource_type}))
        page.on('response', lambda response: document_responses.append(response) if response.request.resource_type == 'document' else None)
        session = context.new_cdp_session(page)
        session.send('Network.enable')
        session.send('Network.setCacheDisabled', {'cacheDisabled': True})
        session.send('Network.emulateNetworkConditions', profile['network'])
        session.send('Emulation.setCPUThrottlingRate', {'rate': profile['cpu']})
        response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
        require(response.status == 200 and response.headers.get('content-encoding') == 'gzip', 'Cold load must receive actual HTTP200 gzip document')
        source_hash = hashlib.sha256((args.site / 'index.html').read_bytes()).hexdigest() if name == 'current' else hashlib.sha256((args.baseline_site / 'index.html').read_bytes()).hexdigest()
        require(response.headers.get('x-report-sha256') == source_hash, 'Measured document differs from audited source')
        page.wait_for_function('window.__loadMetrics?.readable!==null')
        # Exercise a real viewer event, then wait two animation frames for its render.
        # No synthetic API implementation is installed. This is an observed ready
        # control response, not a claim about the browser's formal TTI metric.
        page.locator('[data-view="gallery"]').click()
        interactive = page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(performance.now()))))')
        page.wait_for_load_state('networkidle', timeout=60000)
        page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        metrics = page.evaluate("""() => {const m=window.__loadMetrics,n=performance.getEntriesByType('navigation')[0];
            return {ttfb_ms:n.responseStart,server_ttfb_ms:n.responseStart-n.requestStart,readable_ms:m.readable,
                first_actually_visible_image_ms:m.firstVisibleImage?.ms??null,first_visible_image:m.firstVisibleImage,
                dom_nodes:document.getElementsByTagName('*').length,longtasks:m.longtasks.slice(),
                transfer_bytes:n.transferSize+performance.getEntriesByType('resource').reduce((s,r)=>s+r.transferSize,0),
                document_encoded_bytes:n.encodedBodySize,document_decoded_bytes:n.decodedBodySize,
                active_task:document.querySelector('[name="task_id"]').value,scroll_x:scrollX,scroll_y:scrollY,
                initial_viewport_image_elements:[...document.querySelectorAll('.evidence-card img')].filter(i=>{const r=i.getBoundingClientRect();return i.getClientRects().length&&r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0}).length};}""")
        metrics.update(version=name, profile=profile_name, viewport=viewport_name, repeat=repeat, interactive_ms=interactive,
                       source_sha256=source_hash, longtask_count=len(metrics['longtasks']),
                       longtask_total_ms=sum(task['duration'] for task in metrics['longtasks']))
        if metrics['first_actually_visible_image_ms'] is None:
            metrics['first_image_note'] = 'No loaded image intersected the unscrolled viewport before network idle; no scroll performed'
        cold_requests = list(requests)
        require(metrics['scroll_x'] == 0 and metrics['scroll_y'] == 0, 'Interactive probe scrolled the initial viewport; image timing invalid')
        require(len(document_responses) == 1, 'Cold run needs exactly one document response')
        require(not any('/originals/' in row['url'] for row in cold_requests), 'Cold load fetched an original package')
        # Separate warm filter response, including the documented 250ms debounce.
        candidate = next((run for run in data['runs'] if run.get('task_id') == metrics['active_task']
                          and run.get('purpose') == 'benchmark' and evidence_paths(run)), None)
        require(candidate is not None, 'No media-bearing default-task run for filter measurement')
        query = candidate['id']
        all_filters(page)  # Filter measurement starts after the cold viewport capture.
        page.evaluate("""() => {document.querySelector('[name="q"]').addEventListener('input',()=>{
            window.__filterStart=performance.now();window.__filterTaskIndex=window.__loadMetrics.longtasks.length;},{capture:true,once:true});}""")
        page.locator('[name="q"]').fill(query)
        page.wait_for_function("""q=>new URL(location.href).searchParams.get('q')===q&&
            document.querySelector('#workspace').getAttribute('aria-busy')==='false'&&
            document.querySelectorAll('.evidence-card[data-run-id]').length===1&&document.querySelector('.evidence-card').dataset.runId===q""", arg=query)
        filter_metrics = page.evaluate("""()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({
            total:performance.now()-window.__filterStart,longtasks:window.__loadMetrics.longtasks.slice(window.__filterTaskIndex)}))))""")
        metrics.update(filter_query=query, filter_debounce_ms=250, filter_total_ms=filter_metrics['total'],
                       filter_after_debounce_ms=max(0, filter_metrics['total'] - 250), filter_longtasks=filter_metrics['longtasks'],
                       cold_requests=cold_requests)
        require(not errors, 'Viewer errors: ' + '; '.join(errors))
        require(all(row['method'] == 'GET' and urlsplit(row['url']).netloc == urlsplit(url).netloc and '/api/' not in row['url'] for row in requests), 'Unexpected nonlocal, API or write request')
        return metrics
    finally:
        context.close()


def focused_sources(args, report):
    runtime = args.preview_runtime.resolve(strict=True).read_text(encoding='utf-8')
    sources = {}
    for name, site, script in [('current', args.site, args.viewer_script)] + (
            [('baseline', args.baseline_site, args.baseline_viewer_script)] if args.baseline_site else []):
        html = (site / 'index.html').read_bytes()
        viewer = script.resolve(strict=True).read_text(encoding='utf-8')
        data = audit_html(html.decode('utf-8'), viewer, runtime, descriptors_only=True)
        require(data.get('format') == 'static-media-v2' and data.get('transport') == 'external', 'Focused modes require external static-media-v2')
        validate_data(data, args, report)
        sources[name] = {'site': site, 'data': data}
        report.setdefault('sources', {})[name] = {'site': str(site), 'sha256': hashlib.sha256(html).hexdigest(),
            'viewer_sha256': hashlib.sha256(viewer.encode()).hexdigest(), 'runtime_sha256': hashlib.sha256(runtime.encode()).hexdigest(),
            'html_bytes': len(html), 'gzip_bytes': len(gzip.compress(html, compresslevel=6, mtime=0))}
    if 'baseline' in sources:
        current, baseline = sources['current']['data'], sources['baseline']['data']
        # Managed offline HTML embeds the version-specific viewer, so its
        # already-audited digest/size must change when app.js changes.
        require({key: value for key, value in current.items() if key != 'offline'} ==
                {key: value for key, value in baseline.items() if key != 'offline'},
                'Baseline/current snapshot metadata, originals or media mappings changed')
    return sources


def focused_ux(browser, url, data, args, report):
    """Current product only: exact search IDs and bounded loading/interaction checks."""
    from playwright.sync_api import expect
    context = browser.new_context(viewport=PERFORMANCE_VIEWPORTS['desktop'], color_scheme='light', service_workers='block')
    context.set_default_timeout(args.timeout_ms)
    page = context.new_page()
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append(request.url))
    result = report['focused_ux'] = {'search': [], 'checks': []}
    try:
        page.goto(url + '?purpose=&task_id=#gallery', wait_until='networkidle')
        all_filters(page)
        # Store real option node references, not text/value equivalents.
        page.evaluate("window.__optionNodes=[...document.querySelectorAll('#filters select')].map(s=>({select:s,options:[...s.options]}))")
        queries = ['', 'gpt', 'FAILED', 'crocodile', ' max ', '入口', data['runs'][0]['id'], 'no-such-run-7f918c']
        reviewed = next((run for run in data['runs'] if run.get('reviews')), None)
        if reviewed and reviewed['reviews'][0].get('reviewer'):
            queries.append(reviewed['reviews'][0]['reviewer'])
        for query in queries:
            # Frozen viewer's original JSON.stringify({...run,reviews:undefined})
            # semantics are evaluated against audited data, not app internals.
            expected = page.evaluate("""q=>{q=q.trim().toLocaleLowerCase();return window.BENCH_SNAPSHOT.runs.filter(r=>
                !q||JSON.stringify({...r,reviews:undefined}).toLocaleLowerCase().includes(q)).map(r=>r.id).sort()}""", query)
            result['active_step'] = 'search ' + repr(query)
            changed = page.locator('[name="q"]').input_value() != query
            page.evaluate("window.__previousGallery=document.querySelector('#view-content').firstElementChild")
            page.locator('[name="q"]').fill(query)
            # URL changes before debounce. Filling an unchanged value emits no
            # input event, so only changed values require a replacement subtree.
            page.wait_for_function("({q,changed})=>(new URL(location.href).searchParams.get('q')||'')===q&&(!changed||!window.__previousGallery.isConnected)&&document.querySelector('#workspace').getAttribute('aria-busy')==='false'", arg={'q': query, 'changed': changed})
            history = page.locator('#no-media-history')
            if history.count() and not history.evaluate('d=>d.open'):
                history.locator('summary').click()
            page.wait_for_function("ids=>JSON.stringify([...document.querySelectorAll('.evidence-card[data-run-id]')].map(c=>c.dataset.runId).sort())===JSON.stringify(ids)", arg=expected)
            actual = page.locator('.evidence-card[data-run-id]').evaluate_all('cs=>cs.map(c=>c.dataset.runId).sort()')
            require(actual == expected, 'Search ID set changed for ' + repr(query))
            require(page.evaluate("window.__optionNodes.every(({select,options})=>select.isConnected&&options.length===select.options.length&&options.every((o,i)=>o===select.options[i]))"), 'Unchanged dropdown options were recreated during search')
            result['search'].append({'query': query, 'expected_ids': expected, 'actual_ids': actual})
        result['checks'].append('unchanged dropdown node identity across search')
        page.evaluate("window.__previousGallery=document.querySelector('#view-content').firstElementChild")
        page.locator('[name="q"]').fill('')
        page.wait_for_function("!new URL(location.href).searchParams.get('q')&&!window.__previousGallery.isConnected&&document.querySelector('#workspace').getAttribute('aria-busy')==='false'")
        no_media = [run['id'] for run in data['runs'] if not evidence_paths(run)]
        history = page.locator('#no-media-history')
        require(no_media and history.count(), 'Actual snapshot lacks no-media history fixture')
        require(not history.evaluate('d=>d.open'), 'Mixed gallery no-media history should start collapsed')
        require(history.locator('.evidence-card').count() == 0, 'Collapsed no-media history was eagerly built')
        history.locator('summary').click()
        expect(history.locator('.evidence-card')).to_have_count(len(no_media))
        require(set(history.locator('.evidence-card').evaluate_all('cs=>cs.map(c=>c.dataset.runId)')) == set(no_media), 'First history expansion lost attempts')
        history.locator('summary').click()
        history.locator('summary').click()
        expect(history.locator('.evidence-card')).to_have_count(len(no_media))
        result['checks'].append('no-media history lazy first expansion, no duplicate cards on reopen')
        page.locator('[name="q"]').fill(no_media[0])
        page.wait_for_function("id=>new URL(location.href).searchParams.get('q')===id", arg=no_media[0])
        expect(page.locator('#no-media-history')).to_have_attribute('open', '')
        expect(page.locator('#no-media-history .evidence-card')).to_have_count(1)
        result['checks'].append('all-no-media results default open')
        # A fresh page avoids earlier full-image requests contaminating the
        # assertion that phone B remains unrequested before an explicit switch.
        page.goto(url + '?purpose=benchmark&task_id=crocodile#gallery', wait_until='networkidle')
        page.set_viewport_size(PERFORMANCE_VIEWPORTS['phone'])
        cases = [next(run for run in data['runs'] if run['id'] == run_id) for run_id in (args.case_id or NEW_CASES)]
        require(len(cases) == 2 and cases[0]['task_id'] == cases[1]['task_id'], 'Focused UX needs two real same-task comparison cases')
        result['active_step'] = 'phone comparison selection'
        page.locator(f'#task-tabs [data-task-id="{cases[0]["task_id"]}"]').click()
        page.locator(f'.evidence-card [data-select-run="{cases[0]["id"]}"]').click()
        page.locator('#mixed-comparison').check()
        for run in cases[1:]:
            page.locator(f'.evidence-card [data-select-run="{run["id"]}"]').click()
        expect(page.locator('#selection-label')).to_contain_text('2')
        requests.clear()
        page.locator('#compare-button').click()
        expect(page.locator('#compare-dialog')).to_be_visible()
        page.wait_for_load_state('networkidle')
        b_path = next((path for path in evidence_paths(cases[1]) if path.split('/')[-1] == 'desktop.png'), None)
        b_key = cases[1]['id'] + '/' + str(b_path)
        require(b_key in data['evidence'], 'B lacks actual desktop evidence')
        b_url = urljoin(url, data['evidence'][b_key])
        require(b_url not in requests, 'Hidden phone B full-size image GET before switch')
        require(page.locator('#compare-content .compare-column:visible').count() == 1, 'Phone comparison must show only A')
        page.locator('#compare-ab [data-compare-index="1"]').click()
        page.wait_for_function("[...document.querySelectorAll('#compare-content .compare-column:not([hidden]) img')].some(i=>i.complete&&i.naturalWidth>0)")
        require(b_url in requests, 'Switch to B did not request its full-size image')
        result['checks'].append('phone hidden B full-size GET deferred until explicit switch')
        for stage in ('later', 'final', 'mobile', 'desktop'):
            page.locator('#compare-stage').select_option(stage)
            page.wait_for_function("[...document.querySelectorAll('#compare-content .compare-column:not([hidden]) img')].some(i=>i.complete&&i.naturalWidth>0)")
        page.locator('#compare-stage').select_option('video')
        require(page.locator('#compare-content video[src]').count() == 0, 'Video stage auto-loaded video')
        page.locator('#play-videos').click()
        page.wait_for_function("[...document.querySelectorAll('#compare-content .compare-column:not([hidden]) video')].some(v=>v.readyState>=2&&v.videoWidth>0&&v.currentTime>0&&!v.paused&&!v.error)")
        page.evaluate("window.__cleanupVideos=[...document.querySelectorAll('#compare-content video')]")
        page.locator('#compare-mode').select_option('original')
        require(page.evaluate("window.__cleanupVideos.length>0&&window.__cleanupVideos.every(v=>v.paused&&!v.hasAttribute('src')&&!v.querySelector('source[src]'))"), 'Mode switch failed to release played comparison video')
        require(not any('/originals/' in request for request in requests), 'Original mode fetched package without run action')
        page.locator('#compare-mode').select_option('media')
        page.locator('#compare-stage').select_option('desktop')
        for size in ('desktop', 'phone'):
            page.set_viewport_size(PERFORMANCE_VIEWPORTS[size])
            expect(page.locator('#compare-content .compare-column:visible')).to_have_count(2 if size == 'desktop' else 1)
            for theme in ('light', 'dark'):
                # Background theme button is inert while the modal is open;
                # gallery checks below exercise the actual toggle separately.
                page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
                settle_theme(page)
                overflow(page, 'focused-compare-' + size + '-' + theme, report)
        page.locator('[data-close="compare-dialog"]').click()
        require(page.locator('#compare-content video[src],#compare-content video source[src]').count() == 0, 'Closed comparison retained video sources')
        require(not page.locator('iframe').count(), 'Closed comparison retained original frames')
        expect(page.locator('#selection-label')).to_contain_text('2')
        page.locator('#clear-selection').click()
        expect(page.locator('#compare-button')).to_be_disabled()
        result['checks'].append('selection, stage, mode, resize and dialog media cleanup')
        for size in ('desktop', 'phone'):
            page.set_viewport_size(PERFORMANCE_VIEWPORTS[size])
            for theme in ('light', 'dark'):
                set_theme(page, theme, expect)
                overflow(page, 'focused-gallery-' + size + '-' + theme, report)
        new_gallery_checks(page, data, args, report, expect)
        explicit_filter_compatibility(browser, url, data, args, report)
        page.goto(url + '?purpose=benchmark&task_id=unknown-task#gallery', wait_until='networkidle')
        expect(page.locator('[name="task_id"]')).to_have_value('unknown-task')
        expect(page.locator('.evidence-card')).to_have_count(0)
        page.locator('#reset-filters').click()
        expect(page.locator('[name="purpose"]')).to_have_value('benchmark')
        require(page.locator('.evidence-card').count() > 0, 'Reset did not restore default task results')
        result['checks'].append('full gallery/date coverage, explicit filter URLs, unknown URL value and reset')
        require(not errors, 'Focused UX JavaScript errors: ' + '; '.join(errors))
        result['checks'].append('desktop/phone light/dark overflow')
    finally:
        context.close()


def focused_verify(args, report):
    from playwright.sync_api import sync_playwright
    sources = focused_sources(args, report)
    report['scope'] = 'focused_current_v2_viewer_ux' if args.ux_only else 'performance_only_real_gzip_http_not_original_rendering'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            if args.ux_only:
                source = sources['current']
                with serve_report(source['site'] / 'index.html', source['data'], gzip_html=True) as url:
                    focused_ux(browser, url, source['data'], args, report)
                return
            report['performance'] = {'profiles': PERFORMANCE_PROFILES, 'viewports': PERFORMANCE_VIEWPORTS,
                'repeats': args.timing_repeats, 'transport': 'HTTP gzip level 6, cache disabled, fresh context per sample',
                'ttfb_definition': 'Chromium navigation responseStart (local HTTP headers); server_ttfb_ms subtracts requestStart. CDP data delivery throttling may not delay this header timestamp; do not interpret it as remote RTT.',
                'interactive_definition': 'real gallery button click followed by two animation frames; observed responsiveness, not formal TTI',
                'longtask_definition': 'navigation through cold network idle, including the real interactive readiness probe; warm filter tasks separate',
                'filter_definition': 'input event to exact filtered ID set plus two animation frames; 250ms debounce reported separately',
                'versions': {}}
            # Alternating old/new samples reduce drift; both sources use the same
            # runtime, server transport, browser, viewport and network profile.
            from contextlib import ExitStack
            with ExitStack() as stack:
                urls = {name: stack.enter_context(serve_report(source['site'] / 'index.html', source['data'], gzip_html=True))
                        for name, source in sources.items()}
                for name in sources:
                    report['performance']['versions'][name] = {'samples': [], 'summaries': {}}
                for profile in PERFORMANCE_PROFILES:
                    for viewport in PERFORMANCE_VIEWPORTS:
                        for repeat in range(args.timing_repeats):
                            names = list(reversed(sources)) if repeat % 2 == 0 else list(sources)
                            for name in names:
                                sample = performance_sample(browser, urls[name], sources[name]['data'], args, name, profile, viewport, repeat + 1)
                                report['performance']['versions'][name]['samples'].append(sample)
                        for version in report['performance']['versions'].values():
                            selected = [sample for sample in version['samples'] if sample['profile'] == profile and sample['viewport'] == viewport]
                            version['summaries'][profile + '/' + viewport] = performance_summary(selected)
        finally:
            browser.close()


def verify(args, report):
    if args.performance_only or args.ux_only:
        focused_verify(args, report)
        return
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
        data = audit_html(html_bytes.decode('utf-8'), trusted, args.preview_runtime.read_text(encoding='utf-8') if args.preview_runtime.is_file() else None)
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
                    sample = cold_load(browser, url, data, args, report, 'current', data.get('format') in {'static-media-v1', 'static-media-v2'} and data.get('transport', 'external') == 'external')
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
                if data.get('format') in {'static-media-v1', 'static-media-v2'} and data.get('transport', 'external') == 'external':
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
