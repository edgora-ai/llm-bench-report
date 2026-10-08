"""A per-run inference-only relay. Provider credentials never enter containers."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_egress(config_path, list_path):
    """Reproduce the existing OpenCode wrapper's selected proxy without shell eval."""
    choices = {}
    if Path(list_path).is_file():
        for line in Path(list_path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                parts = line.split("|")
                if len(parts) >= 2:
                    choices[parts[0].strip()] = parts[1].strip()
    selected = os.environ.get("OPENCODE_EGRESS")
    if not selected and Path(config_path).is_file():
        selected = next((line.strip() for line in Path(config_path).read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")), None)
    selected = selected or "us-la"
    proxy = choices.get(selected, selected)
    if proxy in {"direct", "off"}:
        return None
    if not urllib.parse.urlsplit(proxy).scheme:
        raise ValueError("Configured OpenCode egress selection has no proxy URL")
    return proxy


def connections(config):
    settings = json.loads(Path(config["paths"]["claude_settings"]).read_text())
    env = settings.get("env", {})
    base = os.environ.get("ANTHROPIC_BASE_URL") or env.get("ANTHROPIC_BASE_URL")
    secret = os.environ.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_AUTH_TOKEN")
    if not base or not secret:
        raise ValueError("Claude provider URL or authentication missing")
    if urllib.parse.urlsplit(base).scheme not in {"https", "http"}:
        raise ValueError("Invalid Claude provider URL")
    catalogue = json.loads(Path(config["paths"]["models_cache"]).read_text())
    zen = catalogue["opencode"]
    free_key = os.environ.get("OPENCODE_API_KEY", "public")
    auth_path = Path.home() / ".local/share/opencode/auth.json"
    if free_key == "public" and auth_path.is_file():
        auth = json.loads(auth_path.read_text()).get("opencode", {})
        if auth.get("type") == "api" and auth.get("key"):
            free_key = auth["key"]
    return {
        "claude-code": {"base": base.rstrip("/"), "headers": {"Authorization": "Bearer " + secret}, "secret": secret, "proxy": None},
        "opencode": {"base": zen["api"].rstrip("/"), "headers": {"Authorization": "Bearer " + free_key}, "secret": free_key, "proxy": resolve_egress(config["paths"]["opencode_egress_config"], config["paths"]["opencode_egress_list"])},
    }


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Gateway:
    def __init__(self, socket_path, provider, tool, model, log_path, api_model=None):
        self.socket_path = Path(socket_path)
        self.provider = provider
        self.tool = tool
        self.model = model.rsplit("/", 1)[-1]
        self.api_model = api_model or self.model
        self.log_path = Path(log_path)
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": provider["proxy"]} if provider.get("proxy") else {}), NoRedirect())
        gateway = self

        class Request(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                start = time.monotonic()
                record = {"timestamp": datetime.now(timezone.utc).isoformat(), "method": "POST", "path": self.path, "model": None,
                          "status_origin": "local_gateway", "policy_rejected": False}
                response = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 64 * 1024 * 1024:
                        self.fail(400, "Invalid inference payload")
                        record.update(status=400, reason_code="invalid_payload_length")
                        return
                    body = self.rfile.read(length)
                    data = json.loads(body)
                    record["model"] = data.get("model")
                    routes = {"/claude/v1/messages": "/v1/messages", "/claude/v1/messages/count_tokens": "/v1/messages/count_tokens"} if gateway.tool == "claude-code" else {"/opencode/v1/chat/completions": "/chat/completions", "/opencode/v1/responses": "/responses"}
                    parsed_path = urllib.parse.urlsplit(self.path)
                    query = urllib.parse.parse_qs(parsed_path.query)
                    allowed_query = not query or (gateway.tool == "claude-code" and query == {"beta": ["true"]})
                    reason = None
                    if parsed_path.path not in routes:
                        reason = "route_not_allowed"
                    elif not allowed_query:
                        reason = "query_not_allowed"
                    elif data.get("model") not in {gateway.model, gateway.api_model}:
                        reason = "model_not_allowed"
                    if reason:
                        record.update(status=403, status_origin="local_policy", policy_rejected=True, reason_code=reason)
                        self.fail(403, "Route or model outside frozen benchmark protocol")
                        return
                    for tool_def in data.get("tools", []):
                        kind = str(tool_def.get("type", ""))
                        name = str(tool_def.get("name", tool_def.get("function", {}).get("name", ""))).lower()
                        if kind.startswith(("web_search", "web_fetch", "image_generation")) or name in {"websearch", "webfetch", "web_search", "web_fetch", "task", "agent", "image_generation"}:
                            record.update(status=403, status_origin="local_policy", policy_rejected=True, reason_code="tool_not_allowed")
                            self.fail(403, "Disallowed provider tool")
                            return
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in {"authorization", "x-api-key", "host", "connection", "accept-encoding", "content-length"}}
                    headers.update(gateway.provider["headers"])
                    headers["Accept-Encoding"] = "identity"
                    req = urllib.request.Request(gateway.provider["base"] + routes[parsed_path.path] + ("?" + parsed_path.query if parsed_path.query else ""), data=body, headers=headers, method="POST")
                    try:
                        response = gateway.opener.open(req, timeout=60)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    record["status_origin"] = "upstream"
                    # A connection timeout is not a generation/iteration deadline.
                    try:
                        response.fp.raw._sock.settimeout(None)
                    except (AttributeError, OSError):
                        record["read_timeout_override"] = "unsupported"
                    record["status"] = response.status
                    record["request_id"] = response.headers.get("request-id") or response.headers.get("x-request-id")
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    while chunk := response.read1(65536):
                        if gateway.provider["secret"].encode() in chunk and len(gateway.provider["secret"]) > 8:
                            chunk = chunk.replace(gateway.provider["secret"].encode(), b"[REDACTED]")
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    record["error_type"] = type(exc).__name__
                    record["reason_code"] = ("invalid_payload" if isinstance(exc, ValueError) else
                                             "provider_connection_failed" if response is None else "provider_stream_failed")
                    record.setdefault("status", 502)
                    if response is None:
                        self.fail(502, "Provider connection failed: " + type(exc).__name__)
                finally:
                    if response:
                        response.close()
                    record["duration_ms"] = round((time.monotonic() - start) * 1000)
                    gateway.log(record)

            def fail(self, status, text):
                body = json.dumps({"error": {"message": text, "type": "benchmark_gateway_error"}}).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def log_message(self, *_args):
                return

        self.server = UnixServer(str(self.socket_path), Request)
        os.chmod(self.socket_path, 0o666)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def log(self, record):
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.socket_path.unlink(missing_ok=True)
