"""Focused helper utilities only; synthetic inputs are never performance proof."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_public_snapshot as verifier


class PerformanceHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.site = Path(self.temporary.name) / 'site'
        self.site.mkdir()
        self.entry = self.site / 'index.html'
        self.html = b'<!doctype html><p>' + b'hello ' * 200 + b'</p>'
        self.entry.write_bytes(self.html)
        self.viewer = Path(self.temporary.name) / 'app.js'
        self.viewer.write_text('trusted viewer')
        self.output = Path(self.temporary.name) / 'output'

    @staticmethod
    def get(url, encoding='identity'):
        with urlopen(Request(url, headers={'Accept-Encoding': encoding}), timeout=5) as response:
            return dict(response.headers), response.read()

    def test_real_gzip_and_legacy_identity_transport(self):
        with verifier.serve_report(self.entry, {}, gzip_html=True) as url:
            headers, body = self.get(url, 'gzip')
            self.assertEqual(headers['Content-Encoding'], 'gzip')
            self.assertEqual(headers['Vary'], 'Accept-Encoding')
            self.assertEqual(gzip.decompress(body), self.html)
            self.assertEqual(int(headers['Content-Length']), len(body))
            self.assertEqual(headers['X-Report-SHA256'], hashlib.sha256(self.html).hexdigest())
            headers, body = self.get(url)
            self.assertNotIn('Content-Encoding', headers)
            self.assertEqual(body, self.html)
        with verifier.serve_report(self.entry, {}) as url:
            headers, body = self.get(url, 'gzip')
            self.assertNotIn('Content-Encoding', headers)
            self.assertEqual(body, self.html)

    def test_changed_document_and_undeclared_requests_rejected(self):
        with verifier.serve_report(self.entry, {}, gzip_html=True) as url:
            self.entry.write_bytes(b'changed')
            with self.assertRaises(HTTPError) as result:
                self.get(url, 'gzip')
            self.assertEqual(result.exception.code, 409)
            with self.assertRaises(HTTPError) as result:
                self.get(url + 'not-declared')
            self.assertEqual(result.exception.code, 404)

    def test_media_is_hashed_not_gzipped(self):
        payload = b'already-compressed-image'
        digest = hashlib.sha256(payload).hexdigest()
        name = 'media/' + digest + '.jpg'
        (self.site / 'media').mkdir()
        (self.site / name).write_bytes(payload)
        data = {'assets': {name: {'sha256': digest, 'size': len(payload), 'mime': 'image/jpeg'}}}
        with verifier.serve_report(self.entry, data, gzip_html=True) as url:
            headers, body = self.get(url + name, 'gzip')
            self.assertNotIn('Content-Encoding', headers)
            self.assertEqual(body, payload)

    def test_focused_arguments_bounded_and_trust_separate(self):
        common = ['--site', str(self.site), '--output', str(self.output)]
        legacy = verifier.arguments(common)
        self.assertFalse(legacy.performance_only)
        self.assertFalse(legacy.ux_only)
        verifier.arguments(common + ['--performance-only', '--timing-repeats', '3'])
        paired = verifier.arguments(common + ['--performance-only', '--baseline-site', str(self.site),
                                               '--baseline-viewer-script', str(self.viewer)])
        self.assertEqual(paired.baseline_viewer_script, self.viewer)
        for extra in (['--performance-only', '--timing-repeats', '4'],
                      ['--performance-only', '--ux-only'],
                      ['--performance-only', '--baseline-site', str(self.site)],
                      ['--baseline-site', str(self.site), '--baseline-viewer-script', str(self.viewer)]):
            with self.subTest(extra=extra), self.assertRaises(AssertionError):
                verifier.arguments(common + extra)

    def test_summary_missing_visible_image_is_not_zero(self):
        result = verifier.performance_summary([
            {'ttfb_ms': 100, 'first_actually_visible_image_ms': None},
            {'ttfb_ms': 200, 'first_actually_visible_image_ms': 500},
            {'ttfb_ms': 300, 'first_actually_visible_image_ms': None}])
        self.assertEqual(result['ttfb_ms'], {'median': 200, 'observed': 3})
        self.assertEqual(result['first_actually_visible_image_ms'], {'median': 500, 'observed': 1})
        self.assertIsNone(result['readable_ms']['median'])
        json.dumps(result, allow_nan=False)

    def test_pair_equivalence_allows_only_managed_offline_digest_change(self):
        baseline = Path(self.temporary.name) / 'baseline'
        baseline.mkdir()
        baseline_viewer = baseline / 'trusted-app.js'
        baseline_viewer.write_text('old viewer')
        runtime = Path(self.temporary.name) / 'runtime.js'
        runtime.write_text('runtime')
        args = SimpleNamespace(site=self.site, viewer_script=self.viewer, baseline_site=baseline,
                               baseline_viewer_script=baseline_viewer, preview_runtime=runtime)
        old = {'format': 'static-media-v2', 'transport': 'external', 'runs': [{'id': 'r1'}],
               'originals': {'r1': {'status': 'no_entrypoint', 'entrypoint': None, 'entry_sha256': None,
                                    'missing': [], 'policy': 'opaque-srcdoc-v1', 'package': None}},
               'tasks': [], 'evidence': {},
               'assets': {'media/' + '3' * 64 + '.jpg': {'sha256': '3' * 64, 'size': 1, 'mime': 'image/jpeg', 'role': 'thumbnail'}},
               'offline': {'path': 'offline.html', 'sha256': '4' * 64, 'size': 1}}
        current = copy.deepcopy(old)
        current['offline'].update(sha256='5' * 64, size=2)
        def write(site, data, viewer):
            (site / 'index.html').write_text('<script>window.BENCH_SNAPSHOT=' + json.dumps(data) +
                                          ';</script><script>runtime\n' + viewer.read_text() + '</script>')
        write(baseline, old, baseline_viewer)
        write(self.site, current, self.viewer)
        # Counts/case census is tested by the real baseline run, not this tiny
        # utility fixture. Exact executable trust and paired data audits run.
        with mock.patch.object(verifier, 'validate_data'):
            sources = verifier.focused_sources(args, {})
            self.assertEqual(set(sources), {'current', 'baseline'})
            current['runs'][0]['model'] = 'changed'
            write(self.site, current, self.viewer)
            with self.assertRaises(AssertionError):
                verifier.focused_sources(args, {})

    def test_descriptor_only_audit_and_exact_runtime_viewer_trust(self):
        descriptor = {'status': 'missing_dependencies', 'entrypoint': 'index.html',
                      'entry_sha256': '1' * 64, 'missing': ['missing.png'], 'policy': 'opaque-srcdoc-v1',
                      'package': {'sha256': '2' * 64, 'size': 123, 'path': 'originals/' + '2' * 64 + '.json'}}
        data = {'format': 'static-media-v2', 'transport': 'external', 'runs': [{'id': 'r1'}],
                'originals': {'r1': descriptor}, 'evidence': {},
                'assets': {'media/' + '3' * 64 + '.jpg': {'sha256': '3' * 64, 'size': 1, 'mime': 'image/jpeg', 'role': 'thumbnail'}},
                'offline': {'path': 'offline.html', 'sha256': '4' * 64, 'size': 1}}
        html = '<script>window.BENCH_SNAPSHOT=' + json.dumps(data) + ';</script><script>runtime\nviewer</script>'
        self.assertEqual(verifier.audit_html(html, 'viewer', 'runtime', descriptors_only=True), data)
        with self.assertRaises(AssertionError):
            verifier.audit_html(html, 'changed-viewer', 'runtime', descriptors_only=True)
        for mutation in ('path', 'coverage', 'size', 'missing'):
            changed = copy.deepcopy(data)
            if mutation == 'path':
                changed['originals']['r1']['package']['path'] = '../bad.json'
            elif mutation == 'coverage':
                changed['originals'] = {}
            elif mutation == 'size':
                changed['originals']['r1']['package']['size'] = True
            else:
                changed['originals']['r1']['missing'] = ['../bad.png']
            with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                verifier.audit_external_original_descriptors(changed)


if __name__ == '__main__':
    unittest.main()
