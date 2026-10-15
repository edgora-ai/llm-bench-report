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
import importlib.util
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
FULL_VIEWS = ('gallery', 'matrix', 'batch', 'evidence', 'trend', 'blind', 'scores')
CANDIDATE_SOURCES = frozenset(('web/app.js', 'web/preview-runtime.js', 'web/index.html', 'web/styles.css',
    'web/public-gallery.html', 'web/public-gallery.css', 'web/public-gallery.js',
    'tests/make_gallery.py', 'tests/test_gallery_site.py', 'tests/test_public_gallery_performance.py',
    'tests/verify_public_gallery.py', 'tests/verify_public_snapshot.py'))
PAGE_SIZE = 2
PHONE_PAGE_SIZE = 1
# Gallery entry-point ceilings. Doubled on 2026-10-15 at the operator's explicit
# instruction after two more reviewed samples pushed the entry to 91,951 decoded
# / 26,067 gzip. The verifier stays independent of the builder's own constants.
ENTRY_DECODED_BUDGET = 200 * 1024
ENTRY_GZIP_BUDGET = 50 * 1024
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
    p.add_argument('--candidate-source-lock', type=Path, help='Independent final operator JSON {sha256:{12 planned relative paths:digest}}; mandatory for candidate verification including runtime, outside source/input bundles')
    p.add_argument('--candidate-viewer-script', type=Path, help='Current app.js named in the independent candidate-source-lock, not the frozen viewer')
    p.add_argument('--source-root', type=Path, default=ROOT, help='Reviewed current project source; never the generated candidate')
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
    p.add_argument('--score-source', help='Independently expected build preference: exact JSON scope or *; omitted requires legacy default')
    a = p.parse_args(argv)
    require(a.timeout_ms > 0, 'Timeout must be positive')
    require(a.expected_runs > 0 and a.expected_benchmark >= 0 and a.expected_reviews >= 0 and a.expected_latest_runs >= 0, 'Invalid census')
    for name in ('site', 'baseline_site', 'trust_root', 'source_root'):
        path = getattr(a, name).resolve(strict=True)
        require(path.is_dir(), name + ' must be a directory')
        setattr(a, name, path)
    require(a.site != a.baseline_site, 'Candidate and frozen baseline must differ')
    require(not a.trust_root.is_relative_to(a.site), 'Trust cannot come from candidate')
    require(all(not a.source_root.is_relative_to(root) for root in (a.site, a.baseline_site)), 'Current source cannot come from an input bundle')
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
        self.card_attrs = {}
        self.name_attrs = {}
        self.card = None
        self.field = None
        self.score_cells, self.structure = {}, {}
        self.scope = None
        self.cell = None
        self.score_field = None
        self.detail_depth = 0

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
            self.card_attrs[self.card] = attrs
            self.score_cells[self.card], self.structure[self.card] = [], []
        if tag == 'details':
            self.detail_depth += 1
        if self.card:
            classes = attrs.get('class', '').split()
            for role in ('work-heading', 'work-score', 'work-cover', 'work-actions', 'work-facts'):
                if role in classes:
                    self.structure[self.card].append(role)
            if 'work-score' in classes:
                self.scope = attrs.get('data-score-scope')
            if 'score-cell' in classes:
                self.cell = {'dimension': attrs.get('data-dimension'), 'scope': self.scope,
                             'folded': self.detail_depth > 0, 'label': [], 'value': []}
                self.score_cells[self.card].append(self.cell)
            if self.cell and tag in ('dt', 'dd'):
                self.score_field = 'label' if tag == 'dt' else 'value'
            if 'work-name' in attrs.get('class', '').split():
                self.name_attrs[self.card] = attrs
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
        if self.cell and self.score_field:
            self.cell[self.score_field].append(text)

    def handle_endtag(self, tag):
        if tag == 'details':
            self.detail_depth -= 1
        if tag in ('dt', 'dd'):
            self.score_field = None
        if tag == 'div' and self.cell:
            self.cell = None
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
        keys = []
        for path in declared:
            if isinstance(path, str) and re.search(r'\.(png|jpe?g|webp|gif|webm|mp4)$', path, re.I):
                candidate = r['id'] + '/' + path
                if candidate not in keys:
                    keys.append(candidate)
        included = sum(key in data['evidence'] for key in keys)
        key = next((key for key in keys if key.rsplit('/', 1)[-1] == 'desktop.png'), None)
        gaps = []
        if not keys:
            gaps.append('no_media_registered')
        if included != len(keys):
            gaps.append('media_not_included')
        if keys and key is None:
            gaps.append('no_desktop_capture')
        elif key is not None and key not in data['evidence']:
            gaps.append('desktop_not_included')
        elif key is not None and key not in data.get('thumbnails', {}):
            gaps.append('desktop_no_thumbnail')
        full, thumb = data['evidence'].get(key), data.get('thumbnails', {}).get(key)
        image = None
        if full and thumb:
            asset = data['assets'][full]
            if asset['mime'].startswith('image/'):
                image = {'thumb': thumb, 'full': full, 'width': asset['width'], 'height': asset['height'], 'label': 'desktop.png'}
        summaries = v2.score_summaries(r)
        rows.append({**({'scores': summaries} if summaries else {}), 'meter': v2.meter(r),
                     **{k: r.get(k) for k in ('id', 'task_id', 'prompt_version', 'started_at')},
                     **{k: r.get(k) or 'unknown' for k in ('model', 'tool', 'purpose')},
                     'date': str(r.get('date') or r.get('started_at') or '')[:10] or None,
                     'status': r.get('generation_status') or r.get('status') or 'unknown', 'entry_status': entry_status(r),
                     'evaluation_status': (r.get('evaluation') or {}).get('status') or 'unknown',
                     'registered_media': included > 0, 'media_counts': [len(keys), included],
                     **({'media_gaps': gaps} if gaps else {}),
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


def decode_score_rows(seed):
    """Independent strict scope-index-v1 reader; never imports the producer codec."""
    require(seed.get('score_encoding') == 'scope-index-v1', 'Unknown gallery score encoding')
    table = seed.get('score_scopes')
    require(isinstance(table, list), 'Score scope table shape')
    labels = []
    for item in table:
        require(isinstance(item, list) and len(item) == 4, 'Score scope shape')
        rubric, provenance, blind, reviewer = item
        require(rubric is None or isinstance(rubric, str) and rubric not in ('', 'unknown'), 'Score rubric shape')
        require(provenance in ('ai-suggestion', 'label-unverified') and (blind is None or type(blind) is bool), 'Score provenance/blind shape')
        require(reviewer is None or isinstance(reviewer, str) and reviewer.strip(v2.REVIEWER_SPACE), 'Score reviewer shape')
        inferred = 'ai-suggestion' if blind is False and v2.AI_LABEL.search(reviewer or '') else 'label-unverified'
        require(provenance == inferred, 'Conflicting score provenance')
        labels.append(v2.scope_key(item))
    require(labels == sorted(set(labels), key=lambda value: value.encode('utf-16-be', 'surrogatepass')), 'Duplicate/unordered score scopes')
    require(isinstance(seed.get('runs'), list), 'Score rows shape')
    decoded = []
    for record in seed['runs']:
        require(isinstance(record, dict), 'Score row shape')
        row = dict(record)
        if 'scores' in record:
            entries = record['scores']
            require(isinstance(entries, list) and entries, 'Score entries shape')
            summaries, last = [], -1
            for entry in entries:
                require(isinstance(entry, list) and len(entry) == 3, 'Score tuple shape')
                slot, source_index, values = entry
                require(type(slot) is int and last < slot < len(labels), 'Score scope index')
                require(type(source_index) is int and 0 <= source_index <= 9007199254740991, 'Score raw review index')
                require(isinstance(values, list) and len(values) == len(v2.SCORE_LABELS), 'Score values shape')
                require(all(value is None or v2.valid_number(value) and value <= 4 for value in values), 'Score value range/type')
                summaries.append({'scope': labels[slot], 'index': source_index, 'values': values[:]})
                last = slot
            row['scores'] = summaries
        decoded.append(row)
    return decoded


def audit_gallery(html, data, bootstrap, css, full_info, runtime_info, trusted_program, *, score_source=None):
    parsed = GalleryHTML()
    parsed.feed(html)
    parsed.close()
    require(not parsed.unsafe and len(parsed.scripts) == 2 and len(parsed.styles) == 1, 'Unsafe gallery scaffold')
    match = re.fullmatch(r'\s*window\.BENCH_GALLERY=(\{.*\});\s*', parsed.scripts[0], re.S)
    require(match is not None and '<' not in parsed.scripts[0], 'Unescaped gallery seed')
    seed = json.loads(match[1])
    require(parsed.scripts[1].strip() == bootstrap.strip() and parsed.styles[0].strip() == css.strip(), 'Gallery executable/style differs from trusted source')
    expected_keys = {'version', 'format', 'score_encoding', 'tasks', 'runs', 'score_scopes', 'defaults', 'counts', 'full', 'runtime', 'offline'}
    if score_source is not None:
        expected_keys.add('preferred_score_source')
        require(seed.get('preferred_score_source') == score_source, 'Independent preferred source differs')
    require(set(seed) == expected_keys, 'Gallery seed schema')
    require(type(seed['version']) is int and seed['version'] == 3 and seed['format'] == 'progressive-static-v3', 'Gallery version/profile')
    rows = decode_score_rows(seed)
    # Independently verify every deferred reference and six values against the raw record.
    expected_rows = project_runs(data)
    for row in expected_rows:
        if 'scores' in row:
            row['scores'] = [{key: summary[key] for key in ('scope', 'index', 'values')} for summary in row['scores']]
    require(same(rows, expected_rows), 'Exact latest-per-triple projection/order or raw review reference differs')
    tasks = {t['id']: {'id': t['id'], 'name': t.get('name') or t['id']} for t in data['tasks']}
    for row in data['runs']:
        if row.get('task_id') and row['task_id'] not in tasks:
            tasks[row['task_id']] = {'id': row['task_id'], 'name': row.get('task_name') or row['task_id']}
    ordered_tasks = sorted(tasks.values(), key=lambda t: (t['id'] != 'crocodile', t['id']))
    require(seed['tasks'] == ordered_tasks, 'Task metadata differs')
    default_task = 'crocodile' if any(t['id'] == 'crocodile' for t in data['tasks']) else data['tasks'][0]['id']
    scopes = v2.score_scopes(data['runs'])
    require(same(seed['score_scopes'], scopes), 'Exact score scope census/order differs')
    require(same(seed['defaults'], {'task_id': default_task, 'purpose': 'benchmark', 'page_size': PAGE_SIZE,
                                   'mobile_page_size': PHONE_PAGE_SIZE, 'score_source': v2.default_score_source(scopes, score_source)}), 'Defaults differ')
    require(same(seed['counts'], {'runs': len(seed['runs']), 'full_runs': len(data['runs']),
                              'benchmark': sum(r.get('purpose') == 'benchmark' for r in seed['runs']),
                              'full_benchmark': sum(r.get('purpose') == 'benchmark' for r in data['runs']),
                              'reviews': sum(len(r.get('reviews') or []) for r in data['runs'])}), 'Gallery counts differ')
    require(same(seed['offline'], data['offline']), 'Offline descriptor changed')
    require(same(seed['full'], {**full_info, 'script_sha256': digest(trusted_program)}), 'Full viewer descriptor differs')
    require(same(seed['runtime'], runtime_info), 'Runtime descriptor differs')
    expected = [r for r in rows if r['task_id'] == default_task and r['purpose'] == 'benchmark' and r['registered_media']][:PAGE_SIZE]
    require(parsed.cards == [r['id'] for r in expected], 'Server-rendered initial card IDs differ')
    thumbnails = [i for i in parsed.images if 'work-image' in i.get('class', '').split()]
    require([i.get('data-src') for i in thumbnails] == [r['image']['thumb'] for r in expected if r['image']], 'Initial thumbnail mapping differs')
    require(all(i in thumbnails or (i.get('id') == 'public-zoom-image' and not i.get('data-src')) for i in parsed.images), 'Undeclared initial image')
    for row in expected:
        fields = {key: ''.join(value).strip() for key, value in parsed.card_details[row['id']].items()}
        short_model = row['model'][9:] if row['model'].startswith('opencode/') else row['model']
        require(fields.get('work-name') == short_model and parsed.card_attrs[row['id']].get('data-model-route') == row['model']
                and parsed.name_attrs[row['id']].get('aria-label') == row['model'] and parsed.name_attrs[row['id']].get('title') == row['model']
                and row['tool'] in fields.get('work-meta', '') and str(row['date']) in fields.get('work-meta', ''), 'Initial visible label/canonical identity/tool/date differs')
        status = fields.get('work-status', '')
        require(all(value in status for value in (row['status'], row['entry_status'], row['evaluation_status'])), 'Initial card source status presentation differs')
        source = seed['defaults']['score_source']
        summaries = row.get('scores', [])
        selected = summaries if source == '*' and summaries else [next((s for s in summaries if s['scope'] == source), None)]
        cells = parsed.score_cells[row['id']]
        require(len(cells) == 6 * len(selected) and all(not cell['folded'] for cell in cells), 'Six scores must remain unfolded per exact scope')
        require(parsed.structure[row['id']] == ['work-heading'] + ['work-score'] * len(selected) +
                ['work-cover', 'work-actions', 'work-facts'], 'Score-first card order differs')
        for index, summary in enumerate(selected):
            scope = json.loads(summary['scope']) if summary else json.loads(source) if source not in ('', '*') else None
            valid = v2.scope_rankable(scope)
            for dimension_index, (key, label) in enumerate(v2.SCORE_LABELS.items()):
                cell = cells[index * 6 + dimension_index]
                value = summary['values'][dimension_index] if summary and valid else None
                missing = ('未评审' if not summaries else '本来源无记录') if not summary else '协议未核实' if not valid else '分项缺失'
                figure = str(int(value)) if value is not None and value == int(value) else str(value)
                require(cell['dimension'] == key and cell['scope'] == (summary['scope'] if summary else source)
                        and ''.join(cell['label']) == label and ''.join(cell['value']) == (figure + '/4' if value is not None else missing),
                        'Server six-score value/missing state or scope differs')
        for attr in ('data-run-original', 'data-select-run', 'data-zoom-run', 'data-detail-run'):
            buttons = [b for b in parsed.buttons if b.get(attr) == row['id']]
            require(len(buttons) == 1, 'Missing/duplicate server card action: ' + attr)
            if attr in ('data-run-original', 'data-zoom-run'):
                runnable = row['original']['status'] in ('ready', 'missing_dependencies')
                disabled = not (row['image'] or runnable) if attr == 'data-zoom-run' else not runnable
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


def candidate_sources(args, report):
    require(args.candidate_source_lock is not None and args.candidate_viewer_script is not None,
            'Candidate needs an independent final source lock and --candidate-viewer-script')
    path = args.candidate_source_lock
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16384, 'Candidate source lock must be a bounded regular file')
    require(all(not path.resolve().is_relative_to(root.resolve()) for root in (args.site, args.baseline_site, args.source_root, args.trust_root)),
            'Candidate source lock must be outside all source/input/trust trees')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate candidate source lock key')
            result[key] = value
        return result
    raw = path.read_bytes()
    lock = json.loads(raw, object_pairs_hook=unique)
    require(isinstance(lock, dict) and set(lock) == {'sha256'} and isinstance(lock['sha256'], dict)
            and set(lock['sha256']) == CANDIDATE_SOURCES, 'Candidate source lock must name exactly the twelve planned sources')
    sources = {}
    for relative, expected in lock['sha256'].items():
        require(isinstance(expected, str) and SHA.fullmatch(expected), 'Invalid candidate source digest')
        sources[relative] = safe_read(args.source_root, relative)
        require(digest(sources[relative]) == expected, 'Approved current source bytes differ: ' + relative)
    require(Path(v2.__file__).resolve() == (args.source_root / 'tests/verify_public_snapshot.py').resolve()
            and Path(__file__).resolve() == (args.source_root / 'tests/verify_public_gallery.py').resolve(), 'Current verifier/helper not from locked source tree')
    for actual, relative in ((args.candidate_viewer_script, 'web/app.js'), (args.gallery_bootstrap, 'web/public-gallery.js'), (args.gallery_css, 'web/public-gallery.css')):
        require(actual.resolve(strict=True) == (args.source_root / relative).resolve(), 'Candidate viewer/bootstrap/CSS path is not the pinned current source')
    report['candidate_source_lock'] = {'path': str(path.resolve()), 'sha256': digest(raw), 'sources': lock['sha256']}
    return sources


def current_document(data, sources, runtime):
    """Independent exact composition, matching render_snapshot without importing a builder."""
    html = sources['web/index.html'].decode()
    html = html.replace('<link rel="stylesheet" href="/styles.css">', '<style>' + sources['web/styles.css'].decode() + '</style>')
    html = re.sub(r'^[ \t]*<script src="/app\.js" defer></script>[ \t]*$', '', html, flags=re.M)
    program = runtime.decode() + '\n' + sources['web/app.js'].decode()
    require(not re.search(r'</script[\t\n\f\r />]', program, re.I), 'Trusted current composition closes script')
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c')
    return html.replace('</body>', '<script>window.BENCH_SNAPSHOT=' + payload + ';</script><script>' + program + '</script></body>').encode()


def load_sources(args, report):
    lock = json.loads(args.baseline_lock.read_text())
    require(all(lock['sha256'].get(path) == sha for path, sha in FROZEN_PROGRAMS.items()), 'Independent frozen program lock differs')
    index_sha, manifest_sha = baseline_data_pins(args)
    require(digest(safe_read(args.baseline_site, MANIFEST)) == manifest_sha and
            digest(safe_read(args.baseline_site, 'index.html')) == index_sha, 'Wrong frozen/explicitly locked baseline bundle')
    report['baseline_data_lock'] = {'mode': 'explicit-new-v2' if args.new_v2_baseline_lock else 'frozen-default',
                                    'index_sha256': index_sha, 'manifest_sha256': manifest_sha}
    for relative, expected in lock['sha256'].items():
        require(digest(safe_read(args.trust_root, relative)) == expected, 'Frozen trust bytes differ: ' + relative)
    # Load the separately pinned legacy helper, never accept a changed current
    # helper as the old baseline merely because it can parse the new candidate.
    helper_path = args.trust_root / 'tests/verify_public_snapshot.py'
    spec = importlib.util.spec_from_file_location('gallery_frozen_v2', helper_path)
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    approved = candidate_sources(args, report)
    viewer = safe_read(args.trust_root, 'web/app.js')
    frozen_runtime = safe_read(args.trust_root, 'web/preview-runtime.js')
    runtime = approved['web/preview-runtime.js']
    old_manifest, manifest = checked_manifest(args.baseline_site, 2), checked_manifest(args.site, 3)
    old_html = safe_read(args.baseline_site, 'index.html')
    old_data = frozen.audit_html(old_html.decode(), viewer.decode(), frozen_runtime.decode(), descriptors_only=True)
    old_inline = frozen.audit_html(safe_read(args.baseline_site, 'offline.html').decode(), viewer.decode(), frozen_runtime.decode())
    full_paths = [name for name, info in manifest['files'].items() if info.get('role') == 'viewer']
    require(len(full_paths) == 1, 'Exactly one trusted full viewer required')
    full_path, runtime_path = full_paths[0], 'viewer/' + digest(runtime) + '.js'
    inherited = set(old_manifest['files']) - {'index.html', 'offline.html'}
    require(set(manifest['files']) == inherited | {'index.html', 'offline.html', full_path, runtime_path},
            'Candidate must contain exactly inherited assets, current documents, viewer and runtime')
    for name, meta in old_manifest['files'].items():
        if name not in ('index.html', 'offline.html'):
            require(same(manifest['files'][name], meta) and safe_read(args.site, name) == safe_read(args.baseline_site, name), 'Inherited asset changed: ' + name)
    require(safe_read(args.site, runtime_path) == runtime, 'Candidate preview runtime differs from locked current source')
    full_html = safe_read(args.site, full_path)
    current_data = v2.audit_html(full_html.decode(), approved['web/app.js'].decode(), runtime.decode(), descriptors_only=True)
    require(same({k: value for k, value in current_data.items() if k != 'offline'},
                 {k: value for k, value in old_data.items() if k != 'offline'}), 'NEW snapshot data/tasks/assets/thumbnails/originals/reviews differ from frozen v2')
    offline_html = safe_read(args.site, 'offline.html')
    offline = v2.audit_html(offline_html.decode(), approved['web/app.js'].decode(), runtime.decode())
    require(same(offline, old_inline), 'Offline data or embedded media/original bytes changed')
    require(offline_html == current_document(old_inline, approved, runtime), 'Offline code/template/styles are not the pinned current composition')
    expected_data = {**old_data, 'offline': {'path': 'offline.html', 'sha256': digest(offline_html), 'size': len(offline_html)}}
    require(same(current_data, expected_data) and full_html == current_document(expected_data, approved, runtime),
            'Full viewer is not the exact current composition with versioned offline descriptor')
    require(len(old_data['runs']) == args.expected_runs and sum(r.get('purpose') == 'benchmark' for r in old_data['runs']) == args.expected_benchmark
            and sum(len(r.get('reviews') or []) for r in old_data['runs']) == args.expected_reviews, 'Frozen census differs')
    html = safe_read(args.site, 'index.html')
    require(same(manifest['stats'], {**old_manifest['stats'], 'index_bytes': len(html), 'offline_bytes': len(offline_html)}), 'Inherited manifest statistics changed')
    full_info = {'path': full_path, 'sha256': digest(full_html), 'size': len(full_html)}
    runtime_info = {'path': runtime_path, 'sha256': digest(runtime), 'size': len(runtime)}
    seed = audit_gallery(html.decode(), current_data, approved['web/public-gallery.js'].decode(), approved['web/public-gallery.css'].decode(),
                         full_info, runtime_info, runtime + b'\n' + approved['web/app.js'], score_source=args.score_source)
    require(len(html) <= ENTRY_DECODED_BUDGET and len(gzip.compress(html, compresslevel=6, mtime=0)) <= ENTRY_GZIP_BUDGET, 'Initial HTML decoded/gzip budget exceeded')
    report['audit'] = {'baseline_html_sha256': digest(old_html), 'full_html_sha256': digest(full_html), 'runtime_sha256': digest(runtime),
                       'frozen_runtime_sha256': digest(frozen_runtime), 'current_runtime_sha256': digest(runtime),
                       'frozen_v2_helper_sha256': digest(helper_path.read_bytes()), 'current_v2_helper_sha256': digest(approved['tests/verify_public_snapshot.py']),
                       'current_html_sha256': digest(html), 'decoded_html_bytes': len(html), 'gzip_html_bytes': len(gzip.compress(html, compresslevel=6, mtime=0)),
                       'exact_data_equality_including_offline': True, 'run_ids': [r['id'] for r in seed['runs']], 'counts': seed['counts'],
                       'candidate_files': len(manifest['files']), 'baseline_files': len(old_manifest['files'])}
    return {'seed': seed, 'data': current_data, 'baseline_data': old_data, 'manifest': manifest,
            'baseline_manifest': old_manifest, 'frozen_runtime': frozen_runtime}


def accepts_gzip(header):
    preferences = {}
    for entry in header.lower().split(','):
        coding, *parameters = entry.split(';')
        coding = coding.strip()
        if coding not in ('gzip', '*'):
            continue
        quality = 1.0
        for parameter in parameters:
            key, separator, value = parameter.strip().partition('=')
            if key == 'q':
                quality = float(value) if separator and re.fullmatch(r'(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)', value) else 0.0
        preferences[coding] = min(preferences.get(coding, quality), quality)
    return preferences.get('gzip', preferences.get('*', 0.0)) > 0


@contextmanager
def serve_gallery(root, manifest, *, delays=None, fail_once=None):
    """Real bounded static HTTP. Delays/one-shot 503s are server-side lifecycle probes.

    HTML, SDK JavaScript and original JSON gzip bytes are prepared outside the timed request.
    Every request is rehashed; no route, abort, or browser API substitution.
    """
    delays, failures = dict(delays or {}), set(fail_once or ())
    payloads = {name: safe_read(root, name) for name in manifest['files']}
    encoded = {name: gzip.compress(raw, compresslevel=6, mtime=0) for name, raw in payloads.items()
               if manifest['files'][name]['mime'].split(';', 1)[0] in
               ('text/html', 'text/javascript', 'application/javascript', 'application/json') and name != 'offline.html'}
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
            body = encoded.get(name) if accepts_gzip(self.headers.get('Accept-Encoding', '')) else None
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


def validate_requests(requests, url, manifest, seed=None, *, cold=False, viewport=None, allow_preview=True, preview_ids=()):
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
        limit = PAGE_SIZE if viewport == 'desktop' else PHONE_PAGE_SIZE
        require(len(set(preview_ids)) <= limit, 'Too many initial inline preview intents')
        rows = {r.get('id'): r for r in seed['runs']}
        packages = set()
        for run_id in preview_ids:
            row = rows.get(run_id)
            require(row is not None and row.get('original', {}).get('status') in ('ready', 'missing_dependencies'),
                    'Initial preview is not an eligible concrete card')
            packages.add(row['original']['package']['path'])
        runtime = seed.get('runtime', {}).get('path') if packages else None
        allowed = {'index.html'} | thumbs | packages | ({runtime} if runtime else set())
        require(all(name in allowed for name in names), 'Cold full-viewer/video/full-image/nonvisible-original request')
        require(runtime is None or names.count(runtime) <= 1, 'Initial runtime fetched more than once')
        require(all(names.count(path) <= 1 for path in packages), 'Initial original fetched more than once')
        fetched = [name for name in names if name in thumbs]
        require(1 <= len(fetched) <= limit, 'Actual initial thumbnail request budget differs')
    return names


# Reuse the old longtask observer, but replace its image timestamp with an explicit
# decoded, visible, content-addressed thumbnail. Never count SVG/data placeholders.
GALLERY_OBSERVER = v2.PERFORMANCE_OBSERVER + """
(() => { window.__galleryMetrics={readable:null,image:null,inline:null,taskResponse:null};
 const visible=e=>{if(!e||!e.getClientRects().length)return false;let r=e.getBoundingClientRect();
   for(let n=e;n;n=n.parentElement){let s=getComputedStyle(n);if(s.visibility==='hidden'||s.display==='none'||Number(s.opacity)===0)return false;}
   return r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0;};
 const tick=()=>{let m=window.__galleryMetrics;
   if(m.readable===null&&document.documentElement.dataset.galleryReady==='true'&&
      visible(document.querySelector('#public-task-tabs button[data-task-id]'))&&
      visible(document.querySelector('#public-purpose'))&&!document.querySelector('#public-purpose').disabled&&
      visible(document.querySelector('#public-models>summary'))&&
      visible(document.querySelector('#public-grid .evidence-card[data-run-id]')))m.readable=performance.now();
   if(m.image===null)for(const i of document.querySelectorAll('#public-grid .work-image')){
     let row=window.BENCH_GALLERY?.runs.find(r=>r.id===i.closest('[data-run-id]')?.dataset.runId);
     if(!row?.image||!i.complete||i.naturalWidth===0||!visible(i)||i.__decodePending)continue;
     if(i.currentSrc!==new URL(row.image.thumb,location.href).href)continue;
     i.__decodePending=true;i.decode().then(()=>requestAnimationFrame(()=>{
       if(m.image===null&&visible(i)&&i.complete&&i.naturalWidth>0)m.image={ms:performance.now(),src:i.currentSrc,run_id:row.id};
     })).catch(e=>{i.__decodeError=String(e);});
   }
   if(m.inline===null)for(const cover of document.querySelectorAll('#public-grid .work-cover[data-preview-state="loaded"]')){
     if(visible(cover)&&cover.querySelector('iframe')){m.inline={ms:performance.now(),run_id:cover.closest('[data-run-id]').dataset.runId};break;}
   }
   requestAnimationFrame(tick);};requestAnimationFrame(tick);
})();
"""


def two_frames(page):
    return page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(performance.now()))))')


