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
        self.assertEqual(set(seed), {"version", "format", "score_encoding", "tasks", "runs", "score_scopes", "defaults", "counts", "full", "runtime", "offline"})
        self.assertEqual(seed['score_encoding'], 'scope-index-v1')
        # Fixture: 11 crocodile + 1 blackhole. First seven share "Unit Model"
        # (i=0..6) so latest drops 6; remaining distinct models stay (7 total).
        self.assertEqual(seed["counts"], {"runs": 7, "full_runs": 12, "benchmark": 7, "full_benchmark": 12, "reviews": 1})
        self.assertEqual(seed["defaults"], {"task_id": "crocodile", "purpose": "benchmark", "page_size": 2,
                                          "mobile_page_size": 1, "score_source": '[null,"label-unverified",null,null]'})
        self.assertEqual(seed["score_scopes"], [[None, "label-unverified", None, None]])
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

    def test_server_markup_is_real_page2_escaped_and_lazily_loaded(self):
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
        self.assertEqual([attrs["data-run-id"] for attrs in articles], [run["id"] for run in make_gallery.initial_runs(self.seed())[:2]])
        self.assertEqual(len(articles), 2)
        self.assertEqual(len(images), 2)
        for attrs in images:
            self.assertIn("data-src", attrs)
            self.assertNotIn("src", attrs)
        for action in ("run-original", "select-run", "zoom-run", "detail-run"):
            self.assertEqual(sum("data-" + action in attrs for tag, attrs in parsed.elements if tag == "button"), 2)
        self.assertIn("&lt;script&gt;&amp;&quot;", html)
        self.assertIn('Task &lt; &amp; &quot; {{PUBLIC_JS}}', html)
        self.assertEqual(len([tag for tag, _ in parsed.elements if tag == "script"]), 2)
        original_buttons = {a["data-run-original"]: a for tag, a in parsed.elements if "data-run-original" in a}
        # Every initial card retains exact disabled semantics; pagination changes
        # which records are on the first page, never the available action itself.
        self.assertIn("disabled", original_buttons[self.runs[7]["id"]])
        self.assertNotIn("disabled", original_buttons[self.runs[0]["id"]])

    def test_direct_preview_visibility_stop_and_stale_async_cleanup(self):
        program = r'''
const fs=require('fs'),assert=require('assert'),code=fs.readFileSync(process.argv[1],'utf8');
const start=code.indexOf('  function previewButtons('),end=code.indexOf('  async function digest(',start);
let activePreview=null,created=0,destroyed=0,disconnected=0;
const cardPreviews=new Map(),previewObserver={disconnect(){disconnected++}};
const document={hidden:false},$=()=>({open:false});
const allowedOriginal=run=>['ready','missing_dependencies'].includes(run.original.status);
const sdk={create(host,options){created++;assert.strictEqual(options.transport,'external');assert.strictEqual(options.descriptor.status,'ready');return{async start(){options.onState({status:'loaded',message:'loaded'});},destroy(){destroyed++;}};}};
let ensureSDK=async()=>sdk;
eval(code.slice(start,end));
(async()=>{
 const c={run:{id:'a'.repeat(32),original:{status:'ready'}},node:{isConnected:true,dataset:{}},host:{style:{},replaceChildren(){}},status:{},start:{},viewport:'fit',token:0,runtime:null,alive:true,inline:true,visible:false,paused:false};
 cardPreviews.set(c.run.id,c);
 await startPreview(c);assert.strictEqual(created,0);
 c.visible=true;await startPreview(c);assert.strictEqual(created,1);assert.strictEqual(c.node.dataset.previewState,'loaded');assert.strictEqual(c.host.style.visibility,'visible');assert.match(c.status.textContent,/实时原作.*非录像/);
 togglePreview(c);assert.strictEqual(destroyed,1);assert.strictEqual(c.runtime,null);assert.match(c.status.textContent,/静态回退/);
 resumeCards();assert.strictEqual(created,1);
 togglePreview(c);await new Promise(resolve=>setImmediate(resolve));assert.strictEqual(created,2);
 c.visible=false;stopPreview(c);assert.strictEqual(destroyed,2);assert.strictEqual(c.runtime,null);
 let release;ensureSDK=()=>new Promise(resolve=>{release=resolve});c.visible=true;
 const pending=startPreview(c);c.visible=false;stopPreview(c);release(sdk);await pending;assert.strictEqual(created,2);assert.strictEqual(c.runtime,null);
 ensureSDK=async()=>{throw new Error('test download failure')};c.visible=true;c.paused=false;await startPreview(c);assert.strictEqual(c.node.dataset.previewState,'error');assert.match(c.status.textContent,/静态回退/);assert.strictEqual(c.runtime,null);
 clearCards();assert.strictEqual(cardPreviews.size,0);assert.strictEqual(c.alive,false);assert.strictEqual(disconnected,1);
 console.log('direct original visibility, explicit stop, stale async, error fallback and cleanup passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT / 'web/public-gallery.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cleanup passed', result.stdout)

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

    def test_score_reference_cannot_replace_its_raw_review_even_with_rehashed_markup(self):
        self.build()
        original = (self.site / 'index.html').read_bytes()
        for slot, replacement in ((1, 999), (2, [0, None, None, None, None, None])):
            seed = self.seed()
            row = next(row for row in seed['runs'] if 'scores' in row)
            row['scores'][0][slot] = replacement
            self.rewrite_payload('index.html', make_gallery.render_gallery(seed, self.project))
            with self.subTest(slot=slot), self.assertRaisesRegex(RuntimeError, 'projection differs'):
                self.verify(baseline=False)
            self.rewrite_payload('index.html', original)
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
        help_result = subprocess.run([sys.executable, '-I', '-B', script, "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--input-runtime", help_result.stdout)
        missing = subprocess.run([sys.executable, '-I', '-B', script], capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        command = [sys.executable, "-I", '-B', script, str(self.baseline), str(self.site), "--input-viewer", str(self.viewer),
                   "--input-runtime", str(self.runtime), "--root", str(self.project)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # CLI stats still describe the full embedded v2 bundle; seed runs are latest-only.
        self.assertEqual(payload["runs"], 12)
        self.assertEqual(self.seed()["counts"]["runs"], 7)
        self.verify()


class ModelVisibilityTests(unittest.TestCase):
    """Synthetic UI contracts only; do not create benchmark runs or reviews."""
    def test_existing_model_controls_are_open_and_in_normal_flow(self):
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parents = []
                self.model_parent = None
                self.model_attrs = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get('id') == 'public-models':
                    self.model_parent = self.parents[-1]
                    self.model_attrs = attrs
                if tag not in {'meta', 'input', 'link', 'img'}:
                    self.parents.append((tag, attrs))
            def handle_endtag(self, tag):
                if self.parents and self.parents[-1][0] == tag:
                    self.parents.pop()
        web = make_snapshot.ROOT / 'web'
        parsed = Markup()
        parsed.feed((web / 'public-gallery.html').read_text())
        self.assertIn('open', parsed.model_attrs)
        self.assertEqual(parsed.model_parent, ('div', {'class': 'gallery-controls'}))
        css = (web / 'public-gallery.css').read_text()
        menu = css.split('.model-menu{', 1)[1].split('}', 1)[0]
        models = css.split('.public-model-list{', 1)[1].split('}', 1)[0]
        self.assertNotIn('position:absolute', menu)
        self.assertNotIn('max-height', models)
        self.assertNotIn('overflow', models)

    def test_model_filter_exposes_attempts_without_media_without_reordering(self):
        program = r'''
