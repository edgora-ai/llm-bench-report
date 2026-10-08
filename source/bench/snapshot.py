"""Build a self-contained read-only dashboard; never include credentials or raw logs."""
import base64
import copy
import json
from pathlib import Path
import re

from .storage import Store

PUBLIC_FIELDS = {"id", "batch_id", "date", "purpose", "tool", "model", "task_id", "task_name", "prompt_version", "prompt_hash", "condition_fingerprint", "started_at", "finished_at", "status", "duration_ms", "metrics", "checks", "evaluation", "reported_models", "backend_weights_version", "archive_status"}


def build_snapshot(root, db_path, out_path, max_bytes=15_000_000):
    root = Path(root).resolve()
    records = Store(db_path, root).list_runs({})
    runs = []
    evidence = {}
    for record in records:
        run = {k: copy.deepcopy(v) for k, v in record.items() if k in PUBLIC_FIELDS}
        run["error"] = "See private local run evidence for error details" if record.get("error") else None
        run["archive_dir"] = "Private local archive"
        run["reviews"] = []
        run["artifacts"] = [{k: a[k] for k in ("path", "kind", "size", "sha256") if k in a} for a in record.get("artifacts", []) if a.get("kind") in {"evidence", "output"}]
        run["evaluation"] = {k: copy.deepcopy(v) for k, v in record.get("evaluation", {}).items() if k in {"status", "evidence", "browser_version", "renderer", "sample_seconds", "frame_changed_fraction", "duration_ms"}}
        # Check details can contain URLs/browser logs. Publish states only.
        run["checks"] = [{"name": check.get("name"), "status": check.get("status"), "detail": "Details retained in private local archive"} for check in record.get("checks", [])]
        archive = root / record["archive_dir"]
        for relative in run.get("evaluation", {}).get("evidence", []):
            if not isinstance(relative, str) or ".." in Path(relative).parts:
                continue
            if relative not in {a["path"] for a in record.get("artifacts", [])}:
                continue
            path = archive / relative
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() == ".png" and path.name in {"desktop.png", "mobile.png"}:
                from PIL import Image
                import io
                image = Image.open(path)
                image.thumbnail((960, 640))
                encoded = io.BytesIO()
                image.convert("RGB").save(encoded, format="JPEG", quality=75)
                evidence[run["id"] + "/" + relative] = "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode()
            elif path.name == "animation.webm" and path.stat().st_size < 700_000:
                evidence[run["id"] + "/" + relative] = "data:video/webm;base64," + base64.b64encode(path.read_bytes()).decode()
        runs.append(run)
    tasks = []
    for path in sorted((root / "bench/tasks").glob("*.json")):
        task = json.loads(path.read_text())
        tasks.append({k: task[k] for k in ["id", "name", "version", "dimensions"] if k in task})
    payload = json.dumps({"runs": runs, "tasks": tasks, "evidence": evidence}, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = (root / "web/index.html").read_text()
    body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)
    # Give static form controls stable IDs for viewer state restoration.
    body = re.sub(r'<(input|select)([^>]*\bname="([^"]+)"[^>]*)>', lambda m: m.group(0) if re.search(r'\bid="', m.group(2)) else f'<{m.group(1)} id="bench-{m.group(3)}"{m.group(2)}>', body)
    css = (root / "web/styles.css").read_text()
    js = (root / "web/app.js").read_text()
    js = js.replace("  const params = new URLSearchParams(location.search);\n", "  const params = new URLSearchParams(location.search);\n  if (snapshot && !params.has('purpose') && !source.runs.some(run => run.purpose === 'benchmark')) params.set('purpose', 'smoke');\n")
    page = '<title>模型实验台</title>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n<style>\n' + css + '\n</style>\n' + body + '\n<script>window.BENCH_SNAPSHOT=' + payload + ';</script>\n<script>\n' + js + '\n</script>\n'
    while len(page.encode()) > max_bytes and evidence:
        key = next((k for k in evidence if "animation.webm" in k), next(iter(evidence)))
        del evidence[key]
        payload = json.dumps({"runs": runs, "tasks": tasks, "evidence": evidence}, ensure_ascii=False).replace("<", "\\u003c")
        page = '<title>模型实验台</title>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n<style>\n' + css + '\n</style>\n' + body + '\n<script>window.BENCH_SNAPSHOT=' + payload + ';</script>\n<script>\n' + js + '\n</script>\n'
    if len(page.encode()) > max_bytes:
        raise ValueError("Snapshot exceeds publication size limit")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return {"runs": len(runs), "embedded_evidence": len(evidence), "bytes": out_path.stat().st_size, "path": str(out_path)}