def gallery_sample(browser, url, sources, args, profile_name, viewport, repeat):
    profile = PROFILES[profile_name]
    context = browser.new_context(viewport=VIEWPORTS[viewport], color_scheme='light', service_workers='allow')
    context.set_default_timeout(args.timeout_ms)
    requests, errors = [], []
    try:
        context.add_init_script(GALLERY_OBSERVER)
        page = context.new_page()
        page.on('request', lambda r: requests.append({'url': r.url, 'method': r.method, 'type': r.resource_type}))
        errors = observe_browser_errors(context, page, args)
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
        preview_ids = page.locator('#public-grid .work-cover').evaluate_all('''covers=>covers.filter(e=>{
          const r=e.getBoundingClientRect();return r.bottom> -80&&r.top<innerHeight+80&&r.width>0;
        }).map(e=>e.closest('[data-run-id]').dataset.runId)''')
        preview_ids = [run_id for run_id in preview_ids if next(r for r in sources['seed']['runs'] if r['id'] == run_id)['original']['status'] in ('ready', 'missing_dependencies')]
        if preview_ids:
            page.wait_for_function('window.__galleryMetrics.inline!==null')
        names = validate_requests(cold, url, sources['manifest'], sources['seed'], cold=True,
                                  viewport=viewport, preview_ids=preview_ids)
        metrics = page.evaluate('''()=>{let n=performance.getEntriesByType('navigation')[0],m=window.__galleryMetrics;
          return {ttfb_ms:n.responseStart,server_ttfb_ms:n.responseStart-n.requestStart,readable_ms:m.readable,
            first_actually_visible_image_ms:m.image.ms,first_visible_image:m.image,
            first_inline_original_ms:m.inline?.ms??null,first_inline_original:m.inline,
            dom_nodes:document.querySelectorAll('*').length,
            longtasks:window.__loadMetrics.longtasks,document_encoded_bytes:n.encodedBodySize,document_decoded_bytes:n.decodedBodySize,
            transfer_bytes:n.transferSize+performance.getEntriesByType('resource').reduce((s,r)=>s+r.transferSize,0),
            automatic_preview_transfer_bytes:performance.getEntriesByType('resource').filter(r=>/\\/(originals|viewer)\\//.test(new URL(r.name).pathname)).reduce((s,r)=>s+r.transferSize,0),
            first_controls:['#public-task-tabs button[data-task-id]','#public-purpose','#public-models>summary','#public-grid .evidence-card[data-run-id]'].map(s=>{
              let e=document.querySelector(s),r=e.getBoundingClientRect();return {selector:s,label:e.textContent.slice(0,100),top:r.top,bottom:r.bottom,disabled:!!e.disabled};}),
            scroll_x:scrollX,scroll_y:scrollY};}''')
        require(metrics['document_encoded_bytes'] <= ENTRY_GZIP_BUDGET and metrics['document_decoded_bytes'] <= ENTRY_DECODED_BUDGET,
                'Measured cold wire/decoded document budget')
        require(metrics['scroll_x'] == 0 and metrics['scroll_y'] == 0, 'First image was not in unscrolled initial viewport')
        # A real alternative task click must produce that task's actual card set,
        # not only acknowledge a click or render two arbitrary animation frames.
        target = next(t['id'] for t in sources['seed']['tasks'] if t['id'] != sources['seed']['defaults']['task_id'] and
                      any(r['task_id'] == t['id'] and r['purpose'] == 'benchmark' and r['registered_media'] for r in sources['seed']['runs']))
        expected = [r['id'] for r in sources['seed']['runs'] if r['task_id'] == target and r['purpose'] == 'benchmark' and r['registered_media']][:(PAGE_SIZE if viewport == 'desktop' else PHONE_PAGE_SIZE)]
        page.locator(f'#public-task-tabs button[data-task-id="{target}"]').evaluate('''e=>e.addEventListener('click',()=>{
            window.__taskClickAt=performance.now();},{capture:true,once:true})''')
        page.locator(f'#public-task-tabs button[data-task-id="{target}"]').click()
        page.wait_for_function('''ids=>JSON.stringify([...document.querySelectorAll('#public-grid .evidence-card[data-run-id]')].map(e=>e.dataset.runId))===JSON.stringify(ids)''', arg=expected)
        interactive = two_frames(page)
        task_ms = page.evaluate('performance.now()-window.__taskClickAt')
        control = page.locator(f'#public-task-tabs button[data-task-id="{target}"]')
        require(control.get_attribute('aria-selected') == 'true' or control.get_attribute('aria-pressed') == 'true', 'Task control state did not change')
        validate_requests(requests, url, sources['manifest'])
        no_full(requests, sources['seed'])
        require(page.locator('#public-grid .work-preview-host iframe').count() <= len(expected),
                'Task change left extra inline contexts')
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
    context, page, requests, errors = observed_context(browser, url, args, profile=profile, viewport=viewport, baseline=True)
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
        expected = [r['id'] for r in sources['seed']['runs'] if r['task_id'] == target and r['purpose'] == 'benchmark' and r['registered_media']][:(PAGE_SIZE if viewport == 'desktop' else PHONE_PAGE_SIZE)]
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
    for key in ('task_control_response_ms', 'document_encoded_bytes', 'document_decoded_bytes', 'full_viewer_latency_ms', 'primary_original_latency_ms', 'first_inline_original_ms'):
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
    action_row, attempts = select_action_pair(browser, urls['baseline_actions'], sources, args)
    performance['action_pairing'] = {'run_id': action_row['id'], 'preflight': attempts,
                                     'definition': 'One shared subject, selected before all cold measurements in a separate disposable context'}
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
                                                sources, args, name, profile, viewport, action_row))
                    require(sample['primary_original_run_id'] == action_row['id'], 'Paired action subject changed')
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


