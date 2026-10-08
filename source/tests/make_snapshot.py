"""Build a read-only, credential-free dashboard snapshot from real archives.

The snapshot inlines the same run data the local dashboard serves, so it can
be opened without the API and without the login token. It must therefore
carry no private path, no host address, and no log text: only what the
dashboard already renders, fetched over the authenticated local API.
"""

import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765"


def login(opener, token):
    # The server requires a same-origin POST; a cross-site login is refused.
    request = Request(f"{BASE}/api/login", method="POST",
                      data=json.dumps({"token": token}).encode(),
                      headers={"Content-Type": "application/json", "Origin": BASE})
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"login failed: {response.status}")


def fetch(opener, path):
    with opener.open(Request(f"{BASE}{path}"), timeout=60) as response:
        return json.loads(response.read().decode())


def sanitize(run):
    """Drop host paths and raw text; keep only what the dashboard renders."""
    kept = {key: run.get(key) for key in (
        "id", "batch_id", "date", "purpose", "tool", "model", "task_id", "task_name",
        "prompt_version", "prompt_hash", "condition_fingerprint", "conditions",
        "started_at", "finished_at", "status", "error", "duration_ms", "first_model_event_ms",
        "metrics", "checks", "evaluation", "artifacts", "reviews",
        "attempt", "retry_of", "generation_status", "generation_finished_at",
        "evaluation_finished_at", "archive_status", "error_category",
    ) if key in run}
    kept["error"] = _error_class(kept.get("error"))
    kept["artifacts"] = [{"path": a.get("path"), "kind": a.get("kind"), "size": a.get("size")}
                         for a in kept.get("artifacts") or []]
    # Keep the evidence paths so the gallery can still list them; the bytes
    # travel separately in ``evidence`` and the client resolves them by
    # "<run id>/<path>". Only raster images and video are carried -- HTML and
    # SVG are never embedded, so a model artifact cannot execute on the page.
    evaluation = kept.get("evaluation") or {}
    kept["evaluation"] = {**evaluation,
                          "evidence": [e for e in evaluation.get("evidence", [])
                                       if MEDIA_RE.search(str(e))]}
    if "error" in kept["evaluation"]:
        kept["evaluation"]["error"] = _error_class(kept["evaluation"]["error"])
    return kept


MEDIA_RE = re.compile(r"\.(png|jpe?g|webp|gif|webm|mp4)$", re.I)

# A base64 data URI costs 4 characters per 3 bytes, so inlining the archives
# verbatim produced a 98MB page: one recording was 28MB of VP8. Everything is
# re-encoded for sharing -- the archives keep the originals, this is a
# publishing derivative. Measured on the largest recording: 28MB -> 0.6MB.
IMAGE_QUALITY = 82
VIDEO_HEIGHT = 640
VIDEO_CRF = 40
MAX_EVIDENCE_BYTES = 90 * 1024 * 1024
SCREENSHOT_ONLY_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VIDEO_SUFFIXES = (".webm", ".mp4")


def _run(cmd, timeout=600):
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {result.stderr[-400:].decode(errors='replace')}")
    return result


def _encode_image(source, destination):
    with Image.open(source) as image:
        image.convert("RGB").save(destination, "JPEG", quality=IMAGE_QUALITY,
                                   optimize=True, progressive=True)
    return destination


def _content_box(source):
    """Find the drawn area of a recorded frame.

    Playwright was told to record 1440x900 while the page laid out at a
    smaller size, so every recording carries a grey border and the encoded
    frame is mostly padding. The archives keep that defect as recorded; the
    shared copy crops it, because a reader should see the animation and not
    the recorder's mistake.
    """
    frame = source.with_suffix(".probe.png")
    try:
        _run(["ffmpeg", "-v", "error", "-i", str(source), "-frames:v", "1", "-y", str(frame)])
        with Image.open(frame) as image:
            pixels = np.asarray(image.convert("RGB")).astype(np.int16)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None
    finally:
        frame.unlink(missing_ok=True)
    # The padding is flat grey, so a pixel departing from its row/column
    # neighbourhood marks real content.
    spread = pixels.std(axis=2)
    rows = np.nonzero((spread > 6).sum(axis=1) > 20)[0]
    columns = np.nonzero((spread > 6).sum(axis=0) > 20)[0]
    height, width = pixels.shape[:2]
    if not len(rows) or not len(columns):
        return None
    box = (int(columns.min()), int(rows.min()),
           int(columns.max() - columns.min() + 1), int(rows.max() - rows.min() + 1))
    # Only crop when the padding is substantial; a small inset is just the
    # page's own margin and cutting it would change what was recorded.
    if box[2] * box[3] >= 0.9 * width * height:
        return None
    # yuv420p needs even dimensions.
    return (box[0] & ~1, box[1] & ~1, box[2] & ~1, box[3] & ~1)


