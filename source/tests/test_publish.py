"""Tests for the sharing boundary and the snapshot's media pipeline.

The published page is the one artifact that leaves this machine, so the
checks that decide what may cross it are tested rather than assumed: a
snapshot that still referenced /app.js, or carried a host path, would be
published silently and only noticed after it was public.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_snapshot
import publish

SNAPSHOT_HEAD = '<!doctype html><html><head><style>body{}</style></head><body>'
SNAPSHOT_TAIL = "</body></html>"


def snapshot_html(data: str, extra: str = "", tail: str = SNAPSHOT_TAIL) -> str:
    return (SNAPSHOT_HEAD + extra +
            f'<script>window.BENCH_SNAPSHOT={data};</script>' + tail)


MINIMAL = json.dumps({"runs": [], "evidence": {}}, ensure_ascii=False)


class PublishBoundaryTests(unittest.TestCase):
    def test_a_clean_page_passes(self):
        stats = publish.verify(snapshot_html(MINIMAL))
        self.assertEqual(stats["runs"], 0)
        self.assertEqual(stats["evidence"], 0)

    def test_a_host_path_is_refused(self):
        for needle in ("/home/ubuntu", "127.0.0.1", "/workspace", "localhost"):
            page = snapshot_html(MINIMAL, extra=f"<!-- {needle} -->")
            with self.assertRaises(RuntimeError, msg=needle):
                publish.verify(page)

    def test_a_credential_is_refused(self):
        for needle in ("sk-abc", "ghp_abc", "github_pat_abc"):
            page = snapshot_html(MINIMAL, extra=f"<!-- {needle} -->")
            with self.assertRaises(RuntimeError, msg=needle):
                publish.verify(page)

    def test_an_external_asset_is_refused(self):
        # A published page that fetches its script or stylesheet is not the
        # self-contained artifact this project promises to hand over.
        for tag in ('<script src="/app.js"></script>',
                    '<link rel="stylesheet" href="/styles.css">',
                    '<img src="https://example.com/a.png">'):
            page = snapshot_html(MINIMAL, tail=tag + SNAPSHOT_TAIL)
            with self.assertRaises(RuntimeError, msg=tag):
                publish.verify(page)

    def test_inlined_assets_are_accepted(self):
        page = snapshot_html(
            MINIMAL,
            extra='<style>body{color:red}</style>',
            tail='<script>console.log(1)</script>' + SNAPSHOT_TAIL)
        publish.verify(page)

    def test_a_page_without_data_is_refused(self):
        with self.assertRaises(RuntimeError):
            publish.verify(SNAPSHOT_HEAD + SNAPSHOT_TAIL)

    def test_dated_copies_are_named_after_the_data(self):
        page = snapshot_html(json.dumps({"runs": [{"date": "2026-09-30"}], "evidence": {}}))
        self.assertEqual(publish.snapshot_dates(page), ["2026-09-30"])
        with tempfile.TemporaryDirectory() as tmp:
            site = publish.stage(page, Path(tmp), "标题", "说明")
            self.assertTrue((site / "index.html").is_file())
            self.assertTrue((site / "2026-09-30.html").is_file())
            self.assertTrue((site / ".nojekyll").is_file())


    def test_republication_preserves_old_date_bytes_and_links_filter_current_data(self):
        first = snapshot_html(json.dumps({"runs": [{"date": "2026-09-30"}], "evidence": {}}))
        second = snapshot_html(json.dumps({"runs": [{"date": "2026-09-30"}, {"date": "2026-10-08"}], "evidence": {}}))
        with tempfile.TemporaryDirectory() as tmp:
            repo, site = Path(tmp) / "repo", Path(tmp) / "site"
            repo.mkdir()
            publish.stage(first, repo, "标题", "说明")
            previous = (repo / "2026-09-30.html").read_bytes()
            publish.stage(second, site, "标题", "说明")
            publish.copy_site(site, repo)
            self.assertEqual((repo / "2026-09-30.html").read_bytes(), previous)
            self.assertEqual((repo / "index.html").read_text(), second)
            self.assertTrue((repo / "2026-10-08.html").is_file())
            readme = (repo / "README.md").read_text()
            self.assertIn("合并", readme)
            self.assertIn("date_from=2026-09-30&date_to=2026-09-30", readme)


class PublicationFlowTests(unittest.TestCase):
    def test_source_link_survives_report_only_republication(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = publish.stage(snapshot_html(MINIMAL), Path(tmp), "标题", "说明")
            self.assertIn("source/README.md", (site / "README.md").read_text())

    def test_clone_failure_does_not_create_a_repository_or_commit(self):
        commands = []

        def failed_clone(command, **kwargs):
            commands.append(command)
            if command[:3] == ["gh", "repo", "clone"]:
                raise RuntimeError("fixture clone failure")
            return SimpleNamespace(stdout="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(publish, "run", side_effect=failed_clone):
            with self.assertRaisesRegex(RuntimeError, "clone failure"):
                publish.publish_site(Path(tmp) / "site", Path(tmp), "owner/existing", "main", {"runs": 58})
        self.assertFalse(any(command[:3] == ["gh", "repo", "create"] for command in commands))
        self.assertFalse(any("commit" in command or "push" in command for command in commands))

    def exercise_publication(self, branch="main", staged="index.html\0README.md\0.nojekyll\0"):
        commands = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            site = publish.stage(snapshot_html(MINIMAL), workspace / "site", "标题", "说明")

            def fake_run(command, **kwargs):
                commands.append(command)
                if command[:3] == ["gh", "repo", "clone"]:
                    repo = Path(command[-1])
                    repo.mkdir()
                    (repo / "2026-09-30.html").write_text("old immutable dated bytes")
                    (repo / "unrelated.txt").write_text("unmanaged retained bytes")
                if "--name-only" in command:
                    return SimpleNamespace(stdout=staged, returncode=0)
                if "rev-parse" in command:
                    return SimpleNamespace(stdout="a" * 40 + "\n", returncode=0)
                return SimpleNamespace(stdout="", returncode=0)

            with mock.patch.object(publish, "run", side_effect=fake_run):
                result = publish.publish_site(site, workspace, "owner/existing", branch, {"runs": 58})
            self.assertEqual((workspace / "repo/2026-09-30.html").read_text(), "old immutable dated bytes")
            self.assertEqual((workspace / "repo/unrelated.txt").read_text(), "unmanaged retained bytes")
        return result, commands

    def test_publication_branches_before_commit_and_pushes_explicit_target(self):
        for branch in ("main", "reports"):
            with self.subTest(branch=branch):
                result, commands = self.exercise_publication(branch)
                checkout = next(command for command in commands if "checkout" in command)
                commit = next(command for command in commands if "commit" in command)
                fetch = next(command for command in commands if "fetch" in command)
                push = next(command for command in commands if "push" in command)
                add = next(command for command in commands if "add" in command)
                self.assertIn("-b", checkout)
                self.assertTrue(checkout[-2].startswith("report-publish-"))
                self.assertEqual(checkout[-1], "origin/" + branch)
                self.assertLess(commands.index(checkout), commands.index(commit))
                self.assertEqual(fetch[-1], f"{branch}:refs/remotes/origin/{branch}")
                self.assertEqual(push[-2:], ["origin", "HEAD:" + branch])
                self.assertNotIn("--force", push)
                self.assertIn("--", add)
                self.assertNotIn("-A", add)
                self.assertTrue(commit[-1].endswith("Co-Authored-By: Claude Code <noreply@anthropic.com>"))
                self.assertEqual(result["commit"], "a" * 40)

    def test_run_preserves_stdout_only_error_diagnostics(self):
        result = SimpleNamespace(returncode=2, stderr="", stdout="report.html:19: trailing whitespace.\n")
        with mock.patch.object(publish.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "trailing whitespace"):
                publish.run(["git", "-C", "/fixture/repo", "diff", "--cached", "--check"])

    def test_whitespace_policy_preserves_eof_but_rejects_trailing_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            publish.run(["git", "init", str(repo)])
            source = repo / "source.py"
            source.write_text("frozen_source\n\n")
            publish.run(["git", "-C", str(repo), "add", "--", "source.py"])
            check = ["git", "-C", str(repo), "-c", "core.whitespace=-blank-at-eof", "diff", "--cached", "--check"]
            self.assertEqual(publish.run(check).returncode, 0)
            source.write_text("frozen_source \n\n")
            publish.run(["git", "-C", str(repo), "add", "--", "source.py"])
            result = publish.run(check, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("trailing whitespace", result.stdout)

    def test_unexpected_staged_file_prevents_commit_and_push(self):
        with self.assertRaisesRegex(RuntimeError, "Unexpected staged path"):
            self.exercise_publication(staged="data/private.json\0")

    def test_real_source_stage_copy_preserves_hashes_and_old_dates(self):
        import hashlib

        root = Path(__file__).resolve().parent.parent
        page = snapshot_html(json.dumps({"runs": [{"date": "2026-09-30"}], "evidence": {}}))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            site = publish.stage(page, workspace / "site", "标题", "说明", source_root=root)
            repo = workspace / "repo"
            repo.mkdir()
            dated = repo / "2026-09-30.html"
            dated.write_bytes(b"immutable historical report")
            publish.copy_site(site, repo)
            manifest = json.loads((site / "source/.source-manifest.json").read_text())
            self.assertEqual(json.loads((repo / "source/.source-manifest.json").read_text()), manifest)
            for relative, digest in manifest["sha256"].items():
                data = (repo / "source" / relative).read_bytes()
                self.assertEqual(data, (site / "source" / relative).read_bytes(), relative)
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest, relative)
            self.assertIn("runtime/launch.py", manifest["sha256"])
            self.assertIn("runtime/evaluate_worker.py", manifest["sha256"])
            self.assertNotIn("config/bench.toml", manifest["sha256"])
            self.assertEqual(dated.read_bytes(), b"immutable historical report")
            baseline = {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
            publish.copy_site(site, repo)
            self.assertEqual({path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}, baseline)

    def test_symlink_report_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = publish.stage(snapshot_html(MINIMAL), root / "site", "标题", "说明")
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside"
            outside.write_text("do not overwrite")
            (repo / "index.html").symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "Unsafe report destination"):
                publish.copy_site(site, repo)
            self.assertEqual(outside.read_text(), "do not overwrite")


class EvidenceTests(unittest.TestCase):
    def test_builder_removes_indented_script_without_trailing_whitespace(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data/dashboard-token").write_text("fixture-dashboard-token")
            (root / "web").mkdir()
            for name in ("index.html", "app.js", "styles.css"):
                (root / "web" / name).write_text((make_snapshot.ROOT / "web" / name).read_text())
            destination = root / "report.html"
            with (mock.patch.object(make_snapshot, "ROOT", root),
                  mock.patch.object(make_snapshot, "login"),
                  mock.patch.object(make_snapshot, "fetch", side_effect=[[], []]),
                  mock.patch.object(make_snapshot, "collect_evidence", return_value=({}, {})),
                  mock.patch.object(sys, "argv", ["make_snapshot.py", str(destination)]),
                  redirect_stdout(StringIO())):
                make_snapshot.main()
            html = destination.read_text()
            self.assertNotIn('<script src="/app.js"', html)
            self.assertFalse(any(line.endswith((" ", "\t")) for line in html.splitlines()))
            self.assertEqual(publish.verify(html)["runs"], 0)

    def test_recovery_metadata_survives_but_private_evaluation_error_does_not(self):
        run = {"id": "child", "attempt": 1, "retry_of": "parent", "generation_status": "failed",
               "archive_status": "complete", "evaluation": {"status": "infrastructure_error",
               "error": "Timeout at /home/ubuntu/private", "evidence": []}}
        safe = make_snapshot.sanitize(run)
        self.assertEqual(safe["retry_of"], "parent")
        self.assertEqual(safe["attempt"], 1)
        self.assertEqual(safe["generation_status"], "failed")
        self.assertNotIn("/home/ubuntu", json.dumps(safe))

    def test_only_raster_and_video_survive_sanitizing(self):
        run = {"id": "r1", "archive_dir": "runs/2026-09-30/r1",
               "evaluation": {"status": "completed",
                              "evidence": ["evidence/desktop.png", "evidence/animation.webm",
                                           "evidence/checks.json", "output/index.html"]},
               "artifacts": [{"path": "output/index.html", "kind": "source", "size": 1,
                              "sha256": "x"}]}
        kept = make_snapshot.sanitize(run)
        self.assertEqual(kept["evaluation"]["evidence"],
                         ["evidence/desktop.png", "evidence/animation.webm"])
        # The archived HTML is an attachment, never an embedded document: the
        # snapshot must not be able to execute a model artifact.
        self.assertNotIn("archive_dir", kept)
        self.assertNotIn("sha256", kept["artifacts"][0])

    def test_paths_cannot_escape_the_archive(self):
        root = Path("/srv/project")
        self.assertIsNone(make_snapshot._resolve(root, "runs/2026-09-30/r1", "../../etc/passwd"))
        self.assertIsNone(make_snapshot._resolve(root, "runs/2026-09-30/r1", "/etc/passwd"))
        self.assertIsNone(make_snapshot._resolve(root, None, "evidence/desktop.png"))

    def test_a_missing_file_counts_as_missing_not_as_silent_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs/2026-09-30/r1/evidence").mkdir(parents=True)
            mapping, stats = make_snapshot.collect_evidence(
                [{"id": "r1", "archive_dir": "runs/2026-09-30/r1",
                  "evaluation": {"evidence": ["evidence/desktop.png"]}}], root)
        self.assertEqual(mapping, {})
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(stats["screenshots"], 0)

    def test_video_is_dropped_before_it_can_exceed_the_budget(self):
        # Screenshots are never dropped; a recording that does not fit is
        # skipped and counted, because an unlisted omission is a lie.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "runs/2026-09-30/r1/evidence"
            evidence.mkdir(parents=True)
            from PIL import Image
            Image.new("RGB", (8, 6), (10, 20, 30)).save(evidence / "desktop.png")
            make_snapshot._run(["ffmpeg", "-v", "error", "-f", "lavfi",
                                "-i", "color=c=blue:s=64x48:d=1", "-c:v", "libvpx",
                                "-pix_fmt", "yuv420p", "-y",
                                str(evidence / "animation.webm")])
            mapping, stats = make_snapshot.collect_evidence(
                [{"id": "r1", "archive_dir": "runs/2026-09-30/r1",
                  "evaluation": {"evidence": ["evidence/desktop.png",
                                              "evidence/animation.webm"]}}],
                root, budget=0)
        self.assertEqual(stats["screenshots"], 1)
        self.assertEqual(stats["skipped_video"], 1)
        self.assertEqual(stats["missing"], 0)
        self.assertNotIn("r1/evidence/animation.webm", mapping)
        self.assertIn("r1/evidence/desktop.png", mapping)

    def test_png_is_published_as_jpeg(self):
        # The archives stay PNG; the shared copy is re-encoded, so its data URI
        # must announce the format it actually carries or the client rejects it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "runs/2026-09-30/r1/evidence"
            evidence.mkdir(parents=True)
            from PIL import Image
            Image.new("RGB", (8, 6), (10, 20, 30)).save(evidence / "desktop.png")
            mapping, stats = make_snapshot.collect_evidence(
                [{"id": "r1", "archive_dir": "runs/2026-09-30/r1",
                  "evaluation": {"evidence": ["evidence/desktop.png"]}}], root)
        uri = mapping["r1/evidence/desktop.png"]
        self.assertTrue(uri.startswith("data:image/jpeg;base64,"), uri[:40])
        self.assertEqual(stats["screenshots"], 1)
        # The key is exactly what the client looks up: "<run id>/<path>".
        self.assertIn("r1/evidence/desktop.png", mapping)


class ReviewFileTests(unittest.TestCase):
    """The suggested-score file is written by hand; it must stay loadable."""

    def test_suggested_reviews_parse_and_reference_real_runs(self):
        path = Path(make_snapshot.ROOT) / "data/reviews/ai-suggested-visual-v1.json"
        if not path.is_file():
            self.skipTest("no suggested-review file")
        data = json.loads(path.read_text(encoding="utf-8"))
        archives = {}
        for manifest in (Path(make_snapshot.ROOT) / "runs").glob("*/*/manifest.json"):
            record = json.loads(manifest.read_text(encoding="utf-8"))
            archives[record["id"]] = record
        for run_id, entry in data["reviews"].items():
            self.assertIn(run_id, archives, f"{entry['model']}/{entry['task']} 的评分指向不存在的运行")
            record = archives[run_id]
            self.assertEqual(record["model"], entry["model"])
            self.assertEqual(record["task_id"], entry["task"])
            self.assertEqual(record["status"], entry["status"])
            if entry["turns"] is not None:
                self.assertEqual(record["metrics"]["num_turns"], entry["turns"])
            for dimension, score in (entry["scores"] or {}).items():
                self.assertIsInstance(score, int)
                self.assertGreaterEqual(score, 0)
                self.assertLessEqual(score, 4)


if __name__ == "__main__":
    unittest.main()
