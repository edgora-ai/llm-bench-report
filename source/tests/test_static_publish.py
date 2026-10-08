"""Integration tests for validated static packages and append-only publication."""

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_site
import make_snapshot
import publish


class StaticPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "index.html").write_bytes(b"original public report")
        (self.repo / "2026-09-30.html").write_bytes(b"immutable historical report")
        self.counter = 0

    def stage(self, color="blue", date="2026-09-30"):
        self.counter += 1
        buffer = BytesIO()
        Image.new("RGB", (48, 30), color).save(buffer, format="JPEG")
        data = {"runs": [{"id": "fixture", "date": date, "purpose": "benchmark",
                          "evaluation": {"evidence": ["evidence/desktop.png"]}, "reviews": []}],
                "tasks": [], "evidence": {"fixture/evidence/desktop.png":
                    "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()}}
        snapshot = self.root / f"input-{self.counter}.html"
        snapshot.write_text(make_snapshot.render_snapshot(data), encoding="utf-8")
        bundle = self.root / f"bundle-{self.counter}"
        make_site.build_site(snapshot, bundle, publish.ROOT / "web/app.js")
        site = publish.stage_static(bundle, self.root / f"stage-{self.counter}", "Fixture report", "Fixture note")
        return site, bundle

    def files(self):
        return {p.relative_to(self.repo).as_posix(): p.read_bytes()
                for p in self.repo.rglob("*") if p.is_file()}

    def test_static_stage_dates_are_offline_not_lightweight_index(self):
        site, bundle = self.stage()
        self.assertEqual((site / "2026-09-30.html").read_bytes(), (bundle / "offline.html").read_bytes())
        self.assertNotEqual((site / "2026-09-30.html").read_bytes(), (site / "index.html").read_bytes())
        readme = (site / "README.md").read_text()
        self.assertIn("offline.html", readme)
        self.assertIn("tests/publish.py <site-directory>", readme)
        self.assertNotIn("tests/publish.py <snapshot.html>", readme)
        with self.assertRaisesRegex(RuntimeError, "verified site directory"):
            publish.verify((site / "index.html").read_text())
        self.assertEqual(publish.verify((site / "offline.html").read_text())["runs"], 1)

    def test_copy_is_byte_verified_idempotent_and_preserves_dates(self):
        site, bundle = self.stage()
        managed = publish.copy_site(site, self.repo)
        manifest = make_site.verify_site(bundle)
        self.assertIn(publish.REPORT_ASSETS, managed)
        self.assertIn("offline.html", managed)
        for relative, info in manifest["files"].items():
            payload = (self.repo / relative).read_bytes()
            self.assertEqual(payload, (bundle / relative).read_bytes())
            self.assertEqual(hashlib.sha256(payload).hexdigest(), info["sha256"])
        self.assertEqual((self.repo / "2026-09-30.html").read_bytes(), b"immutable historical report")
        before = self.files()
        publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_update_retains_old_assets_and_appends_ownership(self):
        first, _ = self.stage()
        publish.copy_site(first, self.repo)
        old = json.loads((self.repo / publish.REPORT_ASSETS).read_text())["sha256"]
        old_bytes = {path: (self.repo / path).read_bytes() for path in old}
        second, bundle = self.stage("red", "2026-10-08")
        publish.copy_site(second, self.repo)
        new = json.loads((self.repo / publish.REPORT_ASSETS).read_text())["sha256"]
        self.assertGreater(len(new), len(old))
        self.assertTrue(set(old) < set(new))
        for path, data in old_bytes.items():
            self.assertEqual((self.repo / path).read_bytes(), data)
        self.assertEqual((self.repo / "2026-10-08.html").read_bytes(), (bundle / "offline.html").read_bytes())
        self.assertEqual((self.repo / "2026-09-30.html").read_bytes(), b"immutable historical report")

    def test_unknown_remote_media_is_not_adopted_even_if_bytes_match(self):
        site, _ = self.stage()
        path = next((site / "media").iterdir())
        (self.repo / "media").mkdir()
        (self.repo / "media" / path.name).write_bytes(path.read_bytes())
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unmanaged media destination"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_changed_remote_media_prevents_any_report_update(self):
        first, _ = self.stage()
        publish.copy_site(first, self.repo)
        next((self.repo / "media").iterdir()).write_bytes(b"changed media")
        before = self.files()
        second, _ = self.stage("red")
        with self.assertRaisesRegex(RuntimeError, "Managed media was modified"):
            publish.copy_site(second, self.repo)
        self.assertEqual(self.files(), before)

    def test_changed_remote_entrypoint_prevents_update(self):
        site, _ = self.stage()
        publish.copy_site(site, self.repo)
        (self.repo / "index.html").write_bytes(b"concurrent edit")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Managed report file changed"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_missing_owned_media_prevents_update(self):
        site, _ = self.stage()
        publish.copy_site(site, self.repo)
        next((self.repo / "media").iterdir()).unlink()
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Managed media file is missing"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_staged_hash_tampering_prevents_clone_changes(self):
        site, _ = self.stage()
        (site / "offline.html").write_bytes(b"changed after staging")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Managed report file changed"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_extra_staged_media_is_refused(self):
        site, _ = self.stage()
        (site / "media/unmanaged.txt").write_text("not a media asset")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unmanaged or missing staged media"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_unmanaged_offline_destination_is_refused(self):
        site, _ = self.stage()
        (self.repo / "offline.html").write_bytes(b"someone else's document")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unmanaged offline report"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_owned_media_parent_symlink_is_refused(self):
        site, _ = self.stage()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel.txt").write_text("unchanged")
        (self.repo / "media").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Unsafe media destination"):
            publish.copy_site(site, self.repo)
        self.assertEqual((outside / "sentinel.txt").read_text(), "unchanged")
        self.assertEqual((self.repo / "index.html").read_bytes(), b"original public report")

    def test_missing_or_malformed_ownership_refuses_update(self):
        site, _ = self.stage()
        publish.copy_site(site, self.repo)
        ledger = self.repo / publish.REPORT_ASSETS
        original = ledger.read_bytes()
        for data in (b"{}", b'{"version":1,"sha256":{"../escape":"x"}}'):
            ledger.write_bytes(data)
            before = self.files()
            with self.assertRaises(RuntimeError):
                publish.copy_site(site, self.repo)
            self.assertEqual(self.files(), before)
        ledger.write_bytes(original)
        ledger.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing its media ownership ledger"):
            publish.copy_site(site, self.repo)

    def test_legacy_publication_cannot_leave_static_manifest_stale(self):
        site, _ = self.stage()
        publish.copy_site(site, self.repo)
        legacy = publish.stage((site / "offline.html").read_text(), self.root / "legacy", "Title", "Note")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "verified site directory"):
            publish.copy_site(legacy, self.repo)
        self.assertEqual(self.files(), before)

    def test_lightweight_dated_source_is_refused_before_clone_changes(self):
        site, _ = self.stage(date="2026-10-08")
        (site / "2026-10-08.html").write_bytes((site / "index.html").read_bytes())
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Dated report differs from verified offline"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_symlink_dated_source_is_refused_even_with_valid_offline_bytes(self):
        site, _ = self.stage(date="2026-10-08")
        dated = site / "2026-10-08.html"
        dated.unlink()
        dated.symlink_to(site / "offline.html")
        before = self.files()
        with self.assertRaises(RuntimeError):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_unknown_dated_source_is_not_automatically_managed(self):
        site, _ = self.stage()
        (site / "2026-12-31.html").write_bytes((site / "offline.html").read_bytes())
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unexpected or missing staged dates"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_missing_expected_dated_source_is_refused(self):
        site, _ = self.stage(date="2026-10-08")
        (site / "2026-10-08.html").unlink()
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "Unexpected or missing staged dates"):
            publish.copy_site(site, self.repo)
        self.assertEqual(self.files(), before)

    def test_legacy_publication_refuses_orphaned_static_ownership(self):
        site, _ = self.stage()
        publish.copy_site(site, self.repo)
        (self.repo / publish.REPORT_MANIFEST).unlink()
        legacy = publish.stage((site / "offline.html").read_text(), self.root / "legacy", "Title", "Note")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "ownership ledger has no current report manifest"):
            publish.copy_site(legacy, self.repo)
        self.assertEqual(self.files(), before)

    def test_git_stages_exact_media_files_not_a_directory(self):
        site, _ = self.stage()
        commands = []
        workspace = self.root / "publication"
        workspace.mkdir()

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[:3] == ["gh", "repo", "clone"]:
                Path(command[-1]).mkdir()
            if "--name-only" in command:
                paths = [path.relative_to(workspace / "repo").as_posix()
                         for path in (workspace / "repo").rglob("*") if path.is_file()]
                return SimpleNamespace(stdout="\0".join(paths) + "\0", returncode=0)
            if "rev-parse" in command:
                return SimpleNamespace(stdout="a" * 40 + "\n", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        with mock.patch.object(publish, "run", side_effect=fake_run):
            result = publish.publish_site(site, workspace, "owner/existing", "main", {"runs": 1})
        add = next(command for command in commands if "add" in command)
        self.assertNotIn("media", add)
        self.assertIn(publish.REPORT_MANIFEST, add)
        self.assertIn(publish.REPORT_ASSETS, add)
        for relative in make_site.verify_site(self.root / "bundle-1")["files"]:
            self.assertIn(relative, add)
        self.assertTrue(result["pushed"])


if __name__ == "__main__":
    unittest.main()
