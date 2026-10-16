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
from unittest import mock
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

    def test_repair_role_is_only_v3_and_only_content_addressed_json(self):
        repair = self.add_addressed('originals', b'{"derived":true}', 'json', 'application/json', 'repair')
        self.write_manifest()
        self.assertEqual(verifier.checked_manifest(self.site, 3)['files'][repair]['role'], 'repair')
        for name in (self.js, self.full):
            (self.site / name).unlink()
            del self.files[name]
        (self.site / 'viewer').rmdir()
        self.manifest.update(version=2, format='static-media-v2')
        self.write_manifest()
        with self.assertRaisesRegex(AssertionError, 'Original/repair role'):
            verifier.checked_manifest(self.site, 2)
        self.manifest.update(version=3, format='progressive-static-v3')
        self.files[repair]['mime'] = 'text/javascript'
        self.write_manifest()
        with self.assertRaisesRegex(AssertionError, 'Original/repair role'):
            verifier.checked_manifest(self.site, 3)

    def test_repair_validation_requires_exact_manifest_role_set(self):
        repair = self.add_addressed('originals', b'{"derived":true}', 'json', 'application/json', 'repair')
        originals = {'run': {'status': 'ready'}}
        repairs = {'run': {'descriptor': {'package': {'path': repair}}}}
        validator = mock.Mock(return_value={repair})
        self.assertEqual(verifier.checked_repairs({'repairs': repairs}, self.manifest, self.site,
                                                 originals, validator), (repairs, {repair}))
        validator.assert_called_once_with(repairs, {self.original: (self.site / self.original).read_bytes(),
                                                   repair: (self.site / repair).read_bytes()}, originals)
        for returned in (set(), {repair, self.original}, {self.original}, [repair]):
            with self.subTest(returned=returned), self.assertRaisesRegex(AssertionError, 'exactly the repair-role'):
                verifier.checked_repairs({'repairs': repairs}, self.manifest, self.site, originals,
                                         mock.Mock(return_value=returned))
        # A relabelled original or an undeclared extra derived asset cannot be silently ignored.
        self.files[repair]['role'] = 'original'
        with self.assertRaisesRegex(AssertionError, 'exactly the repair-role'):
            verifier.checked_repairs({'repairs': repairs}, self.manifest, self.site, originals, validator)

    def test_optional_repairs_absence_is_legacy_compatible_but_unknown_or_empty_is_not(self):
        validator = mock.Mock(return_value=set())
        self.assertEqual(verifier.checked_repairs({}, self.manifest, self.site, {}, validator), ({}, set()))
        for repairs in ({}, [], None, {'unknown': {}}):
            validator.reset_mock()
            with self.subTest(repairs=repairs), self.assertRaises(AssertionError):
                verifier.checked_repairs({'repairs': repairs}, self.manifest, self.site, {}, validator)
            validator.assert_not_called()
        validator.side_effect = AssertionError('unknown recipe must fail')
        with self.assertRaisesRegex(AssertionError, 'unknown recipe'):
            verifier.checked_repairs({'repairs': {'run': {}}}, self.manifest, self.site, {'run': {}}, validator)

    def test_strict_seed_rejects_duplicate_top_level_nested_and_nonfinite_fields(self):
        for raw in ('{"repairs":{},"repairs":{}}', '{"repairs":{"run":{},"run":{}}}',
                    '{"repairs":{"run":{"version":1,"version":2}}}', '{"x":NaN}'):
            parsed = verifier.GalleryHTML()
            parsed.feed('<script>window.BENCH_GALLERY=' + raw + ';</script><script>void 0;</script>')
            with self.subTest(raw=raw), self.assertRaises(AssertionError):
                verifier.gallery_seed(parsed)

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

    def test_cold_default_repair_allows_only_derived_package_not_original_or_double_fetch(self):
        repair = self.add_addressed('originals', b'{"derived":true}', 'json', 'application/json', 'repair')
        row = {'id': 'visible', 'image': {'thumb': self.thumb}, 'original': {
            'status': 'no_entrypoint', 'package': {'path': self.original}}}
        descriptor = {'status': 'ready', 'package': {'path': repair}}
        seed = {'runtime': {'path': self.js}, 'runs': [row], 'repairs': {'visible': {'descriptor': descriptor}}}
        url = 'https://example.test/'
        requests = lambda names: [{'url': url + name, 'method': 'GET'} for name in names]
        good = ['index.html', self.thumb, self.js, repair]
        self.assertEqual(verifier.validate_requests(requests(good), url, self.manifest, seed,
                         cold=True, viewport='phone', preview_ids=['visible']), good)
        for names in (good + [self.original], good[:-1] + [self.original], good + [repair]):
            with self.subTest(names=names), self.assertRaises(AssertionError):
                verifier.validate_requests(requests(names), url, self.manifest, seed,
                                           cold=True, viewport='phone', preview_ids=['visible'])
        self.assertEqual(verifier.original_intent_counts(seed, ['visible', 'visible']), {repair: 2})
        self.assertEqual(row['original']['status'], 'no_entrypoint')

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
        # Mirrors the real builder: '*' lists one score sheet per available
        # scope with its own exact scope, a single source renders that scope, and
        # an empty default renders the unregistered sheet.
        source = seed['defaults']['score_source']
        summaries = seed['runs'][0].get('scores', [])
        if source == '*' and summaries:
            # Wire rows are [scope_index, review_index, values]; the rendered
            # scope attribute is the exact scope key from the scope table.
            scopes = [verifier.v2.scope_key(seed['score_scopes'][entry[0]]) for entry in summaries]
        else:
            scopes = [source]
        sheets = []
        for scope in scopes:
            cells = ''.join('<div class="score-cell" data-dimension="' + key + '"><dt>' + label + '</dt><dd>未评审</dd></div>'
                            for key, label in verifier.v2.SCORE_LABELS.items())
            sheets.append('<div class="work-score" data-score-scope="' + escape(scope or '', quote=True) + '"><dl>' + cells + '</dl></div>')
        return ('<style>trusted-css</style><article class="evidence-card" data-run-id="a" data-model-route="' + model + '">'
                '<div class="work-heading"><h2 class="work-name" aria-label="' + model + '" title="' + model + '">' + model + '</h2></div>'
                + ''.join(sheets) +
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

class PublicRouteHelperTests(unittest.TestCase):
    def test_native_ledger_is_attached_to_the_page_used_by_pagination(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        page = SimpleNamespace(on=Mock())
        args = SimpleNamespace(browser_catalog_pins='audited-pins', _original_script_catalog=object())
        with patch.object(verifier, 'NativeExceptionObserver'):
            ledger = verifier.observe_browser_errors(object(), page, args)
        self.assertIs(page.bench_error_ledger, ledger)
        self.assertIs(page.on.call_args.args[1].__self__, ledger)

    def test_every_pagination_teardown_settles_its_own_inflight_native_reads(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        events = []
        state = {'page': 2}
        previous, next_page, grid, history = Mock(), Mock(), Mock(), Mock()
        previous.count.return_value = next_page.count.return_value = 1
        previous.is_enabled.side_effect = lambda: state['page'] > 0
        next_page.is_enabled.return_value = True
        def move(delta):
            events.append('previous' if delta < 0 else 'next')
            state['page'] += delta
        previous.click.side_effect = lambda: move(-1)
        next_page.click.side_effect = lambda: move(1)
        grid.count.side_effect = lambda: int(state['page'] == 1)
        history.is_visible.return_value = False
        pages = Mock()
        pages.filter.side_effect = lambda **kw: previous if kw['has_text'] == '上一页' else next_page
        def locator(selector):
            return pages if selector.startswith('#public-pages') else history if selector == '#public-history' else grid
        page = SimpleNamespace(bench_error_ledger=object(), locator=locator, evaluate=lambda _: 3)
        with patch.object(verifier, 'settle_observer', side_effect=lambda ledger, page: events.append('settle')):
            verifier.reach_public_run(page, {'id': 'concrete-run'})
        self.assertEqual(events, ['settle', 'settle', 'previous', 'settle', 'previous', 'settle', 'next'])

    def test_open_run_checks_url_filter_without_retyping_and_remounting(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        row = {'task_id': 'blackhole', 'purpose': 'benchmark', 'model': 'opencode/model-free'}
        for actual in (row['model'], 'wrong-model'):
            with self.subTest(actual=actual):
                model = Mock()
                model.input_value.return_value = actual
                page = SimpleNamespace(goto=Mock(), locator=Mock(return_value=model))
                with patch.object(verifier, 'public_ready'), patch.object(verifier, 'model_menu'), patch.object(verifier, 'reach_public_run') as reach:
                    if actual == row['model']:
                        verifier.open_public_run(page, 'https://report.example/', row)
                        reach.assert_called_once_with(page, row)
                    else:
                        with self.assertRaisesRegex(AssertionError, 'model query'):
                            verifier.open_public_run(page, 'https://report.example/', row)
                        reach.assert_not_called()
                model.fill.assert_not_called()
                self.assertIn('model_query=opencode%2Fmodel-free', page.goto.call_args.args[0])
                self.assertEqual(page.goto.call_args.kwargs, {'wait_until': 'networkidle'})


class ActionPairingTests(unittest.TestCase):
    def fixture(self, states=('error', 'loaded')):
        from types import SimpleNamespace
        from unittest.mock import Mock
        rows = [{'id': str(i), 'task_id': 'crocodile', 'purpose': 'benchmark', 'registered_media': True,
                 'original': {'status': 'ready', 'package': {'path': f'originals/{i}.json'}}}
                for i in range(len(states))]
        sources = {'seed': {'defaults': {'task_id': 'crocodile'}, 'runs': rows},
                   'baseline_data': {'originals': {r['id']: copy.deepcopy(r['original']) for r in rows}}}
        page = Mock()
        def locator(selector):
            element = Mock()
            element.count.return_value = 1
            index = next(i for i, row in enumerate(rows) if f'="{row["id"]}"' in selector)
            element.get_attribute.return_value = states[index]
            element.inner_text.return_value = 'Frozen runtime refuses escaped script'
            return element
        page.locator.side_effect = locator
        context = Mock()
        return rows, sources, SimpleNamespace(), context, page

    def test_refusal_selects_one_shared_subject_before_cold_measurements(self):
        from unittest.mock import patch
        rows, sources, args, context, page = self.fixture()
        with patch.object(verifier, 'decode_score_rows', return_value=rows), patch.object(
                verifier, 'observed_context', return_value=(context, page, [], [])) as observed:
            row, attempts = verifier.select_action_pair(object(), 'https://report.example/', sources, args)
        self.assertIs(row, rows[1])
        self.assertEqual([r['state'] for r in attempts], ['error', 'loaded'])
        self.assertIn('Frozen runtime refuses', attempts[0]['refusal'])
        self.assertIsNone(attempts[1]['refusal'])
        self.assertEqual(page.goto.call_count, 2)
        self.assertEqual(observed.call_args.kwargs, {'original': True, 'baseline': True})
        context.close.assert_called_once()

    def test_timeout_and_javascript_errors_are_not_candidate_refusals(self):
        from unittest.mock import patch
        for timeout in (True, False):
            with self.subTest(timeout=timeout):
                rows, sources, args, context, page = self.fixture()
                errors = [] if timeout else ['unattributed native failure']
                if timeout:
                    page.wait_for_function.side_effect = TimeoutError('probe did not settle')
                with patch.object(verifier, 'decode_score_rows', return_value=rows), patch.object(
                        verifier, 'observed_context', return_value=(context, page, [], errors)):
                    with self.assertRaises(TimeoutError if timeout else AssertionError):
                        verifier.select_action_pair(object(), 'https://report.example/', sources, args)
                self.assertEqual(page.goto.call_count, 1)
                context.close.assert_called_once()

    def test_pair_requires_the_same_package_and_a_real_loaded_candidate(self):
        from unittest.mock import patch
        for mismatch in (True, False):
            with self.subTest(mismatch=mismatch):
                rows, sources, args, context, page = self.fixture(('error',))
                if mismatch:
                    sources['baseline_data']['originals']['0']['package']['path'] = 'originals/other.json'
                with patch.object(verifier, 'decode_score_rows', return_value=rows), patch.object(
                        verifier, 'observed_context', return_value=(context, page, [], [])):
                    with self.assertRaisesRegex(AssertionError, 'package differs' if mismatch else 'actually starts'):
                        verifier.select_action_pair(object(), 'https://report.example/', sources, args)
                context.close.assert_called_once()

    def test_all_repeats_and_both_legs_receive_the_same_action_row(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        row = {'id': 'shared-original'}
        args = SimpleNamespace(trust_root=Path('/trusted'))
        urls = {'baseline': 'https://baseline.example/', 'baseline_actions': 'https://actions.example/',
                'current': 'https://current.example/'}
        def sample(browser, url, sources, args, profile, viewport, repeat):
            value = 50 if url == urls['current'] else 100
            return {'profile': profile, 'viewport': viewport, 'first_actually_visible_image_ms': value,
                    'interactive_ms': value}
        def summarized(samples):
            return {key: {'median': samples[0][key]} for key in ('first_actually_visible_image_ms', 'interactive_ms')}
        action = Mock(return_value={'primary_original_run_id': row['id']})
        report = {}
        with patch.object(verifier, 'select_action_pair', return_value=(row, [])) as select, patch.object(
                verifier, 'gallery_sample', side_effect=sample), patch.object(verifier, 'baseline_sample', side_effect=sample), patch.object(
                verifier, 'summary', side_effect=summarized), patch.object(verifier, 'action_sample', action):
            verifier.paired_performance(object(), urls, {}, args, report)
        select.assert_called_once()
        self.assertEqual(action.call_count, len(verifier.PROFILES) * len(verifier.VIEWPORTS) * 3 * 2)
        self.assertTrue(all(call.args[-1] is row for call in action.call_args_list))
        self.assertEqual(report['performance']['action_pairing']['run_id'], row['id'])
        self.assertTrue(all(c['pass'] for c in report['performance']['comparisons']))


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
                self.assertEqual(methods[:6], ['Runtime.enable', 'Debugger.enable', 'Debugger.setSkipAllPauses',
                    'Page.enable', 'Page.getFrameTree', 'Target.setAutoAttach'])
                if early_owner:
                    self.assertNotIn('DOM.getFrameOwner', methods)
                else:
                    self.assertLess(methods.index('Target.setAutoAttach'), methods.index('DOM.getFrameOwner'))
                self.assertEqual(observer.frames['shell']['binding']['run_id'], '1' * 32)
                self.assertEqual(observer.frames['shell']['session_id'], 'child')

    def test_script_source_capture_precedes_frame_tree_owner_round_trips(self):
        from types import SimpleNamespace
        context, script, frame, shell, owner, source, catalog = self.fixture()
        catalog.script_hashes = {script['hash']}
        commands = []
        observer = None
        class Session:
            def on(self, event, callback):
                pass
            def send(self, method, params=None):
                if method == 'Page.getFrameTree':
                    return {'frameTree': {'frame': {'id': 'report'}}}
                if method == 'DOM.getFrameOwner':
                    return {'backendNodeId': 1}
                if method == 'DOM.resolveNode':
                    return {'object': {'objectId': 'owner'}}
                if method == 'Runtime.callFunctionOn':
                    return {'result': {'value': copy.deepcopy(shell['binding'])}}
                if method != 'Target.sendMessageToTarget':
                    return {}
                command = json.loads(params['message'])
                commands.append(command['method'])
                sid = params['sessionId']
                def deliver(message):
                    observer.message({'sessionId': sid, 'message': json.dumps(message)})
                result = {}
                if command['method'] == 'Debugger.enable':
                    deliver({'method': 'Runtime.executionContextCreated', 'params': {'context': context}})
                    deliver({'method': 'Debugger.scriptParsed', 'params': {'scriptId': 'script', **script}})
                elif command['method'] == 'Debugger.getScriptSource':
                    result = {'scriptSource': source}
                elif command['method'] == 'Page.getFrameTree':
                    result = {'frameTree': {'frame': {'id': 'shell', 'parentId': 'report'},
                        'childFrames': [{'frame': {'id': 'inner', 'parentId': 'shell'}}]}}
                elif command['method'] == 'DOM.getFrameOwner':
                    result = {'backendNodeId': 2}
                elif command['method'] == 'DOM.describeNode':
                    result = {'node': {'nodeName': 'IFRAME', 'attributes': [
                        'sandbox', 'allow-scripts', 'referrerpolicy', 'no-referrer', 'srcdoc', 'original']}}
                deliver({'id': command['id'], 'result': result})
                return {}
        observer = verifier.NativeExceptionObserver(
            SimpleNamespace(new_cdp_session=lambda page: Session()), object(), catalog,
            verifier.BrowserErrorLedger())
        observer.attached({'sessionId': 'native-session', 'targetInfo': {
            'type': 'iframe', 'targetId': 'shell', 'parentFrameId': 'report'}})
        self.assertLess(commands.index('Debugger.getScriptSource'), commands.index('Page.enable'))
        self.assertLess(commands.index('Debugger.getScriptSource'), commands.index('DOM.getFrameOwner'))
        observer.event('native-session', 'Runtime.exceptionThrown', {'exceptionDetails': {
            'scriptId': 'script', 'executionContextId': 3, 'text': 'original failure'}})
        record = observer.ledger.native_errors[0]
        self.assertEqual(record['attribution'], 'model-original')
        self.assertEqual(record['proof']['context_unique_id'], context['uniqueId'])
        self.assertEqual(record['proof']['session_id'], 'native-session')
        # Already captured source/dual-owner/context proof survives late teardown;
        # it is not manufactured by a second matching exception.
        observer.contexts.clear(); observer.frames.clear(); observer.scripts.clear(); observer.sources.clear()
        observer.reclassify()
        self.assertEqual(record['proof']['script_sha256'], script['hash'])
        self.assertFalse(observer.ledger)

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

    def owner_observer(self, root_owner='complete'):
        """Deterministic native command transport; child replies are delivered explicitly."""
        context, script, _, shell, _, source, catalog = self.fixture()
        catalog.script_hashes = {script['hash']}
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.catalog, observer.ledger = catalog, verifier.BrowserErrorLedger()
        observer.main_frame_id = 'report'
        observer.contexts, observer.scripts, observer.sources, observer.pending = {}, {}, {}, {}
        shell['session_id'] = ''
        observer.frames = {'shell': shell}
        observer.command_id, observer.exception_evidence = 0, []
        commands, hooks = [], {}
        node = {'node': {'nodeName': 'IFRAME', 'attributes': [
            'sandbox', 'allow-scripts', 'referrerpolicy', 'no-referrer', 'srcdoc', source]}}
        class Session:
            def send(self, method, params):
                if method == 'Target.sendMessageToTarget':
                    command = json.loads(params['message'])
                    commands.append((params['sessionId'], command))
                    return {}
                commands.append(('', {'method': method, 'params': params}))
                if method in hooks:
                    hooks.pop(method)()
                if method == 'DOM.getFrameOwner':
                    if params['frameId'] == 'shell':
                        return {'backendNodeId': 1}
                    if root_owner == 'error':
                        raise RuntimeError('Root frame detached')
                    return {} if root_owner == 'empty' else {'backendNodeId': 7}
                if method == 'DOM.describeNode':
                    return copy.deepcopy(node)
                if method == 'DOM.resolveNode':
                    return {'object': {'objectId': 'outer'}}
                if method == 'Runtime.callFunctionOn':
                    return {'result': {'value': copy.deepcopy(shell['binding'])}}
                if method == 'Debugger.getScriptSource':
                    return {'scriptSource': source}
                if method in ('Debugger.setSkipAllPauses', 'Runtime.releaseObject'):
                    return {}
                raise AssertionError('Unexpected command: ' + method)
        observer.session = Session()
        return observer, commands, hooks, node

    @staticmethod
    def owner_requests(commands, sid=None):
        return [command for session, command in commands if (sid is None or session == sid) and
                command['method'] == 'DOM.getFrameOwner' and command['params']['frameId'] == 'inner']

    @staticmethod
    def native_inner(observer, sid='child', parent='shell'):
        observer.event(sid, 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': parent})

    @staticmethod
    def owner_reply(observer, sid, command, result=None, error=None):
        message = {'id': command['id']}
        message['error' if error is not None else 'result'] = error if error is not None else result
        observer.message({'sessionId': sid, 'message': json.dumps(message)})

    def test_root_discovered_inner_rejects_active_cache_but_retains_complete_event_proof(self):
        observer, commands, _, node = self.owner_observer()
        context, script, _, _, owner, source, _ = self.fixture()
        observer.event('', 'Runtime.executionContextCreated', {'context': context})
        self.native_inner(observer, '')
        observer.event('', 'Debugger.scriptParsed', {'scriptId': 'script', **script})
        observer.event('', 'Runtime.exceptionThrown', {'exceptionDetails': {
            'scriptId': 'script', 'executionContextId': 3, 'text': 'historical complete event'}})
        historical = observer.ledger.native_errors[0]
        proof, evidence = copy.deepcopy(historical['proof']), copy.deepcopy(historical['evidence'])
        cached = observer.frames['inner']
        # An unscoped root cache cannot authorize a newly native child identity.
        cached['owner'] = copy.deepcopy(owner)
        observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
        self.native_inner(observer)
        self.assertIs(observer.frames['inner'], cached)
        self.assertNotIn('owner', cached)
        observer.event('child', 'Runtime.executionContextCreated', {'context': context})
        observer.event('child', 'Debugger.scriptParsed', {'scriptId': 'script', **script})
        source_command = next(c for sid, c in commands if sid == 'child' and c['method'] == 'Debugger.getScriptSource')
        self.owner_reply(observer, 'child', source_command, {'scriptSource': source})
        observer.event('child', 'Runtime.exceptionThrown', {'exceptionDetails': {
            'scriptId': 'script', 'executionContextId': 3, 'text': 'child needs its own owner'}})
        current = observer.ledger.native_errors[1]
        self.assertNotIn('proof', current)
        request = self.owner_requests(commands, 'child')[0]
        self.owner_reply(observer, 'child', request, {'backendNodeId': 8})
        describe = next(c for sid, c in commands if sid == 'child' and c['method'] == 'DOM.describeNode')
        self.owner_reply(observer, 'child', describe, node)
        self.assertEqual(cached['owner'], owner)
        self.assertEqual(current['proof']['session_id'], 'child')
        observer.event('', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'wrong-shell'})
        self.assertEqual(cached['session_id'], 'child')
        self.assertEqual(cached['parentId'], 'shell')
        self.assertEqual(len(self.owner_requests(commands, 'child')), 1)
        self.assertEqual(historical['proof'], proof)
        self.assertEqual(historical['evidence'], evidence)

    def test_failed_or_empty_root_attempt_allows_one_new_native_child_attempt(self):
        for outcome in ('error', 'empty'):
            for discovery in ('page', 'context', 'tree'):
                with self.subTest(outcome=outcome, discovery=discovery):
                    observer, commands, _, _ = self.owner_observer(outcome)
                    self.native_inner(observer, '')
                    observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
                    if discovery == 'page':
                        self.native_inner(observer)
                    elif discovery == 'tree':
                        observer.frame_tree('child', {'frame': {'id': 'inner', 'parentId': 'shell'}})
                    else:
                        observer.event('child', 'Runtime.executionContextCreated', {'context': self.fixture()[0]})
                    self.assertEqual(len(self.owner_requests(commands, '')), 1)
                    self.assertEqual(len(self.owner_requests(commands, 'child')), 1)
                    self.native_inner(observer)
                    self.assertEqual(len(self.owner_requests(commands, 'child')), 1)

    def test_owner_attempt_is_reserved_before_reentry_and_not_retried_for_same_identity(self):
        observer, commands, hooks, _ = self.owner_observer('empty')
        hooks['DOM.getFrameOwner'] = lambda: self.native_inner(observer, '')
        self.native_inner(observer, '')
        for _ in range(3):
            self.native_inner(observer, '')
        self.assertEqual(len(self.owner_requests(commands)), 1)
        observer.frames['shell']['session_id'] = 'child'
        self.native_inner(observer)
        request = self.owner_requests(commands, 'child')[0]
        self.owner_reply(observer, 'child', request, error={'message': 'Native child detached'})
        self.native_inner(observer)
        self.assertEqual(len(self.owner_requests(commands, 'child')), 1)

    def test_delayed_get_owner_and_describe_replies_cannot_cross_native_generation(self):
        for stage in ('get', 'describe'):
            with self.subTest(stage=stage):
                observer, commands, _, node = self.owner_observer()
                observer.frames['shell']['session_id'] = 'first'
                self.native_inner(observer, 'first')
                old = self.owner_requests(commands, 'first')[0]
                if stage == 'describe':
                    self.owner_reply(observer, 'first', old, {'backendNodeId': 7})
                    old = next(c for sid, c in commands if sid == 'first' and c['method'] == 'DOM.describeNode')
                observer.frames['shell']['session_id'] = 'second'
                self.native_inner(observer, 'second')
                self.owner_reply(observer, 'first', old, node if stage == 'describe' else {'backendNodeId': 7})
                self.assertNotIn('owner', observer.frames['inner'])
                describes = [c for sid, c in commands if sid == 'first' and c['method'] == 'DOM.describeNode']
                self.assertEqual(len(describes), int(stage == 'describe'))
                self.assertEqual(len(self.owner_requests(commands, 'second')), 1)
                self.assertTrue(any('stale' in row for row in observer.ledger.diagnostics))

    def test_synchronous_root_owner_reentry_rejects_stale_get_and_describe(self):
        for stage in ('DOM.getFrameOwner', 'DOM.describeNode'):
            with self.subTest(stage=stage):
                observer, commands, hooks, _ = self.owner_observer()
                def promote():
                    observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
                    self.native_inner(observer)
                hooks[stage] = promote
                self.native_inner(observer, '')
                self.assertNotIn('owner', observer.frames['inner'])
                self.assertEqual(len(self.owner_requests(commands, 'child')), 1)
                root_describes = [c for sid, c in commands if not sid and c['method'] == 'DOM.describeNode']
                self.assertEqual(len(root_describes), int(stage == 'DOM.describeNode'))

    def test_inner_before_shell_is_reconciled_before_synchronous_outer_reads(self):
        for discovery in ('page', 'attach'):
            with self.subTest(discovery=discovery):
                observer, commands, hooks, _ = self.owner_observer()
                observer.frames.clear()
                self.native_inner(observer)
                before_outer = []
                hooks['DOM.getFrameOwner'] = lambda: before_outer.extend(self.owner_requests(commands, 'child'))
                if discovery == 'page':
                    observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
                    observer.bind_report_owner('shell')
                else:
                    observer.targets = {}
                    observer.attached({'sessionId': 'child', 'targetInfo': {
                        'type': 'iframe', 'targetId': 'shell', 'parentFrameId': 'report'}})
                self.assertEqual(len(before_outer), 1)
                self.assertEqual(len(self.owner_requests(commands, 'child')), 1)

    def test_shell_promotion_alone_does_not_promote_inner_and_rejects_root_cache(self):
        observer, commands, _, _ = self.owner_observer()
        self.native_inner(observer, '')
        self.assertIn('owner', observer.frames['inner'])
        observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
        self.assertEqual(observer.frames['inner']['session_id'], '')
        self.assertNotIn('owner', observer.frames['inner'])
        self.assertEqual(self.owner_requests(commands, 'child'), [])
        # Isolated/non-default contexts are not native inner-session authority.
        context = self.fixture()[0]; context['auxData']['isDefault'] = False
        observer.event('child', 'Runtime.executionContextCreated', {'context': context})
        self.assertEqual(observer.frames['inner']['session_id'], '')
        self.assertEqual(self.owner_requests(commands, 'child'), [])

    def test_native_ancestry_generation_changes_invalidate_pending_owner_chain(self):
        observer, commands, _, node = self.owner_observer()
        observer.frames['shell']['session_id'] = 'child'
        self.native_inner(observer)
        old = self.owner_requests(commands, 'child')[0]
        self.owner_reply(observer, 'child', old, {'backendNodeId': 7})
        describe = next(c for sid, c in commands if sid == 'child' and c['method'] == 'DOM.describeNode')
        observer.frames['other'] = {'id': 'other', 'session_id': 'child', 'parentId': 'report'}
        self.native_inner(observer, parent='other')
        self.owner_reply(observer, 'child', describe, node)
        self.assertNotIn('owner', observer.frames['inner'])
        self.assertEqual(len(self.owner_requests(commands, 'child')), 2)
        self.native_inner(observer, parent='shell')
        self.assertEqual(len(self.owner_requests(commands, 'child')), 3)

    def test_unknown_root_event_cannot_borrow_promoted_child_owner(self):
        observer, commands, _, node = self.owner_observer('empty')
        context, script, _, _, _, _, _ = self.fixture()
        self.native_inner(observer, '')
        observer.event('', 'Runtime.executionContextCreated', {'context': context})
        observer.event('', 'Debugger.scriptParsed', {'scriptId': 'script', **script})
        observer.event('', 'Runtime.exceptionThrown', {'exceptionDetails': {
            'scriptId': 'script', 'executionContextId': 3, 'text': 'unknown root event'}})
        observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
        self.native_inner(observer)
        requests = self.owner_requests(commands, 'child')
        self.assertEqual(len(requests), 1)
        self.owner_reply(observer, 'child', requests[0], {'backendNodeId': 8})
        describe = next(c for sid, c in commands if sid == 'child' and c['method'] == 'DOM.describeNode')
        self.owner_reply(observer, 'child', describe, node)
        record = observer.ledger.native_errors[0]
        self.assertNotIn('proof', record)
        self.assertEqual(record['attribution'], 'trusted-or-unknown')
        self.assertEqual(list(observer.ledger), ['unknown root event'])

    def test_owner_chain_diagnostics_correlate_each_stage_without_source_bodies(self):
        for outcome in ('empty-get', 'empty-describe', 'error', 'stale', 'pending', 'cached'):
            with self.subTest(outcome=outcome):
                observer, commands, _, node = self.owner_observer()
                observer.frames['shell']['session_id'] = 'child'
                self.native_inner(observer)
                request = self.owner_requests(commands, 'child')[0]
                if outcome == 'empty-get':
                    self.owner_reply(observer, 'child', request, {})
                elif outcome == 'error':
                    self.owner_reply(observer, 'child', request, error={'message': 'Owner frame detached'})
                elif outcome == 'stale':
                    observer.frames['shell']['session_id'] = 'new-child'
                    self.native_inner(observer, 'new-child')
                    self.owner_reply(observer, 'child', request, {'backendNodeId': 7})
                elif outcome in ('empty-describe', 'cached'):
                    self.owner_reply(observer, 'child', request, {'backendNodeId': 7})
                    describe = next(c for sid, c in commands if sid == 'child' and c['method'] == 'DOM.describeNode')
                    self.owner_reply(observer, 'child', describe, {} if outcome == 'empty-describe' else node)
                notes = list(observer.ledger.diagnostics)
                chain = [row for row in notes if row.startswith('Native owner chain ')]
                self.assertTrue(chain)
                for row in chain:
                    self.assertIn("frame_id='inner'", row)
                    self.assertRegex(row, r"sid='(?:child|new-child)', frame_id='inner', command_id=\d+, generation=\d+, stage=")
                    self.assertNotIn(self.fixture()[5], row)
                stage = 'empty' if outcome.startswith('empty') else outcome
                self.assertTrue(any('stage=' + stage in row for row in chain))
                self.assertTrue(any(f'command_id={request["id"]},' in row and "sid='child'" in row for row in chain))
                proof_commands = [c for _, c in commands if c['method'].startswith('DOM.')]
                self.assertLessEqual(len(chain), 4 * len(proof_commands))
                if outcome == 'error':
                    self.assertTrue(any('Owner frame detached' in row for row in notes))
                if outcome == 'pending':
                    self.assertIn(('child', request['id']), observer.pending)
                    self.native_inner(observer)
                    self.assertEqual(observer.ledger.diagnostics, notes)

    def test_removed_and_recreated_frame_cannot_reuse_old_callback_generation(self):
        observer, commands, _, node = self.owner_observer()
        observer.frames['shell']['session_id'] = 'child'
        self.native_inner(observer)
        request = self.owner_requests(commands, 'child')[0]
        self.owner_reply(observer, 'child', request, {'backendNodeId': 7})
        old = next(c for sid, c in commands if sid == 'child' and c['method'] == 'DOM.describeNode')
        del observer.frames['inner']
        self.native_inner(observer)
        self.owner_reply(observer, 'child', old, node)
        self.assertNotIn('owner', observer.frames['inner'])
        self.assertEqual(len(self.owner_requests(commands, 'child')), 2)

    def test_recreated_native_shell_invalidates_active_inner_owner_without_sid_borrowing(self):
        observer, commands, _, node = self.owner_observer()
        observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
        self.native_inner(observer)
        request = self.owner_requests(commands, 'child')[0]
        self.owner_reply(observer, 'child', request, {'backendNodeId': 7})
        describe = next(c for sid, c in commands if sid == 'child' and c['method'] == 'DOM.describeNode')
        self.owner_reply(observer, 'child', describe, node)
        self.assertIn('owner', observer.frames['inner'])
        del observer.frames['shell']
        observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
        self.assertEqual(observer.frames['inner']['session_id'], 'child')
        self.assertNotIn('owner', observer.frames['inner'])
        self.assertEqual(len(self.owner_requests(commands, 'child')), 2)

    def test_native_navigation_loader_change_invalidates_owner_generation(self):
        observer, commands, _, _ = self.owner_observer()
        observer.frames['shell']['session_id'] = 'child'
        self.native_inner(observer)
        old = self.owner_requests(commands, 'child')[0]
        observer.event('child', 'Page.frameNavigated', {'frame': {
            'id': 'inner', 'parentId': 'shell', 'loaderId': 'new-native-loader'}})
        self.owner_reply(observer, 'child', old, {'backendNodeId': 7})
        self.assertFalse(any(c['method'] == 'DOM.describeNode' for sid, c in commands if sid == 'child'))
        self.assertNotIn('owner', observer.frames['inner'])
        self.assertEqual(len(self.owner_requests(commands, 'child')), 2)

    def test_native_root_discovery_fills_missing_inner_ancestry_without_replacing_child_sid(self):
        observer, commands, _, _ = self.owner_observer()
        observer.event('child', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
        observer.event('child', 'Runtime.executionContextCreated', {'context': self.fixture()[0]})
        self.assertNotIn('parentId', observer.frames['inner'])
        observer.event('', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'shell'})
        self.assertEqual(observer.frames['inner']['session_id'], 'child')
        self.assertEqual(observer.frames['inner']['parentId'], 'shell')
        self.assertEqual(len(self.owner_requests(commands, 'child')), 1)
        observer.event('', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'unrelated'})
        self.assertEqual(observer.frames['inner']['parentId'], 'shell')
        self.assertEqual(len(self.owner_requests(commands, 'child')), 1)

    def test_native_root_discovery_fills_missing_shell_ancestry_before_binding(self):
        observer, commands, hooks, _ = self.owner_observer()
        observer.frames.clear()
        self.native_inner(observer)
        context = copy.deepcopy(self.fixture()[0])
        context['id'], context['uniqueId'], context['auxData']['frameId'] = 9, 'native-shell-context', 'shell'
        observer.event('child', 'Runtime.executionContextCreated', {'context': context})
        observed = []
        hooks['DOM.getFrameOwner'] = lambda: observed.append(observer.frames['shell']['parentId'])
        observer.event('', 'Page.frameAttached', {'frameId': 'shell', 'parentFrameId': 'report'})
        self.assertEqual(observer.frames['shell']['session_id'], 'child')
        self.assertEqual(observer.frames['shell']['parentId'], 'report')
        self.assertEqual(observed, ['report'])
        self.assertEqual(observer.frames['shell']['binding']['report_frame_id'], 'report')

    def test_candidate_script_capture_diagnostics_correlate_without_source_bodies(self):
        observer, _, _, _ = self.owner_observer()
        context, script, _, _, _, source, _ = self.fixture()
        observer.event('', 'Runtime.executionContextCreated', {'context': context})
        parsed = {'scriptId': 'script', **script, 'executionContextAuxData': context['auxData']}
        observer.event('', 'Debugger.scriptParsed', parsed)
        self.assertEqual(observer.sources[('', 'script')], source)
        notes = [row for row in observer.ledger.diagnostics if row.startswith('Native script capture ')]
        self.assertEqual(len(notes), 2)
        for row in notes:
            self.assertIn("sid='', script_id='script', frame_id='inner'", row)
            self.assertRegex(row, r'command_id=\d+, stage=')
            self.assertNotIn(source, row)
        self.assertIn('stage=pending', notes[0])
        self.assertIn('stage=captured', notes[1])

    def test_child_forwarding_enqueues_capture_commands_without_waiting_for_each_ack(self):
        import asyncio
        observer, _, _, _ = self.owner_observer()
        loop = asyncio.new_event_loop()
        queued, acknowledgements, replies = [], [], []
        async def target_sender(method, params):
            queued.append((method, params))
            acknowledgement = loop.create_future()
            acknowledgements.append(acknowledgement)
            return await acknowledgement
        observer._target_loop, observer._target_sender, observer._target_tasks = loop, target_sender, set()
        def pump():
            barrier = loop.create_future()
            loop.call_soon(barrier.set_result, None)
            loop.run_until_complete(barrier)
        try:
            observer.send('child', 'Debugger.getScriptSource', {'scriptId': 'script'}, replies.append)
            observer.send('child', 'DOM.getFrameOwner', {'frameId': 'inner'}, replies.append)
            pump()
            self.assertEqual(len(queued), 2)
            self.assertTrue(all(not item.done() for item in acknowledgements))
            self.assertEqual(len(observer.pending), 2)
            messages = [json.loads(params['message']) for _, params in queued]
            self.assertEqual([message['method'] for message in messages], ['Debugger.getScriptSource', 'DOM.getFrameOwner'])
            # Native replies can arrive before the forwarding acknowledgements.
            for message, result in zip(messages, [{'scriptSource': 'native source'}, {'backendNodeId': 7}]):
                self.owner_reply(observer, 'child', message, result)
            self.assertEqual(replies, [{'scriptSource': 'native source'}, {'backendNodeId': 7}])
            self.assertEqual(observer.pending, {})
            for acknowledgement in acknowledgements:
                acknowledgement.set_result({})
            pump(); pump()
            self.assertEqual(observer._target_tasks, set())
        finally:
            for task in observer._target_tasks:
                task.cancel()
            pump(); pump()
            loop.close()

    def test_async_child_forwarding_failure_preserves_correlated_error_without_callback(self):
        import asyncio
        observer, _, _, _ = self.owner_observer()
        loop = asyncio.new_event_loop()
        replies = []
        async def target_sender(method, params):
            raise RuntimeError('No session with given id')
        observer._target_loop, observer._target_sender, observer._target_tasks = loop, target_sender, set()
        try:
            observer.send('child', 'Debugger.getScriptSource', {'scriptId': 'script'}, replies.append)
            barrier = loop.create_future()
            loop.call_soon(barrier.set_result, None)
            loop.run_until_complete(barrier)
            self.assertEqual(replies, [])
            self.assertEqual(observer.pending, {})
            self.assertTrue(any("Debugger.getScriptSource [sid='child', command_id=1]: No session with given id" in row for row in observer.ledger.diagnostics))
        finally:
            for task in observer._target_tasks:
                task.cancel()
            if observer._target_tasks:
                loop.run_until_complete(asyncio.gather(*observer._target_tasks, return_exceptions=True))
            loop.close()

    def test_critical_reads_send_immediately_but_forwarding_ack_is_not_native_evidence(self):
        from unittest.mock import Mock
        for identity in ('script_identity', 'owner_identity'):
            with self.subTest(identity=identity):
                observer, commands, _, _ = self.owner_observer()
                replies = []
                def capture(result):
                    replies.append(result)
                    return 'captured'
                if identity == 'script_identity':
                    capture.script_identity = ('child', 'script', 'inner')
                else:
                    capture.owner_identity = ('child', 'inner', 'shell', 'loader', 1)
                observer._target_sender = Mock(side_effect=AssertionError('Evidence was deferred'))
                observer._target_loop = Mock()
                observer._target_tasks = set()
                observer.send('child', 'Debugger.getScriptSource', {'scriptId': 'script'}, capture)
                self.assertEqual(len(commands), 1)
                self.assertEqual(commands[0][1]['method'], 'Debugger.getScriptSource')
                observer._target_sender.assert_not_called()
                observer._target_loop.create_task.assert_not_called()
                self.assertTrue(any('stage=forward-started' in x for x in observer.ledger.diagnostics))
                self.assertTrue(any('stage=forward-ack' in x for x in observer.ledger.diagnostics))
                self.assertEqual(replies, [])
                self.assertIn(('child', 1), observer.pending)
                self.owner_reply(observer, 'child', {'id': 1}, {'scriptSource': 'native source'})
                self.assertEqual(replies, [{'scriptSource': 'native source'}])
                self.assertTrue(any('Native response Debugger.getScriptSource' in x for x in observer.ledger.diagnostics))
                self.assertEqual(observer.pending, {})
                self.assertNotIn('native source', '\n'.join(observer.ledger.diagnostics))

    def test_session_detach_retires_pending_owner_and_rejects_late_response(self):
        observer, commands, _, node = self.owner_observer()
        observer.frames['shell']['session_id'] = 'child'
        self.native_inner(observer)
        request = self.owner_requests(commands, 'child')[0]
        self.owner_reply(observer, 'child', request, {'backendNodeId': 7})
        describe = next(c for sid, c in commands if sid == 'child' and c['method'] == 'DOM.describeNode')
        observer.detached({'sessionId': 'child', 'targetId': 'shell'})
        self.assertEqual(observer.pending, {})
        self.assertTrue(observer.frames['inner']['retired'])
        self.assertNotIn('owner_identity', observer.frames['inner'])
        self.assertTrue(any('session-detached' in x and f'command_id={describe["id"]}' in x for x in observer.ledger.diagnostics))
        self.owner_reply(observer, 'child', describe, node)
        self.assertNotIn('owner', observer.frames['inner'])
        self.assertTrue(any('Unmatched native response' in x for x in observer.ledger.diagnostics))
        self.native_inner(observer)
        self.assertEqual(len(self.owner_requests(commands, 'child')), 1)

    def test_frame_detach_invalidates_owner_without_changing_completed_proof(self):
        observer, _, _, _ = self.owner_observer()
        context, script, _, _, _, _, _ = self.fixture()
        observer.event('', 'Runtime.executionContextCreated', {'context': context})
        self.native_inner(observer, '')
        observer.event('', 'Debugger.scriptParsed', {'scriptId': 'script', **script})
        observer.event('', 'Runtime.exceptionThrown', {'exceptionDetails': {
            'scriptId': 'script', 'executionContextId': 3, 'text': 'completed original event'}})
        record = observer.ledger.native_errors[0]
        proof, evidence = copy.deepcopy(record['proof']), copy.deepcopy(record['evidence'])
        observer.event('', 'Page.frameDetached', {'frameId': 'shell', 'reason': 'remove'})
        self.assertTrue(observer.frames['inner']['retired'])
        self.assertNotIn('owner', observer.frames['inner'])
        self.assertEqual(record['proof'], proof)
        self.assertEqual(record['evidence'], evidence)
        self.assertEqual(record['attribution'], 'model-original')

    def test_frame_swap_does_not_retire_native_child_owner(self):
        observer, _, _, _ = self.owner_observer()
        self.native_inner(observer, '')
        identity = observer.frames['inner']['owner_identity']
        observer.event('', 'Page.frameDetached', {'frameId': 'inner', 'reason': 'swap'})
        self.assertEqual(observer.frames['inner']['owner_identity'], identity)
        self.assertNotIn('retired', observer.frames['inner'])

    def test_native_document_owner_requires_exact_frame_and_current_identity(self):
        import asyncio
        for matching, stale in ((True, False), (False, False), (True, True)):
            with self.subTest(matching=matching, stale=stale):
                observer, commands, _, node = self.owner_observer()
                observer.frames['shell']['session_id'] = 'child'
                loop = asyncio.new_event_loop()
                async def target_sender(method, params):
                    commands.append((params['sessionId'], json.loads(params['message'])))
                    return {}
                observer._target_loop, observer._target_sender, observer._target_tasks = loop, target_sender, set()
                def pump():
                    barrier = loop.create_future()
                    loop.call_soon(barrier.set_result, None)
                    loop.run_until_complete(barrier)
                try:
                    self.native_inner(observer)
                    pump(); pump()
                    document = next((c for sid, c in commands if c['method'] == 'DOM.getDocument'), None)
                    self.assertIsNotNone(document)
                    self.assertEqual(document['params'], {'depth': 4, 'pierce': False})
                    identity = observer.frames['inner']['owner_identity']
                    if stale:
                        observer.frames['inner']['loaderId'] = 'another-loader'
                        observer.reconcile_owner('inner')
                    owner_node = {**node['node'], 'frameId': 'inner' if matching else 'another-frame'}
                    self.owner_reply(observer, 'child', document, {'root': {
                        'nodeName': '#document', 'children': [{'nodeName': 'HTML', 'children': [owner_node]}]}})
                    if matching and not stale:
                        self.assertEqual(observer.frames['inner']['owner_cache_identity'], identity)
                        self.assertEqual(observer.frames['inner']['owner'], self.fixture()[4])
                    else:
                        self.assertNotIn('owner', observer.frames['inner'])
                finally:
                    for task in observer._target_tasks:
                        task.cancel()
                    pump(); pump()
                    loop.close()

    def test_root_commands_use_direct_session_and_correlate_failures(self):
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.command_id, observer.pending = 0, {}
        observer.ledger = verifier.BrowserErrorLedger()
        commands, results = [], []
        class Session:
            def send(self, method, params):
                commands.append((method, params))
                if method == 'Debugger.getScriptSource':
                    return {'scriptSource': 'native root source'}
                if method == 'DOM.getFrameOwner':
                    raise RuntimeError('Frame was detached')
                return {}
        observer.session = Session()
        observer.send('', 'Debugger.getScriptSource', {'scriptId': 'root-script'}, results.append)
        self.assertEqual(commands, [('Debugger.getScriptSource', {'scriptId': 'root-script'})])
        self.assertEqual(results, [{'scriptSource': 'native root source'}])
        self.assertEqual(observer.pending, {})
        observer.send('', 'DOM.getFrameOwner', {'frameId': 'inner'}, results.append)
        self.assertEqual(len(results), 1)
        self.assertIn("DOM.getFrameOwner [sid='', command_id=2]", observer.ledger.diagnostics[-1])
        observer.exception_evidence = []
        observer.send('child', 'DOM.describeNode', {'backendNodeId': 1}, results.append)
        observer.message({'sessionId': 'child', 'message': json.dumps({
            'id': 3, 'error': {'message': 'Node was detached'}})})
        self.assertEqual(observer.pending, {})
        self.assertEqual(len(results), 1)
        self.assertIn("DOM.describeNode [sid='child', command_id=3]", observer.ledger.diagnostics[-1])

    def test_root_native_source_and_inner_owner_earn_exact_root_provenance(self):
        context, script, frame, shell, owner, source, catalog = self.fixture()
        frame['session_id'] = shell['session_id'] = ''
        catalog.script_hashes = {script['hash']}
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.catalog, observer.ledger = catalog, verifier.BrowserErrorLedger()
        observer.main_frame_id = 'report'
        observer.contexts, observer.scripts, observer.sources, observer.pending = {}, {}, {}, {}
        observer.frames = {'shell': shell}
        observer.command_id, observer.exception_evidence = 0, []
        commands = []
        class Session:
            def send(self, method, params):
                commands.append((method, params))
                if method == 'DOM.getFrameOwner':
                    return {'backendNodeId': 7}
                if method == 'DOM.describeNode':
                    return {'node': {'nodeName': 'IFRAME', 'attributes': [
                        'sandbox', 'allow-scripts', 'referrerpolicy', 'no-referrer', 'srcdoc', source]}}
                if method == 'Debugger.getScriptSource':
                    return {'scriptSource': source}
                raise AssertionError('Unexpected command: ' + method)
        observer.session = Session()
        observer.event('', 'Runtime.executionContextCreated', {'context': context})
        observer.event('', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'shell'})
        observer.event('', 'Debugger.scriptParsed', {'scriptId': 'script', **script})
        observer.event('', 'Runtime.exceptionThrown', {'exceptionDetails': {
            'scriptId': 'script', 'executionContextId': 3, 'text': 'root original failure'}})
        record = observer.ledger.native_errors[0]
        self.assertEqual(observer.frames['inner']['owner'], owner)
        self.assertEqual([method for method, _ in commands], [
            'DOM.getFrameOwner', 'DOM.describeNode', 'Debugger.getScriptSource'])
        self.assertEqual(record['attribution'], 'model-original')
        self.assertEqual(record['proof']['session_id'], '')
        self.assertEqual(record['proof']['context_unique_id'], context['uniqueId'])
        self.assertEqual(record['proof']['script_sha256'], script['hash'])
        from types import SimpleNamespace
        observer.ledger.pageerror(SimpleNamespace(stack='root original failure'))
        self.assertFalse(observer.ledger)
        proof = copy.deepcopy(record['proof'])
        observer.frames.clear(); observer.contexts.clear(); observer.scripts.clear(); observer.sources.clear()
        observer.reclassify()
        self.assertEqual(record['proof'], proof)
        observer.ledger.pageerror(SimpleNamespace(stack='root original failure'))
        self.assertEqual(list(observer.ledger), ['root original failure'])

    def test_root_inner_owner_requires_native_root_shell_ancestry(self):
        for shell_session, shell_parent in [('child', 'report'), ('', 'unrelated')]:
            with self.subTest(shell_session=shell_session, shell_parent=shell_parent):
                observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
                observer.main_frame_id = 'report'
                observer.frames = {'shell': {'id': 'shell', 'session_id': shell_session,
                                            'parentId': shell_parent}}
                observer.exception_evidence = []
                observer.ledger = verifier.BrowserErrorLedger()
                commands = []
                observer.send = lambda *args: commands.append(args)
                observer.event('', 'Page.frameAttached', {'frameId': 'inner', 'parentFrameId': 'shell'})
                self.assertEqual(commands, [])
                self.assertNotIn('owner_requested', observer.frames['inner'])

    def test_outer_binding_read_is_not_reentrant_and_survives_release_teardown(self):
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.main_frame_id = 'report'
        observer.frames = {'shell': {'id': 'shell'}}
        observer.ledger = verifier.BrowserErrorLedger()
        binding = copy.deepcopy(self.fixture()[3]['binding'])
        commands = []
        class Session:
            def send(self, method, params):
                commands.append(method)
                if method == 'DOM.getFrameOwner':
                    observer.bind_report_owner('shell')
                    return {'backendNodeId': 1}
                if method == 'DOM.resolveNode':
                    return {'object': {'objectId': 'outer-owner'}}
                if method == 'Runtime.callFunctionOn':
                    return {'result': {'value': binding}}
                if method == 'Runtime.releaseObject':
                    raise RuntimeError('Cannot find context with specified id')
                raise AssertionError('Unexpected command: ' + method)
        observer.session = Session()
        observer.bind_report_owner('shell')
        self.assertEqual(commands, ['DOM.getFrameOwner', 'DOM.resolveNode',
                                   'Runtime.callFunctionOn', 'Runtime.releaseObject'])
        self.assertEqual(observer.frames['shell']['binding'], binding)
        self.assertNotIn('binding_inflight', observer.frames['shell'])
        self.assertIn('Cannot find context', observer.ledger.diagnostics[-1])
        observer.bind_report_owner('shell')
        self.assertEqual(len(commands), 4)
        self.assertIs(observer.frames['shell']['binding'], binding)

    def test_outer_binding_never_overwrites_complete_evidence_with_incomplete_result(self):
        observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
        observer.main_frame_id = 'report'
        observer.frames = {'shell': {'id': 'shell'}}
        observer.ledger = verifier.BrowserErrorLedger()
        complete = copy.deepcopy(self.fixture()[3]['binding'])
        class Session:
            def send(self, method, params):
                if method == 'DOM.getFrameOwner':
                    return {'backendNodeId': 1}
                if method == 'DOM.resolveNode':
                    return {'object': {'objectId': 'outer-owner'}}
                if method == 'Runtime.callFunctionOn':
                    observer.frames['shell']['binding'] = complete
                    return {'result': {'value': {'host_class': None, 'host_direct': False}}}
                if method == 'Runtime.releaseObject':
                    return {}
                raise AssertionError('Unexpected command: ' + method)
        observer.session = Session()
        observer.bind_report_owner('shell')
        self.assertIs(observer.frames['shell']['binding'], complete)
        self.assertNotIn('binding_inflight', observer.frames['shell'])
        self.assertEqual(observer.ledger.diagnostics, [])

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

    def add_catalog_repair(self, root, sources, package, source):
        import base64
        package = copy.deepcopy(package)
        entry = ('<html><script>' + source + '</script></html>').encode()
        package['files']['index.html'] = {'mime': 'text/html', 'size': len(entry),
            'sha256': verifier.digest(entry), 'base64': base64.b64encode(entry).decode()}
        package['repair_fixture'] = True
        raw = json.dumps(package).encode(); sha = verifier.digest(raw); path = 'originals/' + sha + '.json'
        (root / path).write_bytes(raw)
        sources['manifest']['files'][path] = {'role': 'repair', 'sha256': sha, 'size': len(raw)}
        descriptor = {'entrypoint': 'index.html', 'entry_sha256': verifier.digest(entry), 'missing': [],
                      'package': {'path': path, 'sha256': sha, 'size': len(raw)}}
        sources['seed']['repairs'] = {'1' * 32: {'descriptor': descriptor}}
        (root / verifier.MANIFEST).write_text(json.dumps(sources['manifest']))
        return path

    def test_catalog_separates_repair_sources_and_rejects_unknown_role_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root, sources, original, package, _ = self.catalog_fixture(directory)
            repair = 'let radius = 0; radius = 1;'
            path = self.add_catalog_repair(root, sources, package, repair)
            catalog = verifier.OriginalScriptCatalog(root, verifier.catalog_audit_pins(sources))
            self.assertIn(verifier.digest(original.encode()), catalog.runs['1' * 32]['scripts'])
            self.assertNotIn(verifier.digest(repair.encode()), catalog.runs['1' * 32]['scripts'])
            self.assertIn(verifier.digest(repair.encode()), catalog.repair_script_hashes)
            self.assertEqual(catalog.repair_runs['1' * 32]['package_path'], path)
            self.assertIn(verifier.digest(repair.encode()), catalog.script_hashes)
            sources['seed'].pop('repairs')
            with self.assertRaisesRegex(AssertionError, 'audited repair descriptors'):
                verifier.OriginalScriptCatalog(root, verifier.catalog_audit_pins(sources))

    def test_repair_mode_shared_source_and_missing_mode_never_earn_original_exemption(self):
        for mode in ('repair', 'static', None, 'unknown'):
            evidence = self.fixture()
            evidence[6].repair_runs = {'1' * 32: {}}
            if mode is not None:
                evidence[3]['binding']['preview_mode'] = mode
            with self.subTest(mode=mode):
                self.assertIsNone(verifier.model_exception_proof(*evidence))
        evidence = self.fixture()
        evidence[6].repair_runs = {'1' * 32: {}}
        evidence[3]['binding']['preview_mode'] = 'original'
        self.assertIsNotNone(verifier.model_exception_proof(*evidence))
        evidence[6].repair_script_hashes = {verifier.digest(evidence[5].encode())}
        self.assertIsNone(verifier.model_exception_proof(*evidence))

    def test_repair_exception_is_failing_not_counted_as_original_even_with_identical_body(self):
        from types import SimpleNamespace
        for mode, shared in (('repair', False), ('original', True)):
            context, script, frame, shell, owner, source, catalog = self.fixture()
            shell['binding']['preview_mode'] = mode
            catalog.repair_runs = {'1' * 32: {}}
            catalog.repair_script_hashes = {script['hash']} if shared else set()
            frame['owner'] = owner
            ledger = verifier.BrowserErrorLedger()
            observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
            observer.catalog, observer.ledger = catalog, ledger
            observer.scripts = {('native-session', 'script'): script}
            observer.frames = {'inner': frame, 'shell': shell}
            observer.sources = {('native-session', 'script'): source}
            record = {'description': 'repair failure', 'attribution': 'trusted-or-unknown',
                'session_id': 'native-session', 'context': context,
                'native': {'exceptionDetails': {'scriptId': 'script', 'executionContextId': 3}}}
            observer.exception_evidence = [record]; ledger.native_errors.append(record)
            observer.reclassify()
            ledger.pageerror(SimpleNamespace(stack='repair failure'))
            self.assertTrue(ledger)
            self.assertEqual(ledger.receipt['model_original_errors'], [])
            self.assertIn(record['attribution'], ('repair', 'repair-or-shared-source'))
            self.assertNotIn('proof', record)
            self.assertEqual(list(ledger), ['repair failure'])

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

    def test_detached_source_request_cannot_borrow_later_same_hash_or_error_proof(self):
        from types import SimpleNamespace
        for missing_owner in (False, True):
            with self.subTest(missing_owner=missing_owner):
                context, script, frame, shell, owner, source, catalog = self.fixture()
                catalog.script_hashes = {script['hash']}
                frame['owner'] = owner
                if missing_owner:
                    del frame['owner']
                observer = verifier.NativeExceptionObserver.__new__(verifier.NativeExceptionObserver)
                observer.catalog, observer.ledger = catalog, verifier.BrowserErrorLedger()
                observer.contexts = {('native-session', 3): context}
                observer.scripts, observer.sources, observer.pending = {}, {}, {}
                observer.frames = {'inner': frame, 'shell': shell}
                observer.command_id, observer.exception_evidence = 0, []
                class Session:
                    def send(self, method, params):
                        if params['sessionId'] == 'native-session':
                            raise RuntimeError('No session with given id')
                        return {}
                observer.session = Session()
                observer.event('native-session', 'Debugger.scriptParsed', {'scriptId': 'script', **script})
                event = {'exceptionDetails': {'scriptId': 'script', 'executionContextId': 3,
                                             'text': 'same complete native error'}}
                observer.event('native-session', 'Runtime.exceptionThrown', event)
                unknown = observer.ledger.native_errors[0]
                self.assertEqual(observer.pending, {})
                self.assertIn("sid='native-session', script_id='script'", observer.ledger.diagnostics[0])
                self.assertIn('stage=pending', observer.ledger.diagnostics[0])
                self.assertTrue(any('No session with given id' in row and "sid='native-session', command_id=1" in row
                                    for row in observer.ledger.diagnostics))
                self.assertTrue(any('stage=error: No session with given id' in row for row in observer.ledger.diagnostics))
                # A later occurrence has its own context/session and two owners,
                # the same local script ID/hash, and a successful source response.
                later_context, later_script, later_frame, later_shell, later_owner, _, _ = self.fixture()
                later_context['uniqueId'] = 'later-native-unique'
                later_context['auxData']['frameId'] = later_frame['id'] = 'later-inner'
                later_frame['parentId'] = later_shell['id'] = 'later-shell'
                later_frame['session_id'] = later_shell['session_id'] = 'later-session'
                later_frame['owner'] = later_owner
                later_script['context_unique_id'] = later_context['uniqueId']
                observer.frames.update({'later-inner': later_frame, 'later-shell': later_shell})
                observer.contexts[('later-session', 3)] = later_context
                observer.event('later-session', 'Debugger.scriptParsed', {'scriptId': 'script', **later_script})
                observer.message({'sessionId': 'later-session', 'message': json.dumps({
                    'id': observer.command_id, 'result': {'scriptSource': source}})})
                observer.event('later-session', 'Runtime.exceptionThrown', copy.deepcopy(event))
                later = observer.ledger.native_errors[1]
                observer.ledger.pageerror(SimpleNamespace(stack='same complete native error'))
                observer.ledger.pageerror(SimpleNamespace(stack='same complete native error'))
                self.assertTrue(observer.ledger)
                self.assertEqual(unknown['attribution'], 'trusted-or-unknown')
                self.assertNotIn('proof', unknown)
                self.assertIsNone(unknown['evidence']['source_sha256'])
                self.assertEqual(later['attribution'], 'model-original')
                self.assertEqual(later['proof']['session_id'], 'later-session')
                self.assertEqual(len(observer.ledger.receipt['native_exceptions']), 2)
                self.assertEqual([row['attribution'] for row in observer.ledger.page_errors],
                                 ['trusted-or-unknown', 'model-original'])
                self.assertEqual(list(observer.ledger), ['same complete native error'])

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


class LateEvidenceAfterDetachTests(unittest.TestCase):
    """A detach must not discard evidence already in flight."""

    def test_script_source_reply_arriving_after_frame_detach_is_still_recorded(self):
        observer, commands, _, _ = NativeExceptionAttributionTests.owner_observer(
            NativeExceptionAttributionTests('test_root_discovered_inner_rejects_active_cache_but_retains_complete_event_proof'))
        observer.frames['shell']['session_id'] = 'child'
        NativeExceptionAttributionTests.native_inner(observer)
        # Capture a candidate script while the frame is alive, then detach the
        # frame before the native reply is delivered.
        def callback(value):
            # Mirrors the observer's real capture callback: store, then report.
            observer.sources[('child', 'script')] = value.get('scriptSource')
            return 'captured'
        callback.script_identity = ('child', 'script', 'inner')
        observer.send('child', 'Debugger.getScriptSource', {'scriptId': 'script'}, callback)
        # The frame-owner read from native_inner is still outstanding too, so
        # target the capture command by its method rather than by position.
        capture_key = next(key for key, value in observer.pending.items()
                           if value[0] == 'Debugger.getScriptSource')
        pending_before = dict(observer.pending)
        self.assertTrue(pending_before)
        observer.event('child', 'Page.frameDetached', {'frameId': 'inner', 'reason': 'remove'})
        self.assertTrue(observer.frames['inner']['retired'])
        # The already-dispatched command stays pending so its real reply can land.
        self.assertEqual(observer.pending, pending_before)
        observer.message({'sessionId': capture_key[0],
                          'message': json.dumps({'id': capture_key[1], 'result': {'scriptSource': 'real source'}})})
        self.assertEqual(observer.sources[('child', 'script')], 'real source')
        self.assertNotIn(capture_key, observer.pending)

    def test_session_detach_still_clears_outstanding_commands(self):
        observer, commands, _, _ = NativeExceptionAttributionTests.owner_observer(
            NativeExceptionAttributionTests('test_root_discovered_inner_rejects_active_cache_but_retains_complete_event_proof'))
        observer.frames['shell']['session_id'] = 'child'
        NativeExceptionAttributionTests.native_inner(observer)
        callback = lambda value: value
        callback.script_identity = ('child', 'script', 'inner')
        observer.send('child', 'Debugger.getScriptSource', {'scriptId': 'script'}, callback)
        self.assertTrue(observer.pending)
        # When the whole channel goes away, nothing can answer any more.
        observer.detached({'sessionId': 'child', 'targetId': 'shell'})
        self.assertEqual(observer.pending, {})


class ObserverSettleTests(unittest.TestCase):
    """Navigation must let already-issued native reads finish first."""

    def test_settle_observer_waits_for_pending_then_returns_remaining(self):
        from types import SimpleNamespace
        class Page:
            def __init__(self): self.waits = 0
            def wait_for_timeout(self, ms): self.waits += 1
        page = Page()
        ledger = verifier.BrowserErrorLedger()
        ledger.observer = SimpleNamespace(pending={('s', 1): ('m', None)})
        calls = {'n': 0}
        def pending_property():
            calls['n'] += 1
            if calls['n'] >= 3:
                ledger.observer.pending = {}
            return ledger.observer.pending
        # A plain dict cannot change on its own, so emulate the reply arriving.
        ledger.observer = SimpleNamespace(pending={('s', 1): ('m', None)})
        def wait_and_clear(ms):
            page.waits += 1
            ledger.observer.pending = {}
        page.wait_for_timeout = wait_and_clear
        remaining = verifier.settle_observer(ledger, page)
        self.assertEqual(remaining, 0)
        self.assertEqual(page.waits, 1)

    def test_settle_observer_is_a_noop_without_pending_or_observer(self):
        from types import SimpleNamespace
        class Page:
            def wait_for_timeout(self, ms): raise AssertionError('must not wait')
        page = Page()
        ledger = verifier.BrowserErrorLedger()
        self.assertEqual(verifier.settle_observer(ledger, page), 0)
        ledger.observer = SimpleNamespace(pending={})
        self.assertEqual(verifier.settle_observer(ledger, page), 0)

    def test_settle_observer_reports_commands_that_never_answered(self):
        from types import SimpleNamespace
        class Page:
            def __init__(self): self.waits = 0
            def wait_for_timeout(self, ms): self.waits += 1
        page = Page()
        ledger = verifier.BrowserErrorLedger()
        ledger.observer = SimpleNamespace(pending={('s', 1): ('m', None)})
        # Bounded: an unanswered command cannot make the verifier hang.
        self.assertEqual(verifier.settle_observer(ledger, page, timeout_ms=50), 1)
        self.assertGreater(page.waits, 0)
