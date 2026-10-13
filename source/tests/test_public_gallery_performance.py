"""Utility-only fixtures: never claimed as gallery performance/UX evidence."""
import copy
import gzip
import json
from html import escape
from pathlib import Path
import time
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_public_gallery as verifier


class GalleryUtilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / 'site'
        self.site.mkdir()
        self.baseline = self.root / 'baseline'
        self.baseline.mkdir()
        self.trust = self.root / 'trust'
        self.trust.mkdir()
        self.files = {}
        self.add_file('index.html', b'<html>' + b'real gzip body ' * 200 + b'</html>', 'text/html', 'index')
        self.js = self.add_addressed('viewer', b'window.BenchPreview={};', 'js', 'text/javascript', 'runtime')
        self.full = self.add_addressed('viewer', b'<html>full viewer</html>', 'html', 'text/html', 'viewer')
        self.thumb = self.add_addressed('media', b'compressed thumbnail', 'jpg', 'image/jpeg', 'thumbnail')
        self.original = self.add_addressed('originals', b'{"entrypoint":"index.html"}', 'json', 'application/json', 'original')
        self.manifest = {'version': 3, 'format': 'progressive-static-v3', 'files': self.files, 'stats': {}}

    def add_file(self, name, raw, mime, role):
        path = self.site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        self.files[name] = {'sha256': verifier.digest(raw), 'size': len(raw), 'mime': mime, 'role': role}
        return name

    def add_addressed(self, directory, raw, extension, mime, role):
        return self.add_file(directory + '/' + verifier.digest(raw) + '.' + extension, raw, mime, role)

    def write_manifest(self):
        (self.site / verifier.MANIFEST).write_text(json.dumps(self.manifest))

    @staticmethod
    def fetch(url, encoding='identity'):
        with urlopen(Request(url, headers={'Accept-Encoding': encoding}), timeout=5) as response:
            return dict(response.headers), response.read()

    def test_cli_standard_library_and_separate_trust(self):
        common = ['--site', str(self.site), '--baseline-site', str(self.baseline), '--trust-root', str(self.trust), '--output', str(self.root / 'out')]
        a = verifier.arguments(common + ['--performance-only'])
        self.assertEqual(a.timing_repeats, 3)
        self.assertTrue(a.performance_only)
        for extra in (['--timing-repeats', '1'], ['--timing-repeats', '4'], ['--ux-only', '--performance-only']):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                verifier.arguments(common + extra)
        for extra in (['--output', str(self.site / 'out')], ['--trust-root', str(self.site)], ['--baseline-site', str(self.site)]):
            with self.subTest(extra=extra), self.assertRaises(AssertionError):
                verifier.arguments(common + extra)

    def test_real_gzip_html_sdk_and_original_json(self):
        with verifier.serve_gallery(self.site, self.manifest) as (url, traffic):
            for name in ('index.html', self.full, self.js, self.original):
                headers, body = self.fetch(url + name, 'gzip')
                raw = (self.site / name).read_bytes()
                self.assertEqual(headers['Content-Encoding'], 'gzip')
                self.assertEqual(headers['Vary'], 'Accept-Encoding')
                self.assertEqual(gzip.decompress(body), raw)
                self.assertEqual(verifier.digest(gzip.decompress(body)), self.files[name]['sha256'])
                self.assertEqual(body, gzip.compress(raw, compresslevel=6, mtime=0))
                self.assertEqual(int(headers['Content-Length']), len(body))
                self.assertEqual(headers['X-Report-SHA256'], verifier.digest(raw))
                headers, body = self.fetch(url + name)
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(headers['Vary'], 'Accept-Encoding')
                self.assertEqual(int(headers['Content-Length']), len(raw))
                self.assertEqual(body, raw)
            self.assertEqual(len(traffic), 8)

    def test_identity_mime_and_media_bytes_unchanged(self):
        with verifier.serve_gallery(self.site, self.manifest) as (url, _):
            for name, mime in ((self.js, 'text/javascript'), (self.thumb, 'image/jpeg'), (self.original, 'application/json')):
                headers, body = self.fetch(url + name, 'gzip' if name == self.thumb else 'identity')
                self.assertEqual(headers['Content-Type'], mime)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(body, (self.site / name).read_bytes())

    def test_gzip_negotiation_requires_an_accepted_coding(self):
        with verifier.serve_gallery(self.site, self.manifest) as (url, _):
            for encoding, compressed in (('gzip,br', True), ('GZip; q=0.5', True), ('*;q=1', True),
                    ('gzip;q=0, br', False), ('*;q=1, gzip;q=0', False), ('gzip;q=bogus', False),
                    ('notgzip', False), ('br', False), ('identity', False), ('', False)):
                with self.subTest(encoding=encoding):
                    headers, body = self.fetch(url + self.original, encoding)
                    raw = (self.site / self.original).read_bytes()
                    self.assertEqual(headers.get('Content-Encoding'), 'gzip' if compressed else None)
                    self.assertEqual(gzip.decompress(body) if compressed else body, raw)
                    self.assertEqual(int(headers['Content-Length']), len(body))
                    self.assertEqual(headers['Vary'], 'Accept-Encoding')
            headers, body = self.fetch(url + self.thumb, 'gzip,br')
            self.assertNotIn('Content-Encoding', headers)
            self.assertNotIn('Vary', headers)
            self.assertEqual(body, (self.site / self.thumb).read_bytes())

    def test_current_and_baseline_actions_share_gzip_and_observed_transfer_accounting(self):
        import inspect
        name = self.add_addressed('viewer', b'window.example=1;\n' * 1000,
                                  'js', 'application/javascript', 'runtime')
        for path in self.files:
            destination = self.baseline / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((self.site / path).read_bytes())
        baseline_manifest = {**self.manifest, 'version': 2}
        bodies = []
        for root, manifest in ((self.site, self.manifest), (self.baseline, baseline_manifest)):
            with verifier.serve_gallery(root, manifest) as (url, _):
                headers, body = self.fetch(url + name, 'gzip,br')
                self.assertEqual(headers['Content-Encoding'], 'gzip')
                self.assertEqual(gzip.decompress(body), (root / name).read_bytes())
                self.assertEqual(int(headers['Content-Length']), len(body))
                self.assertLess(len(body), manifest['files'][name]['size'])
                bodies.append(body)
                measured = verifier.summary([{'transfer_bytes': len(body)}])
                self.assertEqual(measured['transfer_bytes']['median'], len(body))
                self.assertNotEqual(measured['transfer_bytes']['median'], manifest['files'][name]['size'])
        self.assertEqual(bodies[0], bodies[1])
        # No browser claim: guard the real browser samplers' Resource Timing
        # expression against substituting decoded manifest sizes for transfers.
        expression = "transfer_bytes:n.transferSize+performance.getEntriesByType('resource').reduce((s,r)=>s+r.transferSize,0)"
        for sampler in (verifier.gallery_sample, verifier.baseline_sample):
            self.assertIn(expression, inspect.getsource(sampler))
        self.assertIn('s+r.transferSize', inspect.getsource(verifier.gallery_sample))

    def test_real_http_retry_503_then_exact_bytes(self):
        with verifier.serve_gallery(self.site, self.manifest, fail_once=[self.js]) as (url, traffic):
            with self.assertRaises(HTTPError) as caught:
                self.fetch(url + self.js)
            self.assertEqual(caught.exception.code, 503)
            _, body = self.fetch(url + self.js)
            self.assertEqual(body, (self.site / self.js).read_bytes())
            self.assertEqual([r['path'] for r in traffic], [self.js, self.js])

    def test_unknown_paths_changed_bytes_and_symlink_rejected(self):
        with verifier.serve_gallery(self.site, self.manifest) as (url, _):
            for name in ('not-declared', '../private', '%2e%2e/private'):
                with self.subTest(name=name), self.assertRaises(HTTPError) as caught:
                    self.fetch(url + name)
                self.assertEqual(caught.exception.code, 404)
            (self.site / self.js).write_bytes(b'changed')
            with self.assertRaises(HTTPError) as caught:
                self.fetch(url + self.js)
            self.assertEqual(caught.exception.code, 409)

    def test_safe_read_rejects_parent_absolute_and_parent_symlink(self):
        for name in ('../outside', '/index.html', 'viewer/../index.html', 'viewer//x', 'x?y'):
            with self.subTest(name=name), self.assertRaises(AssertionError):
                verifier.safe_read(self.site, name)
        (self.site / 'escape').symlink_to(self.site / 'viewer', target_is_directory=True)
        with self.assertRaises(AssertionError):
            verifier.safe_read(self.site, 'escape/' + Path(self.js).name)

    def test_manifest_exact_inventory_and_hashes(self):
        self.write_manifest()
        self.assertEqual(verifier.checked_manifest(self.site, 3), self.manifest)
        (self.site / 'unmanaged.txt').write_text('unknown')
        with self.assertRaises(AssertionError):
            verifier.checked_manifest(self.site, 3)
        (self.site / 'unmanaged.txt').unlink()
        (self.site / self.thumb).write_bytes(b'changed')
        with self.assertRaises(AssertionError):
            verifier.checked_manifest(self.site, 3)

    def test_manifest_wrong_js_mime_and_arbitrary_executable_rejected(self):
        self.files[self.js]['mime'] = 'text/html'
        self.write_manifest()
        with self.assertRaises(AssertionError):
            verifier.checked_manifest(self.site, 3)
        self.files[self.js]['mime'] = 'text/javascript'
        self.add_addressed('media', b'arbitrary()', 'js', 'application/javascript', 'runtime')
        self.write_manifest()
        with self.assertRaises(AssertionError):
            verifier.checked_manifest(self.site, 3)

    def test_only_explicit_viewer_and_runtime_roles_are_accepted(self):
        self.files[self.js]['role'] = 'model-executable'
        self.write_manifest()
        with self.assertRaises(AssertionError):
            verifier.checked_manifest(self.site, 3)
        self.files[self.js]['role'] = 'runtime'
        self.files[self.full]['role'] = 'index'
        self.write_manifest()
        with self.assertRaises(AssertionError):
            verifier.checked_manifest(self.site, 3)

    def test_legacy_version_never_accepts_viewer_namespace(self):
        self.manifest.update(version=2, format='static-media-v2')
        self.write_manifest()
        with self.assertRaises(AssertionError):
            verifier.checked_manifest(self.site, 2)

    def test_cold_request_budget_requires_actual_thumbnail(self):
        seed = {'runs': [{'image': {'thumb': self.thumb}}]}
        url = 'http://example.test/'
        rows = lambda names: [{'url': url+n, 'method': 'GET', 'type': 'image'} for n in names]
        verifier.validate_requests(rows(['index.html', self.thumb]), url, self.manifest, seed, cold=True, viewport='phone')
        for names in (['index.html'], ['index.html', self.js], ['index.html', self.full], ['index.html', self.original],
                      ['index.html', self.thumb, self.thumb]):
            with self.subTest(names=names), self.assertRaises(AssertionError):
                verifier.validate_requests(rows(names), url, self.manifest, seed, cold=True, viewport='phone')
        verifier.validate_requests(rows(['index.html', self.thumb, self.thumb]), url, self.manifest, seed, cold=True, viewport='desktop')

    def test_cold_inline_requests_are_bounded_to_visible_eligible_cards(self):
        seed = {'runtime': {'path': self.js}, 'runs': [
            {'id': 'visible', 'image': {'thumb': self.thumb}, 'original': {
                'status': 'ready', 'package': {'path': self.original}}},
            {'id': 'hidden', 'image': None, 'original': {'status': 'ready', 'package': {
                'path': self.add_addressed('originals', b'{"hidden":true}', 'json', 'application/json', 'original')}}},
            {'id': 'unavailable', 'image': None, 'original': {'status': 'not_reviewed', 'package': None}}]}
        url = 'https://example.test/'
        rows = lambda names: [{'url': url + name, 'method': 'GET'} for name in names]
        good = ['index.html', self.thumb, self.js, self.original]
        self.assertEqual(verifier.validate_requests(rows(good), url, self.manifest, seed,
                         cold=True, viewport='phone', preview_ids=['visible']), good)
        for names, ids in ((good, []), (good + [self.original], ['visible']),
                           (good + [self.js], ['visible']), (good + [self.full], ['visible']),
                           (good + [seed['runs'][1]['original']['package']['path']], ['visible']),
                           (good, ['unavailable']), (good, ['visible', 'hidden'])):
            with self.subTest(names=names, ids=ids), self.assertRaises(AssertionError):
                verifier.validate_requests(rows(names), url, self.manifest, seed,
                                           cold=True, viewport='phone', preview_ids=ids)

    def test_pages_subdirectory_preserves_exact_inventory_and_origin(self):
        url = 'https://example.test/llm-bench-report/'
        seed = {'runs': [{'image': {'thumb': self.thumb}}]}
        rows = [{'url': url, 'method': 'GET'}, {'url': url + self.thumb, 'method': 'GET'}]
        self.assertEqual(verifier.validate_requests(rows, url, self.manifest, seed, cold=True, viewport='phone'),
                         ['index.html', self.thumb])
        for address in ('https://example.test/index.html',
                        'https://example.test/llm-bench-report-extra/index.html',
                        'https://external.test/llm-bench-report/index.html',
                        'http://example.test/llm-bench-report/index.html',
                        url + '../index.html', url + '%2e%2e/index.html',
                        url + 'api/runs', url + self.full):
            with self.subTest(address=address), self.assertRaises(AssertionError):
                verifier.validate_requests([{'url': address, 'method': 'GET'}], url, self.manifest,
                                           seed, cold=True, viewport='phone')

    def test_passive_request_audit_rejects_external_api_and_writes(self):
        for row in ({'url': 'http://external.test/index.html', 'method': 'GET'},
                    {'url': 'http://example.test/api/runs', 'method': 'GET'},
                    {'url': 'http://example.test/index.html', 'method': 'POST'}):
            with self.subTest(row=row), self.assertRaises(AssertionError):
                verifier.validate_requests([row], 'http://example.test/', self.manifest)

    def test_summary_missing_measurement_is_not_zero(self):
        summary = verifier.summary([{'first_actually_visible_image_ms': None, 'task_control_response_ms': 30},
                                    {'first_actually_visible_image_ms': 400, 'task_control_response_ms': 10},
                                    {'first_actually_visible_image_ms': 300, 'task_control_response_ms': 20}])
        self.assertEqual(summary['first_actually_visible_image_ms'], {'median': 350, 'observed': 2})
        self.assertEqual(summary['task_control_response_ms'], {'median': 20, 'observed': 3})
        self.assertEqual(summary['primary_original_latency_ms'], {'median': None, 'observed': 0})
        json.dumps(summary, allow_nan=False)

    def test_projection_no_mobile_fallback_or_invented_success(self):
        data = {'runs': [{'id': 'a', 'tool': 'tool', 'model': 'model', 'task_id': 'task', 'status': 'failed',
                          'evaluation': {'status': 'failed', 'evidence': ['evidence/mobile.png'], 'checks': [{'name': 'entrypoint', 'status': 'pass'}]}}],
                'evidence': {'a/evidence/mobile.png': self.thumb}, 'thumbnails': {'a/evidence/mobile.png': self.thumb},
                'assets': {self.thumb: {'width': 400, 'height': 850, 'mime': 'image/jpeg'}}, 'originals': {'a': {'status': 'no_entrypoint'}}}
        row = verifier.project_runs(data)[0]
        self.assertTrue(row['registered_media'])
        self.assertIsNone(row['image'])
        self.assertEqual(row['entry_status'], 'unknown')
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['started_at'])
        self.assertEqual(row['original'], data['originals']['a'])

    def test_entry_status_matches_frozen_v2_semantics(self):
        self.assertEqual(verifier.entry_status({'evaluation': {'status': 'failed'}, 'checks': [{'name': 'entrypoint', 'status': 'pass'}]}), 'unknown')
        self.assertEqual(verifier.entry_status({'checks': [{'name': 'entrypoint', 'status': 'fail'}]}), 'fail')
        self.assertEqual(verifier.entry_status({'evaluation': {'checks': [{'name': 'entrypoint', 'status': 'passed'}]}}), 'pass')
        self.assertEqual(verifier.entry_status({}), 'unknown')

    def test_delays_are_real_http_not_browser_interception(self):
        with verifier.serve_gallery(self.site, self.manifest, delays={self.js: .08}) as (url, _):
            started = time.monotonic()
            _, body = self.fetch(url + self.js)
            self.assertGreaterEqual(time.monotonic()-started, .08)
            self.assertEqual(body, (self.site / self.js).read_bytes())

    def test_cancelled_parallel_prefetch_allows_completed_and_native_aborted_intents_not_zero_downloads(self):
        transfers = [{'request_id': 0, 'path': self.original, 'outcome': 'finished', 'failure': None},
                     {'request_id': 1, 'path': self.original, 'outcome': 'failed', 'failure': 'net::ERR_ABORTED'}]
        self.assertTrue(transfers)  # The obsolete zero-package assertion would reject this valid trace.
        result = verifier.check_cancelled_prefetch(transfers, 2, {self.original: 2}, must_abort=[1])
        self.assertEqual(result, transfers)
        self.assertIsNot(result, transfers)
        self.assertEqual(result[0]['outcome'], 'finished')
        with self.assertRaisesRegex(AssertionError, 'in-flight package was not natively aborted'):
            verifier.check_cancelled_prefetch(transfers, 2, {self.original: 2}, must_abort=[0])

    def test_cancelled_parallel_prefetch_rejects_late_extra_wrong_pending_and_nonabort_requests(self):
        good = {'request_id': 0, 'path': self.original, 'outcome': 'finished', 'failure': None}
        for transfers, count, limits in (([good, good], 1, {self.original: 2}),
                ([good, good], 2, {self.original: 1}), ([{**good, 'path': 'originals/wrong.json'}], 1, {self.original: 1}),
                ([{**good, 'outcome': 'pending'}], 1, {self.original: 1}),
                ([{**good, 'outcome': 'failed', 'failure': 'net::ERR_CONNECTION_RESET'}], 1, {self.original: 1})):
            with self.subTest(transfers=transfers), self.assertRaises(AssertionError):
                verifier.check_cancelled_prefetch(transfers, count, limits)

    def test_prefetch_limits_count_each_actual_eligible_intent_and_explicit_restart(self):
        seed = {'runs': [{'id': 'ready', 'original': {'status': 'ready', 'package': {'path': self.original}}},
                         {'id': 'unavailable', 'original': {'status': 'no_entrypoint', 'package': None}}]}
        self.assertEqual(verifier.original_intent_counts(seed, ['ready', 'ready']), {self.original: 2})
        self.assertEqual(verifier.original_intent_counts(seed, ['ready', 'ready', 'ready']), {self.original: 3})
        with self.assertRaisesRegex(AssertionError, 'not eligible'):
            verifier.original_intent_counts(seed, ['unavailable'])

    def test_native_prefetch_observer_keeps_completed_vs_failed_outcomes(self):
        class Page:
            def __init__(self):
                self.events = {}
            def on(self, event, callback):
                self.events[event] = callback
        class Request:
            def __init__(self, url, failure=None):
                self.url, self.failure = url, failure
        page = Page()
        transfers = verifier.observe_original_transfers(page)
        completed = Request('https://example.test/' + self.original)
        cancelled = Request('https://example.test/' + self.original, 'net::ERR_ABORTED')
        page.events['request'](Request('https://example.test/' + self.js))
        for request in (completed, cancelled):
            page.events['request'](request)
        self.assertEqual([row['outcome'] for row in transfers], ['pending', 'pending'])
        page.events['requestfinished'](completed)
        page.events['requestfailed'](cancelled)
        self.assertEqual([row['outcome'] for row in transfers], ['finished', 'failed'])
        self.assertEqual(transfers[1]['failure'], 'net::ERR_ABORTED')
        verifier.check_cancelled_prefetch(transfers, 2, {self.original: 2}, must_abort=[1])

    def test_schema_equality_distinguishes_booleans_from_numbers(self):
        self.assertFalse(verifier.same({'registered_media': True}, {'registered_media': 1}))
        self.assertFalse(verifier.same({'size': 1}, {'size': True}))
        self.assertTrue(verifier.same({'a': 1, 'b': 2}, {'b': 2, 'a': 1}))

    def gallery_fixture(self):
        data = {'runs': [{'id': 'a', 'tool': 'tool', 'model': 'M < &', 'task_id': 'crocodile', 'purpose': 'benchmark',
                          'date': '2026-10-08', 'status': 'failed', 'evaluation': {'status': 'completed', 'evidence': ['evidence/mobile.png']},
                          'checks': [{'name': 'entrypoint', 'status': 'fail'}]}],
                'tasks': [{'id': 'crocodile', 'name': 'Task'}],
                'evidence': {'a/evidence/mobile.png': self.thumb}, 'thumbnails': {'a/evidence/mobile.png': self.thumb},
                'assets': {self.thumb: {'width': 400, 'height': 850, 'mime': 'image/jpeg'}}, 'originals': {'a': {'status': 'no_entrypoint'}},
                'offline': {'path': 'offline.html', 'sha256': '1'*64, 'size': 100}}
        full_info = {k: self.files[self.full][k] for k in ('sha256', 'size')}
        full_info['path'] = self.full
        runtime_info = {k: self.files[self.js][k] for k in ('sha256', 'size')}
        runtime_info['path'] = self.js
        seed = {'version': 3, 'format': 'progressive-static-v3', 'score_encoding': 'scope-index-v1',
                'tasks': [{'id': 'crocodile', 'name': 'Task'}],
                'runs': verifier.project_runs(data), 'score_scopes': [],
                'defaults': {'task_id': 'crocodile', 'purpose': 'benchmark', 'page_size': 2, 'mobile_page_size': 1, 'score_source': ''},
                'counts': {'runs': 1, 'full_runs': 1, 'benchmark': 1, 'full_benchmark': 1, 'reviews': 0},
                'full': {**full_info, 'script_sha256': verifier.digest(b'trusted-program')},
                'runtime': runtime_info, 'offline': data['offline']}
        return data, seed, full_info, runtime_info

    def render_fixture(self, seed):
        payload = json.dumps(seed).replace('<', '\\u003c')
        model = escape(seed['runs'][0]['model'])
        source = escape(seed['defaults']['score_source'], quote=True)
        cells = ''.join('<div class="score-cell" data-dimension="' + key + '"><dt>' + label + '</dt><dd>未评审</dd></div>'
                        for key, label in verifier.v2.SCORE_LABELS.items())
        return ('<style>trusted-css</style><article class="evidence-card" data-run-id="a" data-model-route="' + model + '">'
                '<div class="work-heading"><h2 class="work-name" aria-label="' + model + '" title="' + model + '">' + model + '</h2></div>'
                '<div class="work-score" data-score-scope="' + source + '"><dl>' + cells + '</dl></div>'
                '<div class="work-cover"></div><div class="work-actions">'
                '<button data-run-original="a" disabled></button><button data-select-run="a"></button>'
                '<button data-zoom-run="a" disabled></button><button data-detail-run="a"></button></div>'
                '<details class="work-facts"><p class="work-meta">tool · 2026-10-08</p>'
                '<p class="work-status">会话 failed · 入口 fail · 评估 completed</p></details></article>'
                '<script>window.BENCH_GALLERY=' + payload + ';</script><script>trusted-bootstrap</script>')

    def test_seed_exact_projection_independent_code_and_descriptors(self):
        data, seed, full_info, runtime_info = self.gallery_fixture()
        def audit(changed, html=None):
            return verifier.audit_gallery(html or self.render_fixture(changed), data, 'trusted-bootstrap', 'trusted-css',
                                          full_info, runtime_info, b'trusted-program')
        self.assertEqual(audit(seed), seed)
        for field in ('registered_media', 'model', 'entry_status', 'original'):
            changed = copy.deepcopy(seed)
            changed['runs'][0][field] = {'registered_media': 1, 'model': 'invented', 'entry_status': 'pass', 'original': None}[field]
            with self.subTest(field=field), self.assertRaises(AssertionError):
                audit(changed)
        for path in ('full', 'runtime', 'offline'):
            changed = copy.deepcopy(seed)
            changed[path]['sha256'] = '2'*64
            with self.subTest(path=path), self.assertRaises(AssertionError):
                audit(changed)
        with self.assertRaises(AssertionError):
            audit(seed, self.render_fixture(seed).replace('trusted-bootstrap', 'model-code()'))
        with self.assertRaises(AssertionError):
            audit(seed, self.render_fixture(seed).replace('data-zoom-run="a" disabled', 'data-zoom-run="a"'))
        with self.assertRaises(AssertionError):
            audit(seed, self.render_fixture(seed).replace('会话 failed', '会话 completed'))

    def test_independent_projection_rejects_wrong_raw_review_reference_and_values(self):
        data, seed, full_info, runtime_info = self.gallery_fixture()
        data['runs'][0].update(conditions={'rubric_version': 'visual-v1'}, reviews=[{
            'id': 'real-fixture-review', 'reviewer': 'AI 建议评分', 'blind': False,
            'created_at': '2026-10-08T10:12:13.765432Z', 'scores': {}}])
        seed['score_scopes'] = verifier.v2.score_scopes(data['runs'])
        seed['defaults']['score_source'] = verifier.v2.default_score_source(seed['score_scopes'])
        seed['counts']['reviews'] = 1
        seed['runs'][0]['scores'] = [[0, 0, [None] * 6]]
        def audit(changed):
            html = self.render_fixture(changed).replace('未评审', '分项缺失')
            return verifier.audit_gallery(html, data, 'trusted-bootstrap', 'trusted-css', full_info, runtime_info, b'trusted-program')
        self.assertEqual(audit(seed), seed)
        for slot, replacement in ((1, 999), (2, [0, None, None, None, None, None])):
            changed = copy.deepcopy(seed); changed['runs'][0]['scores'][0][slot] = replacement
            with self.subTest(slot=slot), self.assertRaisesRegex(AssertionError, 'raw review reference'):
                audit(changed)

    def test_empty_zoom_placeholder_is_not_an_initial_thumbnail(self):
        data, seed, full_info, runtime_info = self.gallery_fixture()
        def audit(image):
            return verifier.audit_gallery(self.render_fixture(seed) + image, data, 'trusted-bootstrap', 'trusted-css',
                                          full_info, runtime_info, b'trusted-program')
        self.assertEqual(audit('<img id="public-zoom-image" alt="">'), seed)
        for image in ('<img id="other">', '<img id="public-zoom-image" data-src="media/x.jpg">'):
            with self.subTest(image=image), self.assertRaises(AssertionError):
                audit(image)

    def test_projection_only_registered_declared_desktop_uses_image(self):
        data, _, _, _ = self.gallery_fixture()
        # A dictionary entry not registered on the run does not become evidence.
        key = 'a/evidence/desktop.png'
        data['evidence'][key] = self.thumb
        data['thumbnails'][key] = self.thumb
        self.assertIsNone(verifier.project_runs(data)[0]['image'])
        data['runs'][0]['evaluation']['evidence'].append('evidence/desktop.png')
        image = verifier.project_runs(data)[0]['image']
        self.assertEqual(image, {'thumb': self.thumb, 'full': self.thumb, 'width': 400, 'height': 850, 'label': 'desktop.png'})

    def test_scaffold_rejects_eager_images_and_model_script(self):
        for markup in ('<img src="media/x.jpg">', '<iframe srcdoc="evil"></iframe>', '<button onclick="evil()">',
                       '<script src="viewer/untrusted.js"></script>', '<link rel="stylesheet" href="remote.css">'):
            parsed = verifier.GalleryHTML()
            parsed.feed(markup)
            self.assertTrue(parsed.unsafe)


    def test_score_first_phone_uses_header_history_target_and_compact_unfolded_sheet(self):
        css = (verifier.ROOT / 'web/public-gallery.css').read_text()
        html = (verifier.ROOT / 'web/public-gallery.html').read_text()
        self.assertIn('min-height:44px', css)
        self.assertIn('.public-main{padding-top:4px}', css)
        self.assertIn('.gallery-heading{padding:2px 0}', css)
        self.assertIn('grid-template-columns:repeat(3,minmax(0,1fr))', css)
        self.assertIn('.score-cell strong{font-size:22px}', css)
        self.assertIn('.score-cell dd{display:flex;align-items:center', css)
        self.assertIn('object-fit:contain', css)
        self.assertIn('data-full-view="scores"', html.split('</header>')[0])
        self.assertNotIn('gallery-intro', html)
        self.assertIn('<details id="public-models" class="public-models" open>', html)
        bootstrap = (verifier.ROOT / 'web/public-gallery.js').read_text()
        self.assertIn("if(narrow.matches&&!state.model)$('#public-models').open=false;", bootstrap)
        self.assertNotIn('work-score-details', css)

    def test_gallery_score_and_preview_styles_do_not_override_full_viewer(self):
        css = (verifier.ROOT / 'web/public-gallery.css').read_text()
        for selector in ('.score-sheet', '.score-cell', '.score-unknown', '.score-caption',
                         '.evidence-card', '.preview-controls', '.preview-host', '.dialog-footnote'):
            with self.subTest(selector=selector):
                self.assertIn('.public-gallery ' + selector, css)
                self.assertNotIn('}' + selector, css)

    def test_independent_server_audit_rejects_hidden_missing_or_changed_score_cells(self):
        data, seed, full_info, runtime_info = self.gallery_fixture()
        html = self.render_fixture(seed)
        def audit(markup):
            return verifier.audit_gallery(markup, data, 'trusted-bootstrap', 'trusted-css', full_info, runtime_info, b'trusted-program')
        self.assertEqual(audit(html), seed)
        for markup in (html.replace('未评审', '0/4', 1), html.replace('data-dimension="motion"', 'data-dimension="other"'),
                       html.replace('<dl>', '<details><dl>').replace('</dl>', '</dl></details>'),
                       html.replace('<div class="score-cell" data-dimension="motion"><dt>运动表现</dt><dd>未评审</dd></div>', '')):
            with self.subTest(markup=markup[-64:]), self.assertRaises(AssertionError):
                audit(markup)

