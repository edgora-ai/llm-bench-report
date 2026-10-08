#!/usr/bin/env python3
"""Passive-canary proof of double opaque srcdoc isolation (no request interception)."""
import argparse
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('chromium', 'firefox', 'webkit'), default='chromium')
    parser.add_argument('--runtime', help='Optional trusted preview-runtime.js path for integration checks')
    return parser.parse_args()


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fixture(html, assets=None, missing=None, urls=None):
    entry = html.encode('utf-8')
    files = {'index.html': {'mime': 'text/html', 'size': len(entry), 'sha256': sha(entry), 'base64': b64(entry)}}
    patches = []
    for path, (mime, text) in (assets or {}).items():
        raw = text.encode('utf-8')
        files[path] = {'mime': mime, 'size': len(raw), 'sha256': sha(raw), 'base64': b64(raw)}
        original = (urls or {}).get(path, path)
        marker = original.encode('utf-8')
        start = entry.index(marker)
        patches.append({'start': start, 'end': start + len(marker), 'path': path,
                        'attribute': 'href' if mime == 'text/css' else 'src', 'original': original})
    pack = {'version': 1, 'run_id': 'synthetic-fixture', 'entrypoint': 'index.html', 'files': files,
            'adaptation': {'version': 1, 'head_offset': entry.index(b'<head>') + 6, 'patches': patches},
            'missing': missing or []}
    return pack


def describe(pack, raw=None):
    if raw is None:
        raw = json.dumps(pack, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'status': 'missing_dependencies' if pack['missing'] else 'ready', 'entrypoint': 'index.html',
            'entry_sha256': pack['files']['index.html']['sha256'], 'missing': pack['missing'],
            'policy': 'opaque-srcdoc-v1', 'package': {'sha256': sha(raw), 'size': len(raw), 'base64': b64(raw)}}


def fidelity_checks(page, resources, base):
    script = 'window.ran=(window.ran||0)+1;'
    cases = [('bom', '﻿', 'one.js', 'one.js'),
             ('entity_decimal', '', 'one.js', 'one&#46;js'),
             ('entity_hex', '', 'one.js', 'one&#x2e;js'),
             ('entity_slash', '', 'js/one.js', 'js&#47;one.js'),
             ('entity_named', '', 'js/one.js', 'js&sol;one&period;js'),
             ('entity_no_semicolon', '', 'one.js', 'one&#46js')]
    passed = []
    read_state = '({ran:window.ran,mode:document.compatMode,text:document.body.textContent})'
    for name, prefix, path, original in cases:
        html = prefix + '<!doctype html><html><head><script src="' + original + '"></script></head><body>synthetic fidelity</body></html>'
        resources['/fidelity-native.html'] = html.encode('utf-8')
        resources['/' + path] = script.encode('utf-8')
        native = page.context.new_page()
        native.goto(base + '/fidelity-native.html')
        expected = native.evaluate(read_state)
        native.close()
        assert expected['ran'] == 1 and expected['mode'] == 'CSS1Compat', (name, expected)
        descriptor = describe(fixture(html, {path: ('text/javascript', script)}, urls={path: original}))
        page.bring_to_front()
        page.evaluate("d=>{window.fidelity=BenchPreview.create(document.querySelector('#a'),{runId:'synthetic-fixture',descriptor:d});return fidelity.start()}", descriptor)
        page.wait_for_function("['loaded','error'].includes(fidelity.getState().status)")
        assert page.evaluate('fidelity.getState().status') == 'loaded', page.evaluate('fidelity.getState()')
        inner = [f for f in page.frames if f.parent_frame and f.parent_frame.parent_frame][0]
        assert inner.evaluate(read_state) == expected, name + ' native/adapted mismatch'
        page.evaluate('fidelity.destroy()')
        passed.append(name)
    # This native script contains an apparent closing/resource tag in its JS string.
    escaped = '''<!doctype html><html><head><script><!--
const a='<script>';const b='</script><script src="one.js">';
window.inlineText=b;
//-->
</script><script src="one.js"></script></head><body></body></html>'''
    resources['/fidelity-native.html'] = escaped.encode('utf-8')
    native = page.context.new_page()
    native.goto(base + '/fidelity-native.html')
    assert native.evaluate('window.inlineText') == '</script><script src="one.js">'
    native.close()
    for name, html, raw_url in [('script_double_escape', escaped, 'one.js'),
                                ('nested_entity', '<!doctype html><html><head><script src="one&amp;#46;js"></script></head><body></body></html>', 'one&amp;#46;js'),
                                ('double_bom', '﻿﻿<!doctype html><html><head><script src="one.js"></script></head><body></body></html>', 'one.js'),
                                ('late_bom', ' ﻿<!doctype html><html><head><script src="one.js"></script></head><body></body></html>', 'one.js')]:
        descriptor = describe(fixture(html, {'one.js': ('text/javascript', script)}, urls={'one.js': raw_url}))
        result = page.evaluate("async d=>{try{await BenchPreview.prepare({runId:'synthetic-fixture',descriptor:d});return null}catch(e){return e.message}}", descriptor)
        assert result, name + ' must be rejected rather than rewritten'
        if name == 'script_double_escape':
            assert 'Escaped script' in result, result
        passed.append(name + '_rejected')
    assert len(page.frames) == 1
    return passed


