"""Synthetic trust-contract regressions, not browser/performance evidence.

Fixture-only program pins stand in for the independent historical checkout;
production frozen pins are asserted separately and never rewritten on disk.
"""
import copy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest import mock

from test_gallery_site import GalleryFixture
import make_gallery
import verify_public_gallery as verifier


RUNTIME_SOURCE = 'web/preview-runtime.js'


class FrozenPinTests(unittest.TestCase):
    def test_production_frozen_pins_remain_historical(self):
        self.assertEqual(verifier.FROZEN_PROGRAMS, {
            'web/app.js': 'be40765eda0931545ce995a4e58a7ccab77c7cfd1740746cb4e293684ad23751',
            RUNTIME_SOURCE: '50f44020c5464433c28c9a27a944b83588002c435e7ce33508866afb249b67cf',
            'tests/verify_public_snapshot.py': '7c1a9b0c7ff2357c94e889cc4a84d73431b6544c27854abe650bf4551b457a80',
            'tests/verify_original_previews.py': 'e86710c7c2d9ee1e253dbb14ee0a947ba158fbe79551af11e376a64375e5e644',
        })
        self.assertEqual(verifier.baseline_data_pins(SimpleNamespace(new_v2_baseline_lock=None)), (
            'caeddfe28402d9718f272b8605d7ba55d2be802ca882518577c49e472f51d6bb',
            '66f59b7b9b7dc69fddc74a72dbad9bc4066de185113fc4d7cd7322977da1f329'))

    def test_exact_candidate_sources_include_current_runtime(self):
        self.assertEqual(verifier.CANDIDATE_SOURCES, frozenset((
            'web/app.js', RUNTIME_SOURCE, 'web/index.html', 'web/styles.css',
            'web/public-gallery.html', 'web/public-gallery.css', 'web/public-gallery.js',
            'tests/make_gallery.py', 'tests/test_gallery_site.py',
            'tests/test_public_gallery_performance.py', 'tests/verify_public_gallery.py',
            'tests/verify_public_snapshot.py')))