def catalog_audit_pins(sources, *, baseline=False):
    """Freeze load_sources' verified data/manifest/runtime, before any browser or later disk read."""
    data = sources['baseline_data'] if baseline else sources['data']
    pins = {'mode': 'baseline-frozen-v2' if baseline else 'candidate-v3',
            'manifest': sources['baseline_manifest'] if baseline else sources['manifest'],
            'originals': data['originals'], 'run_ids': [run['id'] for run in data['runs']]}
    if baseline:
        import base64
        frozen = sources['frozen_runtime']
        require(digest(frozen) == FROZEN_PROGRAMS['web/preview-runtime.js'], 'Catalog frozen runtime pin differs')
        pins['frozen_runtime_base64'] = base64.b64encode(frozen).decode()
    else:
        pins['runtime'] = sources['seed']['runtime']
    return json.dumps(pins, sort_keys=True, separators=(',', ':'), allow_nan=False)


class OriginalScriptCatalog:
    """Model source catalog anchored to immutable load_sources audit pins, never a new manifest."""
    def __init__(self, root, audit_pins):
        import base64
        require(isinstance(audit_pins, str), 'Native catalog requires immutable audited source pins')
        pins = json.loads(audit_pins)
        manifest, originals = pins['manifest'], pins['originals']
        require(same(json.loads(safe_read(root, MANIFEST)), manifest), 'Manifest changed after independent audit')
        inventory(root, manifest['files'])
        require(len(set(pins['run_ids'])) == len(pins['run_ids']) and set(originals) == set(pins['run_ids']),
                'Native catalog audited original/run census differs')
        runtime_paths = [path for path, meta in manifest['files'].items() if meta.get('role') == 'runtime']
        if pins['mode'] == 'candidate-v3':
            runtime = pins['runtime']
            require(manifest['version'] == 3 and runtime_paths == [runtime['path']],
                    'Native catalog requires exactly the audited locked v3 runtime')
            meta = manifest['files'][runtime['path']]
            require(meta['sha256'] == runtime['sha256'] == Path(runtime['path']).stem and meta['size'] == runtime['size'],
                    'Native catalog runtime descriptor differs from audited manifest')
            raw = safe_read(root, runtime['path'])
            require(len(raw) == runtime['size'] and digest(raw) == runtime['sha256'], 'Native catalog audited runtime bytes changed')
        else:
            require(pins['mode'] == 'baseline-frozen-v2' and manifest['version'] == 2 and not runtime_paths,
                    'Native catalog baseline must preserve audited v2 inventory')
            raw = base64.b64decode(pins['frozen_runtime_base64'], validate=True)
            require(digest(raw) == FROZEN_PROGRAMS['web/preview-runtime.js'], 'Native catalog frozen runtime bytes differ')
        self.runs, self.script_hashes, self.trusted_script_hashes = {}, set(), {digest(raw)}
        # The pinned SDK serializes this exact trusted function into the model
        # iframe. Even byte-identical model delivery cannot whitelist it.
        diagnostics = re.search(r'(function originalDiagnostics\(\) \{.*?\n  \})\n', raw.decode(), re.S)
        require(diagnostics is not None, 'Native catalog cannot identify trusted SDK diagnostics')
        self.trusted_script_hashes.add(digest(('(' + diagnostics[1] + ')();').encode()))
        package_paths = [descriptor['package']['path'] for descriptor in originals.values() if descriptor.get('package')]
        require(len(package_paths) == len(set(package_paths)) and set(package_paths) ==
                {path for path, meta in manifest['files'].items() if meta.get('role') == 'original'},
                'Native catalog package census differs from audited original descriptors')
        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.current, self.bodies = None, []
            def handle_starttag(self, tag, attrs):
                if tag == 'script' and not dict(attrs).get('src'):
                    self.current = []
            def handle_data(self, text):
                if self.current is not None:
                    self.current.append(text)
            def handle_endtag(self, tag):
                if tag == 'script' and self.current is not None:
                    self.bodies.append(''.join(self.current))
                    self.current = None
        for run_id, descriptor in originals.items():
            if descriptor.get('package') is None:
                continue
            expected = descriptor['package']
            path, meta = expected['path'], manifest['files'][expected['path']]
            require(meta['sha256'] == expected['sha256'] == Path(path).stem and meta['size'] == expected['size'],
                    'Native catalog package differs from audited run descriptor: ' + run_id)
            raw = safe_read(root, path)
            require(len(raw) == expected['size'] and digest(raw) == expected['sha256'],
                    'Native exception audited package bytes changed: ' + path)
            package = json.loads(raw)
            require(package['version'] == 1 and package['run_id'] == run_id and run_id not in self.runs and
                    package['entrypoint'] == descriptor['entrypoint'] and same(package['missing'], descriptor['missing']) and
                    package['files'][package['entrypoint']]['sha256'] == descriptor['entry_sha256'],
                    'Native exception catalog audited run/entry binding differs: ' + run_id)
            sources = {}
            for filename, file in package['files'].items():
                body = base64.b64decode(file['base64'], validate=True)
                require(len(body) == file['size'] and digest(body) == file['sha256'],
                        'Native exception catalog file integrity: ' + filename)
                text = body.decode('utf-8')
                scripts = [text] if file['mime'] == 'text/javascript' else []
                if file['mime'] == 'text/html':
                    parser = Scripts(); parser.feed(text); parser.close()
                    scripts += parser.bodies
                for script in scripts:
                    if script:
                        sources[digest(script.encode('utf-8'))] = {'file': filename, 'file_sha256': file['sha256'],
                            'script_sha256': digest(script.encode('utf-8')), 'source': script}
            self.runs[run_id] = {'package_path': path, 'package_sha256': digest(raw),
                'entry_sha256': package['files'][package['entrypoint']]['sha256'], 'scripts': sources}
            self.script_hashes.update(sources)


