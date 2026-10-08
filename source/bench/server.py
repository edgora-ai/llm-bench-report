"""Authenticated, same-origin standard-library HTTP API for the benchmark index."""
from __future__ import annotations

import csv
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .storage import METRICS, RunConflict, Store, safe_directory, safe_open, safe_parts, summary, validate_id

SESSION_SECONDS = 8 * 60 * 60
MAX_BODY = 1024 * 1024
COOKIE_NAME = "bench_session"
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
LOGGER = logging.getLogger(__name__)


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class BenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: Store, token: str):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A nonempty authentication token is required")
        self.store = store
        self.project_root = store.project_root
        self._token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        self._sessions: dict[str, float] = {}
        self._session_lock = threading.Lock()
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def valid_token(self, candidate: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(candidate.encode("utf-8")).digest(), self._token_digest)

    def new_session(self) -> str:
        session = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._session_lock:
            self._sessions = {key: expiry for key, expiry in self._sessions.items() if expiry > now}
            self._sessions[session] = now + SESSION_SECONDS
        return session

    def valid_session(self, session: str) -> bool:
        with self._session_lock:
            return self._sessions.get(session, 0) > time.monotonic()

    def delete_session(self, session: str) -> None:
        with self._session_lock:
            self._sessions.pop(session, None)

    def handle_error(self, request, client_address):
        # Never log headers, credentials, URL queries, or arbitrary exception strings.
        LOGGER.error("HTTP connection failed")


