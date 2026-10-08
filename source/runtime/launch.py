"""Container-local HTTP bridge. The container has no external network or credentials."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(os.environ["BENCH_GATEWAY_SOCKET"])


class Bridge(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        connection = UnixHTTPConnection("gateway")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection", "accept-encoding"}}
            connection.request("POST", self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for k, v in response.getheaders():
                if k.lower() not in {"connection", "transfer-encoding", "content-length", "content-encoding"}:
                    self.send_header(k, v)
            self.end_headers()
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            print("provider bridge connection failed: " + type(exc).__name__, file=sys.stderr)
            self.close_connection = True
        finally:
            connection.close()

    def log_message(self, *_args):
        return


def main():
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((os.environ["BENCH_INTERNAL_HOST"], 0), Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://%s:%s" % server.server_address
    env = os.environ.copy()
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        env.pop(name, None)
    env.update({"NO_PROXY": "*", "no_proxy": "*"})
    if env["BENCH_TOOL"] == "claude-code":
        env.update({"ANTHROPIC_BASE_URL": base + "/claude", "ANTHROPIC_AUTH_TOKEN": "bench-local-bridge", "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    else:
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": env["BENCH_MODEL"],
            "autoupdate": False,
            "share": "disabled",
            "permission": {"*": "deny", "read": "allow", "edit": "allow", "write": "allow", "glob": "allow", "grep": "allow", "list": "allow", "bash": "allow", "task": "deny", "webfetch": "deny", "websearch": "deny", "codesearch": "deny", "external_directory": "deny", "skill": "deny", "question": "deny"},
            "tools": {"task": False, "webfetch": False, "websearch": False, "codesearch": False, "skill": False, "question": False},
            "mcp": {},
            "plugin": [],
            "provider": {"opencode": {"options": {"baseURL": base + "/opencode/v1", "apiKey": "bench-local-bridge"}}},
        }
        config_dir = home / "config"
        config_dir.mkdir(exist_ok=True)
        env.update({"XDG_CONFIG_HOME": str(config_dir), "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"), "XDG_STATE_HOME": str(home / "state"), "OPENCODE_CONFIG_DIR": str(config_dir / "opencode"), "OPENCODE_CONFIG_CONTENT": json.dumps(config), "OPENCODE_API_KEY": "bench-local-bridge", "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_CLAUDE_CODE": "true", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true", "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true", "OPENCODE_MODELS_PATH": "/catalog/models.json"})
    try:
        return subprocess.call(sys.argv[1:], env=env)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
