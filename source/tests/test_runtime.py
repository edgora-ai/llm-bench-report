from contextlib import ExitStack
import http.client
import os
import io
import sqlite3
import subprocess
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from bench.egress import Gateway
from bench.runner import artifact_index, classify_error, digest, source_metrics, connection_fingerprint, policy_hashes, archive_policy


class SocketConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("gateway")
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.path))


FIXTURE_MODELS = ("gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol")
FIXTURE_SECRET = "fixture-private-key-never-real"
UPSTREAM_FORBIDDEN = b'{"error":{"type":"permission_error","message":"fixture upstream forbidden"}}'


class GatewayHTTPFixture:
    """Real local upstream and Unix gateway, optionally with the runtime bridge."""

    def __init__(self, model, *, bridge=True):
        self.model = model
        self.with_bridge = bridge
        self.observed = []
        self.stack = ExitStack()

    def __enter__(self):
        try:
            self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="gateway-fixture-")))
            fixture = self

            class Upstream(BaseHTTPRequestHandler):
                def do_POST(self):
                    data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    fixture.observed.append({"path": self.path, "body": data,
                                             "auth": self.headers.get("Authorization"),
                                             "api_key": self.headers.get("x-api-key")})
                    if data.get("fixture_error"):
                        status, content_type, body = 403, "application/json", UPSTREAM_FORBIDDEN
                    elif self.path.startswith("/v1/messages/count_tokens"):
                        status, content_type, body = 200, "application/json", b'{"input_tokens":17}'
                    else:
                        status, content_type = 200, "text/event-stream"
                        body = ('data: {"fixture":1,"redact":"' + FIXTURE_SECRET + '"}\n\ndata: [DONE]\n\n').encode()
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args):
                    return

            self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            self._serve(self.upstream)
            self.log_path = self.root / "gateway.jsonl"
            self.socket_path = self.root / "gateway.sock"
            provider = {"base": "http://%s:%s" % self.upstream.server_address,
                        "headers": {"Authorization": "Bearer " + FIXTURE_SECRET},
                        "secret": FIXTURE_SECRET, "proxy": None}
            self.gateway = Gateway(self.socket_path, provider, "claude-code", self.model, self.log_path)
            self.gateway.__enter__()

            def close_gateway():
                self.gateway.__exit__(None, None, None)
                self.gateway.thread.join(5)
                if self.gateway.thread.is_alive():
                    raise RuntimeError("Fixture gateway thread did not stop")

            self.stack.callback(close_gateway)
            if self.with_bridge:
                from runtime.launch import Bridge
                self.stack.enter_context(mock.patch.dict(os.environ, {"BENCH_GATEWAY_SOCKET": str(self.socket_path)}))
                self.bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
                self._serve(self.bridge)
            return self
        except BaseException:
            self.stack.close()
            raise

    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(5)
            if thread.is_alive():
                raise RuntimeError("Fixture HTTP server did not stop")

        self.stack.callback(close)

    def request(self, path, body):
        connection = http.client.HTTPConnection(*self.bridge.server_address, timeout=10)
        try:
            connection.request("POST", path, json.dumps(body),
                               {"Content-Type": "application/json", "Authorization": "Bearer dummy-container-token",
                                "x-api-key": "dummy-container-api-key"})
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def __exit__(self, *_args):
        self.stack.close()

    def records(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]


