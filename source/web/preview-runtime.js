/* Trusted v2-only original preview loader. Never execute original bytes in this realm. */
(() => {
  'use strict';
  const LIMIT = Object.freeze({package: 4 * 1024 * 1024, file: 2 * 1024 * 1024, files: 32});
  const HASH = /^[a-f0-9]{64}$/;
  const encoder = new TextEncoder();
  // Retain U+FEFF while mapping byte offsets. Strip the transport BOM only at srcdoc output.
  const decoder = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true});
  const COMMON = "default-src 'none'; script-src 'unsafe-inline' data:; style-src 'unsafe-inline' data:; img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; ";
  const OUTER_POLICY = COMMON + 'frame-src about:';
  const INNER_POLICY = COMMON + "frame-src 'none'";
  const TYPES = new Set(['text/html', 'text/css', 'text/javascript']);
  const STATUSES = new Set(['ready', 'missing_dependencies', 'no_entrypoint', 'not_reviewed', 'withheld', 'unsupported']);
  const SVG_PROTOCOL = 'standalone-svg-v1';
  const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';
  const XLINK_NAMESPACE = 'http://www.w3.org/1999/xlink';
  const XML_NAMESPACE = 'http://www.w3.org/XML/1998/namespace';
  const SVG_ELEMENTS = new Set(`svg g defs symbol use title desc metadata style path rect circle ellipse line polyline polygon
text tspan textPath linearGradient radialGradient stop pattern clipPath mask marker filter
feBlend feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting feDisplacementMap
feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR feGaussianBlur feImage feMerge feMergeNode
feMorphology feOffset fePointLight feSpecularLighting feSpotLight feTile feTurbulence
animate animateMotion animateTransform mpath set`.split(/\s+/));
  const SVG_ANIMATED_ATTRIBUTES = new Set(`x y x1 y1 x2 y2 cx cy r rx ry width height d points dx dy rotate transform
opacity fill fill-opacity stroke stroke-width stroke-opacity stroke-dasharray stroke-dashoffset
stroke-linecap stroke-linejoin stroke-miterlimit color stop-color stop-opacity flood-color flood-opacity
lighting-color font-size font-weight letter-spacing word-spacing visibility display offset
viewBox preserveAspectRatio pathLength startOffset gradientTransform patternTransform`.split(/\s+/));
  const SVG_FRAGMENT = /^#[A-Za-z0-9_.:-]+$/;
  function fail(message) { throw new Error(message); }
  function object(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function keys(value, expected, name) {
    if (!object(value) || Object.keys(value).length !== expected.length || expected.some(key => !Object.hasOwn(value, key))) fail('Invalid ' + name + ' fields');
  }
  function path(value) {
    if (typeof value !== 'string' || value.length > 240 || !/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/.test(value) || value.split('/').some(p => p === '.' || p === '..')) fail('Noncanonical file path');
    return value;
  }
  function svgEntrypoint(value) {
    return typeof value === 'string' && !value.includes('/') && /.+\.svg$/i.test(value);
  }
  function checkSVGCSS(text) {
    const clean = text.replace(/\/\*[\s\S]*?\*\//g, '');
    if (/\\|@(?!keyframes\b|-webkit-keyframes\b|media\b)|\b(?:image-set|image|src|cross-fade|paint)\s*\(/i.test(clean)) fail('Unsupported standalone SVG CSS resource/escape');
    // /*...*/ inside an unquoted url() can be literal URL path characters.
    // Never turn such a relative resource into an apparently safe fragment.
    for (const candidate of [text, clean]) {
      const withoutFragments = candidate.replace(/url\s*\(\s*(?:"#[A-Za-z0-9_.:-]+"|'#[A-Za-z0-9_.:-]+'|#[A-Za-z0-9_.:-]+)\s*\)/ig, '');
      if (/url\s*\(/i.test(withoutFragments)) fail('External or invalid standalone SVG CSS reference');
    }
  }
  function checkSVG(text) {
    // XML parsing is detached and validation-only. No source node is ever added
    // to the report DOM, serialized as HTML, or used as an executable document.
    const declaration = /^(?:﻿)?<\?xml\s+version\s*=\s*(['"])1\.0\1(?:\s+encoding\s*=\s*(['"])(?:utf-8|utf8)\2)?(?:\s+standalone\s*=\s*(['"])(?:yes|no)\3)?\s*\?>/i;
    if (/^(?:﻿)?<\?xml\s/i.test(text) && !declaration.test(text)) fail('Unsupported standalone SVG XML encoding/version');
    const markup = text.replace(/<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>/g, '');
    if (/<!DOCTYPE|<!ENTITY/i.test(markup)) fail('Standalone SVG DTD/entity');
    const xml = new DOMParser().parseFromString(text, 'image/svg+xml');
    const root = xml.documentElement;
    if (!root || root.namespaceURI !== SVG_NAMESPACE || root.localName !== 'svg' || xml.getElementsByTagName('parsererror').length) fail('Malformed standalone SVG XML');
    const pending = Array.from(xml.childNodes, node => ({node, depth: 0}));
    let count = 0;
    while (pending.length) {
      const {node, depth} = pending.pop();
      if (node.nodeType === 7 || node.nodeType === 10) fail('Standalone SVG DTD/processing instruction');
      if (node.nodeType === 1) {
        if (++count > 20000 || depth >= 128) fail('Standalone SVG XML complexity');
        if (node.namespaceURI !== SVG_NAMESPACE || !SVG_ELEMENTS.has(node.localName)) fail('Unsupported standalone SVG element');
        for (const attribute of node.attributes) {
          if (attribute.namespaceURI === 'http://www.w3.org/2000/xmlns/') continue;
          const name = attribute.localName, ns = attribute.namespaceURI, value = attribute.value;
          if (name.toLowerCase().startsWith('on')) fail('Standalone SVG event handler');
          if (ns && !(ns === XLINK_NAMESPACE && name === 'href' || ns === XML_NAMESPACE && ['lang', 'space'].includes(name))) fail('Unsupported standalone SVG attribute namespace');
          if (['src', 'base', 'data'].includes(name.toLowerCase())) fail('Standalone SVG resource attribute');
          if (name.toLowerCase() === 'href' && !SVG_FRAGMENT.test(value.trim())) fail('External standalone SVG reference');
          checkSVGCSS(value);
        }
        if (['animate', 'animateTransform', 'set'].includes(node.localName) && !SVG_ANIMATED_ATTRIBUTES.has(node.getAttribute('attributeName'))) fail('Unsupported standalone SVG animation target');
        if (node.localName === 'style') {
          if (node.children.length || (node.getAttribute('type') || 'text/css').toLowerCase() !== 'text/css') fail('Unsupported standalone SVG style');
          checkSVGCSS(node.textContent);
        }
      }
      pending.push(...Array.from(node.childNodes, child => ({node: child, depth: depth + 1})));
    }
  }
  function resourcePath(value) {
    // Decode once, without parsing DOM or loading resources. These are all HTML5
    // named references whose expansion fits the canonical ASCII path alphabet.
    const named = {fjlig: 'fj', lowbar: '_', UnderBar: '_', period: '.', sol: '/'};
    const decoded = value.replace(/&(?:#(?:[xX][0-9a-fA-F]+|[0-9]+);?|[A-Za-z][A-Za-z0-9]*;)/g, reference => {
      if (reference.startsWith('&#')) {
        const number = reference.slice(2).replace(/;$/, '');
        const code = /^[xX]/.test(number) ? Number.parseInt(number.slice(1), 16) : Number.parseInt(number, 10);
        if (code < 1 || code > 127) fail('Unsupported URL character reference');
        return String.fromCharCode(code);
      }
      const name = reference.slice(1, -1);
      if (!Object.hasOwn(named, name)) fail('Unsupported URL character reference');
      return named[name];
    });
    return path(decoded.replace(/^\.\//, ''));
  }
  function missing(value) {
    if (!Array.isArray(value) || value.length > LIMIT.files || new Set(value).size !== value.length) fail('Invalid missing dependencies');
    value.forEach(path);
  }
  function integer(value, min, max, name) {
    if (!Number.isSafeInteger(value) || value < min || value > max) fail('Invalid ' + name);
  }
  function unbase64(text, limit) {
    if (typeof text !== 'string' || text.length > Math.ceil(limit / 3) * 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(text)) fail('Invalid base64');
    const raw = atob(text);
    if (raw.length > limit || btoa(raw) !== text) fail('Noncanonical base64');
    return Uint8Array.from(raw, c => c.charCodeAt(0));
  }
  async function digest(bytes) {
    if (!globalThis.crypto || !crypto.subtle) fail('Browser policy: integrity verification requires a secure context');
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), x => x.toString(16).padStart(2, '0')).join('');
  }
  // Parse JSON without ever accepting duplicate object keys (including escaped aliases).
  function parseJSON(text) {
    let at = 0;
    const whitespace = () => { while (/[\t\n\r ]/.test(text[at] || '\0')) at++; };
    function string() {
      const start = at++;
      while (at < text.length) {
        const c = text[at++];
        if (c === '"') return JSON.parse(text.slice(start, at));
        if (c === '\\') at++;
      }
      return fail('Unterminated JSON string');
    }
    function value(depth) {
      if (depth > 24) fail('JSON nesting limit');
      whitespace();
      const c = text[at];
      if (c === '"') return string();
      if (c === '{' || c === '[') {
        at++;
        const isObject = c === '{', result = isObject ? Object.create(null) : [];
        const seen = new Set(), close = isObject ? '}' : ']';
        whitespace();
        if (text[at] === close) { at++; return result; }
        while (at < text.length) {
          whitespace();
          if (isObject) {
            if (text[at] !== '"') fail('Invalid JSON object key');
            const key = string();
            if (seen.has(key)) fail('Duplicate JSON key');
            seen.add(key); whitespace();
            if (text[at++] !== ':') fail('Invalid JSON separator');
            result[key] = value(depth + 1);
          } else result.push(value(depth + 1));
          whitespace();
          const next = text[at++];
          if (next === close) return result;
          if (next !== ',') fail('Invalid JSON delimiter');
        }
        return fail('Unterminated JSON container');
      }
      const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(at));
      if (!match) fail('Invalid JSON value');
      at += match[0].length;
      return JSON.parse(match[0]);
    }
    const result = value(0); whitespace();
    if (at !== text.length) fail('Trailing JSON content');
    return result;
  }
  function validateDescriptor(d) {
    keys(d, ['status', 'entrypoint', 'entry_sha256', 'missing', 'policy', 'package'], 'descriptor');
    if (!STATUSES.has(d.status) || d.policy !== 'opaque-srcdoc-v1') fail('Unsupported preview descriptor');
    missing(d.missing);
    const isSVG = svgEntrypoint(d.entrypoint);
    if (d.entrypoint !== null && d.entrypoint !== 'index.html' && !isSVG) fail('Unsupported entrypoint');
    if (isSVG) {
      path(d.entrypoint);
      if (d.missing.length || d.status === 'missing_dependencies') fail('Standalone SVG dependencies');
    }
    if (d.entry_sha256 !== null && (typeof d.entry_sha256 !== 'string' || !HASH.test(d.entry_sha256))) fail('Invalid entry digest');
    const runnable = d.status === 'ready' || d.status === 'missing_dependencies';
    if (runnable && (d.entrypoint === null || d.entry_sha256 === null || d.package === null)) fail('Incomplete runnable descriptor');
    if (d.status === 'ready' && d.missing.length || d.status === 'missing_dependencies' && !d.missing.length) fail('Dependency status mismatch');
    if (!runnable && d.package !== null) fail('Unavailable original cannot carry executable package');
    if (d.package !== null) {
      const p = d.package;
      keys(p, Object.hasOwn(p, 'path') ? ['sha256', 'size', 'path'] : ['sha256', 'size', 'base64'], 'package descriptor');
      if (typeof p.sha256 !== 'string' || !HASH.test(p.sha256)) fail('Invalid package digest');
      integer(p.size, 1, LIMIT.package, 'package size');
      if (Object.hasOwn(p, 'path')) {
        if (p.path !== 'originals/' + p.sha256 + '.json') fail('Invalid package URL');
      } else if (unbase64(p.base64, LIMIT.package).length !== p.size) fail('Inline package size mismatch');
    }
    return d;
  }
  // Lex only document structure and original URL attributes; never serialize the DOM.
  function scanHTML(html) {
    const tags = [];
    let at = 0, raw = null;
    while (at < html.length) {
      if (raw) {
        const re = new RegExp('<\\/' + raw + '(?=[\\s>])', 'ig'); re.lastIndex = at;
        const end = re.exec(html);
        // HTML's escaped/double-escaped script states cannot be modeled by this lexer.
        // Reject before interpreting an apparent close tag or resource tag inside JS text.
        if (raw === 'script' && html.slice(at, end ? end.index : html.length).includes('<!--')) fail('Escaped script tokenizer states unsupported');
        if (!end) break;
        at = end.index; raw = null;
      }
      const begin = html.indexOf('<', at);
      if (begin < 0) break;
      if (html.startsWith('<!--', begin)) {
        const end = html.indexOf('-->', begin + 4);
        if (end < 0) fail('Unterminated HTML comment');
        at = end + 3; continue;
      }
      if (/^<!doctype\s+html\s*>/i.test(html.slice(begin))) {
        at = html.indexOf('>', begin) + 1; continue;
      }
      const opening = /^<(\/?)([A-Za-z][A-Za-z0-9:-]*)\b/.exec(html.slice(begin));
      if (!opening) { at = begin + 1; continue; }
      at = begin + opening[0].length;
      const name = opening[2].toLowerCase(), attrs = Object.create(null);
      while (at < html.length) {
        while (/\s/.test(html[at] || '\0')) at++;
        if (html[at] === '>' || html.slice(at, at + 2) === '/>') break;
        const attr = /^[^\s=<>/"']+/.exec(html.slice(at));
        if (!attr) fail('Unsupported HTML attribute syntax');
        const key = attr[0].toLowerCase(); at += attr[0].length;
        if (Object.hasOwn(attrs, key)) fail('Duplicate HTML attribute');
        while (/\s/.test(html[at] || '\0')) at++;
        let start = at, end = at;
        if (html[at] === '=') {
          at++; while (/\s/.test(html[at] || '\0')) at++;
          const quote = html[at];
          if (quote === '"' || quote === "'") {
            start = ++at; end = html.indexOf(quote, at);
            if (end < 0) fail('Unterminated HTML attribute');
            at = end + 1;
          } else {
            start = at;
            while (at < html.length && !/[\s>]/.test(html[at])) at++;
            end = at;
          }
        }
        attrs[key] = {start, end, value: html.slice(start, end)};
      }
      if (at >= html.length) fail('Unterminated HTML tag');
      at += html.slice(at, at + 2) === '/>' ? 2 : 1;
      const closing = opening[1] === '/';
      tags.push({name, closing, begin, end: at, attrs});
      if (!closing && ['script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'].includes(name)) raw = name;
    }
    return tags;
  }
  function scriptJSON(value) { return JSON.stringify(value).replace(/</g, '\\u003c'); }
  async function prepare({runId, descriptor, bytes}) {
    validateDescriptor(descriptor);
    if (typeof runId !== 'string' || !runId || runId.length > 200) fail('Invalid run binding');
    if (!descriptor.package) fail('Original unavailable: ' + descriptor.status);
    if (bytes === undefined && Object.hasOwn(descriptor.package, 'base64')) bytes = unbase64(descriptor.package.base64, LIMIT.package);
    if (!(bytes instanceof Uint8Array) || bytes.length !== descriptor.package.size) fail('Package size mismatch');
    if (await digest(bytes) !== descriptor.package.sha256) fail('Package digest mismatch');
    const pack = parseJSON(decoder.decode(bytes));
    keys(pack, ['version', 'run_id', 'entrypoint', 'files', 'adaptation', 'missing'], 'original package');
    if (pack.version !== 1 || pack.run_id !== runId || pack.entrypoint !== descriptor.entrypoint) fail('Package run/entry binding mismatch');
    missing(pack.missing);
    if (JSON.stringify(pack.missing) !== JSON.stringify(descriptor.missing)) fail('Missing dependency mismatch');
    if (!object(pack.files) || !Object.keys(pack.files).length || Object.keys(pack.files).length > LIMIT.files) fail('Invalid file count');
    const isSVG = svgEntrypoint(pack.entrypoint);
    if (isSVG && (Object.keys(pack.files).length !== 1 || pack.missing.length)) fail('Standalone SVG files/dependencies');
    const files = Object.create(null);
    for (const [name, file] of Object.entries(pack.files)) {
      path(name); keys(file, ['mime', 'size', 'sha256', 'base64'], 'original file');
      if (isSVG) {
        if (name !== pack.entrypoint || file.mime !== 'image/svg+xml') fail('Standalone SVG MIME/path mismatch');
      } else {
        if (!TYPES.has(file.mime)) fail('Unsupported MIME');
        if ((name.endsWith('.html') && file.mime !== 'text/html') || (name.endsWith('.css') && file.mime !== 'text/css') || (name.endsWith('.js') && !['text/javascript'].includes(file.mime)) || !/\.(html|css|js)$/.test(name)) fail('File MIME/path mismatch');
      }
      integer(file.size, 0, LIMIT.file, 'file size');
      if (typeof file.sha256 !== 'string' || !HASH.test(file.sha256)) fail('Invalid file digest');
      const raw = unbase64(file.base64, LIMIT.file);
      if (raw.length !== file.size || await digest(raw) !== file.sha256) fail('File integrity mismatch');
      decoder.decode(raw); files[name] = {raw, mime: file.mime, base64: file.base64};
    }
    if (!Object.hasOwn(files, pack.entrypoint) || files[pack.entrypoint].mime !== (isSVG ? 'image/svg+xml' : 'text/html') || pack.files[pack.entrypoint].sha256 !== descriptor.entry_sha256) fail('Entry integrity mismatch');
    if (pack.missing.some(name => Object.hasOwn(files, name))) fail('Missing dependency exists in package');
    const original = files[pack.entrypoint].raw;
    if (isSVG) {
      keys(pack.adaptation, ['version', 'protocol'], 'standalone SVG adaptation');
      if (pack.adaptation.version !== 1 || pack.adaptation.protocol !== SVG_PROTOCOL) fail('Unsupported standalone SVG adaptation');
      checkSVG(decoder.decode(original));
      // The model delivered SVG, not HTML. This wrapper is trusted runtime UI;
      // its image source is the exact hash-verified bytes, with no SVG rewriting.
      const html = '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + INNER_POLICY + '"><title>Standalone SVG</title><style>html,body{margin:0;width:100%;height:100%;overflow:hidden}img{display:block;width:100%;height:100%;object-fit:contain}</style></head><body><img alt="Standalone SVG original" src="data:image/svg+xml;base64,' + files[pack.entrypoint].base64 + '"></body></html>';
      return Object.freeze({html, entry_sha256: descriptor.entry_sha256, missing: Object.freeze([])});
    }
    const html = decoder.decode(original), tags = scanHTML(html);
    const adaptation = pack.adaptation;
    keys(adaptation, ['version', 'head_offset', 'patches'], 'adaptation');
    if (adaptation.version !== 1 || !Array.isArray(adaptation.patches) || adaptation.patches.length > 128) fail('Unsupported adaptation');
    integer(adaptation.head_offset, 1, original.length, 'head offset');
    const head = tags.find(t => t.name === 'head' && !t.closing);
    if (!head || encoder.encode(html.slice(0, head.end)).length !== adaptation.head_offset) fail('Head offset mismatch');
    // Only doctype, comments, whitespace and an opening html tag may precede head.
    const trimHTMLSpace = value => value.replace(/^[\t\n\f\r ]+|[\t\n\f\r ]+$/g, '');
    let prefix = trimHTMLSpace(html.slice(0, head.begin).replace(/^﻿/, '').replace(/<!--[\s\S]*?-->/g, ''));
    prefix = trimHTMLSpace(prefix.replace(/^<!doctype[\t\n\f\r ]+html[\t\n\f\r ]*>/i, ''));
    const htmlTag = tags.find(t => t.name === 'html' && !t.closing && t.end <= head.begin);
    if (htmlTag) prefix = trimHTMLSpace(prefix.replace(html.slice(htmlTag.begin, htmlTag.end), ''));
    if (prefix || tags.some(t => t.begin < head.begin && (t.name !== 'html' || t.closing))) fail('Unsafe content before policy insertion');
    if (tags.some(t => t.name === 'script' && t.attrs.type && t.attrs.type.value.trim().toLowerCase() === 'module')) fail('Module scripts unsupported');
    const patches = [];
    for (const patch of adaptation.patches) {
      keys(patch, ['start', 'end', 'path', 'attribute', 'original'], 'resource patch');
      integer(patch.start, adaptation.head_offset, original.length, 'patch start');
      integer(patch.end, patch.start + 1, original.length, 'patch end');
      path(patch.path);
      if (!Object.hasOwn(files, patch.path) || !['src', 'href'].includes(patch.attribute) || typeof patch.original !== 'string') fail('Invalid patch target');
      if (decoder.decode(original.slice(patch.start, patch.end)) !== patch.original) fail('Patch original bytes mismatch');
      const tag = tags.find(t => !t.closing && t.attrs[patch.attribute] && encoder.encode(html.slice(0, t.attrs[patch.attribute].start)).length === patch.start && encoder.encode(html.slice(0, t.attrs[patch.attribute].end)).length === patch.end);
      const target = files[patch.path];
      if (!tag || !(tag.name === 'script' && patch.attribute === 'src' && ['text/javascript'].includes(target.mime) || tag.name === 'link' && patch.attribute === 'href' && target.mime === 'text/css' && tag.attrs.rel && tag.attrs.rel.value.toLowerCase().split(/\s+/).includes('stylesheet'))) fail('Patch is not a supported resource attribute');
      const normalized = resourcePath(patch.original);
      if (normalized !== patch.path) fail('Patch target path mismatch');
      patches.push({...patch, bytes: encoder.encode('data:' + target.mime + ';base64,' + target.base64)});
    }
    patches.sort((a, b) => b.start - a.start);
    let output = original, boundary = original.length;
    for (const patch of patches) {
      if (patch.end > boundary) fail('Overlapping resource patches');
      boundary = patch.start;
      const next = new Uint8Array(output.length - (patch.end - patch.start) + patch.bytes.length);
      next.set(output.slice(0, patch.start)); next.set(patch.bytes, patch.start); next.set(output.slice(patch.end), patch.start + patch.bytes.length); output = next;
    }
    const policy = '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + INNER_POLICY + '">';
    const offset = adaptation.head_offset;
    // A byte-stream BOM is consumed by native HTML decoding. srcdoc accepts already
    // decoded characters; leaving the BOM there would turn a standards document quirky.
    const rendered = (decoder.decode(output.slice(0, offset)) + policy + decoder.decode(output.slice(offset))).replace(/^﻿/, '');
    return Object.freeze({html: rendered, entry_sha256: descriptor.entry_sha256, missing: Object.freeze(pack.missing.slice())});
  }
  async function fetchBytes(descriptor, signal, response) {
    const p = descriptor.package;
    if (Object.hasOwn(p, 'base64')) return unbase64(p.base64, LIMIT.package);
    const url = new URL(p.path, document.baseURI);
    const base = new URL(document.baseURI);
    if (!['http:', 'https:'].includes(url.protocol) || url.origin !== base.origin || url.username || url.password) fail('External package requires same-origin HTTP transport');
    if (response === undefined) response = await fetch(url.href, {credentials: 'omit', redirect: 'error', signal, cache: 'no-store', referrerPolicy: 'no-referrer'});
    let reader = null;
    try {
      if (!response.ok) fail('Package HTTP ' + response.status);
      const length = response.headers.get('Content-Length');
      const encoding = (response.headers.get('Content-Encoding') || 'identity').trim().toLowerCase();
      // Fetch decodes compressed bodies, but Content-Length describes encoded wire bytes.
      // Only identity responses permit an early comparison to the decoded package size.
      if (length !== null && (!/^\d+$/.test(length) || ((!encoding || encoding === 'identity') && Number(length) !== p.size))) fail('Package response size mismatch');
      if (!response.body) fail('Browser lacks bounded streaming fetch');
      reader = response.body.getReader();
      const chunks = []; let size = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.length;
        if (size > p.size || size > LIMIT.package) fail('Package response too large');
        chunks.push(value);
      }
      if (size !== p.size) fail('Package response truncated');
      const bytes = new Uint8Array(size); let at = 0;
      for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.length; }
      return bytes;
    } finally {
      if (reader) {
        await reader.cancel().catch(error => console.info('Package stream cleanup:', error.name));
        reader.releaseLock();
      } else if (response.body && !response.body.locked) {
        await response.body.cancel().catch(error => console.info('Package stream cleanup:', error.name));
      }
    }
  }
  // Network may start before SRI loads this SDK. Reading starts only after SRI.
  async function readPackage(descriptor, response) {
    if (!(response instanceof Response)) fail('Native package Response required');
    // Native getters enforce the internal Response brand and ignore shadowed fields.
    response = Object.fromEntries(['url', 'ok', 'redirected', 'status', 'headers', 'body'].map(key => [key, Reflect.get(Response.prototype, key, response)]));
    const headers = response.headers;
    response.headers = {get: name => Headers.prototype.get.call(headers, name)};
    let delegated = false;
    try {
      const copy = parseJSON(JSON.stringify(descriptor)); validateDescriptor(copy);
      if (!copy.package || !Object.hasOwn(copy.package, 'path')) fail('External package required');
      const url = new URL(copy.package.path, document.baseURI), base = new URL(document.baseURI);
      if (!['http:', 'https:'].includes(url.protocol) || url.origin !== base.origin || url.username || url.password || response.url !== url.href) fail('Package response URL mismatch');
      if (!response.ok || response.redirected || !/^application\/json(?:;|$)/i.test(response.headers.get('Content-Type') || '')) fail('Package response mismatch');
      delegated = true;
      const bytes = await fetchBytes(copy, undefined, response);
      if (await digest(bytes) !== copy.package.sha256) fail('Package digest mismatch');
      return bytes;
    } finally {
      if (!delegated && response.body && !response.body.locked) {
        await response.body.cancel().catch(error => console.info('Package stream cleanup:', error.name));
      }
    }
  }
  // Diagnostics are untrusted bounded text, never an instruction or acceptance signal.
  function originalDiagnostics() {
    let sent = 0;
    const send = (kind, message) => {
      if (sent++ < 30) parent.postMessage({type: 'original-diagnostic', kind, message: String(message).slice(0, 500)}, '*');
    };
    addEventListener('error', event => send('error', event.message || 'Original resource failed'), true);
    addEventListener('unhandledrejection', event => send('error', String(event.reason)));
    addEventListener('securitypolicyviolation', event => send('policy', 'Blocked by isolation policy: ' + event.violatedDirective));
  }
  // This function is serialized into the trusted opaque shell, not executed in the report.
  function shellBoot(config) {
    const frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts'); frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.style.cssText = 'display:block;border:0;width:100vw;height:100vh';
    let port = null, initialized = false, diagnostics = 0;
    const send = (status, message, kind) => { if (port) port.postMessage({id: config.id, generation: config.generation, status, message, kind}); };
    addEventListener('securitypolicyviolation', event => {
      if (diagnostics++ < 30) send('diagnostic', ('Blocked by shell policy: ' + event.violatedDirective).slice(0, 500), 'policy');
    });
    addEventListener('message', event => {
      const d = event.data;
      if (initialized && event.source === frame.contentWindow && d && d.type === 'original-diagnostic' && ['error', 'policy'].includes(d.kind) && typeof d.message === 'string' && d.message.length <= 500 && diagnostics++ < 30) {
        send('diagnostic', d.message, d.kind); return;
      }
      if (initialized || event.source !== parent || !event.data || event.data.id !== config.id || event.data.generation !== config.generation || event.data.type !== 'initialize' || event.ports.length !== 1) return;
      initialized = true; port = event.ports[0];
      frame.addEventListener('load', () => send('loaded', '已装载（不代表验收成功）'));
      frame.srcdoc = config.html; document.body.append(frame);
    });
    parent.postMessage({type: 'shell-ready', id: config.id, generation: config.generation}, '*');
  }
  function create(container, options) {
    if (!(container instanceof Element) || !object(options)) fail('Invalid preview container/options');
    // Copy the descriptor so a caller cannot mutate a validated URL during an await.
    const descriptor = parseJSON(JSON.stringify(options.descriptor)); validateDescriptor(descriptor);
    const runId = options.runId;
    if (typeof runId !== 'string' || !runId || runId.length > 200) fail('Invalid run binding');
    if (options.transport !== undefined && (!['inline', 'external'].includes(options.transport) || descriptor.package && Object.hasOwn(descriptor.package, 'base64') !== (options.transport === 'inline'))) fail('Preview transport mismatch');
    const onState = typeof options.onState === 'function' ? options.onState : () => {};
    const onDiagnostic = typeof options.onDiagnostic === 'function' ? options.onDiagnostic : () => {};
    let viewport = options.viewport === undefined ? {width: options.width || 1440, height: options.height || 900} : options.viewport;
    let width = 1440, height = 900, scale = 1, lastState = Object.freeze({status: 'idle', message: '未启动', width, height, scale});
    let generation = 0, frame = null, wrapper = null, abort = null, channel = null, listener = null, destroyed = false;
    const id = 'preview-' + Array.from(crypto.getRandomValues(new Uint32Array(4)), x => x.toString(16)).join('-');
    function state(status, message) { lastState = Object.freeze({status, message: String(message || '').slice(0, 500), width, height, scale}); onState(lastState); }
    function layout() {
      const availableWidth = Math.max(1, container.clientWidth), availableHeight = Math.max(1, container.clientHeight);
      width = viewport === 'fit' ? Math.max(200, Math.min(4096, availableWidth)) : viewport.width;
      height = viewport === 'fit' ? Math.max(200, Math.min(4096, availableHeight)) : viewport.height;
      scale = Math.min(1, availableWidth / width, availableHeight / height);
      if (frame) {
        frame.style.width = width + 'px'; frame.style.height = height + 'px';
        frame.style.transform = 'scale(' + scale + ')'; frame.style.transformOrigin = '0 0';
        wrapper.style.width = width * scale + 'px'; wrapper.style.height = height * scale + 'px';
      }
      state(lastState.status, lastState.message);
    }
    function size(value, h) {
      if (destroyed) fail('Preview destroyed');
      if (typeof value === 'number') value = {width: value, height: h};
      if (value !== 'fit') {
        keys(value, ['width', 'height'], 'viewport');
        integer(value.width, 200, 4096, 'viewport width'); integer(value.height, 200, 4096, 'viewport height');
        value = {width: value.width, height: value.height};
      }
      viewport = value; layout();
    }
    function clean() {
      generation++;
      if (abort) abort.abort(); abort = null;
      if (listener) removeEventListener('message', listener); listener = null;
      if (channel) { channel.port1.close(); channel.port2.close(); } channel = null;
      if (wrapper) wrapper.remove(); wrapper = null;
      if (frame) frame.remove(); frame = null;
    }
    size(viewport);
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { if (!destroyed) layout(); }) : null;
    if (observer) observer.observe(container);
    function ownBytes(bytes) {
      if (!(bytes instanceof Uint8Array) || !descriptor.package || Reflect.get(Uint8Array.prototype, 'byteLength', bytes) !== descriptor.package.size) fail('Package size mismatch');
      return new Uint8Array(bytes);
    }
    async function start(bytes) {
      // Observe both outcomes immediately, even if this instance was just destroyed.
      let current;
      const pending = bytes instanceof Promise && Object.getPrototypeOf(bytes) === Promise.prototype ? Promise.prototype.then.call(bytes, value => {
        if (destroyed || current !== generation) return {};
        try { return {value: ownBytes(value)}; } catch (error) { return {error}; }
      }, error => ({error})) : null;
      if (destroyed) fail('Preview destroyed');
      // Raw input is copied before any await/callback. Promised input is copied
      // upon resolution, before verification callbacks/awaits. Never accept HTML.
      if (!pending && bytes !== undefined) bytes = ownBytes(bytes);
      clean(); current = generation;
      if (!descriptor.package) { state('unavailable', 'Original unavailable: ' + descriptor.status); return; }
      abort = new AbortController();
      try {
        state('fetching', '获取原作包');
        if (current !== generation || abort.signal.aborted) return;
        if (pending) {
          const result = await pending;
          if (current !== generation) return;
          if (Object.hasOwn(result, 'error')) throw result.error;
          bytes = result.value;
        } else if (bytes === undefined) bytes = await fetchBytes(descriptor, abort.signal);
        if (current !== generation) return;
        state('verifying', '完整性校验中');
        if (current !== generation || abort.signal.aborted) return;
        const prepared = await prepare({runId, descriptor, bytes});
        if (current !== generation) return;
        state('loading', '隔离与资源装载适配中');
        if (current !== generation || abort.signal.aborted) return;
        frame = document.createElement('iframe'); frame.setAttribute('sandbox', 'allow-scripts');
        frame.setAttribute('referrerpolicy', 'no-referrer'); frame.title = '原作隔离预览 ' + runId;
        frame.style.cssText = 'display:block;border:0;flex:none';
        wrapper = document.createElement('div'); wrapper.style.cssText = 'position:relative;overflow:hidden;flex:none'; wrapper.append(frame); layout();
        channel = new MessageChannel();
        channel.port1.onmessage = event => {
          const d = event.data;
          if (current !== generation || !object(d) || d.id !== id || d.generation !== current || typeof d.message !== 'string' || d.message.length > 500) return;
          if (d.status === 'loaded') state('loaded', d.message);
          else if (d.status === 'diagnostic' && ['error', 'policy'].includes(d.kind)) onDiagnostic(Object.freeze({kind: d.kind, message: d.message}));
        };
        const expected = frame;
        listener = event => {
          const d = event.data;
          if (current !== generation || !frame || event.source !== expected.contentWindow || !object(d) || d.type !== 'shell-ready' || d.id !== id || d.generation !== current) return;
          removeEventListener('message', listener); listener = null;
          expected.contentWindow.postMessage({type: 'initialize', id, generation: current}, '*', [channel.port2]);
        };
        addEventListener('message', listener);
        const policyTag = '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + INNER_POLICY + '">';
        const diagnosticTag = '<script>(' + originalDiagnostics.toString() + ')();<' + '/script>';
        const config = {id, generation: current, html: prepared.html.replace(policyTag, policyTag + diagnosticTag)};
        frame.srcdoc = '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + OUTER_POLICY + '"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden}</style></head><body><script>(' + shellBoot.toString() + ')(' + scriptJSON(config) + ');<' + '/script></body></html>';
        container.append(wrapper);
      } catch (error) {
        if (current !== generation) return;
        clean(); state('error', error instanceof Error ? error.message : 'Preview failed');
      }
    }
    function stop() { if (!destroyed) { clean(); state('stopped', '已停止'); } }
    function hidden() { if (document.hidden) stop(); }
    addEventListener('pagehide', stop); document.addEventListener('visibilitychange', hidden);
    state('idle', '未启动');
    return Object.freeze({start, stop, setViewport: size, getState: () => lastState, destroy() {
      if (destroyed) return;
      clean(); destroyed = true; if (observer) observer.disconnect(); removeEventListener('pagehide', stop); document.removeEventListener('visibilitychange', hidden); state('stopped', '已销毁');
    }});
  }
  window.BenchPreview = Object.freeze({create, prepare, validateDescriptor, readPackage});
})();
