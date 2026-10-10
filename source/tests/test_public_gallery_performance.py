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

    def test_real_gzip_entry_and_full_document(self):
        with verifier.serve_gallery(self.site, self.manifest) as (url, traffic):
            for name in ('index.html', self.full):
                headers, body = self.fetch(url + name, 'gzip')
                raw = (self.site / name).read_bytes()
                self.assertEqual(headers['Content-Encoding'], 'gzip')
                self.assertEqual(headers['Vary'], 'Accept-Encoding')
                self.assertEqual(gzip.decompress(body), raw)
                self.assertEqual(int(headers['Content-Length']), len(body))
                self.assertEqual(headers['X-Report-SHA256'], verifier.digest(raw))
                headers, body = self.fetch(url + name)
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(body, raw)
            self.assertEqual(len(traffic), 4)

    def test_js_mime_and_media_original_bytes_unchanged(self):
        with verifier.serve_gallery(self.site, self.manifest) as (url, _):
            for name, mime in ((self.js, 'text/javascript'), (self.thumb, 'image/jpeg'), (self.original, 'application/json')):
                headers, body = self.fetch(url + name, 'gzip')
                self.assertEqual(headers['Content-Type'], mime)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(body, (self.site / name).read_bytes())

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
                'assets': {self.thumb: {'width': 400, 'height': 850}}, 'originals': {'a': {'status': 'no_entrypoint'}}}
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
                'assets': {self.thumb: {'width': 400, 'height': 850}}, 'originals': {'a': {'status': 'no_entrypoint'}},
                'offline': {'path': 'offline.html', 'sha256': '1'*64, 'size': 100}}
        full_info = {k: self.files[self.full][k] for k in ('sha256', 'size')}
        full_info['path'] = self.full
        runtime_info = {k: self.files[self.js][k] for k in ('sha256', 'size')}
        runtime_info['path'] = self.js
        seed = {'version': 3, 'format': 'progressive-static-v3', 'tasks': [{'id': 'crocodile', 'name': 'Task'}],
                'runs': verifier.project_runs(data), 'defaults': {'task_id': 'crocodile', 'purpose': 'benchmark', 'page_size': 8},
                'counts': {'runs': 1, 'full_runs': 1, 'benchmark': 1, 'full_benchmark': 1, 'reviews': 0},
                'full': {**full_info, 'script_sha256': verifier.digest(b'trusted-program')},
                'runtime': runtime_info, 'offline': data['offline']}
        return data, seed, full_info, runtime_info

    def render_fixture(self, seed):
        payload = json.dumps(seed).replace('<', '\\u003c')
        model = escape(seed['runs'][0]['model'])
        return ('<style>trusted-css</style><article class="evidence-card" data-run-id="a">'
                '<h2 class="work-name">' + model + '</h2><p class="work-meta">tool · 2026-10-08</p>'
                '<p class="work-status">会话 failed · 入口 fail · 评估 completed</p>'
                '<button data-run-original="a" disabled></button><button data-select-run="a"></button>'
                '<button data-zoom-run="a" disabled></button><button data-detail-run="a"></button></article>'
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


if __name__ == '__main__':
    unittest.main()