def encoded_transport_checks(page, resources, descriptor, decoded):
    path = '/' + descriptor['package']['path']
    compressed = gzip.compress(decoded)
    assert len(compressed) != len(decoded), 'fixture must distinguish wire and decoded sizes'
    results = []
    cases = [
        ('gzip', {'body': compressed, 'encoding': 'gzip'}, None),
        ('gzip_without_length', {'body': compressed, 'encoding': 'gzip', 'omit_length': True}, None),
        ('identity', {'body': decoded, 'encoding': 'identity'}, None),
        ('gzip_decoded_oversize', {'body': gzip.compress(decoded + b' '), 'encoding': 'gzip'}, 'Package response too large'),
        ('gzip_decoded_truncated', {'body': gzip.compress(decoded[:-1]), 'encoding': 'gzip'}, 'Package response truncated'),
        ('gzip_decoded_hash_mismatch', {'body': gzip.compress(decoded[:-1] + b' '), 'encoding': 'gzip'}, 'Package digest mismatch'),
        ('identity_header_mismatch', {'body': decoded, 'length': len(decoded) + 1}, 'Package response size mismatch'),
    ]
    for name, response, expected_error in cases:
        resources[path] = response
        page.evaluate("d=>{window.transport=BenchPreview.create(document.querySelector('#a'),{runId:'synthetic-fixture',descriptor:d,transport:'external'});void transport.start()}", descriptor)
        page.wait_for_function("['loaded','error'].includes(transport.getState().status)")
        state = page.evaluate('transport.getState()')
        if expected_error:
            assert state['status'] == 'error' and state['message'] == expected_error, (name, state)
            assert len(page.frames) == 1, name + ' created an executable frame'
        else:
            assert state['status'] == 'loaded', (name, state)
            inner = [f for f in page.frames if f.parent_frame and f.parent_frame.parent_frame][0]
            assert inner.locator('#order').inner_text() == '1,2,3,4'
        page.evaluate('transport.destroy()')
        results.append(name)
    gate, arrived = threading.Event(), threading.Event()
    resources[path] = {'body': compressed, 'encoding': 'gzip', 'gate': gate, 'arrived': arrived}
    page.evaluate("d=>{window.transport=BenchPreview.create(document.querySelector('#a'),{runId:'synthetic-fixture',descriptor:d,transport:'external'});void transport.start()}", descriptor)
    # Some engines withhold the response event until gzip decoding begins. Synchronize
    # on the owned HTTP server, not browser instrumentation, while the body is gated.
    assert arrived.wait(2), 'gzip request did not reach the controlled HTTP server'
    assert page.evaluate('transport.getState().status') == 'fetching', 'gzip cancellation fixture was not in flight'
    page.evaluate('transport.stop()')
    gate.set()
    page.wait_for_timeout(200)
    assert len(page.frames) == 1 and page.evaluate('transport.getState().status') == 'stopped', 'gzip cancellation resurrected frame'
    page.evaluate('transport.destroy()')
    resources[path] = decoded
    return {'wire_size': len(compressed), 'decoded_size': len(decoded), 'cases': results + ['gzip_cancel']}


def wait_for_movement(page, element, initial):
    # Read-only polling avoids both slow first-frame startup and periodic sample aliasing.
    for _ in range(40):
        page.wait_for_timeout(50)
        if element.get_attribute('style') != initial:
            return
    raise AssertionError('original animation did not move within two seconds')