class ProviderRouteTests(unittest.TestCase):
    def test_origin_and_legacy_proof_matrix(self):
        from bench import runner
        cases = [
            ([], False, "unknown", (0, 0, 0, 0)),
            ([{"status": 200, "status_origin": "upstream"}], False, "pass", (0, 0, 0, 0)),
            ([{"status": 403, "status_origin": "upstream"}], False, "pass", (0, 1, 1, 0)),
            ([{"status": 429, "status_origin": "upstream"}, {"status": 500, "status_origin": "upstream"}], False, "pass", (0, 2, 0, 0)),
            ([{"status": 403}], False, "unknown", (0, 0, 0, 1)),
            ([{"status": 403, "request_id": None}], False, "unknown", (0, 0, 0, 1)),
            ([{"status": 403, "request_id": None}], True, "pass", (0, 1, 1, 0)),
            ([{"status": 403, "read_timeout_override": "unsupported"}], False, "unknown", (0, 0, 0, 1)),
            ([{"status": 403, "read_timeout_override": "unsupported"}], True, "pass", (0, 1, 1, 0)),
            ([{"status": 403, "policy_rejected": True}], False, "unknown", (0, 0, 0, 1)),
            ([{"status": 403, "status_origin": "upstream", "policy_rejected": True}], True, "pass", (0, 1, 1, 0)),
            ([{"status": 403, "status_origin": "local_gateway", "request_id": None}], True, "pass", (0, 0, 0, 0)),
            ([{"status": 502, "status_origin": "local_gateway"}], False, "pass", (0, 0, 0, 0)),
            ([{"status": 403, "status_origin": "local_policy", "request_id": "legacy-marker"}], True, "unknown", None),
            ([{"status": 200, "status_origin": "local_policy", "policy_rejected": True}], False, "unknown", None),
            ([{"status": 403, "status_origin": "local_policy", "policy_rejected": False}], False, "unknown", None),
            ([{"status": 403, "status_origin": "local_policy", "policy_rejected": 1}], False, "unknown", None),
            ([{"status": 403, "status_origin": "unrecognized", "request_id": None}], True, "unknown", (0, 0, 0, 1)),
            ([{"status": 403, "status_origin": "upstream"}, {"status": 403}], False, "unknown", (0, 1, 1, 1)),
        ]
        for events, legacy, expected, counts in cases:
            with self.subTest(events=events, legacy=legacy):
                check = runner.provider_route_check(events, legacy_upstream_markers=legacy)
                self.assertEqual(check["name"], "provider_route")
                self.assertEqual(check["status"], expected)
                self.assertIsInstance(check["detail"], str)
                if counts is not None:
                    for label, count in zip(("restricted route rejections", "upstream HTTP errors", "upstream 403", "unknown origin 403"), counts):
                        self.assertIn(f"{label}: {count}", check["detail"])

    def test_policy_rejection_reasons_and_mixed_events(self):
        from bench import runner
        for reason in ("route_not_allowed", "query_not_allowed", "model_not_allowed", "tool_not_allowed"):
            with self.subTest(reason=reason):
                policy = {"status": 403, "status_origin": "local_policy", "policy_rejected": True,
                          "reason_code": reason, "request_id": None}
                events = [policy, {"status": 403, "status_origin": "upstream"}, {"status": 403}]
                check = runner.provider_route_check(events, legacy_upstream_markers=True)
                self.assertEqual(check["status"], "fail")
                for text in ("restricted route rejections: 1", "upstream HTTP errors: 1", "upstream 403: 1", "unknown origin 403: 1"):
                    self.assertIn(text, check["detail"])