def model_exception_proof(context, script, frame, shell, owner, source, catalog):
    """No URL/message heuristics: native default context + two verified SDK owners + exact source."""
    aux = (context or {}).get('auxData', {})
    if not (aux.get('isDefault') is True and aux.get('type') == 'default' and context.get('uniqueId') and
            context.get('origin') in ('://', 'null') and
            script and script.get('executionContextId') == context.get('id') and
            script.get('context_unique_id') == context.get('uniqueId') and frame and
            aux.get('frameId') == frame.get('id') and shell and frame.get('parentId') == shell.get('id') and
            frame.get('session_id') == shell.get('session_id') and owner and
            owner.get('nodeName') == 'IFRAME' and owner.get('sandbox') == 'allow-scripts' and
            owner.get('referrerpolicy') == 'no-referrer' and owner.get('has_srcdoc') is True):
        return None
    binding = shell.get('binding') or {}
    run_id = binding.get('run_id')
    if not (run_id in catalog.runs and binding.get('nodeName') == 'IFRAME' and
            binding.get('sandbox') == 'allow-scripts' and binding.get('referrerpolicy') == 'no-referrer' and
            binding.get('title') == '原作隔离预览 ' + run_id and binding.get('has_srcdoc') is True and
            binding.get('host_direct') is True and binding.get('shell_count') == 1 and
            binding.get('report_frame_id') and binding.get('report_frame_id') == shell.get('parentId') and
            binding.get('host_class') in ('work-preview-host', 'preview-host')):
        return None
    package = catalog.runs[run_id]
    source_hash = digest(source.encode('utf-8')) if isinstance(source, str) else None
    match = package['scripts'].get(source_hash)
    trusted = getattr(catalog, 'trusted_script_hashes', None)
    if not trusted or not match or match['source'] != source or script.get('hash') != source_hash or source_hash in trusted:
        return None
    return {'run_id': run_id, 'package_path': package['package_path'], 'package_sha256': package['package_sha256'],
            'entry_sha256': package['entry_sha256'], 'file': match['file'], 'file_sha256': match['file_sha256'],
            'script_sha256': source_hash, 'context_unique_id': context['uniqueId'],
            'frame_id': frame['id'], 'shell_frame_id': shell['id'], 'session_id': frame['session_id']}


class BrowserErrorLedger(list):
    """List-compatible zero-UI-error assertion; preserve every error in its receipt."""
    def __init__(self):
        super().__init__()
        self.page_errors, self.native_errors, self.model_errors = [], [], []
        self.other_errors, self.ui_errors, self.diagnostics = [], [], []
        self.receipt = {'playwright_pageerrors': self.page_errors, 'native_exceptions': self.native_errors,
                        'model_original_errors': self.model_errors, 'other_errors': self.other_errors,
                        'ui_errors': self.ui_errors, 'observer_diagnostics': self.diagnostics}
    def pageerror(self, error):
        self.page_errors.append({'stack': error.stack or str(error), 'attribution': 'trusted-or-unknown'})
    def append(self, message):
        self.other_errors.append(str(message))
    def refresh(self):
        # Consume exact complete native descriptions one-for-one. A same-text
        # trusted/unknown native exception still fails, and duplicates cannot
        # inherit another event's original attribution.
        available = {}
        for event in self.native_errors:
            available.setdefault(event['description'], []).append(event)
        ui = list(self.other_errors)
        for error in self.page_errors:
            candidates = available.get(error['stack'], [])
            native = candidates.pop(0) if candidates else None
            error['attribution'] = native['attribution'] if native else 'trusted-or-unknown'
            if native and native.get('proof'):
                error['proof'] = native['proof']
            if error['attribution'] != 'model-original':
                ui.append(error['stack'])
        ui += [event['description'] for remaining in available.values() for event in remaining
               if event['attribution'] != 'model-original']
        self.ui_errors[:] = ui
        self.model_errors[:] = [event for event in self.native_errors if event['attribution'] == 'model-original']
        list.clear(self); list.extend(self, ui)
    def __bool__(self):
        self.refresh()
        return list.__len__(self) != 0
    def __iter__(self):
        self.refresh()
        return list.__iter__(self)
    def __len__(self):
        self.refresh()
        return list.__len__(self)