class NativeExceptionAttributionTests(unittest.TestCase):
    def test_native_observer_immediately_skips_pauses_in_root_and_child_sessions(self):
        from types import SimpleNamespace
        commands = []
        class Session:
            def on(self, event, callback):
                pass
            def send(self, method, params=None):
                commands.append((method, params or {}))
                if method == 'Page.getFrameTree':
                    return {'frameTree': {'frame': {'id': 'report'}}}
                return {}
        session = Session()
        observer = verifier.NativeExceptionObserver(
            SimpleNamespace(new_cdp_session=lambda page: session), object(),
            SimpleNamespace(script_hashes=set()), verifier.BrowserErrorLedger())
        root_methods = [method for method, _ in commands]
        enabled = root_methods.index('Debugger.enable')
        self.assertEqual(commands[enabled + 1], ('Debugger.setSkipAllPauses', {'skip': True}))
        self.assertEqual(commands[-1][1]['waitForDebuggerOnStart'], False)
        commands.clear()
        observer.event('', 'Page.frameNavigated', {'frame': {'id': 'report'}})
        self.assertEqual(commands, [('Debugger.setSkipAllPauses', {'skip': True})])
        commands.clear()
        observer.attached({'sessionId': 'child', 'targetInfo': {'type': 'iframe', 'targetId': 'shell'}})
        child = [json.loads(params['message']) for method, params in commands
                 if method == 'Target.sendMessageToTarget']
        enabled = [item['method'] for item in child].index('Debugger.enable')
        self.assertEqual(child[enabled + 1]['method'], 'Debugger.setSkipAllPauses')
        self.assertEqual(child[enabled + 1]['params'], {'skip': True})
        autoattach = next(item for item in child if item['method'] == 'Target.setAutoAttach')
        self.assertEqual(autoattach['params']['waitForDebuggerOnStart'], False)

    def test_capture_precedes_owner_reads_and_early_report_owner_survives_target_attach(self):
        from types import SimpleNamespace
        for early_owner in (False, True):
            with self.subTest(early_owner=early_owner):
                commands = []
                class Session:
                    def on(self, event, callback):
                        pass
                    def send(self, method, params=None):
                        commands.append((method, params or {}))
                        if method == 'Page.getFrameTree':
                            return {'frameTree': {'frame': {'id': 'report'}}}
                        if method == 'DOM.getFrameOwner':
                            return {'backendNodeId': 1}
                        if method == 'DOM.resolveNode':
                            return {'object': {'objectId': 'owner'}}
                        if method == 'Runtime.callFunctionOn':
                            return {'result': {'value': {'run_id': '1' * 32}}}
                        return {}
                session = Session()
                observer = verifier.NativeExceptionObserver(
                    SimpleNamespace(new_cdp_session=lambda page: session), object(),
                    SimpleNamespace(script_hashes=set()), verifier.BrowserErrorLedger())
                if early_owner:
                    observer.event('', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
                    self.assertEqual(observer.frames['shell']['binding']['run_id'], '1' * 32)
                commands.clear()
                observer.attached({'sessionId': 'child', 'targetInfo': {
                    'type': 'iframe', 'targetId': 'shell', 'parentFrameId': 'report'}})
                methods = [json.loads(params['message'])['method'] if method == 'Target.sendMessageToTarget'
                           else method for method, params in commands]
                self.assertEqual(methods[:6], ['Runtime.enable', 'Page.enable', 'Page.getFrameTree',
                    'Debugger.enable', 'Debugger.setSkipAllPauses', 'Target.setAutoAttach'])
                if early_owner:
                    self.assertNotIn('DOM.getFrameOwner', methods)
                else:
                    self.assertLess(methods.index('Target.setAutoAttach'), methods.index('DOM.getFrameOwner'))
                self.assertEqual(observer.frames['shell']['binding']['run_id'], '1' * 32)
                self.assertEqual(observer.frames['shell']['session_id'], 'child')

    def test_inner_native_owner_is_requested_before_report_binding_is_ready(self):
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.frames = {'shell': {'id': 'shell', 'session_id': 'child'}}
        observer.exception_evidence = []
        observer.ledger = verifier.BrowserErrorLedger()
        commands = []
        observer.send = lambda sid, method, params, callback: commands.append((sid, method, params))
        observer.event('child', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'shell'})
        self.assertEqual(commands, [('child', 'DOM.getFrameOwner', {'frameId': 'inner'})])
        self.assertEqual(observer.frames['inner']['parentId'], 'shell')
        self.assertNotIn('binding', observer.frames['shell'])

    def test_root_discovered_inner_frame_adopts_authoritative_child_session_without_losing_cache(self):
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.main_frame_id = 'report'
        observer.frames = {'shell': {'id': 'shell', 'session_id': 'child'}}
        observer.exception_evidence = []
        observer.ledger = verifier.BrowserErrorLedger()
        commands = []
        observer.send = lambda sid, method, params, callback: commands.append((sid, method, params))
        observer.event('', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'shell'})
        cached = observer.frames['inner']
        self.assertEqual(cached['session_id'], '')
        cached['owner'] = {'nodeName': 'IFRAME', 'sandbox': 'allow-scripts',
            'referrerpolicy': 'no-referrer', 'has_srcdoc': True}
        observer.event('child', 'Page.frameNavigated', {'frame': {
            'id': 'inner', 'parentId': 'shell', 'url': 'about:srcdoc'}})
        self.assertIs(observer.frames['inner'], cached)
        self.assertEqual(cached['session_id'], 'child')
        self.assertEqual(cached['parentId'], 'shell')
        self.assertEqual(cached['owner']['sandbox'], 'allow-scripts')
        self.assertEqual(commands, [('child', 'DOM.getFrameOwner', {'frameId': 'inner'})])
        observer.event('', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'shell'})
        self.assertEqual(cached['session_id'], 'child')
        self.assertEqual(len(commands), 1)

    def fixture(self):
        from types import SimpleNamespace
        source = 'const radius = 0; radius = 1;'
        sha = verifier.digest(source.encode())
        run = '1' * 32
        catalog = SimpleNamespace(trusted_script_hashes={verifier.digest(b'pinned trusted SDK script')},
            runs={run: {'package_path': 'originals/' + '2' * 64 + '.json',
            'package_sha256': '2' * 64, 'entry_sha256': '3' * 64,
            'scripts': {sha: {'source': source, 'script_sha256': sha, 'file': 'index.html', 'file_sha256': '3' * 64}}}})
        context = {'id': 3, 'uniqueId': 'native-unique', 'origin': '://',
                   'auxData': {'isDefault': True, 'type': 'default', 'frameId': 'inner'}}
        script = {'executionContextId': 3, 'context_unique_id': 'native-unique', 'hash': sha}
        frame = {'id': 'inner', 'parentId': 'shell', 'session_id': 'native-session'}
        owner = {'nodeName': 'IFRAME', 'sandbox': 'allow-scripts', 'referrerpolicy': 'no-referrer', 'has_srcdoc': True}
        shell = {'id': 'shell', 'parentId': 'report', 'session_id': 'native-session', 'binding': {
            'run_id': run, **owner, 'title': '原作隔离预览 ' + run, 'host_direct': True,
            'shell_count': 1, 'host_class': 'work-preview-host', 'report_frame_id': 'report'}}
        return [context, script, frame, shell, owner, source, catalog]

    def catalog_fixture(self, directory):
        import base64
        root = Path(directory); (root / 'originals').mkdir(); (root / 'viewer').mkdir()
        source = 'const radius = 0; radius = 1;'
        entry = ('<html><script>' + source + '</script></html>').encode()
        package = {'version': 1, 'run_id': '1' * 32, 'entrypoint': 'index.html', 'missing': [], 'files': {'index.html': {
            'mime': 'text/html', 'size': len(entry), 'sha256': verifier.digest(entry),
            'base64': base64.b64encode(entry).decode()}}}
        raw = json.dumps(package).encode(); sha = verifier.digest(raw); path = 'originals/' + sha + '.json'
        (root / path).write_bytes(raw)
        runtime = b'function originalDiagnostics() {\n    void 0;\n  }\n'
        runtime_sha = verifier.digest(runtime); runtime_path = 'viewer/' + runtime_sha + '.js'
        (root / runtime_path).write_bytes(runtime)
        manifest = {'version': 3, 'files': {
            path: {'role': 'original', 'sha256': sha, 'size': len(raw)},
            runtime_path: {'role': 'runtime', 'sha256': runtime_sha, 'size': len(runtime)}}}
        (root / verifier.MANIFEST).write_text(json.dumps(manifest))
        descriptor = {'entrypoint': 'index.html', 'entry_sha256': verifier.digest(entry), 'missing': [],
                      'package': {'path': path, 'sha256': sha, 'size': len(raw)}}
        sources = {'manifest': manifest, 'data': {'runs': [{'id': '1' * 32}], 'originals': {'1' * 32: descriptor}},
                   'seed': {'runtime': {'path': runtime_path, 'sha256': runtime_sha, 'size': len(runtime)}}}
        return root, sources, source, package, runtime

    def test_catalog_checks_package_and_file_integrity_and_exact_inline_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root, sources, source, _, _ = self.catalog_fixture(directory)
            pins = verifier.catalog_audit_pins(sources)
            catalog = verifier.OriginalScriptCatalog(root, pins)
            sha = verifier.digest(source.encode())
            self.assertIn(sha, catalog.script_hashes)
            self.assertEqual(catalog.runs['1' * 32]['scripts'][sha]['source'], source)
            path = root / sources['data']['originals']['1' * 32]['package']['path']
            path.write_bytes(path.read_bytes() + b' ')
            with self.assertRaisesRegex(AssertionError, 'audited package bytes changed'):
                verifier.OriginalScriptCatalog(root, pins)

    def test_catalog_never_adopts_post_audit_self_consistent_repin_or_fake_sdk_package(self):
        import base64
        for attack in ('repin', 'remove-runtime', 'fake-package', 'replace-run'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, sources, _, package, runtime = self.catalog_fixture(directory)
                pins = verifier.catalog_audit_pins(sources)
                changed = copy.deepcopy(sources['manifest'])
                old_path = sources['data']['originals']['1' * 32]['package']['path']
                if attack == 'remove-runtime':
                    del changed['files'][sources['seed']['runtime']['path']]
                else:
                    fake = copy.deepcopy(package)
                    fake['run_id'] = '2' * 32 if attack in ('fake-package', 'replace-run') else '1' * 32
                    body = b'<script>(' + runtime.rstrip() + b')();</script>'
                    fake['files']['index.html'] = {'mime': 'text/html', 'base64': base64.b64encode(body).decode(),
                                                   'size': len(body), 'sha256': verifier.digest(body)}
                    raw = json.dumps(fake).encode(); sha = verifier.digest(raw); path = 'originals/' + sha + '.json'
                    (root / path).write_bytes(raw)
                    if attack != 'fake-package':
                        del changed['files'][old_path]
                    changed['files'][path] = {'role': 'original', 'sha256': sha, 'size': len(raw)}
                (root / verifier.MANIFEST).write_text(json.dumps(changed))
                with self.assertRaisesRegex(AssertionError, 'Manifest changed after independent audit'):
                    verifier.OriginalScriptCatalog(root, pins)

    def test_catalog_requires_unique_runtime_and_pinned_run_entry_descriptors(self):
        for attack in ('no-runtime', 'runtime-descriptor', 'second-runtime', 'run-binding', 'entry-hash', 'package-hash'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, sources, _, _, _ = self.catalog_fixture(directory)
                changed = copy.deepcopy(sources)
                runtime_path = changed['seed']['runtime']['path']
                if attack == 'no-runtime':
                    changed['manifest']['files'][runtime_path]['role'] = 'not-runtime'
                elif attack == 'second-runtime':
                    other = 'viewer/' + '0' * 64 + '.js'
                    changed['manifest']['files'][other] = copy.deepcopy(changed['manifest']['files'][runtime_path])
                    (root / other).write_bytes((root / runtime_path).read_bytes())
                elif attack == 'runtime-descriptor':
                    changed['seed']['runtime']['sha256'] = '0' * 64
                elif attack == 'run-binding':
                    changed['data']['originals']['2' * 32] = changed['data']['originals'].pop('1' * 32)
                    changed['data']['runs'] = [{'id': '2' * 32}]
                elif attack == 'entry-hash':
                    changed['data']['originals']['1' * 32]['entry_sha256'] = '0' * 64
                elif attack == 'package-hash':
                    changed['data']['originals']['1' * 32]['package']['sha256'] = '0' * 64
                (root / verifier.MANIFEST).write_text(json.dumps(changed['manifest']))
                with self.assertRaises(AssertionError):
                    verifier.OriginalScriptCatalog(root, verifier.catalog_audit_pins(changed))

    def test_post_audit_fake_package_added_without_manifest_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, sources, _, package, _ = self.catalog_fixture(directory)
            pins = verifier.catalog_audit_pins(sources)
            package['run_id'] = '2' * 32
            raw = json.dumps(package).encode(); path = 'originals/' + verifier.digest(raw) + '.json'
            (root / path).write_bytes(raw)
            with self.assertRaisesRegex(AssertionError, 'Exact bundle inventory differs'):
                verifier.OriginalScriptCatalog(root, pins)

    def test_audit_pin_snapshot_is_immutable_after_sources_object_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, sources, source, _, _ = self.catalog_fixture(directory)
            pins = verifier.catalog_audit_pins(sources)
            sources['manifest']['files'].clear(); sources['data']['originals'].clear()
            catalog = verifier.OriginalScriptCatalog(root, pins)
            self.assertIn(verifier.digest(source.encode()), catalog.runs['1' * 32]['scripts'])
            self.assertEqual(len(catalog.trusted_script_hashes), 2)

    def test_baseline_catalog_uses_frozen_runtime_without_modifying_v2_inventory(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root, sources, _, _, runtime = self.catalog_fixture(directory)
            baseline = copy.deepcopy(sources['manifest']); baseline['version'] = 2
            del baseline['files'][sources['seed']['runtime']['path']]
            (root / sources['seed']['runtime']['path']).unlink(); (root / 'viewer').rmdir()
            (root / verifier.MANIFEST).write_text(json.dumps(baseline))
            sources.update(baseline_manifest=baseline, baseline_data=copy.deepcopy(sources['data']), frozen_runtime=runtime)
            with patch.dict(verifier.FROZEN_PROGRAMS, {'web/preview-runtime.js': verifier.digest(runtime)}):
                pins = verifier.catalog_audit_pins(sources, baseline=True)
                catalog = verifier.OriginalScriptCatalog(root, pins)
                self.assertEqual(len(catalog.trusted_script_hashes), 2)
                self.assertEqual(json.loads((root / verifier.MANIFEST).read_text()), baseline)
                changed = json.loads(pins); changed['frozen_runtime_base64'] = 'aW52YWxpZA=='
                with self.assertRaisesRegex(AssertionError, 'frozen runtime bytes differ'):
                    verifier.OriginalScriptCatalog(root, json.dumps(changed))

    def test_exact_native_context_source_and_two_sdk_owners_earn_model_proof(self):
        proof = verifier.model_exception_proof(*self.fixture())
        self.assertEqual(proof['run_id'], '1' * 32)
        self.assertEqual(proof['frame_id'], 'inner')
        self.assertEqual(proof['shell_frame_id'], 'shell')
        self.assertEqual(proof['script_sha256'], verifier.digest(b'const radius = 0; radius = 1;'))

    def test_missing_or_mismatched_native_evidence_fails_closed(self):
        cases = [(0, 'uniqueId', None), (0, 'origin', 'https://untrusted.example'),
                 (1, 'executionContextId', 99), (1, 'context_unique_id', 'reused-context'),
                 (1, 'hash', '0' * 64), (2, 'parentId', 'other-frame'), (2, 'session_id', 'other-session'),
                 (4, 'nodeName', 'DIV'), (4, 'sandbox', 'allow-scripts allow-same-origin'),
                 (4, 'referrerpolicy', None), (4, 'has_srcdoc', False)]
        for index, key, value in cases:
            with self.subTest(index=index, key=key):
                evidence = self.fixture(); evidence[index][key] = value
                self.assertIsNone(verifier.model_exception_proof(*evidence))
        evidence = self.fixture(); evidence[0]['auxData']['isDefault'] = False
        self.assertIsNone(verifier.model_exception_proof(*evidence))

    def test_run_binding_cannot_be_inferred_from_srcdoc_or_title_alone(self):
        for key, value in [('run_id', 'unknown'), ('host_direct', False), ('shell_count', 2),
                           ('host_class', 'arbitrary-host'), ('report_frame_id', 'other-report'),
                           ('sandbox', 'allow-scripts allow-same-origin'), ('title', 'original')]:
            with self.subTest(key=key):
                evidence = self.fixture(); evidence[3]['binding'][key] = value
                self.assertIsNone(verifier.model_exception_proof(*evidence))

    def test_trusted_diagnostics_inside_original_frame_are_not_model_code(self):
        evidence = self.fixture()
        evidence[5] = "addEventListener('error', reportTrustedDiagnostic);"
        evidence[1]['hash'] = verifier.digest(evidence[5].encode())
        self.assertIsNone(verifier.model_exception_proof(*evidence))

    def test_model_attribution_without_trusted_runtime_exclusions_fails_closed(self):
        evidence = self.fixture(); evidence[6].trusted_script_hashes.clear()
        self.assertIsNone(verifier.model_exception_proof(*evidence))
        del evidence[6].trusted_script_hashes
        self.assertIsNone(verifier.model_exception_proof(*evidence))

    def test_trusted_runtime_bytes_still_fail_if_model_delivered_identical_text(self):
        evidence = self.fixture()
        evidence[6].trusted_script_hashes = {verifier.digest(evidence[5].encode())}
        self.assertIsNone(verifier.model_exception_proof(*evidence))

    def test_matching_hash_without_exact_source_is_not_sufficient(self):
        evidence = self.fixture(); evidence[5] += '\n'
        self.assertIsNone(verifier.model_exception_proof(*evidence))

    def test_pageerrors_remain_visible_and_unknown_srcdoc_errors_fail(self):
        from types import SimpleNamespace
        ledger = verifier.BrowserErrorLedger()
        ledger.pageerror(SimpleNamespace(stack='TypeError at about:srcdoc:324:32'))
        self.assertTrue(ledger)
        self.assertEqual(ledger.receipt['playwright_pageerrors'][0]['attribution'], 'trusted-or-unknown')
        self.assertEqual(list(ledger), ['TypeError at about:srcdoc:324:32'])

    def test_exact_native_description_matches_once_and_proof_survives_teardown(self):
        from types import SimpleNamespace
        ledger = verifier.BrowserErrorLedger()
        stack = 'TypeError: Assignment to constant variable.\n    at resize (about:srcdoc:324:32)'
        proof = verifier.model_exception_proof(*self.fixture())
        ledger.native_errors.append({'description': stack, 'attribution': 'model-original', 'proof': proof})
        ledger.pageerror(SimpleNamespace(stack=stack))
        self.assertFalse(ledger)
        self.assertEqual(ledger.receipt['playwright_pageerrors'][0]['proof'], proof)
        ledger.pageerror(SimpleNamespace(stack=stack))
        self.assertTrue(ledger)  # A second identical error has no second native proof.
        self.assertEqual(ledger.receipt['playwright_pageerrors'][1]['attribution'], 'trusted-or-unknown')
        self.assertEqual(len(ledger.receipt['playwright_pageerrors']), 2)
        self.assertEqual(ledger.receipt['native_exceptions'][0]['proof']['package_sha256'], '2' * 64)

    def test_native_observer_keeps_immutable_proof_after_context_and_frame_destruction(self):
        context, script, frame, shell, owner, source, catalog = self.fixture()
        frame['owner'] = owner
        ledger = verifier.BrowserErrorLedger()
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.catalog, observer.ledger = catalog, ledger
        observer.contexts = {('native-session', 3): context}
        observer.scripts = {('native-session', 'script'): script}
        observer.frames = {'inner': frame, 'shell': shell}
        observer.sources = {('native-session', 'script'): source}
        record = {'description': 'original failure', 'attribution': 'trusted-or-unknown',
                  'session_id': 'native-session', 'context': context,
                  'native': {'exceptionDetails': {'scriptId': 'script', 'executionContextId': 3}}}
        observer.exception_evidence = [record]; ledger.native_errors.append(record)
        observer.reclassify()
        proof = copy.deepcopy(record['proof']); evidence = copy.deepcopy(record['evidence'])
        frame['parentId'] = 'mutated-after-error'
        observer.frames.clear(); observer.contexts.clear(); observer.scripts.clear(); observer.sources.clear()
        observer.reclassify()
        self.assertEqual(record['proof'], proof)
        self.assertEqual(record['evidence'], evidence)
        self.assertFalse(ledger)
        self.assertEqual(ledger.receipt['model_original_errors'], [record])

    def test_trusted_native_same_text_console_and_unmatched_native_errors_still_fail(self):
        from types import SimpleNamespace
        ledger = verifier.BrowserErrorLedger(); stack = 'same complete stack'
        ledger.native_errors.extend([{'description': stack, 'attribution': 'model-original', 'proof': {}},
                                     {'description': stack, 'attribution': 'trusted-or-unknown'}])
        ledger.pageerror(SimpleNamespace(stack=stack))
        self.assertTrue(ledger)
        ledger.append('console error remains unverified')
        self.assertIn('console error remains unverified', list(ledger))


if __name__ == '__main__':
    unittest.main()