class GatewayBridgeTests(unittest.TestCase):
    def test_four_models_count_tokens_upstream_errors_stream_and_policy(self):
        from bench import runner
        for model in FIXTURE_MODELS:
            with self.subTest(model=model), GatewayHTTPFixture(model) as fixture:
                payload = {"model": model, "messages": [{"role": "user", "content": "fixture"}]}
                for path in ("/claude/v1/messages/count_tokens", "/claude/v1/messages/count_tokens?beta=true"):
                    status, content_type, body = fixture.request(path, payload)
                    self.assertEqual(status, 200)
                    self.assertEqual(content_type, "application/json")
                    self.assertEqual(json.loads(body), {"input_tokens": 17})
                status, content_type, body = fixture.request("/claude/v1/messages?beta=true", payload)
                self.assertEqual((status, content_type), (200, "text/event-stream"))
                self.assertIn(b"data: [DONE]", body)
                self.assertIn(b"[REDACTED]", body)
                self.assertNotIn(FIXTURE_SECRET.encode(), body)
                status, content_type, body = fixture.request("/claude/v1/messages/count_tokens?beta=true", {**payload, "fixture_error": True})
                self.assertEqual((status, content_type, body), (403, "application/json", UPSTREAM_FORBIDDEN))
                denied = [
                    ("/claude/v1/messages", {**payload, "model": "other-model"}, "model_not_allowed"),
                    ("/outside", payload, "route_not_allowed"),
                    ("/claude/v1/messages/count_tokens?unexpected=true", payload, "query_not_allowed"),
                    ("/claude/v1/messages", {**payload, "tools": [{"type": "web_search_20250305"}]}, "tool_not_allowed"),
                    ("/claude/v1/messages", {**payload, "tools": [{"name": "Task"}]}, "tool_not_allowed"),
                ]
                for path, data, reason in denied:
                    with self.subTest(path=path, reason=reason):
                        hits = len(fixture.observed)
                        status, _, body = fixture.request(path, data)
                        self.assertEqual(status, 403)
                        self.assertEqual(json.loads(body)["error"]["type"], "benchmark_gateway_error")
                        self.assertEqual(len(fixture.observed), hits)
                # Shutdown drains request threads before inspecting the final JSONL.
                fixture.bridge.shutdown()
                fixture.gateway.server.shutdown()
                records = fixture.records()
                self.assertEqual(len(fixture.observed), 4)
                self.assertEqual(len(records), 9)
                self.assertEqual([observation["path"] for observation in fixture.observed], [
                    "/v1/messages/count_tokens", "/v1/messages/count_tokens?beta=true",
                    "/v1/messages?beta=true", "/v1/messages/count_tokens?beta=true"])
                self.assertEqual([observation["body"] for observation in fixture.observed],
                                 [payload, payload, payload, {**payload, "fixture_error": True}])
                for observation in fixture.observed:
                    self.assertEqual(observation["auth"], "Bearer " + FIXTURE_SECRET)
                    self.assertIsNone(observation["api_key"])
                    self.assertEqual(observation["body"]["model"], model)
                for record in records[:4]:
                    self.assertEqual(record["status_origin"], "upstream")
                    self.assertIsNot(record.get("policy_rejected"), True)
                for record, (_, _, reason) in zip(records[4:], denied):
                    self.assertEqual(record["status_origin"], "local_policy")
                    self.assertIs(record["policy_rejected"], True)
                    self.assertEqual(record["reason_code"], reason)
                self.assertNotIn(FIXTURE_SECRET, fixture.log_path.read_text())
                self.assertNotIn("dummy-container-token", fixture.log_path.read_text())
                check = runner.provider_route_check(records[:4])
                self.assertEqual(check["status"], "pass")
                self.assertIn("upstream 403: 1", check["detail"])

    def test_gateway_invalid_payload_and_connection_failure_have_local_origin(self):
        with GatewayHTTPFixture(FIXTURE_MODELS[0]) as fixture:
            connection = http.client.HTTPConnection(*fixture.bridge.server_address, timeout=10)
            try:
                connection.request("POST", "/claude/v1/messages", b"")
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                connection.close()
            fixture.upstream.shutdown()
            fixture.upstream.server_close()
            status, _, _ = fixture.request("/claude/v1/messages", {"model": fixture.model})
            self.assertEqual(status, 502)
            fixture.bridge.shutdown()
            fixture.gateway.server.shutdown()
            for record in fixture.records():
                self.assertEqual(record["status_origin"], "local_gateway")
                self.assertIsNot(record.get("policy_rejected"), True)


class RuntimeTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_sensitive(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))
        self.assertNotEqual(digest({"version": 1}), digest({"version": 2}))

    def test_connection_identity_excludes_credentials(self):
        one = {"base": "https://first:secret@fixture.invalid/v1?key=hidden", "secret": "hidden", "proxy": "http://user:secret@proxy.invalid:8080"}
        two = {"base": "https://other:changed@fixture.invalid/v1?key=changed", "secret": "changed", "proxy": "http://other:changed@proxy.invalid:8080"}
        self.assertEqual(connection_fingerprint(one), connection_fingerprint(two))
        self.assertNotEqual(connection_fingerprint(one), connection_fingerprint({**one, "base": "https://other-provider.invalid/v1"}))

    def test_policy_hashes_detect_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bench").mkdir()
            (root / "runtime").mkdir()
            for path in ["bench/runner.py", "runtime/launch.py", "runtime/evaluate_worker.py"]:
                (root / path).write_text("fixture original")
            frozen = policy_hashes(root)
            self.assertEqual(frozen, policy_hashes(root))
            archived = root / "archive"
            archive_policy(root, archived, frozen)
            for relative in frozen:
                self.assertEqual((root / relative).read_bytes(), (archived / relative).read_bytes())
            (root / "runtime/launch.py").write_text("fixture changed")
            self.assertNotEqual(frozen, policy_hashes(root))
            with self.assertRaises(RuntimeError):
                archive_policy(root, root / "rejected", frozen)

    def test_artifact_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "output").mkdir()
            (root / "output/index.html").write_text("verification")
            files = artifact_index(root)
            self.assertEqual(files[0]["path"], "output/index.html")
            (root / "output/escape").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                artifact_index(root)

    def test_source_metrics_count_only_actual_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "output").mkdir()
            (root / "evidence").mkdir()
            (root / "output/index.html").write_bytes("鳄鱼".encode("utf-8"))
            (root / "output/scene.SVG").write_bytes(b"<svg/>")
            (root / "output/self-test.png").write_bytes(b"excluded")
            (root / "evidence/animation.webm").write_bytes(b"excluded")
            (root / "final.txt").write_bytes(b"excluded")
            self.assertEqual(source_metrics(artifact_index(root)), {"source_bytes": 12, "source_file_count": 2})
            self.assertEqual(source_metrics([]), {"source_bytes": 0, "source_file_count": 0})

    def test_stream_disconnect_is_a_transport_failure_not_a_model_failure(self):
        # A dropped stream is the connection failing. Reporting it as a plain
        # session error would charge the model for a broken socket and would
        # not mark the run for the one infrastructure retry.
        from bench.runner import classify_error
        for message in [
            "API Error: stream error: stream disconnected before completion: "
            "stream closed before response.completed",
            "read ECONNRESET",
            "socket hang up",
            "Premature close",
        ]:
            self.assertEqual(classify_error(message), ("failed", "transport_interrupted"), message)
        # Genuine model-side failures must not become retryable.
        for message in ["Request rejected (429)", "rate limit exceeded",
                        "model not found", "The model produced no output"]:
            self.assertNotEqual(classify_error(message)[1], "transport_interrupted", message)

    def test_error_classes(self):
        from bench.runner import classify_error
        self.assertEqual(classify_error("ModelNotFound"), ("unavailable", "model_unavailable"))
        self.assertEqual(classify_error("Endpoint is unavailable."), ("unavailable", "provider_unavailable"))
        self.assertEqual(classify_error("429 Rate Limit")[1], "rate_limit")
        self.assertEqual(classify_error("401 Unauthorized")[1], "authentication_or_permission")

    def test_gateway_stream_credentials_and_policy(self):
        observed = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                observed.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"fixture":1}\n\ndata: [DONE]\n\n')

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                provider = {"base": "http://%s:%s" % server.server_address, "headers": {"Authorization": "Bearer fixture-private-key"}, "secret": "fixture-private-key", "proxy": None}
                with Gateway(root / "gateway.sock", provider, "claude-code", "fixture-model", root / "log.jsonl"):
                    for path, body, status in [("/claude/v1/messages?beta=true", {"model": "fixture-model"}, 200), ("/claude/v1/messages", {"model": "other-model"}, 403), ("/outside", {"model": "fixture-model"}, 403), ("/claude/v1/messages", {"model": "fixture-model", "tools": [{"type": "web_search_20250305"}]}, 403)]:
                        c = SocketConnection(root / "gateway.sock")
                        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "Authorization": "Bearer dummy-container-token"})
                        r = c.getresponse()
                        self.assertEqual(r.status, status)
                        data = r.read()
                        if status == 200:
                            self.assertIn(b"[DONE]", data)
                        c.close()
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0]["auth"], "Bearer fixture-private-key")
                self.assertNotIn("fixture-private-key", (root / "log.jsonl").read_text())
                records = [json.loads(x) for x in (root / "log.jsonl").read_text().splitlines()]
                self.assertEqual([r["status"] for r in records], [200, 403, 403, 403])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.assertFalse(thread.is_alive())


