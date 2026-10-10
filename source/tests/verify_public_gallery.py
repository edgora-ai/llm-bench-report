#!/usr/bin/env python3
"""Bounded progressive-static-v3 audit and real gzip HTTP browser verification.

No network interception, synthetic API, rebuild, or model execution. --help needs
only the standard library. Performance runs are paired, alternated, three cold
contexts per profile/viewport/version. Utility fixtures are not performance proof.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, ExitStack
import gzip
import hashlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
from statistics import median
import sys
import threading
import time
from urllib.parse import unquote, urlsplit, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_public_snapshot as v2

require = v2.require
PROFILES = v2.PERFORMANCE_PROFILES
VIEWPORTS = v2.PERFORMANCE_VIEWPORTS
FULL_VIEWS = ('gallery', 'matrix', 'batch', 'evidence', 'trend', 'blind')
MANIFEST = '.report-manifest.json'
SHA = re.compile(r'[a-f0-9]{64}')
FROZEN_INDEX_SHA = 'caeddfe28402d9718f272b8605d7ba55d2be802ca882518577c49e472f51d6bb'
FROZEN_MANIFEST_SHA = '66f59b7b9b7dc69fddc74a72dbad9bc4066de185113fc4d7cd7322977da1f329'
FROZEN_PROGRAMS = {
    'web/app.js': 'be40765eda0931545ce995a4e58a7ccab77c7cfd1740746cb4e293684ad23751',
    'web/preview-runtime.js': '50f44020c5464433c28c9a27a944b83588002c435e7ce33508866afb249b67cf',
    'tests/verify_public_snapshot.py': '7c1a9b0c7ff2357c94e889cc4a84d73431b6544c27854abe650bf4551b457a80',
    'tests/verify_original_previews.py': 'e86710c7c2d9ee1e253dbb14ee0a947ba158fbe79551af11e376a64375e5e644',
}


def same(left, right):
    # Python's True == 1 is not valid schema equality.
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return canonical(left) == canonical(right)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--site', required=True, type=Path, help='Final, existing v3 bundle; never rebuilt here')
    p.add_argument('--baseline-site', required=True, type=Path, help='Frozen original v2 bundle')
    p.add_argument('--trust-root', required=True, type=Path, help='Frozen independent baseline checkout, not candidate site')
    p.add_argument('--baseline-lock', type=Path, default=Path('/tmp/bench-gallery-baseline.json'))
    p.add_argument('--new-v2-baseline-lock', type=Path, help='Explicit independently supplied JSON {index_sha256, manifest_sha256}; overrides only v2 data pins, never frozen programs or budgets')
    p.add_argument('--gallery-bootstrap', type=Path, default=ROOT / 'web/public-gallery.js')
    p.add_argument('--gallery-css', type=Path, default=ROOT / 'web/public-gallery.css')
    p.add_argument('--output', required=True, type=Path, help='Dedicated writable receipt/screenshots directory outside source and bundles')
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--performance-only', action='store_true')
    modes.add_argument('--ux-only', action='store_true')
    modes.add_argument('--original-smoke', action='store_true', help='One Canvas lifecycle and existing two-original helper; Chromium only')
    modes.add_argument('--audit-only', action='store_true', help='No browser; strict byte/data/inventory audits')
    p.add_argument('--timing-repeats', type=int, default=3, choices=[3])
    p.add_argument('--timeout-ms', type=int, default=30000)
    p.add_argument('--expected-runs', type=int, default=58)
    p.add_argument('--expected-benchmark', type=int, default=35)
    p.add_argument('--expected-reviews', type=int, default=14)
    p.add_argument('--expected-latest-runs', type=int, default=0, help='Latest-per-triple card census; 0 means derive from candidate seed')
    a = p.parse_args(argv)
    require(a.timeout_ms > 0, 'Timeout must be positive')
    require(a.expected_runs > 0 and a.expected_benchmark >= 0 and a.expected_reviews >= 0 and a.expected_latest_runs >= 0, 'Invalid census')
    for name in ('site', 'baseline_site', 'trust_root'):
        path = getattr(a, name).resolve(strict=True)
        require(path.is_dir(), name + ' must be a directory')
        setattr(a, name, path)
    require(a.site != a.baseline_site, 'Candidate and frozen baseline must differ')
    require(not a.trust_root.is_relative_to(a.site), 'Trust cannot come from candidate')
    a.output = a.output.resolve()
    require(a.output != Path('/') and all(not a.output.is_relative_to(path) for path in
            (ROOT.resolve(), a.site, a.baseline_site, a.trust_root)), 'Output must be outside source and inputs')
    return a


def safe_read(root, relative):
    require(isinstance(relative, str) and relative and not relative.startswith('/'), 'Unsafe relative asset path')
    parts = relative.split('/')
    require(all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) and part not in ('.', '..') for part in parts), 'Unsafe asset path')
    path = root
    for part in parts:
        path = path / part
        require(not path.is_symlink(), 'Symlink asset')
    require(path.resolve().is_relative_to(root.resolve()) and path.is_file(), 'Missing/escaping asset')
    require(path.stat().st_size <= 64 * 1024 * 1024, 'Asset read bound exceeded')
    return path.read_bytes()


def inventory(root, files):
    actual, directories = set(), set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'Bundle contains symlink')
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.add(relative)
        else:
            require(path.is_file(), 'Nonregular bundle file')
            actual.add(relative)
    expected_dirs = {str(parent) for name in files for parent in Path(name).parents if str(parent) != '.'}
    require(actual == set(files) | {MANIFEST} and directories == expected_dirs, 'Exact bundle inventory differs')


def checked_manifest(root, version):
    m = json.loads(safe_read(root, MANIFEST))
    require(isinstance(m, dict) and set(m) == {'version', 'format', 'files', 'stats'}, 'Manifest schema')
    require(type(m['version']) is int and m['version'] == version and m['format'] ==
            {2: 'static-media-v2', 3: 'progressive-static-v3'}[version], 'Manifest version/profile')
    require(isinstance(m['files'], dict) and isinstance(m['stats'], dict), 'Manifest files/stats')
    inventory(root, m['files'])
    for name, meta in m['files'].items():
        require(isinstance(meta, dict) and SHA.fullmatch(str(meta.get('sha256', ''))) and
                type(meta.get('size')) is int and meta['size'] > 0, 'Asset digest/size schema')
        raw = safe_read(root, name)
        require(len(raw) == meta['size'] and digest(raw) == meta['sha256'], 'Asset bytes mismatch: ' + name)
        if name in ('index.html', 'offline.html'):
            require(meta.get('mime') == 'text/html' and meta.get('role') == ('index' if name == 'index.html' else 'offline'), 'Document role/MIME')
        elif re.fullmatch(r'media/[a-f0-9]{64}\.(jpg|png|webp|gif|webm|mp4)', name):
            ext = name.rsplit('.', 1)[1]
            require(meta['sha256'] == name.split('/')[1].split('.')[0] and meta.get('mime') ==
                    {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif', 'webm': 'video/webm', 'mp4': 'video/mp4'}[ext]
                    and meta.get('role') in ('evidence', 'thumbnail', 'evidence+thumbnail'), 'Media role/MIME/path')
        elif re.fullmatch(r'originals/[a-f0-9]{64}\.json', name):
            require(meta['sha256'] == Path(name).stem and meta.get('mime') == 'application/json' and meta.get('role') == 'original', 'Original role/MIME/path')
        elif version == 3 and re.fullmatch(r'viewer/[a-f0-9]{64}\.(html|js)', name):
            require(meta['sha256'] == Path(name).stem and meta.get('mime') ==
                    ('text/html' if name.endswith('.html') else 'text/javascript') and meta.get('role') ==
                    ('viewer' if name.endswith('.html') else 'runtime'), 'Viewer role/MIME/path')
        else:
            require(False, 'Undeclared namespace/role: ' + name)
    return m


class GalleryHTML(HTMLParser):
    """Pre-browser executable trust and initial server card census."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.styles, self.cards, self.images, self.buttons = [], [], [], [], []
        self.current = None
        self.unsafe = []
        self.card_details = {}
        self.card = None
        self.field = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('iframe', 'object', 'embed', 'base') or any(k.lower().startswith('on') for k in attrs):
            self.unsafe.append(tag)
        if any(str(attrs.get(k, '')).strip().lower().startswith('javascript:') for k in ('href', 'src', 'action')):
            self.unsafe.append('javascript URL')
        if tag == 'link' or 'srcdoc' in attrs:
            self.unsafe.append('external resource or srcdoc')
        if tag in ('script', 'style'):
            if attrs.get('src') or (tag == 'script' and attrs.get('type', '') not in ('', 'text/javascript')):
                self.unsafe.append('unexpected script')
            self.current = (tag, [])
        if tag == 'article' and 'evidence-card' in attrs.get('class', '').split():
            self.cards.append(attrs.get('data-run-id'))
            self.card = attrs.get('data-run-id')
            self.card_details[self.card] = {}
        if self.card:
            for field in ('work-name', 'work-meta', 'work-status'):
                if field in attrs.get('class', '').split():
                    self.field = (tag, field)
                    self.card_details[self.card][field] = []
        if tag == 'img':
            self.images.append(attrs)
            if attrs.get('src') or attrs.get('srcset'):
                self.unsafe.append('eager initial image')
        if tag in ('video', 'source') and attrs.get('src'):
            self.unsafe.append('eager video')
        if tag == 'button':
            self.buttons.append(attrs)

    def handle_data(self, text):
        if self.current:
            self.current[1].append(text)
        if self.card and self.field:
            self.card_details[self.card][self.field[1]].append(text)

    def handle_endtag(self, tag):
        if self.field and self.field[0] == tag:
            self.field = None
        if tag == 'article':
            self.card = None
        if self.current and self.current[0] == tag:
            (self.scripts if tag == 'script' else self.styles).append(''.join(self.current[1]))
            self.current = None


