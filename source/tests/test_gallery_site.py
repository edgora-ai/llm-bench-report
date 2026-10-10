"""Tiny synthetic gallery packages, not browser/performance evidence."""
import base64
import copy
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_gallery
import make_site
import make_snapshot


class GalleryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gallery-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        web = self.project / "web"
        web.mkdir(parents=True)
        self.viewer = web / "app.js"
        self.runtime = web / "preview-runtime.js"
        self.viewer.write_bytes(b"window.TRUSTED_UNIT_VIEWER=1;\n")
        self.runtime.write_bytes(b"window.TRUSTED_UNIT_RUNTIME=1;\n")
        (web / "styles.css").write_text("body {color:#123456}")
        (web / "index.html").write_text('<!doctype html><html><head>\n<link rel="stylesheet" href="/styles.css">\n'
                                        '<script src="/app.js" defer></script>\n</head><body><main id="workspace"></main></body></html>')
        (web / "public-gallery.html").write_text('<!doctype html><html><head><meta charset="utf-8">'
            '<style>{{PUBLIC_CSS}}</style></head><body><nav id="public-task-tabs">{{TASK_TABS}}</nav>'
            '<main id="public-grid">{{GALLERY_CARDS}}</main><span id="gallery-count">{{GALLERY_COUNT}}</span>'
            '<span id="gallery-total">{{GALLERY_TOTAL}}</span><script>window.BENCH_GALLERY={{GALLERY_SEED}};</script>'
            '<script>{{PUBLIC_JS}}</script></body></html>')
        (web / "public-gallery.css").write_text('body {background:#fefefe}.work-image {width:100%;height:auto}')
        (web / "public-gallery.js").write_text('window.TRUSTED_UNIT_GALLERY=1;\n')
        self.runs = []
        evidence = {}
        raw = BytesIO()
        Image.new("RGB", (48, 30), "navy").save(raw, "JPEG")
        image = "data:image/jpeg;base64," + base64.b64encode(raw.getvalue()).decode()
        # Shared "unit-tool" model identity for the first six crocodile attempts
        # so latest-only selection drops five; later models stay distinct.
        for i in range(12):
            run_id = f"{i + 1:032x}"
            paths = ["evidence/desktop.png"] if i < 9 else ["evidence/later.png", "evidence/mobile.png"] if i == 9 else []
            if i == 0:
                model = 'A Model <script>&"'
            elif i < 7:
                model = "Unit Model"
            elif i == 11:
                model = "Blackhole Model"
            else:
                model = f"Model {i:02d}"
            run = {"id": run_id, "date": "2026-10-08", "started_at": f"2026-10-08T01:{i:02d}:00Z",
                   "model": model, "tool": "unit-tool",
                   "task_id": "crocodile" if i < 11 else "blackhole", "purpose": "benchmark",
                   "status": "failed", "generation_status": "completed" if i in (0, 6, 11) else "failed",
                   "checks": [{"name": "entrypoint", "status": "pass"}],
                   "evaluation": {"status": "completed", "evidence": paths}, "reviews": [{"id": "unit-review"}] if i == 0 else []}
            if i == 1:
                run["evaluation"]["status"] = "failed"
            self.runs.append(run)
            for path in paths:
                evidence[run_id + "/" + path] = image
        self.data = {"runs": self.runs, "tasks": [{"id": "blackhole", "name": "Black hole"},
                     {"id": "crocodile", "name": 'Task < & " {{PUBLIC_JS}}'}], "evidence": evidence}
        snapshot = self.root / "snapshot.html"
        snapshot.write_text(make_snapshot.render_snapshot(self.data, root=self.project))
        legacy = self.root / "legacy"
        make_site.build_site(snapshot, legacy, self.viewer, root=self.project)
        archives = self.root / "archives"
        audit = {"version": 1, "reviewer": "assistant", "files": {}}
        for i, run in enumerate(self.runs):
            folder = archives / run["date"] / run["id"]
            folder.mkdir(parents=True)
            artifacts = []
            checksums = {}
            if i < 2:
                raw = ('<!doctype html><html><head><meta charset="utf-8">'
                       + ('<script src="missing.js"></script>' if i == 1 else '')
                       + '</head><body><p>Synthetic original</p></body></html>').encode()
                (folder / "output").mkdir()
                (folder / "output/index.html").write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                artifacts = [{"path": "output/index.html", "kind": "output", "sha256": digest, "size": len(raw)}]
                checksums["output/index.html"] = digest
                audit["files"][run["id"] + "/index.html"] = {"sha256": digest, "decision": "include", "reviewed_full": True,
                                                            "reason": "full synthetic fixture review"}
            (folder / "manifest.json").write_text(json.dumps({**run, "artifacts": artifacts}))
            (folder / "checksums.json").write_text(json.dumps(checksums))
        self.baseline = self.root / "baseline"
        make_site.upgrade_site(legacy, self.baseline, self.viewer, archives, audit, root=self.project)
        self.source_manifest = make_site.verify_site(self.baseline, self.viewer, preview_runtime=self.runtime)
        self.full_data = make_site.audit_snapshot((self.baseline / "index.html").read_text(), self.viewer,
                                                  static=True, preview_runtime=self.runtime)
        self.site = self.root / "gallery"

    def build(self, destination=None):
        return make_gallery.build_gallery(self.baseline, destination or self.site, self.viewer,
                                          input_runtime=self.runtime, root=self.project)

    def verify(self, baseline=True):
        return make_site.verify_site(self.site, self.viewer, preview_runtime=self.runtime,
                                     gallery_root=self.project, baseline_site=self.baseline if baseline else None)

    def seed(self):
        return make_gallery.audit_gallery((self.site / "index.html").read_bytes(), self.project)

    def manifest(self):
        return json.loads((self.site / make_site.MANIFEST).read_text())

    def snapshot_files(self, directory):
        return {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob('*') if path.is_file()}

    def rewrite_payload(self, path, raw):
        (self.site / path).write_bytes(raw)
        manifest = self.manifest()
        manifest["files"][path].update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
        if path == "index.html":
            manifest["stats"]["index_bytes"] = len(raw)
        (self.site / make_site.MANIFEST).write_text(json.dumps(manifest))