class GalleryRuntimeTrustTests(GalleryFixture):
    def setUp(self):
        super().setUp()
        self.frozen_runtime = self.runtime.read_bytes()
        self.trust = self.root / 'trust'
        for relative in verifier.FROZEN_PROGRAMS:
            destination = self.trust / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = self.project / relative if relative.startswith('web/') else verifier.ROOT / relative
            shutil.copyfile(source, destination)
        self.fixture_pins = {relative: verifier.digest((self.trust / relative).read_bytes())
                             for relative in verifier.FROZEN_PROGRAMS}
        self.baseline_lock = self.root / 'baseline-lock.json'
        self.baseline_lock.write_text(json.dumps({'sha256': self.fixture_pins}))
        self.data_lock = self.root / 'v2-data-lock.json'
        self.write_data_lock()
        self.baseline_before = self.snapshot_files(self.baseline)
        self.trust_before = self.snapshot_files(self.trust)
        self.lock = self.root / 'candidate-lock.json'
        # Fill the synthetic reviewed source tree without borrowing generated assets.
        for relative in verifier.CANDIDATE_SOURCES:
            destination = self.project / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(verifier.ROOT / relative, destination)
        self.current_runtime = b'window.TRUSTED_CURRENT_RUNTIME=2;\n'
        self.runtime.write_bytes(self.current_runtime)
        self.viewer.write_bytes(b'window.TRUSTED_CURRENT_VIEWER=2;\n')
        self.sources = {relative: (self.project / relative).read_bytes()
                        for relative in verifier.CANDIDATE_SOURCES}
        self.write_candidate_lock()
        self.args = SimpleNamespace(site=self.site, baseline_site=self.baseline,
            trust_root=self.trust, source_root=self.project, baseline_lock=self.baseline_lock,
            new_v2_baseline_lock=self.data_lock, candidate_source_lock=self.lock,
            candidate_viewer_script=self.viewer, gallery_bootstrap=self.project / 'web/public-gallery.js',
            gallery_css=self.project / 'web/public-gallery.css', expected_runs=12,
            expected_benchmark=12, expected_reviews=1, score_source=None)
        shutil.copytree(self.baseline, self.site)
        old_inline = verifier.v2.audit_html((self.baseline / 'offline.html').read_text(),
            (self.trust / 'web/app.js').read_text(), self.frozen_runtime.decode())
        self.offline_data = old_inline
        self.write_candidate()
        self.addCleanup(self.assert_frozen_inputs_unchanged)

    def assert_frozen_inputs_unchanged(self):
        self.assertEqual(self.snapshot_files(self.baseline), self.baseline_before)
        self.assertEqual(self.snapshot_files(self.trust), self.trust_before)

    def write_data_lock(self):
        self.data_lock.write_text(json.dumps({
            'index_sha256': verifier.digest((self.baseline / 'index.html').read_bytes()),
            'manifest_sha256': verifier.digest((self.baseline / verifier.MANIFEST).read_bytes())}))

    def write_candidate_lock(self, pins=None):
        pins = pins if pins is not None else {relative: verifier.digest((self.project / relative).read_bytes())
                                             for relative in verifier.CANDIDATE_SOURCES}
        self.lock.write_text(json.dumps({'sha256': pins}))

    def write_candidate(self, *, runtime=None, offline_runtime=None, full_runtime=None,
                        offline_data=None, full_data=None, full_suffix=b'', offline_suffix=b''):
        runtime = self.current_runtime if runtime is None else runtime
        offline_runtime = runtime if offline_runtime is None else offline_runtime
        full_runtime = runtime if full_runtime is None else full_runtime
        offline_data = self.offline_data if offline_data is None else offline_data
        # Build independently of build_gallery, whose runtime migration is tested elsewhere.
        for path in (self.site / 'viewer').glob('*'):
            path.unlink()
        offline = verifier.current_document(offline_data, self.sources, offline_runtime) + offline_suffix
        data = copy.deepcopy(self.full_data if full_data is None else full_data)
        data['offline'] = {'path': 'offline.html', 'sha256': verifier.digest(offline), 'size': len(offline)}
        full = verifier.current_document(data, self.sources, full_runtime) + full_suffix
        seed = make_gallery.project_gallery(data, full, runtime, self.viewer.read_bytes())
        html = make_gallery.render_gallery(seed, root=self.project)
        if isinstance(html, str):
            html = html.encode()
        manifest = copy.deepcopy(self.source_manifest)
        manifest.update(version=3, format='progressive-static-v3')
        self.full_path, self.runtime_path = seed['full']['path'], seed['runtime']['path']
        for name, raw, mime, role in (
            ('index.html', html, 'text/html', 'index'),
            ('offline.html', offline, 'text/html', 'offline'),
            (self.full_path, full, 'text/html', 'viewer'),
            (self.runtime_path, runtime, 'text/javascript', 'runtime')):
            path = self.site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            manifest['files'][name] = {'sha256': verifier.digest(raw), 'size': len(raw), 'mime': mime, 'role': role}
        manifest['stats'].update(index_bytes=len(html), offline_bytes=len(offline))
        (self.site / verifier.MANIFEST).write_text(json.dumps(manifest))

    def checked_sources(self, report=None):
        with mock.patch.object(verifier, '__file__', str(self.project / 'tests/verify_public_gallery.py')), \
             mock.patch.object(verifier.v2, '__file__', str(self.project / 'tests/verify_public_snapshot.py')):
            return verifier.candidate_sources(self.args, {} if report is None else report)

    def audit(self, report=None):
        # All trust checks, real helper parsing, inventories and exact compositions run.
        with mock.patch('sys.dont_write_bytecode', True), \
             mock.patch.object(verifier, 'FROZEN_PROGRAMS', self.fixture_pins), \
             mock.patch.object(verifier, '__file__', str(self.project / 'tests/verify_public_gallery.py')), \
             mock.patch.object(verifier.v2, '__file__', str(self.project / 'tests/verify_public_snapshot.py')):
            return verifier.load_sources(self.args, {} if report is None else report)

    def test_distinct_locked_runtime_passes_and_records_both_hashes(self):
        report = {}
        with mock.patch.object(verifier.v2, 'audit_html', wraps=verifier.v2.audit_html) as current_audit:
            result = self.audit(report)
        self.assertNotEqual(self.current_runtime, self.frozen_runtime)
        self.assertEqual(current_audit.call_count, 2)  # Baseline uses independently imported frozen helper.
        self.assertTrue(all(call.args[2] == self.current_runtime.decode() for call in current_audit.call_args_list))
        self.assertEqual(report['audit']['frozen_runtime_sha256'], verifier.digest(self.frozen_runtime))
        self.assertEqual(report['audit']['current_runtime_sha256'], verifier.digest(self.current_runtime))
        self.assertEqual(report['audit']['runtime_sha256'], verifier.digest(self.current_runtime))
        self.assertEqual(report['candidate_source_lock']['sources'][RUNTIME_SOURCE], verifier.digest(self.current_runtime))
        self.assertEqual(result['seed']['runtime']['path'], self.runtime_path)
        self.assertEqual(result['data']['runs'], self.full_data['runs'])

    def test_missing_runtime_pin_and_extra_source_fail_closed(self):
        pins = json.loads(self.lock.read_text())['sha256']
        for changed in ({key: value for key, value in pins.items() if key != RUNTIME_SOURCE},
                        {**pins, 'web/unplanned.js': verifier.digest(b'unplanned')}):
            with self.subTest(keys=sorted(changed)):
                self.write_candidate_lock(changed)
                with self.assertRaisesRegex(AssertionError, 'exactly the twelve'):
                    self.checked_sources()

    def test_missing_lock_fails_closed(self):
        self.args.candidate_source_lock = None
        with self.assertRaisesRegex(AssertionError, 'independent final source lock'):
            self.audit()

    def test_stale_runtime_pin_and_post_lock_source_mutation_fail_closed(self):
        pins = json.loads(self.lock.read_text())['sha256']
        self.write_candidate_lock({**pins, RUNTIME_SOURCE: verifier.digest(self.frozen_runtime)})
        with self.assertRaisesRegex(AssertionError, 'Approved current source bytes differ: web/preview-runtime.js'):
            self.checked_sources()
        self.write_candidate_lock(pins)
        self.runtime.write_bytes(self.current_runtime + b'window.MUTATED=1;')
        with self.assertRaisesRegex(AssertionError, 'Approved current source bytes differ: web/preview-runtime.js'):
            self.audit()

    def test_invalid_runtime_digest_and_duplicate_pin_fail_closed(self):
        pins = json.loads(self.lock.read_text())['sha256']
        self.write_candidate_lock({**pins, RUNTIME_SOURCE: 'not-a-sha256'})
        with self.assertRaisesRegex(AssertionError, 'Invalid candidate source digest'):
            self.checked_sources()
        self.lock.write_text('{"sha256": {' + json.dumps(RUNTIME_SOURCE) + ': "' + pins[RUNTIME_SOURCE]
            + '", ' + json.dumps(RUNTIME_SOURCE) + ': "' + pins[RUNTIME_SOURCE] + '"}}')
        with self.assertRaisesRegex(AssertionError, 'Duplicate candidate source lock key'):
            self.checked_sources()

    def test_runtime_asset_mutation_fails_manifest_hash(self):
        (self.site / self.runtime_path).write_bytes(b'window.MUTATED=1;')
        with self.assertRaisesRegex(AssertionError, 'Asset bytes mismatch'):
            self.audit()

    def test_rehashed_stale_runtime_bundle_fails_exact_inventory(self):
        self.write_candidate(runtime=self.frozen_runtime)
        with self.assertRaisesRegex(AssertionError, 'exactly inherited assets'):
            self.audit()

    def test_missing_runtime_asset_fails_closed(self):
        (self.site / self.runtime_path).unlink()
        with self.assertRaisesRegex(AssertionError, 'Exact bundle inventory differs'):
            self.audit()

    def test_stale_full_and_offline_programs_fail_even_when_rehashed(self):
        for keyword in ('offline_runtime', 'full_runtime'):
            with self.subTest(document=keyword):
                self.write_candidate(**{keyword: self.frozen_runtime})
                with self.assertRaisesRegex(AssertionError, 'Inline executable code differs'):
                    self.audit()

    def test_exact_composition_rejects_parseable_full_or_offline_suffix(self):
        for keyword in ('full_suffix', 'offline_suffix'):
            with self.subTest(document=keyword):
                self.write_candidate(**{keyword: b'\n<!--unapproved suffix-->'})
                with self.assertRaisesRegex(AssertionError, 'pinned current composition|exact current composition'):
                    self.audit()

    def test_offline_and_full_data_mutations_fail_closed(self):
        for field, message in (('offline_data', 'Offline data'), ('full_data', 'NEW snapshot data')):
            data = copy.deepcopy(self.offline_data if field == 'offline_data' else self.full_data)
            data['runs'][0]['model'] = 'Changed Model'
            with self.subTest(field=field):
                self.write_candidate(**{field: data})
                with self.assertRaisesRegex(AssertionError, message):
                    self.audit()

    def test_media_and_original_bytes_cannot_be_replaced_or_appended(self):
        for namespace in ('media/', 'originals/'):
            name = next(name for name in self.source_manifest['files'] if name.startswith(namespace))
            raw = (self.site / name).read_bytes()
            try:
                (self.site / name).write_bytes(raw + b'changed')
                with self.assertRaisesRegex(AssertionError, 'Asset bytes mismatch'):
                    self.audit()
            finally:
                (self.site / name).write_bytes(raw)
        extra = b'{"unplanned":true}'
        name = 'originals/' + verifier.digest(extra) + '.json'
        self.rewrite_payload('index.html', (self.site / 'index.html').read_bytes())
        manifest = self.manifest()
        (self.site / name).write_bytes(extra)
        manifest['files'][name] = {'sha256': verifier.digest(extra), 'size': len(extra),
                                 'mime': 'application/json', 'role': 'original'}
        (self.site / verifier.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(AssertionError, 'exactly inherited assets'):
            self.audit()

    def test_census_stays_frozen(self):
        self.args.expected_runs += 1
        with self.assertRaisesRegex(AssertionError, 'Frozen census differs'):
            self.audit()

    def test_repinning_frozen_runtime_in_external_lock_cannot_move_program_pin(self):
        path = self.trust / RUNTIME_SOURCE
        try:
            path.write_bytes(self.current_runtime)
            lock = {'sha256': {**self.fixture_pins, RUNTIME_SOURCE: verifier.digest(self.current_runtime)}}
            self.baseline_lock.write_text(json.dumps(lock))
            with self.assertRaisesRegex(AssertionError, 'Independent frozen program lock differs'):
                self.audit()
        finally:
            path.write_bytes(self.frozen_runtime)

    def test_frozen_runtime_mutation_without_repin_fails_closed(self):
        path = self.trust / RUNTIME_SOURCE
        try:
            path.write_bytes(self.current_runtime)
            with self.assertRaisesRegex(AssertionError, 'Frozen trust bytes differ: web/preview-runtime.js'):
                self.audit()
        finally:
            path.write_bytes(self.frozen_runtime)

    def test_missing_frozen_runtime_pin_fails_closed(self):
        self.baseline_lock.write_text(json.dumps({'sha256': {key: value for key, value in self.fixture_pins.items()
                                                          if key != RUNTIME_SOURCE}}))
        with self.assertRaisesRegex(AssertionError, 'Independent frozen program lock differs'):
            self.audit()

    def test_explicit_data_pins_do_not_allow_current_code_in_frozen_baseline(self):
        for name in ('index.html', 'offline.html'):
            raw = (self.baseline / name).read_bytes()
            try:
                changed = raw.replace(self.frozen_runtime, self.current_runtime)
                (self.baseline / name).write_bytes(changed)
                manifest = json.loads(self.baseline_before[verifier.MANIFEST])
                manifest['files'][name].update(sha256=verifier.digest(changed), size=len(changed))
                (self.baseline / verifier.MANIFEST).write_text(json.dumps(manifest))
                self.write_data_lock()
                with self.subTest(document=name), self.assertRaisesRegex(AssertionError, 'Inline executable code differs'):
                    self.audit()
            finally:
                (self.baseline / name).write_bytes(raw)
                (self.baseline / verifier.MANIFEST).write_bytes(self.baseline_before[verifier.MANIFEST])
                self.write_data_lock()


if __name__ == '__main__':
    unittest.main()