class NativeExceptionObserver:
    """Passive native CDP observation, including opaque OOPIF targets; never pause or intercept."""
    HOST_BINDING = '''function(){const host=this.closest('.work-preview-host,.preview-host');
      const card=host?.closest('.evidence-card[data-run-id],.original-preview[data-original-run]');
      return {run_id:card?.dataset.originalRun||card?.dataset.runId,nodeName:this.nodeName,
        sandbox:this.getAttribute('sandbox'),referrerpolicy:this.getAttribute('referrerpolicy'),
        title:this.getAttribute('title'),has_srcdoc:this.hasAttribute('srcdoc'),
        host_class:host?.classList.contains('work-preview-host')?'work-preview-host':host?.classList.contains('preview-host')?'preview-host':null,
        host_direct:this.parentElement?.parentElement===host,shell_count:host?.querySelectorAll('iframe').length};}'''
    def __init__(self, context, page, catalog, ledger):
        self.catalog, self.ledger = catalog, ledger
        self.session = context.new_cdp_session(page)
        native = getattr(self.session, '_impl_obj', None)
        self._target_sender = native.send if native is not None else None
        self._target_loop = native._loop if native is not None else None
        self._target_tasks = set()
        self._retired_sessions = set()
        self.targets, self.contexts, self.scripts, self.frames, self.sources, self.pending = {}, {}, {}, {}, {}, {}
        self.command_id, self.owner_generation, self.exception_evidence = 0, 0, []
        self.session.on('Target.attachedToTarget', self.attached)
        self.session.on('Target.receivedMessageFromTarget', self.message)
        self.session.on('Target.detachedFromTarget', self.detached)
        self.session.on('Page.frameDetached', lambda e: self.event('', 'Page.frameDetached', e))
        self.session.on('close', lambda *args: self.closed())
        self.session.on('Runtime.executionContextCreated', lambda e: self.event('', 'Runtime.executionContextCreated', e))
        self.session.on('Runtime.exceptionThrown', lambda e: self.event('', 'Runtime.exceptionThrown', e))
        self.session.on('Debugger.scriptParsed', lambda e: self.event('', 'Debugger.scriptParsed', e))
        self.session.on('Page.frameAttached', lambda e: self.event('', 'Page.frameAttached', e))
        self.session.on('Page.frameNavigated', lambda e: self.event('', 'Page.frameNavigated', e))
        for domain in ('Page', 'Runtime', 'Debugger'):
            self.session.send(domain + '.enable')
        self.session.send('Debugger.setSkipAllPauses', {'skip': True})
        self.main_frame_id = self.session.send('Page.getFrameTree')['frameTree']['frame']['id']
        self.session.send('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': False, 'flatten': False})
    def note(self, message):
        self.ledger.diagnostics.append(str(message))
    def owner_stage(self, method, identifier, callback, stage):
        identity = getattr(callback, 'owner_identity', None)
        if identity is not None:
            self.note(f'Native owner chain {method} [sid={identity[0]!r}, frame_id={identity[1]!r}, '
                      f'command_id={identifier}, generation={identity[-1]}, stage={stage}]')
        script = getattr(callback, 'script_identity', None)
        if script is not None:
            self.note(f'Native script capture {method} [sid={script[0]!r}, script_id={script[1]!r}, '
                      f'frame_id={script[2]!r}, command_id={identifier}, stage={stage}]')
    def command_failed(self, sid, identifier, method, callback, error):
        self.pending.pop((sid, identifier), None)
        self.note(f'{method} [sid={sid!r}, command_id={identifier}]: {error}')
        self.owner_stage(method, identifier, callback, 'error: ' + str(error))
    def send(self, sid, method, params=None, callback=None):
        self.command_id += 1
        identifier = self.command_id
        if sid and callback:
            self.pending[(sid, identifier)] = (method, callback)
        self.owner_stage(method, identifier, callback, 'pending')
        if sid in getattr(self, '_retired_sessions', set()):
            self.command_failed(sid, identifier, method, callback, 'Native session already detached')
            return
        try:
            if sid:
                envelope = {'sessionId': sid,
                    'message': json.dumps({'id': identifier, 'method': method, 'params': params or {}})}
                critical = getattr(callback, 'owner_identity', None) is not None or getattr(callback, 'script_identity', None) is not None
                if getattr(self, '_target_sender', None) is not None and not critical:
                    # Domain setup can be pipelined. Evidence reads cannot wait
                    # for a scheduled task: an original's own error handler may
                    # destroy its target before that task sends the request.
                    async def forward():
                        if sid in getattr(self, '_retired_sessions', set()):
                            self.owner_stage(method, identifier, callback, 'forward-skipped-retired')
                            return
                        self.owner_stage(method, identifier, callback, 'forward-started')
                        try:
                            await self._target_sender('Target.sendMessageToTarget', envelope)
                            self.owner_stage(method, identifier, callback, 'forward-ack')
                        except Exception as error:
                            self.command_failed(sid, identifier, method, callback, error)
                    task = self._target_loop.create_task(forward())
                    self._target_tasks.add(task)
                    def completed(task):
                        self._target_tasks.discard(task)
                        if task.cancelled():
                            self.command_failed(sid, identifier, method, callback, 'Native forwarding cancelled')
                    task.add_done_callback(completed)
                else:
                    # Send the native read now, without pausing the debugger or
                    # postponing the product's actual error/teardown lifecycle.
                    self.owner_stage(method, identifier, callback, 'forward-started')
                    self.session.send('Target.sendMessageToTarget', envelope)
                    self.owner_stage(method, identifier, callback, 'forward-ack')
            else:
                result = self.session.send(method, params or {})
        except Exception as error:
            self.command_failed(sid, identifier, method, callback, error)
            return
        if not sid and callback:
            stage = callback(result)
            self.owner_stage(method, identifier, callback, stage)
    def retire_frames(self, identifiers, reason):
        retired = set(identifiers)
        while True:
            children = {fid for fid, frame in self.frames.items() if frame.get('parentId') in retired}
            if children <= retired:
                break
            retired.update(children)
        for fid in retired:
            frame = self.frames.get(fid)
            if frame is not None:
                frame['retired'] = True
                for key in ('owner', 'owner_identity', 'owner_cache_identity', 'owner_attempt'):
                    frame.pop(key, None)
        for (sid, identifier), (method, callback) in list(self.pending.items()):
            owner = getattr(callback, 'owner_identity', ())
            script = getattr(callback, 'script_identity', ())
            if (owner and owner[1] in retired) or (script and script[2] in retired):
                # A frame detach does not cancel an already-dispatched command:
                # Chromium can still answer it, and that answer is genuine
                # evidence about the frame that just went away. Owner reads are
                # identity-checked on arrival and reject themselves if the frame
                # is gone, so only the note is recorded here and the callback is
                # left to complete. The session-detach path below clears anything
                # still outstanding when the channel itself disappears.
                self.owner_stage(method, identifier, callback, reason)
    def detached(self, event):
        sid = event['sessionId']
        if not hasattr(self, '_retired_sessions'):
            self._retired_sessions = set()
        self._retired_sessions.add(sid)
        self.note(f'Native session detached [sid={sid!r}, target_id={event.get("targetId")!r}]')
        self.retire_frames([fid for fid, frame in self.frames.items() if frame.get('session_id') == sid], 'session-detached')
        for key, (method, callback) in list(self.pending.items()):
            if key[0] == sid:
                self.pending.pop(key, None)
                self.owner_stage(method, key[1], callback, 'session-detached')
        self.reclassify()
    def closed(self):
        self.note('Native root CDP session closed')
        self.retire_frames(list(self.frames), 'root-session-closed')
        for (sid, identifier), (method, callback) in list(self.pending.items()):
            self.pending.pop((sid, identifier), None)
            self.owner_stage(method, identifier, callback, 'root-session-closed')
        self.reclassify()
    def attached(self, event):
        sid, info = event['sessionId'], event['targetInfo']
        self.targets[sid] = info
        shell = info.get('type') == 'iframe' and info.get('parentFrameId') == self.main_frame_id
        if shell:
            self.frames.setdefault(info['targetId'], {}).update(
                id=info['targetId'], parentId=self.main_frame_id, session_id=sid)
        # Enable exception/context/script capture before Page discovery: its
        # frame-tree callbacks issue synchronous DOM owner round trips, during
        # which a failing original may disappear before getScriptSource is sent.
        self.send(sid, 'Runtime.enable')
        self.send(sid, 'Debugger.enable')
        self.send(sid, 'Debugger.setSkipAllPauses', {'skip': True})
        self.send(sid, 'Page.enable')
        self.send(sid, 'Page.getFrameTree', callback=lambda result, sid=sid: self.frame_tree(sid, result.get('frameTree')))
        self.send(sid, 'Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': False, 'flatten': False})
        if shell:
            self.reconcile_owner(info['targetId'])
            for fid, frame in list(self.frames.items()):
                if frame.get('parentId') == info['targetId']:
                    self.reconcile_owner(fid)
            if not self.frames[info['targetId']].get('binding'):
                self.bind_shell(sid)
    def frame_tree(self, sid, tree):
        if tree:
            self.event(sid, 'Page.frameNavigated', {'frame': tree['frame']})
            for child in tree.get('childFrames', []):
                self.frame_tree(sid, child)
    def bind_shell(self, sid):
        self.bind_report_owner(self.targets[sid]['targetId'])
    def bind_report_owner(self, fid):
        frame = self.frames[fid]
        if frame.get('binding') or frame.get('binding_inflight'):
            return
        # Synchronous CDP reads can dispatch another frame event reentrantly.
        # Do not start a second owner read or replace already captured evidence.
        frame['binding_inflight'] = True
        try:
            # Discovering/promoting the shell cannot lend its SID to an inner.
            # Reconcile already-native children before any synchronous outer read.
            for child_id, child in list(self.frames.items()):
                if child.get('parentId') == fid:
                    self.reconcile_owner(child_id)
            node = self.session.send('DOM.getFrameOwner', {'frameId': fid})
            remote = self.session.send('DOM.resolveNode', {'backendNodeId': node['backendNodeId']})['object']['objectId']
            try:
                response = self.session.send('Runtime.callFunctionOn', {'objectId': remote,
                    'functionDeclaration': self.HOST_BINDING, 'returnByValue': True})
                binding = response.get('result', {}).get('value')
                if isinstance(binding, dict):
                    binding['report_frame_id'] = self.main_frame_id
                    if not frame.get('binding'):
                        frame['binding'] = binding
            finally:
                self.session.send('Runtime.releaseObject', {'objectId': remote})
        except Exception as error:
            self.note('Shell owner proof unavailable: ' + str(error))
        finally:
            frame.pop('binding_inflight', None)
    def message(self, event):
        sid, message = event['sessionId'], json.loads(event['message'])
        if 'method' in message:
            self.event(sid, message['method'], message.get('params', {}))
        elif (sid, message.get('id')) in self.pending:
            method, callback = self.pending.pop((sid, message['id']))
            self.note(f"Native response {method} [sid={sid!r}, command_id={message['id']}]")
            if 'error' in message:
                self.note(f"Native proof command failed: {method} [sid={sid!r}, command_id={message['id']}]: {message['error']}")
                self.owner_stage(method, message['id'], callback, 'error: ' + str(message['error']))
            else:
                stage = callback(message.get('result', {}))
                self.owner_stage(method, message['id'], callback, stage)
        elif 'id' in message and sid in getattr(self, '_retired_sessions', set()):
            self.note(f"Unmatched native response [sid={sid!r}, command_id={message['id']}, retired=True]")
        self.reclassify()
    def event(self, sid, method, event):
        if method == 'Target.detachedFromTarget':
            self.detached(event)
            return
        if sid in getattr(self, '_retired_sessions', set()):
            return
        if method == 'Page.frameDetached':
            fid, reason = event.get('frameId'), event.get('reason')
            self.note(f'Native frame detached [sid={sid!r}, frame_id={fid!r}, reason={reason!r}]')
            if reason == 'remove' and fid:
                self.retire_frames([fid], 'frame-detached')
        elif method == 'Runtime.executionContextCreated':
            item = event['context']
            self.contexts[(sid, item['id'])] = item
            aux = item.get('auxData', {})
            fid = aux.get('frameId')
            if fid and aux.get('isDefault') is True and aux.get('type') == 'default':
                frame = self.frames.setdefault(fid, {'id': fid, 'session_id': sid})
                if sid:
                    frame['session_id'] = sid
                self.reconcile_owner(fid)
                for child_id, child in list(self.frames.items()):
                    if child.get('parentId') == fid:
                        self.reconcile_owner(child_id)
        elif method in ('Page.frameAttached', 'Page.frameNavigated'):
            if method == 'Page.frameNavigated' and not sid:
                # A cross-renderer report navigation resets Chromium's skip flag.
                self.session.send('Debugger.setSkipAllPauses', {'skip': True})
            item = event.get('frame', event)
            fid = item.get('id') or item.get('frameId')
            if fid:
                previous = self.frames.setdefault(fid, {'id': fid, 'session_id': sid})
                # Only native facts for this inner may promote its SID. Late
                # root discovery must not overwrite child ancestry or lifecycle.
                if sid or not previous.get('session_id'):
                    previous['session_id'] = sid
                    previous.update({key: item[key] for key in ('parentId', 'url', 'loaderId') if key in item})
                    if item.get('parentFrameId'):
                        previous['parentId'] = item['parentFrameId']
                elif not previous.get('parentId'):
                    # A native root event may fill ancestry absent from an early
                    # child context, but cannot replace its SID or known parent.
                    parent = item.get('parentFrameId') or item.get('parentId')
                    if parent:
                        previous['parentId'] = parent
                self.reconcile_owner(fid)
                for child_id, child in list(self.frames.items()):
                    if child.get('parentId') == fid:
                        self.reconcile_owner(child_id)
                if not sid and previous.get('parentId') == self.main_frame_id and not previous.get('binding'):
                    self.bind_report_owner(fid)
        elif method == 'Debugger.scriptParsed':
            item = dict(event)
            context = self.contexts.get((sid, item.get('executionContextId')), {})
            item['context_unique_id'] = context.get('uniqueId')
            self.scripts[(sid, item['scriptId'])] = item
            # Capture a candidate script immediately, before a diagnostic may
            # destroy its frame. Attribution still requires that run's exact
            # source and both native frame-owner proofs below.
            if item.get('hash') in self.catalog.script_hashes:
                def capture(result, key=(sid, item['scriptId'])):
                    source = result.get('scriptSource')
                    self.sources[key] = source
                    return 'captured' if isinstance(source, str) else 'empty'
                capture.script_identity = (sid, item['scriptId'], item.get('executionContextAuxData', {}).get('frameId'))
                self.send(sid, 'Debugger.getScriptSource', {'scriptId': item['scriptId']}, capture)
        elif method == 'Runtime.exceptionThrown':
            details = event['exceptionDetails']
            context = self.contexts.get((sid, details.get('executionContextId')))
            description = details.get('exception', {}).get('description') or details.get('text', 'Unknown native exception')
            record = {'description': description, 'attribution': 'trusted-or-unknown', 'native': event,
                      'session_id': sid, 'context': context}
            self.ledger.native_errors.append(record)
            self.exception_evidence.append(record)
            # The owner reads are normally driven by frame events. A failing
            # original can be torn down before that walk reaches it, so start a
            # read for this exact frame now, while it is still alive. Do this
            # only when no owner has been collected yet: reconcile_owner adopts a
            # fresh identity and would otherwise discard an owner that is already
            # valid for the frame the exception actually came from.
            failed_frame = ((context or {}).get('auxData') or {}).get('frameId')
            known = self.frames.get(failed_frame)
            if known is not None and 'owner' not in known:
                self.reconcile_owner(failed_frame)
        self.reclassify()
    def owner_signature(self, fid):
        frame = self.frames.get(fid, {})
        shell = self.frames.get(frame.get('parentId'), {})
        return (frame.get('session_id'), fid, frame.get('parentId'), frame.get('loaderId'), frame.get('url'),
                shell.get('session_id'), shell.get('parentId'), shell.get('loaderId'), shell.get('url'),
                shell.get('owner_generation'))
    def owner_is_current(self, identity):
        frame = self.frames.get(identity[1], {})
        return not frame.get('retired') and frame.get('owner_identity') == identity and self.owner_signature(identity[1]) == identity[:-1]
    def reconcile_owner(self, fid):
        frame = self.frames[fid]
        if frame.get('retired'):
            return
        signature = self.owner_signature(fid)
        if frame.get('owner_identity', ())[:-1] != signature:
            # Observer-wide generations cannot be reused after frame teardown.
            self.owner_generation = getattr(self, 'owner_generation', 0) + 1
            frame['owner_generation'] = self.owner_generation
            frame['owner_identity'] = (*signature, frame['owner_generation'])
            frame.pop('owner', None)
            frame.pop('owner_cache_identity', None)
        identity = frame['owner_identity']
        sid = frame.get('session_id')
        shell = self.frames.get(frame.get('parentId'), {})
        native_owner = sid is not None and shell.get('session_id') == sid and (bool(sid) or (
            shell.get('parentId') == getattr(self, 'main_frame_id', None)))
        if not native_owner or frame.get('owner_attempt') == identity:
            return
        # Reserve before dispatch: direct root sends and target responses can
        # deliver native events synchronously, including a new generation.
        frame['owner_attempt'] = identity
        if sid and getattr(self, '_target_sender', None) is not None:
            # The shell's native DOM tree contains the inner iframe's frameId
            # and attributes in one reply, before a second owner round trip.
            document = lambda result: self.document_owner(identity, result)
            document.owner_identity = identity
            self.send(sid, 'DOM.getDocument', {'depth': 4, 'pierce': False}, document)
        callback = lambda result: self.owner_node(identity, result)
        callback.owner_identity = identity
        self.send(sid, 'DOM.getFrameOwner', {'frameId': fid}, callback)
    def document_owner(self, identity, result):
        if not self.owner_is_current(identity):
            return 'stale'
        nodes, matches = [result.get('root', {})], []
        while nodes:
            node = nodes.pop()
            if node.get('frameId') == identity[1]:
                matches.append(node)
            nodes.extend(node.get('children', []))
        if len(matches) != 1:
            return 'empty-or-ambiguous'
        return self.cache_owner(identity, {'node': matches[0]})
    def owner_node(self, identity, result):
        if not self.owner_is_current(identity):
            return 'stale'
        if not result.get('backendNodeId'):
            return 'empty'
        callback = lambda response: self.cache_owner(identity, response)
        callback.owner_identity = identity
        self.send(identity[0], 'DOM.describeNode', {'backendNodeId': result['backendNodeId'], 'depth': 0}, callback)
        return 'describe-dispatched'
    def cache_owner(self, identity, response):
        if not self.owner_is_current(identity):
            return 'stale'
        node = response.get('node', {})
        if not node:
            return 'empty'
        attrs = node.get('attributes', [])
        attrs = dict(zip(attrs[::2], attrs[1::2]))
        frame = self.frames[identity[1]]
        frame['owner'] = {'nodeName': node.get('nodeName'), 'sandbox': attrs.get('sandbox'),
            'referrerpolicy': attrs.get('referrerpolicy'), 'has_srcdoc': 'srcdoc' in attrs}
        frame['owner_cache_identity'] = identity
        return 'cached'
    def reclassify(self):
        for record in self.exception_evidence:
            if record.get('proof'):
                continue  # Retain immutable evidence after context/frame teardown.
            details, sid, context = record['native']['exceptionDetails'], record['session_id'], record['context']
            calls = details.get('stackTrace', {}).get('callFrames', [])
            script_id = details.get('scriptId') or (calls[0].get('scriptId') if calls else None)
            script = self.scripts.get((sid, script_id))
            frame = self.frames.get((context or {}).get('auxData', {}).get('frameId'))
            shell = self.frames.get((frame or {}).get('parentId'))
            source = self.sources.get((sid, script_id))
            record['evidence'] = {'script_id': script_id, 'script': script, 'frame': frame, 'shell': shell,
                                  'source_sha256': digest(source.encode('utf-8')) if isinstance(source, str) else None}
            owner = (frame or {}).get('owner')
            if frame and 'owner_identity' in frame and (
                    not self.owner_is_current(frame['owner_identity']) or
                    frame.get('owner_cache_identity') != frame['owner_identity']):
                owner = None
            proof = model_exception_proof(context, script, frame, shell, owner, source, self.catalog) if (
                frame and frame.get('session_id') == sid) else None
            if proof:
                record['evidence'] = json.loads(json.dumps(record['evidence']))
                record.update(attribution='model-original', proof=proof)
        self.ledger.refresh()


def observe_browser_errors(context, page, args, *, baseline=False):
    cache_name = '_baseline_script_catalog' if baseline else '_original_script_catalog'
    pin_name = 'baseline_catalog_pins' if baseline else 'browser_catalog_pins'
    require(hasattr(args, pin_name), 'Native observer requires load_sources audit pins before browsing')
    if not hasattr(args, cache_name):
        root = args.baseline_site if baseline else args.site
        setattr(args, cache_name, OriginalScriptCatalog(root, getattr(args, pin_name)))
    ledger = BrowserErrorLedger()
    page.on('pageerror', ledger.pageerror)
    observer = NativeExceptionObserver(context, page, getattr(args, cache_name), ledger)
    # Keep the observer reachable so a caller that is about to destroy previews
    # can let already-issued native reads finish first.
    ledger.observer = observer
    page.bench_error_ledger = ledger
    receipts = getattr(args, 'browser_error_receipts', None)
    if receipts is not None:
        receipts.append(ledger.receipt)
    return ledger


def settle_observer(ledger, page, timeout_ms=5000):
    """Wait briefly for the observer's own in-flight reads before navigating away.

    Preview frames are destroyed by this verifier's own scrolling, and a native
    answer cannot arrive once the frame is gone. Waiting here keeps genuine
    evidence that has already been requested; the wait is bounded, and anything
    still unanswered is reported as a loss rather than silently accepted.
    """
    observer = getattr(ledger, 'observer', None)
    if observer is None or not getattr(observer, 'pending', None):
        return 0
    deadline = time.monotonic() + timeout_ms / 1000
    while observer.pending and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    return len(observer.pending)


def observed_context(browser, url, args, *, profile=None, viewport='desktop', original=True, baseline=False):
    # Playwright's service_workers='block' injects an access to Navigator.serviceWorker
    # into opaque sandbox documents, producing a verifier-induced SecurityError.
    # Original lifecycle probes use the browser default; isolation remains the
    # unchanged SDK's real sandbox policy, never an interception-based substitute.
    context = browser.new_context(viewport=VIEWPORTS[viewport], color_scheme='light',
                                  service_workers='allow' if original else 'block', reduced_motion='no-preference')
    context.set_default_timeout(args.timeout_ms)
    page = context.new_page()
    requests, errors = [], observe_browser_errors(context, page, args, baseline=baseline)
    # Any navigation destroys running previews, and a native answer cannot arrive
    # once its frame is gone. Settle the observer's own outstanding reads before
    # each navigation so evidence that was already requested is not discarded.
    raw_goto = page.goto
    raw_viewport = page.set_viewport_size

    def settling_goto(*args, **kwargs):
        settle_observer(errors, page)
        return raw_goto(*args, **kwargs)

    def settling_viewport(*args, **kwargs):
        # A viewport change can unmount inline previews through the page's own
        # observer, so let the native reads already in flight finish first.
        settle_observer(errors, page)
        return raw_viewport(*args, **kwargs)

    page.goto = settling_goto
    page.set_viewport_size = settling_viewport
    page.on('request', lambda r: requests.append({'url': r.url, 'method': r.method, 'type': r.resource_type}))
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