class GallerySiteTests(GalleryFixture):
    def test_projection_covers_latest_triples_preserves_status_and_originals(self):
        self.build()
        seed = self.seed()
        self.assertEqual(set(seed), {"version", "format", "tasks", "runs", "defaults", "counts", "full", "runtime", "offline"})
        # Fixture: 11 crocodile + 1 blackhole. First seven share "Unit Model"
        # (i=0..6) so latest drops 6; remaining distinct models stay (7 total).
        self.assertEqual(seed["counts"], {"runs": 7, "full_runs": 12, "benchmark": 7, "full_benchmark": 12, "reviews": 1})
        self.assertEqual(seed["defaults"], {"task_id": "crocodile", "purpose": "benchmark", "page_size": 8})
        expected = [self.runs[i]["id"] for i in (0, 11, 7, 8, 9, 10, 6)]
        self.assertEqual([r["id"] for r in seed["runs"]], expected)
        by_id = {run["id"]: run for run in seed["runs"]}
        latest_unit = by_id[self.runs[6]["id"]]
        self.assertEqual(latest_unit["status"], "completed")
        self.assertEqual(latest_unit["entry_status"], "pass")
        self.assertEqual(latest_unit["evaluation_status"], "completed")
        self.assertIsNone(latest_unit["prompt_version"])
        self.assertEqual(latest_unit["image"]["width"], 48)
        self.assertEqual(latest_unit["image"]["height"], 30)
        self.assertEqual(latest_unit["history_count"], 5)
        later = by_id[self.runs[8]["id"]]
        self.assertTrue(later["registered_media"])
        self.assertIsNotNone(later["image"])
        # runs[9] has later.png / mobile.png only → registered, no desktop image.
        registered_no_desktop = by_id[self.runs[9]["id"]]
        self.assertTrue(registered_no_desktop["registered_media"])
        self.assertIsNone(registered_no_desktop["image"])
        absent = by_id[self.runs[11]["id"]]
        self.assertFalse(absent["registered_media"])
        self.assertIsNone(absent["image"])
        dropped = {self.runs[i]["id"] for i in range(1, 6)} & {r["id"] for r in seed["runs"]}
        self.assertEqual(dropped, set())
        for run in seed["runs"]:
            self.assertEqual(run["original"], self.full_data["originals"][run["id"]])
            self.assertNotIn("metrics", run)
            self.assertNotIn("reviews", run)
        self.assertEqual(self.verify()["version"], 3)

    def test_unchanged_v2_viewer_runtime_media_offline_and_original_bytes(self):
        before = self.snapshot_files(self.baseline)
        with mock.patch.object(make_site, "build_originals", side_effect=AssertionError("must not rebuild originals")), \
             mock.patch.object(make_site, "_thumbnail", side_effect=AssertionError("must not reencode media")), \
             mock.patch.object(make_site, "render_snapshot", side_effect=AssertionError("must not rerender full report")):
            stats = self.build()
        seed = self.seed()
        self.assertEqual((self.site / seed["full"]["path"]).read_bytes(), before["index.html"])
        self.assertEqual((self.site / seed["runtime"]["path"]).read_bytes(), self.runtime.read_bytes())
        self.assertEqual(seed["full"]["script_sha256"], hashlib.sha256(self.runtime.read_bytes() + b"\n" + self.viewer.read_bytes()).hexdigest())
        for path, raw in before.items():
            if path not in {"index.html", make_site.MANIFEST}:
                self.assertEqual((self.site / path).read_bytes(), raw)
                self.assertEqual(self.manifest()["files"][path], self.source_manifest["files"][path])
        self.assertEqual(before, self.snapshot_files(self.baseline))
        self.assertEqual(stats["reviews"], 1)
        self.assertEqual(self.verify()["stats"], stats)

    def test_server_markup_is_real_page8_escaped_and_lazily_loaded(self):
        self.build()
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = []
            def handle_starttag(self, tag, attrs):
                self.elements.append((tag, dict(attrs)))
        parsed = Markup()
        html = (self.site / "index.html").read_text()
        parsed.feed(html)
        articles = [attrs for tag, attrs in parsed.elements if tag == "article"]
        images = [attrs for tag, attrs in parsed.elements if tag == "img"]
        self.assertEqual([attrs["data-run-id"] for attrs in articles], [run["id"] for run in make_gallery.initial_runs(self.seed())[:8]])
        self.assertEqual(len(articles), 5)
        self.assertEqual(len(images), 4)
        for attrs in images:
            self.assertIn("data-src", attrs)
            self.assertNotIn("src", attrs)
        for action in ("run-original", "select-run", "zoom-run", "detail-run"):
            self.assertEqual(sum("data-" + action in attrs for tag, attrs in parsed.elements if tag == "button"), 5)
        self.assertIn("&lt;script&gt;&amp;&quot;", html)
        self.assertIn('Task &lt; &amp; &quot; {{PUBLIC_JS}}', html)
        self.assertEqual(len([tag for tag, _ in parsed.elements if tag == "script"]), 2)
        original_buttons = {a["data-run-original"]: a for tag, a in parsed.elements if "data-run-original" in a}
        # Latest crocodile media run among page cards; blackhole is no-media and not
        # server-rendered as a card. Unit Model latest (runs[6]) has no original.
        self.assertIn("disabled", original_buttons[self.runs[6]["id"]])
        self.assertNotIn("disabled", original_buttons[self.runs[0]["id"]])

    def test_first_desktop_only_and_registration_requires_included_evidence(self):
        data = copy.deepcopy(self.full_data)
        run = data["runs"][0]
        desktop = run["id"] + "/evidence/desktop.png"
        del data["evidence"][desktop]
        seed = make_gallery.project_gallery(data, b"full", b"runtime", b"viewer")
        first = next(item for item in seed["runs"] if item["id"] == run["id"])
        self.assertFalse(first["registered_media"])
        self.assertIsNone(first["image"])

    def test_fallback_tasks_and_missing_fields_do_not_invent_success(self):
        data = copy.deepcopy(self.full_data)
        data["tasks"] = [{"id": "blackhole", "name": "source name"}]
        for run in data["runs"]:
            run["task_id"] = "blackhole"
            for key in ("model", "tool", "status", "generation_status", "started_at", "date", "checks", "evaluation"):
                run.pop(key, None)
        seed = make_gallery.project_gallery(data, b"full", b"runtime", b"viewer")
        self.assertEqual(seed["defaults"]["task_id"], "blackhole")
        for run in seed["runs"]:
            for key in ("model", "tool", "status", "entry_status", "evaluation_status"):
                self.assertEqual(run[key], "unknown")
            self.assertIsNone(run["started_at"])
            self.assertIsNone(run["date"])
            self.assertFalse(run["registered_media"])

    def test_entrypoint_semantics_match_legacy(self):
        for status, expected in (("pass", "pass"), ("passed", "pass"), ("ok", "pass"), ("fail", "fail"), ("failed", "fail"), ("unknown", "unknown")):
            self.assertEqual(make_gallery.entrypoint_status({"checks": [{"name": "entrypoint", "status": status}]}), expected)
            self.assertEqual(make_gallery.entrypoint_status({"evaluation": {"checks": [{"name": "entrypoint", "status": status}]}}), expected)
        self.assertEqual(make_gallery.entrypoint_status({"evaluation": {"status": "running"}, "checks": [{"name": "entrypoint", "status": "pass"}]}), "unknown")
        self.assertEqual(make_gallery.entrypoint_status({"checks": [{"name": "entrypoint", "status": "fail"}], "evaluation": {"checks": [{"name": "entrypoint", "status": "pass"}]}}), "fail")

    def test_build_is_deterministic_idempotent_and_does_not_adopt_nonempty(self):
        self.build()
        before = self.snapshot_files(self.site)
        times = {p: (self.site / p).stat().st_mtime_ns for p in before}
        self.build()
        self.assertEqual(before, self.snapshot_files(self.site))
        self.assertEqual(times, {p: (self.site / p).stat().st_mtime_ns for p in before})
        other = self.root / "other"
        self.build(other)
        self.assertEqual(before, self.snapshot_files(other))
        blocked = self.root / "blocked"
        blocked.mkdir()
        (blocked / "sentinel").write_bytes(b"retain")
        with self.assertRaises(RuntimeError):
            self.build(blocked)
        self.assertEqual((blocked / "sentinel").read_bytes(), b"retain")

    def test_hash_updated_public_script_css_markup_and_seed_tampering_rejected(self):
        self.build()
        original = (self.site / "index.html").read_bytes()
        for old, new in ((b"TRUSTED_UNIT_GALLERY=1", b"TRUSTED_UNIT_GALLERY=2"),
                         (b"#fefefe", b"#eeeeee"), (b"work-name", b"fake-name"),
                         (b'"registered_media":true', b'"registered_media":1'),
                         (b'"history_count":0', b'"history_count":"x"'),
                         (b'"version":3', b'"version":2')):
            with self.subTest(old=old):
                self.assertIn(old, original)
                self.rewrite_payload("index.html", original.replace(old, new, 1))
                with self.assertRaises(RuntimeError):
                    self.verify(baseline=False)
        self.rewrite_payload("index.html", original)
        self.verify()

    def test_full_viewer_runtime_and_retained_assets_tampering_rejected(self):
        self.build()
        seed = self.seed()
        for path in (seed["full"]["path"], seed["runtime"]["path"], "offline.html",
                     next(path for path in self.manifest()["files"] if path.startswith("originals/")),
                     next(path for path in self.manifest()["files"] if path.startswith("media/"))):
            with self.subTest(path=path):
                raw = (self.site / path).read_bytes()
                (self.site / path).write_bytes(raw + b"tamper")
                with self.assertRaises(RuntimeError):
                    self.verify(baseline=False)
                (self.site / path).write_bytes(raw)
        self.verify()

    def test_untrusted_runtime_with_content_address_and_self_reported_hash_rejected(self):
        self.build()
        seed = self.seed()
        old = seed["runtime"]["path"]
        raw = b"window.UNTRUSTED_RUNTIME=1;"
        new = "viewer/" + hashlib.sha256(raw).hexdigest() + ".js"
        manifest = self.manifest()
        del manifest["files"][old]
        manifest["files"][new] = make_site._file_info(raw, "text/javascript", "runtime")
        (self.site / old).unlink()
        (self.site / new).write_bytes(raw)
        seed["runtime"] = {"path": new, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        index = make_gallery.render_gallery(seed, self.project)
        (self.site / "index.html").write_bytes(index)
        manifest["files"]["index.html"] = make_site._file_info(index, "text/html", "index")
        manifest["stats"]["index_bytes"] = len(index)
        (self.site / make_site.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "untrusted gallery preview runtime"):
            self.verify(baseline=False)

    def test_forged_full_program_and_self_reported_digest_are_rejected(self):
        self.build()
        seed = self.seed()
        old = seed["full"]["path"]
        raw = (self.site / old).read_bytes().replace(b"TRUSTED_UNIT_VIEWER=1", b"UNTRUSTED_UNIT_VIEWER=1")
        new = "viewer/" + hashlib.sha256(raw).hexdigest() + ".html"
        manifest = self.manifest()
        del manifest["files"][old]
        manifest["files"][new] = make_site._file_info(raw, "text/html", "viewer")
        (self.site / old).unlink()
        (self.site / new).write_bytes(raw)
        seed["full"] = {"path": new, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                        "script_sha256": hashlib.sha256(self.runtime.read_bytes() + b"\nwindow.UNTRUSTED_UNIT_VIEWER=1;\n").hexdigest()}
        index = make_gallery.render_gallery(seed, self.project)
        (self.site / "index.html").write_bytes(index)
        manifest["files"]["index.html"] = make_site._file_info(index, "text/html", "index")
        manifest["stats"]["index_bytes"] = len(index)
        (self.site / make_site.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "input viewer mismatch"):
            self.verify(baseline=False)

    def test_original_descriptor_or_omitted_full_run_cannot_be_approved_by_seed(self):
        self.build()
        for mutate in (lambda seed: seed["runs"].pop(), lambda seed: seed["runs"][2]["original"].update(status="ready"),
                       lambda seed: seed["runs"][0].update(extra="unknown field")):
            seed = self.seed()
            mutate(seed)
            self.rewrite_payload("index.html", make_gallery.render_gallery(seed, self.project))
            with self.assertRaises(RuntimeError):
                self.verify(baseline=False)
            self.rewrite_payload("index.html", make_gallery.render_gallery(make_gallery.project_gallery(self.full_data,
                (self.baseline / "index.html").read_bytes(), self.runtime.read_bytes(), self.viewer.read_bytes()), self.project))
        self.verify()

    def test_inventory_and_symlinks_rejected(self):
        self.build()
        for name in ("viewer/extra.js", "extra.txt", "viewer/" + "f" * 64 + ".html"):
            path = self.site / name
            path.write_bytes(b"unregistered")
            with self.assertRaises(RuntimeError):
                self.verify()
            path.unlink()
        folder = self.site / "unknown"
        folder.mkdir()
        with self.assertRaises(RuntimeError):
            self.verify()
        folder.rmdir()
        path = self.site / self.seed()["runtime"]["path"]
        raw = path.read_bytes()
        outside = self.root / "outside.js"
        outside.write_bytes(raw)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.verify()
        path.unlink()
        path.write_bytes(raw)
        moved = self.root / "viewer-moved"
        (self.site / "viewer").rename(moved)
        (self.site / "viewer").symlink_to(moved, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_manifest_exact_roles_and_legacy_branches_remain_strict(self):
        self.build()
        original = self.manifest()
        full_path = self.seed()["full"]["path"]
        for mutate in (lambda m: m.update(version=2), lambda m: m.update(format=make_site.FORMAT),
                       lambda m: m.update(extra=1), lambda m: m["files"][full_path].update(mime="application/json"),
                       lambda m: m["files"][full_path].update(role="runtime"),
                       lambda m: m["files"][full_path].update(width=48, height=30),
                       lambda m: m["files"].pop(full_path),
                       lambda m: m["stats"].update(index_bytes=True)):
            manifest = copy.deepcopy(original)
            mutate(manifest)
            with self.assertRaises(RuntimeError):
                make_site.validate_manifest(manifest)
        self.assertEqual(make_site.verify_site(self.baseline, self.viewer, preview_runtime=self.runtime)["version"], 2)
        legacy = copy.deepcopy(self.source_manifest)
        legacy["files"][full_path] = original["files"][full_path]
        with self.assertRaises(RuntimeError):
            make_site.validate_manifest(legacy)

    def test_missing_duplicate_markers_and_oversize_entrypoint_fail_without_output(self):
        template = self.project / "web/public-gallery.html"
        original = template.read_bytes()
        for raw in (original.replace(b"{{GALLERY_SEED}}", b""), original + b"{{GALLERY_CARDS}}",
                    original + b"x" * (101 * 1024)):
            template.write_bytes(raw)
            with self.assertRaises(RuntimeError):
                self.build()
            self.assertFalse(self.site.exists())
        template.write_bytes(original)

    def test_gzip_budget_failure_leaves_destination_absent(self):
        css = self.project / "web/public-gallery.css"
        css.write_text('body{color:#123456}/*' + ''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(800)) + '*/')
        with self.assertRaisesRegex(RuntimeError, "gzip entrypoint budget"):
            self.build()
        self.assertFalse(self.site.exists())
        self.assertFalse(any(path.name.startswith('.static-site-') for path in self.root.iterdir()))

    def test_cli_help_required_arguments_and_real_fixture_build(self):
        script = str(Path(make_gallery.__file__).resolve())
        help_result = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--input-runtime", help_result.stdout)
        missing = subprocess.run([sys.executable, script], capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        command = [sys.executable, "-I", script, str(self.baseline), str(self.site), "--input-viewer", str(self.viewer),
                   "--input-runtime", str(self.runtime), "--root", str(self.project)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # CLI stats still describe the full embedded v2 bundle; seed runs are latest-only.
        self.assertEqual(payload["runs"], 12)
        self.assertEqual(self.seed()["counts"]["runs"], 7)
        self.verify()


if __name__ == "__main__":
    unittest.main()
