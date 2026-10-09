"""Synthetic local-copy tests for append-only v2 to v3 ownership migration."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_gallery
import make_site
import make_snapshot
import publish
import source_export
from test_gallery_site import GalleryFixture


class GalleryPublicationTests(GalleryFixture):
    def setUp(self):
        super().setUp()
        original_verify = make_site.verify_site
        def verify_fixture(directory, viewer_script=None, **kwargs):
            kwargs.setdefault("preview_runtime", self.runtime)
            kwargs.setdefault("gallery_root", self.project)
            return original_verify(directory, viewer_script or self.viewer, **kwargs)
        patcher = mock.patch.object(make_site, "verify_site", side_effect=verify_fixture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "index.html").write_bytes(b"prior public report")
        (self.repo / "2026-09-30.html").write_bytes(b"immutable historical report")
        self.build()
        self.staged = publish.stage_static(self.site, self.root / "stage-v3", "Fixture", "Synthetic only")
        self.v2_stage = publish.stage_static(self.baseline, self.root / "stage-v2", "Fixture", "Synthetic only")

    def files(self):
        return self.snapshot_files(self.repo)

    def migrate(self):
        publish.copy_site(self.v2_stage, self.repo)
        publish.copy_site(self.staged, self.repo)

    def test_v2_to_v3_preserves_all_owned_assets_and_dated_history(self):
        publish.copy_site(self.v2_stage, self.repo)
        before = self.files()
        old = json.loads(before[publish.REPORT_ASSETS])
        self.assertEqual(old["version"], 2)
        publish.copy_site(self.staged, self.repo)
        ledger = json.loads((self.repo / publish.REPORT_ASSETS).read_text())
        self.assertEqual(ledger["version"], 3)
        self.assertTrue(set(old["sha256"]) < set(ledger["sha256"]))
        for path, digest in old["sha256"].items():
            self.assertEqual(ledger["sha256"][path], digest)
            self.assertEqual((self.repo / path).read_bytes(), before[path])
        for path, info in self.manifest()["files"].items():
            self.assertEqual((self.repo / path).read_bytes(), (self.site / path).read_bytes())
            self.assertEqual(hashlib.sha256((self.repo / path).read_bytes()).hexdigest(), info["sha256"])
        self.assertEqual((self.repo / "2026-09-30.html").read_bytes(), b"immutable historical report")
        self.assertEqual((self.repo / "2026-10-08.html").read_bytes(), (self.baseline / "offline.html").read_bytes())
        self.assertEqual(publish.owned_media(self.repo, publish.report_manifest(self.repo)), ledger["sha256"])
        before = self.files()
        publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)

    def test_initial_v3_has_only_declared_content_addressed_roles(self):
        publish.copy_site(self.staged, self.repo)
        manifest = publish.report_manifest(self.repo)
        ledger = json.loads((self.repo / publish.REPORT_ASSETS).read_text())
        expected = {p: info["sha256"] for p, info in manifest["files"].items() if publish.asset_digest(p, 3)}
        self.assertEqual(ledger, {"version": 3, "sha256": expected})
        self.assertEqual(sum(path.startswith("viewer/") for path in expected), 2)
        self.assertIsNone(publish.asset_digest("viewer/" + "a" * 64 + ".js", 2))
        self.assertIsNone(publish.asset_digest("viewer/" + "a" * 64 + ".json", 3))
        self.assertIsNone(publish.asset_digest("viewer/../" + "a" * 64 + ".html", 3))

    def test_future_v3_update_appends_viewer_and_retains_old_viewer_bytes(self):
        self.migrate()
        ledger = json.loads((self.repo / publish.REPORT_ASSETS).read_text())["sha256"]
        old = {path: (self.repo / path).read_bytes() for path in ledger}
        data = copy.deepcopy(self.full_data)
        data["runs"][0]["model"] = "Changed synthetic metadata"
        offline_data = make_site.audit_snapshot((self.baseline / "offline.html").read_text(), self.viewer, preview_runtime=self.runtime)
        offline_data["runs"] = copy.deepcopy(data["runs"])
        offline = make_snapshot.render_snapshot(offline_data, root=self.project).encode()
        offline_info = make_site._file_info(offline, "text/html", "offline")
        data["offline"] = {"path": "offline.html", "sha256": offline_info["sha256"], "size": len(offline)}
        full = make_snapshot.render_snapshot(data, root=self.project).encode()
        manifest = copy.deepcopy(self.source_manifest)
        manifest["files"]["offline.html"] = offline_info
        manifest["files"]["index.html"] = make_site._file_info(full, "text/html", "index")
        manifest["stats"].update(index_bytes=len(full), offline_bytes=len(offline))
        next_baseline = self.root / "next-baseline"
        next_baseline.mkdir()
        for path, raw in self.snapshot_files(self.baseline).items():
            target = next_baseline / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        (next_baseline / "offline.html").write_bytes(offline)
        (next_baseline / "index.html").write_bytes(full)
        (next_baseline / make_site.MANIFEST).write_text(json.dumps(manifest))
        make_site.verify_site(next_baseline, self.viewer)
        next_gallery = self.root / "next-gallery"
        make_gallery.build_gallery(next_baseline, next_gallery, self.viewer, input_runtime=self.runtime, root=self.project)
        next_stage = publish.stage_static(next_gallery, self.root / "next-stage", "Fixture", "Synthetic only")
        publish.copy_site(next_stage, self.repo)
        new = json.loads((self.repo / publish.REPORT_ASSETS).read_text())["sha256"]
        self.assertTrue(set(ledger) < set(new))
        for path, raw in old.items():
            self.assertEqual((self.repo / path).read_bytes(), raw)
        self.assertEqual(sum(path.startswith("viewer/") and path.endswith(".html") for path in new), 2)
        self.assertEqual((self.repo / "2026-10-08.html").read_bytes(), (self.baseline / "offline.html").read_bytes())
        publish.owned_media(self.repo, publish.report_manifest(self.repo))

    def test_v3_cannot_downgrade_to_v2(self):
        self.migrate()
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "downgrade"):
            publish.copy_site(self.v2_stage, self.repo)
        self.assertEqual(self.files(), before)

    def test_unknown_viewer_directory_and_matching_unknown_bytes_are_not_adopted(self):
        publish.copy_site(self.v2_stage, self.repo)
        viewer = self.repo / "viewer"
        viewer.mkdir()
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unmanaged viewer destination"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)
        path = next((self.site / "viewer").iterdir())
        (viewer / path.name).write_bytes(path.read_bytes())
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unmanaged viewer destination"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)

    def test_changed_missing_and_unknown_owned_viewer_prevent_all_writes(self):
        self.migrate()
        path = next((self.repo / "viewer").iterdir())
        raw = path.read_bytes()
        path.write_bytes(raw + b"tampered")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Managed viewer was modified"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)
        path.unlink()
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Managed viewer file is missing"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)
        path.write_bytes(raw)
        unknown = self.repo / "viewer/unknown.js"
        unknown.write_bytes(b"unknown")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unmanaged viewer destination"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)

    def test_viewer_symlink_parent_and_leaf_rejected(self):
        publish.copy_site(self.v2_stage, self.repo)
        outside = self.root / "outside-viewer"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"untouched")
        viewer = self.repo / "viewer"
        viewer.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Unsafe viewer destination"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual((outside / "sentinel").read_bytes(), b"untouched")
        viewer.unlink()
        publish.copy_site(self.staged, self.repo)
        path = next(viewer.iterdir())
        raw = path.read_bytes()
        external = self.root / "external-runtime"
        external.write_bytes(raw)
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(RuntimeError):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(external.read_bytes(), raw)

    def test_ledger_version_mismatch_unknown_roles_and_hash_collisions_rejected(self):
        self.migrate()
        ledger_path = self.repo / publish.REPORT_ASSETS
        original = json.loads(ledger_path.read_text())
        for mutate in (lambda value: value.update(version=2), lambda value: value.update(version=True),
                       lambda value: value["sha256"].update({"viewer/" + "a" * 64 + ".json": "a" * 64}),
                       lambda value: value["sha256"].update({next(p for p in value["sha256"] if p.startswith("viewer/")): "f" * 64}),
                       lambda value: value.update(extra="not allowed")):
            ledger = copy.deepcopy(original)
            mutate(ledger)
            ledger_path.write_text(json.dumps(ledger))
            before = self.files()
            with self.assertRaises(RuntimeError):
                publish.copy_site(self.staged, self.repo)
            self.assertEqual(self.files(), before)
        ledger_path.write_text(json.dumps(original))
        publish.copy_site(self.staged, self.repo)

    def test_extra_staged_viewer_or_modified_public_script_rejected(self):
        self.migrate()
        extra = self.staged / "viewer/unmanaged.js"
        extra.write_bytes(b"not a managed role")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unmanaged or missing staged viewer"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)
        extra.unlink()
        index = (self.staged / "index.html").read_bytes().replace(b"TRUSTED_UNIT_GALLERY=1", b"TRUSTED_UNIT_GALLERY=2")
        manifest = json.loads((self.staged / make_site.MANIFEST).read_text())
        manifest["files"]["index.html"] = make_site._file_info(index, "text/html", "index")
        manifest["stats"]["index_bytes"] = len(index)
        (self.staged / "index.html").write_bytes(index)
        (self.staged / make_site.MANIFEST).write_text(json.dumps(manifest))
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "untrusted gallery bootstrap"):
            publish.copy_site(self.staged, self.repo)
        self.assertEqual(self.files(), before)

    def test_explicit_source_allowlist_contains_only_named_public_gallery_files(self):
        for path in ("web/public-gallery.html", "web/public-gallery.css", "web/public-gallery.js", "tests/make_gallery.py",
                     "tests/verify_public_gallery.py", "tests/test_gallery_site.py", "tests/test_gallery_publish.py"):
            self.assertTrue(source_export._allowed(path), path)
        for path in ("web/unreviewed.js", "web/public-gallery.map", "tests/arbitrary_helper.py", "private/data.json"):
            self.assertFalse(source_export._allowed(path), path)


if __name__ == "__main__":
    unittest.main()
