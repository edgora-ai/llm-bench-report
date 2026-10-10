"""Publish a sanitized report and optional allowlisted source to an existing repo.

Accept a self-contained report or a validated static-media package. Evidence
and reviewed original payloads cross explicit manifest boundaries; project
source crosses a separate allowlist, never a recursive working-directory copy.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = ("/home/ubuntu", "127.0.0.1", "/workspace", "localhost", "data/dashboard-token")
CREDENTIAL = re.compile(r"(?<![A-Za-z0-9_])(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+")
EXTERNAL_ASSET = re.compile(r'<(?:link[^>]+href|script[^>]+src|img[^>]+src)="(?!data:)([^"]+)"', re.I)
REPORT_MANIFEST = ".report-manifest.json"
REPORT_ASSETS = ".report-assets.json"
MEDIA_PATH = re.compile(r"media/([a-f0-9]{64})\.(?:jpg|png|webp|gif|webm|mp4)\Z")
ORIGINAL_PATH = re.compile(r"originals/([a-f0-9]{64})\.json\Z")
VIEWER_PATH = re.compile(r"viewer/([a-f0-9]{64})\.(?:html|js)\Z")
ASSET_DIRECTORIES = ("media", "originals", "viewer")


def verify(html: str) -> dict:
    """Refuse to publish anything that is not a shareable, self-contained page."""
    problems = []
    for needle in FORBIDDEN:
        if needle in html:
            problems.append(f"contains a private detail: {needle}")
    if CREDENTIAL.search(html):
        problems.append("contains a credential signature")
    external = sorted({url for url in EXTERNAL_ASSET.findall(html)
                       if not url.startswith(("data:", "#"))})
    if external:
        problems.append(f"references external assets: {external[:5]}")
    if "window.BENCH_SNAPSHOT=" not in html:
        problems.append("carries no run data")
    if problems:
        raise RuntimeError("refusing to publish: " + "; ".join(problems))
    match = re.search(r"window\.BENCH_SNAPSHOT=(\{.*?\});</script>", html, re.S)
    if not match:
        raise RuntimeError("refusing to publish: snapshot data is not readable")
    from make_site import _json
    data = _json(match.group(1))
    if any(key in data for key in ("format", "transport", "originals")) or any(
            not isinstance(value, str) or not value.startswith("data:")
            for value in data.get("evidence", {}).values()):
        raise RuntimeError("refusing offline publication: external media requires a verified site directory")
    return {"runs": len(data.get("runs", [])),
            "evidence": len(data.get("evidence", {})),
            "bytes": len(html.encode("utf-8"))}


def snapshot_dates(html: str) -> list[str]:
    return sorted(set(re.findall(r'"date":\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})"', html)))


def stage(html: str, directory: Path, title: str, note: str, source_root=None) -> Path:
    """Stage a combined snapshot; dated copies are not per-day datasets."""
    directory.mkdir(parents=True, exist_ok=True)
    if source_root is not None:
        from source_export import export_source
        export_source(Path(source_root), directory / "source")
    (directory / "index.html").write_text(html, encoding="utf-8")
    dates = snapshot_dates(html)
    for date in dates:
        dated = directory / f"{date}.html"
        if not dated.exists():
            shutil.copyfile(directory / "index.html", dated)
    links = "\n".join(f"- [{date}](index.html?purpose=benchmark&date_from={date}&date_to={date})" for date in dates)
    (directory / "README.md").write_text(
        f"# {title}\n\n{note}\n\n本仓库的报告由 `llm-bench` 发布步骤自动生成，"
        "报告为只读脱敏合并快照，不含凭据、原始日志或本机路径。"
        "源码与运行配置分开，私有配置、二进制和完整运行归档不上传。\n"
        "index.html 随发布更新。日期命名文件是首次创建时的合并数据副本，"
        "不是该日期专属数据；旧版文件保留原字节，不重新包装成日快照。\n\n"
        "[项目源码与复现说明](source/README.md)\n\n"
        "[同题作品对比](index.html) · [含冒烟的全部尝试](index.html?purpose=)\n\n"
        f"按样本日期筛选最新合并数据：\n{links}\n\n"
        "## 结果解释\n\n"
        "会话正常结束、页面交付、自动检查与人工视觉评分分别展示。"
        "CLI费用未经供应商账单核验，未知计量不补零。既有14条非盲AI建议评分仍待人工复核。\n\n"
        "画廊默认仅展示每个（工具，模型，任务）三元组的最新一次；"
        "点击详情进入完整实验台查看历史对照。全部尝试与正式样本计数见首页。\n\n"
        "第二轮补齐样本：1 个组合新交付；5 个组合已尝试仍失败（多为 session_error/length 空交付）；"
        "1 个组合条件阻塞（step-5 黑洞）。失败样本保留，不被覆盖，也不重跑。\n\n"
        "重新发布请在 `source/` 内执行 `python3 tests/publish.py <snapshot.html> --include-source`。\n",
        encoding="utf-8")
    (directory / ".nojekyll").write_text("", encoding="utf-8")
    return directory


def stage_static(bundle: Path, directory: Path, title: str, note: str, source_root=None) -> Path:
    """Stage a verified multi-file report; dates always use the offline input."""
    from make_site import verify_site

    manifest = verify_site(bundle)
    captured = checked_report_files(bundle, manifest)
    offline = captured["offline.html"].decode("utf-8")
    # v2 inline originals are audited by verify_site, never the legacy publisher.
    if manifest["version"] == 1:
        verify(offline)
    directory = stage(offline, directory, title, note, source_root)
    for relative, data in captured.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (directory / ".report-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    readme = directory / "README.md"
    preview_note = ("在线首页按需加载截图与录像，不执行模型HTML。"
                    if manifest["version"] == 1 else
                    "在线首页按需加载截图、录像及已审核的原作入口与运行依赖。"
                    "点击“运行原作”后在不透明源沙箱中执行原程序；不是视频回放。"
                    "原文件字节不变，执行文档经过隔离与资源装载适配。"
                    "沙箱限制下载、存储及常规外联，不提供容器级断网或资源配额。")
    readme.write_text(readme.read_text(encoding="utf-8").replace(
        "[项目源码与复现说明]", preview_note + "\n\n"
        "[下载离线完整版](offline.html)（自包含，首次下载较大）\n\n[项目源码与复现说明]").replace(
        "tests/publish.py <snapshot.html>", "tests/publish.py <site-directory>"), encoding="utf-8")
    return directory


def report_manifest(directory):
    from make_site import _json, read_asset_safe, validate_manifest

    path = directory / REPORT_MANIFEST
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return validate_manifest(_json(read_asset_safe(directory, REPORT_MANIFEST)))
    except (ValueError, UnicodeDecodeError) as error:
        raise RuntimeError("Invalid report manifest") from error


def checked_report_files(directory, manifest):
    from make_site import read_asset_safe

    captured = {}
    for relative, info in manifest["files"].items():
        data = read_asset_safe(directory, relative)
        if len(data) != info["size"] or hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise RuntimeError("Managed report file changed: " + relative)
        captured[relative] = data
    return captured


def asset_digest(relative, version):
    """Only these versioned namespaces can acquire append-only ownership."""
    if not isinstance(relative, str):
        return None
    match = MEDIA_PATH.fullmatch(relative)
    if match is None and version in {2, 3}:
        match = ORIGINAL_PATH.fullmatch(relative)
    if match is None and version == 3:
        match = VIEWER_PATH.fullmatch(relative)
    return match.group(1) if match else None


def owned_media(repo, previous):
    """Validate append-only ownership: v1 media, v2 originals, v3 viewer roles."""
    from make_site import _json, read_asset_safe

    ledger_path = repo / REPORT_ASSETS
    hashes = {}
    if ledger_path.exists() or ledger_path.is_symlink():
        if previous is None:
            raise RuntimeError("Media ownership ledger has no current report manifest")
        ledger = _json(read_asset_safe(repo, REPORT_ASSETS))
        if (not isinstance(ledger, dict) or set(ledger) != {"version", "sha256"}
                or type(ledger["version"]) is not int or ledger["version"] not in {1, 2, 3}
                or ledger["version"] != previous["version"]
                or not isinstance(ledger["sha256"], dict)):
            raise RuntimeError("Invalid media ownership ledger")
        hashes = ledger["sha256"]
        for relative, digest in hashes.items():
            expected = asset_digest(relative, ledger["version"])
            if expected is None or digest != expected:
                raise RuntimeError("Invalid managed asset path or hash")
    elif previous is not None:
        raise RuntimeError("Published report is missing its media ownership ledger")
    if previous is not None:
        for relative, info in previous["files"].items():
            if (asset_digest(relative, previous["version"]) is not None
                    and hashes.get(relative) != info["sha256"]):
                raise RuntimeError("Current report asset is not owned: " + relative)
    observed = set()
    for namespace in ASSET_DIRECTORIES:
        directory = repo / namespace
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("Unsafe " + namespace + " destination")
            if namespace == "viewer" and (previous is None or previous["version"] < 3):
                raise RuntimeError("Unmanaged viewer destination")
            for path in directory.iterdir():
                relative = namespace + "/" + path.name
                if relative not in hashes:
                    raise RuntimeError("Unmanaged " + namespace + " destination: " + relative)
                data = read_asset_safe(repo, relative)
                if hashlib.sha256(data).hexdigest() != hashes[relative]:
                    raise RuntimeError("Managed " + namespace + " was modified: " + relative)
                observed.add(relative)
    if observed != set(hashes):
        missing = sorted(set(hashes) - observed)[0].split("/", 1)[0]
        raise RuntimeError("Managed " + missing + " file is missing")
    return hashes


def prepare_static_copy(site, repo):
    """Capture and audit the whole staged package before touching the clone."""
    from make_site import read_asset_safe, verify_site

    current = report_manifest(site)
    previous = report_manifest(repo)
    if current is None:
        if previous is not None:
            raise RuntimeError("Publish a verified site directory to update this static-media repository")
        ledger = repo / REPORT_ASSETS
        if ledger.exists() or ledger.is_symlink():
            raise RuntimeError("Media ownership ledger has no current report manifest")
        return None
    if previous is not None and current["version"] < previous["version"]:
        raise RuntimeError("Report format downgrade requires an explicit migration")
    for namespace in ASSET_DIRECTORIES:
        expected = {path for path in current["files"] if path.startswith(namespace + "/")}
        directory = site / namespace
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("Unsafe staged " + namespace + " directory")
            if (not expected or {namespace + "/" + path.name for path in directory.iterdir()} != expected):
                raise RuntimeError("Unmanaged or missing staged " + namespace)
        elif expected:
            raise RuntimeError("Missing staged " + namespace + " directory")
    captured = checked_report_files(site, current)
    manifest_bytes = read_asset_safe(site, REPORT_MANIFEST)
    # The publication stage also contains source, README and dated history.
    # Audit only the exact package, without teaching the verifier to ignore extras.
    with tempfile.TemporaryDirectory(prefix="llm-bench-package-audit-") as tmp:
        audit = Path(tmp)
        for relative, data in captured.items():
            target = audit / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (audit / REPORT_MANIFEST).write_bytes(manifest_bytes)
        if verify_site(audit) != current:
            raise RuntimeError("Staged report manifest changed during validation")
    hashes = owned_media(repo, previous)
    if previous is not None:
        checked_report_files(repo, previous)
    elif (repo / "offline.html").exists() or (repo / "offline.html").is_symlink():
        raise RuntimeError("Unmanaged offline report destination")
    for relative, info in current["files"].items():
        if asset_digest(relative, current["version"]) is not None:
            if relative in hashes and hashes[relative] != info["sha256"]:
                raise RuntimeError("Content-addressed asset collision")
            hashes[relative] = info["sha256"]
    captured[REPORT_MANIFEST] = manifest_bytes
    captured[REPORT_ASSETS] = (json.dumps({"version": current["version"], "sha256": hashes},
                                         sort_keys=True, indent=2) + "\n").encode("utf-8")
    return captured


def checked_dated_files(site, offline):
    """Capture only complete offline copies named by the audited snapshot."""
    from make_site import read_asset_safe

    expected = {date + ".html" for date in snapshot_dates(offline.decode("utf-8"))}
    if {path.name for path in site.glob("20??-??-??.html")} != expected:
        raise RuntimeError("Unexpected or missing staged dates")
    captured = {}
    for relative in sorted(expected):
        data = read_asset_safe(site, relative)
        if data != offline:
            raise RuntimeError("Dated report differs from verified offline: " + relative)
        captured[relative] = data
    return captured


def copy_site(site, repo):
    """Update only managed files, retaining already-published dated reports."""
    from make_site import read_asset_safe

    for name in ("index.html", "README.md", ".nojekyll"):
        target = repo / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError("Unsafe report destination: " + name)
    static_files = prepare_static_copy(site, repo)
    root_files = {name: static_files[name] if static_files is not None and name in static_files
                  else read_asset_safe(site, name) for name in ("index.html", "README.md", ".nojekyll")}
    offline = static_files["offline.html"] if static_files is not None else root_files["index.html"]
    if static_files is None:
        verify(offline.decode("utf-8"))
    dated_files = checked_dated_files(site, offline)
    for relative in dated_files:
        target = repo / relative
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError("Unsafe dated report destination: " + relative)
    if (site / "source").exists():
        from source_export import export_source
        copied = export_source(site / "source", repo / "source")
        expected = json.loads((site / "source/.source-manifest.json").read_text())["sha256"]
        if copied["sha256"] != expected:
            raise RuntimeError("Staged source changed before publication")
    for name, data in root_files.items():
        if static_files is not None and name in static_files:
            continue
        target = repo / name
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)
    if static_files is not None:
        for relative, data in static_files.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.read_bytes() != data:
                target.write_bytes(data)
    for relative, data in dated_files.items():
        target = repo / relative
        if not target.exists():
            target.write_bytes(data)
    return sorted(set(static_files or {}) | set(dated_files))


def run(cmd, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    if check and result.returncode:
        action = "git " + cmd[3] if cmd[:2] == ["git", "-C"] else " ".join(cmd[:3])
        detail = (result.stderr or result.stdout)[-600:]
        raise RuntimeError(f"{action} failed (exit {result.returncode}): {detail}")
    return result


def publish_site(site, workspace, repo_name, branch, stats):
    run(["git", "check-ref-format", "--branch", branch])
    repo = workspace / "repo"
    run(["gh", "repo", "clone", repo_name, str(repo)])
    run(["git", "-C", str(repo), "fetch", "origin", f"{branch}:refs/remotes/origin/{branch}"])
    publish_branch = "report-publish-" + uuid.uuid4().hex[:12]
    run(["git", "-C", str(repo), "checkout", "-b", publish_branch, "origin/" + branch])
    static_paths = copy_site(site, repo)
    managed = ["index.html", "README.md", ".nojekyll"]
    managed += [path for path in static_paths if path not in managed]
    if (site / "source").is_dir():
        managed.append("source")
    run(["git", "-C", str(repo), "add", "--", *managed])
    staged = run(["git", "-C", str(repo), "diff", "--cached", "--name-only", "-z"]).stdout
    paths = [path for path in staged.split("\0") if path]
    allowed = set(managed) - {"source"}
    if "source" in managed:
        allowed.update("source/" + path.relative_to(site / "source").as_posix()
                       for path in (site / "source").rglob("*") if path.is_file())
    if any(path not in allowed for path in paths):
        raise RuntimeError("Unexpected staged path; no commit or push performed")
    print(json.dumps({"staged_paths": paths}, ensure_ascii=False))
    if not paths:
        return {"pushed": False, "reason": "managed files are byte-identical",
                "commit": run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()}
    # Preserve frozen source bytes, including existing empty lines at EOF.
    # Line-end whitespace and space-before-tab checks remain enabled.
    run(["git", "-C", str(repo), "-c", "core.whitespace=-blank-at-eof", "diff", "--cached", "--check"])
    message = f"更新评测报告与源码：{stats['runs']} 个样本\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>"
    run(["git", "-C", str(repo), "commit", "-m", message])
    commit = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    run(["git", "-C", str(repo), "push", "origin", "HEAD:" + branch])
    return {"pushed": True, "repo": repo_name, "branch": branch, "commit": commit,
            "url": f"https://{repo_name.split('/')[0]}.github.io/{repo_name.split('/')[1]}/", **stats}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", nargs="?", default=str(ROOT / "artifacts/observatory.html"),
                        help="self-contained HTML or a verified static-media site directory")
    parser.add_argument("--repo", default="edgora-ai/llm-bench-report")
    parser.add_argument("--title", default="模型能力基准 · 一次会话评测")
    parser.add_argument("--note", default="固定任务、固定隔离环境下的模型一次会话完成度对比。")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--include-source", action="store_true", help="include separately validated source, never local configuration or archives")
    parser.add_argument("--dry-run", action="store_true", help="stage and verify without cloning or pushing")
    args = parser.parse_args()
    target = Path(args.snapshot)
    if target.is_dir():
        from make_site import verify_site
        manifest = verify_site(target)
        stats = {**manifest["stats"], "bytes": manifest["stats"]["index_bytes"], "format": manifest["format"]}
        html = None
    else:
        html = target.read_text(encoding="utf-8")
        stats = verify(html)
    print(json.dumps({"verified": stats}, ensure_ascii=False))
    workspace = Path(tempfile.mkdtemp(prefix="llm-bench-publish-"))
    try:
        source_root = ROOT if args.include_source else None
        if html is None:
            site = stage_static(target, workspace / "site", args.title, args.note, source_root)
        else:
            site = stage(html, workspace / "site", args.title, args.note, source_root)
        if args.dry_run:
            print(json.dumps({"staged": str(site), "files": sorted(path.relative_to(site).as_posix()
                             for path in site.rglob("*") if path.is_file())}, ensure_ascii=False))
            return
        print(json.dumps(publish_site(site, workspace, args.repo, args.branch, stats), ensure_ascii=False))
    finally:
        shutil.rmtree(workspace)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error))
