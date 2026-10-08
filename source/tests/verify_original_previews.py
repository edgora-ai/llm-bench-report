#!/usr/bin/env python3
"""Real original-preview acceptance, not a benchmark run or isolation canary.

Run --help first. Requires Python Playwright, Pillow and NumPy (the strict
builder audit imports NumPy); mount trusted source and the audited site read-only and
only --output writable. No request routing/aborts, API replacements, fabricated
packages, model runs, SQL writes, or original-source modifications are used.
Every approved original is opened through the actual product on HTTP and on
file:// with the network offline. Unmodified source is separately observed on
an isolated local reference origin to distinguish source failures from regressions.
The independent verify_preview_isolation.py owns adversarial isolation testing.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import traceback
from urllib.parse import unquote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
# Only trusted siblings are importable, even when invoked with python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_public_snapshot import audit_html, overflow, require
from make_originals import validate_originals

SVG = '42ec6adce94640f2a79659c7446ea3d3'
CSS = '6c3f48c1c35747369efe292af88de884'
CANVAS = '2f16d8652ca8431cba4accedabb353cb'
WEBGL = '9353cf0252d54a0fb787a7e2227b08c8'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument('--site', type=Path, help='Existing fully audited static-media-v2 directory (never built by this verifier)')
    target.add_argument('--url', help='Actual HTTPS edgora-ai.github.io/llm-bench-report/ report')
    p.add_argument('--output', required=True, type=Path, help='Dedicated writable evidence directory outside the source/site')
    p.add_argument('--viewer-script', type=Path, default=ROOT / 'web/app.js')
    p.add_argument('--preview-runtime', type=Path, default=ROOT / 'web/preview-runtime.js')
    p.add_argument('--engines', nargs='+', choices=['chromium', 'firefox', 'webkit'], default=['chromium', 'firefox', 'webkit'])
    p.add_argument('--scope', choices=['full','interactions'], default='full', help='Full per-original census, or explicitly limited representative/product postdeploy checks')
    p.add_argument('--run-id', action='append', help='Explicit targeted diagnostic rerun; other originals and product matrix are reported untested, never a full census')
    p.add_argument('--offline-network-isolated', action='store_true', help='Local --site only: require actual loopback-only Linux network namespace instead of Playwright offline emulation; not isolation/CSP proof')
    p.add_argument('--timeout-ms', type=int, default=20000)
    p.add_argument('--sample-ms', type=int, default=700, help='Motion sampling interval; at least 300 ms')
    args = p.parse_args(argv)
    require(args.sample_ms >= 300 and args.timeout_ms >= 1000, 'Invalid time budget')
    args.output = args.output.resolve()
    require(args.output != Path('/') and not args.output.is_relative_to(ROOT), 'Output must be dedicated and outside source')
    if args.site:
        args.site = args.site.resolve(strict=True)
        require(not args.output.is_relative_to(args.site) and not args.site.is_relative_to(args.output), 'Output/site must be separate')
    else:
        u = urlsplit(args.url)
        require(u.scheme == 'https' and u.hostname == 'edgora-ai.github.io' and u.port in (None, 443)
                and not u.username and not u.password and u.path.startswith('/llm-bench-report/'), 'Unapproved public URL')
    if args.offline_network_isolated:
        require(args.site is not None,'Network-isolated offline alternative is local --site only')
        verify_network_namespace()
    return args


def verify_network_namespace():
    interfaces={line.split(':',1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:] if ':' in line}
    require(interfaces=={'lo'},'Offline alternative requires a real loopback-only network namespace')
    routes=Path('/proc/net/route').read_text().splitlines()[1:]
    require(not any(line.split()[0]!='lo' for line in routes if line.split()),'Unexpected non-loopback route')
    return {'interfaces':sorted(interfaces),'method':'actual network namespace; no browser API replacement; not CSP proof'}


def set_offline(context,args):
    if args.offline_network_isolated:
        verify_network_namespace()
    else:
        context.set_offline(True)


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    require(json.loads(path.read_text(encoding='utf-8')) == value, 'Evidence readback mismatch')


@contextmanager
def server(files, delay_originals=False):
    """Actual HTTP responses, exact audited bytes. Delay is server-side, not routing."""
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = unquote(urlsplit(self.path).path).lstrip('/') or 'index.html'
            requests.append({'path': name, 'method': 'GET'})
            item = files.get(name)
            if item is None:
                self.send_error(404)
                return
            raw, mime = item
            if delay_originals and name.startswith('originals/'):
                time.sleep(.4)  # Allows real in-flight Stop/close to be observed.
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('X-Verified-Body-SHA256', digest(raw))
            self.send_header('Cache-Control', 'no-store')
            if name == 'offline.html':
                self.send_header('Content-Disposition', 'attachment; filename="offline.html"')
            self.end_headers()
            try:
                self.wfile.write(raw)
                if name.startswith('originals/'):
                    requests.append({'path':name,'sent_bytes':len(raw),'sent_sha256':digest(raw)})
            except (BrokenPipeError, ConnectionResetError):
                requests.append({'path': name, 'cancelled_connection': True})
        def log_message(self, fmt, *args):
            pass  # Passive structured requests are retained above.
    http = ThreadingHTTPServer((os.environ.get('BENCH_VERIFY_BIND', '127.0.0.1'), 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://{http.server_address[0]}:{http.server_port}/', requests
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)


class Observer:
    """Passive only. Never installs init scripts, routes, or browser API shims."""
    def __init__(self, context):
        self.requests, self.responses, self.errors, self.console, self.failures = [], [], [], [], []
        self.package_bodies = []
        self.phase = 'cold'
        context.on('request', self.request)
        context.on('response', self.response)
        context.on('requestfinished', self.finished)
        context.on('requestfailed', lambda r: self.failures.append({'url': r.url, 'error': r.failure, 'phase': self.phase}))
    def request(self, request):
        self.requests.append({'url': request.url, 'method': request.method, 'type': request.resource_type, 'phase': self.phase})
    def response(self, response):
        self.responses.append({'url': response.url, 'status': response.status, 'mime': response.headers.get('content-type', ''),
                               'size':response.headers.get('content-length'), 'encoding':response.headers.get('content-encoding','').strip().lower(),
                               'owned_server_sha256':response.headers.get('x-verified-body-sha256'), 'phase': self.phase})
    def finished(self, request):
        if re.search(r'/originals/[a-f0-9]{64}\.json$', urlsplit(request.url).path):
            try:
                response = request.response()
                raw = response.body()
                self.package_bodies.append({'url':request.url,'sha256':digest(raw),'size':len(raw),
                                            'status':response.status,'mime':response.headers.get('content-type','').split(';')[0]})
            except Exception as error:
                self.package_bodies.append({'url':request.url,'read_error':str(error)[:400]})
    def attach(self, page):
        # Newer Playwright WebErrors identify the emitting page but not its frame.
        # Stack/console source and per-run runtime diagnostics are retained, not suppressed.
        page.on('pageerror', lambda e: self.errors.append({'phase': self.phase, 'message': str(e)[:1200], 'stack': str(e.stack or '')[:2000]}))
        page.on('console', lambda m: self.console.append({'phase': self.phase, 'type': m.type, 'text': m.text[:1000], 'location': m.location}) if m.type in {'error', 'warning'} else None)
    def receipt(self):
        return {'requests': self.requests, 'responses': self.responses, 'javascript_errors': self.errors,
                'console': self.console, 'request_failures': self.failures, 'actual_package_responses':self.package_bodies}


def prepare(playwright, args, report):
    from make_site import verify_site
    trusted = args.viewer_script.read_text(encoding='utf-8')
    runtime = args.preview_runtime.read_text(encoding='utf-8')
    if args.site:
        manifest = verify_site(args.site, args.viewer_script, preview_runtime=args.preview_runtime)
        raw = (args.site / 'index.html').read_bytes()
        files = {name: ((args.site / name).read_bytes(), info['mime']) for name, info in manifest['files'].items()}
    else:
        client = playwright.request.new_context()
        try:
            response = client.get(args.url, max_redirects=0, timeout=120000)
            require(response.status == 200 and 'text/html' in response.headers.get('content-type', ''), 'Public HTML fetch failed')
            raw = response.body()
            preliminary = audit_html(raw.decode('utf-8'), trusted, runtime)
            require(preliminary.get('format') == 'static-media-v2' and preliminary.get('transport') == 'external', 'Expected external v2')
            files = {'index.html': (raw, 'text/html')}
            entries = {preliminary['offline']['path']: {**preliminary['offline'], 'mime': 'text/html'}}
            entries.update({d['package']['path']: {**d['package'], 'mime': 'application/json'} for d in preliminary['originals'].values() if d['package']})
            for name, item in entries.items():
                response = client.get(urljoin(args.url, name), max_redirects=0, timeout=120000)
                body = response.body()
                require(response.status == 200 and response.headers.get('content-type', '').split(';')[0] == item['mime'], 'Public resource status/MIME failure: ' + name)
                require(len(body) == item['size'] and digest(body) == item['sha256'], 'Public resource hash/size failure: ' + name)
                files[name] = (body, item['mime'])
        finally:
            client.dispose()
    data = audit_html(raw.decode('utf-8'), trusted, runtime)
    require(data.get('format') == 'static-media-v2' and data.get('transport') == 'external', 'Only external v2 is accepted')
    packages = validate_originals(data, {name: pair[0] for name, pair in files.items()})
    offline_raw = files['offline.html'][0]
    require(digest(offline_raw) == data['offline']['sha256'] and len(offline_raw) == data['offline']['size'], 'Offline bytes mismatch')
    inline = audit_html(offline_raw.decode('utf-8'), trusted, runtime)
    inline_packages = validate_originals(inline)
    require(inline_packages == packages and inline['runs'] == data['runs'] and inline['tasks'] == data['tasks'], 'HTTP/offline original or history mismatch')
    for run_id, descriptor in data['originals'].items():
        require({k:v for k,v in descriptor.items() if k != 'package'} == {k:v for k,v in inline['originals'][run_id].items() if k != 'package'}, 'Offline descriptor mismatch')
        if descriptor['package']:
            require(base64.b64decode(inline['originals'][run_id]['package']['base64'], validate=True) == files[descriptor['package']['path']][0], 'Offline package is not byte-identical')
    runs = data['runs']
    entries = [r for r in runs if data['originals'][r['id']]['entrypoint']]
    counts = {'runs': len(runs), 'benchmark_runs': sum(r.get('purpose') == 'benchmark' for r in runs),
              'reviews': sum(len(r.get('reviews', [])) for r in runs), 'entrypoints': len(entries),
              'benchmark_entrypoints': sum(r.get('purpose') == 'benchmark' for r in entries),
              'entrypoint_statuses': dict(Counter(r['status'] for r in entries)),
              'approved_packages': len(packages), 'multifile': sum(len(p['files']) > 1 for p in packages.values()),
              'missing_dependencies': sum(bool(p['missing']) for p in packages.values())}
    require((counts['runs'], counts['benchmark_runs'], counts['reviews'], counts['entrypoints'], counts['benchmark_entrypoints']) == (58,35,14,35,23), 'Historical 58/35/14 and 35-entrypoint/23-benchmark facts changed')
    require(counts['entrypoint_statuses'] == {'completed':29, 'failed':5, 'interrupted':1}, 'Failed/interrupted delivered sources dropped')
    require(counts['multifile'] == 9, 'All nine complete multifile originals are required, not a sample')
    require(all(run_id in packages for run_id in (SVG,CSS,CANVAS,WEBGL)), 'Required real representative originals not approved/packaged')
    report.update(counts=counts, source={'html_sha256':digest(raw), 'html_bytes':len(raw), 'offline_sha256':digest(offline_raw),
                                      'trusted_viewer_sha256':digest(trusted.encode()), 'trusted_runtime_sha256':digest(runtime.encode())})
    # This exact downloaded/verified HTML is opened as file://, never set_content.
    offline_path = args.output / 'actual-offline.html'
    offline_path.write_bytes(offline_raw)
    require(offline_path.read_bytes() == offline_raw, 'Offline file readback mismatch')
    reference = {}
    for run_id, package in packages.items():
        for name, info in package['files'].items():
            reference[f'{run_id}/{name}'] = (base64.b64decode(info['base64'], validate=True), info['mime'])
    return data, packages, files, reference, offline_path


def wait_loaded(page, run_id):
    # Full-width launch keeps the closed comparison's stopped panel in the DOM.
    # Resolve only the visible panel in an open dialog, never that stale duplicate.
    selector = f'dialog[open] [data-original-run="{run_id}"]'
    page.wait_for_function("selector=>{const panels=[...document.querySelectorAll(selector)].filter(p=>p.getClientRects().length);return panels.length===1&&['loaded','error','unavailable'].includes(panels[0].dataset.previewState)}", arg=selector)
    panel = page.locator(selector + ':visible')
    require(panel.get_attribute('data-preview-state') == 'loaded', 'Packaging/runtime launch failure: ' + panel.inner_text()[:600])
    outer = panel.locator(f'[data-preview-host="{run_id}"] iframe').element_handle().content_frame()
    require(outer is not None, 'Missing trusted shell')
    outer.wait_for_selector('iframe', state='attached')
    inner = outer.locator('iframe').element_handle().content_frame()
    require(inner is not None, 'Missing original browsing context')
    inner.wait_for_load_state('domcontentloaded')
    return inner


def gallery_run(page, run_id):
    page.locator('[data-view="gallery"]').click()
    page.locator('#reset-filters').click()
    page.locator('#filters details').evaluate_all('items=>items.forEach(d=>d.open=true)')
    page.locator('[name="task_id"]').select_option('')
    page.locator('[name="purpose"]').select_option('')
    page.locator('[name="q"]').fill(run_id)
    # Submit the real form to commit its debounce before opening the preview.
    # A retry may also mention this ID; do not assume search yields one attempt.
    page.locator('[name="q"]').press('Enter')
    page.wait_for_function("document.querySelector('#workspace').getAttribute('aria-busy')==='false'")
    button = page.locator(f'.evidence-card [data-run-original="{run_id}"]')
    button.wait_for(state='visible')
    require(button.inner_text() == '运行原作' and button.is_enabled(), 'Delivered original entry is not actionable: ' + run_id)
    button.click()
    page.locator('#original-dialog').wait_for(state='visible')
    return wait_loaded(page, run_id)


def frame_wait(frame, predicate, arg=None, timeout=20000):
    # Playwright's wait_for_function uses an in-page eval poller, which the real
    # original CSP correctly rejects. Inspector evaluate reads the DOM directly;
    # never add unsafe-eval or a page-side helper to make test assertions work.
    deadline=time.monotonic()+timeout/1000
    while time.monotonic()<deadline:
        if frame.evaluate(predicate,arg):
            return
        frame.page.wait_for_timeout(50)
    raise AssertionError('Original state did not satisfy predicate: '+predicate[:200])


def screenshot(page, frame, path):
    if frame == page.main_frame:
        return page.screenshot(path=str(path), animations='allow')
    # Element screenshots wait for animation stability and may therefore never
    # finish on a genuinely animated original. Capture its visible frame pixels
    # directly without pausing animations or changing the original's APIs.
    element=frame.frame_element()
    element.evaluate('e=>e.scrollIntoView({block:"center",inline:"center"})')
    box=element.bounding_box()
    require(box is not None,'Original frame has no visible rectangle')
    size=page.viewport_size
    x,y=max(0,box['x']),max(0,box['y'])
    width=min(box['x']+box['width'],size['width'])-x
    height=min(box['y']+box['height'],size['height'])-y
    require(width>20 and height>20,'Original frame clipped to an unobservable rectangle')
    # Capture the compositor's normal viewport then crop locally. Chromium's
    # clipped capture can stall on a continuously rendering scaled WebGL iframe.
    from PIL import Image
    image=Image.open(io.BytesIO(page.screenshot(animations='allow')))
    image=image.crop((round(x),round(y),round(x+width),round(y+height)))
    buffer=io.BytesIO();image.save(buffer,format='PNG');raw=buffer.getvalue()
    path.write_bytes(raw)
    return raw


def pixel_difference(a, b):
    from PIL import Image, ImageChops
    first, second = Image.open(io.BytesIO(a)).convert('RGB'), Image.open(io.BytesIO(b)).convert('RGB')
    require(first.size == second.size, 'Motion screenshots have different dimensions')
    diff = ImageChops.difference(first, second)
    changed = sum(1 for pixel in diff.getdata() if max(pixel) > 8)
    return {'changed_pixels': changed, 'pixels':first.width*first.height, 'ratio':changed/(first.width*first.height),
            'sha256_before':digest(a), 'sha256_after':digest(b)}


def observe_motion(page, frame, prefix, args):
    page.wait_for_timeout(args.sample_ms)
    first = screenshot(page, frame, args.output / (prefix + '-before.png'))
    page.wait_for_timeout(args.sample_ms)
    second = screenshot(page, frame, args.output / (prefix + '-after.png'))
    result = pixel_difference(first, second)
    result['visible_change'] = result['changed_pixels'] > 10
    result['dom'] = frame.evaluate("""() => ({title:document.title, width:innerWidth,height:innerHeight,
        bodyText:document.body?.innerText.slice(0,160),svg:document.querySelectorAll('svg').length,
        canvases:[...document.querySelectorAll('canvas')].map(c=>({width:c.width,height:c.height})),
        animations:document.getAnimations().map(a=>({playState:a.playState,currentTime:a.currentTime})).slice(0,20),
        reducedMotion:matchMedia('(prefers-reduced-motion:reduce)').matches})""")
    result['screenshots'] = [prefix + '-before.png', prefix + '-after.png']
    return result


def check_viewport(page, frame, run_id, report):
    results = []
    for choice, width, height in [('desktop',1440,900),('mobile',400,800)]:
        page.locator(f'[data-preview-viewport="{run_id}"]').select_option(choice)
        frame_wait(frame,'size=>innerWidth===size[0]&&innerHeight===size[1]', arg=[width,height])
        actual = frame.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')
        text = page.locator(f'[data-original-run="{run_id}"] .preview-dimensions').inner_text()
        require(f'{width} × {height}' in text, 'UI viewport label differs from original frame')
        results.append({'option':choice, 'actual':actual, 'display':text})
        overflow(page, 'original-' + choice, report)
    page.locator(f'[data-preview-viewport="{run_id}"]').select_option('fit')
    actual = frame.evaluate('({width:innerWidth,height:innerHeight})')
    require(actual['width'] > 0 and actual['height'] > 0, 'Fit has empty viewport')
    return results


def controls(page, frame, run_id, prefix, args, touch=False):
    """Known controls of audited actual originals; no generic button-count success."""
    events = []
    def click(selector):
        control = frame.locator(selector)
        if touch:
            control.tap()
        else:
            control.click()
    def changed(label, getter, action):
        before = frame.evaluate(getter)
        action()
        frame_wait(frame,'(p)=>JSON.stringify((' + getter + ')())!==JSON.stringify(p)', arg=before)
        after = frame.evaluate(getter)
        events.append({'action':label, 'before':before, 'after':after})
    if run_id == SVG:
        getter = '()=>document.body.classList.contains("is-paused")'
        changed('pause', getter, lambda:click('#play-toggle'))
        require(frame.evaluate(getter), 'SVG original pause state not set')
        changed('resume', getter, lambda:click('#play-toggle'))
        require(not frame.evaluate(getter), 'SVG original did not resume')
        changed('speed', '()=>document.querySelector("#pace-value").textContent', lambda:frame.locator('#pace').press('End'))
        changed('scene', '()=>document.querySelector(".scene-number").textContent', lambda:click('#golden-hour'))
        click('#bell-button')
        frame_wait(frame,'document.querySelector(".scene").classList.contains("is-ringing")')
        frame_wait(frame,'parseFloat(getComputedStyle(document.querySelector(".bell-bubble")).opacity)>.5')
        events.append({'action':'bell', 'visible_bubble_opacity':frame.locator('.bell-bubble').evaluate('e=>getComputedStyle(e).opacity')})
    elif run_id == CSS:
        getter = '()=>document.body.classList.contains("is-paused")'
        changed('pause',getter,lambda:click('#toggle-motion'))
        require(frame.evaluate(getter), 'CSS pause state not set')
        running = frame.evaluate('document.getAnimations().filter(a=>a.playState==="running").length')
        events.append({'action':'paused CSS animation states', 'running':running})
        changed('resume',getter,lambda:click('#toggle-motion'))
        require(not frame.evaluate(getter), 'CSS resume state not set')
        click('#ring-bell')
        frame_wait(frame,'parseFloat(getComputedStyle(document.querySelector("#bell-bubble")).opacity)>.3')
        events.append({'action':'bell', 'visible_bubble_opacity':frame.locator('#bell-bubble').evaluate('e=>getComputedStyle(e).opacity')})
    elif run_id == CANVAS:
        getter = '()=>document.querySelector("#playButton").getAttribute("aria-label")'
        changed('pause',getter,lambda:click('#playButton'))
        require(frame.evaluate(getter) == '继续模拟', 'Canvas pause state not set')
        stopped = observe_motion(page, frame, prefix + '-paused', args)
        require(not stopped['visible_change'], 'Canvas2D pixels continue moving while paused')
        events.append({'action':'Canvas2D paused pixels', 'motion':stopped})
        changed('resume',getter,lambda:click('#playButton'))
        moving = observe_motion(page, frame, prefix + '-resumed', args)
        require(moving['visible_change'], 'Canvas2D resumed pixels do not change')
        events.append({'action':'Canvas2D resumed pixels', 'motion':moving})
        changed('speed','()=>document.querySelector("#speedValue").textContent',lambda:frame.locator('#speed').press('End'))
    elif run_id == WEBGL:
        # The original deliberately keeps gl/state inside an IIFE. Read its
        # existing, linked shader program and uniforms; never expose/patch its
        # closure. A newly created context could not have CURRENT_PROGRAM set.
        gpu = frame.evaluate("""()=>{const g=document.querySelector('#gl').getContext('webgl');
            const p=g&&g.getParameter(g.CURRENT_PROGRAM);return {context:g instanceof WebGLRenderingContext,
            linked:!!p&&g.getProgramParameter(p,g.LINK_STATUS),lost:g?g.isContextLost():true,
            renderer:g&&g.getParameter(g.RENDERER),version:g&&g.getParameter(g.VERSION)}}""")
        require(gpu['context'] and gpu['linked'] and not gpu['lost'], 'Actual original WebGL program unavailable (not accepted as Canvas fallback)')
        events.append({'action':'actual linked original WebGL program', **gpu})
        def uniform(name):
            return "()=>{const g=document.querySelector('#gl').getContext('webgl'),p=g.getParameter(g.CURRENT_PROGRAM),v=g.getUniform(p,g.getUniformLocation(p,"+json.dumps(name)+"));return ArrayBuffer.isView(v)?Array.from(v):v}"
        time_getter=uniform('uTime')
        button_getter='()=>document.querySelector("#bPause").textContent'
        changed('pause',button_getter,lambda:click('#bPause'))
        require(frame.evaluate(button_getter)=='继续','WebGL pause not applied')
        page.wait_for_timeout(100)
        before=frame.evaluate(time_getter)
        page.wait_for_timeout(args.sample_ms)
        require(frame.evaluate(time_getter)==before,'Actual shader time advances while paused')
        events.append({'action':'paused shader uniform','uTime':before})
        changed('resume',button_getter,lambda:click('#bPause'))
        frame_wait(frame,'before=>('+time_getter+')()>before',arg=before)
        changed('speed','()=>document.querySelector("#vSpeed").textContent',lambda:frame.locator('#sSpeed').press('End'))
        canvas = frame.locator('#gl')
        canvas.scroll_into_view_if_needed()
        box = canvas.bounding_box()
        require(box and box['width'] > 100, 'WebGL canvas not visible')
        x,y = box['x']+box['width']*.35,box['y']+box['height']*.5
        camera_getter=uniform('uCam');before=frame.evaluate(camera_getter)
        page.mouse.move(x,y);page.mouse.down();page.mouse.move(x+60,y+25,steps=8);page.mouse.up()
        frame_wait(frame,'before=>JSON.stringify(('+camera_getter+')())!==JSON.stringify(before)',arg=before)
        events.append({'action':'drag','shader_uniform':'uCam','before':before,'after':frame.evaluate(camera_getter)})
        zoom_getter=uniform('uZoom');before=frame.evaluate(zoom_getter)
        page.mouse.wheel(0,150)
        frame_wait(frame,'before=>('+zoom_getter+')()!==before',arg=before)
        events.append({'action':'zoom','shader_uniform':'uZoom','before':before,'after':frame.evaluate(zoom_getter)})
    return events


def all_originals(browser, url, reference_url, data, packages, args, report, engine, mode):
    for run in data['runs']:
        run_id = run['id']
        if run_id not in packages:
            continue
        selected=args.run_id or ([SVG,CSS,CANVAS,WEBGL] if args.scope=='interactions' else None)
        if selected is not None and run_id not in selected:
            continue
        prefix = f'{engine}-{mode}-{run_id}'
        row = {'engine':engine,'transport':mode,'run_id':run_id,'generation_status':run['status'],
               'purpose':run.get('purpose'),'package_sha256':data['originals'][run_id]['package']['sha256'],
               'files':{name:{k:v for k,v in info.items() if k != 'base64'} for name,info in packages[run_id]['files'].items()},
               'missing_dependencies':packages[run_id]['missing'], 'status':'running'}
        report['originals'].append(row)
        context = browser.new_context(viewport={'width':1440,'height':900}, reduced_motion='no-preference')
        context.set_default_timeout(args.timeout_ms)
        observer = Observer(context)
        page = context.new_page(); observer.attach(page)
        try:
            if mode == 'file':
                set_offline(context,args)
            page.goto(url, wait_until='networkidle',timeout=120000)
            require(page.locator('iframe').count() == 0, 'Original frame before explicit action')
            require(not any('/originals/' in r['url'] for r in observer.requests), 'Original request before explicit action')
            require(not observer.errors and not [m for m in observer.console if m['type']=='error'], 'Report errors before original starts')
            observer.phase = run_id
            observer.phase=run_id+':launch'
            frame = gallery_run(page,run_id)
            observer.phase=run_id+':motion'
            # Observe the product's default fit viewport; representatives below
            # additionally exercise both explicit fixed sizes in the real frame.
            row['launch'] = 'loaded-only-not-a-pass'
            row['motion'] = observe_motion(page,frame,prefix,args)
            if run_id in (SVG,CSS,CANVAS,WEBGL):
                row['controls'] = controls(page,frame,run_id,prefix,args)
                row['viewports'] = check_viewport(page,frame,run_id,report)
            row['diagnostics'] = page.locator('.preview-diagnostics li').all_text_contents()
            panel_text=page.locator(f'[data-original-run="{run_id}"]').inner_text()
            require(f'生成会话：{run.get("generation_status") or run["status"]}' in panel_text,'Original generation failure/interruption status hidden')
            if packages[run_id]['missing']:
                require(all(name in panel_text for name in packages[run_id]['missing']) and '缺少依赖' in panel_text,
                        'Loaded partial original lost its persistent missing-dependency explanation')
                row['visible_missing_dependencies']=list(packages[run_id]['missing'])
            expected_url = urljoin(url,data['originals'][run_id]['package']['path'])
            original_requests = [r for r in observer.requests if '/originals/' in r['url']]
            require((mode=='file' and not original_requests) or (mode=='http' and original_requests and all(r['url']==expected_url and r['method']=='GET' for r in original_requests)), 'Preview requested an unrelated original package')
            if mode == 'file':
                require(not [r for r in observer.requests if urlsplit(r['url']).scheme in {'http','https','ws','wss'}], 'Offline original attempted network')
            else:
                info=data['originals'][run_id]['package']
                responses=[r for r in observer.responses if r['url']==expected_url]
                # Content-Length describes encoded wire bytes when compression
                # is used. Decoded browser bytes still require exact size+SHA below.
                require(responses and all(r['status']==200 and r['mime'].split(';')[0]=='application/json'
                        and (r['size'] is None or r['encoding'] not in {'','identity'} or int(r['size'])==info['size'])
                        for r in responses), 'Actual package status/MIME/identity-length mismatch')
                if args.site:
                    require(all(r['owned_server_sha256']==info['sha256'] for r in responses), 'Owned HTTP server sent unexpected package bytes')
                for item in observer.package_bodies:
                    require('read_error' not in item and item['sha256']==info['sha256'] and item['size']==info['size'],
                            'Retained inspector body differs from approved bytes')
                independent=None
                if not observer.package_bodies:
                    # A separate, explicit verification GET after user launch,
                    # through untouched native fetch/crypto APIs. This is not the
                    # product request and is never counted as cold UI traffic.
                    observer.phase=run_id+':independent-body-audit'
                    independent=page.evaluate("""async path=>{const r=await fetch(path,{credentials:'omit',redirect:'error',cache:'no-store'});
                        const bytes=await r.arrayBuffer(),hash=await crypto.subtle.digest('SHA-256',bytes);
                        return {status:r.status,mime:r.headers.get('content-type').split(';')[0],size:bytes.byteLength,
                        sha256:Array.from(new Uint8Array(hash),v=>v.toString(16).padStart(2,'0')).join('')}}""",info['path'])
                    require(independent=={'status':200,'mime':'application/json','size':info['size'],'sha256':info['sha256']},
                            'Independent actual browser fetch/crypto package proof mismatch')
                row['package_integrity_evidence']={
                    'preflight_sha256':info['sha256'], 'preflight_size':info['size'],
                    'runtime_verified_before_actual_original_execution':True,
                    'browser_inspector_body_retained':bool(observer.package_bodies),
                    'owned_http_sent_hash':responses[0]['owned_server_sha256'],
                    'independent_browser_fetch_crypto':independent,
                    'note':'Original Fetch may report ERR_ABORTED at nested srcdoc navigation; preserve that diagnostic. Independently re-read and hash bytes through native browser APIs rather than interpreting requestfinished as execution proof.'}
                allowed={url,expected_url}|{urljoin(url,name) for name in data['assets']}
                declared_missing={urljoin(url,name) for name in packages[run_id]['missing']}
                blocked_missing={r['url'] for r in observer.failures if r['url'] in declared_missing
                                 and r['error'] in {'csp','net::ERR_BLOCKED_BY_CSP'}}
                require(not any(r['url'] in blocked_missing for r in observer.responses),'Declared missing resource unexpectedly reached HTTP')
                row['declared_missing_csp_blocks']=sorted(blocked_missing)
                require(all(r['url'] in allowed|blocked_missing and r['method']=='GET' for r in observer.requests
                            if urlsplit(r['url']).scheme in {'http','https','ws','wss'}), 'Unexpected original/report network request')
            # Stop/restart uses real buttons and must destroy the old frame.
            observer.phase=run_id+':stop'
            page.locator(f'[data-preview-stop="{run_id}"]').click()
            require(frame.is_detached() and page.locator('iframe').count()==0, 'Stop retained executable original')
            observer.phase=run_id+':restart'
            page.locator(f'[data-preview-restart="{run_id}"]').click()
            restarted = wait_loaded(page,run_id)
            require(restarted != frame, 'Restart reused destroyed original frame')
            observer.phase=run_id+':close'
            page.locator('[data-close="original-dialog"]').click()
            require(restarted.is_detached() and page.locator('iframe').count()==0, 'Close retained executable original')
            row['lifecycle'] = {'stop':'destroyed','restart':'new-context','close':'destroyed'}
            row['status'] = 'observed'
        except Exception as error:
            row['status'] = 'fail'
            row['failure'] = f'{type(error).__name__}: {str(error)[:1200]}'
            row['traceback'] = traceback.format_exc(limit=5)
            try:
                page.screenshot(path=str(args.output/(prefix+'-failure.png')))
                row['failure_panels']=page.locator('[data-original-run]').all_text_contents()
            except Exception as evidence_error:
                row['failure_evidence_error']=str(evidence_error)[:300]
        finally:
            row['browser'] = observer.receipt()
            context.close()
        save_json(args.output/(prefix+'.json'),row)
        # Independent unadapted delivery at the observed viewport, same bytes,
        # own origin/context, no report credentials or loader adaptation.
        observed_size=row.get('motion',{}).get('dom',{'width':1440,'height':900})
        reference_context = browser.new_context(viewport={k:observed_size[k] for k in ('width','height')},reduced_motion='no-preference')
        reference_context.set_default_timeout(args.timeout_ms)
        baseline = Observer(reference_context)
        reference_page = reference_context.new_page(); baseline.attach(reference_page)
        try:
            reference_page.goto(urljoin(reference_url,run_id+'/index.html'),wait_until='networkidle',timeout=120000)
            row['reference'] = observe_motion(reference_page,reference_page.main_frame,prefix+'-unadapted',args)
            if row['status'] != 'fail':
                if row['motion']['visible_change']:
                    row['classification'] = 'visible-motion-observed'
                elif row['reference']['visible_change']:
                    row['classification'] = 'packaging-policy-or-runtime-regression'
                    row['status'] = 'fail'
                elif packages[run_id]['missing']:
                    row['classification'] = 'existing-missing-dependencies-no-motion'
                else:
                    row['classification'] = 'no-motion-in-unadapted-original-either'
                source_errors = [e['message'] for e in baseline.errors]
                novel = [e for e in observer.errors if e['message'] not in source_errors]
                if novel:
                    row['novel_preview_errors'] = novel
                    row['status'] = 'fail'
                    row['classification'] = 'packaging-policy-or-runtime-error-not-seen-in-reference'
        except Exception as error:
            row['reference_failure'] = f'{type(error).__name__}: {str(error)[:600]}'
            row['status'] = 'fail'
        finally:
            row['reference_browser'] = baseline.receipt()
            reference_context.close()
        save_json(args.output / (prefix+'.json'),row)
        print(json.dumps({'engine':engine,'transport':mode,'run':run_id,'status':row['status'],'classification':row.get('classification'),'failure':row.get('failure')},ensure_ascii=False),flush=True)


def cold_and_matrix(browser, url, args, report, engine, mode):
    for phone in (False,True):
        for theme in ('light','dark'):
            context = browser.new_context(viewport={'width':400 if phone else 1440,'height':850 if phone else 900},
                                          has_touch=phone,color_scheme=theme,reduced_motion='reduce' if phone else 'no-preference')
            context.set_default_timeout(args.timeout_ms)
            observer = Observer(context)
            page=context.new_page();observer.attach(page)
            row={'engine':engine,'transport':mode,'phone':phone,'theme':theme,'status':'running'}
            report['matrix'].append(row)
            try:
                if mode=='file':set_offline(context,args)
                page.goto(url,wait_until='networkidle',timeout=120000)
                buttons=page.locator('.evidence-card [data-run-original]').evaluate_all("es=>es.filter(e=>{const b=e.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight&&b.left>=0&&b.right<=innerWidth&&!e.disabled&&e.textContent==='运行原作'}).map(e=>e.dataset.runOriginal)")
                require(buttons,'First viewport has no fully visible actionable Run original button')
                require(page.locator('iframe').count()==0 and not any('/originals/' in r['url'] for r in observer.requests),'Cold original work not lazy')
                overflow(page,f'{engine}-{mode}-cold-{phone}-{theme}',report)
                # Toggle through actual product control; initial system theme may be implicit.
                page.locator('#theme-toggle').click()
                page.locator('#theme-toggle').click()
                frame=gallery_run(page,SVG)
                row['motion']=observe_motion(page,frame,f'{engine}-{mode}-{phone}-{theme}-matrix',args)
                # Reduced motion may intentionally start paused. Resume via the original button.
                if frame.evaluate('document.body.classList.contains("is-paused")'):
                    frame.locator('#play-toggle').tap() if phone else frame.locator('#play-toggle').click()
                row['controls']=controls(page,frame,SVG,f'{engine}-{mode}-matrix',args,touch=phone)
                overflow(page,f'{engine}-{mode}-original-{phone}-{theme}',report)
                page.locator('[data-close="original-dialog"]').click()
                require(frame.is_detached() and page.locator('iframe').count()==0,'Matrix close retained original')
                require(not observer.errors,'Matrix JavaScript error')
                row['status']='pass'
            except Exception as error:
                row['status']='fail';row['failure']=f'{type(error).__name__}: {str(error)[:900]}'
            finally:
                row['browser']=observer.receipt();context.close()


def comparison(browser,url,data,args,report,engine,mode):
    context=browser.new_context(viewport={'width':1440,'height':900},reduced_motion='no-preference')
    context.set_default_timeout(args.timeout_ms)
    observer=Observer(context);page=context.new_page();observer.attach(page)
    row={'engine':engine,'transport':mode,'status':'running'};report['comparisons'].append(row)
    try:
        if mode=='file':set_offline(context,args)
        page.goto(url,wait_until='networkidle',timeout=120000)
        page.locator('[data-view="gallery"]').click();page.locator('#reset-filters').click()
        run=next(r for r in data['runs'] if r['id']==SVG)
        page.locator(f'#task-tabs [data-task-id="{run["task_id"]}"]').click()
        # The comparison toolbar appears only after the first actual selection.
        page.locator(f'.evidence-card [data-select-run="{SVG}"]').click()
        page.locator('#mixed-comparison').check()
        page.locator(f'.evidence-card [data-select-run="{CSS}"]').click()
        page.locator('#compare-button').click();page.locator('#compare-mode').select_option('original')
        require(page.locator('iframe').count()==0,'Comparison auto-started before explicit pair action')
        page.locator('#start-original-pair').click()
        first,second=wait_loaded(page,SVG),wait_loaded(page,CSS)
        require(len([f for f in page.frames if f.parent_frame])==4,'Double comparison did not create exactly two nested contexts')
        before=second.evaluate('document.body.classList.contains("is-paused")')
        first.locator('#play-toggle').click()
        require(first.evaluate('document.body.classList.contains("is-paused")'),'A pause did not change A')
        require(second.evaluate('document.body.classList.contains("is-paused")')==before,'A pause changed B')
        second.locator('#toggle-motion').click()
        require(second.evaluate('document.body.classList.contains("is-paused")'),'B pause did not change B')
        first.locator('#play-toggle').click()
        require(not first.evaluate('document.body.classList.contains("is-paused")') and second.evaluate('document.body.classList.contains("is-paused")'),'A resume affected paused B')
        row['independent_controls']='A pause/B unchanged; B pause/A resume/B still paused'
        row['A_motion']=observe_motion(page,first,f'{engine}-{mode}-pair-A',args)
        row['B_paused']=observe_motion(page,second,f'{engine}-{mode}-pair-B-paused',args)
        require(row['A_motion']['visible_change'] and not row['B_paused']['visible_change'],'Pair visual state contradicts independent controls')
        row['viewport']=check_viewport(page,first,SVG,report)
        page.locator(f'[data-preview-stop="{SVG}"]').click()
        require(first.is_detached() and not second.is_detached(),'Stopping A stopped B or retained A')
        page.locator(f'[data-preview-restart="{SVG}"]').click()
        first=wait_loaded(page,SVG)
        require(not second.is_detached() and second.evaluate('document.body.classList.contains("is-paused")'),
                'Authorized pair restart destroyed or changed the sibling original')
        page.locator('#start-original-pair').click();first,second=wait_loaded(page,SVG),wait_loaded(page,CSS)
        page.set_viewport_size({'width':400,'height':850})
        # Viewport emulation completes before the native matchMedia change event.
        # Await that lifecycle transition, then retain the strict zero-frame check.
        page.wait_for_function("()=>{const panels=[...document.querySelectorAll('#compare-content [data-original-run]')];return panels.length===2&&panels.filter(p=>p.getClientRects().length).length===1&&panels.filter(p=>!p.getClientRects().length).every(p=>!p.querySelector('iframe'))}", timeout=args.timeout_ms)
        columns=page.locator('#compare-content [data-original-run]').evaluate_all('es=>es.map(e=>({id:e.dataset.originalRun,visible:!!e.getClientRects().length}))')
        visible=next(e['id'] for e in columns if e['visible'])
        hidden=CSS if visible==SVG else SVG
        require(page.locator(f'[data-preview-host="{hidden}"] iframe').count()==0,'Phone hidden comparison still running')
        order=page.locator('#compare-content [data-original-run]').evaluate_all('es=>es.map(e=>e.dataset.originalRun)')
        index=order.index(hidden)
        page.locator(f'#compare-ab [data-compare-index="{index}"]').click()
        require(page.locator('iframe').count()==0,'Phone A/B switch auto-started or retained hidden frame')
        page.locator(f'[data-preview-start="{hidden}"]').click();mobile=wait_loaded(page,hidden)
        require(page.locator('iframe').count()==1,'Phone must run only one outer frame')
        overflow(page,f'{engine}-{mode}-phone-original-AB',report)
        page.locator(f'[data-original-fullwidth="{hidden}"]').click();single=wait_loaded(page,hidden)
        require(mobile.is_detached() and page.locator('#original-dialog').is_visible(),'Full width did not destroy compare instance')
        require(page.locator('#compare-content iframe').count()==0,'Closed comparison retained an executable frame')
        # Both actual SVG originals put #ride-scene below their phone intro.
        # Scroll their native document; do not alter responsive layout or APIs.
        single.locator('#ride-scene').scroll_into_view_if_needed()
        row['fullwidth_motion']=observe_motion(page,single,f'{engine}-{mode}-fullwidth',args)
        require(row['fullwidth_motion']['visible_change'],'Visible full-width original did not execute/move')
        row['closed_comparison_frames']=0
        page.locator('[data-close="original-dialog"]').click();require(single.is_detached(),'Fullwidth close retained context')
        row['status']='pass'
    except Exception as error:
        row['status']='fail';row['failure']=f'{type(error).__name__}: {str(error)[:1200]}'
    finally:
        row['browser']=observer.receipt();context.close()


def cancellation(browser,url,args,report,engine,mode):
    context=browser.new_context(viewport={'width':1440,'height':900})
    context.set_default_timeout(args.timeout_ms)
    page=context.new_page();row={'engine':engine,'transport':mode,'status':'running'};report['cancellations'].append(row)
    try:
        if mode=='file':set_offline(context,args)
        page.goto(url,wait_until='networkidle',timeout=120000)
        frame=gallery_run(page,SVG)
        page.locator(f'[data-preview-stop="{SVG}"]').click()
        require(frame.is_detached(),'Initial stop failed')
        page.locator(f'[data-preview-start="{SVG}"]').click(no_wait_after=True)
        row['cancel_state']=page.locator(f'[data-original-run="{SVG}"]').get_attribute('data-preview-state')
        page.locator('[data-close="original-dialog"]').click()
        page.wait_for_timeout(1200)
        require(page.locator('iframe').count()==0,'Closed in-flight launch resurrected a frame')
        row['inflight_observed']=row['cancel_state'] in {'fetching','verifying','loading'}
        if mode=='http':require(row['inflight_observed'],'In-flight cancellation not observed; not accepted as tested')
        # Genuine navigation emits pagehide; back must not resurrect an instance.
        gallery_run(page,SVG)
        page.goto('about:blank')
        page.go_back(wait_until='networkidle',timeout=120000)
        require(page.locator('iframe').count()==0,'Pagehide/back resurrected a preview')
        row['pagehide']='real navigation and back; zero frames'
        row['status']='pass'
    except Exception as error:
        row['status']='fail';row['failure']=f'{type(error).__name__}: {str(error)[:900]}'
    finally:context.close()


def main(argv=None):
    args=arguments(argv);args.output.mkdir(parents=True,exist_ok=True)
    report={'status':'running','scope':'actual original product acceptance, not benchmark scoring or isolation proof',
            'offline_environment':verify_network_namespace() if args.offline_network_isolated else {'method':'Playwright native offline emulation'},
            'engines':{},'originals':[],'matrix':[],'comparisons':[],'cancellations':[],'overflow_checks':[],
            'limitations':['Motion is observed visible change, not fidelity/performance scoring.',
                           'Reference originals are byte-identical but unsandboxed on an isolated local origin.',
                           'Adversarial isolation is covered separately by verify_preview_isolation.py.']}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            data,packages,files,reference,offline_path=prepare(playwright,args,report)
            selected=set(args.run_id or ([SVG,CSS,CANVAS,WEBGL] if args.scope=='interactions' else packages))
            require(selected<=set(packages),'Requested diagnostic original is not an approved package')
            report['selected_originals']=sorted(selected)
            report['untested_originals']=sorted(set(packages)-selected)
            report['requested_scope']='targeted-diagnostic' if args.run_id else args.scope
            with server(reference) as (reference_url,reference_log):
                @contextmanager
                def target():
                    if args.url:yield args.url,[]
                    else:
                        with server(files,delay_originals=True) as current:yield current
                with target() as (url,http_log):
                    for engine in ('chromium','firefox','webkit'):
                        if engine not in args.engines:
                            report['engines'][engine]={'status':'untested','reason':'Explicitly excluded by --engines'};continue
                        browser_type=getattr(playwright,engine)
                        if not Path(browser_type.executable_path).is_file():
                            report['engines'][engine]={'status':'untested','reason':'Browser executable not installed in immutable test environment'};continue
                        try:
                            browser=browser_type.launch(headless=True)
                        except Exception as error:
                            report['engines'][engine]={'status':'untested','reason':str(error)[:600]};continue
                        report['engines'][engine]={'status':'tested','version':browser.version}
                        try:
                            for mode,target_url in [('http',url),('file',offline_path.as_uri())]:
                                all_originals(browser,target_url,reference_url,data,packages,args,report,engine,mode)
                                if not args.run_id:
                                    cold_and_matrix(browser,target_url,args,report,engine,mode)
                                    comparison(browser,target_url,data,args,report,engine,mode)
                                    cancellation(browser,target_url,args,report,engine,mode)
                                save_json(args.output/'verification.json',report)
                        finally:browser.close()
                    report['http_server_requests']=http_log;report['reference_server_requests']=reference_log
            require(any(e['status']=='tested' for e in report['engines'].values()),'No browser tested')
            required=len(selected)*2*sum(e['status']=='tested' for e in report['engines'].values())
            require(len(report['originals'])==required,'Missing per-original HTTP/offline receipt')
            failures=[row for group in ('originals','matrix','comparisons','cancellations') for row in report[group] if row['status']=='fail']
            report['failure_count']=len(failures)
            report['status']='fail' if failures else 'pass'
            report['coverage']='tested engines and explicitly selected scope only; see untested_originals and engine statuses'
            if args.run_id:
                report['untested_product_matrix']='Targeted diagnostic run omits phone/theme/comparison/cancellation matrix'
    except Exception as error:
        report['status']='fail';report['failure']=f'{type(error).__name__}: {str(error)[:1500]}'
    save_json(args.output/'verification.json',report)
    print(json.dumps({key:report[key] for key in ('status','requested_scope','selected_originals','untested_originals','counts','engines','failure_count','failure') if key in report},ensure_ascii=False),flush=True)
    return 0 if report['status']=='pass' else 1


if __name__=='__main__':
    raise SystemExit(main())