def runtime_checks(page, args, resources, hits, base):
    source = Path(args.runtime).read_text(encoding='utf-8')
    assert '</script' not in source.lower(), 'unsafe bundled script terminator'
    page.add_script_tag(content=source)
    page.evaluate("document.querySelector('#a').style.cssText='width:720px;height:450px';document.querySelector('#b').style.cssText='width:720px;height:450px'")
    html = '''<!doctype html><html><head><meta charset="utf-8"><!-- 中文 🐱 UTF8 offsets -->
    <link rel="stylesheet" href="css/style.css"><script src="js/one.js"></script><script src="js/two.js"></script>
    <script src="js/three.js"></script><script defer src="js/four.js"></script></head>
    <body><button id="pause">pause</button><input id="speed" type="range" min="1" max="5" value="1"><div id="ball"></div><output id="order"></output></body></html>'''
    assets = {'css/style.css': ('text/css', '#ball{width:20px;height:20px;background:rgb(255,0,0)}'),
              'js/one.js': ('text/javascript', 'window.order=[1];'),
              'js/two.js': ('text/javascript', 'order.push(2);'),
              'js/three.js': ('text/javascript', 'order.push(3);'),
              'js/four.js': ('text/javascript', "order.push(4);document.querySelector('#order').textContent=order.join(',');let play=true,x=0;document.querySelector('#pause').onclick=()=>play=!play;function tick(){if(play)x+=Number(document.querySelector('#speed').value);document.querySelector('#ball').style.transform='translateX('+(x%100)+'px)';requestAnimationFrame(tick)}tick();")}
    pack = fixture(html, assets)
    descriptor = describe(pack)
    checkpoint = len(hits)
    page.evaluate('''d=>{window.states=[];window.diagnostics=[];window.preview=BenchPreview.create(document.querySelector('#a'),{runId:'synthetic-fixture',descriptor:d,transport:'inline',viewport:{width:1440,height:900},onState:s=>states.push(s),onDiagnostic:d=>diagnostics.push(d)});}''', descriptor)
    assert len(page.frames) == 1 and len(hits) == checkpoint, 'create eagerly started preview'
    page.evaluate('preview.start()')
    page.wait_for_function("states.some(s=>s.status==='loaded')")
    inner = [f for f in page.frames if f.parent_frame and f.parent_frame.parent_frame][0]
    assert inner.locator('#order').inner_text() == '1,2,3,4', 'classic/defer order changed'
    assert inner.evaluate('({width:innerWidth,height:innerHeight})') == {'width': 1440, 'height': 900}
    assert inner.locator('#ball').evaluate("e=>getComputedStyle(e).backgroundColor") == 'rgb(255, 0, 0)'
    initial = inner.locator('#ball').get_attribute('style')
    wait_for_movement(page, inner.locator('#ball'), initial)
    inner.locator('#pause').click()
    initial = inner.locator('#ball').get_attribute('style')
    page.wait_for_timeout(150)
    assert inner.locator('#ball').get_attribute('style') == initial
    inner.locator('#speed').fill('5')
    inner.locator('#pause').click()
    wait_for_movement(page, inner.locator('#ball'), initial)
    assert page.evaluate('preview.getState().scale') == 0.5
    page.evaluate("preview.setViewport('fit')")
    page.wait_for_timeout(100)
    assert inner.evaluate('({width:innerWidth,height:innerHeight})') == {'width': 720, 'height': 450}
    # A spoof from the original must not become trusted state, including origin=null.
    before = page.evaluate('preview.getState().status')
    inner.evaluate("top.postMessage({type:'shell-ready',status:'error',message:'forged'},'*');parent.postMessage({status:'loaded',message:'forged'},'*')")
    page.wait_for_timeout(80)
    assert page.evaluate('preview.getState().status') == before
    inner.evaluate("setTimeout(()=>{throw new Error('synthetic original diagnostic')},0)")
    # Engines may redact the ErrorEvent message across an opaque-origin boundary.
    page.wait_for_function("diagnostics.some(d=>d.kind==='error')")
    diagnostic_messages = page.evaluate("diagnostics.filter(d=>d.kind==='error').map(d=>d.message)")
    page.evaluate('preview.stop()')
    assert len(page.frames) == 1
    page.evaluate('preview.start();preview.stop()')
    page.wait_for_timeout(100)
    assert len(page.frames) == 1, 'canceled work resurrected frame'
    page.evaluate('preview.destroy()')
    # External transport uses the same exact bytes, only after explicit start.
    external = copy.deepcopy(descriptor)
    raw = base64.b64decode(external['package'].pop('base64'))
    external['package']['path'] = 'originals/' + sha(raw) + '.json'
    resources['/' + external['package']['path']] = raw
    checkpoint = len(hits)
    page.evaluate("d=>{window.external=BenchPreview.create(document.querySelector('#a'),{runId:'synthetic-fixture',descriptor:d,transport:'external',viewport:{width:400,height:800}})}", external)
    assert len(hits) == checkpoint and len(page.frames) == 1
    page.evaluate('external.start()')
    page.wait_for_function("external.getState().status==='loaded'")
    assert hits[checkpoint:] == ['/' + external['package']['path']]
    page.evaluate('external.destroy()')
    transport_results = encoded_transport_checks(page, resources, external, raw)
    # Tampered packages still have a valid outer package hash, exercising inner validation.
    failures = []
    variants = []
    p = copy.deepcopy(pack); p['run_id'] = 'wrong-run'; variants.append(('run binding', describe(p)))
    p = copy.deepcopy(pack); p['files']['js/one.js']['sha256'] = '0'*64; variants.append(('file digest', describe(p)))
    p = copy.deepcopy(pack); p['adaptation']['head_offset'] += 1; variants.append(('head boundary', describe(p)))
    p = copy.deepcopy(pack); p['adaptation']['patches'][0]['start'] += 1; variants.append(('byte offset', describe(p)))
    p = copy.deepcopy(pack); p['adaptation']['patches'][0]['original'] = 'wrong.css'; variants.append(('original attribute', describe(p)))
    p = copy.deepcopy(pack); p['adaptation']['patches'].append(p['adaptation']['patches'][0]); variants.append(('overlap', describe(p)))
    p = copy.deepcopy(pack); p['files']['js/one.js']['mime'] = 'text/css'; variants.append(('MIME', describe(p)))
    duplicate = raw.replace(b'"version":1', b'"version":1,"version":1', 1)
    variants.append(('duplicate JSON key', describe(pack, duplicate)))
    d = copy.deepcopy(descriptor); d['package']['sha256'] = '0'*64; variants.append(('package digest', d))
    d = copy.deepcopy(external); d['package']['path'] = base + '/escape'; variants.append(('URL namespace', d))
    d = copy.deepcopy(descriptor); d['package']['size'] = 4*1024*1024 + 1; variants.append(('size limit', d))
    for name, d in variants:
        result = page.evaluate("async d=>{try{await BenchPreview.prepare({runId:'synthetic-fixture',descriptor:d});return null}catch(e){return e.message}}", d)
        assert result, 'negative accepted: ' + name
        failures.append(name)
    assert len(page.frames) == 1
    attacks = {
        'self_http': "location.href=u+'/runtime-self'",
        'link_http': "const a=document.createElement('a');a.href=u+'/runtime-link';a.textContent='go';document.body.append(a);a.click()",
        'meta_refresh': "const m=document.createElement('meta');m.httpEquiv='refresh';m.content='0;url='+u+'/runtime-refresh';document.head.append(m)",
        'remove_meta': "document.querySelectorAll('meta').forEach(e=>e.remove());fetch(u+'/runtime-meta-removed').catch(e=>window.blocked=String(e));location.href=u+'/runtime-meta-navigation'",
        'document_write': "document.open();document.write('<html><body><img src=\"'+u+'/runtime-write-image\"><script>fetch('+JSON.stringify(u+'/runtime-write-fetch')+').catch(e=>window.blocked=String(e));location.href='+JSON.stringify(u+'/runtime-write-navigation')+';<'+ '/script></body></html>');document.close()",
        'about_blank': "location.href='about:blank'",
        'data_navigation': "location.href='data:text/html,<h1>new document</h1>'",
        'blob_navigation': "location.href=URL.createObjectURL(new Blob(['<h1>new document</h1>'],{type:'text/html'}))",
        'nested_srcdoc': "const f=document.createElement('iframe');f.srcdoc='<img src=\"'+u+'/runtime-nested-image\">';document.body.append(f)",
    }
    navigation_results = []
    minimal = describe(fixture('<!doctype html><html><head></head><body>synthetic navigation probe</body></html>'))
    for name, attack in attacks.items():
        page.evaluate("d=>{window.probe=BenchPreview.create(document.querySelector('#a'),{runId:'synthetic-fixture',descriptor:d,viewport:{width:400,height:800}});return probe.start()}", minimal)
        page.wait_for_function("probe.getState().status==='loaded'")
        original = [f for f in page.frames if f.parent_frame and f.parent_frame.parent_frame][0]
        checkpoint = len(hits)
        original.evaluate('u=>{' + attack + '}', base)
        page.wait_for_timeout(180)
        if name == 'about_blank':
            original.evaluate("u=>{fetch(u+'/runtime-about-fetch').catch(e=>window.blocked=String(e));location.href=u+'/runtime-about-navigation'}", base)
            page.wait_for_timeout(150)
        assert not hits[checkpoint:], name + ' reached passive canary: ' + repr(hits[checkpoint:])
        assert page.url == base + '/parent'
        page.evaluate('probe.destroy()')
        navigation_results.append(name)
    fidelity_results = fidelity_checks(page, resources, base)
    return {'encoded_transport': transport_results, 'native_fidelity': fidelity_results, 'runtime_passive_canary_navigation_cases': navigation_results, 'movement_controls': 'pass', 'classic_defer_utf8_patches': 'pass', 'lazy_fetch_cancel_destroy': 'pass',
            'viewport_fit_and_fixed': 'pass', 'message_spoof_rejected': 'pass', 'diagnostic_messages': diagnostic_messages,
            'rejected': failures}


