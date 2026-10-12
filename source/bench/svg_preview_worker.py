"""Container-only standalone SVG validation, with one still image and no video."""
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

from playwright.sync_api import sync_playwright


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'")
        super().end_headers()

    def log_message(self, *_args):
        return


def evaluate():
    output = Path("/workspace/output")
    evidence = Path("/evidence")
    entry = os.environ["BENCH_SVG_ENTRYPOINT"]
    result = {"status": "completed", "delivery_format": "standalone-svg-v1",
              "entrypoint": entry, "recording": False, "evidence": [], "checks": []}
    if Path(entry).name != entry or Path(entry).suffix.lower() != ".svg":
        raise ValueError("Invalid SVG entrypoint")
    raw = (output / entry).read_bytes()
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("SVG document declarations are not supported")
    root = ET.fromstring(raw)
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError("The delivered file is not an SVG document")
    if any(node.tag.rsplit("}", 1)[-1].lower() == "script" for node in root.iter()):
        raise ValueError("This declarative SVG protocol does not allow scripts")
    result["checks"].append({"name": "entrypoint", "status": "pass", "detail": "Standalone SVG delivered; no HTML entry required"})
    server = ThreadingHTTPServer((os.environ["BENCH_INTERNAL_HOST"], 0), partial(Handler, directory=str(output)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = "http://%s:%s" % server.server_address
    external = []
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                def route(request):
                    url = request.request.url
                    if urlsplit(url).scheme in {"data", "blob"} or url.startswith(origin + "/"):
                        request.continue_()
                    else:
                        external.append(url)
                        request.abort()
                page.route("**/*", route)
                response = page.goto(origin + "/" + quote(entry), wait_until="load", timeout=30000)
                page.wait_for_timeout(250)
                visible = page.evaluate("""() => {
                    const svg = document.documentElement;
                    const box = svg.getBoundingClientRect();
                    return svg.namespaceURI === 'http://www.w3.org/2000/svg' && svg.localName === 'svg'
                        && box.width > 0 && box.height > 0;
                }""")
                result["checks"].extend([
                    {"name": "load", "status": "pass" if response and response.status == 200 and visible else "fail", "detail": "SVG document load and nonempty viewport"},
                    {"name": "external_requests", "status": "fail" if external else "pass", "detail": "Blocked external request count: " + str(len(external))},
                    {"name": "javascript_errors", "status": "fail" if errors else "pass", "detail": "Page error count: " + str(len(errors))},
                ])
                if visible:
                    session = context.new_cdp_session(page)
                    capture = session.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
                    (evidence / "desktop.png").write_bytes(base64.b64decode(capture["data"], validate=True))
                    session.detach()
                    result["evidence"].append("evidence/desktop.png")
                context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    return result


def main():
    evidence = Path("/evidence")
    evidence.mkdir(exist_ok=True)
    try:
        result = evaluate()
    except Exception as exc:
        result = {"status": "infrastructure_error", "recording": False,
                  "error": type(exc).__name__ + ": " + str(exc), "checks": [], "evidence": []}
    (evidence / "evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
