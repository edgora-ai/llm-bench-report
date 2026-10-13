"""Local fixtures only: no model calls, real credentials or production database."""
from contextlib import ExitStack
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from bench import runner
from bench.egress import Gateway, MIMO_GENERATION_MODEL, validate_generation_request_policy
from bench.storage import Store


POLICY = {"version": 1, "thinking": {"type": "disabled"}, "max_output_tokens": 32000}
MODEL = "mimo-v2.6-flash-free"
CANARY = "fixture-private-payload-never-real"
SECRET = "fixture-private-auth-never-real"
PATH = "/opencode/v1/chat/completions"


class SocketConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("gateway", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(str(self.path))


class LocalGateway:
    def __init__(self, policy=POLICY, *, statuses=None):
        self.policy = policy
        self.statuses = statuses
        self.observed = []
        self.stack = ExitStack()

    def __enter__(self):
        try:
            self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="request-policy-")))
            fixture = self

            class Upstream(BaseHTTPRequestHandler):
                def do_POST(self):
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    fixture.observed.append({"path": self.path, "bytes": body,
                                             "body": json.loads(body), "auth": self.headers.get("Authorization")})
                    response = b'{"fixture":true}'
                    status = fixture.statuses[len(fixture.observed) - 1] if fixture.statuses is not None else 200
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.send_header("request-id", CANARY)
                    self.end_headers()
                    self.wfile.write(response)

                def log_message(self, *_args):
                    return

            # A synthetic upstream deliberately uses loopback and an OS-assigned port.
            server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def close_upstream():
                server.shutdown()
                server.server_close()
                thread.join(5)
                if thread.is_alive():
                    raise RuntimeError("Fixture upstream did not stop")

            self.stack.callback(close_upstream)
            self.socket = self.root / "gateway.sock"
            self.log = self.root / "gateway.jsonl"
            provider = {"base": "http://%s:%s" % server.server_address,
                        "headers": {"Authorization": "Bearer " + SECRET}, "secret": SECRET, "proxy": None}
            gateway = Gateway(self.socket, provider, "opencode", MIMO_GENERATION_MODEL, self.log,
                              generation_request_policy=self.policy)
            gateway.__enter__()

            def close_gateway():
                gateway.__exit__(None, None, None)
                gateway.thread.join(5)
                if gateway.thread.is_alive():
                    raise RuntimeError("Fixture gateway did not stop")

            self.stack.callback(close_gateway)
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *_args):
        self.stack.close()

    def request(self, data, path=PATH):
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        connection = SocketConnection(self.socket)
        try:
            connection.request("POST", path, body,
                               {"Content-Type": "application/json", "Authorization": "Bearer " + CANARY,
                                "x-api-key": CANARY, "X-Fixture-Private": CANARY})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def payload(**controls):
    return {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": CANARY}],
            "tools": [{"type": "function", "function": {"name": "write", "description": CANARY,
                       "parameters": {"type": "object", "properties": {}}}}], **controls}


