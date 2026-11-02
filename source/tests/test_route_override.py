"""Operator gateway route: declared explicitly, recorded without credentials."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.egress import connections, opencode_route, route_record  # noqa: E402


def config(base=None, key_env=None, proxy="direct"):
    routes = {}
    if base is not None:
        route = {"base": base}
        if key_env is not None:
            route["key_env"] = key_env
        if proxy is not None:
            route["proxy"] = proxy
        routes["opencode"] = route
    return {"routes": routes,
            "paths": {"claude_settings": "/nonexistent", "models_cache": "/nonexistent",
                      "opencode_egress_config": "/nonexistent", "opencode_egress_list": "/nonexistent"}}


class OpencodeRouteTests(unittest.TestCase):
    def test_absent_route_keeps_default_behaviour(self):
        self.assertIsNone(opencode_route({"paths": {}}))
        self.assertIsNone(route_record({"paths": {}}))

    def test_route_requires_https_base_and_existing_credential(self):
        with self.assertRaises(ValueError):
            opencode_route(config(base="ftp://gateway.example/v1"))
        with self.assertRaises(ValueError):
            opencode_route(config(base="https://gateway.example/v1", key_env="BENCH_TEST_ROUTE_KEY"))

    def test_route_record_never_contains_the_secret(self):
        with mock.patch.dict(os.environ, {"BENCH_TEST_ROUTE_KEY": "fixture-secret-value"}):
            route = opencode_route(config(base="https://gateway.example/v1/", key_env="BENCH_TEST_ROUTE_KEY"))
            record = route_record(config(base="https://gateway.example/v1/", key_env="BENCH_TEST_ROUTE_KEY"))
        self.assertEqual(route["base"], "https://gateway.example/v1")
        self.assertEqual(route["secret"], "fixture-secret-value")
        self.assertEqual(record["base"], {"scheme": "https", "host": "gateway.example", "port": None, "path": "/v1"})
        self.assertNotIn("secret", record)
        self.assertNotIn("fixture-secret-value", repr(record))

    def test_connections_uses_declared_route_and_never_public_fallback(self):
        settings = Path(tempfile.mkdtemp()) / "settings.json"
        settings.write_text('{"env": {"ANTHROPIC_BASE_URL": "https://claude.example", "ANTHROPIC_AUTH_TOKEN": "test-token"}}')
        catalogue = settings.parent / "models.json"
        catalogue.write_text('{"opencode": {"api": "https://zen.example/v1", "models": {}}}')
        cfg = {"paths": {"claude_settings": str(settings), "models_cache": str(catalogue),
                         "opencode_egress_config": "/nonexistent", "opencode_egress_list": "/nonexistent"},
               "routes": {"opencode": {"base": "https://gateway.example/v1",
                                       "key_env": "BENCH_TEST_ROUTE_KEY", "proxy": "direct"}}}
        with mock.patch.dict(os.environ, {"BENCH_TEST_ROUTE_KEY": "fixture-route-key"}):
            provider = connections(cfg)["opencode"]
        self.assertEqual(provider["base"], "https://gateway.example/v1")
        self.assertEqual(provider["headers"]["Authorization"], "Bearer fixture-route-key")
        self.assertIsNone(provider["proxy"])


if __name__ == "__main__":
    unittest.main()