const fs=require('fs'),assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8');
const start=code.indexOf('  function cardRuns('),end=code.indexOf('  const shortScope=',start);
const state={model:'',sort:''},sortedRuns=runs=>runs;
const runs=[{id:'synthetic-media',registered_media:true},{id:'synthetic-original-only',registered_media:false},{id:'synthetic-no-entrypoint',registered_media:false}];
const before=JSON.stringify(runs);
eval(code.slice(start,end));
assert.deepEqual(cardRuns(runs),[runs[0]]);
state.model=' synthetic ';assert.deepEqual(cardRuns(runs),runs);
state.model='   ';assert.deepEqual(cardRuns(runs),[runs[0]]);
state.sort='compliance';assert.deepEqual(cardRuns(runs),runs);
assert.equal(JSON.stringify(runs),before);
console.log('selected model retains media-free and missing attempts');
'''
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT / 'web/public-gallery.js')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('retains media-free and missing attempts', result.stdout)

    def test_missing_card_names_the_model_and_keeps_scores_uninvented(self):
        run = {'id': 'a' * 32, 'model': 'Synthetic absent model', 'tool': 'Synthetic tool',
               'task_id': 'crocodile', 'purpose': 'benchmark', 'started_at': '2026-01-01',
               'evaluation': {}, 'reviews': [], 'metrics': {}}
        data = {'runs': [run], 'evidence': {}, 'thumbnails': {}, 'assets': {},
                'originals': {run['id']: {'status': 'no_entrypoint', 'package': None}}}
        row = make_gallery.project_runs(data)[0]
        html = make_gallery.render_card(row)
        self.assertIn('class="evidence-card no-registered-media"', html)
        self.assertIn('aria-label="Synthetic absent model"', html)
        self.assertIn('未交付，没有可展示 SVG/HTML', html)
        self.assertEqual(html.count('未评审</span>'), 6)
        self.assertIn('data-run-original="' + run['id'] + '" disabled', html)
        self.assertNotIn('scores', row)
        self.assertIsNone(row['original']['package'])


class ModelDiscoverabilityTests(unittest.TestCase):
    """Synthetic selection contracts only; never new model output or grading."""

    def test_model_controls_are_expanded_in_normal_flow_without_internal_scroll(self):
        root = make_snapshot.ROOT
        template = (root / 'web/public-gallery.html').read_text()
        css = (root / 'web/public-gallery.css').read_text()
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.models = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get('id') == 'public-models':
                    self.models = (tag, attrs)
        parsed = Controls(); parsed.feed(template)
        self.assertEqual(parsed.models[0], 'details')
        self.assertIn('open', parsed.models[1])
        for selector in ('.model-menu{', '.public-model-list{'):
            rule = css.split(selector, 1)[1].split('}', 1)[0]
            self.assertNotIn('position:absolute', rule)
            self.assertNotIn('max-height', rule)
            self.assertNotIn('overflow:auto', rule)

    def test_selected_missing_model_is_a_named_card_without_invented_delivery(self):
        source = (make_snapshot.ROOT / 'web/public-gallery.js').read_text()
        self.assertEqual(source.count('  function cardRuns('), 1)
        program = r'''
