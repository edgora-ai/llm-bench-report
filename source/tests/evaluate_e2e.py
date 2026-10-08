#!/usr/bin/env python3
"""Run the real evaluate_command against isolated fixtures (no model sessions).

After rebuilding the runtime image: python3 tests/evaluate_e2e.py
Without rebuilding: python3 tests/evaluate_e2e.py --worker-override runtime/evaluate_worker.py
Use --artifacts-dir /tmp/bench-evaluate-checks to retain reports/screenshots/logs.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bench.isolation import evaluate_command, image_id


CLEAN = """<!doctype html><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="script-src-attr 'none'">
<link rel="stylesheet" href="/local.css"><script src="/local.js"></script>
<h1>Local-only dependency fixture</h1><img id="local-image" src="/local.svg">
<iframe src="/local-frame.html" title="Intentionally CSP-blocked local frame"></iframe>
<iframe src="data:text/html,Local%20data%20frame" title="CSP-blocked data frame"></iframe>
<button id="inline-block" onclick="window.fixtureInlineExecuted = true">Blocked inline handler</button>
<script>
const blobFrame = document.createElement('iframe');
blobFrame.title = 'CSP-blocked blob frame';
blobFrame.src = URL.createObjectURL(new Blob(['Local blob frame'], {type: 'text/html'}));
document.body.append(blobFrame);
addEventListener('load', async () => {
    document.querySelector('#inline-block').click();
    if (window.fixtureInlineExecuted) throw new Error('Inline CSP restriction did not apply');
    const response = await fetch('/local.json');
    const data = await response.json();
    const image = document.querySelector('#local-image');
    if (!window.localScriptLoaded || !response.ok || data.fixture !== 'local-only' ||
        !image.complete || image.naturalWidth !== 8 ||
        getComputedStyle(document.documentElement).getPropertyValue('--fixture').trim() !== 'loaded') {
        throw new Error('Local self-serving dependency failed');
    }
    document.querySelector('h1').textContent = 'Local assets and fetch verified';
});
</script>"""

REMOTE = """<!doctype html><meta name="viewport" content="width=device-width">
<script src="https://script.fixture.invalid/library.js"></script>
<link rel="stylesheet" href="https://style.fixture.invalid/library.css">
<style>
@font-face { font-family: FixtureRemote; src: url('https://font.fixture.invalid/font.woff2'); }
h1 { font-family: FixtureRemote, sans-serif; }
</style>
<h1>Remote dependencies must fail the check</h1>
<img src="https://image.fixture.invalid/image.png">
<iframe src="https://frame.fixture.invalid/frame.html" title="Remote frame"></iframe>
<script>
fetch('https://connect.fixture.invalid/data.json').catch(error => {
    window.fixtureFetchError = error.message;
});
if (innerWidth < 600) {
    fetch('https://mobile.fixture.invalid/data.json').catch(error => {
        window.fixtureMobileError = error.message;
    });
}
</script>"""

LOCAL_FILES = {
    'local.js': 'window.localScriptLoaded = true;',
    'local.css': ':root { --fixture: loaded; } body { margin: 16px; } img, iframe { max-width: 100%; }',
    'local.svg': '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>',
    'local.json': '{"fixture":"local-only"}',
    'local-frame.html': '<p>Local frame fixture</p>',
}


def run_fixture(config, root, name, html, worker_override):
    fixture = root / name
    output = fixture / 'output'
    evidence = fixture / 'evidence'
    output.mkdir(parents=True)
    evidence.mkdir()
    (output / 'index.html').write_text(html)
    for filename, content in LOCAL_FILES.items():
        (output / filename).write_text(content)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    command = evaluate_command(config, output, evidence, 'dependency-fixture')
    if worker_override:
        # Keep evaluate_command's sandbox/entrypoint; add only a read-only source override.
        command[-3:-3] = ['--mount', f'type=bind,source={worker_override},target=/opt/bench/evaluate_worker.py,readonly']
    (fixture / 'command.json').write_text(json.dumps(command, indent=2))
    process = subprocess.run(command, capture_output=True, text=True, timeout=180)
    (fixture / 'stdout.txt').write_text(process.stdout)
    (fixture / 'stderr.txt').write_text(process.stderr)
    assert process.returncode == 0, f'{name}: Docker exited {process.returncode}: {process.stderr}'
    report = json.loads((evidence / 'evaluation.json').read_text())
    assert report['status'] == 'completed', f'{name}: {report}'
    checks = {check['name']: check for check in report['checks']}
    for check in ('entrypoint', 'load', 'javascript', 'mobile_overflow'):
        assert checks[check]['status'] == 'pass', f'{name}: {checks[check]}'
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}, 'Fixture output changed'
    for filename in ('desktop.png', 'mobile.png', 'animation.webm'):
        assert (evidence / filename).stat().st_size > 0, f'{name}: missing {filename}'
    violations = report['security_policy_violations']
    if name == 'clean':
        assert checks['external_requests']['status'] == 'pass', checks['external_requests']
        assert report['blocked_requests'] == [], report['blocked_requests']
        # Local/inline/data/blob restrictions are evidence, not remote dependencies.
        assert any(v['effective_directive'] == 'frame-src' and '/local-frame.html' in v['blocked_uri']
                   for v in violations), violations
        for prefix, directive in [('inline', 'script-src-attr'), ('blob', 'frame-src')]:
            assert sum(v['blocked_uri'].startswith(prefix) and v['effective_directive'] == directive
                       for v in violations) >= 2, (prefix, violations)
        # Chromium can redact blocked data-frame URLs to an empty blockedURI.
        assert sum((v['blocked_uri'] == '' or v['blocked_uri'].startswith('data'))
                   and v['effective_directive'] == 'frame-src' for v in violations) >= 2, violations
    else:
        assert checks['external_requests']['status'] == 'fail', checks['external_requests']
        expected = {
            'https://script.fixture.invalid': 'script-src-elem',
            'https://style.fixture.invalid': 'style-src-elem',
            'https://font.fixture.invalid': 'font-src',
            'https://image.fixture.invalid': 'img-src',
            'https://frame.fixture.invalid': 'frame-src',
            'https://connect.fixture.invalid': 'connect-src',
            'https://mobile.fixture.invalid': 'connect-src',
        }
        for origin, directive in expected.items():
            assert any(v['blocked_uri'].startswith(origin) and v['effective_directive'] == directive
                       and v['disposition'] == 'enforce' for v in violations), (origin, violations)
            assert any(url.startswith(origin) for url in report['blocked_requests']), (origin, report)
            assert origin in checks['external_requests']['detail'], checks['external_requests']
        # Non-mobile dependencies must be captured from both browser contexts.
        for origin in expected.keys() - {'https://mobile.fixture.invalid'}:
            assert sum(v['blocked_uri'].startswith(origin) for v in violations) >= 2, (origin, violations)
    summary = {'fixture': name, 'evaluation_status': report['status'],
               'external_requests': checks['external_requests']['status'],
               'blocked_requests': report['blocked_requests'],
               'security_policy_violation_count': len(violations),
               'javascript': checks['javascript']['status'], 'report': str(evidence / 'evaluation.json')}
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/bench.toml')
    parser.add_argument('--worker-override', type=Path)
    parser.add_argument('--artifacts-dir', type=Path)
    args = parser.parse_args()
    with args.config.open('rb') as stream:
        config = tomllib.load(stream)
    worker = args.worker_override.resolve() if args.worker_override else None
    if worker:
        assert worker.is_file(), worker
    before_image = image_id(config)
    with tempfile.TemporaryDirectory(prefix='bench-evaluate-e2e-') as tmp:
        root = args.artifacts_dir.resolve() if args.artifacts_dir else Path(tmp)
        root.mkdir(parents=True, exist_ok=True)
        for name, html in [('clean', CLEAN), ('remote', REMOTE)]:
            run_fixture(config, root, name, html, worker)
    assert image_id(config) == before_image, 'Runtime image changed during verification'
    print(json.dumps({'result': 'PASS', 'image_id': before_image,
                      'worker_override': str(worker) if worker else None}), flush=True)


if __name__ == '__main__':
    main()