class Handler(BaseHTTPRequestHandler):
    server: BenchHTTPServer
    server_version = "BenchHTTP"
    sys_version = ""

    def log_message(self, format, *args):
        # BaseHTTPRequestHandler logs raw request lines (possibly containing secrets).
        LOGGER.debug("HTTP request processed")

    def _headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _bytes(self, status, content_type, data: bytes, extra=None):
        self._headers(status, content_type, len(data), extra)
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, value, extra=None):
        self._bytes(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), extra)

    def _cookie_session(self) -> str:
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""
        except CookieError:
            return ""

    def _header_authenticated(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and self.server.valid_token(auth[7:]):
            return True
        key = self.headers.get("x-api-key")
        return key is not None and self.server.valid_token(key)

    def _authenticate(self):
        if not self._header_authenticated() and not self.server.valid_session(self._cookie_session()):
            raise HTTPError(401, "Authentication required")

    def _same_origin(self, allow_cli=False):
        origin = self.headers.get("Origin")
        if origin is None and allow_cli and self._header_authenticated():
            return
        # No forwarding headers are trusted: an HTTP server requires an HTTP origin.
        host = self.headers.get("Host", "")
        if not host or any(char in host for char in "/\\@\r\n"):
            raise HTTPError(403, "Same-origin request required")
        try:
            parsed = urlsplit(origin or "")
            expected = urlsplit("http://" + host)
            if parsed.scheme != expected.scheme or parsed.hostname != expected.hostname or (parsed.port or 80) != (expected.port or 80) or parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
                raise HTTPError(403, "Same-origin request required")
        except ValueError:
            raise HTTPError(403, "Same-origin request required") from None
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise HTTPError(403, "Same-origin request required")

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPError(415, "JSON Content-Type required")
        if self.headers.get("Transfer-Encoding") is not None:
            raise HTTPError(400, "Transfer-Encoding is unsupported")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise HTTPError(400, "Invalid Content-Length") from None
        if length < 0:
            raise HTTPError(411, "Content-Length required")
        if length > MAX_BODY:
            raise HTTPError(413, "Request body too large")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
        except (ValueError, UnicodeError):
            raise HTTPError(400, "Invalid JSON") from None
        if not isinstance(value, dict):
            raise HTTPError(400, "JSON object required")
        return value

    def _query(self, query: str) -> dict[str, str]:
        try:
            parsed = parse_qs(query, keep_blank_values=True, max_num_fields=50)
        except ValueError:
            raise HTTPError(400, "Invalid query") from None
        if any(len(values) != 1 for values in parsed.values()):
            raise HTTPError(400, "Duplicate query parameters")
        return {key: values[0] for key, values in parsed.items()}

    def _options(self):
        runs = self.server.store.list_runs()
        tasks = {}
        for run in runs:
            tasks[run["task_id"]] = run["task_name"]
        return {
            "tools": sorted({run["tool"] for run in runs}),
            "models": sorted({run["model"] for run in runs}),
            "tasks": [{"id": key, "name": tasks[key]} for key in sorted(tasks)],
            "prompt_versions": sorted({run["prompt_version"] for run in runs}),
            "dates": sorted({run["date"] for run in runs}),
            "purposes": sorted({run["purpose"] for run in runs}),
            "statuses": sorted({run["status"] for run in runs}),
        }

    def _tasks(self):
        tasks = []
        try:
            with safe_directory(self.server.project_root, "bench/tasks") as directory:
                names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return tasks
        for name in names:
            if name.endswith(".json"):
                with safe_open(self.server.project_root, "bench/tasks/" + name) as stream:
                    tasks.append(json.load(stream))
        return tasks

    def _export(self, query):
        runs = self.server.store.list_runs(query)
        format_name = query.get("format", "json")
        if format_name == "json":
            self._json(200, {"runs": runs, "summary": summary(runs)}, {"Content-Disposition": 'attachment; filename="runs.json"'})
        elif format_name == "csv":
            fields = ("id", "batch_id", "date", "purpose", "tool", "model", "task_id", "task_name", "prompt_version", "status", "started_at", "finished_at", "duration_ms") + METRICS + ("cost_source",)
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for run in runs:
                row = {key: run.get(key) for key in fields}
                for metric in METRICS + ("cost_source",):
                    row[metric] = run.get("metrics", {}).get(metric)
                # CSV formulas must not execute when a run's arbitrary text is opened.
                for key, value in row.items():
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                        row[key] = "'" + value
                writer.writerow(row)
            self._bytes(200, "text/csv; charset=utf-8", output.getvalue().encode("utf-8"), {"Content-Disposition": 'attachment; filename="runs.csv"'})
        else:
            raise HTTPError(400, "format must be json or csv")

    def _file(self, relative: str, *, attachment=False, allow_range=False):
        safe_parts(relative)
        with safe_open(self.server.project_root, relative) as stream:
            size = os.fstat(stream.fileno()).st_size
            start, end, status = 0, size - 1, 200
            extra = {}
            if allow_range:
                extra["Accept-Ranges"] = "bytes"
                range_header = self.headers.get("Range")
                if range_header:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                    if not match or not any(match.groups()) or size == 0:
                        self._json(416, {"error": "Invalid range"}, {"Content-Range": f"bytes */{size}"})
                        return
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    else:
                        suffix = int(last)
                        start = max(0, size - suffix)
                    if start > end or start >= size or (not first and int(last) == 0):
                        self._json(416, {"error": "Unsatisfiable range"}, {"Content-Range": f"bytes */{size}"})
                        return
                    status = 206
                    extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
            if attachment:
                # A constant safe basename avoids header injection via artifact filenames.
                suffix = Path(relative).suffix.lower()
                suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else ".bin"
                extra["Content-Disposition"] = f'attachment; filename="artifact{suffix}"'
                if suffix in (".html", ".htm", ".svg", ".xml"):
                    content_type = "application/octet-stream"
            length = max(0, end - start + 1)
            self._headers(status, content_type, length, extra)
            if self.command == "HEAD":
                return
            stream.seek(start)
            while length:
                chunk = stream.read(min(length, 64 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def _get(self):
        url = urlsplit(self.path)
        path = unquote(url.path)
        query = self._query(url.query)
        if path == "/api/health":
            self._json(200, {"status": "ok"})
            return
        if path in ("/", "/app.js", "/styles.css"):
            name = "index.html" if path == "/" else path[1:]
            self._file("web/" + name)
            return
        self._authenticate()
        if path == "/api/runs":
            runs = self.server.store.list_runs(query)
            self._json(200, {"runs": runs, "summary": summary(runs)})
        elif path == "/api/options":
            self._json(200, self._options())
        elif path == "/api/tasks":
            self._json(200, {"tasks": self._tasks()})
        elif path == "/api/export":
            self._export(query)
        else:
            match = re.fullmatch(r"/api/runs/([^/]+)(/file)?", path)
            if not match:
                raise HTTPError(404, "Not found")
            run_id = validate_id(match[1])
            run = self.server.store.get_run(run_id)
            if run is None:
                raise HTTPError(404, "Run not found")
            if match[2]:
                artifact_path = query.get("path", "")
                parts = safe_parts(artifact_path)
                if parts[0] == "reviews" or artifact_path not in {artifact["path"] for artifact in run.get("artifacts", [])}:
                    raise HTTPError(404, "Artifact not found")
                suffix = Path(artifact_path).suffix.lower()
                self._file(run["archive_dir"] + "/" + artifact_path, attachment=suffix not in (".png", ".webm"), allow_range=True)
            else:
                self._json(200, {"run": run})

    def _post(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/api/login":
            self._same_origin()
            body = self._body()
            candidate = body.get("token")
            if not isinstance(candidate, str) or not self.server.valid_token(candidate):
                raise HTTPError(401, "Invalid credentials")
            session = self.server.new_session()
            self._json(200, {"authenticated": True}, {"Set-Cookie": f"{COOKIE_NAME}={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"})
            return
        self._authenticate()
        self._same_origin(allow_cli=path != "/api/logout")
        body = self._body()
        if path == "/api/logout":
            self.server.delete_session(self._cookie_session())
            self._json(200, {"authenticated": False}, {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
            return
        match = re.fullmatch(r"/api/runs/([^/]+)/reviews", path)
        if not match:
            raise HTTPError(404, "Not found")
        review = self.server.store.add_review(validate_id(match[1]), body)
        self._json(201, {"review": review})

    def _dispatch(self, action):
        try:
            action()
        except HTTPError as error:
            self._json(error.status, {"error": error.message})
        except RunConflict:
            self._json(409, {"error": "Run conflict"})
        except (FileNotFoundError, KeyError, NotADirectoryError):
            self._json(404, {"error": "Not found"})
        except PermissionError:
            self._json(403, {"error": "Access denied"})
        except (ValueError, TypeError, UnicodeError):
            self._json(400, {"error": "Invalid request"})
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.debug("HTTP client disconnected")
        except OSError:
            self._json(403, {"error": "File access denied"})
        except Exception:
            LOGGER.error("HTTP operation failed")
            self._json(500, {"error": "Internal server error"})

    def do_GET(self):
        self._dispatch(self._get)

    def do_HEAD(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "Unsupported request"})


def make_server(project_root: Path, db_path: Path, host: str, port: int, token: str) -> BenchHTTPServer:
    """Nonblocking constructor for embedding/testing; caller owns server_close()."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("A nonempty authentication token is required")
    return BenchHTTPServer((host, port), Store(db_path, project_root), token)


def serve(project_root: Path, db_path: Path, host: str, port: int, token: str) -> None:
    """Block until interrupted, then close the listener. No insecure defaults."""
    server = make_server(project_root, db_path, host, port, token)
    try:
        server.serve_forever()
    finally:
        server.server_close()