const fs=require('fs'),assert=require('node:assert/strict'),code=fs.readFileSync(process.argv[1],'utf8');
const start=code.indexOf('  function cardRuns('),end=code.indexOf('  const shortScope=',start);
const state={sort:'',model:''},sortedRuns=runs=>runs;
eval(code.slice(start,end));
const ready={id:'ready',registered_media:true,original:{status:'ready'}};
const absent={id:'missing',model:'fixture-missing-model',registered_media:false,original:{status:'no_entrypoint'}};
const runs=[ready,absent];
assert.deepEqual(cardRuns(runs),[ready]);
state.model='fixture-missing-model';assert.deepEqual(cardRuns(runs),runs);
state.model=' ';assert.deepEqual(cardRuns(runs),[ready]);
state.sort='compliance';assert.deepEqual(cardRuns(runs),runs);
assert.equal(absent.original.status,'no_entrypoint');assert.equal(absent.scores,undefined);
assert.match(code,/m\.hidden=!!state\.sort\|\|!!state\.model\.trim\(\)/);
console.log('selected no-media model remains in the named-card projection');
'''
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT / 'web/public-gallery.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = {'id': 'a' * 32, 'model': 'fixture-missing-model', 'tool': 'fixture-tool', 'task_id': 'crocodile',
               'purpose': 'benchmark', 'registered_media': False, 'media_counts': [0, 0], 'image': None,
               'original': {'status': 'no_entrypoint'}, 'meter': [None] * 5,
               'date': '2026-01-01', 'prompt_version': 'fixture-v1', 'status': 'failed',
               'entry_status': 'fail', 'evaluation_status': 'completed'}
        html = make_gallery.render_card(run)
        self.assertIn('fixture-missing-model</h2>', html)
        self.assertIn('未交付，没有可展示 SVG/HTML', html)
        self.assertIn('未评审', html)
        self.assertIn('data-run-original="' + run['id'] + '" disabled', html)


class ScoreEdgeContractTests(unittest.TestCase):
    def run_fixture(self, reviews, **fields):
        return {"id": "a" * 32, "task_id": "crocodile", "purpose": "benchmark",
                "conditions": {"rubric_version": "visual-v1"}, "reviews": reviews, **fields}

    def review(self, value=3, **fields):
        return {"id": "a", "created_at": "2026-10-01T00:00:00Z", "blind": False,
                "reviewer": "AI 建议评分（待操作员复核）", "scores": {"compliance": value}, **fields}

    def test_valid_zero_partial_null_and_invalid_are_not_coerced(self):
        for value, expected in ((0, 0), (4, 4), (2.5, 2.5), (None, None), (True, None),
                                (False, None), ("3", None), (-1, None), (4.01, None),
                                (float("nan"), None), (float("inf"), None)):
            with self.subTest(value=value):
                summary = make_gallery.score_summaries(self.run_fixture([self.review(value)]))[0]
                self.assertEqual(summary["values"], [expected, None, None, None, None, None])
                self.assertEqual(set(summary), {"scope", "id", "created_at", "index", "values"})

    def test_newest_not_best_and_no_partial_backfill(self):
        older = self.review(4, scores={key: 4 for key in make_gallery.SCORE_LABELS}, note="private note")
        newer = self.review(0, id="b", created_at="2026-10-02T00:00:00Z", private_metadata={"x": 1})
        run = self.run_fixture([older, newer])
        before = copy.deepcopy(run)
        summaries = make_gallery.score_summaries(run)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["id"], "b")
        self.assertEqual(summaries[0]["values"], [0, None, None, None, None, None])
        self.assertEqual(run, before)
        self.assertNotIn("private", json.dumps(summaries))
        self.assertEqual(make_gallery.score_summaries(self.run_fixture([])), [])

    def test_timestamp_then_id_then_stable_source_index(self):
        reviews = [self.review(4, id="z", created_at="2026-10-01T03:00:00+02:00"),
                   self.review(2, id="b", created_at="2026-10-01T02:00:00Z"),
                   self.review(1, id="c", created_at="2026-10-01T02:00:00Z"),
                   self.review(0, id="c", created_at="2026-10-01T02:00:00Z")]
        selected = make_gallery.score_summaries(self.run_fixture(reviews))[0]
        self.assertEqual((selected["id"], selected["index"], selected["values"][0]), ("c", 3, 0))

    def test_sources_reviewer_blindness_and_protocol_are_separable(self):
        reviews = [self.review(), self.review(reviewer="AI 第二来源建议"),
                   self.review(blind=True), self.review(reviewer="declared-source"),
                   self.review(rubric_version="visual-v2"), self.review(blind=None)]
        summaries = make_gallery.score_summaries(self.run_fixture(reviews))
        self.assertEqual(len(summaries), 6)
        self.assertEqual(len({s["scope"] for s in summaries}), 6)
        unknown = make_gallery.score_summaries(self.run_fixture([self.review()], conditions={}))[0]
        self.assertIsNone(json.loads(unknown["scope"])[0])
        # Never infer the old run protocol from a task definition.
        self.assertIsNone(make_gallery.score_scope(self.run_fixture([], conditions={}), self.review())[0])
        conflict = make_gallery.score_scope(self.run_fixture([]), self.review(rubric_version="visual-v2"))
        self.assertIsNone(conflict[0])
        self.assertFalse(make_gallery.scope_rankable(conflict))
        # An arbitrary source string/private object is not an authenticated scope.
        normal = make_gallery.score_summaries(self.run_fixture([self.review()]))
        opaque = make_gallery.score_summaries(self.run_fixture([self.review(source={"private_path": "/private/source"})]))
        self.assertEqual(normal, opaque)
        self.assertNotIn("private", json.dumps(opaque))

    def test_metrics_preserve_real_zero_and_unknown_without_mutating_source(self):
        row = {"duration_ms": 0, "metrics": {"cost_usd": 0, "cost_source": "cli_reported_unverified"}}
        self.assertEqual(make_gallery.meter(row), [0, 0, "cli_reported_unverified", None, None])
        for bad in (None, "0", True, -1, float("nan")):
            self.assertEqual(make_gallery.meter({"duration_ms": bad, "metrics": {"cost_usd": bad}}), [None, None, None, None, None])


class ScoreContractTests(unittest.TestCase):
    def review(self, value=3, **fields):
        return {"id": "a", "reviewer": "AI 建议评分", "blind": False,
                "created_at": "2026-01-01T00:00:00Z", "scores": {"compliance": value}, **fields}

    def run_record(self, reviews, **fields):
        return {"id": "a" * 32, "task_id": "crocodile", "purpose": "benchmark",
                "conditions": {"rubric_version": "visual-v1"}, "reviews": reviews, **fields}

    def test_zero_null_partial_and_invalid_are_not_coerced(self):
        for value in (0, 4, 1.5):
            summary = make_gallery.score_summaries(self.run_record([self.review(value)]))[0]
            self.assertEqual(summary["values"], [value, None, None, None, None, None])
        for value in (None, "3", True, False, -1, 4.1, float("inf"), float("nan"), [], {}):
            with self.subTest(value=value):
                self.assertIsNone(make_gallery.score_summaries(self.run_record([self.review(value)]))[0]["values"][0])

    def test_latest_never_best_backfilled_or_borrowed(self):
        old = self.review(4, scores={dimension: 4 for dimension in make_gallery.SCORE_LABELS})
        new = self.review(None, id="b", created_at="2026-02-01T00:00:00Z", scores={"motion": 0})
        run = self.run_record([old, new])
        before = copy.deepcopy(run)
        summary = make_gallery.score_summaries(run)[0]
        self.assertEqual(summary["id"], "b")
        self.assertEqual(summary["index"], 1)
        self.assertEqual(summary["values"], [None, None, 0, None, None, None])
        self.assertEqual(run, before)
        self.assertEqual(make_gallery.score_summaries(self.run_record([])), [])

    def test_timestamp_id_and_stable_index_ties(self):
        # Equivalent offsets are a real time tie; higher ID wins, then later source index.
        run = self.run_record([self.review(4, id="z"), self.review(2, id="b", created_at="2026-01-01T01:00:00+01:00"),
                               self.review(0, id="z", created_at="2026-01-01T00:00:00Z")])
        self.assertEqual(make_gallery.score_summaries(run)[0]["values"][0], 0)
        self.assertEqual(make_gallery.score_summaries(run)[0]["index"], 2)
        missing = self.run_record([self.review(4, created_at=None), self.review(1)])
        self.assertEqual(make_gallery.score_summaries(missing)[0]["values"][0], 1)

    def test_sources_protocols_and_exact_reviewer_labels_are_separate(self):
        run = self.run_record([self.review(), self.review(0, reviewer="AI 其他建议"), self.review(1, blind=True),
                               self.review(2, reviewer="Operator"), self.review(4, blind=None)])
        summaries = make_gallery.score_summaries(run)
        self.assertEqual(len(summaries), 5)
        scopes = make_gallery.score_scopes([run])
        self.assertEqual(sum(make_gallery.scope_rankable(scope) for scope in scopes), 4)
        preferred = json.loads(make_gallery.default_score_source(scopes))
        self.assertEqual(preferred[1:3], ["ai-suggestion", False])
        for conditions in ({}, {"rubric_version": "unknown"}, {"rubric_version": "visual-v2"}):
            foreign = self.run_record([self.review(4)], conditions=conditions)
            self.assertFalse(make_gallery.scope_rankable(make_gallery.score_scope(foreign, foreign["reviews"][0])))
        # Task definitions cannot lend their rubric to an old run.
        self.assertIsNone(make_gallery.score_scope(self.run_record([self.review()], conditions={}), self.review())[0])

    def test_summary_identity_conflicts_invalid_ids_and_source_privacy(self):
        reviews = [self.review(4, rubric_version='visual-v2'),
                   self.review(0, source={'rubric_version': 'visual-v2', 'private': 'do not copy'}),
                   self.review(1, run_id='b' * 32), self.review(2, id=True, reviewer='Operator'),
                   self.review(3, id=-2, reviewer='Operator 2')]
        run = self.run_record(reviews)
        before = copy.deepcopy(run)
        summaries = make_gallery.score_summaries(run)
        for summary in summaries:
            scope = json.loads(summary['scope'])
            if scope[3] == 'AI 建议评分':
                self.assertFalse(make_gallery.scope_rankable(scope))
            else:
                self.assertIsNone(summary['id'])
        self.assertNotIn('private', json.dumps(summaries))
        self.assertEqual(run, before)
        self.assertFalse(make_gallery.valid_number(10 ** 999))

    def test_python_independent_audit_and_javascript_summary_parity(self):
        import verify_public_snapshot as audit
        runs = [self.run_record([self.review(value)]) for value in (0, 4, 2.5, True, False, '3', None, [], {})]
        runs += [self.run_record([self.review(4, id='z', created_at='2026-01-01T00:00:00.000001Z'),
                                 self.review(0, id='a', created_at='2026-01-01T00:00:00.000002Z')]),
                 self.run_record([self.review(4, id='z', created_at='2026-02-30T00:00:00Z'), self.review(0)]),
                 self.run_record([self.review(4), self.review(None, id='b', scores={'motion': 0}, created_at='2026-02-01')]),
                 self.run_record([self.review(2, rubric_version='visual-v2')]),
                 self.run_record([self.review(1, source={'rubric_version': 'visual-v2', 'private': 'never project'})]),
                 self.run_record([self.review(0, reviewer='AI Other 建议'), self.review(4, blind=None)]),
                 self.run_record([self.review(4)], conditions={}), self.run_record([])]
        expected = [make_gallery.score_summaries(run) for run in runs]
        self.assertEqual([audit.score_summaries(run) for run in runs], expected)
        self.assertEqual(audit.score_scopes(runs), make_gallery.score_scopes(runs))
        program = r'''