def model_menu(page):
    details = page.locator('#public-models')
    require(details.evaluate('e=>e.tagName==="DETAILS"'), 'Model selector is not native keyboard disclosure')
    if not details.evaluate('e=>e.open'):
        details.locator('summary').click()


def reach_public_run(page, row):
    # Paging replaces the rendered cards, which destroys their previews. Let the
    # observer finish any native read it already issued before that happens.
    ledger = getattr(page, 'bench_error_ledger', None)
    if ledger is not None:
        settle_observer(ledger, page)
    previous = page.locator('#public-pages [data-gallery-page]').filter(has_text='上一页')
    while previous.count() and previous.is_enabled():
        if ledger is not None:
            settle_observer(ledger, page)
        previous.click()
    limit = page.evaluate('window.BENCH_GALLERY.runs.length') + 1
    for _ in range(limit):
        if page.locator(f'#public-grid [data-run-id="{row["id"]}"]').count():
            return
        history = page.locator('#public-history')
        if history.is_visible():
            if not history.evaluate('d=>d.open'):
                history.locator('summary').click()
            if page.locator(f'#public-history-list [data-run-id="{row["id"]}"]').count():
                return
        next_page = page.locator('#public-pages [data-gallery-page]').filter(has_text='下一页')
        require(next_page.count() == 1 and next_page.is_enabled(), 'Requested concrete run is not reachable in basic gallery')
        if ledger is not None:
            settle_observer(ledger, page)
        next_page.click()
    require(False, 'Requested run exceeded exact-record bounded pagination')


def open_public_run(page, url, row):
    page.goto(url + '?' + urlencode({'task_id': row['task_id'], 'purpose': row['purpose'], 'model_query': row['model']}), wait_until='networkidle')
    public_ready(page)
    model_menu(page)
    require(page.locator('#public-model').input_value() == row['model'], 'URL model query did not populate the actual filter')
    reach_public_run(page, row)


def click_clock(locator, page):
    locator.evaluate('e=>e.addEventListener("click",()=>{window.__explicitClick=performance.now();},{capture:true,once:true})')
    locator.click()


def select_action_pair(browser, url, sources, args):
    """Probe the frozen runtime once, outside every measured cold context."""
    runnable = ('ready', 'missing_dependencies')
    originals = sources['baseline_data'].get('originals') or {}
    defaults = sources['seed']['defaults']
    candidates = [r for r in decode_score_rows(sources['seed'])
                  if r['task_id'] == defaults['task_id'] and r['purpose'] == 'benchmark'
                  and r['registered_media'] and r['original']['status'] in runnable
                  and (originals.get(r['id']) or {}).get('status') in runnable]
    require(candidates, 'No original candidate runnable in both paired legs')
    context, page, requests, errors = observed_context(browser, url, args, original=True, baseline=True)
    attempts = []
    try:
        for row in candidates:
            require(same(row['original']['package'], originals[row['id']]['package']),
                    'Paired original package differs between legs')
            page.goto(url, wait_until='networkidle', timeout=60000)
            page.wait_for_function('document.querySelector("#workspace")?.getAttribute("aria-busy")==="false"')
            button = page.locator(f'.evidence-card [data-run-original="{row["id"]}"]')
            require(button.count() == 1, 'Paired candidate has no unique frozen original control')
            button.click()
            preview = page.locator(f'#original-content .original-preview[data-original-run="{row["id"]}"]')
            page.wait_for_function('id=>["loaded","error"].includes(document.querySelector(`#original-content .original-preview[data-original-run="${id}"]`)?.dataset.previewState)',
                                   arg=row['id'])
            state = preview.get_attribute('data-preview-state')
            attempts.append({'run_id': row['id'], 'state': state,
                             'refusal': preview.inner_text() if state == 'error' else None})
            require(not errors, 'Action pairing preflight JavaScript errors: ' + '; '.join(errors))
            if state == 'loaded':
                return row, attempts
        require(False, 'No original actually starts in both paired legs')
    finally:
        context.close()


def action_sample(browser, url, sources, args, name, profile, viewport, row):
    """Extra waiting is measured separately; never called fake TTI or cold shell time."""
    context, page, requests, errors = observed_context(browser, url, args, profile=profile, viewport=viewport,
                                                      original=True, baseline=name == 'baseline')
    try:
        # Both legs measure the same preselected original in fresh contexts. Only
        # their actual product controls differ; selection never warms this context.
        page.goto(url, wait_until='networkidle', timeout=60000)
        if name == 'current':
            public_ready(page)
            reach_public_run(page, row)
            # Reaching the card can start neighbouring cards' automatic inline
            # previews. Pause those through the product's own toggle so the timed
            # action is the explicit click, not the page's background previews.
            stop_pending_inline(page)
            page.wait_for_load_state('networkidle')
        else:
            page.wait_for_function('document.querySelector("#workspace")?.getAttribute("aria-busy")==="false"')
        button = page.locator(f'.evidence-card [data-zoom-run="{row["id"]}"]' if name == 'current' else
                              f'.evidence-card [data-run-original="{row["id"]}"]')
        requests_before_action = len(requests)
        click_clock(button, page)
        page.wait_for_function('id=>document.querySelector(`#original-content .original-preview[data-original-run="${id}"]`)?.dataset.previewState==="loaded"', arg=row['id'])
        original_ms = page.evaluate('performance.now()-window.__explicitClick')
        require(page.locator('#original-content iframe').count() == 1, 'Single original context count')
        before_full = list(requests)
        packages = [urlsplit(r['url']).path.lstrip('/') for r in before_full[requests_before_action:] if '/originals/' in r['url']]
        require(packages == [row['original']['package']['path']], 'Single original action fetched other/duplicate package')
        if name == 'current':
            no_full(before_full, sources['seed'])
            validate_requests(before_full, url, sources['manifest'], allow_preview=True)
            script = page.locator(f'script[src$="{sources["seed"]["runtime"]["path"]}"]')
            import base64
            require(script.count() == 1 and script.get_attribute('integrity') == 'sha256-' + base64.b64encode(bytes.fromhex(sources['seed']['runtime']['sha256'])).decode(), 'Single-original SDK SRI differs')
            page.locator('[data-public-close="original-dialog"]').click()
            require(page.locator('#original-content iframe').count() == 0, 'Closing lightweight single original retained modal sandbox')
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