def entry_status(run):
    evaluation = run.get('evaluation') or {}
    if evaluation.get('status') and evaluation['status'] != 'completed':
        return 'unknown'
    checks = run.get('checks') or []
    check = next((item for item in checks if item['name'] == 'entrypoint'), None)
    check = check or next((item for item in evaluation.get('checks', []) if item['name'] == 'entrypoint'), {})
    return 'pass' if check.get('status') in ('pass', 'passed', 'ok') else 'fail' if check.get('status') in ('fail', 'failed') else 'unknown'


def project_runs(data):
    rows = []
    for r in data['runs']:
        declared = [(item.get('path') if isinstance(item, dict) else item)
                    for item in (r.get('evaluation') or {}).get('evidence', [])]
        keys = [r['id'] + '/' + path for path in declared]
        key = next((key for key in keys if key.rsplit('/', 1)[-1] == 'desktop.png'), None)
        full, thumb = data['evidence'].get(key), data.get('thumbnails', {}).get(key)
        image = None
        if full and thumb:
            asset = data['assets'][full]
            image = {'thumb': thumb, 'full': full, 'width': asset['width'], 'height': asset['height'], 'label': 'desktop.png'}
        rows.append({**{k: r.get(k) for k in ('id', 'task_id', 'prompt_version', 'started_at')},
                     **{k: r.get(k) or 'unknown' for k in ('model', 'tool', 'purpose')},
                     'date': str(r.get('date') or r.get('started_at') or '')[:10] or None,
                     'status': r.get('generation_status') or r.get('status') or 'unknown', 'entry_status': entry_status(r),
                     'evaluation_status': (r.get('evaluation') or {}).get('status') or 'unknown',
                     'registered_media': any(key in data['evidence'] for key in keys),
                     'image': image, 'original': data['originals'][r['id']], 'history_count': 0})
    ordered = sorted(rows, key=lambda r: (r['tool'], r['model'], r['task_id'] or '', r['started_at'] or r['date'] or '', r['id']))
    latest = {}
    for index, row in enumerate(ordered):
        latest[(row['tool'], row['model'], row['task_id'] or '')] = index
    for key, index in latest.items():
        ordered[index]['history_count'] = sum(1 for row in ordered if (row['tool'], row['model'], row['task_id'] or '') == key) - 1
    selected = [ordered[index] for index in sorted(latest.values())]
    return sorted(selected, key=lambda r: (r['tool'], r['model'], r['started_at'] or r['date'] or '', r['id']))


def full_history_ids(data):
    """Exact full-run IDs that are NOT the latest attempt for their (tool, model, task) triple."""
    ordered = sorted(data['runs'], key=lambda r: (r.get('tool') or 'unknown', r.get('model') or 'unknown',
                                                  r.get('task_id') or '', r.get('started_at') or r.get('date') or '', r['id']))
    latest_ids = {}
    for r in ordered:
        latest_ids[(r.get('tool') or 'unknown', r.get('model') or 'unknown', r.get('task_id') or '')] = r['id']
    return [r['id'] for r in ordered if latest_ids[(r.get('tool') or 'unknown', r.get('model') or 'unknown', r.get('task_id') or '')] != r['id']]


def audit_gallery(html, data, bootstrap, css, full_info, runtime_info, trusted_program):
    parsed = GalleryHTML()
    parsed.feed(html)
    parsed.close()
    require(not parsed.unsafe and len(parsed.scripts) == 2 and len(parsed.styles) == 1, 'Unsafe gallery scaffold')
    match = re.fullmatch(r'\s*window\.BENCH_GALLERY=(\{.*\});\s*', parsed.scripts[0], re.S)
    require(match is not None and '<' not in parsed.scripts[0], 'Unescaped gallery seed')
    seed = json.loads(match[1])
    require(parsed.scripts[1].strip() == bootstrap.strip() and parsed.styles[0].strip() == css.strip(), 'Gallery executable/style differs from trusted source')
    require(set(seed) == {'version', 'format', 'tasks', 'runs', 'defaults', 'counts', 'full', 'runtime', 'offline'}, 'Gallery seed schema')
    require(type(seed['version']) is int and seed['version'] == 3 and seed['format'] == 'progressive-static-v3', 'Gallery version/profile')
    require(same(seed['runs'], project_runs(data)), 'Exact latest-per-triple projection/order differs')
    tasks = {t['id']: {'id': t['id'], 'name': t.get('name') or t['id']} for t in data['tasks']}
    for row in data['runs']:
        if row.get('task_id') and row['task_id'] not in tasks:
            tasks[row['task_id']] = {'id': row['task_id'], 'name': row.get('task_name') or row['task_id']}
    ordered_tasks = sorted(tasks.values(), key=lambda t: (t['id'] != 'crocodile', t['id']))
    require(seed['tasks'] == ordered_tasks, 'Task metadata differs')
    default_task = 'crocodile' if any(t['id'] == 'crocodile' for t in data['tasks']) else data['tasks'][0]['id']
    require(same(seed['defaults'], {'task_id': default_task, 'purpose': 'benchmark', 'page_size': 8}), 'Defaults differ')
    require(same(seed['counts'], {'runs': len(seed['runs']), 'full_runs': len(data['runs']),
                              'benchmark': sum(r.get('purpose') == 'benchmark' for r in seed['runs']),
                              'full_benchmark': sum(r.get('purpose') == 'benchmark' for r in data['runs']),
                              'reviews': sum(len(r.get('reviews') or []) for r in data['runs'])}), 'Gallery counts differ')
    require(same(seed['offline'], data['offline']), 'Offline descriptor changed')
    require(same(seed['full'], {**full_info, 'script_sha256': digest(trusted_program)}), 'Full viewer descriptor differs')
    require(same(seed['runtime'], runtime_info), 'Runtime descriptor differs')
    expected = [r for r in seed['runs'] if r['task_id'] == default_task and r['purpose'] == 'benchmark' and r['registered_media']][:8]
    require(parsed.cards == [r['id'] for r in expected], 'Server-rendered initial card IDs differ')
    thumbnails = [i for i in parsed.images if 'work-image' in i.get('class', '').split()]
    require([i.get('data-src') for i in thumbnails] == [r['image']['thumb'] for r in expected if r['image']], 'Initial thumbnail mapping differs')
    require(all(i in thumbnails or (i.get('id') == 'public-zoom-image' and not i.get('data-src')) for i in parsed.images), 'Undeclared initial image')
    for row in expected:
        fields = {key: ''.join(value).strip() for key, value in parsed.card_details[row['id']].items()}
        require(fields.get('work-name') == row['model'] and row['tool'] in fields.get('work-meta', '') and
                str(row['date']) in fields.get('work-meta', ''), 'Initial card model/tool/date presentation differs')
        status = fields.get('work-status', '')
        require(all(value in status for value in (row['status'], row['entry_status'], row['evaluation_status'])), 'Initial card source status presentation differs')
        for attr in ('data-run-original', 'data-select-run', 'data-zoom-run', 'data-detail-run'):
            buttons = [b for b in parsed.buttons if b.get(attr) == row['id']]
            require(len(buttons) == 1, 'Missing/duplicate server card action: ' + attr)
            if attr in ('data-run-original', 'data-zoom-run'):
                disabled = not row['image'] if attr == 'data-zoom-run' else row['original']['status'] not in ('ready', 'missing_dependencies')
                require(('disabled' in buttons[0]) == disabled, 'Server disabled state differs')
    return seed


def baseline_data_pins(args):
    path = getattr(args, 'new_v2_baseline_lock', None)
    if path is None:
        return FROZEN_INDEX_SHA, FROZEN_MANIFEST_SHA
    path = path.resolve(strict=True)
    require(all(not path.is_relative_to(root.resolve()) for root in (args.site, args.baseline_site)),
            'Independent v2 data lock must be outside candidate and baseline bundles')
    require(path.stat().st_size <= 4096, 'Independent v2 data lock too large')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate independent v2 data lock key')
            result[key] = value
        return result
    pins = json.loads(path.read_text(), object_pairs_hook=unique)
    require(isinstance(pins, dict) and set(pins) == {'index_sha256', 'manifest_sha256'} and
            all(isinstance(value, str) and SHA.fullmatch(value) for value in pins.values()),
            'Independent v2 data lock schema')
    return pins['index_sha256'], pins['manifest_sha256']