class GenerationLifecycleTests(unittest.TestCase):
    def setUp(self):
        from bench import runner
        from bench.storage import Store
        self.runner = runner
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for folder in ("runtime", "bench", "catalogue"):
            (self.root / folder).mkdir()
        for relative in ("runtime/launch.py", "bench/egress.py"):
            (self.root / relative).write_text("fixture policy")
        (self.root / "catalogue/models.json").write_text('{"opencode": {}}')
        self.store = Store(self.root / "index.sqlite", self.root)
        self.config = {"runtime": {"profile": "fixture"}}
        self.facts = {"source_hashes": {"bench/runner.py": "fixture"}, "effort_levels": ["max"],
                      "versions": {"claude": "fixture"}, "image_id": "fixture-image", "strategy": "fixture-isolated"}
        self.task = {"id": "crocodile", "name": "fixture 鳄鱼", "version": "standard-v1",
                     "rubric_version": "fixture", "prompt": "fixture prompt"}
        self.provider = {"base": "https://fixture.invalid", "secret": "", "proxy": None}

    def generate(self, gateway_events=None, *, result_error=None):
        result = {"type": "result", "subtype": "success", "is_error": False,
                  "result": "fixture complete", "total_cost_usd": 0.12, "num_turns": 2,
                  "usage": {"input_tokens": 7, "output_tokens": 9}}
        if result_error is not None:
            result.update({"subtype": "error_during_execution", "is_error": True, "errors": [result_error]})
        process = mock.Mock(returncode=0, stdin=io.StringIO(),
                            stdout=io.StringIO(json.dumps(result) + "\n"), stderr=io.StringIO())

        def command(config, output, *args):
            (output / "index.html").write_text("<html>fixture 鳄鱼</html>")
            if gateway_events is not None:
                (output.parent / "gateway.jsonl").write_text(
                    "".join(json.dumps(event) + "\n" for event in gateway_events))
            return ["fixture-command"]

        with mock.patch.object(self.runner, "Gateway"), \
             mock.patch.object(self.runner, "effort_record", return_value={"requested": "max", "applied": "max", "supported": ["max"], "control": "controlled", "ceiling": "max"}), \
             mock.patch.object(self.runner, "generation_command", side_effect=command), \
             mock.patch.object(self.runner.subprocess, "Popen", return_value=process):
            return self.runner.run_one(self.root, self.config, self.store, self.provider,
                                       self.root / "catalogue", self.facts, "fixture-batch", "fixture",
                                       "claude-code", "fixture-model", self.task, True,
                                       attempt=1, retry_of="fixture-parent", retry_reason="stream disconnected")

    def assert_stored(self, manifest):
        with sqlite3.connect(self.store.db_path) as db:
            row = json.loads(db.execute("SELECT manifest_json FROM runs WHERE id=?", (manifest["id"],)).fetchone()[0])
            indexed = json.loads(db.execute("SELECT fulljson FROM runs_search WHERE id=?", (manifest["id"],)).fetchone()[0])
        self.assertEqual(row, manifest)
        self.assertEqual(indexed, manifest)
        readback = self.store.get_run(manifest["id"])
        readback.pop("reviews")
        self.assertEqual(readback, manifest)
        self.assertEqual([r["id"] for r in self.store.list_runs({"q": manifest["id"]})], [manifest["id"]])
        self.assertEqual(json.loads((self.root / manifest["archive_dir"] / "manifest.json").read_text()), manifest)

    def test_real_generation_to_evaluation_sealing_sql_readback_search(self):
        generated = self.generate()
        self.assertEqual(generated["status"], "generated")
        self.assertEqual(generated["metrics"]["cost_usd"], 0.12)
        self.assertTrue(generated["generation_artifacts"])
        self.assert_stored(generated)
        directory = self.root / generated["archive_dir"]
        with mock.patch.object(self.runner, "evaluate_run", return_value={"status": "completed", "checks": [{"name": "load", "status": "pass"}], "evidence": []}):
            final = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["generation_status"], "completed")
        self.assertEqual(final["finished_at"], generated["finished_at"])
        self.assertEqual(final["metrics"]["cost_usd"], 0.12)
        self.assertEqual(final["archive_status"], "complete")
        self.assert_stored(final)
        actual = {a["path"]: a["sha256"] for a in artifact_index(directory)}
        self.assertEqual(actual, json.loads((directory / "checksums.json").read_text()))
        self.assertEqual(actual, {a["path"]: a["sha256"] for a in final["artifacts"]})
        with mock.patch.object(self.runner, "evaluate_run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            failed = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
        self.assertEqual(failed["evaluation"]["status"], "infrastructure_error")
        self.assertEqual([c["name"] for c in failed["checks"]], ["provider_route"])
        self.assertEqual(failed["finished_at"], generated["finished_at"])
        self.assert_stored(failed)

    def test_upstream_403_is_not_a_route_violation(self):
        generated = self.generate([{"status": 403, "status_origin": "upstream", "request_id": None}])
        directory = self.root / generated["archive_dir"]
        with mock.patch.object(self.runner, "evaluate_run", return_value={"status": "completed", "checks": [], "evidence": []}):
            final = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
        route = next(check for check in final["checks"] if check["name"] == "provider_route")
        self.assertEqual(route["status"], "pass")
        self.assertIn("restricted route rejections: 0", route["detail"])
        self.assertIn("upstream 403: 1", route["detail"])
        self.assert_stored(final)

    def test_route_evaluation_preserves_generation_facts_and_usage_with_sql_fts_readback(self):
        scenarios = [
            ("upstream", [{"status": 403, "status_origin": "upstream"}], "pass", "401 Unauthorized fixture"),
            ("policy", [{"status": 403, "status_origin": "local_policy", "policy_rejected": True,
                         "reason_code": "model_not_allowed"}], "fail", None),
            ("legacy_unknown", [{"status": 403, "request_id": None}], "unknown", None),
            ("legacy_unmarked", [{"status": 403}], "unknown", None),
            ("no_events", [], "unknown", None),
        ]
        for name, events, expected, error in scenarios:
            with self.subTest(scenario=name):
                # A preflight count is not billed model usage, even if a provider
                # puts token-looking values into the gateway observation.
                if events:
                    events = [{"status": 200, "status_origin": "upstream",
                               "path": "/claude/v1/messages/count_tokens?beta=true",
                               "input_tokens": 17000, "cost_usd": 999}, *events]
                generated = self.generate(events, result_error=error)
                self.assert_stored(generated)
                directory = self.root / generated["archive_dir"]
                original_output_hash = next(a["sha256"] for a in generated["generation_artifacts"]
                                            if a["path"] == "output/index.html")
                evaluation = {"status": "completed", "checks": [
                    {"name": "load", "status": "pass"},
                    {"name": "provider_route", "status": "fail", "detail": "stale evaluator route"}], "evidence": []}
                with mock.patch.object(self.runner, "evaluate_run", return_value=evaluation):
                    first = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
                    repeated = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
                for final in (first, repeated):
                    self.assertEqual(final["status"], generated["generation_status"])
                    self.assertEqual(final["archive_status"], "complete")
                    for field in ("error", "error_category", "generation_status", "generation_finished_at",
                                  "finished_at", "duration_ms", "conditions", "condition_fingerprint",
                                  "attempt", "retry_of", "retry_reason", "usage_raw", "generation_artifacts"):
                        self.assertEqual(final.get(field), generated.get(field), field)
                    for metric, value in generated["metrics"].items():
                        self.assertEqual(final["metrics"][metric], value, metric)
                    self.assertEqual(final["metrics"]["input_tokens"], 7)
                    self.assertEqual(final["metrics"]["output_tokens"], 9)
                    self.assertEqual(final["metrics"]["cost_usd"], 0.12)
                    route_checks = [c for c in final["checks"] if c["name"] == "provider_route"]
                    self.assertEqual(len(route_checks), 1)
                    self.assertEqual(route_checks[0]["status"], expected)
                    self.assertEqual(next(a["sha256"] for a in final["artifacts"] if a["path"] == "output/index.html"), original_output_hash)
                self.assert_stored(repeated)

    def test_legacy_upstream_markers_are_gated_by_frozen_generation_policy_hash(self):
        real_sha256 = self.runner.hashlib.sha256
        policy = b"fixture frozen legacy egress policy"
        (self.root / "bench/egress.py").write_bytes(policy)
        known_hash = "2d3c00abf7ebf12fc189935654ee87f43b4aa3867f43ef5ac6ebe75e759a8a5b"
        self.assertIn(known_hash, self.runner.LEGACY_EGRESS_UPSTREAM_MARKERS)
        for frozen_hash, expected in ((known_hash, "pass"), ("unknown-frozen-policy-hash", "unknown")):
            with self.subTest(frozen_hash=frozen_hash):
                def frozen_digest(data=b""):
                    # Set the condition during actual generation; never change a
                    # sealed manifest or its immutable gateway log afterwards.
                    if data == policy:
                        return mock.Mock(hexdigest=mock.Mock(return_value=frozen_hash))
                    return real_sha256(data)

                with mock.patch.object(self.runner.hashlib, "sha256", side_effect=frozen_digest):
                    generated = self.generate([{"status": 403, "request_id": None}])
                self.assertEqual(generated["conditions"]["egress_policy_sha256"], frozen_hash)
                directory = self.root / generated["archive_dir"]
                with mock.patch.object(self.runner, "evaluate_run", return_value={"status": "completed", "checks": [], "evidence": []}):
                    final = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
                route = next(c for c in final["checks"] if c["name"] == "provider_route")
                self.assertEqual(route["status"], expected)
                self.assertEqual(final["conditions"], generated["conditions"])
                self.assertEqual(final["archive_status"], "complete")
                self.assert_stored(final)

    def test_non_object_gateway_log_is_infrastructure_unknown_not_route_failure(self):
        for malformed in ([], None, "fixture non-object", 42):
            with self.subTest(malformed=malformed):
                generated = self.generate([malformed])
                directory = self.root / generated["archive_dir"]
                with mock.patch.object(self.runner, "evaluate_run", return_value={"status": "completed", "checks": [], "evidence": []}):
                    final = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
                self.assertEqual(final["evaluation"]["status"], "infrastructure_error")
                self.assertEqual(final["checks"][0]["name"], "provider_route")
                self.assertEqual(final["checks"][0]["status"], "unknown")
                self.assertEqual(final["archive_status"], "complete")
                self.assertEqual(final["generation_artifacts"], generated["generation_artifacts"])
                self.assertIsNone(final["error"])
                self.assert_stored(final)

    def test_generated_output_mutation_is_not_resealed_as_genuine(self):
        generated = self.generate()
        directory = self.root / generated["archive_dir"]
        (directory / "output/index.html").write_text("tampered output")
        with mock.patch.object(self.runner, "evaluate_run") as evaluate:
            final = self.runner.evaluate_archive(self.config, self.store, directory, "crocodile", generated["id"])
        evaluate.assert_not_called()
        self.assertEqual(final["archive_status"], "integrity_error")
        self.assertEqual(final["evaluation"]["status"], "infrastructure_error")
        self.assertIsNone(final["error"])
        self.assertEqual(final["generation_artifacts"], generated["generation_artifacts"])
        self.assert_stored(final)

    def test_evaluator_timeout_stops_its_named_container(self):
        directory = self.root / "render"
        directory.mkdir()
        name = "llm-bench-eval-fixture"
        with mock.patch.object(self.runner, "evaluate_command", return_value=["fixture"]), \
             mock.patch.object(self.runner.subprocess, "run", side_effect=[subprocess.TimeoutExpired("fixture", 1), mock.Mock(returncode=0)]) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.runner.evaluate_run(self.config, directory, "crocodile", name)
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "stop", "--time", "3", name])


if __name__ == "__main__":
    unittest.main()