def _encode_video(source, destination):
    box = _content_box(source)
    filters = []
    if box:
        filters.append(f"crop={box[2]}:{box[3]}:{box[0]}:{box[1]}")
    filters.append(f"scale=-2:{VIDEO_HEIGHT}:flags=bicubic")
    filters.append("setsar=1")
    # VP9 in WebM, not H.264 in MP4. The open-source Chromium that runs this
    # benchmark ships without proprietary codecs: an H.264 data URI failed with
    # "no supported streams", while VP9 decoded in that same browser.
    _run(["ffmpeg", "-v", "error", "-i", str(source), "-vf", ",".join(filters),
          "-c:v", "libvpx-vp9", "-crf", str(VIDEO_CRF), "-b:v", "0",
          "-row-mt", "1", "-cpu-used", "4", "-pix_fmt", "yuv420p",
          "-an", "-deadline", "good", "-y", str(destination)])
    return destination, box


def collect_evidence(runs, root, budget=MAX_EVIDENCE_BYTES, cache=None):
    """Inline media as data URIs, re-encoded for sharing.

    Screenshots always travel; a recording travels only while the total
    inline budget allows it, so one long session cannot crowd every other
    run's picture out of the file. Anything left out is counted and
    reported, never dropped silently.
    """
    mapping, stats = {}, {"screenshots": 0, "videos": 0, "skipped_video": 0,
                          "missing": 0, "bytes": 0, "original_bytes": 0, "cropped": 0}
    for run in runs:
        paths = (run.get("evaluation") or {}).get("evidence") or []
        for path in paths:
            lowered = str(path).lower()
            if lowered.endswith(SCREENSHOT_ONLY_SUFFIXES):
                if _inline(mapping, stats, root, run, path, "image", cache):
                    stats["screenshots"] += 1
            elif lowered.endswith(VIDEO_SUFFIXES):
                # Encode first, then weigh it: the budget is about what lands
                # in the page, and a trial encode just to measure it would
                # cost a minute per recording for nothing.
                try:
                    encoded, _ = _encode(root, run, path, "video", cache)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                    stats["missing"] += 1
                    print(f"  跳过 {run.get('id', '')[:8]}/{path}: {error}", file=sys.stderr)
                    continue
                if stats["bytes"] + encoded.stat().st_size > budget:
                    stats["skipped_video"] += 1
                    continue
                if _inline(mapping, stats, root, run, path, "video", cache):
                    stats["videos"] += 1
    return mapping, stats