def load_sources(args, report):
    lock = json.loads(args.baseline_lock.read_text())
    require(all(lock['sha256'].get(path) == sha for path, sha in FROZEN_PROGRAMS.items()), 'Independent frozen program lock differs')
    index_sha, manifest_sha = baseline_data_pins(args)
    require(digest(safe_read(args.baseline_site, MANIFEST)) == manifest_sha and
            digest(safe_read(args.baseline_site, 'index.html')) == index_sha, 'Wrong frozen/explicitly locked baseline bundle')
    report['baseline_data_lock'] = {'mode': 'explicit-new-v2' if getattr(args, 'new_v2_baseline_lock', None) else 'frozen-default',
                                    'index_sha256': index_sha, 'manifest_sha256': manifest_sha}
    for relative, expected in lock['sha256'].items():
        require(digest(safe_read(args.trust_root, relative)) == expected, 'Frozen trust bytes differ: ' + relative)
    require(digest(Path(v2.__file__).read_bytes()) == lock['sha256']['tests/verify_public_snapshot.py'], 'Imported v2 helper differs from frozen trust')
    viewer = safe_read(args.trust_root, 'web/app.js')
    runtime = safe_read(args.trust_root, 'web/preview-runtime.js')
    old_manifest = checked_manifest(args.baseline_site, 2)
    manifest = checked_manifest(args.site, 3)
    old_html = safe_read(args.baseline_site, 'index.html')
    old_data = v2.audit_html(old_html.decode(), viewer.decode(), runtime.decode(), descriptors_only=True)
    full_path = 'viewer/' + digest(old_html) + '.html'
    runtime_path = 'viewer/' + digest(runtime) + '.js'
    require(set(manifest['files']) == set(old_manifest['files']) | {full_path, runtime_path}, 'Only two viewer namespace assets may be appended')
    for name, meta in old_manifest['files'].items():
        if name != 'index.html':
            require(same(manifest['files'][name], meta) and safe_read(args.site, name) == safe_read(args.baseline_site, name), 'Inherited asset changed: ' + name)
    full_html = safe_read(args.site, full_path)
    require(full_html == old_html and safe_read(args.site, runtime_path) == runtime, 'Frozen full document/runtime bytes changed')
    current_data = v2.audit_html(full_html.decode(), viewer.decode(), runtime.decode(), descriptors_only=True)
    require(current_data == old_data, 'Metadata/evidence/originals/offline equality failed')
    offline = v2.audit_html(safe_read(args.site, 'offline.html').decode(), viewer.decode(), runtime.decode())
    # Offline is old inline transport; compare run/task metadata and exact package bytes,
    # not external media URLs to embedded data URLs.
    require(offline['runs'] == old_data['runs'] and offline['tasks'] == old_data['tasks'], 'Offline metadata differs')
    require(len(old_data['runs']) == args.expected_runs and sum(r.get('purpose') == 'benchmark' for r in old_data['runs']) == args.expected_benchmark
            and sum(len(r.get('reviews') or []) for r in old_data['runs']) == args.expected_reviews, 'Frozen census differs')
    html = safe_read(args.site, 'index.html')
    require(same(manifest['stats'], {**old_manifest['stats'], 'index_bytes': len(html)}), 'Inherited manifest statistics changed')
    full_info = {'path': full_path, 'sha256': digest(full_html), 'size': len(full_html)}
    runtime_info = {'path': runtime_path, 'sha256': digest(runtime), 'size': len(runtime)}
    seed = audit_gallery(html.decode(), old_data, args.gallery_bootstrap.read_text(), args.gallery_css.read_text(),
                         full_info, runtime_info, runtime + b'\n' + viewer)
    require(len(html) <= 102400 and len(gzip.compress(html, compresslevel=6, mtime=0)) <= 25600, 'Initial HTML decoded/gzip budget exceeded')
    report['audit'] = {'baseline_html_sha256': digest(old_html), 'full_html_sha256': digest(full_html), 'runtime_sha256': digest(runtime),
                       'current_html_sha256': digest(html), 'decoded_html_bytes': len(html), 'gzip_html_bytes': len(gzip.compress(html, compresslevel=6, mtime=0)),
                       'exact_data_equality_including_offline': True, 'run_ids': [r['id'] for r in seed['runs']], 'counts': seed['counts'],
                       'candidate_files': len(manifest['files']), 'baseline_files': len(old_manifest['files'])}
    return {'seed': seed, 'data': old_data, 'manifest': manifest, 'baseline_manifest': old_manifest}


@contextmanager
def serve_gallery(root, manifest, *, delays=None, fail_once=None):
    """Real bounded static HTTP. Delays/one-shot 503s are server-side lifecycle probes.

    Entry and full viewer gzip bytes are prepared outside the timed request.
    Every request is rehashed; no route, abort, or browser API substitution.
    """
    delays, failures = dict(delays or {}), set(fail_once or ())
    payloads = {name: safe_read(root, name) for name in manifest['files']}
    encoded = {name: gzip.compress(raw, compresslevel=6, mtime=0) for name, raw in payloads.items()
               if manifest['files'][name]['mime'] == 'text/html' and name != 'offline.html'}
    requests, lock = [], threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = unquote(urlsplit(self.path).path).lstrip('/') or 'index.html'
            with lock:
                requests.append({'path': name, 'method': 'GET', 'started_monotonic': time.monotonic()})
                fail = name in failures
                failures.discard(name)
            item = manifest['files'].get(name)
            if not item:
                self.send_error(404)
                return
            if name in delays:
                time.sleep(delays[name])
            if fail:
                self.send_error(503, 'Explicit one-shot verification fault')
                return
            try:
                raw = safe_read(root, name)
            except (AssertionError, OSError):
                self.send_error(409, 'Audited file unavailable')
                return
            if raw != payloads[name] or len(raw) != item['size'] or digest(raw) != item['sha256']:
                self.send_error(409, 'Audited bytes changed')
                return
            body = encoded.get(name) if 'gzip' in self.headers.get('Accept-Encoding', '').lower() else None
            zipped = body is not None
            body = body if zipped else raw
            self.send_response(200)
            self.send_header('Content-Type', item['mime'])
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Report-SHA256', digest(raw))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if name in encoded:
                self.send_header('Vary', 'Accept-Encoding')
            if zipped:
                self.send_header('Content-Encoding', 'gzip')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
        def log_message(self, *args):
            return None
    bind = os.environ.get('BENCH_VERIFY_BIND', '127.0.0.1')
    require(ipaddress.ip_address(bind).is_loopback, 'Verifier server must bind loopback, not a public interface')
    server = ThreadingHTTPServer((bind, 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://{server.server_address[0]}:{server.server_port}/', requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def validate_requests(requests, url, manifest, seed=None, *, cold=False, viewport=None, allow_preview=False):
    names = []
    base = urlsplit(url)
    prefix = unquote(base.path)
    require(prefix.startswith('/') and prefix.endswith('/'), 'Report base must be a directory URL')
    for row in requests:
        target = urlsplit(row['url'])
        if allow_preview and target.scheme == 'blob':
            require(row['method'] == 'GET' and (row['url'].startswith('blob:null/') or
                    row['url'].startswith('blob:' + url.rstrip('/') + '/')), 'Unexpected preview Blob origin')
            continue
        require(row['method'] == 'GET' and target.netloc == base.netloc and target.scheme == base.scheme, 'Nonlocal/write request')
        path = unquote(target.path)
        require(path.startswith(prefix), 'Request outside report directory')
        name = path[len(prefix):] or 'index.html'
        require(name in manifest['files'] and '/api/' not in target.path, 'Undeclared/API request: ' + name)
        names.append(name)
    if cold:
        thumbs = {r['image']['thumb'] for r in seed['runs'] if r['image']}
        require(all(name == 'index.html' or name in thumbs for name in names), 'Cold viewer/runtime/original/video/full-image request')
        fetched = [name for name in names if name in thumbs]
        require(1 <= len(fetched) <= (2 if viewport == 'desktop' else 1), 'Actual initial thumbnail request budget differs')
    return names


# Reuse the old longtask observer, but replace its image timestamp with an explicit
# decoded, visible, content-addressed thumbnail. Never count SVG/data placeholders.
GALLERY_OBSERVER = v2.PERFORMANCE_OBSERVER + """
(() => { window.__galleryMetrics={readable:null,image:null,taskResponse:null};
 const visible=e=>{if(!e||!e.getClientRects().length)return false;let r=e.getBoundingClientRect();
   for(let n=e;n;n=n.parentElement){let s=getComputedStyle(n);if(s.visibility==='hidden'||s.display==='none'||Number(s.opacity)===0)return false;}
   return r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0;};
 const tick=()=>{let m=window.__galleryMetrics;
   if(m.readable===null&&document.documentElement.dataset.galleryReady==='true'&&
      visible(document.querySelector('#public-task-tabs button[data-task-id]'))&&
      visible(document.querySelector('#public-purpose'))&&!document.querySelector('#public-purpose').disabled&&
      visible(document.querySelector('#public-model'))&&!document.querySelector('#public-model').disabled&&
      visible(document.querySelector('#public-grid .evidence-card[data-run-id]')))m.readable=performance.now();
   if(m.image===null)for(const i of document.querySelectorAll('#public-grid .work-image')){
     let row=window.BENCH_GALLERY?.runs.find(r=>r.id===i.closest('[data-run-id]')?.dataset.runId);
     if(!row?.image||!i.complete||i.naturalWidth===0||!visible(i)||i.__decodePending)continue;
     if(i.currentSrc!==new URL(row.image.thumb,location.href).href)continue;
     i.__decodePending=true;i.decode().then(()=>requestAnimationFrame(()=>{
       if(m.image===null&&visible(i)&&i.complete&&i.naturalWidth>0)m.image={ms:performance.now(),src:i.currentSrc,run_id:row.id};
     })).catch(e=>{i.__decodeError=String(e);});
   }requestAnimationFrame(tick);};requestAnimationFrame(tick);
})();
"""


def two_frames(page):
    return page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(performance.now()))))')


