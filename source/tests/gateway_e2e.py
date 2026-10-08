"""No-paid Docker bridge fixture; never invokes a model CLI or connections()."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid


CONTAINER_PROBE = r'''
import http.client
import json
import os
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, '/opt/bench')
from launch import Bridge

credentials = {'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'OPENCODE_API_KEY'}
assert not credentials.intersection(os.environ), 'Host provider credentials entered container'
mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
assert any(row[4] == '/bridge' and 'ro' in row[5].split(',') for row in mounts), 'Bridge mount is writable'
server = ThreadingHTTPServer((os.environ['BENCH_INTERNAL_HOST'], 0), Bridge)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
model = os.environ['FIXTURE_MODEL']
payload = {'model': model, 'messages': [{'role': 'user', 'content': 'fixture'}]}
requests = [
    ('/claude/v1/messages/count_tokens', payload, 200),
    ('/claude/v1/messages/count_tokens?beta=true', payload, 200),
    ('/claude/v1/messages?beta=true', payload, 200),
    ('/claude/v1/messages/count_tokens?beta=true', {**payload, 'fixture_error': True}, 403),
    ('/claude/v1/messages', {**payload, 'model': 'other-model'}, 403),
    ('/outside', payload, 403),
    ('/claude/v1/messages/count_tokens?unexpected=true', payload, 403),
    ('/claude/v1/messages', {**payload, 'tools': [{'type': 'web_search_20250305'}]}, 403),
    ('/claude/v1/messages', {**payload, 'tools': [{'name': 'Task'}]}, 403),
]
statuses = []
upstream_body = None
try:
    for index, (path, data, expected) in enumerate(requests):
        connection = http.client.HTTPConnection(*server.server_address, timeout=10)
        try:
            connection.request('POST', path, json.dumps(data), {
                'Content-Type': 'application/json', 'Authorization': 'Bearer dummy-container-token',
                'x-api-key': 'dummy-container-api-key'})
            response = connection.getresponse()
            body = response.read()
            assert response.status == expected, (path, response.status, expected)
            statuses.append(response.status)
            if index < 2:
                assert response.getheader('Content-Type') == 'application/json'
                assert json.loads(body) == {'input_tokens': 17}
            elif index == 2:
                assert response.getheader('Content-Type') == 'text/event-stream'
                assert b'data: [DONE]' in body and b'[REDACTED]' in body
            elif index == 3:
                upstream_body = body.decode()
                assert json.loads(body)['error']['type'] == 'permission_error'
            else:
                assert json.loads(body)['error']['type'] == 'benchmark_gateway_error'
        finally:
            connection.close()
finally:
    server.shutdown()
    server.server_close()
    thread.join(5)
    assert not thread.is_alive(), 'Bridge server did not stop'
print(json.dumps({'model': model, 'statuses': statuses, 'upstream_403_body': upstream_body,
                  'count_tokens': 17, 'stream_preserved': True, 'credential_env_absent': True,
                  'readonly_bridge': True}))
'''


def run(config_path=None):
    from bench.cli import load_config
    from bench.isolation import docker_base
    from bench.runner import provider_route_check
    from test_runtime import FIXTURE_MODELS, FIXTURE_SECRET, GatewayHTTPFixture, UPSTREAM_FORBIDDEN

    # This loads only project TOML; it never resolves configured provider secrets.
    config = load_config(config_path)
    image = config['runtime']['image']
    reports = []
    for model in FIXTURE_MODELS:
        with GatewayHTTPFixture(model, bridge=False) as fixture:
            name = 'llm-bench-gateway-fixture-' + uuid.uuid4().hex[:12]
            command = docker_base(image, name) + [
                '--mount', f'type=bind,source={fixture.root},target=/bridge,readonly',
                '-e', 'BENCH_GATEWAY_SOCKET=/bridge/gateway.sock',
                '-e', 'BENCH_INTERNAL_HOST=' + config['runtime']['internal_host'],
                '-e', 'FIXTURE_MODEL=' + model,
                image, 'python', '-c', CONTAINER_PROBE,
            ]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                cleanup = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=30)
                if cleanup.returncode:
                    raise RuntimeError('Docker fixture timed out and cleanup failed: ' + cleanup.stderr.strip())
                raise
            if result.returncode:
                raise RuntimeError('Docker gateway fixture failed: ' + result.stderr.strip())
            if result.stderr.strip():
                raise RuntimeError('Docker gateway fixture emitted unexpected errors: ' + result.stderr.strip())
            probe = json.loads(result.stdout)
            assert probe['upstream_403_body'].encode() == UPSTREAM_FORBIDDEN
            fixture.gateway.server.shutdown()
            records = fixture.records()
            assert len(records) == 9, records
            assert len(fixture.observed) == 4, fixture.observed
            payload = {'model': model, 'messages': [{'role': 'user', 'content': 'fixture'}]}
            assert [observed['path'] for observed in fixture.observed] == [
                '/v1/messages/count_tokens', '/v1/messages/count_tokens?beta=true',
                '/v1/messages?beta=true', '/v1/messages/count_tokens?beta=true']
            assert [observed['body'] for observed in fixture.observed] == [
                payload, payload, payload, {**payload, 'fixture_error': True}]
            assert [record['status_origin'] for record in records] == ['upstream'] * 4 + ['local_policy'] * 5
            assert [record.get('reason_code') for record in records[4:]] == [
                'model_not_allowed', 'route_not_allowed', 'query_not_allowed', 'tool_not_allowed', 'tool_not_allowed']
            assert all(record.get('policy_rejected') is not True for record in records[:4])
            assert all(record.get('policy_rejected') is True for record in records[4:])
            assert all(observed['auth'] == 'Bearer ' + FIXTURE_SECRET for observed in fixture.observed)
            assert all(observed['api_key'] is None for observed in fixture.observed)
            assert all(observed['body']['model'] == model for observed in fixture.observed)
            assert FIXTURE_SECRET not in fixture.log_path.read_text()
            assert 'dummy-container-token' not in fixture.log_path.read_text()
            allowed = provider_route_check(records[:4])
            denied = provider_route_check(records)
            assert allowed['status'] == 'pass' and 'upstream 403: 1' in allowed['detail']
            assert denied['status'] == 'fail' and 'restricted route rejections: 5' in denied['detail']
            probe.pop('upstream_403_body')
            probe.update({'upstream_hits': len(fixture.observed), 'upstream_403_preserved': True,
                          'origin_verified': True, 'policy_rejections': 5,
                          'allowed_route_check': allowed['status'], 'full_route_check': denied['status']})
            reports.append(probe)
    print(json.dumps({'fixture': 'docker-network-none-unix-gateway', 'image': image,
                      'paid_model_calls': 0, 'results': reports}, separators=(',', ':')))


def main():
    parser = argparse.ArgumentParser(description='Run a local-only Docker Bridge/Gateway regression fixture without model CLI calls.')
    parser.add_argument('--config', type=Path, help='Project TOML configuration (default: config/bench.toml)')
    args = parser.parse_args()
    run(args.config)


if __name__ == '__main__':
    main()