const fs=require('fs'); const code=fs.readFileSync(process.argv[1],'utf8');
const runs=JSON.parse(fs.readFileSync(0,'utf8'));
const scoreKeys=['compliance','recognizability','motion','visual_detail','interaction','completeness'];
const scoreCache=new WeakMap(), snapshot=true, source={runs}, state={};
const finite=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
const pure=code.slice(code.indexOf('  function scoreScope('),code.indexOf('  const finite = value =>'));
console.log(JSON.stringify(eval(pure+';runs.map(scoreSummaries)')));
'''
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT / 'web/app.js')],
                                input=json.dumps(runs), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_projected_scores_stay_on_own_run_without_old_score_borrowing(self):
        old = self.run_record([self.review(4)], model='M', tool='T', started_at='2026-01-01', evaluation={}, id='a' * 32)
        new = self.run_record([], model='M', tool='T', started_at='2026-02-01', evaluation={}, id='b' * 32)
        data = {'runs': [old, new], 'evidence': {}, 'thumbnails': {}, 'assets': {},
                'originals': {run['id']: {'status': 'no_entrypoint'} for run in (old, new)}}
        before = copy.deepcopy(data)
        projected = make_gallery.project_runs(data)
        self.assertEqual([row['id'] for row in projected], [new['id']])
        self.assertNotIn('scores', projected[0])
        self.assertEqual(projected[0]['meter'], [None, None, None, None, None])
        self.assertEqual(data, before)

    def test_meter_preserves_real_zero_and_source_without_inventing_unknown(self):
        self.assertEqual(make_gallery.meter({}), [None, None, None, None, None])
        self.assertEqual(make_gallery.meter({"duration_ms": 0, "metrics": {"cost_usd": 0, "cost_source": "cli_reported_unverified"}}),
                         [0, 0, "cli_reported_unverified", None, None])
        self.assertIn("CLI 未核验", make_gallery.meter_text([0, 0, "cli_reported_unverified", None, None]))
        self.assertIn("生成会话 0 秒", make_gallery.meter_text([0, 0, None, None, None]))
        for invalid in (True, "0", -2, float("inf")):
            self.assertEqual(make_gallery.meter({"duration_ms": invalid, "metrics": {"cost_usd": invalid}}), [None, None, None, None, None])


    def test_compact_metrics_round_only_display_with_javascript_parity(self):
        values = [3850664, 3.8506648000000006, 'cli_reported_unverified', 127854, 12001]
        original = copy.deepcopy(values)
        display = make_gallery.meter_text(values)
        self.assertIn('约 USD 3.85（approx）', display)
        self.assertIn('CLI 未核验，非账单', display)
        self.assertNotIn('3.8506648000000006', display)
        self.assertNotIn('cli_reported_unverified', display)
        self.assertNotIn('manifest.duration_ms', display)
        self.assertEqual(values, original)
        self.assertIn('费用 未上报', make_gallery.meter_text([None, None, None, None, None]))
        program = r"""
const fs=require('fs'); const code=fs.readFileSync(process.argv[1],'utf8');
const finite=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
const pure=code.slice(code.indexOf('  function meterText('),code.indexOf('  const allowedOriginal'));
console.log(JSON.stringify(eval(pure+';JSON.parse(fs.readFileSync(0,"utf8")).map(meter=>meterText({meter}))')));
"""
        cases = [values, [0, 0, 'cli_reported_unverified', 0, 0], [None, None, None, None, None],
                 [1, .00001, 'undeclared_internal', None, 999], [1000, .01, None, 4.5, 1000]]
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT / 'web/public-gallery.js')],
                                input=json.dumps(cases), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [make_gallery.meter_text(case) for case in cases])
        self.assertNotIn('undeclared_internal', json.loads(result.stdout)[3])
        self.assertIn('< USD 0.01', json.loads(result.stdout)[3])
        self.assertIn('生成会话 <1 秒', json.loads(result.stdout)[3])
        self.assertIn('评估 <1 秒', json.loads(result.stdout)[3])
        self.assertIn('总 Token 0', json.loads(result.stdout)[1])


class ScoreFirstPresentationTests(unittest.TestCase):
    """Synthetic unit records only; not new benchmark reviews or UI evidence."""
    review = ScoreContractTests.review
    run_record = ScoreContractTests.run_record

    def projected(self, reviews, **fields):
        run = self.run_record(reviews, model='Test model', tool='Test tool', started_at='2026-01-01', evaluation={}, **fields)
        data = {'runs': [run], 'tasks': [{'id': 'crocodile', 'name': 'Synthetic task'}],
                'format': 'static-media-v2', 'transport': 'external', 'evidence': {}, 'thumbnails': {}, 'assets': {},
                'originals': {run['id']: {'status': 'no_entrypoint'}}, 'offline': {'path': 'offline.html', 'size': 1, 'sha256': 'a' * 64}}
        return run, data, make_gallery.project_runs(data)[0]

    def test_six_unfolded_values_zero_and_missing_reasons_stay_distinct(self):
        import verify_public_gallery as audit
        run, data, row = self.projected([self.review(0, scores={'compliance': 0, 'motion': 2.5, 'interaction': 4})])
        source = make_gallery.scope_key(make_gallery.score_scope(run, run['reviews'][0]))
        html = make_gallery.render_card(row, source)
        parsed = audit.GalleryHTML(); parsed.feed(html)
        cells = parsed.score_cells[row['id']]
        self.assertEqual([cell['dimension'] for cell in cells], list(make_gallery.SCORE_LABELS))
        self.assertEqual([''.join(cell['value']) for cell in cells], ['0/4', '分项缺失', '2.5/4', '分项缺失', '4/4', '分项缺失'])
        self.assertTrue(all(not cell['folded'] for cell in cells))
        self.assertIn('非盲 AI 建议 · 待人工复核', html)
        self.assertEqual(parsed.structure[row['id']], ['work-heading', 'work-score', 'work-cover', 'work-actions', 'work-facts'])
        foreign = make_gallery.scope_key(['visual-v1', 'ai-suggestion', False, 'Other AI 建议'])
        self.assertEqual(make_gallery.render_card(row, foreign).count('本来源无记录'), 6)
        unreviewed = copy.deepcopy(row); unreviewed.pop('scores')
        self.assertEqual(make_gallery.render_card(unreviewed, source).count('未评审</span>'), 6)
        _, _, conflict = self.projected([self.review(4, rubric_version='conflicting-protocol')])
        self.assertEqual(make_gallery.render_card(conflict, conflict['scores'][0]['scope']).count('协议未核实</span>'), 6)

    def test_explicit_source_selection_not_reviewer_order_and_star_not_merged(self):
        run, data, row = self.projected([self.review(4, reviewer='AI A 建议'), self.review(0, reviewer='AI Z 建议')])
        scopes = make_gallery.score_scopes([run]); requested = make_gallery.scope_key(scopes[-1])
        seed = make_gallery.project_gallery(data, b'full', b'runtime', b'viewer', score_source=requested)
        self.assertEqual(seed['preferred_score_source'], requested)
        self.assertEqual(seed['defaults']['score_source'], requested)
        self.assertNotEqual(make_gallery.default_score_source(scopes), requested)
        for bad in ('["visual-v1"]', json.dumps(scopes[-1]), 1, '', '["visual-v1","ai-suggestion",false,"AI nonexistent 建议"]'):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                make_gallery.default_score_source(scopes, bad)
        html = make_gallery.render_card(row, '*')
        self.assertEqual(html.count('class="score-cell"'), 12)
        self.assertIn('口径 1', html); self.assertIn('口径 2', html)
        self.assertIn('4</strong><span>/4', html); self.assertIn('0</strong><span>/4', html)
        self.assertEqual(make_gallery.default_score_source(scopes, '*'), '*')
        # Legacy empty snapshots retain their empty default, not a future source.
        self.assertEqual(make_gallery.default_score_source([]), '')
        self.assertEqual(make_gallery.default_score_source([], ''), '')

    def test_five_slot_meter_uses_only_direct_valid_totals_and_evaluation_duration(self):
        import verify_public_snapshot as audit
        record = {'duration_ms': 1, 'started_at': '2026-01-01', 'finished_at': '2026-02-01',
                  'metrics': {'cost_usd': .00001, 'total_tokens': 0, 'input_total': 800, 'input_tokens': 300,
                              'output_tokens': 500, 'cache_read_tokens': 200}, 'evaluation': {'duration_ms': 0, 'sample_seconds': 15}}
        before = copy.deepcopy(record)
        self.assertEqual(make_gallery.meter(record), [1, .00001, None, 0, 0])
        self.assertEqual(audit.meter(record), make_gallery.meter(record))
        self.assertEqual(record, before)
        for bad in (None, True, False, '0', -1, float('nan'), float('inf')):
            record['metrics']['total_tokens'] = bad; record['evaluation']['duration_ms'] = bad
            self.assertEqual(make_gallery.meter(record)[3:], [None, None])
        del record['metrics']['total_tokens']; record['evaluation'].pop('duration_ms')
        self.assertEqual(make_gallery.meter(record)[3:], [None, None])

    def test_media_counts_deduplicate_registration_and_never_equate_inclusion_with_load(self):
        import verify_public_gallery as audit
        run, data, _ = self.projected([])
        run['evaluation']['evidence'] = ['evidence/desktop.png', 'evidence/mobile.png', 'evidence/mobile.png', 'output/index.html']
        data['evidence'][run['id'] + '/evidence/mobile.png'] = 'media/' + 'b' * 64 + '.jpg'
        row = make_gallery.project_runs(data)[0]
        self.assertEqual(row['media_counts'], [2, 1])
        self.assertTrue(row['registered_media']); self.assertIsNone(row['image'])
        self.assertEqual(row['media_gaps'], ['media_not_included', 'desktop_not_included'])
        self.assertEqual(audit.project_runs(data), [row])
        self.assertIn('媒体登记 2 · 公开包含 1（非加载结果）', make_gallery.media_text(row))
        self.assertIn('未交付作品（无入口）', make_gallery.media_text(row))
        self.assertNotIn('output/index.html', json.dumps(row))


class SnapshotHandoffRegressionTests(unittest.TestCase):
    """Trusted source unit fixture; never executes packaged/archived candidate scripts."""
    def test_public_handoff_carries_exact_scope_and_sort_before_mount_for_every_action(self):
        program = r'''
