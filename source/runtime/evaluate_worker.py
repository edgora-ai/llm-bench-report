"""Executed inside a credential-free, network-none container after generation."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import sync_playwright


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
        super().end_headers()

    def log_message(self, *_args):
        return


def capture(context, page, path, result):
    """Screenshot through CDP.

    Page.screenshot waits for a stable frame and never returns on a page that
    repaints continuously (measured: a WebGL page that renders at ~19ms/frame
    still timed out at both 30s and 120s). CDP capture returns, so it is the
    capture path of record; the fallback exists only for CDP being unavailable.
    """
    started = time.monotonic()
    try:
        data = context.new_cdp_session(page).send("Page.captureScreenshot", {"format": "png"})["data"]
        import base64
        Path(path).write_bytes(base64.b64decode(data))
        result["captures"].append({"path": Path(path).name, "method": "cdp", "ms": round((time.monotonic() - started) * 1000)})
        return True
    except Exception as exc:
        result["captures"].append({"path": Path(path).name, "method": "playwright", "ms": round((time.monotonic() - started) * 1000), "cdp_error": str(exc)[:200]})
        try:
            page.screenshot(path=path, timeout=15000)
            return True
        except Exception as fallback_exc:
            result["captures"][-1]["error"] = str(fallback_exc)[:200]
            return False


def evaluate():
    evidence = Path("/evidence")
    evidence.mkdir(exist_ok=True)
    result = {"status": "completed", "checks": [], "evidence": [], "errors": [], "blocked_requests": [], "security_policy_violations": [], "console": [], "captures": [], "renderer": "Chromium headless / software WebGL", "sample_seconds": 8}
    if not Path("/workspace/output/index.html").is_file():
        result["checks"].append({"name": "entrypoint", "status": "fail", "detail": "index.html missing"})
        return result
    server = ThreadingHTTPServer((os.environ["BENCH_INTERNAL_HOST"], 0), partial(Handler, directory="/workspace/output"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = "http://%s:%s" % server.server_address
    result["checks"].append({"name": "entrypoint", "status": "pass", "detail": "index.html exists"})
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        result["browser_version"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(evidence / "video"), record_video_size={"width": 1440, "height": 900})

        def route(request_route):
            url = request_route.request.url
            if url.startswith(origin + "/") or url.startswith(("data:", "blob:")):
                request_route.continue_()
            else:
                result["blocked_requests"].append(url[:500])
                request_route.abort()

        def security_policy_violation(_source, violation):
            result["security_policy_violations"].append(violation)
            blocked = urlsplit(violation["blocked_uri"])
            local = urlsplit(origin)
            if blocked.scheme in {"http", "https", "ws", "wss"} and (blocked.scheme, blocked.netloc) != (local.scheme, local.netloc):
                result["blocked_requests"].append(violation["blocked_uri"][:500])

        def monitor_requests(browser_context):
            browser_context.route("**/*", route)
            # CSP can reject dependencies before Playwright's routing sees them.
            browser_context.expose_binding("__bench_security_policy_violation", security_policy_violation)
            browser_context.add_init_script("""
                addEventListener('securitypolicyviolation', event => {
                    window.__bench_security_policy_violation({
                        blocked_uri: event.blockedURI,
                        effective_directive: event.effectiveDirective,
                        document_uri: event.documentURI,
                        disposition: event.disposition
                    });
                });
            """)

        monitor_requests(context)
        page = context.new_page()
        page.on("pageerror", lambda err: result["errors"].append(str(err)[:2000]))
        page.on("console", lambda msg: result["console"].append({"type": msg.type, "text": msg.text[:2000]}))
        response = page.goto(origin + "/index.html", wait_until="load", timeout=30000)
        page.wait_for_timeout(1000)
        if not capture(context, page, evidence / "desktop.png", result):
            raise RuntimeError("Desktop capture failed: " + json.dumps(result["captures"][-1], ensure_ascii=False))
        result["evidence"].append("evidence/desktop.png")
        page.wait_for_timeout(2000)
        if not capture(context, page, evidence / "frame-2.png", result):
            raise RuntimeError("Frame capture failed: " + json.dumps(result["captures"][-1], ensure_ascii=False))
        page.wait_for_timeout(5000)
        if not capture(context, page, evidence / "frame-8.png", result):
            raise RuntimeError("Frame capture failed: " + json.dumps(result["captures"][-1], ensure_ascii=False))
        result["evidence"] += ["evidence/frame-2.png", "evidence/frame-8.png"]
        before = Image.open(evidence / "desktop.png").convert("RGB")
        after = Image.open(evidence / "frame-8.png").convert("RGB")
        diff = ImageChops.difference(before, after).convert("L")
        changed = sum(n for value, n in enumerate(diff.histogram()) if value > 10) / (before.width * before.height)
        result["frame_changed_fraction"] = changed
        result["checks"].append({"name": "frame_change", "status": "pass" if changed > 0.0001 else "fail", "detail": f"Changed pixel fraction over sample: {changed:.6f}; not a quality score"})
        result["checks"].append({"name": "load", "status": "pass" if response and response.ok else "fail", "detail": f"HTTP {response.status if response else 'unknown'}"})
        svg_count = page.locator("svg").count()
        if os.environ.get("BENCH_TASK") == "crocodile":
            result["checks"].append({"name": "svg", "status": "pass" if svg_count else "fail", "detail": f"Inline SVG elements: {svg_count}; scene recognition requires visual review"})
        result["canvas_count"] = page.locator("canvas").count()
        result["controls"] = page.locator("button,input,select").count()
        video = page.video
        context.close()
        if video:
            shutil.copyfile(video.path(), evidence / "animation.webm")
            result["evidence"].append("evidence/animation.webm")
        mobile = browser.new_context(viewport={"width": 400, "height": 800})
        monitor_requests(mobile)
        mpage = mobile.new_page()
        mpage.on("pageerror", lambda err: result["errors"].append(str(err)[:2000]))
        mpage.goto(origin + "/index.html", wait_until="load", timeout=30000)
        mpage.wait_for_timeout(1000)
        capture(mobile, mpage, evidence / "mobile.png", result)
        overflow = mpage.evaluate("Math.max(document.body.scrollWidth,document.documentElement.scrollWidth)>innerWidth+2")
        result["evidence"].append("evidence/mobile.png")
        result["checks"].append({"name": "mobile_overflow", "status": "fail" if overflow else "pass", "detail": "Viewport 400px"})
        mobile.close()
        browser.close()
    server.shutdown()
    server.server_close()
    result["checks"].append({"name": "javascript", "status": "fail" if result["errors"] else "pass", "detail": json.dumps(result["errors"], ensure_ascii=False)})
    result["checks"].append({"name": "external_requests", "status": "fail" if result["blocked_requests"] else "pass", "detail": json.dumps(result["blocked_requests"], ensure_ascii=False)})
    return result


if __name__ == "__main__":
    start = time.monotonic()
    try:
        report = evaluate()
    except Exception as exc:
        report = {"status": "infrastructure_error", "error": f"{type(exc).__name__}: {exc}", "checks": [], "evidence": []}
    report["duration_ms"] = round((time.monotonic() - start) * 1000)
    Path("/evidence/evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"evaluation_status": report["status"], "checks": report["checks"]}, ensure_ascii=False))