def score_first_layout(page, viewport, seed):
    """Actual geometry, not source CSS assertions or a count of hidden scores."""
    measured = page.evaluate('''()=>({width:innerWidth,overflow:Math.max(document.body.scrollWidth,document.documentElement.scrollWidth),
      first_image_y:document.querySelector('#public-grid .work-image')?.getBoundingClientRect().top??null,
      cards:[...document.querySelectorAll('#public-grid .evidence-card')].map(card=>({id:card.dataset.runId,left:card.getBoundingClientRect().left,
        sheets:[...card.querySelectorAll('.score-sheet')].map(sheet=>({cells:sheet.querySelectorAll('.score-cell').length,
          visible:[...sheet.querySelectorAll('.score-cell')].filter(cell=>cell.getClientRects().length&&getComputedStyle(cell).visibility==='visible'&&cell.getBoundingClientRect().height>0).length})),
        figure_fonts:[...card.querySelectorAll('.score-cell strong')].map(el=>parseFloat(getComputedStyle(el).fontSize)),
        action_heights:[...card.querySelectorAll('.work-actions button')].map(el=>el.getBoundingClientRect().height)}))})''')
    require(measured['overflow'] <= measured['width'], 'Score-first page horizontal overflow')
    require(len(measured['cards']) == (min(PHONE_PAGE_SIZE if viewport == 'phone' else PAGE_SIZE,
                len([r for r in seed['runs'] if r['task_id'] == seed['defaults']['task_id'] and r['purpose'] == seed['defaults']['purpose'] and r['registered_media']]))), 'Initial 2/1 cards differ')
    for card in measured['cards']:
        require(card['sheets'] and all(s['cells'] == 6 and s['visible'] == 6 for s in card['sheets']), 'Six score cells not visible')
        require(all(22 <= size <= 24 for size in card['figure_fonts']), 'Score figures not 22–24px')
        require(len(card['action_heights']) == 4 and min(card['action_heights']) >= 44, 'Work actions below 44px')
    if viewport == 'phone':
        require(all(abs(card['left'] - 16) <= .5 for card in measured['cards']), 'Phone gutter differs from 16px')
        if seed['defaults']['score_source'] != '*':
            require(measured['first_image_y'] is not None and measured['first_image_y'] <= 300, 'Default phone first real image below y=300')
    return measured


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
                for index in range(max(1, (len(media) + PAGE_SIZE - 1)//PAGE_SIZE)):
                    if index:
                        settle_observer(errors, page)
                        page.locator(f'#public-pages [data-gallery-page="{index}"]').click()
                    expected = media[index*PAGE_SIZE:(index+1)*PAGE_SIZE]
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
        open_public_run(page, url, next(r for r in seed['runs'] if r['id'] == latest_for_triple['id']))
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
        # A runnable original is required for the inline lifecycle checks below, but
        # it is not guaranteed to be the first default-task row: a newer delivery
        # may be an unsupported or screenshot-only original. Fall back to any other
        # runnable card before giving up, and name the condition when none exists.
        runnable = ('ready', 'missing_dependencies')
        first = next((r for r in default if r['original']['status'] in runnable), None)
        if first is None:
            first = next((r for r in seed['runs'] if r['purpose'] == 'benchmark'
                          and r['registered_media'] and r['original']['status'] in runnable), None)
        require(first is not None, 'No runnable original is reachable from the public gallery')
        # Detail handoff replaced the light gallery DOM with the full viewer
        # and fetched the full document. Reuse the same page request ledger,
        # hard-navigate back to the light shell, and require that later
        # lightweight interactions add no further full-viewer requests.
        full_before_zoom = sum(1 for r in requests if urlsplit(r['url']).path.endswith('/' + seed['full']['path']))
        page.goto(url, wait_until='networkidle')
        public_ready(page)
        model_menu(page)
        page.locator('#public-model').fill(first['model'])
        # A model query deliberately promotes even no-media rows into cards.
        expected = [r['id'] for r in seed['runs'] if r['task_id'] == first['task_id'] and
                    r['purpose'] == 'benchmark' and first['model'].lower() in r['model'].lower()][:PAGE_SIZE]
        page.wait_for_function('ids=>JSON.stringify([...document.querySelectorAll("#public-grid .evidence-card[data-run-id]")].map(e=>e.dataset.runId))===JSON.stringify(ids)', arg=expected)
        reach_public_run(page, first)
        page.locator(f'[data-zoom-run="{first["id"]}"]').click()
        modal = wait_public_loaded(page, first['id'])
        toggle = page.locator(f'#original-content [data-preview-start="{first["id"]}"]')
        require(toggle.count() == 1 and page.locator('#original-content .preview-controls button').count() == 1,
                'Lightweight modal must have one start/stop toggle')
        require(not page.locator('#public-zoom').is_visible(), 'Runnable zoom opened historical screenshot instead of original')
        toggle.click()
        wait_public_stopped(page, first['id'], modal)
        toggle.click()
        modal = wait_public_loaded(page, first['id'])
        page.locator('[data-public-close="original-dialog"]').click()
        page.wait_for_function('!document.querySelector("#original-dialog").open')
        require(modal.is_detached() and page.locator('#original-content iframe').count() == 0,
                'Closing lightweight modal retained its sandbox')
        full_after_zoom = sum(1 for r in requests if urlsplit(r['url']).path.endswith('/' + seed['full']['path']))
        require(full_after_zoom == full_before_zoom, 'Basic gallery interaction fetched full viewer')
        checks['checks'].append('model-filter exact IDs, runnable zoom, modal single-toggle stop/restart and detached close')
        zoom_states = Counter()
        for r in seed['runs']:
            runnable = r['original']['status'] in ('ready', 'missing_dependencies')
            open_public_run(page, url, r)
            zoom = page.locator(f'[data-zoom-run="{r["id"]}"]')
            require(zoom.is_disabled() == (not runnable and not r['image']), 'Original/screenshot zoom enabled state differs')
            require(page.locator(f'[data-run-original="{r["id"]}"]').is_disabled() == (not runnable),
                    'Original availability enabled state differs')
            if runnable:
                zoom_states['original'] += 1
            elif r['image']:
                zoom.click()
                page.wait_for_function('document.querySelector("#public-zoom-image").complete&&document.querySelector("#public-zoom-image").naturalWidth>0')
                require(page.locator('#public-zoom-image').get_attribute('src') == r['image']['full'], 'Screenshot fallback mapping differs')
                require(not page.locator('#original-dialog').is_visible(), 'Unavailable original opened executable modal')
                page.locator('[data-public-close="public-zoom"]').click()
                zoom_states['screenshot'] += 1
            else:
                zoom_states['disabled'] += 1
        checks['zoom_states'] = dict(zoom_states)
        checks['checks'].append('all latest rows: runnable original zoom, unavailable screenshot fallback, neither disabled')

        # Exercise automatic inline execution, explicit pause/restart, offscreen
        # cleanup and both render-replacement paths against actual SDK frames.
        open_public_run(page, url, first)
        inline = wait_public_loaded(page, first['id'], inline=True)
        run_toggle = page.locator(f'#public-grid [data-run-original="{first["id"]}"]')
        require(not page.locator('#original-dialog').is_visible(), 'Automatic inline preview opened a modal')
        run_toggle.click()
        wait_public_stopped(page, first['id'], inline, inline=True)
        # Prove rendered-frame stability of this stopped card. Other visible
        # originals have independent lifecycles; whole-page network quiescence
        # is not the explicit-stop condition.
        two_frames(page)
        require(public_preview_panel(page, first['id'], inline=True).get_attribute('data-preview-state') == 'stopped',
                'Explicit inline stop automatically resumed')
        run_toggle.click()
        inline = wait_public_loaded(page, first['id'], inline=True)
        # A short real viewport places the cover below the observer's 80px
        # margin without inventing DOM content or changing intersection APIs.
        page.set_viewport_size({'width': VIEWPORTS['desktop']['width'], 'height': 100})
        page.evaluate('window.scrollTo(0,0)')
        cover = public_preview_panel(page, first['id'], inline=True)
        require(cover.evaluate('e=>{let r=e.getBoundingClientRect();return r.bottom < -80||r.top > innerHeight+80;}'),
                'Offscreen lifecycle probe did not leave preview observer margin')
        wait_public_stopped(page, first['id'], inline, inline=True)
        page.set_viewport_size(VIEWPORTS['desktop'])
        cover.scroll_into_view_if_needed()
        inline = wait_public_loaded(page, first['id'], inline=True)
        model_menu(page)
        page.locator('#public-model').fill('does-not-match-any-real-model')
        page.wait_for_function('document.querySelectorAll("#public-grid .evidence-card").length===0')
        require(inline.is_detached() and page.locator('#public-grid iframe').count() == 0, 'Model filter retained removed inline sandbox')
        page.locator('#public-model').fill('')
        page.locator(f'#public-task-tabs [data-task-id="{first["task_id"]}"]').click()
        reach_public_run(page, first)
        inline = wait_public_loaded(page, first['id'], inline=True)
        next_page = page.locator('#public-pages [data-gallery-page]').filter(has_text='下一页')
        previous = page.locator('#public-pages [data-gallery-page]').filter(has_text='上一页')
        replacement = next_page if next_page.count() and next_page.is_enabled() else previous
        require(replacement.count() == 1 and replacement.is_enabled(), 'Need another actual page for inline teardown proof')
        old_ids = public_ids(page)
        settle_observer(errors, page)
        replacement.click()
        page.wait_for_function('ids=>JSON.stringify([...document.querySelectorAll("#public-grid .evidence-card")].map(e=>e.dataset.runId))!==JSON.stringify(ids)', arg=old_ids)
        require(inline.is_detached(), 'Pagination retained previous page inline sandbox')
        checks['checks'].append('inline automatic start, explicit stop/restart, offscreen teardown/resume, filter and pagination cleanup')

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
                checks.setdefault('score_layout', []).append({'viewport': viewport, 'theme': theme, **score_first_layout(page, viewport, seed)})
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
        reach_public_run(page, compatible[0])
        page.locator(f'[data-select-run="{compatible[0]["id"]}"]').click()
        other = next(r for r in seed['runs'] if r['task_id'] != compatible[0]['task_id'] and r['purpose'] == 'benchmark' and r['registered_media'])
        page.locator(f'#public-task-tabs [data-task-id="{other["task_id"]}"]').click()
        reach_public_run(page, other)
        page.locator(f'[data-select-run="{other["id"]}"]').click()
        require(page.locator(f'[data-select-run="{other["id"]}"]').get_attribute('aria-pressed') == 'false' and
                page.locator('#public-notice').is_visible(), 'Cross-task selection bypassed compatibility restriction')
        page.locator(f'#public-task-tabs [data-task-id="{compatible[0]["task_id"]}"]').click()
        reach_public_run(page, compatible[0])
        require(page.locator(f'[data-select-run="{compatible[0]["id"]}"]').get_attribute('aria-pressed') == 'true', 'Task filter discarded first selection')
        reach_public_run(page, compatible[1])
        page.locator(f'[data-select-run="{compatible[1]["id"]}"]').click()
        require(page.locator('#public-selection-label').inner_text().find('2') >= 0, 'Two selections not retained')
        require(page.locator('#public-compare').is_disabled(), 'Cross-condition comparison did not require explicit confirmation')
        page.locator('#public-mixed').check()
        require(page.locator('#public-compare').is_enabled(), 'Explicit same-task comparison confirmation unavailable')
        model_menu(page)
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


def observe_original_transfers(page):
    """Read-only native request outcomes; completed prefetches are not called aborted."""
    transfers, active = [], {}
    def started(request):
        path = unquote(urlsplit(request.url).path).lstrip('/')
        if path.startswith('originals/'):
            row = {'request_id': len(transfers), 'path': path, 'outcome': 'pending', 'failure': None}
            transfers.append(row)
            active[request] = row
    def finished(request):
        if request in active:
            active[request]['outcome'] = 'finished'
    def failed(request):
        if request in active:
            active[request].update(outcome='failed', failure=request.failure)
    page.on('request', started)
    page.on('requestfinished', finished)
    page.on('requestfailed', failed)
    return transfers


def original_intent_counts(seed, run_ids):
    counts = {}
    for run_id in run_ids:
        run = next(row for row in seed['runs'] if row['id'] == run_id)
        descriptor = run['original']
        require(descriptor['status'] in ('ready', 'missing_dependencies') and descriptor.get('package'),
                'Package prefetch intent is not eligible')
        path = descriptor['package']['path']
        counts[path] = counts.get(path, 0) + 1
    return counts


def check_cancelled_prefetch(transfers, cancelled_count, allowed_counts, *, must_abort=()):
    require(len(transfers) == cancelled_count, 'Cancelled intent started a late/extra package request')
    paths = [row['path'] for row in transfers]
    require(all(path in allowed_counts and paths.count(path) <= allowed_counts[path] for path in paths),
            'Package request is not bound one-per-eligible intent')
    require(all(row['outcome'] == 'finished' or
                (row['outcome'] == 'failed' and row['failure'] == 'net::ERR_ABORTED') for row in transfers),
            'Cancelled prefetch lacks a completed or native-aborted outcome')
    require(all(0 <= index < len(transfers) and transfers[index]['outcome'] == 'failed' and
                transfers[index]['failure'] == 'net::ERR_ABORTED' for index in must_abort),
            'Real delayed in-flight package was not natively aborted')
    return json.loads(json.dumps(transfers))


def delayed_lifecycle(browser, sources, args, report):
    """Real delayed SDK/full-document and real HTTP503 retry; no interception."""
    seed = sources['seed']
    row = next(r for r in seed['runs'] if r['task_id'] == seed['defaults']['task_id'] and r['purpose'] == 'benchmark'
               and r['registered_media'] and r['original']['status'] == 'ready')
    results = report['lifecycle'] = []
    with serve_gallery(args.site, sources['manifest'], delays={seed['runtime']['path']: 2, seed['full']['path']: 1}) as (url, server_requests):
        context, page, requests, errors = observed_context(browser, url, args)
        transfers = observe_original_transfers(page)
        try:
            page.goto(url, wait_until='domcontentloaded')
            public_ready(page)
            page.wait_for_function('document.querySelector("#public-grid .work-cover[data-preview-state=loading]")!==null')
            paused = stop_pending_inline(page)
            require(paused, 'Delayed SDK probe had no automatic inline loading intent')
            require(page.evaluate('typeof window.BenchPreview === "undefined"'), 'SDK delay elapsed before cancellation coverage')
            phase_frames = []
            capture_frame = lambda frame: phase_frames.append(frame)
            page.on('frameattached', capture_frame)
            with page.expect_request(lambda request: urlsplit(request.url).path.endswith('/' + row['original']['package']['path'])):
                page.locator(f'[data-zoom-run="{row["id"]}"]').click(no_wait_after=True)
            page.wait_for_selector('#original-dialog[open]')
            page.wait_for_function('s=>document.querySelector(s)?.dataset.previewState==="loading"', arg=public_preview_selector(row['id']))
            cancelled_count = len(transfers)
            page.locator('[data-public-close="original-dialog"]').click()
            page.wait_for_load_state('networkidle')
            require(any(r['path'] == seed['runtime']['path'] for r in server_requests), 'Delayed SDK probe never reached actual HTTP server')
            require(page.locator('#original-content iframe').count() == 0 and not page.locator('#original-dialog').is_visible(), 'Cancelled delayed SDK intent started late')
            cancelled = check_cancelled_prefetch(transfers, cancelled_count, original_intent_counts(seed, paused + [row['id']]))
            require(not phase_frames, 'Cancelled SDK intent executed a transient/late iframe')
            require(page.evaluate('typeof window.BenchPreview?.create === "function"'), 'Delayed SDK was not actually released')
            for run_id in paused:
                wait_public_stopped(page, run_id, inline=True)
            validate_requests(requests, url, sources['manifest'], allow_preview=True)
            no_full(requests, seed)
            page.remove_listener('frameattached', capture_frame)
            results.append({'check': 'real delayed SDK cancellation permits only intent-bound prefetch; no stale frames/packages',
                            'pass': True, 'paused_ids': paused, 'cancelled_prefetch': cancelled, 'cancelled_phase_frames': len(phase_frames)})
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
            before = wait_inline_registered(page, seed['runs'])
            page.locator('[data-full-view="evidence"]').first.click()
            page.wait_for_load_state('networkidle')
            require(page.locator('#public-grid').is_visible() and public_ids(page), 'Failed full request erased current gallery')
            require(all(frame.is_detached() for _, frame in before), 'Full handoff retained old inline sandbox')
            require(page.evaluate('document.documentElement.dataset.fullViewerReady') != 'true', 'HTTP503 incorrectly marked full ready')
            restored = wait_inline_registered(page, seed['runs'])
            require([run_id for run_id, _ in restored] == [run_id for run_id, _ in before],
                    'HTTP503 recovery changed registered inline run IDs')
            # Exercise the restored registration, not merely its existing host.
            restored_id, restored_frame = restored[0]
            restored_toggle = page.locator(f'#public-grid [data-run-original="{restored_id}"]')
            restored_toggle.click()
            wait_public_stopped(page, restored_id, restored_frame, inline=True)
            restored_toggle.click()
            wait_public_loaded(page, restored_id, inline=True)
            retry = page.get_by_role('button', name=re.compile('重试|Retry', re.I))
            require(retry.count() == 1, 'Full-view failure lacks one explicit retry control')
            retry.click()
            full_ready(page)
            require(len([r for r in requests if r['url'].endswith(seed['full']['path'])]) == 2, 'Real HTTP503 retry request count differs')
            require(not errors, 'Retry page errors: ' + '; '.join(errors))
            results.append({'check': 'real full-document HTTP503 restores inline registrations/toggles before explicit retry',
                            'pass': True, 'restored_inline_ids': [run_id for run_id, _ in restored], 'requests': requests})
        finally:
            context.close()

    for retry_mode in (False, True):
        package_path = row['original']['package']['path']
        delays = {seed['runtime']['path']: 2}
        if not retry_mode:
            delays[package_path] = 2  # Real in-flight cancellation; runs concurrently with the existing SDK delay.
        with serve_gallery(args.site, sources['manifest'], delays=delays,
                           fail_once=[seed['runtime']['path']] if retry_mode else []) as (url, server_requests):
            context, page, requests, errors = observed_context(browser, url, args, original=True)
            transfers = observe_original_transfers(page)
            try:
                page.goto(url, wait_until='domcontentloaded')
                public_ready(page)
                page.wait_for_function('document.querySelector("#public-grid .work-cover[data-preview-state=loading]")!==null')
                paused = stop_pending_inline(page)
                require(paused, 'SDK probe lacked inline intent to pause')
                require(page.evaluate('typeof window.BenchPreview === "undefined"'), 'SDK delay elapsed before cancellation coverage')
                phase_frames = []
                capture_frame = lambda frame: phase_frames.append(frame)
                page.on('frameattached', capture_frame)
                modal_start = len(transfers)
                with page.expect_request(lambda request: urlsplit(request.url).path.endswith('/' + package_path)):
                    page.locator(f'[data-zoom-run="{row["id"]}"]').click(no_wait_after=True)
                require(len(transfers) == modal_start + 1 and transfers[-1]['path'] == package_path,
                        'Modal prefetch was not exactly one selected eligible package')
                panel = public_preview_panel(page, row['id'])
                toggle = panel.locator(f'[data-preview-start="{row["id"]}"]')
                require(toggle.count() == 1 and panel.locator('.preview-controls button').count() == 1,
                        'SDK probe requires the actual one-button modal')
                cancelled_count = len(transfers)
                allowed_counts = original_intent_counts(seed, paused + [row['id']])
                if retry_mode:
                    page.wait_for_function('s=>document.querySelector(s)?.dataset.previewState==="error"', arg=public_preview_selector(row['id']))
                    page.wait_for_load_state('networkidle')
                    require(panel.locator('iframe').count() == 0, 'Failed SDK fetch left executable modal frame')
                    cancelled = check_cancelled_prefetch(transfers, cancelled_count, allowed_counts)
                else:
                    page.wait_for_function('s=>document.querySelector(s)?.dataset.previewState==="loading"', arg=public_preview_selector(row['id']))
                    require(transfers[modal_start]['outcome'] == 'pending', 'Delayed package completed before actual stop coverage')
                    require(sum(r['path'] == package_path for r in server_requests) >=
                            sum(r['path'] == package_path for r in transfers), 'Delayed package did not reach the actual server')
                    toggle.click()
                    wait_public_stopped(page, row['id'])
                    # SDK release must not restart a cancelled intent. Package
                    # prefetch may have completed, but this deliberately delayed
                    # modal request must show native cancellation.
                    page.wait_for_load_state('networkidle')
                    wait_public_stopped(page, row['id'])
                    cancelled = check_cancelled_prefetch(transfers, cancelled_count, allowed_counts, must_abort=[modal_start])
                    require(page.evaluate('typeof window.BenchPreview?.create === "function"'), 'Delayed SDK was not actually released')
                    require(panel.locator('iframe').count() == 0, 'Stop during SDK loading created a late frame')
                require(not phase_frames, 'Cancelled/failed SDK intent executed a transient/late iframe')
                for run_id in paused:
                    wait_public_stopped(page, run_id, inline=True)
                require(toggle.inner_text() == '运行原作', 'Cancelled SDK toggle retained loading/running state')
                page.remove_listener('frameattached', capture_frame)
                restart_at = len(transfers)
                toggle.click()
                frame = wait_public_loaded(page, row['id'])
                require([r['path'] for r in transfers[restart_at:]] == [package_path],
                        'Explicit restart did not fetch exactly one new selected package')
                paths = validate_requests(requests, url, sources['manifest'], allow_preview=True)
                require(paths.count(seed['runtime']['path']) == (2 if retry_mode else 1), 'SDK duplicate/retry HTTP request count')
                require(sum(r['path'] == seed['runtime']['path'] for r in server_requests) == (2 if retry_mode else 1),
                        'SDK request count not confirmed by actual server')
                expected_packages = [transfer['path'] for transfer in transfers]
                require(paths.count(package_path) == sum(transfer['path'] == package_path for transfer in cancelled) + 1
                        and panel.locator('iframe').count() == 1, 'Latest original intent launched duplicate/wrong package')
                require([p for p in paths if p.startswith('originals/')] == expected_packages,
                        'Original prefetch traffic was omitted or changed between native observers')
                require(sum(r['path'] == package_path for r in server_requests) == paths.count(package_path),
                        'Selected package request count not confirmed by actual server')
                no_full(requests, seed)
                toggle.click()
                wait_public_stopped(page, row['id'], frame)
                page.locator('[data-public-close="original-dialog"]').click()
                require(frame.is_detached() and page.locator('#original-content iframe').count() == 0, 'SDK probe close retained modal frame')
                require(not errors, 'SDK lifecycle errors: ' + '; '.join(errors))
                results.append({'check': 'real SDK HTTP503 one-toggle explicit retry with intent-bound prefetch' if retry_mode else
                                'real delayed SDK stop aborts in-flight prefetch, prevents stale execution; explicit start reuses one SDK request',
                                'pass': True, 'requests': requests, 'cancelled_prefetch': cancelled,
                                'cancelled_phase_frames': len(phase_frames), 'restart_prefetch': json.loads(json.dumps(transfers[restart_at:]))})
            finally:
                context.close()


def public_preview_selector(run_id, *, inline=False):
    return (f'#public-grid .evidence-card[data-run-id="{run_id}"] .work-cover' if inline else
            f'#original-content .original-preview[data-original-run="{run_id}"]')


def public_preview_panel(page, run_id, *, inline=False):
    return page.locator(public_preview_selector(run_id, inline=inline))


def wait_public_loaded(page, run_id, *, inline=False):
    selector = public_preview_selector(run_id, inline=inline)
    if inline:
        page.locator(selector).scroll_into_view_if_needed()
    page.wait_for_function('s=>["loaded","error","unavailable"].includes(document.querySelector(s)?.dataset.previewState)', arg=selector)
    panel = page.locator(selector)
    require(panel.get_attribute('data-preview-state') == 'loaded', 'Public original failed: ' + panel.inner_text()[:600])
    host = '.work-preview-host' if inline else '.preview-host'
    require(panel.locator(host + ' iframe').count() == 1, 'Expected one trusted SDK shell')
    outer_element = panel.locator(host + ' iframe').element_handle()
    require(outer_element is not None, 'Missing trusted SDK shell')
    outer = outer_element.content_frame()
    require(outer is not None, 'Missing shell browsing context')
    outer.wait_for_selector('iframe', state='attached')
    require(outer.locator('iframe').count() == 1, 'Expected one actual original context')
    # An attached iframe can be present before its browsing context is
    # addressable, and a heavy original (a WebGL scene under software rendering)
    # widens that window. Poll for the real frame instead of sampling once; a
    # frame that never becomes addressable still fails.
    inner = None
    for _ in range(200):
        inner = outer.locator('iframe').element_handle().content_frame()
        if inner is not None:
            break
        page.wait_for_timeout(50)
    require(inner is not None, 'Missing actual original browsing context')
    inner.wait_for_load_state('domcontentloaded')
    return inner


def wait_public_stopped(page, run_id, frame=None, *, inline=False):
    selector = public_preview_selector(run_id, inline=inline)
    page.wait_for_function('s=>document.querySelector(s)?.dataset.previewState==="stopped"&&!document.querySelector(s).querySelector("iframe")', arg=selector)
    if frame is not None:
        require(frame.is_detached(), 'Stopped original retained its actual browsing context')


def stop_pending_inline(page):
    """Pause each current automatic intent while the actual SDK is delayed."""
    ids = page.locator('#public-grid .work-cover[data-preview-state]').evaluate_all('''es=>es.filter(e=>
        !['error','stopped','unavailable'].includes(e.dataset.previewState)).map(e=>e.closest('[data-run-id]').dataset.runId)''')
    for run_id in ids:
        # Native dispatch avoids scrolling a card out of the observer margin,
        # which would test offscreen teardown rather than the explicit toggle.
        page.locator(f'#public-grid [data-run-original="{run_id}"]').dispatch_event('click')
        wait_public_stopped(page, run_id, inline=True)
    return ids


def wait_inline_registered(page, rows):
    """Prove restored registration by loaded real frames, not retained card DOM."""
    eligible = [r for r in rows if r['original']['status'] in ('ready', 'missing_dependencies') and
                page.locator(f'#public-grid [data-run-id="{r["id"]}"]').count()]
    require(eligible, 'Need an eligible card to verify restored inline registrations')
    return [(r['id'], wait_public_loaded(page, r['id'], inline=True)) for r in eligible]


def public_fit_viewport(page, frame, run_id, report):
    """Light modal is fit-only: resize the real host rather than invent controls."""
    results = []
    for viewport in ('desktop', 'phone'):
        page.set_viewport_size(VIEWPORTS[viewport])
        host = public_preview_panel(page, run_id).locator('.preview-host')
        page.wait_for_function('s=>{let h=document.querySelector(s),f=h?.querySelector("iframe");return !!f&&parseFloat(f.style.width)===Math.max(200,Math.min(4096,h.clientWidth))&&parseFloat(f.style.height)===Math.max(200,Math.min(4096,h.clientHeight));}',
                               arg=public_preview_selector(run_id) + ' .preview-host')
        expected = host.evaluate('e=>({width:Math.max(200,Math.min(4096,e.clientWidth)),height:Math.max(200,Math.min(4096,e.clientHeight))})')
        frame.wait_for_function('size=>innerWidth===size.width&&innerHeight===size.height', arg=expected)
        actual = frame.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')
        require(actual['width'] == expected['width'] and actual['height'] == expected['height'], 'Canvas fit viewport differs from actual modal host')
        strict_overflow(page, 'public-original-fit-' + viewport, report)
        results.append({'viewport': viewport, 'host': expected, 'actual': actual})
    require(results[1]['actual']['width'] < results[0]['actual']['width'], 'Phone resize did not change original viewport')
    page.set_viewport_size(VIEWPORTS['desktop'])
    two_frames(page)
    return results


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
        # Stay on the latest public projection but never substitute an arbitrary
        # SVG/CSS original for the required real Canvas representative.
        import base64
        canvas_rows = []
        for candidate in sources['seed']['runs']:
            descriptor = candidate.get('original') or {}
            if descriptor.get('status') not in ('ready', 'missing_dependencies') or descriptor.get('entrypoint') != 'index.html':
                continue
            package = json.loads(safe_read(args.site, descriptor['package']['path']))
            entry = base64.b64decode(package['files'][descriptor['entrypoint']]['base64'], validate=True).decode()
            if re.search(r'<canvas\b', entry, re.I):
                canvas_rows.append(candidate)
        require(canvas_rows, 'Latest public projection has no real Canvas original')
        row = next((r for r in canvas_rows if r['id'] == originals.CANVAS), canvas_rows[0])
        open_public_run(page, url, row)
        click_clock(page.locator(f'[data-zoom-run="{row["id"]}"]'), page)
        frame = wait_public_loaded(page, row['id'])
        latency = page.evaluate('performance.now()-window.__explicitClick')
        require(frame.locator('canvas').count() > 0, 'Canvas smoke did not load a real canvas original')
        motion = originals.observe_motion(page, frame, 'public-canvas', args)
        require(motion['visible_change'], 'Canvas screenshot pixels did not change')
        viewport = public_fit_viewport(page, frame, row['id'], report)
        toggle = page.locator(f'#original-content [data-preview-start="{row["id"]}"]')
        require(toggle.count() == 1 and page.locator('#original-content .preview-controls button').count() == 1,
                'Canvas modal lacks its single start/stop toggle')
        toggle.click()
        wait_public_stopped(page, row['id'], frame)
        toggle.click()
        restarted = wait_public_loaded(page, row['id'])
        require(not restarted.is_detached() and restarted.locator('canvas').count() > 0, 'Restart did not recreate real Canvas')
        page.locator('[data-public-close="original-dialog"]').click()
        page.wait_for_function('!document.querySelector("#original-dialog").open')
        require(restarted.is_detached() and page.locator('#original-content iframe').count() == 0, 'Close retained Canvas modal context')
        wait_public_loaded(page, row['id'], inline=True)
        validate_requests(requests, url, sources['manifest'], allow_preview=True)
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
    pair_errors = observe_browser_errors(pair_context, pair_page, args)
    try:
        row_a = next(r for r in sources['seed']['runs']
                     if r['registered_media'] and r.get('original', {}).get('status') in ('ready', 'missing_dependencies'))
        row_b = next(r for r in sources['seed']['runs']
                     if r['id'] != row_a['id'] and r['task_id'] == row_a['task_id'] and r['purpose'] == row_a['purpose']
                     and r['registered_media'] and r.get('original', {}).get('status') in ('ready', 'missing_dependencies'))
        pair_page.goto(url + '?' + urlencode({'task_id': row_a['task_id'], 'purpose': row_a['purpose']}), wait_until='networkidle')
        public_ready(pair_page)
        reach_public_run(pair_page, row_a)
        pair_page.locator(f'[data-select-run="{row_a["id"]}"]').first.click()
        reach_public_run(pair_page, row_b)
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
        pair_page.wait_for_function('ids=>{let actual=[...document.querySelectorAll("#compare-content .compare-column[data-run-id]")].map(e=>e.dataset.runId);return actual.length===ids.length&&ids.every(id=>actual.includes(id));}',
                                    arg=[row_a['id'], row_b['id']])
        require(pair_page.locator('#compare-content p.error').count() == 0,
                'Comparison dialog rendered an error instead of the exact selected pair')
        require(not pair_errors, 'Pair UI JavaScript errors: ' + '; '.join(pair_errors))
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
    args.browser_error_receipts = report['javascript_errors'] = []
    if not args.original_smoke:
        report['untested'].append('Canvas/pair lifecycle smoke (run separately with --original-smoke)')
    if args.audit_only or args.original_smoke or args.ux_only:
        report['untested'].append('Paired performance distributions')
    if args.audit_only or args.original_smoke or args.performance_only:
        report['untested'].append('Complete gallery UX/routes/delayed-loader suite')
    try:
        sources = load_sources(args, report)
        args.browser_catalog_pins = catalog_audit_pins(sources)
        args.baseline_catalog_pins = catalog_audit_pins(sources, baseline=True)
        if not args.audit_only:
            from playwright.sync_api import sync_playwright
            with ExitStack() as stack:
                current, _ = stack.enter_context(serve_gallery(args.site, sources['manifest']))
                baseline = stack.enter_context(v2.serve_report(args.baseline_site / 'index.html', sources['baseline_data'], gzip_html=True))
                baseline_actions, _ = stack.enter_context(serve_gallery(args.baseline_site, sources['baseline_manifest']))
                playwright = stack.enter_context(sync_playwright())
                # Full Chromium's new headless mode keeps native utility-world
                # actions responsive with animated originals; headless_shell stalls.
                browser = playwright.chromium.launch(channel='chromium', headless=True, args=['--no-sandbox'])
                report['browser_fixture'] = {'channel': 'chromium', 'version': browser.version,
                                             'headless': True, 'forced_software_gl': False}
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
        import traceback
        report.update(status='fail', failure=f'{type(error).__name__}: {error}', failure_stack=traceback.format_exc())
    (args.output / 'gallery-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'status': report['status'], 'receipt': str(args.output / 'gallery-verification.json'), 'failure': report.get('failure')}, ensure_ascii=False))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
