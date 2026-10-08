"""Public source boundary tests, using isolated allowlisted fixture roots."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_export


class SourceExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "project"
        self.destination = self.base / "site" / "source"
        self.root.mkdir()
        for relative in source_export.REQUIRED_FILES:
            self.put(relative, "# public fixture\n")
        self.put("config/bench.example.toml", '# Copy and customize locally.\n[server]\nhost = "127.0.0.1"\nport = 8765\n')
        self.put("bench/__init__.py", '"""Fixture package."""\n')
        self.put("bench/cli.py", 'print("public fixture")\n')
        self.put("bench/adapters/metrics.py", 'FIELDS = ["input_tokens", "api_key"]\n')
        self.put("tests/test_small.py", 'TOKEN = "unit-test-secret"\n')
        self.put("tests/publish.py", '"""Optional helper."""\n')

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def export(self):
        return source_export.export_source(self.root, self.destination)

    def tree(self, root):
        if not root.exists():
            return {}
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def assert_refused_without_writes(self):
        before = self.tree(self.destination)
        existed = self.destination.exists()
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertEqual(self.tree(self.destination), before)
        self.assertEqual(self.destination.exists(), existed)

    def test_deterministic_bytes_sorted_paths_and_hash_manifest(self):
        result = self.export()
        self.assertEqual(result["paths"], sorted(result["paths"]))
        self.assertEqual(result["files"], len(result["paths"]))
        self.assertIn("runtime/launch.py", result["paths"])
        self.assertIn("containers/runtime.Dockerfile", result["paths"])
        self.assertIn("bench/adapters/metrics.py", result["paths"])
        self.assertIn("tests/publish.py", result["paths"])
        manifest = json.loads((self.destination / source_export.MANIFEST_NAME).read_bytes())
        self.assertEqual(manifest, {"version": 1, "sha256": result["sha256"]})
        self.assertEqual(set(self.tree(self.destination)), set(result["paths"]) | {source_export.MANIFEST_NAME})
        for relative in result["paths"]:
            source_bytes = (self.root / relative).read_bytes()
            exported = (self.destination / relative).read_bytes()
            self.assertEqual(source_bytes, exported)
            self.assertEqual(hashlib.sha256(exported).hexdigest(), result["sha256"][relative])
        other = self.base / "other-source"
        self.assertEqual(source_export.export_source(self.root, other), result)
        self.assertEqual(self.tree(other), self.tree(self.destination))

    def test_required_static_and_runtime_files_cannot_be_omitted(self):
        for relative in source_export.REQUIRED_FILES:
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.unlink()
                self.assert_refused_without_writes()
                path.write_bytes(original)

    def test_private_and_nested_files_are_never_selected(self):
        private_paths = (
            "config/bench.toml", "config/claude-provider.json", ".env",
            "data/dashboard-token", "data/discovery/cache.json", "runs/session/raw.json",
            "artifacts/index.html", "runtime/opaque-uuid/launch.py", "runtime/private.py",
            "bench/__pycache__/cached.py", "bench/private/nested.py",
            "bench/adapters/private/nested.py", "bench/tasks/private.json",
            "tests/random_helper.py", "tests/__pycache__/test_private.py",
            ".claude/auth.json", "cli-assets/provider.json",
        )
        credential = "sk-" + "A" * 48
        for relative in private_paths:
            self.put(relative, credential)
        result = self.export()
        self.assertTrue(set(private_paths).isdisjoint(result["paths"]))
        self.assertNotIn(credential.encode(), b"".join(self.tree(self.destination).values()))

    def test_optional_helpers_are_included_when_present(self):
        for helper in source_export.OPTIONAL_HELPERS:
            self.put("tests/" + helper, "# public optional helper\n")
        result = self.export()
        for helper in source_export.OPTIONAL_HELPERS:
            self.assertIn("tests/" + helper, result["paths"])
        self.assertNotIn("tests/random_helper.py", result["paths"])

    def test_optional_browser_checker_absence_is_allowed(self):
        result = self.export()
        self.assertNotIn("tests/verify_public_snapshot.py", result["paths"])

    def test_escaping_absolute_and_nonallowlisted_paths_are_rejected(self):
        for relative in ("../README.md", "/README.md", "bench/../README.md", "./README.md",
                         "web//app.js", "web\\app.js", "web/app.js/", "", "bad\x00name",
                         "config/bench.toml", "runtime/opaque/launch.py", "tests/private.py"):
            with self.subTest(relative=relative), self.assertRaises(RuntimeError):
                source_export.validate_selected_source(self.root, relative)
        self.assertFalse(self.destination.exists())

    def test_source_symlink_file_rejected_even_when_target_inside_root(self):
        path = self.root / "README.md"
        path.unlink()
        path.symlink_to(self.root / "pyproject.toml")
        self.assert_refused_without_writes()

    def test_source_symlink_escape_rejected_before_any_write(self):
        outside = self.base / "outside.txt"
        outside.write_text("private outside contents")
        path = self.root / "web/app.js"
        path.unlink()
        path.symlink_to(outside)
        self.assert_refused_without_writes()
        self.assertEqual(outside.read_text(), "private outside contents")

    def test_source_symlink_ancestor_rejected(self):
        original = self.root / "web"
        saved = self.root / "saved-web"
        original.rename(saved)
        original.symlink_to(saved, target_is_directory=True)
        self.assert_refused_without_writes()

    def test_root_symlink_and_external_ancestor_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            source_export.export_source(alias, self.destination)
        with self.assertRaises(RuntimeError):
            source_export.export_source(alias / ".." / "project", self.destination)
        self.assertFalse(self.destination.exists())

    def test_selected_directory_and_fifo_rejected_without_write(self):
        path = self.root / "README.md"
        path.unlink()
        path.mkdir()
        self.assert_refused_without_writes()
        path.rmdir()
        os.mkfifo(path)
        self.assert_refused_without_writes()

    def test_credential_signatures_rejected_without_echo_or_writes(self):
        # Build signatures at runtime so this test's source remains publishable.
        credentials = (
            "ghp_" + "a" * 36,
            "github_pat_" + "b" * 70,
            "sk-" + "c" * 48,
            "sk-" + "proj-" + "d" * 90,
            "sk-" + "ant-" + "e" * 60,
            "AKIA" + "F" * 16,
            "AIza" + "G" * 35,
            "xoxb-" + "1234567890-" * 4,
            "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "confidential-body",
        )
        for credential in credentials:
            with self.subTest(signature=credential[:4]):
                self.put("web/app.js", "// " + credential)
                with self.assertRaises(RuntimeError) as caught:
                    self.export()
                self.assertNotIn(credential, str(caught.exception))
                self.assertNotIn("confidential-body", str(caught.exception))
                self.assertFalse(self.destination.exists())

    def test_private_json_environment_and_credential_urls_rejected(self):
        value = "confidential-" + "credential-value-123"
        payloads = (
            json.dumps({"api_key": value}),
            json.dumps({"apiKey": value}),
            json.dumps({"secret": value}),
            json.dumps({"Authorization": "Bearer " + value}),
            "OPENAI_API_KEY=" + value,
            "export PROVIDER_TOKEN=" + value,
            "https://operator:" + value + "@provider.example/v1",
            "https://provider.example/v1?api_key=" + value,
            "https://provider.example/v1?key=" + value,
            "https://" + value + "@provider.example/v1",
        )
        for payload in payloads:
            self.put("docs/methodology.md", payload)
            with self.assertRaises(RuntimeError) as caught:
                self.export()
            self.assertNotIn(value, str(caught.exception))
            self.assertFalse(self.destination.exists())

    def test_legitimate_fieldnames_loopback_and_placeholders_pass(self):
        text = '\n'.join((
            'FIELDS = ["api_key", "token", "Authorization"]',
            'TOKEN = "unit-test-secret"',
            'API_KEY = "dummy-container-token"',
            'API_KEY = "<YOUR_API_KEY>"',
            'SECRET = "${PROVIDER_SECRET}"',
            'AUTH_TOKEN = read_token()',
            'AUTHORIZATION = "Bearer " + secret',
            'url = "http://127.0.0.1:8765"',
            'url = "http://localhost:8765"',
            'url = "https://first:secret@fixture.invalid/v1?key=hidden"',
            'url = f"http://{config[\'host\']}:{port}"',
            'documented_prefixes = ["sk-abc", "ghp_abc", "github_pat_abc"]',
        ))
        self.put("tests/test_small.py", text)
        self.export()
        self.assertEqual((self.destination / "tests/test_small.py").read_text(), text)

    def test_content_failure_does_not_modify_existing_export(self):
        self.export()
        original = self.tree(self.destination)
        self.put("README.md", "# changed README\n")
        self.put("web/app.js", "sk-" + "a" * 48)
        self.assert_refused_without_writes()
        self.assertEqual(self.tree(self.destination), original)

    def test_identical_reexport_preserves_file_and_manifest_bytes_and_mtimes(self):
        first = self.export()
        original = self.tree(self.destination)
        mtimes = {name: (self.destination / name).stat().st_mtime_ns for name in original}
        self.assertEqual(self.export(), first)
        self.assertEqual(self.tree(self.destination), original)
        self.assertEqual({name: (self.destination / name).stat().st_mtime_ns for name in original}, mtimes)

    def test_untampered_managed_source_can_be_updated_and_extended(self):
        first = self.export()
        changed = "# updated public README\n"
        self.put("README.md", changed)
        self.put("tests/verify_public_snapshot.py", "# new public helper\n")
        second = self.export()
        self.assertEqual((self.destination / "README.md").read_text(), changed)
        self.assertNotEqual(first["sha256"]["README.md"], second["sha256"]["README.md"])
        self.assertIn("tests/verify_public_snapshot.py", second["paths"])
        self.assertEqual(json.loads((self.destination / source_export.MANIFEST_NAME).read_bytes())["sha256"], second["sha256"])

    def test_existing_unmanaged_files_are_not_adopted_even_if_identical(self):
        self.destination.mkdir(parents=True)
        (self.destination / "README.md").write_bytes((self.root / "README.md").read_bytes())
        self.assert_refused_without_writes()

    def test_empty_initial_destination_is_supported(self):
        self.destination.mkdir(parents=True)
        self.export()
        self.assertTrue((self.destination / source_export.MANIFEST_NAME).is_file())

    def test_existing_unmanaged_empty_directory_is_not_adopted(self):
        (self.destination / "unrecognized").mkdir(parents=True)
        self.assert_refused_without_writes()

    def test_unmanaged_file_added_after_export_is_rejected(self):
        self.export()
        (self.destination / "unknown.txt").write_text("must not publish")
        self.assert_refused_without_writes()

    def test_managed_tamper_and_missing_managed_file_rejected(self):
        self.export()
        path = self.destination / "README.md"
        path.write_text("local changes must survive")
        self.assert_refused_without_writes()
        self.assertEqual(path.read_text(), "local changes must survive")
        path.unlink()
        self.assert_refused_without_writes()

    def test_stale_managed_files_are_not_deleted_or_silently_published(self):
        self.export()
        (self.root / "tests/publish.py").unlink()
        self.assert_refused_without_writes()
        self.assertTrue((self.destination / "tests/publish.py").is_file())

    def test_destination_symlink_and_ancestor_symlink_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.destination.parent.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertEqual(list(outside.iterdir()), [])
        self.destination.unlink()
        self.destination.parent.rmdir()
        self.destination.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertEqual(list(outside.iterdir()), [])

    def test_managed_destination_file_and_directory_symlinks_rejected(self):
        self.export()
        file = self.destination / "README.md"
        file.unlink()
        file.symlink_to(self.root / "README.md")
        with self.assertRaises(RuntimeError):
            self.export()
        file.unlink()
        file.write_bytes((self.root / "README.md").read_bytes())
        directory = self.destination / "web"
        saved = self.base / "saved-web"
        directory.rename(saved)
        directory.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.export()

    def test_invalid_or_symlink_manifest_rejected_without_writes(self):
        self.destination.mkdir(parents=True)
        manifest = self.destination / source_export.MANIFEST_NAME
        for data in ("not json", "[]", '{"version": 2, "sha256": {}}',
                     json.dumps({"version": 1, "sha256": {"../private": "a" * 64}}),
                     json.dumps({"version": 1, "sha256": {"README.md": "not-a-hash"}})):
            manifest.write_text(data)
            self.assert_refused_without_writes()
        manifest.unlink()
        outside = self.base / "manifest.json"
        outside.write_text('{"version": 1, "sha256": {}}')
        manifest.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.export()

    def test_example_config_requires_valid_commented_generic_toml(self):
        bad_configs = (
            '[server]\nhost = "127.0.0.1"\n',
            '# generic example\n[server\n',
            '# generic example\npath = "/home/' + 'ubuntu/private"\n',
            '# generic example\n' + 'api_key' + ' = ' + json.dumps('confidential-value-123') + '\n',
        )
        for config in bad_configs:
            self.put("config/bench.example.toml", config)
            self.assert_refused_without_writes()
        valid = '# Customize this generic example.\n[paths]\ndatabase = "data/bench.sqlite3"\n'
        self.put("config/bench.example.toml", valid)
        self.export()
        self.assertEqual(tomllib.loads((self.destination / "config/bench.example.toml").read_text()), tomllib.loads(valid))

    def test_real_project_exports_only_safe_source_and_reexports_identically(self):
        root = Path(__file__).resolve().parent.parent
        result = source_export.export_source(root, self.destination)
        self.assertIn("tests/source_export.py", result["paths"])
        self.assertIn("tests/test_source_export.py", result["paths"])
        self.assertIn("config/bench.example.toml", result["paths"])
        self.assertNotIn("config/bench.toml", result["paths"])
        original = self.tree(self.destination)
        self.assertEqual(source_export.export_source(root, self.destination), result)
        self.assertEqual(self.tree(self.destination), original)
        for relative in result["paths"]:
            exported = (self.destination / relative).read_bytes()
            self.assertEqual(exported, (root / relative).read_bytes())
            self.assertEqual(hashlib.sha256(exported).hexdigest(), result["sha256"][relative])
        config = (self.destination / "config/bench.example.toml").read_text()
        tomllib.loads(config)
        self.assertNotIn("/home/" + "ubuntu", config)


if __name__ == "__main__":
    unittest.main()