def _resolve(root, archive_dir, path):
    """Find one archived file, refusing anything that escapes the archive."""
    if not archive_dir or not isinstance(path, str) or path.startswith("/") or ".." in path.split("/"):
        return None
    candidate = (root / archive_dir / path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _cache_key(root, run, path, kind):
    """Identify one encoded derivative, so a rerun reuses it."""
    source = _resolve(root, run.get("archive_dir"), path)
    if source is None:
        return None
    stamp = source.stat()
    return (str(source), stamp.st_mtime_ns, stamp.st_size, kind, IMAGE_QUALITY, VIDEO_CRF, VIDEO_HEIGHT)


def _encode(root, run, path, kind, cache):
    """Return (encoded file, crop box) for one piece of evidence."""
    source = _resolve(root, run.get("archive_dir"), path)
    if source is None:
        raise FileNotFoundError(f"evidence not found: {path}")
    key = _cache_key(root, run, path, kind)
    if cache is not None and key in cache:
        # The crop box is part of what was derived, not just the file: the
        # second caller needs it to report the crop, so it travels with the
        # encoded derivative instead of being dropped on the cache hit.
        return cache[key][0], cache[key][1]
    if kind == "image":
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            destination = Path(handle.name)
        box = None
        _encode_image(source, destination)
    else:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as handle:
            destination = Path(handle.name)
        destination, box = _encode_video(source, destination)
    if cache is not None:
        cache[key] = (destination, box)
    return destination, box


def _inline(mapping, stats, root, run, path, kind, cache):
    source = _resolve(root, run.get("archive_dir"), path)
    if source is None:
        stats["missing"] += 1
        return False
    try:
        encoded, box = _encode(root, run, path, kind, cache)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        stats["missing"] += 1
        print(f"  编码失败 {run.get('id', '')[:8]}/{path}: {error}", file=sys.stderr)
        return False
    mime = "image/jpeg" if kind == "image" else "video/webm"
    payload = base64.b64encode(encoded.read_bytes()).decode("ascii")
    mapping[f"{run.get('id')}/{path}"] = f"data:{mime};base64,{payload}"
    stats["bytes"] += encoded.stat().st_size
    stats["original_bytes"] += source.stat().st_size
    if box:
        stats["cropped"] += 1
    return True


def _error_class(message):
    """An error string can quote host paths; keep the class, drop the text."""
    if not message:
        return None
    text = str(message)
    for marker, label in (("429", "provider_rate_limited_or_out_of_credit"),
                          ("401", "provider_unauthorized"),
                          ("403", "provider_forbidden"),
                          ("404", "provider_not_found"),
                          ("unrecognized_model", "model_not_recognized"),
                          ("cooling down", "provider_cooldown"),
                          ("余额不足", "provider_out_of_credit"),
                          ("Interrupted", "operator_interrupted"),
                          ("timeout", "timeout"),
                          ("Connection", "connection_failure")):
        if marker in text:
            return label
    return "other"


def main():
    token = (ROOT / "data/dashboard-token").read_text().strip()
    opener = build_opener(HTTPCookieProcessor())
    login(opener, token)
    runs = fetch(opener, "/api/runs")
    runs = runs["runs"] if isinstance(runs, dict) and "runs" in runs else runs
    tasks = fetch(opener, "/api/tasks")
    tasks = tasks["tasks"] if isinstance(tasks, dict) and "tasks" in tasks else tasks
    data = {"runs": [sanitize(r) for r in runs], "tasks": tasks}
    # Resolved against the *unsanitized* runs: archive_dir is what locates the
    # bytes on disk, and it is deliberately absent from the snapshot.
    evidence, stats = collect_evidence(runs, ROOT, cache={})
    data["evidence"] = evidence

    leaked = [key for run in data["runs"] for key in ("archive_dir", "cli_command", "usage_raw")
              if key in run]
    if leaked:
        raise RuntimeError(f"snapshot must not carry private fields: {sorted(set(leaked))}")
    blob = json.dumps(data, ensure_ascii=False)
    for forbidden in (str(ROOT), "127.0.0.1", "/workspace", "runs/2026"):
        if forbidden in blob:
            raise RuntimeError(f"snapshot leaks a host detail: {forbidden}")

    destination = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "artifacts/observatory.html")
    html = (ROOT / "web/index.html").read_text()
    # Inline the stylesheet and script: a snapshot that still points at /app.js
    # only renders when served by the dashboard, so it cannot be opened or
    # handed over on its own. A self-contained file is the whole point.
    html = html.replace('<link rel="stylesheet" href="/styles.css">',
                        '<style>' + (ROOT / "web/styles.css").read_text() + '</style>')
    # An inline script runs during parsing, before the DOM it queries exists.
    # A <script defer> has no inline equivalent, so move the whole script to
    # the end of <body> instead of wrapping app.js in a closure or a
    # DOMContentLoaded handler, either of which would change its scope.
    html = re.sub(r'^[ \t]*<script src="/app\.js" defer></script>[ \t]*$', "", html, flags=re.M)
    html = html.replace("</body>", '<script>window.BENCH_SNAPSHOT=' + blob.replace("<", "\\u003c")
                        + ';</script><script>' + (ROOT / "web/app.js").read_text()
                        + '</script></body>')
    # Match the link/script tags specifically: model outputs legitimately
    # contain files named app.js and styles.css, and a substring check would
    # reject a valid snapshot over them.
    for pattern in (r'<link[^>]+href="/styles\.css"', r'<script[^>]+src="/app\.js"'):
        if re.search(pattern, html):
            raise RuntimeError(f"snapshot still references external asset: {pattern}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    # The destination may sit outside the project (a staging copy for a
    # publish step), so fall back to the absolute path rather than crashing
    # on a pretty relative one.
    try:
        shown = str(destination.relative_to(ROOT))
    except ValueError:
        shown = str(destination)
    print(json.dumps({"destination": shown,
                      "runs": len(data["runs"]),
                      "evidence": stats,
                      "bytes": destination.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        raise SystemExit(f"snapshot failed: HTTP {error.code} on {error.url}")