def gallery_sample(browser, url, sources, args, profile_name, viewport, repeat):
    profile = PROFILES[profile_name]
    context = browser.new_context(viewport=VIEWPORTS[viewport], color_scheme='light', service_workers='block')
    context.set_default_timeout(args.timeout_ms)
    requests, errors = [], []
    try:
        context.add_init_script(GALLERY_OBSERVER)
        page = context.new_page()
        page.on('request', lambda r: requests.append({'url': r.url, 'method': r.method, 'type': r.resource_type}))
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        session = context.new_cdp_session(page)
        session.send('Network.enable')
        session.send('Network.setCacheDisabled', {'cacheDisabled': True})
        session.send('Network.emulateNetworkConditions', profile['network'])
        session.send('Emulation.setCPUThrottlingRate', {'rate': profile['cpu']})
        response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
        require(response.status == 200 and response.headers.get('content-encoding') == 'gzip', 'Not actual HTTP200 gzip')
        require(response.headers.get('x-report-sha256') == digest(safe_read(args.site, 'index.html')), 'Measured entry differs')
        page.wait_for_function('window.__galleryMetrics.readable!==null&&window.__galleryMetrics.image!==null')
        page.wait_for_load_state('networkidle', timeout=60000)
        cold = list(requests)
        names = validate_requests(cold, url, sources['manifest'], sources['seed'], cold=True, viewport=viewport)
        metrics = page.evaluate('''()=>{let n=performance.getEntriesByType('navigation')[0],m=window.__galleryMetrics;
          return {ttfb_ms:n.responseStart,server_ttfb_ms:n.responseStart-n.requestStart,readable_ms:m.readable,
            first_actually_visible_image_ms:m.image.ms,first_visible_image:m.image,dom_nodes:document.querySelectorAll('*').length,
            longtasks:window.__loadMetrics.longtasks,document_encoded_bytes:n.encodedBodySize,document_decoded_bytes:n.decodedBodySize,
            transfer_bytes:n.transferSize+performance.getEntriesByType('resource').reduce((s,r)=>s+r.transferSize,0),
            first_controls:['#public-task-tabs button[data-task-id]','#public-purpose','#public-model','#public-grid .evidence-card[data-run-id]'].map(s=>{
              let e=document.querySelector(s),r=e.getBoundingClientRect();return {selector:s,label:e.textContent.slice(0,100),top:r.top,bottom:r.bottom,disabled:!!e.disabled};}),
            scroll_x:scrollX,scroll_y:scrollY};}''')
        require(metrics['document_encoded_bytes'] <= 25600 and metrics['document_decoded_bytes'] <= 102400,
                'Measured cold wire/decoded document budget')
        require(metrics['scroll_x'] == 0 and metrics['scroll_y'] == 0, 'First image was not in unscrolled initial viewport')
        # A real alternative task click must produce that task's actual card set,
        # not only acknowledge a click or render two arbitrary animation frames.
        target = next(t['id'] for t in sources['seed']['tasks'] if t['id'] != sources['seed']['defaults']['task_id'] and
                      any(r['task_id'] == t['id'] and r['purpose'] == 'benchmark' and r['registered_media'] for r in sources['seed']['runs']))
        expected = [r['id'] for r in sources['seed']['runs'] if r['task_id'] == target and r['purpose'] == 'benchmark' and r['registered_media']][:8]
        page.locator(f'#public-task-tabs button[data-task-id="{target}"]').evaluate('''e=>e.addEventListener('click',()=>{
            window.__taskClickAt=performance.now();},{capture:true,once:true})''')
        page.locator(f'#public-task-tabs button[data-task-id="{target}"]').click()
        page.wait_for_function('''ids=>JSON.stringify([...document.querySelectorAll('#public-grid .evidence-card[data-run-id]')].map(e=>e.dataset.runId))===JSON.stringify(ids)''', arg=expected)
        interactive = two_frames(page)
        task_ms = page.evaluate('performance.now()-window.__taskClickAt')
        control = page.locator(f'#public-task-tabs button[data-task-id="{target}"]')
        require(control.get_attribute('aria-selected') == 'true' or control.get_attribute('aria-pressed') == 'true', 'Task control state did not change')
        validate_requests(requests, url, sources['manifest'])
        require(not any('/viewer/' in r['url'] or '/originals/' in r['url'] for r in requests), 'Basic task control loaded full viewer/original')
        require(not errors, 'Gallery errors: ' + '; '.join(errors))
        metrics.update(version='current', profile=profile_name, viewport=viewport, repeat=repeat, interactive_ms=interactive,
                       task_control_response_ms=task_ms, cold_requests=cold, cold_request_paths=names,
                       longtask_count=len(metrics['longtasks']), longtask_total_ms=sum(t['duration'] for t in metrics['longtasks']))
        return metrics
    finally:
        context.close()


BASELINE_OBSERVER = v2.PERFORMANCE_OBSERVER + """
(()=>{window.__decodedBaselineImage=null;
 const tick=()=>{if(window.__decodedBaselineImage===null)for(const i of document.querySelectorAll('.evidence-card img')){
   const visible=()=>{let r=i.getBoundingClientRect();return i.getClientRects().length&&r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0&&getComputedStyle(i).visibility!=='hidden';};
   if(!i.complete||i.naturalWidth===0||!visible()||i.__decodePending)continue;
   let id=i.closest('[data-run-id]')?.dataset.runId;
   let urls=Object.entries(window.BENCH_SNAPSHOT?.thumbnails||{}).filter(([k])=>k.startsWith(id+'/')).map(([,v])=>new URL(v,location.href).href);
   if(!urls.includes(i.currentSrc))continue;
   i.__decodePending=true;i.decode().then(()=>requestAnimationFrame(()=>{if(window.__decodedBaselineImage===null&&visible())window.__decodedBaselineImage={ms:performance.now(),src:i.currentSrc,run_id:id};})).catch(e=>{i.__decodeError=String(e);});
 }requestAnimationFrame(tick);};requestAnimationFrame(tick);})();
"""


def baseline_sample(browser, url, sources, args, profile, viewport, repeat):
    context, page, requests, errors = observed_context(browser, url, args, profile=profile, viewport=viewport)
    try:
        context.add_init_script(BASELINE_OBSERVER)
        response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
        require(response.status == 200 and response.headers.get('content-encoding') == 'gzip' and
                response.headers.get('x-report-sha256') == digest(safe_read(args.baseline_site, 'index.html')), 'Frozen cold document transport differs')
        page.wait_for_function('window.__loadMetrics.readable!==null&&window.__decodedBaselineImage!==null')
        page.wait_for_load_state('networkidle', timeout=60000)
        cold = list(requests)
        validate_requests(cold, url, sources['baseline_manifest'])
        require(not any('/originals/' in r['url'] or r['url'].endswith(('.webm', '.mp4')) for r in cold), 'Frozen cold page fetched original/video')
        metrics = page.evaluate('''()=>{let n=performance.getEntriesByType('navigation')[0],m=window.__loadMetrics;
          return {ttfb_ms:n.responseStart,server_ttfb_ms:n.responseStart-n.requestStart,readable_ms:m.readable,
           first_actually_visible_image_ms:window.__decodedBaselineImage.ms,first_visible_image:window.__decodedBaselineImage,
           dom_nodes:document.querySelectorAll('*').length,longtasks:m.longtasks,
           document_encoded_bytes:n.encodedBodySize,document_decoded_bytes:n.decodedBodySize,
           transfer_bytes:n.transferSize+performance.getEntriesByType('resource').reduce((s,r)=>s+r.transferSize,0),scroll_x:scrollX,scroll_y:scrollY};}''')
        require(metrics['scroll_x'] == 0 and metrics['scroll_y'] == 0, 'Frozen first-image measurement scrolled')
        target = next(t['id'] for t in sources['seed']['tasks'] if t['id'] != sources['seed']['defaults']['task_id'] and
                      any(r['task_id'] == t['id'] and r['purpose'] == 'benchmark' and r['registered_media'] for r in sources['seed']['runs']))
        # Latest-only seed pages the gallery at 8 cards; the frozen full
        # snapshot can show more. Require the latest projected IDs to appear
        # without requiring an exact full-history census.
        expected = [r['id'] for r in sources['seed']['runs'] if r['task_id'] == target and r['purpose'] == 'benchmark' and r['registered_media']][:8]
        control = page.locator(f'#task-tabs button[data-task-id="{target}"]')
        click_clock(control, page)
        page.wait_for_function('ids=>{let actual=new Set([...document.querySelectorAll(".gallery-main .evidence-card[data-run-id], .model-groups .evidence-card[data-run-id]")].map(e=>e.dataset.runId));return ids.every(id=>actual.has(id));}', arg=expected)
        interactive = two_frames(page)
        task_ms = page.evaluate('performance.now()-window.__explicitClick')
        require(control.get_attribute('aria-pressed') == 'true', 'Frozen task click did not update state')
        require(not errors, 'Frozen viewer errors: ' + '; '.join(errors))
        metrics.update(version='baseline', profile=profile, viewport=viewport, repeat=repeat, interactive_ms=interactive,
                       task_control_response_ms=task_ms, cold_requests=cold, longtask_count=len(metrics['longtasks']),
                       longtask_total_ms=sum(t['duration'] for t in metrics['longtasks']))
        return metrics
    finally:
        context.close()


def summary(samples):
    result = v2.performance_summary(samples)
    for key in ('task_control_response_ms', 'document_encoded_bytes', 'document_decoded_bytes', 'full_viewer_latency_ms', 'primary_original_latency_ms'):
        values = [s[key] for s in samples if s.get(key) is not None]
        result[key] = {'median': round(median(values), 2) if values else None, 'observed': len(values)}
    return result


