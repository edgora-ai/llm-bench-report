#!/usr/bin/env python3
"""Browser checks for the dashboard. Snapshot fixtures never enter the run store.

Snapshot-only needs no server: python3 tests/dashboard_e2e.py --snapshot-only
Live mode requires an isolated test store with safe evidence and BENCH_TEST_TOKEN:
  python3 tests/dashboard_e2e.py --base-url "$BENCH_TEST_URL" --review-file "$BENCH_REVIEW_FILE"
Live mode appends one clearly identified test review; never use a production store.
Screenshots and downloads default to /tmp/llm-bench-dashboard-e2e.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
import zlib

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def fixture_png() -> str:
    """A visibly synthetic raster test scene, with no external assets."""
    width, height = 480, 300
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            color = (224, 234, 247)
            if y > 228:
                color = (157, 177, 205)
            elif (x - 359) ** 2 + (y - 76) ** 2 < 27 ** 2:
                color = (242, 189, 98)
            elif 45 < x < 285 and 104 < y < 222:
                color = (42, 120, 214) if y < 130 else (238, 244, 253)
                if 63 < x < 267 and 152 < y < 159:
                    color = (172, 192, 220)
                if 63 < x < 166 and 182 < y < 204:
                    color = (42, 120, 214)
            pixels.extend(color)
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(bytes(pixels))) + chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(png).decode()


def fixture() -> dict:
    runs = []
    evidence = {}
    for index in range(8):
        run_id = f'fixture-only-{index + 1:02}'
        model = ['Model Atlas', 'Model Cobalt', 'Model Slate', 'Model Quartz'][index % 4]
        date = ['2026-09-25', '2026-09-27'][index // 4]
        # Four effort states the dashboard must tell apart: applied at the
        # model's own ceiling, downgraded from the request, uncontrollable,
        # and a pre-effort archive that records no condition at all.
        efforts = [
            {'reasoning_effort_requested': 'max', 'reasoning_effort_applied': 'max',
             'reasoning_effort_supported': ['low', 'medium', 'high', 'xhigh', 'max'],
             'reasoning_effort_control': 'declared'},
            {'reasoning_effort_requested': 'max', 'reasoning_effort_applied': 'xhigh',
             'reasoning_effort_supported': ['minimal', 'low', 'medium', 'high', 'xhigh'],
             'reasoning_effort_control': 'declared'},
            {'reasoning_effort_requested': 'max', 'reasoning_effort_applied': None,
             'reasoning_effort_supported': None,
             'reasoning_effort_control': 'fixed_unspecified_effort'},
            {},
        ][index % 4]
        run = {
            'id': run_id, 'batch_id': f'fixture-batch-{index // 4 + 1}', 'date': date,
            'purpose': 'benchmark' if index < 7 else 'smoke', 'tool': 'fixture-tool',
            'model': model, 'task_id': 'fixture-task', 'task_name': '明确标注的测试任务',
            'prompt_version': 'fixture-v1', 'condition_fingerprint': f'fixture-condition-{index % 4}',
            'started_at': date + 'T10:00:00Z', 'finished_at': date + 'T10:01:00Z',
            'status': 'completed' if index < 6 else 'failed', 'duration_ms': 12000 + index * 7100,
            'metrics': {'cost_usd': None if index == 2 else round(.013 + index * .012, 4), 'cost_source': 'fixture explicit', 'input_tokens': 1300 + index * 500, 'output_tokens': 2400 + index * 500, 'total_tokens': None if index == 3 else 3700 + index * 1000},
            'artifacts': [{'path': 'evidence/fixture.png', 'size': 6400, 'sha256': 'fixture-not-real-hash', 'kind': 'evidence'}, {'path': 'generated/index.html', 'size': 123, 'kind': 'generated'}, {'path': 'logs/test.log', 'kind': 'log', 'size': 100}],
            'checks': [{'name': 'fixture-check', 'status': 'pass' if index % 2 == 0 else 'fail', 'detail': '<script>fixture unsafe text</script>'}],
            'evaluation': {'status': 'infrastructure_error' if index == 5 else 'fixture', 'evidence': ['evidence/fixture.png', 'generated/index.html', 'evidence/unsafe.svg']},
            'reviews': [], 'prompt_hash': 'fixture-prompt-hash', 'archive_dir': '/fixture/not-real',
            'conditions': {'task_id': 'fixture-task', 'prompt_hash': 'fixture-prompt-hash', **efforts},
        }
        runs.append(run)
        evidence[run_id + '/evidence/fixture.png'] = fixture_png()
    return {'runs': runs, 'tasks': [{'id': 'fixture-task', 'name': '明确标注的测试任务'}], 'evidence': evidence}


def check_overflow(page) -> None:
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Page body horizontally overflows'


def screenshot_matrix(page, output: Path, prefix: str) -> None:
    for width, height, size in [(1440, 1000, 'desktop'), (400, 900, 'mobile')]:
        page.set_viewport_size({'width': width, 'height': height})
        for theme in ['light', 'dark']:
            page.evaluate('(theme) => { document.documentElement.dataset.theme = theme; }', theme)
            check_overflow(page)
            page.screenshot(path=str(output / f'{prefix}-{size}-{theme}.png'), full_page=True)


def snapshot_checks(browser, output: Path) -> None:
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    errors, api_calls = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    data = fixture()
    html = (ROOT / 'web/index.html').read_text()
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    html = html.replace('<script src="/app.js" defer></script>', '<script>window.BENCH_SNAPSHOT=' + payload + ';</script><script src="/app.js" defer></script>')
    def route_handler(route):
        path = urlparse(route.request.url).path
        if path.startswith('/api/'):
            api_calls.append(path)
            route.abort()
        elif path == '/':
            route.fulfill(status=200, content_type='text/html', body=html)
        elif path in ['/app.js', '/styles.css']:
            route.fulfill(status=200, content_type='text/javascript' if path.endswith('.js') else 'text/css', body=(ROOT / 'web' / path[1:]).read_text())
        else:
            route.abort()
    page.route('https://bench-fixture.test/**', route_handler)
    page.goto('https://bench-fixture.test/')
    page.wait_for_load_state('networkidle')
    expect(page.locator('#result-label')).to_contain_text('7 运行')
    expect(page.locator('#auth-button')).to_be_disabled()
    expect(page.locator('[data-export="json"]')).to_be_disabled()
    assert not page.locator('#auth-dialog').is_visible()
    expect(page.locator('#summary')).to_contain_text('unknown')
    screenshot_matrix(page, output, 'snapshot-fixture')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('.chart .mark').first.focus()
    expect(page.locator('#chart-tooltip')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('#chart-tooltip')).to_be_hidden()
    page.locator('#texture-toggle').click()
    expect(page.locator('#texture-toggle')).to_have_attribute('aria-pressed', 'true')
    page.locator('#texture-toggle').click()
    for key, selected in [('tool', 'fixture-tool'), ('task_id', 'fixture-task'), ('prompt_version', 'fixture-v1')]:
        page.locator(f'[name="{key}"]').select_option(selected)
        expect(page.locator('#result-label')).to_contain_text('7 运行')
    page.locator('[name="purpose"]').select_option('')
    expect(page.locator('#result-label')).to_contain_text('8 运行')
    page.locator('[name="status"]').select_option('failed')
    expect(page.locator('#result-label')).to_contain_text('2 运行')
    page.locator('[name="model"]').select_option('Model Quartz')
    expect(page.locator('#result-label')).to_contain_text('1 运行')
    page.locator('#reset-filters').click()
    # A collapsed check set from a failed evaluator is not a passing ratio.
    expect(page.locator('.run-table tbody tr').nth(5)).to_contain_text('未验收')
    assert not page.locator('.run-table tbody tr').nth(5).inner_text().count('1/1 通过')
    expect(page.locator('#result-label')).to_contain_text('7 运行')
    page.locator('[name="date_to"]').fill('2026-09-25')
    expect(page.locator('#result-label')).to_contain_text('4 运行')
    page.locator('[name="date_from"]').fill('2026-09-25')
    expect(page.locator('#result-label')).to_contain_text('4 运行')
    page.locator('[name="q"]').fill('fixture-prompt-hash')
    expect(page.locator('.run-table tbody tr')).to_have_count(4)
    page.locator('#reset-filters').click()
    expect(page.locator('#result-label')).to_contain_text('7 运行')
    page.locator('[name="model"]').select_option('Model Atlas')
    expect(page.locator('#result-label')).to_contain_text('2 运行')
    assert page.locator('.run-table tbody tr').count() == 2
    page.locator('[data-view="trend"]').click()
    expect(page.locator('#view-content')).to_contain_text('指纹 fixture-condition-0')
    # The absent middle date is unknown rather than bridged or zero-filled.
    page.locator('.chart-table summary').first.click()
    expect(page.locator('.chart-table')).to_contain_text('2026-09-26')
    expect(page.locator('.chart-table')).to_contain_text('unknown')
    page.locator('[data-view="batch"]').click()
    # Generate a tiny, explicitly synthetic WebM without adding a dependency or disk asset.
    webm = page.evaluate('''() => new Promise((resolve, reject) => {
      const canvas = document.createElement('canvas'); canvas.width = 160; canvas.height = 100;
      const ctx = canvas.getContext('2d'); ctx.fillStyle = '#2a78d6'; ctx.fillRect(0, 0, 160, 100);
      const stream = canvas.captureStream(10), chunks = [];
      const recorder = new MediaRecorder(stream, {mimeType: 'video/webm'});
      recorder.ondataavailable = event => chunks.push(event.data);
      recorder.onerror = reject;
      recorder.onstop = () => { const reader = new FileReader(); reader.onload = () => { stream.getTracks().forEach(track => track.stop()); resolve(reader.result); }; reader.onerror = reject; reader.readAsDataURL(new Blob(chunks, {type: 'video/webm'})); };
      recorder.start(); setTimeout(() => { ctx.fillStyle = '#dbe9fc'; ctx.fillRect(30, 30, 80, 40); }, 80);
      setTimeout(() => recorder.stop(), 280);
    })''')
    page.evaluate('''uri => { window.BENCH_SNAPSHOT.runs.forEach(run => {
      run.evaluation.evidence.push('evidence/fixture.webm');
      window.BENCH_SNAPSHOT.evidence[run.id + '/evidence/fixture.webm'] = uri;
    }); }''', webm)
    page.locator('.run-table tbody input[type="checkbox"]').nth(0).check()
    page.locator('.run-table tbody input[type="checkbox"]').nth(1).check()
    page.locator('#compare-button').click()
    expect(page.locator('#compare-dialog')).to_be_visible()
    assert page.locator('#compare-content img').count() == 2
    expect(page.locator('#compare-content video')).to_have_count(2)
    page.wait_for_function('Array.from(document.querySelectorAll("#compare-content video")).every(video => video.readyState >= 2)')
    page.evaluate('document.querySelectorAll("#compare-content video").forEach(video => { video.currentTime = .1; })')
    page.locator('#restart-videos').click()
    page.wait_for_function('Array.from(document.querySelectorAll("#compare-content video")).every(video => !video.paused && video.currentTime < .15)')
    assert page.locator('iframe, object, embed').count() == 0
    page.locator('[data-close="compare-dialog"]').click()
    # Effort must be visible in the detail dialog and must distinguish the
    # three controlled states from a pre-effort archive with no record. The
    # filters above narrowed the table, so reset before indexing rows.
    page.locator('#reset-filters').click()
    expect(page.locator('.run-name button')).to_have_count(7)
    for index, expected in [(0, 'max（已设为该模型最高档）'),
                            (1, 'xhigh（请求 max，该模型上限 xhigh）'),
                            (2, '不可控：模型无档位阶梯，强度由模型内部固定'),
                            (3, '未记录（旧归档无此条件）')]:
        page.locator('.run-name button').nth(index).click()
        expect(page.locator('#detail-content')).to_contain_text('推理强度')
        expect(page.locator('#detail-content')).to_contain_text(expected)
        page.locator('[data-close="detail-dialog"]').click()
    page.locator('.run-name button').first.click()
    expect(page.locator('#detail-content')).to_contain_text('fixture-prompt-hash')
    expect(page.locator('#detail-content')).to_contain_text('不包含、不请求文本日志')
    assert page.locator('#detail-content button').filter(has_text='快照不可下载').count() == 3
    assert page.locator('#detail-content script').count() == 0
    page.locator('[data-close="detail-dialog"]').click()
    page.locator('#reset-filters').click()
    expect(page.locator('#result-label')).to_contain_text('7 运行')
    page.locator('[name="q"]').fill('does-not-exist-fixture')
    expect(page.locator('#view-content')).to_contain_text('当前条件下，没有运行')
    page.locator('#reset-filters').click()
    expect(page.locator('#result-label')).to_contain_text('7 运行')
    page.locator('[data-view="gallery"]').click()
    expect(page.locator('#view-content .gallery img')).to_have_count(7)
    for width in [1440, 400]:
        page.set_viewport_size({'width': width, 'height': 900})
        check_overflow(page)
    page.locator('[data-view="blind"]').click()
    expect(page.locator('#view-content')).to_contain_text('写入已禁用')
    expect(page.locator('#summary')).to_be_hidden()
    expect(page.locator('#filters')).to_be_hidden()
    # No transient token or other per-viewer state beyond theme is stored.
    assert page.evaluate('Object.keys(localStorage).every(key => key === "bench-theme")')
    assert not errors, errors
    assert not api_calls, api_calls
    print('PASS snapshot: no API requests; filters, unknowns, gaps, safe evidence, details, comparison, read-only controls; desktop/400px light+dark')
    context.close()


def live_checks(browser, args, output: Path) -> None:
    if not args.base_url or not args.token:
        raise SystemExit('Live mode requires --base-url and BENCH_TEST_TOKEN / --token. Use --snapshot-only when backend is not ready.')
    base = args.base_url.rstrip('/')
    context = browser.new_context(base_url=base, viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
    assert context.request.get(base + '/').status == 200, 'Public root must return 200'
    assert context.request.get(base + '/api/runs').status == 401, 'Runs must reject unauthenticated requests'
    assert context.request.get(base + '/api/options').status == 401
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base)
    expect(page.locator('#auth-dialog')).to_be_visible()
    page.locator('#login-token').fill(args.token)
    page.locator('#login-form button[type="submit"]').click()
    expect(page.locator('#auth-dialog')).to_be_hidden()
    expect(page.locator('#updated-at')).to_contain_text('更新于')
    assert page.locator('#login-token').input_value() == ''
    cookies = context.cookies()
    assert any(cookie['httpOnly'] for cookie in cookies), 'Session cookie must be HttpOnly'
    assert args.token not in page.evaluate('JSON.stringify(localStorage)')
    assert context.request.get(base + '/api/runs').status == 200
    if args.purpose != 'benchmark':
        page.locator('[name="purpose"]').select_option(args.purpose)
    initial = context.request.get(base + '/api/runs', params={'purpose': args.purpose}).json()['runs']
    assert initial, 'Live isolated store must have clearly identified test runs'
    expect(page.locator('#result-label')).to_contain_text(f'{len(initial)} 运行')
    known_model = str(initial[0]['model'])
    page.locator('[name="model"]').select_option(known_model)
    expect(page.locator('#result-label')).to_contain_text(f'{sum(run["model"] == known_model for run in initial)} 运行')
    page.locator('.run-name button').first.click()
    expect(page.locator('#detail-content')).to_contain_text('Manifest')
    assert page.locator('#detail-content iframe').count() == 0
    if args.purpose == 'fixture':
        page.get_by_role('button', name='读取日志', exact=True).click()
        expect(page.locator('#detail-content .log-picker + .pre-scroll pre')).to_contain_text('<script>not executable</script> explicit fixture log')
        assert page.evaluate('window.__UNSAFE_GENERATED_HTML_EXECUTED === undefined')
        with page.expect_download() as raw_html:
            page.locator('#detail-content .attachment-list li').nth(1).get_by_role('button', name='下载附件').click()
        raw_path = output / 'explicit-fixture-raw-attachment.html'
        raw_html.value.save_as(raw_path)
        assert '<script>' in raw_path.read_text()
        assert page.evaluate('window.__UNSAFE_GENERATED_HTML_EXECUTED === undefined')
    page.locator('[data-close="detail-dialog"]').click()
    page.locator('[name="model"]').select_option('')
    expect(page.locator('#result-label')).to_contain_text(f'{len(initial)} 运行')
    screenshot_matrix(page, output, 'live')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('[data-view="blind"]').click()
    expect(page.locator('#review-form')).to_be_visible()
    expect(page.locator('#summary')).to_be_hidden()
    expect(page.locator('#filters')).to_be_hidden()
    assert 'Model' not in page.locator('#review-form').inner_text()
    reviewer = f'dashboard-e2e-{time.time_ns()}'
    note = 'Explicit isolated browser test; partial score, not a quality claim.'
    page.locator('[name="reviewer"]').fill(reviewer)
    page.locator('[name="compliance"]').select_option('3')
    page.locator('[name="note"]').fill(note)
    with page.expect_request(re.compile(r'/api/runs/[^/]+/reviews$')) as submitted:
        page.locator('#review-form button[type="submit"]').click()
    run_id = urlparse(submitted.value.url).path.split('/')[3]
    expect(page.locator('#review-result')).to_contain_text('已读回验证')
    detail = context.request.get(base + '/api/runs/' + run_id).json()['run']
    records = [review for review in detail['reviews'] if review['reviewer'] == reviewer]
    assert len(records) == 1
    assert records[0]['scores']['compliance'] == 3 and records[0]['note'] == note and records[0]['blind'] is True
    assert records[0]['scores'].get('motion') is None, 'Partial scores must remain unknown'
    if getattr(args, 'isolated_root', None):
        with sqlite3.connect(args.isolated_db) as database:
            rows = database.execute('SELECT review_json FROM reviews WHERE run_id=?', (run_id,)).fetchall()
        persisted = [json.loads(row[0]) for row in rows if json.loads(row[0])['reviewer'] == reviewer]
        assert len(persisted) == 1 and persisted[0]['scores']['compliance'] == 3
        archived = list((args.isolated_root / detail['archive_dir'] / 'reviews').glob('*.json'))
        archived_reviews = [json.loads(path.read_text()) for path in archived]
        assert any(review['reviewer'] == reviewer and review['note'] == note for review in archived_reviews)
        print(f'PASS actual SQLite reviews row and run_archive/reviews JSON; {len(persisted)} test review persisted')
    elif args.review_file:
        raw = Path(args.review_file).read_text()
        assert reviewer in raw and note in raw, 'Review missing from actual backing store'
        print(f'PASS direct storage read: {args.review_file}')
    else:
        print('NOT TESTED direct backing-store verification: provide --review-file (API read-back is tested)')
    # Readback after full frontend reload plus searchable retrieval.
    page.reload()
    expect(page.locator('#updated-at')).to_contain_text('更新于')
    page.locator('[data-view="evidence"]').click()
    page.locator('[name="q"]').fill(run_id)
    expect(page.locator('#result-label')).to_contain_text('1 运行')
    search = context.request.get(base + '/api/runs', params={'purpose': args.purpose, 'q': run_id}).json()['runs']
    assert any(run['id'] == run_id for run in search)
    page.locator('.run-name button').first.click()
    expect(page.locator('#detail-content')).to_contain_text(reviewer)
    expect(page.locator('#detail-content')).to_contain_text(note)
    page.locator('[data-close="detail-dialog"]').click()
    with page.expect_download() as downloaded:
        page.locator('[data-export="json"]').click()
    saved = output / 'live-filtered-export.json'
    downloaded.value.save_as(saved)
    exported = json.loads(saved.read_text())
    exported_runs = exported if isinstance(exported, list) else exported['runs']
    assert len(exported_runs) == 1 and exported_runs[0]['id'] == run_id
    with page.expect_download() as csv_download:
        page.locator('[data-export="csv"]').click()
    csv_path = output / 'live-filtered-export.csv'
    csv_download.value.save_as(csv_path)
    assert run_id in csv_path.read_text()
    page.locator('#auth-button').click()
    expect(page.locator('#auth-button')).to_have_text('登录')
    assert context.request.get(base + '/api/runs').status == 401
    assert not errors, errors
    print(f'PASS live: public/401/HttpOnly login -> data -> filter -> details -> partial blind review append -> API readback -> reload -> search -> JSON/CSV export -> logout; reviewed run {run_id}')
    context.close()


def isolated_checks(browser, args, output: Path) -> None:
    """Use the real server/Store in a temporary root; never touch production runs."""
    sys.path.insert(0, str(ROOT))
    from bench.server import make_server
    from bench.storage import Store
    with tempfile.TemporaryDirectory(prefix='bench-dashboard-fixture-') as temporary:
        root = Path(temporary)
        shutil.copytree(ROOT / 'web', root / 'web')
        shutil.copytree(ROOT / 'bench/tasks', root / 'bench/tasks')
        db_path = root / 'data/index.sqlite'
        store = Store(db_path, root)
        data = fixture()
        for run in data['runs']:
            run['purpose'] = 'fixture'
            run['tool'] = 'opencode'
            run['archive_dir'] = f'runs/{run["date"]}/{run["id"]}'
            run['evaluation']['evidence'] = ['evidence/fixture.png']
            archive = root / run['archive_dir']
            (archive / 'evidence').mkdir(parents=True)
            (archive / 'generated').mkdir()
            (archive / 'logs').mkdir()
            (archive / 'evidence/fixture.png').write_bytes(base64.b64decode(data['evidence'][run['id'] + '/evidence/fixture.png'].split(',')[1]))
            (archive / 'generated/index.html').write_text('<script>window.__UNSAFE_GENERATED_HTML_EXECUTED=true</script>')
            (archive / 'logs/test.log').write_text('<script>not executable</script> explicit fixture log')
            (archive / 'manifest.json').write_text(json.dumps(run))
            store.upsert_run(run)
        with sqlite3.connect(db_path) as database:
            assert database.execute('SELECT COUNT(*) FROM runs WHERE purpose=?', ('fixture',)).fetchone()[0] == 8
        assert store.get_run('fixture-only-01')['purpose'] == 'fixture'
        assert len(store.list_runs({'purpose': 'fixture', 'q': 'fixture-only-01'})) == 1
        print('PASS seed storage flow: 8 explicit fixture runs -> direct SQLite count -> get_run -> indexed search')
        server = make_server(root, db_path, '127.0.0.1', 0, 'fixture-only-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        args.base_url = f'http://127.0.0.1:{server.server_port}'
        args.token, args.purpose = 'fixture-only-token', 'fixture'
        args.isolated_root, args.isolated_db = root, db_path
        try:
            live_checks(browser, args, output)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot-only', action='store_true')
    parser.add_argument('--isolated', action='store_true', help='Start real HTTP API on an ephemeral port with a temporary fixture-only store')
    parser.add_argument('--purpose', default='benchmark')
    parser.add_argument('--base-url', default=os.environ.get('BENCH_TEST_URL'))
    parser.add_argument('--token', default=os.environ.get('BENCH_TEST_TOKEN'))
    parser.add_argument('--review-file', default=os.environ.get('BENCH_REVIEW_FILE'))
    parser.add_argument('--output', type=Path, default=Path('/tmp/llm-bench-dashboard-e2e'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            snapshot_checks(browser, args.output)
            if args.isolated:
                isolated_checks(browser, args, args.output)
            elif not args.snapshot_only:
                live_checks(browser, args, args.output)
        finally:
            browser.close()
    print(f'Screenshots/downloads: {args.output}')


if __name__ == '__main__':
    main()
