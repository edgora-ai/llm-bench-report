"""Synthetic, offline tests for the content-addressed public report boundary."""
import base64
import copy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_site
import make_snapshot


class StaticSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        web = self.project / "web"
        web.mkdir(parents=True)
        self.old_viewer = self.root / "old-app.js"
        self.old_viewer.write_text("window.OLD_TRUSTED_VIEWER = 1;\n")
        (web / "app.js").write_text("window.NEW_TRUSTED_VIEWER = 1;\n")
        (web / "styles.css").write_text("body { color: #223344; }\n")
        (web / "index.html").write_text('<!doctype html><html><head>\n<link rel="stylesheet" href="/styles.css">\n'
                                         '  <script src="/app.js" defer></script>\n</head><body>'
                                         '<main id="workspace"></main></body></html>')
        self.viewer = web / "app.js"
        self.snapshot = self.root / "snapshot.html"
        self.site = self.root / "site"
        self.image = self.image_bytes("JPEG", (960, 600))
        self.portrait = self.image_bytes("PNG", (240, 600))
        self.data = {
            "runs": [{"id": "r1", "purpose": "benchmark", "date": "2026-10-08", "status": "failed",
                      "attempt": 2, "retry_of": "earlier", "evaluation": {"status": "completed", "evidence": [
                          "evidence/desktop.png", "evidence/later.png", "evidence/mobile.png"]},
                      "conditions": {"reasoning_effort_applied": "max"}, "checks": [{"name": "entrypoint", "status": "pass"}],
                      "metrics": {"duration_ms": 1234, "cost_usd": None},
                      "reviews": [{"id": "review1", "blind": False, "reviewer": "AI suggestion awaiting review"}]}],
            "tasks": [{"id": "fixture", "prompt": "literal </script><script>not executable</script> & 中文"}],
            "evidence": {"r1/evidence/desktop.png": self.uri("image/jpeg", self.image),
                         "r1/evidence/later.png": self.uri("image/jpeg", self.image),
                         "r1/evidence/mobile.png": self.uri("image/png", self.portrait)}}
        self.write_input()

    @staticmethod
    def image_bytes(kind, size):
        result = BytesIO()
        Image.new("RGB", size, (35, 80, 110)).save(result, kind)
        return result.getvalue()

    @staticmethod
    def uri(mime, payload):
        return "data:" + mime + ";base64," + base64.b64encode(payload).decode("ascii")

    def write_input(self, data=None, extra=""):
        data = self.data if data is None else data
        html = '<!doctype html><html><head><style>body{}</style></head><body>' + extra
        html += '<script>window.BENCH_SNAPSHOT=' + json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        html += ';</script><script>' + self.old_viewer.read_text() + '</script></body></html>'
        self.snapshot.write_text(html)

    def build(self):
        return make_site.build_site(self.snapshot, self.site, self.old_viewer, root=self.project)

    def audit(self, name="index.html"):
        return make_site.audit_snapshot((self.site / name).read_text(), self.viewer, static=name == "index.html")

    def manifest(self):
        return json.loads((self.site / make_site.MANIFEST).read_text())

    def rewrite_index(self, data):
        payload = make_snapshot.render_snapshot(data, root=self.project).encode()
        (self.site / "index.html").write_bytes(payload)
        manifest = self.manifest()
        manifest["files"]["index.html"].update(sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
        manifest["stats"]["index_bytes"] = len(payload)
        (self.site / make_site.MANIFEST).write_text(json.dumps(manifest))

    def assert_refused(self):
        with self.assertRaises(RuntimeError):
            make_site.verify_site(self.site, self.viewer)

    def test_build_preserves_metadata_bytes_and_deduplicates(self):
        before = self.snapshot.read_bytes()
        stats = self.build()
        data = self.audit()
        offline = self.audit("offline.html")
        self.assertEqual(offline, self.data)
        self.assertEqual(data["runs"], self.data["runs"])
        self.assertEqual(data["tasks"], self.data["tasks"])
        self.assertEqual(self.snapshot.read_bytes(), before)
        self.assertNotIn("data:image/", (self.site / "index.html").read_text())
        self.assertNotIn("OLD_TRUSTED_VIEWER", (self.site / "offline.html").read_text())
        self.assertEqual(stats["runs"], 1)
        self.assertEqual(stats["benchmark_runs"], 1)
        self.assertEqual(stats["reviews"], 1)
        self.assertEqual(stats["evidence"], 3)
        self.assertEqual(stats["unique_evidence"], 2)
        self.assertEqual(stats["thumbnails"], 3)
        self.assertEqual(stats["media_files"], 4)
        self.assertLess(stats["index_bytes"], make_site.INDEX_BUDGET)
        self.assertEqual(data["evidence"]["r1/evidence/desktop.png"], data["evidence"]["r1/evidence/later.png"])
        for key, uri in self.data["evidence"].items():
            expected = base64.b64decode(uri.split(",", 1)[1])
            relative = data["evidence"][key]
            self.assertEqual((self.site / relative).read_bytes(), expected)
            self.assertEqual(Path(relative).stem, hashlib.sha256(expected).hexdigest())
        self.assertEqual(make_site.verify_site(self.site, self.viewer)["stats"], stats)
        self.assertEqual(set(self.manifest()["files"]), {"index.html", "offline.html", *data["assets"]})

    def test_thumbnail_width_aspect_ratio_and_jpeg_decode(self):
        self.build()
        data = self.audit()
        for key, path in data["thumbnails"].items():
            original = data["assets"][data["evidence"][key]]
            with Image.open(self.site / path) as image:
                image.load()
                self.assertEqual(image.format, "JPEG")
                width = min(original["width"], 480)
                self.assertEqual(image.width, width)
                self.assertEqual(image.height, round(original["height"] * width / original["width"]))
                self.assertEqual(data["assets"][path]["width"], image.width)
        self.assertEqual(data["assets"][data["thumbnails"]["r1/evidence/mobile.png"]]["height"], 600)

    def test_repeat_build_is_deterministic_and_idempotent(self):
        first = self.build()
        before = {path.relative_to(self.site): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.site.rglob("*") if path.is_file()}
        self.assertEqual(self.build(), first)
        self.assertEqual(before, {path.relative_to(self.site): (path.read_bytes(), path.stat().st_mtime_ns)
                                 for path in self.site.rglob("*") if path.is_file()})
        second = self.root / "second"
        make_site.build_site(self.snapshot, second, self.old_viewer, root=self.project)
        self.assertEqual({path: value[0] for path, value in before.items()},
                         {path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()})

    def test_missing_media_and_tampered_bytes_are_refused(self):
        self.build()
        path = self.site / next(iter(self.audit()["assets"]))
        original = path.read_bytes()
        path.unlink()
        self.assert_refused()
        path.write_bytes(original + b"changed")
        self.assert_refused()
        path.write_bytes(original)
        make_site.verify_site(self.site, self.viewer)

    def test_unmanaged_files_and_empty_directories_are_refused(self):
        self.build()
        for relative in ("unexpected.txt", "media/unmanaged.jpg", "payload.svg", "payload.html"):
            path = self.site / relative
            path.write_text("unmanaged")
            self.assert_refused()
            path.unlink()
        extra = self.site / "empty"
        extra.mkdir()
        self.assert_refused()

    def test_symbolic_links_leaf_directory_and_ancestor_are_refused(self):
        self.build()
        path = self.site / next(iter(self.audit()["assets"]))
        outside = self.root / "outside.jpg"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        self.assert_refused()
        path.unlink()
        path.write_bytes(outside.read_bytes())
        media = self.site / "media"
        moved = self.root / "moved-media"
        media.rename(moved)
        media.symlink_to(moved, target_is_directory=True)
        self.assert_refused()
        media.unlink()
        moved.rename(media)
        linked = self.root / "linked"
        linked.symlink_to(self.site, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            make_site.read_asset_safe(linked / "media", path.name)
        with self.assertRaises(RuntimeError):
            make_site.verify_site(linked, self.viewer)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX special-file test")
    def test_fifo_is_rejected_without_blocking(self):
        self.build()
        path = self.site / next(iter(self.audit()["assets"]))
        path.unlink()
        os.mkfifo(path)
        self.assert_refused()
        with self.assertRaises(RuntimeError):
            make_site.read_asset_safe(self.site, str(path.relative_to(self.site)))

    def test_unknown_and_differing_nonempty_destinations_are_untouched(self):
        self.site.mkdir()
        unknown = self.site / "unknown"
        unknown.write_bytes(b"do not overwrite")
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertEqual(unknown.read_bytes(), b"do not overwrite")
        unknown.unlink()
        self.build()
        original = (self.site / "index.html").read_bytes()
        self.data["runs"][0]["status"] = "completed"
        self.write_input()
        with self.assertRaisesRegex(RuntimeError, "byte-identical"):
            self.build()
        self.assertEqual((self.site / "index.html").read_bytes(), original)

    def test_destination_symlink_and_parent_symlink_do_not_write(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.site.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(RuntimeError):
            make_site.build_site(self.snapshot, self.site / "nested", self.old_viewer, root=self.project)
        self.assertEqual(list(outside.iterdir()), [])

    def test_wrong_old_viewer_is_not_accepted_and_nothing_is_written(self):
        with self.assertRaisesRegex(RuntimeError, "viewer mismatch"):
            make_site.build_site(self.snapshot, self.site, self.viewer, root=self.project)
        self.assertFalse(self.site.exists())

    def test_new_output_viewer_must_match(self):
        self.build()
        with self.assertRaisesRegex(RuntimeError, "viewer mismatch"):
            make_site.verify_site(self.site, self.old_viewer)

    def test_all_input_validated_before_any_write(self):
        self.data["evidence"]["r1/evidence/mobile.png"] = self.uri("image/png", b"not an image")
        self.write_input()
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertFalse(self.site.exists())
        self.assertEqual(list(self.root.glob(".static-site-*")), [])

    def test_nested_private_fields_and_credentials_are_refused(self):
        original = copy.deepcopy(self.data)
        for private in ("archive_dir", "cli_command", "usage_raw", "api_key", "apiKey", "Access-Token", "authorization", "password"):
            with self.subTest(private=private):
                self.data = copy.deepcopy(original)
                self.data["runs"][0]["evaluation"]["extra"] = [{"deeper": {private: "secret-value"}}]
                self.write_input()
                with self.assertRaisesRegex(RuntimeError, "private field"):
                    self.build()
        for text in ("sk-test123", "ghp_test123", "github_pat_test123", "/home/ubuntu/private",
                     "http://localhost/private", "/workspace/archive", "http://fixture-user:fixture-password@example.invalid/",
                     "https://example.invalid/?api_key=fixture-secret", "%2Fhome%2Fubuntu%2Fprivate"):
            with self.subTest(text=text):
                self.data = copy.deepcopy(original)
                self.data["runs"][0]["reviews"][0]["note"] = text
                self.write_input()
                with self.assertRaises(RuntimeError):
                    self.build()
        self.assertFalse(self.site.exists())

    def test_html_and_svg_are_never_media(self):
        for mime, payload in (("text/html", b"<script>alert(1)</script>"),
                              ("image/svg+xml", b"<svg onload='alert(1)'/>"),
                              ("image/jpeg", b"<svg/>"), ("video/webm", b"<html/>")):
            self.data["evidence"]["r1/evidence/desktop.png"] = self.uri(mime, payload)
            self.write_input()
            with self.assertRaises(RuntimeError):
                self.build()
        self.assertFalse(self.site.exists())

    def test_declared_mime_must_match_actual_image_bytes(self):
        self.data["evidence"]["r1/evidence/desktop.png"] = self.uri("image/png", self.image)
        self.write_input()
        with self.assertRaisesRegex(RuntimeError, "MIME mismatch"):
            self.build()

    def test_exactly_two_scripts_safely_escaped_data_and_no_extra_execution(self):
        self.build()
        html = (self.site / "index.html").read_text()
        self.assertEqual(html.count("<script>"), 2)
        self.assertIn("\\u003c/script>", html)
        self.assertEqual(self.audit()["tasks"], self.data["tasks"])
        extras = ['<script>window.evil=1</script>', '<img src="x" onerror="evil()">',
                  '<iframe srcdoc="evil"></iframe>', '<svg></svg>', '<object data="evil"></object>',
                  '<link rel="stylesheet" href="https://example.invalid/a.css">',
                  '<style>@import "https://example.invalid/a.css";</style>',
                  '<style>body{background:u/**/rl(https://example.invalid/a.png)}</style>',
                  '<meta http-equiv="refresh" content="0;url=https://example.invalid">',
                  '<a href="javascript:evil()">run</a>', '<form action="https://example.invalid"></form>',
                  '<img srcset="https://example.invalid/a.png 1x">']
        for extra in extras:
            with self.subTest(extra=extra):
                self.write_input(extra=extra)
                with self.assertRaises(RuntimeError):
                    make_site.audit_snapshot(self.snapshot.read_text(), self.old_viewer)
        self.write_input()
        unsafe = self.snapshot.read_text().replace("\\u003c", "<")
        with self.assertRaises(RuntimeError):
            make_site.audit_snapshot(unsafe, self.old_viewer)

    def test_json_duplicates_nonfinite_and_bad_shapes_are_refused(self):
        for blob in ('{"runs":[],"runs":[],"evidence":{}}', '{"runs":[],"evidence":{},"n":NaN}',
                     '{"runs":{},"evidence":{}}', '{"runs":[],"evidence":[]}'):
            html = '<script>window.BENCH_SNAPSHOT=' + blob + ';</script><script>' + self.old_viewer.read_text() + '</script>'
            with self.assertRaises(RuntimeError):
                make_site.audit_snapshot(html, self.old_viewer)

    def test_strict_media_and_filesystem_paths(self):
        self.build()
        original = self.audit()
        for path in ("../escape.jpg", "/media/a.jpg", "https://example.invalid/a.jpg", "//example.invalid/a.jpg",
                     "media/../a.jpg", "media//a.jpg", "media\\a.jpg", "media/%2e%2e/a.jpg", "media/a.jpg?x=1",
                     "media/" + "a" * 64 + ".svg", "media/" + "A" * 64 + ".jpg"):
            with self.subTest(path=path):
                data = copy.deepcopy(original)
                data["evidence"]["r1/evidence/desktop.png"] = path
                self.rewrite_index(data)
                self.assert_refused()
        for path in ("../snapshot.html", "/snapshot.html", "media/../index.html", "media//a.jpg", "media\\a.jpg", "media/a.jpg?x"):
            with self.assertRaises(RuntimeError):
                make_site.read_asset_safe(self.site, path)

    def test_changed_run_or_task_data_rejected_even_with_rehashed_index(self):
        self.build()
        original = self.audit()
        for field in ("runs", "tasks"):
            data = copy.deepcopy(original)
            data[field][0]["changed"] = "not the offline record"
            self.rewrite_index(data)
            self.assert_refused()

    def test_missing_thumbnail_and_changed_offline_descriptor_refused(self):
        self.build()
        original = self.audit()
        data = copy.deepcopy(original)
        data["thumbnails"].pop("r1/evidence/desktop.png")
        self.rewrite_index(data)
        self.assert_refused()
        data = copy.deepcopy(original)
        data["offline"]["sha256"] = "0" * 64
        self.rewrite_index(data)
        self.assert_refused()
        data = copy.deepcopy(original)
        data["offline"]["path"] = "other.html"
        self.rewrite_index(data)
        self.assert_refused()

    def test_manifest_schema_hash_mime_size_and_roles_refused(self):
        self.build()
        original = self.manifest()
        media = next(path for path in original["files"] if path.startswith("media/"))
        changes = [("sha256", "0" * 64), ("mime", "text/html"), ("mime", "image/svg+xml"),
                   ("size", True), ("size", -1), ("role", "script"), ("width", 0)]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                manifest = copy.deepcopy(original)
                manifest["files"][media][key] = value
                (self.site / make_site.MANIFEST).write_text(json.dumps(manifest))
                self.assert_refused()
        manifest = copy.deepcopy(original)
        manifest["files"]["../escape"] = manifest["files"].pop(media)
        with self.assertRaises(RuntimeError):
            make_site.validate_manifest(manifest)

    def test_unregistered_or_unsafe_evidence_keys_refused(self):
        for key in ("unknown/evidence/a.jpg", "r1/../a.jpg", "/r1/evidence/a.jpg", "r1/evidence/a.html"):
            data = copy.deepcopy(self.data)
            data["evidence"][key] = self.uri("image/jpeg", self.image)
            self.write_input(data)
            with self.assertRaises(RuntimeError):
                self.build()

    def test_zero_run_snapshot_is_valid_and_has_no_media_directory(self):
        self.data = {"runs": [], "tasks": [], "evidence": {}}
        self.write_input()
        stats = self.build()
        self.assertEqual(stats["runs"], 0)
        self.assertEqual(stats["evidence"], 0)
        self.assertEqual(stats["media_files"], 0)
        self.assertFalse((self.site / "media").exists())
        self.assertEqual(self.audit("offline.html"), self.data)
        self.assertEqual(make_site.verify_site(self.site, self.viewer)["stats"], stats)

    def test_legacy_png_gif_webp_bytes_are_kept_with_actual_extensions(self):
        for kind, mime, extension in (("PNG", "image/png", "png"), ("GIF", "image/gif", "gif"), ("WEBP", "image/webp", "webp")):
            payload = self.image_bytes(kind, (80, 60))
            key = "r1/evidence/extra." + extension
            self.data["runs"][0]["evaluation"]["evidence"].append(key.removeprefix("r1/"))
            self.data["evidence"][key] = self.uri(mime, payload)
        self.write_input()
        self.build()
        data = self.audit()
        for key, uri in self.data["evidence"].items():
            self.assertEqual((self.site / data["evidence"][key]).read_bytes(), base64.b64decode(uri.split(",", 1)[1]))
            mime = uri[5:].split(";", 1)[0]
            self.assertEqual(Path(data["evidence"][key]).suffix, "." + make_site.MIME_EXTENSIONS[mime])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is needed only to create a tiny synthetic video")
    def test_existing_video_is_copied_not_reencoded(self):
        video = self.root / "tiny.webm"
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x24:d=0.2",
                                 "-c:v", "libvpx", "-an", "-y", str(video)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = video.read_bytes()
        key = "r1/evidence/animation.webm"
        self.data["runs"][0]["evaluation"]["evidence"].append("evidence/animation.webm")
        self.data["evidence"][key] = self.uri("video/webm", payload)
        self.write_input()
        real_run = subprocess.run
        commands = []

        def checked_run(command, *args, **kwargs):
            commands.append(command)
            self.assertNotEqual(Path(command[0]).name, "ffmpeg", "builder must not encode videos")
            return real_run(command, *args, **kwargs)

        with mock.patch.object(make_site.subprocess, "run", side_effect=checked_run):
            stats = self.build()
        self.assertEqual(stats["videos"], 1)
        data = self.audit()
        self.assertEqual((self.site / data["evidence"][key]).read_bytes(), payload)
        self.assertNotIn(key, data["thumbnails"])
        with mock.patch.object(make_site.shutil, "which", return_value=None):
            self.assertEqual(make_site.verify_site(self.site, self.viewer)["stats"], stats)

    def test_cli_help_and_required_arguments(self):
        command = [sys.executable, str(Path(make_site.__file__))]
        help_result = subprocess.run(command + ["--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--input-viewer", help_result.stdout)
        missing = subprocess.run(command + [str(self.snapshot), str(self.site)], capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--input-viewer", missing.stderr)
        invalid = subprocess.run(command + [str(self.root / "missing.html"), str(self.site), "--input-viewer", str(self.old_viewer)], capture_output=True, text=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertFalse(self.site.exists())

    def test_task_identifiers_are_not_mistaken_for_credentials(self):
        self.write_input(extra='<div id="task-picker" class="task-control">task-filter</div>'
                               '<style>html{scroll-behavior:smooth}</style>')
        self.assertEqual(make_site.audit_snapshot(self.snapshot.read_text(), self.old_viewer), self.data)
        self.build()

    def test_manifest_tampering_cannot_hide_mime_or_dimensions(self):
        self.build()
        original = self.manifest()
        path = next(path for path, info in original["files"].items() if info["role"] == "thumbnail")
        manifest = copy.deepcopy(original)
        manifest["files"][path]["height"] += 1
        (self.site / make_site.MANIFEST).write_text(json.dumps(manifest))
        self.assert_refused()
        manifest = copy.deepcopy(original)
        manifest["stats"]["runs"] += 1
        (self.site / make_site.MANIFEST).write_text(json.dumps(manifest))
        self.assert_refused()
        for mime in ([], {}, None):
            manifest = copy.deepcopy(original)
            manifest["files"][path]["mime"] = mime
            with self.assertRaises(RuntimeError):
                make_site.validate_manifest(manifest)

    def test_render_helper_still_rejects_unserializable_nonfinite_data(self):
        with self.assertRaises(ValueError):
            make_snapshot.render_snapshot({"runs": [], "evidence": {}, "bad": float("nan")}, root=self.project)


class StaticSiteV2Tests(unittest.TestCase):
    def setUp(self):
        from test_originals import OriginalsTests
        self.legacy = StaticSiteTests()
        self.legacy.setUp()
        self.addCleanup(self.legacy.doCleanups)
        self.original = OriginalsTests()
        self.original.setUp()
        self.addCleanup(self.original.doCleanups)
        old_dir = self.original.directory
        self.original.runs[0]["id"] = "r1"
        self.original.directory = old_dir.with_name("r1")
        old_dir.rename(self.original.directory)
        self.original.register()
        self.original.audit["files"] = {key.replace("failed-run/", "r1/"): value for key, value in self.original.audit["files"].items()}
        self.runtime = self.legacy.project / "web/preview-runtime.js"
        # A deliberately recognizable synthetic trusted script, not a production
        # runtime fallback. Production integration is tested with the real file.
        self.runtime.write_text("window.SYNTHETIC_TRUSTED_RUNTIME = 1;\n")
        self.destination = self.legacy.root / "v2"
        self.legacy.build()

    def upgrade(self):
        return make_site.upgrade_site(self.legacy.site, self.destination, self.legacy.viewer,
                                      self.original.root, self.original.audit, root=self.legacy.project)

    def audit(self, name="index.html"):
        return make_site.audit_snapshot((self.destination / name).read_text(), self.legacy.viewer, static=name == "index.html")

    def test_upgrade_preserves_all_media_mappings_metadata_and_original_bytes(self):
        before = {path.relative_to(self.legacy.site): path.read_bytes() for path in self.legacy.site.rglob("*") if path.is_file()}
        old = self.legacy.audit()
        with mock.patch.object(make_site, "_thumbnail", side_effect=AssertionError("upgrade must not thumbnail")):
            stats = self.upgrade()
        online, inline = self.audit(), self.audit("offline.html")
        self.assertEqual(online["format"], "static-media-v2")
        self.assertEqual(online["transport"], "external")
        self.assertEqual(inline["transport"], "inline")
        for key in ("runs", "tasks", "assets", "evidence", "thumbnails"):
            self.assertEqual(old[key], online[key])
        self.assertEqual(inline["runs"], old["runs"])
        for path in old["assets"]:
            self.assertEqual((self.destination / path).read_bytes(), before[Path(path)])
        ext = online["originals"]["r1"]["package"]
        embedded = inline["originals"]["r1"]["package"]
        self.assertEqual((self.destination / ext["path"]).read_bytes(), base64.b64decode(embedded["base64"]))
        self.assertEqual(stats["original_packages"], 1)
        self.assertEqual(stats["original_files"], 6)
        self.assertEqual(stats["media_files"], len(old["assets"]))
        self.assertEqual(before, {path.relative_to(self.legacy.site): path.read_bytes() for path in self.legacy.site.rglob("*") if path.is_file()})
        self.assertEqual(make_site.verify_site(self.destination, self.legacy.viewer)["stats"], stats)
        html = (self.destination / "index.html").read_text()
        self.assertEqual(html.count("<script>"), 2)
        self.assertNotIn("<iframe", html)
        self.assertIn(self.runtime.read_text() + "\n" + self.legacy.viewer.read_text(), html)

    def test_v2_repeat_deterministic_and_v2_input_upgrade(self):
        stats = self.upgrade()
        self.assertEqual(self.upgrade(), stats)
        second = self.legacy.root / "v2-second"
        make_site.upgrade_site(self.destination, second, self.legacy.viewer, self.original.root,
                               self.original.audit, root=self.legacy.project)
        self.assertEqual({p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()},
                         {p.relative_to(self.destination): p.read_bytes() for p in self.destination.rglob("*") if p.is_file()})

    def test_explicit_runtime_validation_no_extracted_viewer_trust(self):
        self.upgrade()
        bad = self.legacy.root / "bad-runtime.js"
        bad.write_text("window.BAD=1;\n")
        with self.assertRaisesRegex(RuntimeError, "viewer mismatch"):
            make_site.verify_site(self.destination, self.legacy.viewer, preview_runtime=bad)
        original = self.runtime.read_bytes()
        self.runtime.unlink()
        with self.assertRaises(RuntimeError):
            make_site.verify_site(self.destination, self.legacy.viewer)
        self.runtime.write_bytes(original)
        make_site.verify_site(self.destination, self.legacy.viewer)

    def rewrite(self, name, value):
        raw = make_snapshot.render_snapshot(value, root=self.legacy.project).encode()
        (self.destination / name).write_bytes(raw)
        manifest = json.loads((self.destination / make_site.MANIFEST).read_bytes())
        manifest["files"][name].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        manifest["stats"]["index_bytes" if name == "index.html" else "offline_bytes"] = len(raw)
        (self.destination / make_site.MANIFEST).write_text(json.dumps(manifest))

    def test_v2_unknown_schema_transport_paths_original_binding(self):
        self.upgrade()
        online = self.audit()
        mutations = [lambda d: d.update(unknown=1), lambda d: d.update(transport="inline"),
                     lambda d: d["originals"].update(unknown=d["originals"]["r1"]),
                     lambda d: d["originals"]["r1"].update(entry_sha256="0" * 64),
                     lambda d: d["originals"]["r1"]["package"].update(path="media/" + "0" * 64 + ".json"),
                     lambda d: d["originals"]["r1"].update(missing=["invented.js"]),
                     lambda d: d["runs"][0].update(status="changed-history")]
        for mutation in mutations:
            data = copy.deepcopy(online)
            mutation(data)
            self.rewrite("index.html", data)
            with self.assertRaises(RuntimeError):
                make_site.verify_site(self.destination, self.legacy.viewer)
        self.rewrite("index.html", online)
        make_site.verify_site(self.destination, self.legacy.viewer)

    def test_missing_tampered_extra_original_and_mime_are_refused(self):
        self.upgrade()
        path = self.destination / self.audit()["originals"]["r1"]["package"]["path"]
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(RuntimeError):
            make_site.verify_site(self.destination, self.legacy.viewer)
        path.write_bytes(original + b"changed")
        with self.assertRaises(RuntimeError):
            make_site.verify_site(self.destination, self.legacy.viewer)
        path.write_bytes(original)
        extra = self.destination / "originals/unregistered.json"
        extra.write_bytes(original)
        with self.assertRaises(RuntimeError):
            make_site.verify_site(self.destination, self.legacy.viewer)
        extra.unlink()
        manifest = json.loads((self.destination / make_site.MANIFEST).read_bytes())
        manifest["files"][str(path.relative_to(self.destination))]["mime"] = "text/html"
        with self.assertRaises(RuntimeError):
            make_site.validate_manifest(manifest)

    def test_missing_dependency_and_not_reviewed_status_preserved(self):
        self.original.manifest["artifacts"] = [item for item in self.original.manifest["artifacts"] if item["path"] != "output/js/two.js"]
        self.original.write_manifest()
        self.upgrade()
        self.assertEqual(self.audit()["originals"]["r1"]["status"], "missing_dependencies")
        self.assertEqual(self.audit("offline.html")["originals"]["r1"]["missing"], ["js/two.js"])
        self.destination = self.legacy.root / "v2-unreviewed"
        self.original.audit["files"] = {}
        self.upgrade()
        self.assertEqual(self.audit()["originals"]["r1"]["status"], "not_reviewed")
        self.assertFalse((self.destination / "originals").exists())

    def test_missing_runtime_fails_without_output_and_legacy_still_builds(self):
        self.runtime.unlink()
        with self.assertRaises(FileNotFoundError):
            self.upgrade()
        self.assertFalse(self.destination.exists())
        self.legacy.build()

    def test_v1_manifest_and_index_reject_valid_v2_offline(self):
        self.upgrade()
        inline_bytes = (self.destination / "offline.html").read_bytes()
        online = self.legacy.audit()
        self.destination = self.legacy.site
        # Keep a strictly v1 manifest/index and no external originals, but bind
        # the v1 offline descriptor to the otherwise valid v2 inline report.
        (self.destination / "offline.html").write_bytes(inline_bytes)
        offline_info = make_site._file_info(inline_bytes, "text/html", "offline")
        manifest = json.loads((self.destination / make_site.MANIFEST).read_bytes())
        manifest["files"]["offline.html"] = offline_info
        manifest["stats"]["offline_bytes"] = len(inline_bytes)
        (self.destination / make_site.MANIFEST).write_text(json.dumps(manifest))
        online["offline"] = {"path": "offline.html", "sha256": offline_info["sha256"], "size": len(inline_bytes)}
        self.rewrite("index.html", online)
        with self.assertRaisesRegex(RuntimeError, "offline.*profile|offline.*format"):
            make_site.verify_site(self.destination, self.legacy.viewer)

    def test_direct_renderer_rejects_every_script_end_delimiter(self):
        for delimiter in (">", "/", " ", "\t", "\n", "\r", "\f"):
            for target in (self.runtime, self.legacy.viewer):
                original = target.read_bytes()
                try:
                    target.write_text("/* </ScRiPt" + delimiter + "> */")
                    for data in ({"format": "static-media-v2", "transport": "inline"},
                                 *([{}] if target == self.legacy.viewer else [])):
                        with self.subTest(delimiter=repr(delimiter), target=target.name, format=data.get("format")):
                            with self.assertRaisesRegex(RuntimeError, "script closing"):
                                make_snapshot.render_snapshot(data, root=self.legacy.project)
                finally:
                    target.write_bytes(original)

    def test_trusted_script_end_delimiter_rejected_json_stays_escaped(self):
        self.runtime.write_text("const text='</script><script>bad()</script>';\n")
        with self.assertRaisesRegex(RuntimeError, "script closing"):
            self.upgrade()
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