const fs=require('fs'),assert=require('assert'); const code=fs.readFileSync(process.argv[1],'utf8');
const start=code.indexOf('  async function fullAction('), end=code.indexOf('  function changeFilter(',start);
const a={id:'a'.repeat(32),task_id:'crocodile',purpose:'benchmark'},b={id:'b'.repeat(32),task_id:'crocodile',purpose:'benchmark'};
const records=new Map([[a.id,a],[b.id,b]]), original=JSON.stringify([a,b]);
const state={task:'crocodile',purpose:'benchmark',model:'',source:'["visual-v1","ai-suggestion",false,"AI Z 建议"]',sort:'motion',selected:new Set([a.id,b.id]),mixed:true};
let fullIntent=0,returnURL=null,mounted=0; const location={href:'https://example.test/report/?score_sort=compliance#gallery'},calls=[];
const history={pushState(_,__,url){location.href=String(url)},replaceState(_,__,url){location.href=String(url)}};
const $=()=>null,destroyPreview=()=>{},clearCards=()=>{},notice=()=>{};
const mountFull=async()=>{mounted++;const url=new URL(location.href);assert.strictEqual(url.searchParams.get('score_source'),state.source);assert.strictEqual(url.searchParams.get('score_sort'),state.sort)};
const window={BENCH_GALLERY_ACTION:async action=>calls.push(action)};
eval(code.slice(start,end));
(async()=>{
 for(const action of [{kind:'view',view:'scores'},{kind:'view',view:'matrix'},{kind:'detail',view:'evidence',run:a.id},{kind:'model',view:'matrix',model:'M'},{kind:'compare',view:'gallery'}]){
  await fullAction(action);const received=calls.at(-1);
  assert.strictEqual(received.score_source,state.source);assert.strictEqual(received.score_sort,'motion');
  assert.deepStrictEqual(received.selected,[a.id,b.id]);assert.strictEqual(received.mixed,true);
 }
 assert.strictEqual(calls.length,5);assert.strictEqual(mounted,5);
 state.source='*';state.sort='';await fullAction({kind:'view',view:'scores'});
 assert.strictEqual(calls.at(-1).score_source,'*');assert.strictEqual(calls.at(-1).score_sort,'');
 assert.strictEqual(JSON.stringify([a,b]),original);
 console.log('public exact scope/sort URL and action handoff passed before mount');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT / 'web/public-gallery.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('passed before mount', result.stdout)

    def test_bridge_uses_exact_runs_clears_model_blockers_and_preserves_scores_selection(self):
        program = r"""
const fs=require('fs'), assert=require('assert'); const code=fs.readFileSync(process.argv[1],'utf8');
const cut=(a,b)=>code.slice(code.indexOf(a),code.indexOf(b,code.indexOf(a)));
let snapshot=false; const window={}, source={runs:[
{id:'latest',tool:'cli',model:'M',task_id:'A',purpose:'benchmark',reviews:[{scores:{motion:4}}]},
{id:'old',tool:'cli',model:'M',task_id:'A',purpose:'smoke',reviews:[]},
{id:'other-task',tool:'cli',model:'M',task_id:'B',purpose:'smoke',reviews:[]},
{id:'other-tool',tool:'api',model:'M',task_id:'B',purpose:'experiment',reviews:[]},
{id:'different-model',tool:'cli',model:'N',task_id:'A',purpose:'benchmark',reviews:[]}]};
const state={filters:{},view:'evidence',selected:new Set(),selectionRuns:new Map()},
filterKeys=['q','tool','model','task_id','purpose','prompt_version','status','date_from','date_to'];
let filterTimer, calls=[]; const controls={'#mixed-comparison':{checked:false}};
const $=s=>controls[s], dateOf=r=>r.date||'', text=v=>v??'unknown', searchText=r=>JSON.stringify(r).toLowerCase();
const fillOptions=()=>calls.push(['options']), updateSelection=()=>calls.push(['selection']);
const setView=v=>{state.view=v; calls.push(['view',v]);};
const loadData=async()=>{state.runs=localFilter(source.runs);calls.push(['load',state.view,state.runs.map(r=>r.id)]);};
const openDetail=async id=>{const run=await getRun(id);assert.strictEqual(run,source.runs.find(r=>r.id===id));calls.push(['detail',id]);};
const openComparison=async()=>calls.push(['compare',[...state.selected],state.mixed,[...state.selectionRuns.keys()]]);
const el=(tag,attrs={},children=[])=>({tag,...attrs,children}), detailBlock=(title,children)=>({title,children});
const reviewStatus=r=>r.reviews.length?'reviewed':'unreviewed';
eval(cut('  function localFilter(', '  function updateAuth(')+';'+cut('  async function getRun(', '  function metadata('));
const bridge=cut('  if (snapshot) window.BENCH_GALLERY_ACTION', '  const params = new URLSearchParams(location.search);');
eval(bridge); assert.strictEqual(window.BENCH_GALLERY_ACTION,undefined); snapshot=true;eval(bridge);
(async()=>{
 state.filters={q:'blocking',tool:'no',model:'no',task_id:'A',purpose:'benchmark',prompt_version:'no',status:'failed',date_from:'2099',date_to:'1900'};
 await window.BENCH_GALLERY_ACTION({kind:'model',view:'matrix',model:'M'});
 assert.deepStrictEqual(state.runs.map(r=>r.id),['latest','old','other-task','other-tool']);
 assert(filterKeys.every(k=>state.filters[k]===(k==='model'?'M':'')));
 await window.BENCH_GALLERY_ACTION({kind:'detail',view:'evidence',run:'old'});
 assert.deepStrictEqual(calls.at(-1),['detail','old']);
 const history=snapshotHistory(source.runs[0]);
 const buttons=history.children[1].children.map(li=>li.children);
 assert.deepStrictEqual(buttons.map(b=>b['data-run-id']),['latest','old']);
 assert(buttons[1].text.endsWith('unreviewed')); assert(buttons[0].text.endsWith('reviewed'));
 buttons[1].onclick(); assert.deepStrictEqual(source.runs[1].reviews,[]);
 const filters={q:'M',task_id:'A',purpose:'benchmark',status:'completed'};
 state.filters={...filters};state.scoreSource='exact-scope';state.scoreSort='motion';calls=[];
 await window.BENCH_GALLERY_ACTION({kind:'view',view:'scores'});
 assert.deepStrictEqual(calls.slice(0,2).map(c=>c.slice(0,2)),[['view','scores'],['load','scores']]);
 assert.deepStrictEqual(state.filters,filters); assert.strictEqual(state.scoreSource,'exact-scope'); assert.strictEqual(state.scoreSort,'motion');
 for(const view of ['gallery','scores','evidence','matrix']){
  calls=[];
  await window.BENCH_GALLERY_ACTION({kind:'view',view,selected:['old','latest'],mixed:true});
  assert.deepStrictEqual([...state.selected],['old','latest']);
  assert.deepStrictEqual([...state.selectionRuns.keys()],['old','latest']);
  assert.strictEqual(state.mixed,true);
  assert.strictEqual(controls['#mixed-comparison'].checked,true);
  assert(!calls.some(call=>call[0]==='compare'));
  assert(calls.some(call=>call[0]==='load'&&call[1]===view));
 }
 await window.BENCH_GALLERY_ACTION({kind:'view',view:'scores',selected:[],mixed:false});
 assert.strictEqual(state.selected.size,0); assert.strictEqual(state.selectionRuns.size,0);
 assert.strictEqual(controls['#mixed-comparison'].checked,false);
 for(const mixed of [true,false]){
  await window.BENCH_GALLERY_ACTION({kind:'compare',view:'gallery',selected:['old','latest'],mixed});
  assert.deepStrictEqual(calls.at(-1),['compare',['old','latest'],mixed,['old','latest']]);
  assert.strictEqual(controls['#mixed-comparison'].checked,mixed);
 }
 await assert.rejects(window.BENCH_GALLERY_ACTION({kind:'view',selected:['missing']}),/快照中不存在/);
 await assert.rejects(window.BENCH_GALLERY_ACTION({kind:'view',selected:['old','old']}),/不同的记录/);
 await assert.rejects(window.BENCH_GALLERY_ACTION({kind:'detail',run:'missing'}),/快照中不存在/);
 await assert.rejects(window.BENCH_GALLERY_ACTION({kind:'compare',selected:['old','old']}),/两个不同/);
 console.log('snapshot bridge contracts passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT / 'web/app.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('snapshot bridge contracts passed', result.stdout)

class CompactScoreCodecTests(unittest.TestCase):
    """Versioned wire DATA only; all production/raw summary APIs remain unchanged."""
    def fixture(self):
        return {'score_encoding': 'scope-index-v1',
                'score_scopes': [['visual-v1', 'ai-suggestion', False, 'AI 建议评分']],
                'runs': [{'id': 'a' * 32, 'scores': [[0, 2, [0, None, 2.5, 4, None, 1]]]}]}

    def test_strict_python_independent_and_native_js_decoders_agree(self):
        import verify_public_gallery as audit
        good = self.fixture()
        expected = [{'id': 'a' * 32, 'scores': [{'scope': '["visual-v1","ai-suggestion",false,"AI 建议评分"]',
                    'index': 2, 'values': [0, None, 2.5, 4, None, 1]}]}]
        before = copy.deepcopy(good)
        self.assertEqual(make_gallery.decode_scores(good), expected)
        self.assertEqual(audit.decode_score_rows(good), expected)
        self.assertEqual(good, before)
        mutations = [lambda s: s.pop('score_encoding'), lambda s: s.update(score_encoding=True),
            lambda s: s.update(score_encoding='scope-index-v0'), lambda s: s.update(score_scopes=None),
            lambda s: s.update(runs=None), lambda s: s.update(runs=[None]),
            lambda s: s['score_scopes'].append(s['score_scopes'][0]),
            lambda s: s['score_scopes'][0].append('extra'),
            lambda s: s['score_scopes'][0].__setitem__(0, 'unknown'),
            lambda s: s['score_scopes'][0].__setitem__(1, 'authenticated-reviewer'),
            lambda s: s['score_scopes'][0].__setitem__(2, 0),
            lambda s: s['score_scopes'][0].__setitem__(2, True),
            lambda s: s['score_scopes'][0].__setitem__(3, '﻿'),
            lambda s: s['score_scopes'][0].__setitem__(3, 'AI\r建议评分'),
            lambda s: s['runs'][0].update(scores=None), lambda s: s['runs'][0].update(scores=[]),
            lambda s: s['runs'][0].update(scores=[{}]),
            lambda s: s['runs'][0]['scores'].append(s['runs'][0]['scores'][0]),
            lambda s: s['runs'][0]['scores'][0].append('hidden metadata'),
            lambda s: s['runs'][0]['scores'][0].__setitem__(0, True),
            lambda s: s['runs'][0]['scores'][0].__setitem__(0, -1),
            lambda s: s['runs'][0]['scores'][0].__setitem__(0, 1),
            lambda s: s['runs'][0]['scores'][0].__setitem__(0, 0.5),
            lambda s: s['runs'][0]['scores'][0].__setitem__(1, False),
            lambda s: s['runs'][0]['scores'][0].__setitem__(1, -1),
            lambda s: s['runs'][0]['scores'][0].__setitem__(1, 9007199254740992),
            lambda s: s['runs'][0]['scores'][0].__setitem__(2, None),
            lambda s: s['runs'][0]['scores'][0].__setitem__(2, [0, None]),
            lambda s: s['runs'][0]['scores'][0][2].__setitem__(0, False),
            lambda s: s['runs'][0]['scores'][0][2].__setitem__(0, '0'),
            lambda s: s['runs'][0]['scores'][0][2].__setitem__(0, -1),
            lambda s: s['runs'][0]['scores'][0][2].__setitem__(0, 4.01)]
        cases = [good]
        for index, mutate in enumerate(mutations):
            changed = copy.deepcopy(good); mutate(changed); cases.append(changed)
            with self.subTest(index=index):
                with self.assertRaises(RuntimeError): make_gallery.decode_scores(changed)
                with self.assertRaises(AssertionError): audit.decode_score_rows(changed)
        program = r'''
const fs=require('fs'),code=fs.readFileSync(process.argv[1],'utf8');
const start=code.indexOf('  function decodeScores('),end=code.indexOf('\n  seed.runs',start);
if(start<0||end<0)throw Error('trusted decoder fixture bounds');
eval(code.slice(start,end));
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify(cases.map(seed=>{try{return{valid:true,rows:decodeScores(seed)}}catch(error){return{valid:false}}})));
'''
        result = subprocess.run(['node', '-e', program, str(make_snapshot.ROOT/'web/public-gallery.js')],
                                input=json.dumps(cases), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        answers = json.loads(result.stdout)
        self.assertEqual(answers[0], {'valid': True, 'rows': expected})
        self.assertTrue(all(answer == {'valid': False} for answer in answers[1:]))
        for invalid in (float('inf'), float('nan')):
            changed = copy.deepcopy(good); changed['runs'][0]['scores'][0][2][0] = invalid
            with self.assertRaises(RuntimeError): make_gallery.decode_scores(changed)
            with self.assertRaises(AssertionError): audit.decode_score_rows(changed)

    def test_conflicting_protocol_stays_unknown_and_partial_is_not_backfilled(self):
        seed = self.fixture(); seed['score_scopes'][0][0] = None
        rows = make_gallery.decode_scores(seed)
        self.assertIsNone(json.loads(rows[0]['scores'][0]['scope'])[0])
        self.assertFalse(make_gallery.scope_rankable(json.loads(rows[0]['scores'][0]['scope'])))
        self.assertEqual(rows[0]['scores'][0]['values'], [0, None, 2.5, 4, None, 1])
        self.assertEqual(set(rows[0]['scores'][0]), {'scope', 'index', 'values'})

    def test_scope_blankness_provenance_and_utf16_order_match_exact_js(self):
        import verify_public_snapshot as audit
        labels = ['AI Z 建议评分 ', 'AI Z\r建议评分', 'AI Z\n建议评分',
                  'AI Z 建议评分', 'AI Z 建议评分', 'aı 内容建议',
                  'aİ 内容建议', 'AI\r ai 建议评分', 'AI 建议 ',
                  'AI Z\u0085建议评分', '﻿', '\t\v\f                　﻿\n\r  ',
                  '\u0085', '\u001c', '\u001d', '\u001e', '\u001f', '᠎', '​',
                  '﻿\u0085﻿', '﻿ AI Z 建议评分 ﻿', '', '\U00010000']
        tests = ScoreContractTests()
        run = tests.run_record([tests.review(reviewer=label) for label in labels])
        runs = [run] + [tests.run_record([tests.review(reviewer=label, **fields)])
                for label in labels for fields in ({}, {'blind': True}, {'blind': 'false'}, {'blind': 0},
                                                  {'source': {'rubric_version': 'visual-v2'}})]
        expected = [make_gallery.score_summaries(run) for run in runs]
        self.assertEqual([audit.score_summaries(run) for run in runs], expected)
        self.assertEqual(audit.score_scopes(runs), make_gallery.score_scopes(runs))
        program = r'''
const fs=require('fs'),code=fs.readFileSync(process.argv[1],'utf8'),runs=JSON.parse(fs.readFileSync(0,'utf8'));
const scoreKeys=['compliance','recognizability','motion','visual_detail','interaction','completeness'];
const scoreCache=new WeakMap(),snapshot=true,source={runs},state={};
const finite=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
const pure=code.slice(code.indexOf('  function scoreScope('),code.indexOf('  const finite = value =>'));
console.log(JSON.stringify(eval(pure+';({summaries:runs.map(scoreSummaries),scopes:snapshotScoreScopes()})')));
'''
        result = subprocess.run(['node','-e',program,str(make_snapshot.ROOT/'web/app.js')],
                                input=json.dumps(runs),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout)
        self.assertEqual(observed['summaries'],expected)
        self.assertEqual(observed['scopes'],make_gallery.score_scopes(runs))


if __name__ == "__main__":
    unittest.main()
