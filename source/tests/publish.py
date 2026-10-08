"""Publish a sanitized report and optional allowlisted source to an existing repo.

Report evidence is inlined; raw archives, credentials and local provider
configuration never leave the benchmark machine. Source is exported through a
separate boundary, not by recursively copying the working directory.
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = ("/home/ubuntu", "127.0.0.1", "/workspace", "localhost",
             "sk-", "ghp_", "github_pat_", "data/dashboard-token")
EXTERNAL_ASSET = re.compile(r'<(?:link[^>]+href|script[^>]+src|img[^>]+src)="(?!data:)([^"]+)"', re.I)
SHARE_QUERY = "?purpose=benchmark"


def verify(html: str) -> dict:
    """Refuse to publish anything that is not a shareable, self-contained page."""
    problems = []
    for needle in FORBIDDEN:
        if needle in html:
            problems.append(f"contains a private detail: {needle}")
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
    data = json.loads(match.group(1).replace("\\u003c", "<"))
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
        "源码与运行配置分开，私有配置、二进制和原始归档不上传。\n"
        "index.html 随发布更新。日期命名文件是首次创建时的合并数据副本，"
        "不是该日期专属数据；旧版文件保留原字节，不重新包装成日快照。\n\n"
        "[项目源码与复现说明](source/README.md)\n\n"
        f"[正式评测](index.html{SHARE_QUERY}) · [含冒烟的全部尝试](index.html?purpose=)\n\n"
        f"按样本日期筛选最新合并数据：\n{links}\n\n"
        "## 结果解释\n\n"
        "会话正常结束、页面交付、自动检查与人工视觉评分分别展示。"
        "CLI费用未经供应商账单核验，未知计量不补零。既有14条非盲AI建议评分仍待人工复核。\n\n"
        "2026-10-08 的 Sol 与 6.1-Sol 鳄鱼题人工补跑均正常结束、8/8检查通过；"
        "此前失败保留，不被新结果覆盖。Astra/Luna早先黑洞样本的上游count_tokens403"
        "曾被旧评估逻辑误记为路由违规；原检查仍保留，源码已区分上游与本地拒绝，"
        "不把历史误判当成模型调用了其他路由的证据。\n\n"
        "重新发布请在 `source/` 内执行 `python3 tests/publish.py <snapshot.html> --include-source`。\n",
        encoding="utf-8")
    (directory / ".nojekyll").write_text("", encoding="utf-8")
    return directory


def copy_site(site, repo):
    """Update only managed files, retaining already-published dated reports."""
    for name in ("index.html", "README.md", ".nojekyll"):
        target = repo / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError("Unsafe report destination: " + name)
    if (site / "source").exists():
        from source_export import export_source
        copied = export_source(site / "source", repo / "source")
        expected = json.loads((site / "source/.source-manifest.json").read_text())["sha256"]
        if copied["sha256"] != expected:
            raise RuntimeError("Staged source changed before publication")
    for name in ("index.html", "README.md", ".nojekyll"):
        source, target = site / name, repo / name
        if target.exists() and target.read_bytes() == source.read_bytes():
            continue
        shutil.copyfile(source, target)
    for dated in site.glob("20??-??-??.html"):
        target = repo / dated.name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError("Unsafe dated report destination: " + dated.name)
        if not target.exists():
            shutil.copyfile(dated, target)


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
    copy_site(site, repo)
    managed = ["index.html", "README.md", ".nojekyll"]
    managed += sorted(path.name for path in site.glob("20??-??-??.html"))
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
    parser.add_argument("snapshot", nargs="?", default=str(ROOT / "artifacts/observatory.html"))
    parser.add_argument("--repo", default="edgora-ai/llm-bench-report")
    parser.add_argument("--title", default="模型能力基准 · 一次会话评测")
    parser.add_argument("--note", default="固定任务、固定隔离环境下的模型一次会话完成度对比。")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--include-source", action="store_true", help="include separately validated source, never local configuration or archives")
    parser.add_argument("--dry-run", action="store_true", help="stage and verify without cloning or pushing")
    args = parser.parse_args()
    html = Path(args.snapshot).read_text(encoding="utf-8")
    stats = verify(html)
    print(json.dumps({"verified": stats}, ensure_ascii=False))
    workspace = Path(tempfile.mkdtemp(prefix="llm-bench-publish-"))
    try:
        site = stage(html, workspace / "site", args.title, args.note, ROOT if args.include_source else None)
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