def main():
    args = arguments()
    from playwright.sync_api import sync_playwright
    hits = []
    resources = {}
    disconnects = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            resource = resources.get(self.path, b'<!doctype html><html><head><meta charset="utf-8"></head><body><main id="secret">parent-secret</main><div id="a"></div><div id="b"></div></body></html>')
            response = resource if isinstance(resource, dict) else {'body': resource}
            raw = response['body']
            self.send_response(200)
            content_type = 'application/json' if self.path.endswith('.json') else 'text/javascript' if self.path.endswith('.js') else 'text/html; charset=utf-8'
            self.send_header('Content-Type', content_type)
            if not response.get('omit_length'):
                self.send_header('Content-Length', str(response.get('length', len(raw))))
            if response.get('encoding'):
                self.send_header('Content-Encoding', response['encoding'])
            self.end_headers()
            if response.get('arrived'):
                response['arrived'].set()
            if response.get('gate'):
                response['gate'].wait(5)
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                disconnects.append(self.path)
        def do_POST(self):
            self.do_GET()
        def log_message(self, *unused):
            pass

    # Ephemeral loopback is a reachable synthetic test endpoint, never product config.
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    outer_policy = "default-src 'none'; script-src 'unsafe-inline' data:; style-src 'unsafe-inline' data:; img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-src about:"
    inner_policy = outer_policy.replace('frame-src about:', "frame-src 'none'")
    report = {'browser': args.browser, 'outer_policy': outer_policy, 'inner_policy': inner_policy,
              'interception': False, 'offline': False, 'monkeypatch': False,
              'limits': ['No WebRTC/STUN or DNS/preconnect zero-egress guarantee.',
                         'No CPU, memory or GPU hard quota; this proof uses only bounded animation.',
                         'Only selected browser engine tested; other installed engines require separate runs.']}
    try:
        with sync_playwright() as playwright:
            browser = getattr(playwright, args.browser).launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            violations = []
            page.on('console', lambda msg: violations.append(msg.text) if msg.type == 'error' else None)
            page.goto(base + '/parent', wait_until='networkidle')
            assert page.evaluate('(u)=>fetch(u).then(r=>r.status)', base + '/positive-control') == 200
            if args.runtime:
                report['runtime'] = runtime_checks(page, args, resources, hits, base)
            page.evaluate("localStorage.setItem('synthetic', 'parent-storage');document.cookie='synthetic_parent_cookie=parent-canary; Path=/; SameSite=Lax'")
            assert page.evaluate("document.cookie.includes('synthetic_parent_cookie=parent-canary')"), 'cookie positive control unavailable'
            page.evaluate('''({outerPolicy,innerPolicy}) => {
                window.install = (target, html) => {
                    const inner = '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + innerPolicy + '"></head><body>' + html + '</body></html>';
                    const outer = document.createElement('iframe'); outer.sandbox = 'allow-scripts';
                    outer.srcdoc = '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="' + outerPolicy + '"></head><body><script>const f=document.createElement("iframe"); f.sandbox="allow-scripts"; f.srcdoc=' + JSON.stringify(inner).replaceAll('<',String.fromCharCode(92)+'u003c') + '; document.body.append(f);<'+ '/script></body></html>';
                    document.querySelector(target).replaceChildren(outer);
                };
            }''', {'outerPolicy': outer_policy, 'innerPolicy': inner_policy})
            animation = '''<button id="toggle">pause</button><input id="speed" type="range" min="1" max="5" value="1"><div id="ball" style="width:20px;height:20px;background:red"></div><script>
            let running=true, x=0; document.querySelector('#toggle').onclick=()=>{running=!running;};
            function tick(){if(running)x+=Number(document.querySelector('#speed').value);document.querySelector('#ball').style.transform='translateX('+(x%100)+'px)';requestAnimationFrame(tick)};tick();</script>'''
            for target in ('#a', '#b'):
                page.evaluate('([target,html])=>install(target,html)', [target, animation])
            page.wait_for_timeout(350)
            inners = [f for f in page.frames if f.parent_frame and f.parent_frame.parent_frame]
            assert len(inners) == 2, 'double-srcdoc initialization failed'
            a, b = inners
            start = a.locator('#ball').get_attribute('style')
            page.wait_for_timeout(180)
            assert a.locator('#ball').get_attribute('style') != start, 'animation did not move'
            a.locator('#toggle').click()
            start = a.locator('#ball').get_attribute('style')
            peer = b.locator('#ball').get_attribute('style')
            page.wait_for_timeout(160)
            assert a.locator('#ball').get_attribute('style') == start, 'pause control failed'
            assert b.locator('#ball').get_attribute('style') != peer, 'peer paused unexpectedly'
            a.locator('#speed').fill('5')
            a.locator('#toggle').click()
            page.wait_for_timeout(150)
            assert a.locator('#ball').get_attribute('style') != start, 'resume failed'
            report['movement_controls'] = 'moving, pause, speed, resume; peer independently moving'
            denial = a.evaluate('''() => {
                const results={};
                for(const [name, fn] of Object.entries({parentDOM:()=>parent.document.body,
                  topDOM:()=>top.document.body, peerDOM:()=>top.frames[1].document.body,
                  localStorage:()=>localStorage.getItem('synthetic'),sessionStorage:()=>sessionStorage.length,
                  sandboxRemoval:()=>frameElement.removeAttribute('sandbox')})) {
                  try {fn(); results[name]=false;} catch(e){results[name]=true;}
                }
                try {results.cookie=document.cookie==='';} catch(e){results.cookie=true;}
                return results;
            }''')
            assert all(denial.values()), denial
            report['denied'] = denial
            checkpoint = len(hits)
            a.evaluate('''u => {
              fetch(u+'/fetch').catch(()=>{});
              const x=new XMLHttpRequest();x.open('GET',u+'/xhr');x.send();
              navigator.sendBeacon(u+'/beacon','synthetic');
              try {new WebSocket(u.replace('http:','ws:')+'/websocket');} catch(e){}
              for(const [tag,attr,path] of [['img','src','image'],['script','src','script'],['iframe','src','frame'],['object','data','object'],['link','href','css']]){
                const e=document.createElement(tag);if(tag==='link')e.rel='stylesheet';e[attr]=u+'/'+path;document.body.append(e);
              }
              const style=document.createElement('style');style.textContent='@font-face{font-family:canary;src:url('+u+'/font)}body{font-family:canary;background:url('+u+'/background)}';document.head.append(style);
              const m=document.createElement('script');m.type='module';m.src=u+'/module';document.body.append(m);
              try{new Worker(u+'/worker');}catch(e){}
              try{navigator.serviceWorker.register(u+'/service-worker').catch(()=>{});}catch(e){}
              try{parent.location=u+'/parent-nav';}catch(e){}
              try{top.location=u+'/top-nav';}catch(e){}
              try{window.open(u+'/popup');}catch(e){window.popupDenied=String(e);}
              const form=document.createElement('form');form.action=u+'/form';form.method='POST';document.body.append(form);form.submit();
              const nested=document.createElement('iframe');nested.srcdoc='<script>fetch('+JSON.stringify(u+'/nested')+')<'+ '/script>';document.body.append(nested);
              location.href=u+'/self-navigation';
            }''', base)
            page.wait_for_timeout(700)
            unexpected = hits[checkpoint:]
            report['canary_after_attacks'] = unexpected
            report['csp_error_count'] = len(violations)
            assert not unexpected, 'CANARY REACHED: ' + repr(unexpected)
            assert page.url == base + '/parent', 'parent navigated'
            assert page.locator('#secret').inner_text() == 'parent-secret'
            report['self_navigation'] = 'HTTP blocked by trusted outer frame-src about:'
            report['positive_control'] = '/positive-control reached passive HTTP server'
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
