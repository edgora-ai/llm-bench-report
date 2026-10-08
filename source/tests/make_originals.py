"""Read-only extraction and strict validation of reviewed original source packages.

No original is executed or rewritten here. HTMLParser locates real tags; a small
attribute lexer operates only on those parser-confirmed start tags. Offsets are
UTF-8 byte offsets, not Python character or browser UTF-16 offsets.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

FORMAT_V2 = "static-media-v2"
ORIGINAL_PATH = re.compile(r"originals/([a-f0-9]{64})\.json\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
MAX_ORIGINAL_BYTES = 2 * 1024 * 1024
MAX_FILES = 32
MIMES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
STATUSES = {"ready", "missing_dependencies", "no_entrypoint", "not_reviewed", "withheld", "unsupported"}


def _site():
    # Lazy imports keep make_site's public re-exports free from import cycles.
    import make_site
    return make_site


def require(condition, message):
    if not condition:
        raise RuntimeError("Unsafe original: " + message)


def relative(path):
    return _site()._relative(path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode_json(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def decode_base64(value):
    require(isinstance(value, str) and len(value) <= ((MAX_PACKAGE_BYTES + 2) // 3) * 4, "invalid base64 type/size")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise RuntimeError("Unsafe original: invalid base64") from error
    require(base64.b64encode(raw).decode("ascii") == value, "noncanonical base64")
    return raw


def scan_source(path, raw):
    from source_export import _scan_content
    _scan_content(path, raw)
    text = raw.decode("utf-8")
    require("\x00" not in text, "NUL in source")
    _site()._public_text(unquote(text))
    return text


class Unsupported(RuntimeError):
    """Known source constructs that this bounded adapter cannot preserve."""


def supported(condition, message):
    if not condition:
        raise Unsupported(message)


def resource_path(value):
    # HTMLParser already decoded the attribute exactly once. Decoding again
    # would turn literal '&amp;#46;' into a different browser resource path.
    if value.startswith(("#", "data:")):
        return None
    parsed = urlsplit(value)
    supported(not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment,
              "external or noncanonical resource")
    if value.startswith("./"):
        value = value[2:]
    relative(value)
    return value


# Matches one attribute at the current cursor, never searches arbitrary HTML.
ATTRIBUTE = re.compile(r'''\s+([^\s/=>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?''')


class EntryParser(HTMLParser):
    # Preview enables scripting: noscript content is inert raw text in that mode.
    CDATA_CONTENT_ELEMENTS = ("script", "style", "noscript", "title", "textarea")

    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0]
        self.lines.extend(match.end() for match in re.finditer("\n", text))
        self.head = None
        self.references = []
        self.before_head = True
        self.in_style = False
        self.in_script = False
        self.styles = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        line, column = self.getpos()
        offset = self.lines[line - 1] + column
        supported(len(dict(attrs)) == len(attrs), "duplicate source attribute")
        values = dict(attrs)
        supported(tag not in {"base", "iframe", "frame", "frameset", "object", "embed", "applet", "template", "xmp", "plaintext", "noembed", "noframes"},
                  "unsupported source document structure")
        if tag == "head":
            supported(self.head is None, "multiple head tags")
            self.head = len(self.text[:offset + len(raw)].encode("utf-8"))
            self.before_head = False
        elif self.before_head:
            supported(tag == "html", "content precedes explicit head")
        if tag == "script":
            self.in_script = True
            supported(values.get("type", "").lower() in {"", "text/javascript", "application/javascript"}, "module or nonclassic script")
            supported(not values.get("src", "").startswith(("data:", "#")), "unregistered executable source")
        if tag == "meta":
            supported("http-equiv" not in values, "HTTP-equivalent source meta")
            if "charset" in values:
                supported(values["charset"].lower() in {"utf-8", "utf8"}, "non-UTF8 document")
        supported(not {"srcset", "imagesrcset", "ping", "background", "codebase", "manifest"}.intersection(values), "unsupported resource attribute")
        if "style" in values:
            check_css(values["style"])
        if tag == "style":
            self.in_style = True
        cursor = re.match(r"<[^\s/>]+", raw).end()
        spans = {}
        while cursor < len(raw):
            if re.fullmatch(r"\s*/?>", raw[cursor:]):
                break
            match = ATTRIBUTE.match(raw, cursor)
            supported(match is not None, "ambiguous source attributes")
            name = match.group(1).lower()
            for group in (2, 3, 4):
                if match.group(group) is not None:
                    spans[name] = (offset + match.start(group), offset + match.end(group), match.group(group))
                    break
            cursor = match.end()
        for name in ("src", "href", "xlink:href", "poster", "action", "formaction", "data"):
            if not values.get(name):
                continue
            value = values[name]
            # SVG fragment references and inline data are left byte-for-byte alone.
            if value.startswith(("#", "data:")):
                continue
            path = resource_path(value)
            # Existing works include restart links to their own entry. They are
            # navigation, not runtime dependencies: leave them unchanged and let
            # the separately tested runtime navigation policy enforce isolation.
            if tag == "a" and name == "href" and path == "index.html":
                continue
            supported((tag == "script" and name == "src" and path.endswith(".js")) or
                      (tag == "link" and name == "href" and values.get("rel", "").lower() == "stylesheet" and path.endswith(".css")),
                      "resource is not a supported local classic script/stylesheet")
            start, end, original = spans[name]
            self.references.append({"start": len(self.text[:start].encode("utf-8")),
                                    "end": len(self.text[:end].encode("utf-8")),
                                    "path": path, "attribute": name, "original": original})

    def handle_startendtag(self, tag, attrs):
        supported(tag not in {"script", "head", "style"}, "self-closing active source tag")
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if tag == "script":
            self.in_script = False

    def handle_decl(self, decl):
        supported(self.before_head and decl.lower() == "doctype html", "unsupported document declaration")

    def unknown_decl(self, data):
        raise Unsupported("unsupported document declaration")

    def handle_pi(self, data):
        raise Unsupported("unsupported processing instruction")

    def handle_data(self, data):
        if self.before_head:
            prolog = data[1:] if self.getpos() == (1, 0) and data.startswith("﻿") else data
            supported(not prolog.strip(" \t\r\n"), "text precedes explicit head")
        if self.in_style:
            self.styles.append(data)
        if self.in_script:
            # HTMLParser does not implement HTML script escaped/double-escaped
            # states. Reject their entry marker before a misleading </script>
            # can cause JS string contents to be mistaken for resource tags.
            supported("<!--" not in data, "unsupported escaped script tokenizer state")
            self.scripts.append(data)


def check_js(text):
    # Conservative capability detection, not a JS safety proof. Ignore comments
    # and ordinary string literals to avoid flagging inert examples. The full
    # hash-bound assistant review remains mandatory, including dynamic code.
    clean = re.sub(r'''/\*.*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''',
                   " ", text, flags=re.S | re.X)
    supported(not re.search(r"\b(?:import|fetch|XMLHttpRequest|WebSocket|EventSource|Worker|SharedWorker|importScripts)\s*\(|\bimport\s+[\w{*]|\bserviceWorker\s*\.", clean),
              "unsupported dynamic/module resource")


def check_css(text):
    clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    supported(not re.search(r"@import|\\", clean, re.I), "unsupported CSS import/escape")
    for match in re.finditer(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", clean, re.I | re.S):
        supported(match.group(2).startswith(("data:", "#")), "unsupported CSS resource")


def adaptation(entry, available):
    text = entry.decode("utf-8")
    parser = EntryParser(text)
    parser.feed(text)
    parser.close()
    supported(parser.head is not None, "missing explicit head")
    for style in parser.styles:
        check_css(style)
    for script in parser.scripts:
        check_js(script)
    patches = [patch for patch in parser.references if patch["path"] in available]
    missing = sorted({patch["path"] for patch in parser.references if patch["path"] not in available})
    return {"version": 1, "head_offset": parser.head, "patches": patches}, missing


def validate_original_package(raw, run_id=None, descriptor=None):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_PACKAGE_BYTES, "package size")
    package = _site()._json(raw)
    require(isinstance(package, dict) and set(package) == {"version", "run_id", "entrypoint", "files", "adaptation", "missing"}, "package schema")
    require(type(package["version"]) is int and package["version"] == 1, "package version")
    relative(package["run_id"])
    require("/" not in package["run_id"] and (run_id is None or package["run_id"] == run_id), "package run binding")
    require(package["entrypoint"] == "index.html", "package entrypoint")
    files = package["files"]
    require(isinstance(files, dict) and 1 <= len(files) <= MAX_FILES and "index.html" in files, "package files")
    decoded = {}
    for path, info in files.items():
        relative(path)
        require(path == "index.html" or Path(path).suffix in {".js", ".css"}, "unexpected packaged path")
        require(isinstance(info, dict) and set(info) == {"mime", "size", "sha256", "base64"}, "file schema")
        require(info["mime"] == MIMES.get(Path(path).suffix), "file MIME")
        require(type(info["size"]) is int and 0 <= info["size"] <= MAX_ORIGINAL_BYTES, "raw file size")
        data = decode_base64(info["base64"])
        require(len(data) == info["size"] and digest(data) == info["sha256"], "raw file integrity")
        text = scan_source(path, data)
        if path.endswith(".css"):
            check_css(text)
        if path.endswith(".js"):
            check_js(text)
        decoded[path] = data
    adapter = package["adaptation"]
    require(isinstance(adapter, dict) and set(adapter) == {"version", "head_offset", "patches"}
            and type(adapter["version"]) is int and adapter["version"] == 1
            and type(adapter["head_offset"]) is int and isinstance(adapter["patches"], list), "adaptation schema")
    for patch in adapter["patches"]:
        require(isinstance(patch, dict) and set(patch) == {"start", "end", "path", "attribute", "original"}
                and type(patch["start"]) is int and type(patch["end"]) is int
                and isinstance(patch["path"], str) and isinstance(patch["attribute"], str)
                and isinstance(patch["original"], str), "patch schema")
    expected, missing = adaptation(decoded["index.html"], files)
    require(adapter == expected and package["missing"] == missing, "adaptation/missing mismatch")
    require(set(files) == {"index.html"} | {patch["path"] for patch in expected["patches"]}, "unreferenced packaged file")
    if descriptor is not None:
        require(descriptor["entry_sha256"] == digest(decoded["index.html"]) and descriptor["missing"] == missing, "descriptor entry/missing binding")
        info = descriptor["package"]
        require(info is not None and info["sha256"] == digest(raw) and info["size"] == len(raw), "descriptor package integrity")
    return package


def validate_original_descriptor(descriptor, transport):
    require(transport in ("external", "inline"), "transport")
    require(isinstance(descriptor, dict) and set(descriptor) == {"status", "entrypoint", "entry_sha256", "missing", "policy", "package"}, "descriptor schema")
    require(isinstance(descriptor["status"], str) and descriptor["status"] in STATUSES and descriptor["policy"] == "opaque-srcdoc-v1", "descriptor policy/status")
    missing = descriptor["missing"]
    require(isinstance(missing, list) and all(isinstance(item, str) for item in missing) and missing == sorted(set(missing)), "missing paths")
    for path in missing:
        relative(path)
    if descriptor["status"] == "no_entrypoint":
        require(descriptor["entrypoint"] is None and descriptor["entry_sha256"] is None and not missing, "absent entrypoint metadata")
    else:
        require(descriptor["entrypoint"] == "index.html" and isinstance(descriptor["entry_sha256"], str) and SHA.fullmatch(descriptor["entry_sha256"]), "entry descriptor")
    ready = descriptor["status"] in {"ready", "missing_dependencies"}
    require((descriptor["package"] is not None) == ready, "status/package mismatch")
    if ready:
        require((descriptor["status"] == "missing_dependencies") == bool(missing), "missing status mismatch")
        info = descriptor["package"]
        require(isinstance(info, dict) and set(info) == {"sha256", "size", "path" if transport == "external" else "base64"}, "package descriptor schema")
        require(isinstance(info["sha256"], str) and SHA.fullmatch(info["sha256"]), "package digest")
        require(type(info["size"]) is int and 0 < info["size"] <= MAX_PACKAGE_BYTES, "package descriptor size")
        if transport == "external":
            require(info["path"] == "originals/" + info["sha256"] + ".json", "package path")
        else:
            raw = decode_base64(info["base64"])
            require(len(raw) == info["size"] and digest(raw) == info["sha256"], "inline package integrity")
    return descriptor


def validate_originals(data, payloads=None):
    require(data.get("format") == FORMAT_V2 and data.get("transport") in ("external", "inline"), "report format/transport")
    originals = data.get("originals")
    require(isinstance(originals, dict) and set(originals) == {run["id"] for run in data["runs"]}, "originals run coverage")
    result = {}
    for run_id, descriptor in originals.items():
        validate_original_descriptor(descriptor, data["transport"])
        info = descriptor["package"]
        if info is None:
            continue
        if data["transport"] == "inline":
            raw = decode_base64(info["base64"])
        elif payloads is not None:
            require(info["path"] in payloads, "missing original package")
            raw = payloads[info["path"]]
        else:
            continue
        result[run_id] = validate_original_package(raw, run_id, descriptor)
    return result


def validate_audit(audit):
    require(isinstance(audit, dict) and set(audit) == {"version", "reviewer", "files"} and
            type(audit["version"]) is int and audit["version"] == 1 and audit["reviewer"] == "assistant" and isinstance(audit["files"], dict), "review schema")
    for path, info in audit["files"].items():
        relative(path)
        require("/" in path and isinstance(info, dict) and set(info) == {"sha256", "decision", "reviewed_full", "reason"}, "review file schema")
        require(isinstance(info["sha256"], str) and SHA.fullmatch(info["sha256"]) and info["decision"] in ("include", "withhold") and info["reviewed_full"] is True and isinstance(info["reason"], str) and bool(info["reason"].strip()), "review decision")
    return audit


def build_originals(runs, archive_root, audit):
    """Return (external descriptors, content-addressed package bytes), no writes.

    archive_root is the runs directory, containing date/run-id/manifest.json.
    Only report IDs/dates select locations; archive_dir is never trusted.
    """
    if isinstance(audit, (str, Path)):
        audit = _site()._json(_site()._read(audit))
    validate_audit(audit)
    originals, payloads = {}, {}
    for run in runs:
        run_id = relative(run["id"])
        date = relative(run["date"])
        require("/" not in run_id and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), "archive locator")
        directory = Path(archive_root) / date / run_id
        manifest = _site()._json(_site().read_asset_safe(directory, "manifest.json"))
        checksums = _site()._json(_site().read_asset_safe(directory, "checksums.json"))
        require(isinstance(manifest, dict) and manifest.get("id") == run_id and manifest.get("date") == date, "archive run binding")
        require(isinstance(checksums, dict) and isinstance(manifest.get("artifacts"), list), "archive registration schema")
        registered = {}
        for item in manifest["artifacts"]:
            require(isinstance(item, dict), "artifact schema")
            path = item.get("path")
            # Archive registrations also contain Playwright page@... evidence.
            # Such files are never selected; original output paths retain the
            # stricter public canonical alphabet below.
            require(isinstance(path, str) and bool(path) and all(re.fullmatch(r"[A-Za-z0-9_.@-]+", part)
                    and part not in {".", ".."} for part in path.split("/")), "artifact registration path")
            if path.startswith("output/"):
                relative(path)
            require(path not in registered, "duplicate artifact registration")
            registered[path] = item
        descriptor = {"status": "no_entrypoint", "entrypoint": None, "entry_sha256": None,
                      "missing": [], "policy": "opaque-srcdoc-v1", "package": None}
        originals[run_id] = descriptor
        if "output/index.html" not in registered:
            continue

        def read_output(path):
            key = "output/" + relative(path)
            item = registered[key]
            require(item.get("kind") == "output" and type(item.get("size")) is int and 0 <= item["size"] <= MAX_ORIGINAL_BYTES, "output registration")
            raw = _site().read_asset_safe(directory, key, max_bytes=MAX_ORIGINAL_BYTES)
            require(len(raw) == item["size"] and digest(raw) == item.get("sha256") == checksums.get(key), "archive output hash/size mismatch")
            if "generation_artifacts" in manifest:
                generation = manifest["generation_artifacts"]
                require(isinstance(generation, list), "generation artifact schema")
                matching = [entry for entry in generation if isinstance(entry, dict) and entry.get("path") == key]
                require(len(matching) == 1 and matching[0].get("size") == len(raw) and matching[0].get("sha256") == digest(raw), "generation artifact mismatch")
            return raw

        entry = read_output("index.html")
        descriptor.update(status="unsupported", entrypoint="index.html", entry_sha256=digest(entry))
        available = {path.removeprefix("output/") for path, item in registered.items() if path.startswith("output/") and item.get("kind") == "output"}
        try:
            adapter, missing = adaptation(entry, available)
            descriptor["missing"] = missing
            selected = {"index.html"} | {patch["path"] for patch in adapter["patches"]}
            supported(len(selected) <= MAX_FILES, "too many original files")
            raw_files = {path: entry if path == "index.html" else read_output(path) for path in sorted(selected)}
            decisions = []
            for path, raw in raw_files.items():
                review = audit["files"].get(run_id + "/" + path)
                decisions.append(review["decision"] if review and review["sha256"] == digest(raw) else "not_reviewed")
            if "withhold" in decisions:
                descriptor["status"] = "withheld"
                continue
            if "not_reviewed" in decisions:
                descriptor["status"] = "not_reviewed"
                continue
            files = {}
            for path, raw in raw_files.items():
                text = scan_source(path, raw)
                if path.endswith(".css"):
                    check_css(text)
                if path.endswith(".js"):
                    check_js(text)
                files[path] = {"mime": MIMES[Path(path).suffix], "size": len(raw), "sha256": digest(raw),
                               "base64": base64.b64encode(raw).decode("ascii")}
            package = {"version": 1, "run_id": run_id, "entrypoint": "index.html", "files": files,
                       "adaptation": adapter, "missing": missing}
            raw = encode_json(package)
            supported(len(raw) <= MAX_PACKAGE_BYTES, "original package too large")
            validate_original_package(raw, run_id)
            path = "originals/" + digest(raw) + ".json"
            descriptor.update(status="missing_dependencies" if missing else "ready",
                              package={"sha256": digest(raw), "size": len(raw), "path": path})
            payloads[path] = raw
        except Unsupported:
            descriptor["status"] = "unsupported"
    return originals, payloads
