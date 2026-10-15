"""Synthetic source extraction tests; never execute sources or touch real archives."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_originals as originals


# Only a temporary synthetic package fixture, never a registered model work.
SVG_SOURCE = ('﻿<?xml version="1.0" encoding="UTF-8"?>\r\n'
              '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 160 80">\r\n'
              '<title>合成 SVG</title><!-- inert &lt;script&gt; example -->\r\n'
              '<defs><linearGradient id="paint"><stop stop-color="red"/><stop offset="1" stop-color="gold"/></linearGradient>'
              '<circle id="shape" r="5"/></defs><style>@keyframes fade{from{opacity:.5}to{opacity:1}}</style>\r\n'
              '<rect width="160" height="80" fill="#123456"/><use xlink:href="#shape" x="80" y="40" fill="url(#paint)"/>'
              '<circle cx="20" cy="40" r="10" fill="url(\'\x23paint\')"><animate attributeName="cx" values="20;140;20" dur="1s" repeatCount="indefinite"/></circle>\r\n'
              '</svg>\r\n').encode("utf-8")


class OriginalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = [{"id": "failed-run", "date": "2026-10-08", "status": "failed"}]
        self.directory = self.root / "2026-10-08/failed-run"
        self.directory.mkdir(parents=True)
        self.audit = {"version": 1, "reviewer": "assistant", "files": {}}
        self.files = {"index.html": '<!doctype html><html><head><meta charset="utf-8"><title>中文 🐊</title>\n'
                      '<!-- <script src="fake.js"></script> -->\n<link rel="stylesheet" href="css/main.css">'
                      '<script>const text = \'<script src="fake.js">\';</script>'
                      '<script src="js/one.js"></script><script defer src="./js/two.js"></script>'
                      '<script src="js/three.js"></script><script src="js/four.js"></script>'
                      '</head><body><svg><use href="#shape"/></svg>'
                      '<img src="data:image/png;base64,YQ=="></body></html>',
                      "css/main.css": "body { color: red; }", "js/one.js": "window.order = [1];",
                      "js/two.js": "window.order.push(2);", "js/three.js": "window.order.push(3);",
                      "js/four.js": "window.order.push(4);"}
        self.register()

    def register(self, generation=False):
        artifacts, checksums = [], {}
        self.audit["files"] = {}
        for path, text in self.files.items():
            raw = text.encode("utf-8") if isinstance(text, str) else text
            target = self.directory / "output" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            artifacts.append({"path": "output/" + path, "sha256": sha, "size": len(raw), "kind": "output"})
            checksums["output/" + path] = sha
            self.audit["files"]["failed-run/" + path] = {"sha256": sha, "decision": "include", "reviewed_full": True, "reason": "synthetic fixture fully reviewed"}
        self.manifest = {**self.runs[0], "artifacts": artifacts}
        if generation:
            self.manifest["generation_artifacts"] = copy.deepcopy(artifacts)
        self.write_manifest()
        (self.directory / "checksums.json").write_text(json.dumps(checksums))

    def write_manifest(self):
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest))

    def build(self):
        return originals.build_originals(self.runs, self.root, self.audit)

    def package(self):
        descriptors, payloads = self.build()
        desc = descriptors["failed-run"]
        return desc, originals.validate_original_package(payloads[desc["package"]["path"]], "failed-run", desc)

    def test_failed_entry_four_scripts_defer_nested_utf8_offsets(self):
        desc, package = self.package()
        self.assertEqual(desc["status"], "ready")
        self.assertEqual(len(package["files"]), 6)
        entry = self.files["index.html"].encode("utf-8")
        adapter = package["adaptation"]
        self.assertEqual(entry[:adapter["head_offset"]], b"<!doctype html><html><head>")
        self.assertEqual([patch["path"] for patch in adapter["patches"]], ["css/main.css", "js/one.js", "js/two.js", "js/three.js", "js/four.js"])
        for patch in adapter["patches"]:
            self.assertEqual(entry[patch["start"]:patch["end"]].decode(), patch["original"])
        self.assertEqual(adapter["patches"][2]["original"], "./js/two.js")
        for path, item in package["files"].items():
            self.assertEqual(base64.b64decode(item["base64"]), self.files[path].encode())
        self.assertNotIn("fake.js", json.dumps(adapter))
        self.assertEqual(self.build(), self.build())

    def test_missing_dependencies_not_fabricated(self):
        del self.files["js/two.js"]
        self.register()
        desc, package = self.package()
        self.assertEqual(desc["status"], "missing_dependencies")
        self.assertEqual(desc["missing"], ["js/two.js"])
        self.assertEqual(package["missing"], ["js/two.js"])
        self.assertNotIn("js/two.js", package["files"])
        self.assertNotIn("js/two.js", [patch["path"] for patch in package["adaptation"]["patches"]])

    def test_no_entry_and_interrupted_entry(self):
        self.runs[0]["status"] = "interrupted"
        self.assertEqual(self.package()[0]["status"], "ready")
        self.files = {"note.txt": "diagnostic excluded"}
        self.register()
        descriptors, payloads = self.build()
        self.assertEqual(descriptors["failed-run"]["status"], "no_entrypoint")
        self.assertIsNone(descriptors["failed-run"]["entry_sha256"])
        self.assertEqual(payloads, {})

    def test_archive_evidence_at_sign_is_not_public_output_permission(self):
        self.manifest["artifacts"].append({"path": "evidence/video/page@capture.webm", "kind": "evidence", "size": 1, "sha256": "0" * 64})
        self.write_manifest()
        self.assertEqual(self.package()[0]["status"], "ready")
        self.manifest["artifacts"].append({"path": "output/file@alias.js", "kind": "output", "size": 1, "sha256": "0" * 64})
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "noncanonical"):
            self.build()

    def test_unreferenced_outputs_not_in_package(self):
        self.files.update({"unused.js": "window.unused=1", "diagnostic.txt": "not public", "image.png": b"not used"})
        self.register()
        self.assertEqual(len(self.package()[1]["files"]), 6)

    def test_review_missing_stale_withhold_and_no_human_claim(self):
        original = copy.deepcopy(self.audit)
        for mutation, status in ((lambda a: a["files"].pop("failed-run/js/two.js"), "not_reviewed"),
                                 (lambda a: a["files"]["failed-run/index.html"].update(sha256="0" * 64), "not_reviewed"),
                                 (lambda a: a["files"]["failed-run/js/one.js"].update(decision="withhold"), "withheld")):
            self.audit = copy.deepcopy(original)
            mutation(self.audit)
            desc, payloads = self.build()
            self.assertEqual(desc["failed-run"]["status"], status)
            self.assertEqual(payloads, {})
        self.audit = copy.deepcopy(original)
        self.audit["reviewer"] = "human-approved"
        with self.assertRaises(RuntimeError):
            self.build()
        self.audit = copy.deepcopy(original)
        self.audit["files"]["failed-run/index.html"]["reviewed_full"] = False
        with self.assertRaises(RuntimeError):
            self.build()

    def test_integrity_manifest_checksums_generation(self):
        self.register(generation=True)
        self.package()
        self.manifest["generation_artifacts"][0]["sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "generation artifact"):
            self.build()
        self.register()
        (self.directory / "checksums.json").write_text(json.dumps({"output/index.html": "0" * 64}))
        with self.assertRaisesRegex(RuntimeError, "hash/size"):
            self.build()
        self.register()
        (self.directory / "output/index.html").write_text("tamper")
        with self.assertRaises(RuntimeError):
            self.build()
        self.register()
        self.manifest["artifacts"].append(self.manifest["artifacts"][0])
        self.write_manifest()
        with self.assertRaises(RuntimeError):
            self.build()

    def test_duplicate_json_and_symlink_parent_leaf(self):
        (self.directory / "checksums.json").write_text('{"x":1,"x":2}')
        with self.assertRaisesRegex(RuntimeError, "duplicate JSON"):
            self.build()
        self.register()
        entry = self.directory / "output/index.html"
        other = self.root / "entry.html"
        entry.rename(other)
        entry.symlink_to(other)
        with self.assertRaises(RuntimeError):
            self.build()
        entry.unlink()
        other.rename(entry)
        target = self.root / "moved"
        (self.directory / "output").rename(target)
        (self.directory / "output").symlink_to(target, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.build()

    def test_private_decoded_source_not_hidden_by_base64(self):
        # Construct intentionally rejected source at runtime; the public test
        # source itself must pass the same no-literal-credentials export gate.
        credential = 'const api_' + 'key="' + 'synthetic-sensitive-value' + '";'
        query = 'const x="https://example.invalid/' + '?to' + 'ken=' + 'synthetic-value' + '";'
        for private in (credential, 'const x="/home/private/file";', query):
            self.files["js/one.js"] = private
            self.register()
            with self.assertRaises(RuntimeError):
                self.build()

    def test_unsupported_modules_remote_dynamic_css_and_head(self):
        original = self.files["index.html"]
        for entry in (original.replace('src="js/one.js"', 'type="module" src="js/one.js"'),
                      original.replace('src="js/one.js"', 'src="https://example.invalid/a.js"'),
                      original.replace("<head>", ""), original.replace("<head>", '<script>bad()</script><head>'),
                      original.replace("<head>", '<head><base href="/">'),
                      original.replace("<head>", '<head><template><head></template>')):
            self.files["index.html"] = entry
            self.register()
            desc, payloads = self.build()
            self.assertEqual(desc["failed-run"]["status"], "unsupported")
            self.assertEqual(payloads, {})
        self.files["index.html"] = original
        self.files["css/main.css"] = 'body{background:url(missing.png)}'
        self.register()
        self.assertEqual(self.build()[0]["failed-run"]["status"], "unsupported")

    def test_package_tampering_strict_schema_run_offsets_and_extra_file(self):
        desc, package = self.package()
        mutations = [lambda p: p.update(version=2), lambda p: p.update(extra=1), lambda p: p.update(run_id="other"),
                     lambda p: p["adaptation"].update(head_offset=0),
                     lambda p: p["adaptation"]["patches"][0].update(start=1),
                     lambda p: p["files"]["index.html"].update(mime="image/svg+xml"),
                     lambda p: p["files"]["index.html"].update(size=True),
                     lambda p: p["files"]["index.html"].update(sha256="0" * 64),
                     lambda p: p["files"].update({"extra.js": p["files"]["js/one.js"]}),
                     lambda p: p["files"].update({"../bad.js": p["files"].pop("js/one.js")}),
                     lambda p: p.update(missing=["js/nonexistent.js"])]
        for mutation in mutations:
            value = copy.deepcopy(package)
            mutation(value)
            with self.assertRaises(RuntimeError):
                originals.validate_original_package(originals.encode_json(value), "failed-run")
        raw = originals.encode_json(package).replace(b'"version":1', b'"version":1,"version":1', 1)
        with self.assertRaises(RuntimeError):
            originals.validate_original_package(raw)

    def test_descriptor_inline_external_equivalence_and_rejections(self):
        descriptors, payloads = self.build()
        external = descriptors["failed-run"]
        originals.validate_original_descriptor(external, "external")
        info = external["package"]
        inline = copy.deepcopy(external)
        inline["package"] = {"sha256": info["sha256"], "size": info["size"], "base64": base64.b64encode(payloads[info["path"]]).decode()}
        originals.validate_originals({"format": originals.FORMAT_V2, "transport": "inline", "runs": self.runs, "originals": {"failed-run": inline}})
        for mutation in (lambda d: d.update(status="invented"), lambda d: d.update(policy="allow-same-origin"),
                         lambda d: d.update(extra=1), lambda d: d.update(status="withheld"),
                         lambda d: d["package"].update(path="media/a.json"), lambda d: d["package"].update(size=True)):
            bad = copy.deepcopy(external)
            mutation(bad)
            with self.assertRaises(RuntimeError):
                originals.validate_original_descriptor(bad, "external")

    def test_rawtext_entities_comments_and_unquoted_attribute_positions(self):
        self.files["index.html"] = ('<!doctype html><html><head><title>&lt;tag&gt; 🐊</title>'
            '<!-- <script src="fake.js"></script> -->'
            '<script src="js&#47;one.js"></script><script src=js/two.js defer></script>'
            '</head><body><textarea><script src="fake.js"></script></textarea>'
            '<noscript><script src="fake.js"></script></noscript><a href="./index.html">Restart</a></body></html>')
        self.register()
        _, package = self.package()
        patches = package["adaptation"]["patches"]
        self.assertEqual([p["path"] for p in patches], ["js/one.js", "js/two.js"])
        self.assertEqual(patches[0]["original"], "js&#47;one.js")
        raw = self.files["index.html"].encode()
        for patch in patches:
            self.assertEqual(raw[patch["start"]:patch["end"]].decode(), patch["original"])

    def test_script_escaped_state_divergence_is_unsupported(self):
        # A browser leaves the script-data escaped state at "-->", so a balanced
        # comment block cannot change how the document is built. Only a "<script"
        # or "</script" reached while the state is still open makes a browser and
        # a plain CDATA parser disagree, and those stay unsupported.
        divergent = ['<!--\nconst a=\'<script>\';const b=\'</script><script src="one.js">\';\nwindow.inlineText=b;\n//-->',
                     '<!-- note without a close </script>']
        for script in divergent:
            self.files = {"index.html": '<!doctype html><html><head><script>' + script +
                          '</script><script src="one.js"></script></head><body></body></html>',
                          "one.js": "window.example=1;"}
            self.register()
            with self.subTest(script=script):
                descriptors, payloads = self.build()
                self.assertEqual(descriptors["failed-run"]["status"], "unsupported")
                self.assertEqual(payloads, {})

    def test_balanced_script_comment_block_is_supported(self):
        # "<!--" ... "//-->" is an ordinary JavaScript comment idiom: the escape
        # state opens and closes, so the page runs instead of being withheld.
        script = '<!--\nwindow.example=1;\n//-->'
        self.files = {"index.html": '<!doctype html><html><head><script>' + script +
                      '</script><script src="one.js"></script></head><body></body></html>',
                      "one.js": "window.other=2;"}
        self.register()
        descriptors, payloads = self.build()
        self.assertEqual(descriptors["failed-run"]["status"], "ready")
        self.assertTrue(payloads)

    def test_bom_and_entity_refs_preserve_raw_bytes_and_offsets(self):
        for source_ref in ("js&#47;one&#46;js", "js&#x2f;one&#x2e;js", "js&sol;one&period;js"):
            self.files = {"index.html": '﻿<!doctype html><html><head><title>中文 🐊</title><script src="' + source_ref + '"></script></head><body></body></html>',
                          "js/one.js": "window.example=1;"}
            self.register()
            _, package = self.package()
            entry = self.files["index.html"].encode()
            self.assertEqual(base64.b64decode(package["files"]["index.html"]["base64"]), entry)
            self.assertEqual(entry[:package["adaptation"]["head_offset"]], b"\xef\xbb\xbf<!doctype html><html><head>")
            patch, = package["adaptation"]["patches"]
            self.assertEqual(patch["path"], "js/one.js")
            self.assertEqual(entry[patch["start"]:patch["end"]].decode(), source_ref)

    def test_multiple_or_noninitial_bom_is_unsupported(self):
        for prefix in ("﻿﻿", " ﻿", "<!doctype html>﻿"):
            self.files = {"index.html": prefix + '<html><head></head><body></body></html>'}
            self.register()
            descriptors, payloads = self.build()
            self.assertEqual(descriptors["failed-run"]["status"], "unsupported")
            self.assertEqual(payloads, {})

    def test_entity_refs_are_decoded_once_not_twice(self):
        self.files = {"index.html": '<!doctype html><html><head><script src="one&amp;#46;js"></script></head><body></body></html>',
                      "one.js": "window.example=1;"}
        self.register()
        descriptors, payloads = self.build()
        self.assertEqual(descriptors["failed-run"]["status"], "unsupported")
        self.assertEqual(payloads, {})

    def test_dynamic_resource_and_module_dependencies_marked_unsupported(self):
        for code in ('fetch("file.json")', 'new Worker("worker.js")', 'import("module.js")',
                     'import x from "module.js"', 'new XMLHttpRequest()'):
            self.files["js/one.js"] = code
            self.register()
            descriptors, payloads = self.build()
            self.assertEqual(descriptors["failed-run"]["status"], "unsupported")
            self.assertEqual(payloads, {})

    def test_legacy_html_package_bytes_unchanged(self):
        descriptor, _ = self.package()
        self.assertEqual(descriptor["package"]["sha256"], "3a8a16b6a61491bbe4047d46b87c664ab53e1cd1e40e4a5abe83ab84adbd9c75")
        self.assertEqual(descriptor["package"]["size"], 2141)

    def test_standalone_svg_exact_bytes_audit_and_both_transports(self):
        for entrypoint in ("index.svg", "scene.SVG"):
            with self.subTest(entrypoint=entrypoint):
                self.files = {entrypoint: SVG_SOURCE}
                self.register(generation=True)
                descriptor, package = self.package()
                self.assertEqual(descriptor["status"], "ready")
                self.assertEqual(descriptor["entrypoint"], entrypoint)
                self.assertEqual(descriptor["policy"], "opaque-srcdoc-v1")
                self.assertEqual(descriptor["missing"], [])
                self.assertEqual(package["adaptation"], {"version": 1, "protocol": "standalone-svg-v1"})
                self.assertEqual(set(package["files"]), {entrypoint})
                file = package["files"][entrypoint]
                self.assertEqual(file["mime"], "image/svg+xml")
                self.assertEqual(file["size"], len(SVG_SOURCE))
                self.assertEqual(file["sha256"], hashlib.sha256(SVG_SOURCE).hexdigest())
                self.assertEqual(base64.b64decode(file["base64"]), SVG_SOURCE)
                self.assertEqual(self.build(), self.build())
                descriptors, payloads = self.build()
                originals.validate_originals({"format": originals.FORMAT_V2, "transport": "external",
                                              "runs": self.runs, "originals": descriptors}, payloads)
                inline = copy.deepcopy(descriptor)
                info = inline["package"]
                raw = payloads[info.pop("path")]
                info["base64"] = base64.b64encode(raw).decode()
                self.assertEqual(originals.validate_originals({"format": originals.FORMAT_V2, "transport": "inline",
                                                              "runs": self.runs, "originals": {"failed-run": inline}}),
                                 {"failed-run": package})
                self.audit["files"] = {}
                self.assertEqual(self.build(), ({"failed-run": {**descriptor, "status": "not_reviewed", "package": None}}, {}))
                self.register(generation=True)
                self.audit["files"]["failed-run/" + entrypoint]["sha256"] = "0" * 64
                self.assertEqual(self.build()[0]["failed-run"]["status"], "not_reviewed")
                self.register(generation=True)
                self.audit["files"]["failed-run/" + entrypoint]["decision"] = "withhold"
                self.assertEqual(self.build()[0]["failed-run"]["status"], "withheld")

    def test_svg_entry_selection_index_preferred_sole_top_level_or_none(self):
        for paths, expected in ((["one.svg"], "one.svg"), (["one.SVG"], "one.SVG"),
                                (["one.svg", "index.svg", "two.svg"], "index.svg"),
                                (["one.svg", "two.svg"], None), (["nested/scene.svg"], None)):
            with self.subTest(paths=paths):
                self.files = {path: SVG_SOURCE for path in paths}
                self.register()
                descriptors, payloads = self.build()
                self.assertEqual(descriptors["failed-run"]["entrypoint"], expected)
                self.assertEqual(descriptors["failed-run"]["status"], "ready" if expected else "no_entrypoint")
                self.assertEqual(bool(payloads), expected is not None)
        self.files["index.html"] = '<!doctype html><html><head></head><body>legacy wins</body></html>'
        self.register()
        self.assertEqual(self.package()[0]["entrypoint"], "index.html")
        self.manifest["artifacts"].append({"path": "output/../escape.svg", "kind": "output"})
        self.write_manifest()
        with self.assertRaises(RuntimeError):
            self.build()

    def test_standalone_svg_rejects_xml_active_and_external_payloads(self):
        opening = '<svg xmlns="http://www.w3.org/2000/svg">'
        cases = [opening + '<g></svg>', '<svg/>', '<html/>', '<svg xmlns="https://example.invalid/svg"/>',
                 opening + '<script>alert(1)</script></svg>', opening + '<g onload="alert(1)"/></svg>',
                 opening + '<g ONCLICK="alert(1)"/></svg>',
                 opening + '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject></svg>',
                 opening + '<use href="https://example.invalid/a.svg#x"/></svg>',
                 opening + '<use href="other.svg#x"/></svg>', opening + '<use href="javascript:alert(1)"/></svg>',
                 opening + '<use href="data:image/svg+xml;base64,PHN2Zy8+"/></svg>',
                 opening + '<feImage href="https://example.invalid/a.png"/></svg>',
                 opening + '<path fill="url(https://example.invalid/a.svg#x)"/></svg>',
                 opening + '<path fill="url(/*not-a-comment*/#paint)"/></svg>',
                 opening + '<style>g{fill:url(/**/#paint)}</style></svg>',
                 opening + '<style>@import "https://example.invalid/a.css";</style></svg>',
                 opening + '<style>g{fill:url(https://example.invalid/a.svg#x)}</style></svg>',
                 opening + '<style>g{fill:u\\72l(https://example.invalid/a.svg#x)}</style></svg>',
                 opening + '<style>g{background:image-set("https://example.invalid/a.png" 1x)}</style></svg>',
                 opening + '<g style="fill:url(https://example.invalid/a.svg#x)"/></svg>',
                 opening + '<animate attributeName="onload" to="alert(1)"/></svg>',
                 opening + '<set attributeName="href" to="https://example.invalid/x"/></svg>',
                 opening + '<set attributeName="style" to="fill:red"/></svg>',
                 opening + '<g xml:base="https://example.invalid/"/></svg>',
                 opening + '<style><g/></style></svg>',
                 '<!DOCTYPE svg>' + opening + '</svg>',
                 '<!DOCTYPE svg SYSTEM "https://example.invalid/svg.dtd">' + opening + '</svg>',
                 '<!DOCTYPE svg [<!ENTITY x "expanded">]>' + opening + '<text>&x;</text></svg>',
                 '<?xml-stylesheet href="https://example.invalid/a.css"?>' + opening + '</svg>',
                 '<?xml version="1.0" encoding="iso-8859-1"?>' + opening + '</svg>',
                 '<?xml version="1.1"?>' + opening + '</svg>']
        for entry in cases:
            with self.subTest(entry=entry):
                self.files = {"index.svg": entry}
                self.register()
                descriptors, payloads = self.build()
                self.assertEqual(descriptors["failed-run"]["status"], "unsupported")
                self.assertEqual(payloads, {})

    def test_standalone_svg_package_schema_binding_and_tamper_rejections(self):
        self.files = {"index.svg": SVG_SOURCE}
        self.register(generation=True)
        descriptor, package = self.package()
        for mutation in (lambda p: p.update(entrypoint="nested/index.svg"),
                         lambda p: p.update(entrypoint="../index.svg"),
                         lambda p: p.update(entrypoint="index.html"),
                         lambda p: p["adaptation"].update(protocol="html"),
                         lambda p: p["adaptation"].update(version=True),
                         lambda p: p["adaptation"].update(head_offset=1),
                         lambda p: p.update(missing=["one.css"]),
                         lambda p: p["files"].update({"second.svg": p["files"]["index.svg"]}),
                         lambda p: p["files"]["index.svg"].update(mime="text/html"),
                         lambda p: p["files"]["index.svg"].update(sha256="0" * 64)):
            bad = copy.deepcopy(package)
            mutation(bad)
            with self.assertRaises(RuntimeError):
                originals.validate_original_package(originals.encode_json(bad))
        bad = copy.deepcopy(package)
        raw = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'
        bad["files"]["index.svg"].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), base64=base64.b64encode(raw).decode())
        with self.assertRaises(RuntimeError):
            originals.validate_original_package(originals.encode_json(bad))
        self.manifest["generation_artifacts"][0]["sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "generation artifact"):
            self.build()
        for mutation in (lambda d: d.update(entrypoint="nested/index.svg"), lambda d: d.update(entrypoint="../index.svg"),
                         lambda d: d.update(status="missing_dependencies", missing=["one.css"])):
            bad = copy.deepcopy(descriptor)
            mutation(bad)
            with self.assertRaises(RuntimeError):
                originals.validate_original_descriptor(bad, "external")

    def test_limits_and_boolean_adaptation_are_not_accepted(self):
        with self.assertRaises(RuntimeError):
            originals.validate_original_package(b" " * (originals.MAX_PACKAGE_BYTES + 1))
        _, package = self.package()
        package["adaptation"]["version"] = True
        with self.assertRaises(RuntimeError):
            originals.validate_original_package(originals.encode_json(package))
        self.files = {"index.html": '<html><head>' + ''.join('<script src="f%d.js"></script>' % i for i in range(32)) + '</head><body></body></html>',
                      **{"f%d.js" % i: "/* empty synthetic file */" for i in range(32)}}
        self.register()
        self.assertEqual(self.build()[0]["failed-run"]["status"], "unsupported")

    def test_safe_reader_detects_changes_during_read(self):
        import make_site
        original_fstat = make_site.os.fstat
        calls = 0
        def changing_fstat(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                current = original_fstat(fd)
                make_site.os.utime(self.directory / "output/index.html", ns=(current.st_atime_ns, current.st_mtime_ns + 1000000))
            return original_fstat(fd)
        with mock.patch.object(make_site.os, "fstat", side_effect=changing_fstat):
            with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                make_site.read_asset_safe(self.directory, "output/index.html")

    def test_raw_size_encoding_and_path_limits(self):
        self.files["index.html"] = b"\xff"
        self.register()
        with self.assertRaises(UnicodeError):
            self.build()
        self.files["index.html"] = "x" * (originals.MAX_ORIGINAL_BYTES + 1)
        self.register()
        with self.assertRaises(RuntimeError):
            self.build()
        self.runs[0]["id"] = "../escape"
        with self.assertRaises(RuntimeError):
            self.build()


if __name__ == "__main__":
    unittest.main()