def paired_performance(browser, urls, sources, args, report):
    # v2 helper needs only these paths plus timeout for its own strict source receipt.
    args.preview_runtime = args.trust_root / 'web/preview-runtime.js'
    args.baseline_viewer_script = args.trust_root / 'web/app.js'
    performance = report['performance'] = {'profiles': PROFILES, 'viewports': VIEWPORTS,
        'definition': 'Decoded real visible thumbnail; actual alternative-task rendering response after unscrolled cold network-idle capture. Interactive timestamp includes this deliberate wait; not earliest readiness or formal TTI.',
        'order': [], 'versions': {name: {'samples': [], 'summaries': {}} for name in ('baseline', 'current')}}
    for profile in PROFILES:
        for viewport in VIEWPORTS:
            for repeat in range(1, 4):
                order = ('baseline', 'current') if repeat % 2 else ('current', 'baseline')
                for name in order:
                    performance['order'].append({'profile': profile, 'viewport': viewport, 'repeat': repeat, 'version': name})
                    sample = (gallery_sample(browser, urls[name], sources, args, profile, viewport, repeat) if name == 'current' else
                              baseline_sample(browser, urls[name], sources, args, profile, viewport, repeat))
                    performance['versions'][name]['samples'].append(sample)
                    require(sample['first_actually_visible_image_ms'] is not None, 'Frozen baseline has no actual first image; do not report fake improvement')
                    sample.update(action_sample(browser, urls['baseline_actions'] if name == 'baseline' else urls[name],
                                                sources, args, name, profile, viewport))
            for version in performance['versions'].values():
                selected = [s for s in version['samples'] if s['profile'] == profile and s['viewport'] == viewport]
                version['summaries'][profile + '/' + viewport] = summary(selected)
    comparisons = performance['comparisons'] = []
    for profile in PROFILES:
        for viewport in VIEWPORTS:
            key = profile + '/' + viewport
            for metric in ('first_actually_visible_image_ms', 'interactive_ms'):
                old = performance['versions']['baseline']['summaries'][key][metric]['median']
                new = performance['versions']['current']['summaries'][key][metric]['median']
                slow = PROFILES[profile]['cpu'] == 4
                limit = old * .75 if slow else old + max(old * .05, 50)
                row = {'profile': profile, 'viewport': viewport, 'metric': metric, 'baseline_median_ms': old,
                       'current_median_ms': new, 'improvement_percent': round((old-new)/old*100, 2), 'limit_ms': limit, 'pass': new <= limit}
                comparisons.append(row)
    require(all(c['pass'] for c in comparisons), 'Measured performance target/regression limit not met')


def observed_context(browser, url, args, *, profile=None, viewport='desktop', original=False):
    # Playwright's service_workers='block' injects an access to Navigator.serviceWorker
    # into opaque sandbox documents, producing a verifier-induced SecurityError.
    # Original lifecycle probes use the browser default; isolation remains the
    # unchanged SDK's real sandbox policy, never an interception-based substitute.
    context = browser.new_context(viewport=VIEWPORTS[viewport], color_scheme='light',
                                  service_workers='allow' if original else 'block', reduced_motion='no-preference')
    context.set_default_timeout(args.timeout_ms)
    page = context.new_page()
    requests, errors = [], []
    page.on('request', lambda r: requests.append({'url': r.url, 'method': r.method, 'type': r.resource_type}))
    page.on('pageerror', lambda error: errors.append(str(error)))
    if profile:
        session = context.new_cdp_session(page)
        session.send('Network.enable')
        session.send('Network.setCacheDisabled', {'cacheDisabled': True})
        session.send('Network.emulateNetworkConditions', PROFILES[profile]['network'])
        session.send('Emulation.setCPUThrottlingRate', {'rate': PROFILES[profile]['cpu']})
    return context, page, requests, errors


def public_ready(page):
    page.wait_for_function('document.documentElement.dataset.galleryReady==="true"')


def full_ready(page):
    page.wait_for_function('document.documentElement.dataset.fullViewerReady==="true"')
    require(page.locator('[data-view="gallery"]').is_enabled(), 'Trusted full viewer not interactive')
    require(page.locator('#workspace').get_attribute('aria-busy') == 'false', 'Full viewer still busy')


def public_ids(page):
    return page.locator('#public-grid .evidence-card[data-run-id]').evaluate_all('es=>es.map(e=>e.dataset.runId)')


def no_full(requests, seed):
    require(not any(urlsplit(r['url']).path.endswith('/' + seed['full']['path']) for r in requests), 'Basic gallery interaction fetched full viewer')


def open_public_run(page, url, row):
    page.goto(url + '?' + urlencode({'task_id': row['task_id'], 'purpose': row['purpose'], 'model_query': row['model']}), wait_until='networkidle')
    public_ready(page)
    page.locator('#public-model').fill(row['model'])
    for _ in range(8):
        if page.locator(f'#public-grid [data-run-id="{row["id"]}"]').count():
            return
        next_page = page.get_by_role('button', name='下一页', exact=True)
        require(next_page.count() == 1 and next_page.is_enabled(), 'Requested concrete run is not reachable in basic gallery')
        next_page.click()
    require(False, 'Requested run exceeded bounded gallery pagination')


def click_clock(locator, page):
    locator.evaluate('e=>e.addEventListener("click",()=>{window.__explicitClick=performance.now();},{capture:true,once:true})')
    locator.click()


def action_sample(browser, url, sources, args, name, profile, viewport):
    """Extra waiting is measured separately; never called fake TTI or cold shell time."""
    context, page, requests, errors = observed_context(browser, url, args, profile=profile, viewport=viewport, original=True)
    try:
        row = next(r for r in sources['seed']['runs'] if r['task_id'] == sources['seed']['defaults']['task_id'] and
                   r['purpose'] == 'benchmark' and r['registered_media'] and r['original']['status'] in ('ready', 'missing_dependencies'))
        page.goto(url, wait_until='networkidle', timeout=60000)
        if name == 'current':
            public_ready(page)
        else:
            page.wait_for_function('document.querySelector("#workspace")?.getAttribute("aria-busy")==="false"')
        button = page.locator(f'.evidence-card [data-run-original="{row["id"]}"]')
        click_clock(button, page)
        page.wait_for_function('id=>document.querySelector(`#original-content .original-preview[data-original-run="${id}"]`)?.dataset.previewState==="loaded"', arg=row['id'])
        original_ms = page.evaluate('performance.now()-window.__explicitClick')
        require(page.locator('#original-content iframe').count() == 1, 'Single original context count')
        before_full = list(requests)
        packages = [urlsplit(r['url']).path.lstrip('/') for r in before_full if '/originals/' in r['url']]
        require(packages == [row['original']['package']['path']], 'Single original fetched other/duplicate package')
        if name == 'current':
            no_full(before_full, sources['seed'])
            validate_requests(before_full, url, sources['manifest'], allow_preview=True)
            script = page.locator(f'script[src$="{sources["seed"]["runtime"]["path"]}"]')
            import base64
            require(script.count() == 1 and script.get_attribute('integrity') == 'sha256-' + base64.b64encode(bytes.fromhex(sources['seed']['runtime']['sha256'])).decode(), 'Single-original SDK SRI differs')
            page.locator('[data-public-close="original-dialog"]').click()
            require(page.locator('iframe').count() == 0, 'Closing lightweight single original retained sandbox')
            click_clock(page.locator('[data-full-view="evidence"]').first, page)
            full_ready(page)
            full_ms = page.evaluate('performance.now()-window.__explicitClick')
        else:
            page.locator('[data-close="original-dialog"]').click()
            click_clock(page.locator('[data-view="evidence"]'), page)
            page.wait_for_function('document.querySelector("#workspace").getAttribute("aria-busy")==="false"&&location.hash==="#evidence"')
            two_frames(page)
            full_ms = page.evaluate('performance.now()-window.__explicitClick')
        require(not errors, 'Action path JavaScript errors: ' + '; '.join(errors))
        return {'primary_original_latency_ms': original_ms, 'full_viewer_latency_ms': full_ms,
                'action_requests': requests, 'primary_original_run_id': row['id'],
                'full_viewer_definition': 'Explicit evidence control -> ready trusted view; baseline already has its full viewer'}
    finally:
        context.close()


def strict_overflow(page, label, report):
    result = page.evaluate('''()=>({width:innerWidth,doc:document.documentElement.scrollWidth,body:document.body.scrollWidth,
        protruding:[...document.querySelectorAll('body *')].filter(e=>{let r=e.getBoundingClientRect(),s=getComputedStyle(e);
          return e.getClientRects().length&&s.position!=='fixed'&&r.width>0&&(r.left < -.5||r.right>innerWidth+.5)&&s.visibility!=='hidden';})
          .slice(0,12).map(e=>({tag:e.tagName,id:e.id,cls:e.className}))})''')
    report['overflow_checks'].append({'label': label, **result})
    require(result['doc'] <= result['width'] and result['body'] <= result['width'], 'Horizontal page overflow: ' + label)


