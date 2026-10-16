"""Repair unit/CLI tests use temporary synthetic archives, never approve real works."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_originals as original
import make_repairs as repairs


REAL_CATALOG = copy.deepcopy(repairs.CATALOG)
NEMOTRON = "111ded7301fc4cb68b2fe4fa30dd58f7"
CATJAM = "008c06189a844125af6b8e37c0b08a07"
TESTS = Path(__file__).resolve().parent


def source_descriptor(sha):
    return {"status": "unsupported", "entrypoint": "index.html", "entry_sha256": sha,
            "missing": [], "policy": "opaque-srcdoc-v1", "package": None}


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "runs"
        self.catalog = copy.deepcopy(REAL_CATALOG)
        patch = mock.patch.dict(repairs.CATALOG, self.catalog, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.sources = {
            NEMOTRON: ('<!doctype html><html><head><meta charset="utf-8"><title>合成</title></head><body>\r\n'
                       '<script>const eventHorizonRadius = 0;\r\n'
                       'const otherRadius = 0; const radius = 0;</script></body></html>\r\n').encode(),
        }
        self.first_html = (b'<!doctype html><html><head><meta charset="utf-8"></head>\r\n<body>\r\n'
                           + '<!-- 合成 -->\r\n'.encode() * 173 + b'</body>\r\n</html>\r\n')
        self.assertEqual(len(self.first_html.splitlines()), 177)
        self.sources[CATJAM] = self.first_html + b'```\r\nNot selected\r\n<!doctype html><html><head></head><body>SECOND</body></html>\r\n'
        self.results = {NEMOTRON: self.sources[NEMOTRON].replace(b"const eventHorizonRadius = 0;", b"let eventHorizonRadius = 0;"),
                        CATJAM: self.first_html}
        self.runs, self.originals = [], {}
        self.audit = {"version": 1, "repairs": {}}
        for run_id in (NEMOTRON, CATJAM):
            recipe = repairs.CATALOG[run_id]
            recipe["source_sha256"] = original.digest(self.sources[run_id])
            recipe["result_sha256"] = original.digest(self.results[run_id])
            raw = repairs._package(run_id, self.results[run_id])
            recipe.update(package_sha256=original.digest(raw), package_size=len(raw))
            run = {"id": run_id, "date": recipe["date"], "status": "failed", "archive_dir": "ignored/untrusted"}
            self.runs.append(run)
            self.originals[run_id] = source_descriptor(recipe["source_sha256"])
            self.audit["repairs"][run_id] = {
                **{key: recipe[key] for key in ("recipe_id", "source_sha256", "result_sha256", "package_sha256")},
                "reviewer": "assistant", "decision": "include", "reviewed_full": True,
                "reason": "Synthetic fixture fully reviewed, not a model work approval.",
            }
            self.register(run_id)

    def directory(self, run_id=NEMOTRON):
        return self.archive / repairs.CATALOG[run_id]["date"] / run_id

    def register(self, run_id):
        directory = self.directory(run_id)
        (directory / "output").mkdir(parents=True, exist_ok=True)
        raw = self.sources[run_id]
        (directory / "output/index.html").write_bytes(raw)
        item = {"kind": "output", "path": "output/index.html", "size": len(raw), "sha256": original.digest(raw)}
        manifest = {"id": run_id, "date": repairs.CATALOG[run_id]["date"],
                    "artifacts": [item], "generation_artifacts": [copy.deepcopy(item)]}
        (directory / "manifest.json").write_bytes(original.encode_json(manifest))
        (directory / "checksums.json").write_bytes(original.encode_json({item["path"]: item["sha256"]}))

    def build(self, audit=None):
        return repairs.build_repairs(self.runs, self.archive, self.audit if audit is None else audit)

    def assert_invalid(self, registry, payloads, originals=None):
        with self.assertRaises(RuntimeError):
            repairs.validate_repairs(registry, payloads, self.originals if originals is None else originals)

    def test_roundtrip_six_field_sdk_v1_package_and_determinism(self):
        registry, payloads = self.build()
        self.assertEqual(set(registry), {NEMOTRON, CATJAM})
        self.assertEqual(repairs.validate_repairs(registry, payloads, self.originals), set(payloads))
        self.assertEqual(self.build(), (registry, payloads))
        self.assertEqual(repairs.build_repairs(list(reversed(self.runs)), self.archive, self.audit), (registry, payloads))
        for run_id, record in registry.items():
            descriptor = record["descriptor"]
            self.assertEqual(set(descriptor), {"status", "entrypoint", "entry_sha256", "missing", "policy", "package"})
            info = descriptor["package"]
            self.assertEqual(info["path"], "originals/" + info["sha256"] + ".json")
            package = original.validate_original_package(payloads[info["path"]], run_id, descriptor)
            self.assertEqual(package["version"], 1)
            self.assertEqual(base64.b64decode(package["files"]["index.html"]["base64"]), self.results[run_id])
        result = self.results[NEMOTRON]
        self.assertIn(b"let eventHorizonRadius = 0;", result)
        self.assertIn(b"const otherRadius = 0; const radius = 0;", result)
        self.assertEqual(self.results[CATJAM], self.first_html)
        self.assertTrue(self.results[CATJAM].endswith(b"</html>\r\n"))
        # Callers may hold other media/original payloads; only verified repairs returned.
        self.assertEqual(repairs.validate_repairs(registry, {**payloads, "media/unrelated.bin": b"x"}, self.originals), set(payloads))

    def test_source_wrong_hash_even_with_consistent_archive_registrations(self):
        self.sources[NEMOTRON] += b"\n<!-- altered -->"
        self.register(NEMOTRON)
        with self.assertRaisesRegex(RuntimeError, "source hash"):
            self.build()

    def test_patch_count_zero_or_multiple(self):
        for raw in (b"none", b"const eventHorizonRadius = 0;" * 2):
            recipe = copy.deepcopy(repairs.CATALOG[NEMOTRON])
            recipe["source_sha256"] = original.digest(raw)
            with self.assertRaisesRegex(RuntimeError, "patch count"):
                repairs.apply_recipe(raw, recipe)
        recipe = copy.deepcopy(repairs.CATALOG[NEMOTRON])
        recipe["result_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "result hash"):
            repairs.apply_recipe(self.sources[NEMOTRON], recipe)

    def test_fixed_line_range_preserves_bytes_not_better_document(self):
        self.assertEqual(repairs.apply_recipe(self.sources[CATJAM], repairs.CATALOG[CATJAM]), self.first_html)
        with self.assertRaisesRegex(RuntimeError, "line range"):
            repairs._apply_operations(b"short\n", [{"op": "extract_lines", "first": 1, "last": 177}])
        self.assertEqual(repairs._apply_operations(b"a\x0bb\r\nc\nlast", [{"op": "extract_lines", "first": 1, "last": 2}]), b"a\x0bb\r\nc\n")

    def test_missing_withhold_and_pending_review_never_include(self):
        self.assertEqual(self.build({"version": 1, "repairs": {}}), ({}, {}))
        audit = copy.deepcopy(self.audit)
        for review in audit["repairs"].values():
            review.update(decision="withhold", reviewed_full=False)
        self.assertEqual(self.build(audit), ({}, {}))
        for missing in (None, {}, {"version": 1, "repairs": {}, "extra": 1}):
            with self.assertRaises(RuntimeError):
                repairs.build_repairs(self.runs, self.archive, missing)
        audit = copy.deepcopy(self.audit)
        audit["repairs"].pop(CATJAM)
        registry, payloads = self.build(audit)
        self.assertEqual(set(registry), {NEMOTRON})
        self.assertEqual(len(payloads), 1)

    def test_review_all_bindings_strict_schema_and_full_assistant_gate(self):
        for field, value in (("recipe_id", "unknown"), ("source_sha256", "0" * 64),
                             ("result_sha256", "0" * 64), ("package_sha256", "0" * 64),
                             ("reviewer", "human"), ("reviewed_full", False), ("reviewed_full", 1),
                             ("decision", "approve"), ("reason", " "), ("extra", True)):
            audit = copy.deepcopy(self.audit)
            audit["repairs"][NEMOTRON][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                self.build(audit)
        registry, payloads = self.build()
        for field in repairs.REVIEW_FIELDS:
            changed = copy.deepcopy(registry)
            del changed[NEMOTRON]["review"][field]
            self.assert_invalid(changed, payloads)
        for field, value in (("decision", "withhold"), ("reviewed_full", False), ("reviewer", "human"), ("package_sha256", "f" * 64)):
            changed = copy.deepcopy(registry)
            changed[NEMOTRON]["review"][field] = value
            self.assert_invalid(changed, payloads)

    def test_validate_without_archives_rejects_self_reported_catalog_metadata(self):
        registry, payloads = self.build()
        for field, value in (("version", True), ("version", 2), ("recipe_id", "new-recipe"),
                             ("source_sha256", "0" * 64), ("result_sha256", "0" * 64),
                             ("operations", [{"op": "replace_exact", "old": "x", "new": "y", "count": 1}]),
                             ("extra", 1)):
            changed = copy.deepcopy(registry)
            changed[NEMOTRON][field] = value
            self.assert_invalid(changed, payloads)
        changed = copy.deepcopy(registry)
        changed[NEMOTRON]["operations"][0]["count"] = True
        self.assert_invalid(changed, payloads)
        changed = copy.deepcopy(registry)
        changed["unknown-run"] = changed.pop(NEMOTRON)
        self.assert_invalid(changed, payloads)
        for originals in ({}, {**self.originals, NEMOTRON: source_descriptor("0" * 64)},
                          {**self.originals, NEMOTRON: {**self.originals[NEMOTRON], "entrypoint": "index.svg"}}):
            self.assert_invalid(registry, payloads, originals)
        for key in repairs.RECORD_FIELDS:
            changed = copy.deepcopy(registry)
            changed[NEMOTRON].pop(key)
            self.assert_invalid(changed, payloads)

    def test_payload_hash_size_mime_schema_run_binding_and_content_tampering(self):
        registry, payloads = self.build()
        path = registry[NEMOTRON]["descriptor"]["package"]["path"]
        self.assert_invalid(registry, {key: raw for key, raw in payloads.items() if key != path})
        for raw in (payloads[path] + b" ", b"garbage", "not bytes"):
            self.assert_invalid(registry, {**payloads, path: raw})
        package = original._site()._json(payloads[path])
        mutations = [lambda p: p.update(run_id=CATJAM), lambda p: p.update(version=True),
                     lambda p: p["files"]["index.html"].update(mime="text/javascript"),
                     lambda p: p["files"]["index.html"].update(size=True),
                     lambda p: p["files"]["index.html"].update(sha256="0" * 64),
                     lambda p: p["adaptation"].update(head_offset=0),
                     lambda p: p["files"].update({"../x.js": p["files"]["index.html"]})]
        for mutate in mutations:
            changed = copy.deepcopy(package)
            mutate(changed)
            self.assert_invalid(registry, {**payloads, path: original.encode_json(changed)})
        # Rehashing every self-reported package field cannot bypass catalog pins.
        changed = copy.deepcopy(package)
        changed["files"]["index.html"]["mime"] = "text/javascript"
        raw = original.encode_json(changed)
        sha = original.digest(raw)
        forged = copy.deepcopy(registry)
        forged[NEMOTRON]["descriptor"]["package"] = {"path": "originals/" + sha + ".json", "sha256": sha, "size": len(raw)}
        forged[NEMOTRON]["review"]["package_sha256"] = sha
        self.assert_invalid(forged, {**payloads, "originals/" + sha + ".json": raw})

    def test_descriptor_paths_mime_status_and_size(self):
        registry, payloads = self.build()
        for field, value in (("status", "not_reviewed"), ("policy", "relaxed"), ("entrypoint", "other.html"),
                             ("entry_sha256", "0" * 64), ("missing", ["x.js"]), ("extra", 1)):
            changed = copy.deepcopy(registry)
            changed[NEMOTRON]["descriptor"][field] = value
            self.assert_invalid(changed, payloads)
        for field, value in (("path", "originals/../x.json"), ("path", "/absolute.json"),
                             ("path", "originals/" + "0" * 64 + ".json"), ("size", True),
                             ("size", 1), ("sha256", "0" * 64), ("extra", 1)):
            changed = copy.deepcopy(registry)
            changed[NEMOTRON]["descriptor"]["package"][field] = value
            self.assert_invalid(changed, payloads)

    def test_archive_manifest_checksums_generation_duplicate_and_traversal(self):
        directory = self.directory()
        for mutate in (lambda m: m.update(id="other"), lambda m: m.update(date="2000-01-01"),
                       lambda m: m["artifacts"].append(copy.deepcopy(m["artifacts"][0])),
                       lambda m: m["artifacts"][0].update(path="output/../index.html"),
                       lambda m: m["artifacts"][0].update(kind="process"),
                       lambda m: m["generation_artifacts"][0].update(sha256="0" * 64),
                       lambda m: m["generation_artifacts"][0].update(kind="process"),
                       lambda m: m["generation_artifacts"].append(copy.deepcopy(m["generation_artifacts"][0])),
                       lambda m: m.pop("generation_artifacts")):
            self.register(NEMOTRON)
            manifest = json.loads((directory / "manifest.json").read_bytes())
            mutate(manifest)
            (directory / "manifest.json").write_bytes(original.encode_json(manifest))
            with self.assertRaises(RuntimeError):
                self.build()
        self.register(NEMOTRON)
        (directory / "checksums.json").write_bytes(b'{"output/index.html":"bad"}')
        with self.assertRaises(RuntimeError):
            self.build()
        (directory / "checksums.json").write_bytes(b'{"x":1,"x":2}')
        with self.assertRaisesRegex(RuntimeError, "duplicate JSON"):
            self.build()
        self.register(NEMOTRON)
        (directory / "output/index.html").write_bytes(b"tampered")
        with self.assertRaises(RuntimeError):
            self.build()

    def test_safe_locators_and_symlink_leaf_parent(self):
        for key, value in (("id", "../evil"), ("date", "../2026-10-10"), ("date", "2026-10-11")):
            runs = copy.deepcopy(self.runs)
            runs[0][key] = value
            with self.assertRaises(RuntimeError):
                repairs.build_repairs(runs, self.archive, self.audit)
        with self.assertRaisesRegex(RuntimeError, "duplicate run"):
            repairs.build_repairs(self.runs * 2, self.archive, self.audit)
        entry = self.directory() / "output/index.html"
        moved = self.root / "moved.html"
        entry.rename(moved)
        entry.symlink_to(moved)
        with self.assertRaises(RuntimeError):
            self.build()
        entry.unlink()
        moved.rename(entry)
        output = self.directory() / "output"
        moved = self.root / "moved-output"
        output.rename(moved)
        output.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.build()

    def test_reuse_original_source_scan_adaptation_and_package_validator(self):
        bad_sources = [b'<!doctype html><html><head></head><body><script>fetch("https://example.invalid")</script></body></html>',
                       b'<!doctype html><html><head><script src="missing.js"></script></head><body></body></html>',
                       b'<!doctype html><html><head></head><body>/home/private/file</body></html>']
        for raw in bad_sources:
            with self.assertRaises(RuntimeError):
                repairs._package(NEMOTRON, raw)
        with mock.patch.object(original, "validate_original_package", wraps=original.validate_original_package) as validate:
            self.build()
            self.assertEqual(validate.call_count, 4)

    def test_prepare_pending_separate_review_file_then_build_and_readback(self):
        destination = self.root / "pending"
        pending = repairs.prepare_repairs(self.runs, self.archive, destination)
        self.assertEqual(original._site()._json((destination / "audit.json").read_bytes()), pending)
        self.assertEqual(self.build(destination / "audit.json"), ({}, {}))
        for run_id in self.sources:
            self.assertEqual((destination / run_id / "index.html").read_bytes(), self.results[run_id])
            self.assertFalse(pending["repairs"][run_id]["reviewed_full"])
            self.assertEqual(pending["repairs"][run_id]["decision"], "withhold")
        approved = self.root / "synthetic-full-review.json"
        approved.write_bytes(original.encode_json(self.audit))
        registry, payloads = self.build(approved)
        self.assertEqual(repairs.validate_repairs(registry, payloads, self.originals), set(payloads))
        with self.assertRaises((RuntimeError, FileExistsError)):
            repairs.prepare_repairs(self.runs, self.archive, destination)
        # No archive source or original descriptor is rewritten.
        for run_id in self.sources:
            self.assertEqual((self.directory(run_id) / "output/index.html").read_bytes(), self.sources[run_id])
            self.assertEqual(self.originals[run_id]["entry_sha256"], original.digest(self.sources[run_id]))

    def test_cli_prepare_build_help_and_required_audit(self):
        runs = self.root / "runs.json"
        runs.write_bytes(original.encode_json({"format": "static-media-v2", "runs": self.runs}))
        pending = self.root / "cli-pending"
        # In-process CLI exercises synthetic catalog; subprocess --help imports in isolated mode.
        self.assertEqual(repairs.main(["prepare", "--runs", str(runs), "--archive-root", str(self.archive), "--destination", str(pending)]), 0)
        destination = self.root / "cli-build"
        self.assertEqual(repairs.main(["build", "--runs", str(runs), "--archive-root", str(self.archive), "--destination", str(destination), "--audit", str(pending / "audit.json")]), 0)
        self.assertEqual(json.loads((destination / "repairs.json").read_bytes()), {})
        audit = self.root / "review.json"
        audit.write_bytes(original.encode_json(self.audit))
        destination = self.root / "reviewed-build"
        self.assertEqual(repairs.main(["build", "--runs", str(runs), "--archive-root", str(self.archive), "--destination", str(destination), "--audit", str(audit)]), 0)
        registry = original._site()._json((destination / "repairs.json").read_bytes())
        payloads = {p.relative_to(destination).as_posix(): p.read_bytes() for p in (destination / "originals").glob("*.json")}
        self.assertEqual(repairs.validate_repairs(registry, payloads, self.originals), set(payloads))
        script = str(TESTS / "make_repairs.py")
        result = subprocess.run([sys.executable, "-I", script, "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, "-I", script, "build", "--runs", str(runs), "--archive-root", str(self.archive), "--destination", str(self.root / "no-audit")], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build requires --audit", result.stderr)
        result = subprocess.run([sys.executable, "-I", script, "prepare", "--runs", str(runs), "--archive-root", str(self.archive), "--destination", str(self.root / "not-created"), "--audit", str(audit)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "not-created").exists())

    def test_empty_delivery_unknown_runs_and_empty_registry(self):
        self.assertEqual(repairs.build_repairs([], self.root / "absent", {"version": 1, "repairs": {}}), ({}, {}))
        empty_runs = [{"id": "empty-delivery", "date": "2026-10-14", "status": "failed"}]
        # Unknown runs never cause archive access, invented HTML, or catalog recipes.
        self.assertEqual(repairs.build_repairs(empty_runs, self.root / "absent", {"version": 1, "repairs": {}}), ({}, {}))
        self.assertEqual(repairs.validate_repairs({}, {}, {}), set())
        audit = {"version": 1, "repairs": {"empty-delivery": copy.deepcopy(self.audit["repairs"][NEMOTRON])}}
        with self.assertRaisesRegex(RuntimeError, "unknown audit run"):
            repairs.build_repairs(empty_runs, self.root / "absent", audit)


class RealCatalogPendingTests(unittest.TestCase):
    def test_real_fixed_sources_prepare_only_no_approval_or_archive_writes(self):
        archive = TESTS.parent / "runs"
        if not all((archive / recipe["date"] / run_id / "output/index.html").is_file() for run_id, recipe in REAL_CATALOG.items()):
            self.skipTest("real read-only archives not present")
        runs = [{"id": run_id, "date": recipe["date"]} for run_id, recipe in REAL_CATALOG.items()]
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "pending"
            audit = repairs.prepare_repairs(runs, archive, destination)
            self.assertEqual(set(audit["repairs"]), set(REAL_CATALOG))
            self.assertEqual(repairs.build_repairs(runs, archive, destination / "audit.json"), ({}, {}))
            for run_id, recipe in REAL_CATALOG.items():
                raw = original._site().read_asset_safe(archive / recipe["date"] / run_id, "output/index.html")
                self.assertEqual(original.digest(raw), recipe["source_sha256"])
                result = (destination / run_id / "index.html").read_bytes()
                self.assertEqual(original.digest(result), recipe["result_sha256"])
                self.assertEqual(original.digest(repairs._package(run_id, result)), recipe["package_sha256"])
                self.assertEqual(audit["repairs"][run_id]["decision"], "withhold")
                self.assertFalse(audit["repairs"][run_id]["reviewed_full"])
                if run_id == CATJAM:
                    self.assertEqual(result, b"".join(raw.splitlines(keepends=True)[:177]))
                else:
                    self.assertEqual(raw.count(b"const eventHorizonRadius = 0;"), 1)
                    self.assertEqual(result, raw.replace(b"const eventHorizonRadius = 0;", b"let eventHorizonRadius = 0;"))


if __name__ == "__main__":
    unittest.main()