class GenerationRequestPolicyTests(unittest.TestCase):
    def test_exact_policy_types_route_and_independent_copy(self):
        original = copy.deepcopy(POLICY)
        valid = validate_generation_request_policy(original, "opencode", MIMO_GENERATION_MODEL)
        original["thinking"]["type"] = "enabled"
        self.assertEqual(valid, POLICY)
        for cap in (1, 4096, 32000):
            policy = {**POLICY, "max_output_tokens": cap}
            self.assertEqual(validate_generation_request_policy(policy, "opencode", MIMO_GENERATION_MODEL), policy)
        malformed = [None, [], {}, {**POLICY, "version": True}, {**POLICY, "version": 1.0},
                     {**POLICY, "version": 2}, {**POLICY, "thinking": None},
                     {**POLICY, "thinking": {"type": "enabled"}}, {**POLICY, "headers": {}}]
        malformed += [{**POLICY, "max_output_tokens": cap} for cap in (0, -1, 32001, True, 32000.0, "32000")]
        for policy in malformed:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_generation_request_policy(policy, "opencode", MIMO_GENERATION_MODEL)
        for tool, model in [("claude-code", MIMO_GENERATION_MODEL), ("opencode", MODEL),
                            ("opencode", "opencode/other-model")]:
            with self.subTest(tool=tool, model=model), self.assertRaises(ValueError):
                validate_generation_request_policy(POLICY, tool, model)

    def test_constructor_rejects_alternate_api_model_before_opening_socket(self):
        with tempfile.TemporaryDirectory(prefix="request-policy-invalid-") as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                Gateway(root / "socket", {}, "opencode", MIMO_GENERATION_MODEL, root / "log", "other-model",
                        generation_request_policy=POLICY)
            self.assertFalse((root / "socket").exists())
            self.assertFalse((root / "log").exists())

    def test_actual_request_bytes_caps_and_secret_free_outgoing_log(self):
        cases = [({}, {"max_tokens": 32000}), ({"max_tokens": 100000}, {"max_tokens": 32000}),
                 ({"max_tokens": 700}, {"max_tokens": 700}),
                 ({"max_completion_tokens": 1200}, {"max_completion_tokens": 1200}),
                 ({"max_completion_tokens": 65000}, {"max_completion_tokens": 32000}),
                 ({"max_tokens": 16000, "max_completion_tokens": 4096},
                  {"max_tokens": 4096, "max_completion_tokens": 4096}),
                 ({"max_tokens": 1024, "max_completion_tokens": 65000},
                  {"max_tokens": 1024, "max_completion_tokens": 1024})]
        with LocalGateway() as fixture:
            for incoming, caps in cases:
                with self.subTest(incoming=incoming):
                    data = payload(thinking={"type": "enabled", "budget_tokens": 8192}, **incoming)
                    data["messages"][0]["content"] += " 鳄鱼"
                    expected = {**data, "thinking": {"type": "disabled"}, **caps}
                    status, response = fixture.request(data)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(response), {"fixture": True})
                    observed = fixture.observed[-1]
                    self.assertEqual(observed["path"], "/chat/completions")
                    self.assertEqual(observed["bytes"], json.dumps(expected, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode())
                    self.assertEqual(observed["body"], expected)
                    self.assertEqual(observed["auth"], "Bearer " + SECRET)
                    record = fixture.records()[-1]
                    self.assertEqual(record["generation_request"], {"path": "/chat/completions", "model": MODEL,
                                     "controls": {"thinking": {"type": "disabled"}, **caps}})
                    self.assertEqual(record["status_origin"], "upstream")
                    self.assertFalse(record["policy_rejected"])
                    self.assertNotIn("request_id", record)
            logged = fixture.log.read_text()
            for private in (CANARY, SECRET, "messages", "tools", "headers", "Authorization", "upstream_honored"):
                self.assertNotIn(private, logged)

    def test_lower_policy_cap_and_tool_followup_requests_remain_allowed(self):
        policy = {**POLICY, "max_output_tokens": 2048}
        with LocalGateway(policy) as fixture:
            followup = payload(max_tokens=512, max_completion_tokens=4096)
            followup["messages"] += [{"role": "assistant", "tool_calls": [{"id": "fixture-write-1",
                                    "type": "function", "function": {"name": "write", "arguments": "{}"}}]},
                                     {"role": "tool", "tool_call_id": "fixture-write-1", "content": CANARY}]
            for data in [payload(max_tokens=32000), followup]:
                self.assertEqual(fixture.request(data)[0], 200)
            self.assertEqual(len(fixture.observed), 2)
            self.assertEqual(fixture.observed[0]["body"]["max_tokens"], 2048)
            self.assertEqual(fixture.observed[1]["body"]["max_tokens"], 512)
            self.assertEqual(fixture.observed[1]["body"]["max_completion_tokens"], 512)

    def test_malformed_conflicting_and_wrong_route_requests_never_reach_upstream(self):
        cases = [(payload(), "/opencode/v1/responses"), (payload(), "/opencode/v1/chat/completions?private=" + CANARY),
                 (payload(), "/opencode/v1/chat/completions#" + CANARY),
                 (payload(model=CANARY), PATH), (payload(model=MIMO_GENERATION_MODEL), PATH),
                 (payload(model=[CANARY]), PATH), ([], PATH), (b'{', PATH), (b'\xff', PATH),
                 (payload(top_p=float("nan")), PATH), (payload(top_p=float("inf")), PATH)]
        cases += [(payload(**{key: value}), PATH) for key in ("max_tokens", "max_completion_tokens")
                  for value in (True, None, "100", 0, -1, 1.5)]
        cases += [(payload(thinking=value), PATH) for value in (None, "disabled", {"type": []},
                  {"type": "adaptive"}, {"type": "disabled", "budget_tokens": True},
                  {"type": "enabled", "private": CANARY})]
        cases += [(payload(**{key: CANARY}), PATH) for key in ("reasoning_effort", "reasoning", "enable_thinking",
                  "chat_template_kwargs", "extra_body", "generation_config", "max_output_tokens")]
        cases += [(payload(tools=value), PATH) for value in ({}, [None], [{"function": None}])]
        cases.append((payload(tools=[{"type": "function", "function": {"name": "task"}}]), PATH))
        with LocalGateway() as fixture:
            for data, path in cases:
                with self.subTest(data=data, path=path):
                    self.assertEqual(fixture.request(data, path)[0], 403)
            self.assertEqual(fixture.observed, [])
            records = fixture.records()
            self.assertTrue(all(record["policy_rejected"] and record["status_origin"] == "local_policy" for record in records))
            self.assertTrue(all("generation_request" not in record for record in records))
            self.assertNotIn(CANARY, fixture.log.read_text())
            self.assertNotIn(SECRET, fixture.log.read_text())
            self.assertEqual(runner.provider_route_check(records)["status"], "fail")
            self.assertIn("request_controls_not_allowed=", runner.provider_route_check(records)["detail"])

    def test_upstream_errors_stay_upstream_without_retry_or_honoured_claim(self):
        with LocalGateway(statuses=[400, 504]) as fixture:
            for status in (400, 504):
                self.assertEqual(fixture.request(payload())[0], status)
            self.assertEqual(len(fixture.observed), 2)
            records = fixture.records()
            self.assertEqual([record["status"] for record in records], [400, 504])
            self.assertTrue(all(record["status_origin"] == "upstream" and not record["policy_rejected"]
                                for record in records))
            self.assertTrue(all(record["generation_request"]["controls"]["thinking"] == {"type": "disabled"}
                                for record in records))
            self.assertEqual(runner.provider_route_check(records)["status"], "pass")
            self.assertNotIn(CANARY, fixture.log.read_text())
            self.assertNotIn(SECRET, fixture.log.read_text())

    def test_unconfigured_forwarding_preserves_exact_original_bytes(self):
        raw = ('{ "model" : "mimo-v2.6-flash-free", "thinking": {"type":"enabled"}, '
               '"max_tokens": 65000, "messages": [{"role":"user", "content":"鳄鱼"}] }').encode("utf-8")
        with LocalGateway(None) as fixture:
            for path in (PATH, "/opencode/v1/responses"):
                self.assertEqual(fixture.request(raw, path)[0], 200)
                self.assertEqual(fixture.observed[-1]["bytes"], raw)
                self.assertEqual(fixture.observed[-1]["body"]["max_tokens"], 65000)
                record = fixture.records()[-1]
                self.assertNotIn("generation_request", record)
                self.assertEqual(record["request_id"], CANARY)
            self.assertEqual([observed["path"] for observed in fixture.observed], ["/chat/completions", "/responses"])


class GenerationPolicyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="request-policy-runner-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for folder in ("runtime", "bench", "catalogue"):
            (self.root / folder).mkdir()
        for relative in ("runtime/launch.py", "bench/egress.py"):
            (self.root / relative).write_text("synthetic fixture policy")
        catalogue = {"opencode": {"models": {MODEL: {"reasoning": True, "reasoning_options": []}}}}
        (self.root / "catalogue/models.json").write_text(json.dumps(catalogue))
        self.store = Store(self.root / "fixture.sqlite", self.root)
        self.config = {"runtime": {"profile": "fixture"}}
        self.facts = {"source_hashes": {"bench/runner.py": "fixture"}, "effort_levels": ["max"],
                      "versions": {"opencode": "fixture"}, "image_id": "fixture-image", "strategy": "fixture"}
        self.provider = {"base": "https://fixture.invalid", "secret": "", "proxy": None}
        self.task = {"id": "crocodile", "name": "synthetic fixture", "version": "standalone-svg-no-thinking-v2",
                     "rubric_version": "fixture", "prompt": "synthetic fixture prompt",
                     "generation_request_policy": copy.deepcopy(POLICY)}

    def generate(self, task):
        event = {"type": "step_finish", "part": {"id": "fixture-part", "reason": "stop", "cost": 0,
                 "tokens": {"input": 1, "output": 2, "reasoning": 0, "total": 3, "cache": {"read": 0, "write": 0}}}}
        process = mock.Mock(returncode=0, stdin=io.StringIO(), stdout=io.StringIO(json.dumps(event) + "\n"),
                            stderr=io.StringIO())

        def command(config, output, *args):
            self.assertIs(config, self.config)
            (output / "fixture.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>synthetic fixture</title></svg>')
            return ["synthetic-fixture-not-a-model"]

        with mock.patch.object(runner, "Gateway") as gateway, \
             mock.patch.object(runner, "generation_command", side_effect=command), \
             mock.patch.object(runner.subprocess, "Popen", return_value=process):
            manifest = runner.run_one(self.root, self.config, self.store, self.provider, self.root / "catalogue",
                                      self.facts, "fixture-batch", "fixture", "opencode", MIMO_GENERATION_MODEL,
                                      task, defer_evaluation=True)
        return manifest, gateway

    def assert_readback(self, manifest):
        with sqlite3.connect(self.store.db_path) as db:
            stored = json.loads(db.execute("SELECT manifest_json FROM runs WHERE id=?", (manifest["id"],)).fetchone()[0])
            indexed = json.loads(db.execute("SELECT fulljson FROM runs_search WHERE id=?", (manifest["id"],)).fetchone()[0])
        self.assertEqual(stored, manifest)
        self.assertEqual(indexed, manifest)
        readback = self.store.get_run(manifest["id"])
        readback.pop("reviews")
        self.assertEqual(readback, manifest)
        self.assertEqual([run["id"] for run in self.store.list_runs({"q": manifest["id"]})], [manifest["id"]])
        self.assertEqual(json.loads((self.root / manifest["archive_dir"] / "manifest.json").read_text()), manifest)

    def test_policy_bound_to_task_condition_and_unverified_effort(self):
        manifest, gateway = self.generate(self.task)
        condition = manifest["conditions"]
        self.assertEqual(condition["task_definition_sha256"], runner.digest(self.task))
        self.assertEqual(condition["generation_request_policy"], POLICY)
        self.assertEqual(manifest["condition_fingerprint"], runner.digest(condition))
        self.assertEqual(condition["reasoning_effort_requested"], "none")
        self.assertIsNone(condition["reasoning_effort_applied"])
        self.assertEqual(condition["reasoning_effort_control"], "per_request_disabled_requested_unverified")
        self.assertNotIn("--variant", manifest["cli_command"])
        self.assertEqual(gateway.call_args.kwargs, {"generation_request_policy": POLICY})
        self.assert_readback(manifest)
        second = {**self.task, "generation_request_policy": {**POLICY, "max_output_tokens": 2048}}
        changed, _ = self.generate(second)
        self.assertNotEqual(manifest["condition_fingerprint"], changed["condition_fingerprint"])
        self.assertNotEqual(condition["task_definition_sha256"], changed["conditions"]["task_definition_sha256"])
        self.assertEqual(manifest["cli_command"], changed["cli_command"])
        self.assert_readback(changed)
        alternate_protocol, _ = self.generate({**self.task, "version": "synthetic-other-protocol"})
        self.assertNotEqual(manifest["condition_fingerprint"], alternate_protocol["condition_fingerprint"])
        self.assert_readback(alternate_protocol)

    def test_absent_policy_keeps_legacy_command_effort_and_gateway_arguments(self):
        task = {key: value for key, value in self.task.items() if key != "generation_request_policy"}
        manifest, gateway = self.generate(task)
        condition = manifest["conditions"]
        self.assertNotIn("generation_request_policy", condition)
        self.assertEqual(condition["reasoning_effort_requested"], "max")
        self.assertIsNone(condition["reasoning_effort_applied"])
        self.assertEqual(condition["reasoning_effort_control"], "fixed_unspecified_effort")
        self.assertEqual(gateway.call_args.kwargs, {})
        self.assert_readback(manifest)

    def test_invalid_or_wrong_route_task_fails_before_run_or_store_writes(self):
        for policy, model in [(None, MIMO_GENERATION_MODEL), ({**POLICY, "max_output_tokens": 32001}, MIMO_GENERATION_MODEL),
                              (POLICY, "opencode/other-model")]:
            task = {**self.task, "generation_request_policy": policy}
            with self.subTest(policy=policy, model=model), mock.patch.object(runner, "Gateway") as gateway, \
                 mock.patch.object(runner.subprocess, "Popen") as popen, self.assertRaises(ValueError):
                runner.run_one(self.root, self.config, self.store, self.provider, self.root / "catalogue", self.facts,
                               "fixture", "fixture", "opencode", model, task, defer_evaluation=True)
            gateway.assert_not_called()
            popen.assert_not_called()
        self.assertFalse((self.root / "runs").exists())
        with sqlite3.connect(self.store.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