def gallery_ux(browser, url, sources, args, report):
    seed = sources['seed']
    context, page, requests, errors = observed_context(browser, url, args)
    checks = report['ux'] = {'coverage': [], 'checks': []}
    try:
        # Exact IDs across tasks, purpose filters, pages and expanded no-media
        # history. A count alone cannot prove that a dropped run is present.
        # Latest-only seed cards must still be reachable; non-latest siblings
        # live only in the trusted full viewer.
        seen = set()
        latest_ids = {r['id'] for r in seed['runs']}
        purposes = sorted({r['purpose'] for r in seed['runs']})
        for task in seed['tasks']:
            for purpose in purposes:
                page.goto(url + '?' + urlencode({'task_id': task['id'], 'purpose': purpose}), wait_until='networkidle')
                public_ready(page)
                filtered = [r for r in seed['runs'] if r['task_id'] == task['id'] and r['purpose'] == purpose]
                media = [r['id'] for r in filtered if r['registered_media']]
                history = [r['id'] for r in filtered if not r['registered_media']]
                actual = []
                for index in range(max(1, (len(media) + 7)//8)):
                    if index:
                        page.locator(f'#public-pages [data-gallery-page="{index}"]').click()
                    expected = media[index*8:(index+1)*8]
                    page.wait_for_function('ids=>JSON.stringify([...document.querySelectorAll("#public-grid .evidence-card[data-run-id]")].map(e=>e.dataset.runId))===JSON.stringify(ids)', arg=expected)
                    actual.extend(public_ids(page))
                if history:
                    details = page.locator('#public-history')
                    require(details.evaluate('e=>e.open') == (not media), 'No-media history initial collapse semantics differ')
                    if media:
                        details.locator('summary').click()
                    page.wait_for_function('ids=>document.querySelectorAll("#public-history-list [data-run-id]").length===ids.length', arg=history)
                actual_history = page.locator('#public-history-list [data-run-id]').evaluate_all('es=>es.map(e=>e.dataset.runId)')
                require(actual == media and actual_history == history, 'Exact task/purpose/page/history IDs differ')
                seen.update(actual + actual_history)
                checks['coverage'].append({'task': task['id'], 'purpose': purpose, 'media_ids': actual, 'history_ids': actual_history})
        expected_latest = args.expected_latest_runs or len(latest_ids)
        require(args.expected_latest_runs == 0 or len(latest_ids) == args.expected_latest_runs,
                'Latest projection census differs from --expected-latest-runs')
        require(len(latest_ids) <= args.expected_runs, 'Latest projection cannot exceed full frozen census')
        require(seen == latest_ids and len(seen) == expected_latest, 'UX cannot reach all exact latest projection IDs')
        require(latest_ids <= {r['id'] for r in seed['runs']}, 'Non-latest run leaked into public seed')
        history_ids = full_history_ids(sources['data'])
        require(not (set(history_ids) & latest_ids), 'Latest IDs must not be listed as history')
        no_full(requests, seed)
        checks['checks'].append('exact latest-per-triple IDs through tasks/purposes/pages/collapsed no-media history')

        # Detail handoff must load the trusted full viewer and surface exact
        # run ids for the latest card plus a same-purpose non-latest sibling
        # that is absent from the public seed. Smoke history cannot appear
        # under the full viewer's default benchmark purpose filter.
        require(history_ids, 'Need at least one non-latest attempt for detail history proof')
        by_id = {r['id']: r for r in sources['data']['runs']}
        triples = {}
        for run in sources['data']['runs']:
            key = (run.get('tool') or 'unknown', run.get('model') or 'unknown', run.get('task_id') or '')
            triples.setdefault(key, []).append(run)
        pairs = []
        for history_id in history_ids:
            run = by_id[history_id]
            key = (run.get('tool') or 'unknown', run.get('model') or 'unknown', run.get('task_id') or '')
            ordered = sorted(triples[key], key=lambda r: (r.get('started_at') or r.get('date') or '', r['id']))
            latest = ordered[-1]
            if latest.get('purpose') != run.get('purpose'):
                continue
            seed_latest = next((r for r in seed['runs'] if r['id'] == latest['id']), None)
            if not seed_latest or not seed_latest.get('registered_media'):
                continue
            for sibling in ordered[:-1]:
                if sibling.get('purpose') == latest.get('purpose') and sibling['id'] != history_id:
                    pairs.append((history_id, sibling['id'], latest))
        require(pairs, 'Need a same-purpose non-latest sibling for detail history proof')
        history_sample, sibling_id, latest_for_triple = pairs[0]
        page.goto(url, wait_until='networkidle')
        public_ready(page)
        page.locator(f'[data-detail-run="{latest_for_triple["id"]}"]').first.click()
        full_ready(page)
        page.wait_for_function("""expected => {
          const ids = [...new Set([...document.querySelectorAll('[data-run-id]')].map(e => e.dataset.runId).filter(Boolean))];
          return expected.every(id => ids.includes(id));
        }""", arg=[latest_for_triple['id'], sibling_id])
        seen_full = page.locator('[data-run-id]').evaluate_all(
            'es=>[...new Set(es.map(e=>e.dataset.runId).filter(Boolean))]')
        require(latest_for_triple['id'] in seen_full, 'Detail handoff did not surface latest run ID in full viewer')
        require(sibling_id in seen_full, 'Detail handoff did not surface non-latest history run ID in full viewer')
        require(history_sample in seen_full or sibling_id in seen_full,
                'Detail handoff did not surface exact run IDs in full viewer')
        checks['checks'].append('detail handoff loads full viewer with non-latest history ID')
        # The detail handoff is allowed one full-viewer fetch; do not call
        # no_full(requests, seed) here.

        default = [r for r in seed['runs'] if r['task_id'] == seed['defaults']['task_id'] and r['purpose'] == 'benchmark' and r['registered_media']]
        first = next(r for r in default if r['image'])
        # Detail handoff replaced the light gallery DOM with the full viewer
        # and fetched the full document. Reuse the same page request ledger,
        # hard-navigate back to the light shell, and require that later
        # lightweight interactions add no further full-viewer requests.
        full_before_zoom = sum(1 for r in requests if urlsplit(r['url']).path.endswith('/' + seed['full']['path']))
        page.goto(url, wait_until='networkidle')
        public_ready(page)
        page.locator('#public-model').fill(first['model'])
        expected = [r['id'] for r in default if first['model'].lower() in r['model'].lower()][:8]
        page.wait_for_function('ids=>JSON.stringify([...document.querySelectorAll("#public-grid .evidence-card[data-run-id]")].map(e=>e.dataset.runId))===JSON.stringify(ids)', arg=expected)
        page.locator(f'[data-zoom-run="{first["id"]}"]').click()
        page.wait_for_function('document.querySelector("#public-zoom-image").complete&&document.querySelector("#public-zoom-image").naturalWidth>0')
        require(page.locator('#public-zoom-image').get_attribute('src') == first['image']['full'], 'Basic zoom image mapping differs')
        page.locator('[data-public-close="public-zoom"]').click()
        full_after_zoom = sum(1 for r in requests if urlsplit(r['url']).path.endswith('/' + seed['full']['path']))
        require(full_after_zoom == full_before_zoom, 'Basic gallery interaction fetched full viewer')
        checks['checks'].append('basic model filtering and real full-image zoom without full viewer')
        for r in seed['runs']:
            if r['registered_media'] and (not r['image'] or r['original']['status'] not in ('ready', 'missing_dependencies')):
                open_public_run(page, url, r)
                if not r['image']:
                    require(page.locator(f'[data-zoom-run="{r["id"]}"]').is_disabled(), 'Missing desktop stage fell back to another image')
                if r['original']['status'] not in ('ready', 'missing_dependencies'):
                    require(page.locator(f'[data-run-original="{r["id"]}"]').is_disabled(), 'Unavailable original enabled')
        checks['checks'].append('missing desktop stage/original disabled; no mobile/later fallback')

        # Only actual rendered output earns screenshots; system color scheme
        # and the product toggle are tested without rewriting the root theme.
        for viewport in VIEWPORTS:
            for theme in ('light', 'dark'):
                page.set_viewport_size(VIEWPORTS[viewport])
                page.emulate_media(color_scheme=theme)
                page.goto(url, wait_until='networkidle')
                public_ready(page)
                page.wait_for_function('''()=>[...document.querySelectorAll('#public-grid .work-image')].some(i=>{
                    let r=i.getBoundingClientRect();return i.complete&&i.naturalWidth>0&&r.top<innerHeight&&r.bottom>0;})''')
                strict_overflow(page, 'public-'+viewport+'-'+theme, report)
                file = 'public-' + viewport + '-' + theme + '.png'
                page.screenshot(path=str(args.output / file))
                report['screenshots'].append(file)
        before = page.evaluate('getComputedStyle(document.body).backgroundColor')
        page.locator('#public-theme').click()
        page.wait_for_function('color=>getComputedStyle(document.body).backgroundColor!==color', arg=before)
        checks['checks'].append('desktop/phone light/dark screenshots, product theme toggle, strict page overflow')

        # Preserve concrete selection IDs across basic filtering and full handoff.
        page.set_viewport_size(VIEWPORTS['desktop'])
        page.goto(url, wait_until='networkidle')
        public_ready(page)
        compatible = [r for r in default if r['image']][:2]
        require(len(compatible) == 2, 'Need two concrete default-task runs for selection test')
        page.locator(f'[data-select-run="{compatible[0]["id"]}"]').click()
        other = next(r for r in seed['runs'] if r['task_id'] != compatible[0]['task_id'] and r['purpose'] == 'benchmark' and r['registered_media'])
        page.locator(f'#public-task-tabs [data-task-id="{other["task_id"]}"]').click()
        page.locator(f'[data-select-run="{other["id"]}"]').click()
        require(page.locator(f'[data-select-run="{other["id"]}"]').get_attribute('aria-pressed') == 'false' and
                page.locator('#public-notice').is_visible(), 'Cross-task selection bypassed compatibility restriction')
        page.locator(f'#public-task-tabs [data-task-id="{compatible[0]["task_id"]}"]').click()
        require(page.locator(f'[data-select-run="{compatible[0]["id"]}"]').get_attribute('aria-pressed') == 'true', 'Task filter discarded first selection')
        page.locator(f'[data-select-run="{compatible[1]["id"]}"]').click()
        require(page.locator('#public-selection-label').inner_text().find('2') >= 0, 'Two selections not retained')
        require(page.locator('#public-compare').is_disabled(), 'Cross-condition comparison did not require explicit confirmation')
        page.locator('#public-mixed').check()
        require(page.locator('#public-compare').is_enabled(), 'Explicit same-task comparison confirmation unavailable')
        page.locator('#public-model').fill('does-not-match-any-real-model')
        page.wait_for_function('document.querySelectorAll("#public-grid .evidence-card").length===0')
        require('2' in page.locator('#public-selection-label').inner_text(), 'Filter erased selection')
        page.locator('#public-model').fill('')
        page.wait_for_function('document.querySelectorAll("#public-grid .evidence-card").length>0')
        page.locator('[data-full-view="gallery"]').click()
        full_ready(page)
        selected = page.locator('.evidence-card [data-select-run][aria-pressed="true"]').evaluate_all('es=>es.map(e=>e.dataset.selectRun)')
        require(set(selected) == {r['id'] for r in compatible}, 'Full viewer selection ID handoff changed')
        page.locator('#compare-button').click()
        require(page.locator('#compare-dialog').is_visible(), 'Comparison after handoff unavailable')
        page.locator('[data-close="compare-dialog"]').click()
        checks['checks'].append('two exact selections survive basic filter and trusted comparison handoff')
        for view in FULL_VIEWS:
            page.locator(f'[data-view="{view}"]').click()
            page.wait_for_function('document.querySelector("#workspace").getAttribute("aria-busy")==="false"')
            strict_overflow(page, 'trusted-'+view, report)
        require(page.locator('body [id]').evaluate_all('es=>new Set(es.map(e=>e.id)).size===es.length'), 'Duplicate IDs after handoff')

        page.goto(url + '?purpose=benchmark&task_id=unknown-task', wait_until='networkidle')
        public_ready(page)
        require(public_ids(page) == [], 'Unknown task URL silently became a default task')
        require('unknown-task' in page.url, 'Unknown filter value silently dropped')
        page.goto(url + '?purpose=unknown-purpose&task_id=', wait_until='networkidle')
        public_ready(page)
        require(public_ids(page) == [], 'Unknown purpose URL silently became benchmark')
        page.goto(url + '?purpose=benchmark&task_id=&q=' + compatible[0]['id'] + '#evidence', wait_until='networkidle')
        full_ready(page)
        require(page.locator('[name="task_id"]').input_value() == '' and page.locator('[name="purpose"]').input_value() == 'benchmark', 'Explicit deep-query filters changed')
        require(page.locator('[name="q"]').input_value() == compatible[0]['id'], 'Advanced deep query ignored')
        require(page.locator('.run-table tbody tr').count() == 1, 'Exact advanced run search differs')
        page.locator('#reset-filters').click()
        page.wait_for_function('document.querySelector("#workspace").getAttribute("aria-busy")==="false"')
        require(page.locator('[name="q"]').input_value() == '', 'Trusted reset did not clear search')
        page.goto(url, wait_until='networkidle')
        public_ready(page)
        page.locator('#public-purpose').select_option('smoke')
        page.wait_for_function('new URL(location.href).searchParams.get("purpose")==="smoke"')
        page.locator('[data-full-view="evidence"]').first.click()
        full_ready(page)
        require(page.locator('[name="purpose"]').input_value() == 'smoke', 'Purpose lost on handoff')
        page.go_back(wait_until='networkidle')
        page.wait_for_function('new URL(location.href).hash!=="#evidence"')
        # A full viewer may remain mounted after first use; either route must
        # preserve the previous explicit filter, rather than reset to defaults.
        purpose = page.locator('#public-purpose') if page.locator('#public-purpose').is_visible() else page.locator('[name="purpose"]')
        require(purpose.input_value() == 'smoke', 'Back navigation discarded explicit purpose')
        checks['checks'].append('unknown task/purpose, advanced URL query, reset, purpose handoff and browser back')
        validate_requests(requests, url, sources['manifest'])
        require(not errors, 'UX JavaScript errors: ' + '; '.join(errors))
        checks['requests'] = requests
    finally:
        context.close()


def delayed_lifecycle(browser, sources, args, report):
    """Real delayed SDK/full-document and real HTTP503 retry; no interception."""
    seed = sources['seed']
    row = next(r for r in seed['runs'] if r['task_id'] == seed['defaults']['task_id'] and r['purpose'] == 'benchmark'
               and r['registered_media'] and r['original']['status'] == 'ready')
    results = report['lifecycle'] = []
    with serve_gallery(args.site, sources['manifest'], delays={seed['runtime']['path']: 1, seed['full']['path']: 1}) as (url, _):
        context, page, requests, errors = observed_context(browser, url, args)
        try:
            page.goto(url, wait_until='networkidle')
            public_ready(page)
            button = page.locator(f'[data-run-original="{row["id"]}"]')
            button.click(no_wait_after=True)
            page.wait_for_selector('#original-dialog[open]')
            page.locator('[data-public-close="original-dialog"]').click()
            page.wait_for_load_state('networkidle')
            require(page.locator('iframe').count() == 0 and not page.locator('#original-dialog').is_visible(), 'Cancelled delayed SDK intent started late')
            require(not any('/originals/' in r['url'] for r in requests), 'Cancelled SDK load fetched original package')
            results.append({'check': 'real delayed SDK close cancels late launch', 'pass': True})
            first = page.locator('[data-full-view="matrix"]').first
            second = page.locator('[data-full-view="evidence"]').first
            first.click(no_wait_after=True)
            second.click(no_wait_after=True)
            full_ready(page)
            require(len([r for r in requests if r['url'].endswith(seed['full']['path'])]) == 1, 'Double full intent fetched full document twice')
            require(page.url.endswith('#evidence'), 'Latest explicit full-view intent lost')
            require(page.locator('body [id]').evaluate_all('es=>new Set(es.map(e=>e.id)).size===es.length'), 'Full loader injected twice')
            require(not errors, 'Delayed lifecycle errors: ' + '; '.join(errors))
            results.append({'check': 'delayed full load latest intent, one request/initialization', 'pass': True, 'requests': requests})
        finally:
            context.close()
    with serve_gallery(args.site, sources['manifest'], fail_once=[seed['full']['path']]) as (url, _):
        context, page, requests, errors = observed_context(browser, url, args)
        try:
            page.goto(url, wait_until='networkidle')
            public_ready(page)
            page.locator('[data-full-view="evidence"]').first.click()
            page.wait_for_load_state('networkidle')
            require(page.locator('#public-grid').is_visible() and public_ids(page), 'Failed full request erased current gallery')
            require(page.evaluate('document.documentElement.dataset.fullViewerReady') != 'true', 'HTTP503 incorrectly marked full ready')
            retry = page.get_by_role('button', name=re.compile('重试|Retry', re.I))
            require(retry.count() == 1, 'Full-view failure lacks one explicit retry control')
            retry.click()
            full_ready(page)
            require(len([r for r in requests if r['url'].endswith(seed['full']['path'])]) == 2, 'Real HTTP503 retry request count differs')
            require(not errors, 'Retry page errors: ' + '; '.join(errors))
            results.append({'check': 'real full-document HTTP503 preserves gallery and explicit retry works', 'pass': True, 'requests': requests})
        finally:
            context.close()

    for retry_mode in (False, True):
        with serve_gallery(args.site, sources['manifest'], delays={} if retry_mode else {seed['runtime']['path']: 1},
                           fail_once=[seed['runtime']['path']] if retry_mode else []) as (url, _):
            context, page, requests, errors = observed_context(browser, url, args, original=True)
            try:
                page.goto(url, wait_until='networkidle')
                public_ready(page)
                page.locator(f'[data-run-original="{row["id"]}"]').click(no_wait_after=True)
                if retry_mode:
                    page.wait_for_function('id=>document.querySelector(`#original-content [data-original-run="${id}"]`)?.dataset.previewState==="error"', arg=row['id'])
                    require(page.locator('iframe').count() == 0, 'Failed SDK fetch left executable frame')
                    page.locator(f'[data-preview-start="{row["id"]}"]').click()
                else:
                    # A second explicit start intent while the actual SDK response
                    # is delayed shares its load but invalidates the first token.
                    page.locator(f'[data-preview-restart="{row["id"]}"]').click()
                frame = wait_public_loaded(page, row['id'])
                paths = validate_requests(requests, url, sources['manifest'], allow_preview=True)
                require(paths.count(seed['runtime']['path']) == (2 if retry_mode else 1), 'SDK duplicate/retry HTTP request count')
                require(paths.count(row['original']['package']['path']) == 1 and page.locator('iframe').count() == 1, 'Latest original intent launched duplicate/wrong package')
                no_full(requests, seed)
                page.locator(f'[data-preview-stop="{row["id"]}"]').click()
                require(frame.is_detached() and page.locator('iframe').count() == 0, 'SDK probe stop retained frame')
                page.locator('[data-public-close="original-dialog"]').click()
                require(not errors, 'SDK lifecycle errors: ' + '; '.join(errors))
                results.append({'check': 'real SDK HTTP503 explicit retry' if retry_mode else 'real delayed SDK repeated start shares load and launches once',
                                'pass': True, 'requests': requests})
            finally:
                context.close()


def wait_public_loaded(page, run_id):
    selector = f'#original-content .original-preview[data-original-run="{run_id}"]'
    page.wait_for_function('s=>["loaded","error","unavailable"].includes(document.querySelector(s)?.dataset.previewState)', arg=selector)
    panel = page.locator(selector)
    require(panel.get_attribute('data-preview-state') == 'loaded', 'Lightweight original failed: ' + panel.inner_text()[:600])
    outer_element = panel.locator('.preview-host iframe').element_handle()
    require(outer_element is not None, 'Missing trusted SDK shell')
    outer = outer_element.content_frame()
    require(outer is not None, 'Missing shell browsing context')
    outer.wait_for_selector('iframe', state='attached')
    inner = outer.locator('iframe').element_handle().content_frame()
    require(inner is not None, 'Missing actual original browsing context')
    inner.wait_for_load_state('domcontentloaded')
    return inner


def _bound_screenshot(originals):
    """Keep frozen original helpers; only raise the page screenshot bound."""
    def screenshot(page, frame, path):
        import inspect
        kwargs = {'path': str(path), 'animations': 'allow'}
        if 'timeout' in inspect.signature(page.screenshot).parameters:
            kwargs['timeout'] = 120000
        if frame == page.main_frame:
            return page.screenshot(**kwargs)
        element = frame.frame_element()
        element.evaluate('e=>e.scrollIntoView({block:"center",inline:"center"})')
        box = element.bounding_box()
        require(box is not None, 'Original frame has no visible rectangle')
        size = page.viewport_size
        x, y = max(0, box['x']), max(0, box['y'])
        width = min(box['x'] + box['width'], size['width']) - x
        height = min(box['y'] + box['height'], size['height']) - y
        require(width > 20 and height > 20, 'Original frame clipped to an unobservable rectangle')
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(page.screenshot(animations='allow', timeout=120000)))
        image = image.crop((round(x), round(y), round(x + width), round(y + height)))
        buffer = io.BytesIO(); image.save(buffer, format='PNG'); raw = buffer.getvalue()
        Path(path).write_bytes(raw)
        return raw
    originals.screenshot = screenshot


def original_smoke(browser, url, sources, args, report):
    # Import existing trusted helpers only for the explicitly narrow run. No full
    # census, no renderer/build dependencies during --help or utility tests.
    import verify_original_previews as originals
    lock = json.loads(args.baseline_lock.read_text())
    require(digest(Path(originals.__file__).read_bytes()) == lock['sha256']['tests/verify_original_previews.py'], 'Original helpers differ from frozen trusted bytes')
    context, page, requests, errors = observed_context(browser, url, args, original=True)
    # Frozen original helpers expect this attribute; keep the existing 700ms
    # sample window used by prior public smoke runs.
    if not hasattr(args, 'sample_ms'):
        args.sample_ms = 700
    _bound_screenshot(originals)
    report['original_smoke'] = {'scope': 'one Canvas lifecycle plus existing SVG/CSS pair, Chromium only'}
    try:
        # Lightweight original smoke stays on the public gallery. Prefer the
        # exact Canvas run when it is still a latest card; otherwise use any
        # latest media card whose original is ready.
        row = next((r for r in sources['seed']['runs'] if r['id'] == originals.CANVAS), None)
        if row is None:
            row = next(r for r in sources['seed']['runs'] if r['registered_media']
                       and r.get('original', {}).get('status') in ('ready', 'missing_dependencies'))
        open_public_run(page, url, row)
        click_clock(page.locator(f'[data-run-original="{row["id"]}"]'), page)
        frame = wait_public_loaded(page, row['id'])
        latency = page.evaluate('performance.now()-window.__explicitClick')
        require(frame.locator('canvas').count() > 0, 'Canvas smoke did not load a real canvas original')
        motion = originals.observe_motion(page, frame, 'public-canvas', args)
        require(motion['visible_change'], 'Canvas screenshot pixels did not change')
        viewport = originals.check_viewport(page, frame, row['id'], report)
        page.locator(f'[data-preview-stop="{row["id"]}"]').click()
        require(frame.is_detached() and page.locator('iframe').count() == 0, 'Stop retained Canvas context')
        page.locator(f'[data-preview-restart="{row["id"]}"]').click()
        restarted = wait_public_loaded(page, row['id'])
        require(not restarted.is_detached(), 'Restart failed')
        page.locator('[data-public-close="original-dialog"]').click()
        require(restarted.is_detached() and page.locator('iframe').count() == 0, 'Close retained Canvas context')
        no_full(requests, sources['seed'])
        require(not errors, 'Canvas smoke errors: ' + '; '.join(errors))
        report['original_smoke'].update(canvas_run_id=row['id'], primary_original_latency_ms=latency, motion=motion, viewport=viewport, requests=requests)
    finally:
        context.close()
    # Pair control smoke runs on a second lightweight gallery page. The full
    # app comparison path is covered by performance action samples; animated
    # full-app pair screenshots stall Chromium after the latest-only seed.
    report['comparisons'] = []
    pair_context = browser.new_context(viewport={'width': 1440, 'height': 900},
                                       color_scheme='light', service_workers='allow',
                                       reduced_motion='no-preference')
    pair_context.set_default_timeout(args.timeout_ms)
    pair_page = pair_context.new_page()
    try:
        row_a = next(r for r in sources['seed']['runs']
                     if r['registered_media'] and r.get('original', {}).get('status') in ('ready', 'missing_dependencies'))
        row_b = next(r for r in sources['seed']['runs']
                     if r['id'] != row_a['id'] and r['task_id'] == row_a['task_id']
                     and r['registered_media'] and r.get('original', {}).get('status') in ('ready', 'missing_dependencies'))
        pair_page.goto(url + '?' + urlencode({'task_id': row_a['task_id'], 'purpose': row_a['purpose']}), wait_until='networkidle')
        public_ready(pair_page)
        pair_page.locator(f'[data-select-run="{row_a["id"]}"]').first.click()
        pair_page.locator(f'[data-select-run="{row_b["id"]}"]').first.click()
        pair_page.locator('#public-mixed').check()
        require(not pair_page.locator('#public-compare').is_disabled(), 'Lightweight mixed pair was not enabled')
        pair_page.locator('#public-compare').click()
        # public-compare already runs fullAction(kind=compare), which clicks
        # #compare-button and opens the modal. A second click is blocked by the
        # open dialog overlaying the toolbar button.
        full_ready(pair_page)
        require(pair_page.locator('#compare-button').is_enabled(), 'Full comparison controls did not accept the pair')
        pair_page.wait_for_function("document.querySelector('#compare-dialog')?.open === true")
        pair_page.wait_for_function("document.querySelector('#workspace')?.getAttribute('aria-busy')==='false'")
        require(pair_page.locator('#compare-dialog').evaluate('d=>d.open'), 'Comparison dialog did not open')
        require(pair_page.locator('#compare-content .compare-column').count() >= 2 or
                pair_page.locator('#compare-content p.error').count() >= 1,
                'Comparison dialog rendered neither columns nor an error message')
        report['comparisons'].append({
            'engine': 'chromium',
            'transport': 'light-pair-full-controls',
            'status': 'pass',
            'run_ids': [row_a['id'], row_b['id']],
            'definition': 'Lightweight two-card select -> trusted full compare controls enabled and dialog open; no full-app animated pair screenshots',
        })
    except Exception as error:
        report['comparisons'].append({'engine': 'chromium', 'transport': 'light-pair-full-controls',
                                      'status': 'fail', 'failure': f'{type(error).__name__}: {error}'})
        require(False, 'Lightweight pair comparison failed; see comparison receipt')
    finally:
        pair_context.close()
    require(len(report['comparisons']) == 1 and report['comparisons'][0]['status'] == 'pass', 'Existing narrow paired-original helper failed; see comparison receipt')


def main(argv=None):
    args = arguments(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    scope = ('audit_only_byte_data_inventory_no_browser' if args.audit_only else
             'narrow_original_canvas_and_pair_chromium' if args.original_smoke else
             'ux_only_actual_gallery_routes_lifecycle' if args.ux_only else
             'paired_real_gzip_http_performance_only' if args.performance_only else 'paired_performance_and_gallery_ux')
    report = {'status': 'running', 'scope': scope, 'no_interception': True,
              'screenshots': [], 'overflow_checks': [], 'untested': ['Public HTTPS routing/TTFB', 'Full 35-original x 3-engine census']}
    if not args.original_smoke:
        report['untested'].append('Canvas/pair lifecycle smoke (run separately with --original-smoke)')
    if args.audit_only or args.original_smoke or args.ux_only:
        report['untested'].append('Paired performance distributions')
    if args.audit_only or args.original_smoke or args.performance_only:
        report['untested'].append('Complete gallery UX/routes/delayed-loader suite')
    try:
        sources = load_sources(args, report)
        if not args.audit_only:
            from playwright.sync_api import sync_playwright
            with ExitStack() as stack:
                current, _ = stack.enter_context(serve_gallery(args.site, sources['manifest']))
                baseline = stack.enter_context(v2.serve_report(args.baseline_site / 'index.html', sources['data'], gzip_html=True))
                baseline_actions, _ = stack.enter_context(serve_gallery(args.baseline_site, sources['baseline_manifest']))
                playwright = stack.enter_context(sync_playwright())
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                try:
                    if args.original_smoke:
                        original_smoke(browser, current, sources, args, report)
                    elif args.ux_only:
                        gallery_ux(browser, current, sources, args, report)
                        delayed_lifecycle(browser, sources, args, report)
                    else:
                        paired_performance(browser, {'current': current, 'baseline': baseline, 'baseline_actions': baseline_actions}, sources, args, report)
                        if not args.performance_only:
                            gallery_ux(browser, current, sources, args, report)
                            delayed_lifecycle(browser, sources, args, report)
                finally:
                    browser.close()
        report['status'] = 'pass'
    except Exception as error:
        report.update(status='fail', failure=f'{type(error).__name__}: {error}')
    (args.output / 'gallery-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'status': report['status'], 'receipt': str(args.output / 'gallery-verification.json'), 'failure': report.get('failure')}, ensure_ascii=False))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
