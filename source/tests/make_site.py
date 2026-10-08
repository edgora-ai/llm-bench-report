#!/usr/bin/env python3
"""Build a verified static report from a trusted snapshot or upgrade a verified site.

Legacy builds use no archives. The explicit --upgrade-site mode reads only
registered, reviewed original sources and reuses every existing media byte and
mapping. Neither mode executes originals, calls an API, or encodes video. The
explicit old viewer authenticates input; the current template renders output.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import secrets
from urllib.parse import parse_qsl, unquote, urlsplit
import warnings

from PIL import Image

from make_snapshot import ROOT, render_snapshot
from make_originals import (FORMAT_V2, ORIGINAL_PATH, MAX_PACKAGE_BYTES, build_originals,
                            validate_original_descriptor, validate_original_package, validate_originals)

FORMAT = "static-media-v1"
MANIFEST = ".report-manifest.json"
MIME_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                   "image/gif": "gif", "video/webm": "webm", "video/mp4": "mp4"}
IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
MEDIA_PATH = re.compile(r"media/([a-f0-9]{64})\.(jpg|png|webp|gif|webm|mp4)\Z")
DATA_URI = re.compile(r"data:(image/(?:jpeg|png|webp|gif)|video/(?:webm|mp4));base64,([A-Za-z0-9+/=\r\n]+)\Z")
EVIDENCE_SUFFIX = re.compile(r"\.(png|jpe?g|webp|gif|webm|mp4)\Z", re.I)
MAX_FILE_BYTES = 256 * 1024 * 1024
INDEX_BUDGET = 1024 * 1024
THUMBNAIL_WIDTH = 480
PRIVATE_KEYS = {"archivedir", "clicommand", "usageraw", "apikey", "accesstoken", "authorization",
                "password", "passwd", "secret", "clientsecret", "token", "refreshtoken", "cookie",
                "setcookie", "dashboardtoken", "privatekey", "credential", "credentials"}
PRIVATE_TEXT = re.compile(r"/home/|/Users/|/workspace(?:/|\b)|/root/|127\.0\.0\.1|\blocalhost\b|data/dashboard-token|runs/20\d\d-", re.I)
CREDENTIAL_TEXT = re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9]|gh[pousr]_|github_pat_|xox[baprs]-)|-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
STATS_KEYS = {"runs", "benchmark_runs", "reviews", "evidence", "unique_evidence", "images", "videos",
              "thumbnails", "media_files", "media_bytes", "index_bytes", "offline_bytes"}


def _require(condition, message):
    if not condition:
        raise RuntimeError("Unsafe static report: " + message)


def _relative(path):
    _require(isinstance(path, str) and bool(path) and all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."}
        for part in path.split("/")), "noncanonical relative path")
    return path


@contextmanager
def _directory(path):
    """Open every parent with O_NOFOLLOW, not just the final path component."""
    path = Path(path).absolute()
    _require(".." not in path.parts, "parent traversal in filesystem path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as error:
        raise RuntimeError("Unsafe static report directory: " + str(path)) from error
    finally:
        os.close(fd)


def read_asset_safe(directory: Path, relative: str, *, max_bytes=MAX_FILE_BYTES) -> bytes:
    """Read a canonical regular file, rejecting symlinks and special files."""
    _relative(relative)
    path = Path(directory) / relative
    with _directory(path.parent) as parent:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                _require(stat.S_ISREG(before.st_mode), "asset is not a regular file: " + relative)
                _require(before.st_size <= max_bytes, "asset exceeds size limit: " + relative)
                payload = handle.read(max_bytes + 1)
                after = os.fstat(handle.fileno())
                _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                         == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                         and len(payload) == before.st_size, "asset changed while reading: " + relative)
                return payload
        except OSError as error:
            raise RuntimeError("Unsafe static report file: " + relative) from error


def _read(path):
    path = Path(path)
    return read_asset_safe(path.parent, path.name)


def _inventory(directory):
    files, directories = set(), set()

    def visit(fd, prefix=""):
        for name in os.listdir(fd):
            relative = _relative(prefix + name)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, relative + "/")
                finally:
                    os.close(child)
            else:
                _require(stat.S_ISREG(info.st_mode), "symlink or special file: " + relative)
                files.add(relative)

    with _directory(directory) as fd:
        visit(fd)
    return files, directories


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise RuntimeError("Unsafe static report: nonfinite JSON value " + value)

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as error:
        raise RuntimeError("Unsafe static report: invalid JSON") from error


def _public_text(value):
    _require(not PRIVATE_TEXT.search(value), "private host detail in public data")
    _require(not CREDENTIAL_TEXT.search(value), "credential signature in public data")
    for match in re.finditer(r"https?://[^\s<>\"']+", value, re.I):
        try:
            url = urlsplit(match.group())
            _require(not url.username and not url.password, "credential-bearing URL")
            for key, content in parse_qsl(url.query):
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                _require(not content or normalized not in PRIVATE_KEYS | {"key", "auth"}, "credential-bearing URL query")
        except ValueError as error:
            raise RuntimeError("Unsafe static report: invalid URL") from error


def _public_data(value):
    if isinstance(value, dict):
        for key, child in value.items():
            _require(re.sub(r"[^a-z0-9]", "", key.lower()) not in PRIVATE_KEYS, "private field: " + key)
            _public_text(key)
            _public_data(child)
    elif isinstance(value, list):
        for child in value:
            _public_data(child)
    elif isinstance(value, str):
        if not DATA_URI.fullmatch(value):
            _public_text(unquote(value))


def _css(text):
    clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    _require(not re.search(r"url\s*\(|@import|expression\s*\(|-moz-binding|(?:^|[;{])\s*behavior\s*:|\\", clean, re.I),
             "CSS contains a resource or executable construct")


class _ReportHTML(HTMLParser):
    def __init__(self, static):
        super().__init__(convert_charrefs=False)
        self.static = static
        self.scripts = []
        self.current = None
        self.in_style = False
        self.references = []

    def handle_starttag(self, tag, attrs):
        _require(len({key for key, _ in attrs}) == len(attrs), "duplicate HTML attribute")
        attrs = dict(attrs)
        _require(tag not in {"iframe", "object", "embed", "base", "svg", "math", "link", "applet"}, "active/external markup: " + tag)
        _require(not any(key.startswith("on") for key in attrs) and "srcdoc" not in attrs, "inline executable attribute")
        _require(not {"srcset", "imagesrcset", "ping", "background", "codebase", "manifest"}.intersection(attrs), "unmanaged resource attribute")
        _require(tag != "meta" or "http-equiv" not in attrs, "HTTP-equivalent meta directive")
        if "style" in attrs:
            _css(attrs["style"] or "")
        for name, value in attrs.items():
            if value:
                _public_text(value)
            if name in {"href", "src", "poster", "action", "formaction", "xlink:href", "data"} and value:
                if tag == "a" and name == "href":
                    _require(value.startswith("#") or (self.static and value == "offline.html"), "unmanaged navigation")
                    if value == "offline.html":
                        _require("download" in attrs, "offline link must be an explicit download")
                elif tag in {"img", "video", "source"} and name in {"src", "poster"}:
                    _require(bool(MEDIA_PATH.fullmatch(value) if self.static else DATA_URI.fullmatch(value)), "unmanaged media reference")
                    self.references.append(value)
                else:
                    _require(False, "external resource or form action")
        if tag == "script":
            _require(self.current is None and set(attrs) <= {"type"} and attrs.get("type", "") in {"", "text/javascript"}, "non-viewer script attributes")
            self.current = []
        if tag == "style":
            self.in_style = True

    def handle_startendtag(self, tag, attrs):
        _require(tag not in {"script", "style"}, "self-closing active element")
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)
        elif self.in_style:
            _css(data)
            _public_text(data)
        else:
            _public_text(data)

    def handle_comment(self, text):
        _public_text(text)

    def handle_endtag(self, tag):
        if tag == "script":
            _require(self.current is not None, "unmatched script closing tag")
            self.scripts.append("".join(self.current))
            self.current = None
        if tag == "style":
            self.in_style = False


def audit_snapshot(html: str, viewer_script: Path, *, static=False, preview_runtime=None) -> dict:
    """Parse JSON, never evaluate input JavaScript; match the viewer byte-for-byte."""
    parsed = _ReportHTML(static)
    parsed.feed(html)
    parsed.close()
    _require(parsed.current is None and len(parsed.scripts) == 2, "expected exactly two scripts")
    match = re.fullmatch(r"window\.BENCH_SNAPSHOT=(\{.*\});", parsed.scripts[0], re.S)
    _require(match is not None and "<" not in parsed.scripts[0], "snapshot JSON must be safely escaped")
    data = _json(match.group(1))
    _require(isinstance(data, dict) and isinstance(data.get("runs"), list)
             and isinstance(data.get("evidence"), dict), "invalid snapshot shape")
    v2 = data.get("format") == FORMAT_V2
    if v2:
        _require(data.get("transport") == ("external" if static else "inline"), "invalid v2 transport/profile")
        _require(set(data) <= {"runs", "tasks", "evidence", "format", "transport", "originals", "assets", "thumbnails", "offline"}, "unknown v2 report fields")
    else:
        _require(data.get("format") == (FORMAT if static else None), "invalid report format/profile")
        _require(not {"transport", "originals"}.intersection(data), "v2 fields in legacy report")
    expected_viewer = _read(viewer_script).decode("utf-8")
    if v2:
        runtime = Path(preview_runtime) if preview_runtime is not None else Path(viewer_script).with_name("preview-runtime.js")
        expected_viewer = _read(runtime).decode("utf-8") + "\n" + expected_viewer
    _require(parsed.scripts[1] == expected_viewer, "input viewer mismatch")
    _require("tasks" not in data or isinstance(data["tasks"], list), "invalid tasks")
    _public_data(data)
    ids, registered = set(), set()
    for run in data["runs"]:
        _require(isinstance(run, dict) and isinstance(run.get("id"), str), "invalid run")
        run_id = _relative(run["id"])
        _require("/" not in run_id and run_id not in ids, "duplicate or invalid run ID")
        ids.add(run_id)
        evaluation = run.get("evaluation") or {}
        _require(isinstance(evaluation, dict), "invalid evaluation")
        paths = evaluation.get("evidence") or []
        _require(isinstance(paths, list), "invalid evidence registration")
        for item in paths:
            path = item.get("path") if isinstance(item, dict) else item
            _relative(path)
            _require(EVIDENCE_SUFFIX.search(path), "non-raster/video evidence registration")
            registered.add(run_id + "/" + path)
    for key, value in data["evidence"].items():
        _relative(key)
        _require(key in registered, "unregistered evidence mapping")
        _require(isinstance(value, str) and bool(MEDIA_PATH.fullmatch(value) if static else DATA_URI.fullmatch(value)), "invalid evidence media")
    if static:
        _require(data.get("format") in {FORMAT, FORMAT_V2} and isinstance(data.get("assets"), dict)
                 and isinstance(data.get("thumbnails"), dict), "invalid static media fields")
        _require(all(ref in data["assets"] for ref in parsed.references), "unlisted markup media")
    else:
        _require(not ({"assets", "thumbnails", "offline"} | (set() if v2 else {"format"})).intersection(data), "offline input contains static fields")
        _require(all(ref in data["evidence"].values() for ref in parsed.references), "unlisted inline media")
    if v2:
        validate_originals(data)
    return data


def _decode_uri(uri):
    match = DATA_URI.fullmatch(uri)
    _require(match is not None, "unsafe data URI MIME")
    try:
        payload = base64.b64decode(match.group(2).replace("\r", "").replace("\n", ""), validate=True)
    except (ValueError, binascii.Error) as error:
        raise RuntimeError("Unsafe static report: invalid base64") from error
    _require(bool(payload), "empty media")
    return match.group(1), payload


def _vint(payload, offset, *, identifier=False):
    _require(offset < len(payload) and payload[offset] != 0, "invalid WebM header")
    first = payload[offset]
    length = 1
    while not first & (0x80 >> (length - 1)):
        length += 1
    _require(length <= (4 if identifier else 8) and offset + length <= len(payload), "invalid WebM element")
    value = int.from_bytes(payload[offset:offset + length], "big")
    if not identifier:
        value &= (1 << (7 * length)) - 1
    return value, offset + length


def _webm(payload):
    _require(payload.startswith(b"\x1a\x45\xdf\xa3"), "WebM magic mismatch")
    length, pos = _vint(payload, 4)
    end = pos + length
    _require(end <= min(len(payload), 4096), "invalid WebM header size")
    doctype = None
    while pos < end:
        field, pos = _vint(payload, pos, identifier=True)
        size, pos = _vint(payload, pos)
        _require(pos + size <= end, "truncated WebM header")
        if field == 0x4282:
            doctype = payload[pos:pos + size]
        pos += size
    _require(doctype == b"webm" and payload[end:end + 4] == b"\x18\x53\x80\x67", "WebM document/segment mismatch")
    _, content = _vint(payload, end + 4)
    _require(len(payload) > content + 16, "empty WebM segment")


def _media_info(payload, mime):
    _require(mime in MIME_EXTENSIONS and 0 < len(payload) <= MAX_FILE_BYTES, "invalid media MIME or size")
    info = {"sha256": hashlib.sha256(payload).hexdigest(), "mime": mime, "size": len(payload)}
    if mime.startswith("image/"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(payload)) as image:
                    _require(IMAGE_FORMATS.get(image.format) == mime, "image MIME mismatch")
                    image.verify()
                with Image.open(BytesIO(payload)) as image:
                    for frame in range(getattr(image, "n_frames", 1)):
                        image.seek(frame)
                        image.load()
                    info.update(width=image.width, height=image.height)
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise RuntimeError("Unsafe static report: image does not decode") from error
    else:
        if mime == "video/webm":
            _webm(payload)
        else:
            _require(len(payload) >= 24 and payload[4:8] == b"ftyp" and 16 <= int.from_bytes(payload[:4], "big") <= len(payload), "MP4 magic mismatch")
        probe = shutil.which("ffprobe")
        if probe:
            try:
                result = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                         "stream=width,height,codec_name", "-of", "json", "pipe:0"],
                                        input=payload, capture_output=True, timeout=30)
                streams = _json(result.stdout).get("streams", [])
                _require(result.returncode == 0 and len(streams) == 1, "video has no decodable stream")
                stream = streams[0]
                _require(type(stream.get("width")) is int and stream["width"] > 0
                         and type(stream.get("height")) is int and stream["height"] > 0, "invalid video dimensions")
                info.update(width=stream["width"], height=stream["height"])
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RuntimeError("Unsafe static report: video probe failed") from error
    return info


def _thumbnail(payload):
    with Image.open(BytesIO(payload)) as image:
        image.seek(0)
        image = image.convert("RGB")
        width = min(image.width, THUMBNAIL_WIDTH)
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, "JPEG", quality=78, optimize=True, progressive=True)
        return output.getvalue()


def _file_info(payload, mime, role):
    return {"sha256": hashlib.sha256(payload).hexdigest(), "mime": mime, "size": len(payload), "role": role}


def validate_manifest(manifest: dict) -> dict:
    """Validate the exact manifest schema and canonical content-addressed names."""
    _require(isinstance(manifest, dict) and set(manifest) == {"version", "format", "files", "stats"}, "invalid manifest fields")
    _require(type(manifest["version"]) is int and (manifest["version"], manifest["format"]) in ((1, FORMAT), (2, FORMAT_V2)), "unsupported manifest version")
    v2 = manifest["version"] == 2
    files = manifest["files"]
    _require(isinstance(files, dict) and {"index.html", "offline.html"} <= set(files), "missing entrypoint or offline file")
    for path, info in files.items():
        _relative(path)
        media = MEDIA_PATH.fullmatch(path)
        original = ORIGINAL_PATH.fullmatch(path) if v2 else None
        _require(media is not None or original is not None or path in {"index.html", "offline.html"}, "unmanaged manifest file")
        _require(isinstance(info, dict) and {"sha256", "mime", "size", "role"} <= set(info)
                 <= {"sha256", "mime", "size", "role", "width", "height"}, "invalid asset metadata")
        _require(isinstance(info["mime"], str) and isinstance(info["role"], str), "invalid MIME/role types")
        _require(isinstance(info["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", info["sha256"]), "invalid SHA-256")
        _require(type(info["size"]) is int and 0 < info["size"] <= MAX_FILE_BYTES, "invalid asset size")
        _require(("width" in info) == ("height" in info), "incomplete dimensions")
        for dimension in ("width", "height"):
            if dimension in info:
                _require(type(info[dimension]) is int and 0 < info[dimension] <= 100000, "invalid dimensions")
        if media:
            _require(info["sha256"] == media.group(1) and MIME_EXTENSIONS.get(info["mime"]) == media.group(2), "media name/hash/MIME mismatch")
            _require(info["role"] in {"evidence", "thumbnail", "evidence+thumbnail"}, "invalid media role")
            _require(not info["mime"].startswith("image/") or "width" in info, "missing raster dimensions")
            _require("thumbnail" not in info["role"] or info["mime"] == "image/jpeg", "thumbnails must be JPEG")
        elif original:
            _require(set(info) == {"sha256", "mime", "size", "role"} and info["sha256"] == original.group(1)
                     and info["mime"] == "application/json" and info["role"] == "original"
                     and info["size"] <= MAX_PACKAGE_BYTES, "invalid original metadata")
        else:
            _require(info["mime"] == "text/html" and info["role"] == path.removesuffix(".html")
                     and "width" not in info, "invalid HTML metadata")
    stats = manifest["stats"]
    stats_keys = STATS_KEYS | ({"original_packages", "original_bytes", "original_files"} if v2 else set())
    _require(isinstance(stats, dict) and set(stats) == stats_keys
             and all(type(value) is int and value >= 0 for value in stats.values()), "invalid manifest statistics")
    _require(stats["index_bytes"] == files["index.html"]["size"] <= INDEX_BUDGET
             and stats["offline_bytes"] == files["offline.html"]["size"], "invalid HTML size statistics or entrypoint budget")
    return manifest


def _stats(data, files, packages=None):
    media = {path: info for path, info in files.items() if MEDIA_PATH.fullmatch(path)}
    result = {"runs": len(data["runs"]), "benchmark_runs": sum(run.get("purpose") == "benchmark" for run in data["runs"]),
            "reviews": sum(len(run.get("reviews") or []) for run in data["runs"]),
            "evidence": len(data["evidence"]), "unique_evidence": len(set(data["evidence"].values())),
            "images": sum(media[path]["mime"].startswith("image/") for path in data["evidence"].values()),
            "videos": sum(media[path]["mime"].startswith("video/") for path in data["evidence"].values()),
            "thumbnails": len(data["thumbnails"]), "media_files": len(media),
            "media_bytes": sum(info["size"] for info in media.values()),
            "index_bytes": files["index.html"]["size"], "offline_bytes": files["offline.html"]["size"]}
    if data.get("format") == FORMAT_V2:
        originals = {path: info for path, info in files.items() if ORIGINAL_PATH.fullmatch(path)}
        _require(packages is not None, "original statistics require validated packages")
        result.update(original_packages=len(originals), original_bytes=sum(info["size"] for info in originals.values()),
                      original_files=sum(len(package["files"]) for package in packages.values()))
    return result


def verify_site(directory: Path, viewer_script: Path = ROOT / "web/app.js", *, preview_runtime=None) -> dict:
    """Verify a standalone bundle completely; extra files/directories fail closed."""
    manifest = validate_manifest(_json(read_asset_safe(directory, MANIFEST)))
    files, directories = _inventory(directory)
    expected_dirs = {"media"} if any(MEDIA_PATH.fullmatch(path) for path in manifest["files"]) else set()
    if any(ORIGINAL_PATH.fullmatch(path) for path in manifest["files"]):
        expected_dirs.add("originals")
    _require(files == set(manifest["files"]) | {MANIFEST} and directories == expected_dirs, "missing or unmanaged bundle files")
    payloads = {}
    for path, info in manifest["files"].items():
        payload = read_asset_safe(directory, path)
        _require(len(payload) == info["size"] and hashlib.sha256(payload).hexdigest() == info["sha256"], "asset hash/size mismatch: " + path)
        if MEDIA_PATH.fullmatch(path):
            actual = _media_info(payload, info["mime"])
            _require(all(actual.get(key) == value for key, value in info.items() if key != "role"
                         and (key not in {"width", "height"} or key in actual)), "media metadata mismatch: " + path)
        payloads[path] = payload
    data = audit_snapshot(payloads["index.html"].decode("utf-8"), viewer_script, static=True, preview_runtime=preview_runtime)
    offline = audit_snapshot(payloads["offline.html"].decode("utf-8"), viewer_script, preview_runtime=preview_runtime)
    _require(data.get("format") == manifest["format"], "manifest/report format mismatch")
    if manifest["format"] == FORMAT:
        _require(not {"format", "transport", "originals"}.intersection(offline), "offline profile mismatch: v1 requires legacy inline data")
        _require(not {"transport", "originals"}.intersection(data), "v2 fields in legacy report")
    packages = None
    if manifest["format"] == FORMAT_V2:
        _require(offline.get("format") == FORMAT_V2, "offline format mismatch")
        packages = validate_originals(data, payloads)
        validate_originals(offline)
        original_paths = {descriptor["package"]["path"] for descriptor in data["originals"].values() if descriptor["package"]}
        _require(original_paths == {path for path in manifest["files"] if ORIGINAL_PATH.fullmatch(path)}, "unreferenced original package")
        for run_id, descriptor in data["originals"].items():
            inline = offline["originals"][run_id]
            _require({key: value for key, value in descriptor.items() if key != "package"} ==
                     {key: value for key, value in inline.items() if key != "package"}, "original descriptors differ")
            if descriptor["package"]:
                expected = descriptor["package"]
                actual = inline["package"]
                _require(actual["sha256"] == expected["sha256"] and actual["size"] == expected["size"]
                         and base64.b64decode(actual["base64"], validate=True) == payloads[expected["path"]], "online/inline original bytes differ")
    assets = {path: info for path, info in manifest["files"].items() if MEDIA_PATH.fullmatch(path)}
    _require(data["assets"] == assets, "index assets differ from manifest")
    offline_info = manifest["files"]["offline.html"]
    _require(data.get("offline") == {"path": "offline.html", "sha256": offline_info["sha256"], "size": offline_info["size"]}, "offline descriptor mismatch")
    excluded = {"format", "evidence", "thumbnails", "assets", "offline", "transport", "originals"}
    original = {key: value for key, value in data.items() if key not in excluded}
    _require(original == {key: value for key, value in offline.items() if key not in excluded}, "index/offline metadata differ")
    _require(set(data["evidence"]) == set(offline["evidence"]), "index/offline evidence keys differ")
    for key, path in data["evidence"].items():
        _require(path in assets, "missing evidence asset")
        mime, payload = _decode_uri(offline["evidence"][key])
        _require(assets[path]["mime"] == mime and payloads[path] == payload, "offline evidence bytes differ")
    images = {key for key, path in data["evidence"].items() if assets[path]["mime"].startswith("image/")}
    _require(set(data["thumbnails"]) == images, "missing or extra image thumbnail")
    for key, path in data["thumbnails"].items():
        _require(isinstance(path, str) and MEDIA_PATH.fullmatch(path) and path in assets, "invalid thumbnail path")
        full, thumb = assets[data["evidence"][key]], assets[path]
        width = min(full["width"], THUMBNAIL_WIDTH)
        _require(thumb["mime"] == "image/jpeg" and thumb["width"] == width
                 and thumb["height"] == max(1, round(full["height"] * width / full["width"])), "thumbnail dimensions/aspect mismatch")
    evidence_paths, thumbnail_paths = set(data["evidence"].values()), set(data["thumbnails"].values())
    _require(set(assets) == evidence_paths | thumbnail_paths, "unreferenced media asset")
    for path, info in assets.items():
        role = "evidence+thumbnail" if path in evidence_paths & thumbnail_paths else "evidence" if path in evidence_paths else "thumbnail"
        _require(info["role"] == role, "media role mismatch")
    _require(manifest["stats"] == _stats(data, manifest["files"], packages), "manifest statistics mismatch")
    return manifest


def build_site(snapshot: Path, destination: Path, input_viewer: Path, root: Path = ROOT) -> dict:
    """Build only into an empty destination, or accept an identical verified rerun."""
    snapshot, destination, root = Path(snapshot), Path(destination), Path(root)
    original = audit_snapshot(_read(snapshot).decode("utf-8"), input_viewer)
    payloads, assets, evidence, thumbnails = {}, {}, {}, {}
    decoded = {}
    # Validate every input before creating any output, even if a late entry fails.
    for key, uri in original["evidence"].items():
        mime, payload = _decode_uri(uri)
        digest = hashlib.sha256(payload).hexdigest()
        if digest not in decoded:
            decoded[digest] = (payload, _media_info(payload, mime))
        _require(decoded[digest][1]["mime"] == mime, "same bytes declared with different MIME")
        evidence[key] = "media/" + digest + "." + MIME_EXTENSIONS[mime]
    for key, path in evidence.items():
        payload, info = decoded[MEDIA_PATH.fullmatch(path).group(1)]
        payloads[path] = payload
        assets[path] = {**info, "role": "evidence"}
    thumb_cache = {}
    for key, path in evidence.items():
        if assets[path]["mime"].startswith("image/"):
            if path not in thumb_cache:
                payload = _thumbnail(payloads[path])
                info = _media_info(payload, "image/jpeg")
                thumb_path = "media/" + info["sha256"] + ".jpg"
                payloads[thumb_path] = payload
                role = "evidence+thumbnail" if thumb_path in assets and assets[thumb_path]["role"].startswith("evidence") else "thumbnail"
                assets[thumb_path] = {**info, "role": role}
                thumb_cache[path] = thumb_path
            thumbnails[key] = thumb_cache[path]
    viewer = root / "web/app.js"
    payloads["offline.html"] = render_snapshot(original, root=root).encode("utf-8")
    offline_info = _file_info(payloads["offline.html"], "text/html", "offline")
    data = {**original, "format": FORMAT, "evidence": evidence, "thumbnails": thumbnails,
            "assets": dict(sorted(assets.items())),
            "offline": {"path": "offline.html", "sha256": offline_info["sha256"], "size": offline_info["size"]}}
    payloads["index.html"] = render_snapshot(data, root=root).encode("utf-8")
    audit_snapshot(payloads["offline.html"].decode("utf-8"), viewer)
    audit_snapshot(payloads["index.html"].decode("utf-8"), viewer, static=True)
    files = {**assets, "index.html": _file_info(payloads["index.html"], "text/html", "index"), "offline.html": offline_info}
    manifest = validate_manifest({"version": 1, "format": FORMAT, "files": dict(sorted(files.items())), "stats": _stats(data, files)})
    payloads[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return _write_site_payloads(destination, payloads, manifest, viewer)


def _write_site_payloads(destination, payloads, manifest, viewer):
    destination = Path(destination)
    _relative(destination.name)
    with _directory(destination.parent) as parent:
        if destination.exists() or destination.is_symlink():
            old_files, old_dirs = _inventory(destination)
            if old_files or old_dirs:
                verify_site(destination, viewer)
                _require(old_files == set(payloads) and all(read_asset_safe(destination, path) == payload for path, payload in payloads.items()),
                         "destination is not byte-identical; use an empty directory")
                return dict(manifest["stats"])
        stage_name = ".static-site-" + secrets.token_hex(12)
        os.mkdir(stage_name, mode=0o700, dir_fd=parent)
        staged = True
        try:
            stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            children = {}
            try:
                for folder in ("media", "originals"):
                    if any(path.startswith(folder + "/") for path in payloads):
                        os.mkdir(folder, dir_fd=stage_fd)
                        children[folder] = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage_fd)
                for path, payload in payloads.items():
                    target_fd = children[path.split("/")[0]] if "/" in path else stage_fd
                    fd = os.open(Path(path).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o644, dir_fd=target_fd)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(payload)
            finally:
                for child_fd in children.values():
                    os.close(child_fd)
                os.close(stage_fd)
            verify_site(destination.parent / stage_name, viewer)
            # Directory-relative rename cannot follow a swapped parent symlink or
            # replace a populated directory. Never merge/take over existing files.
            os.rename(stage_name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            staged = False
        finally:
            if staged:
                shutil.rmtree(stage_name, dir_fd=parent)
    return dict(manifest["stats"])


def upgrade_site(source: Path, destination: Path, input_viewer: Path, archive_root: Path,
                 audit, root: Path = ROOT, *, input_runtime=None) -> dict:
    """Upgrade a verified bundle without re-encoding or re-thumbnailing media.

    All input bytes are captured and rechecked against the verified manifest
    before any destination write. The original site is never changed.
    """
    source, root = Path(source), Path(root)
    manifest = verify_site(source, input_viewer, preview_runtime=input_runtime)
    captured = {path: read_asset_safe(source, path) for path in manifest["files"]}
    for path, raw in captured.items():
        info = manifest["files"][path]
        _require(len(raw) == info["size"] and hashlib.sha256(raw).hexdigest() == info["sha256"], "input changed after verification")
    old = audit_snapshot(captured["index.html"].decode("utf-8"), input_viewer, static=True, preview_runtime=input_runtime)
    old_inline = audit_snapshot(captured["offline.html"].decode("utf-8"), input_viewer, preview_runtime=input_runtime)
    originals, packages = build_originals(old["runs"], archive_root, audit)
    inline_originals = {}
    for run_id, descriptor in originals.items():
        inline = dict(descriptor)
        if descriptor["package"]:
            info = descriptor["package"]
            inline["package"] = {"sha256": info["sha256"], "size": info["size"],
                                 "base64": base64.b64encode(packages[info["path"]]).decode("ascii")}
        inline_originals[run_id] = inline
    inline = {**old_inline, "format": FORMAT_V2, "transport": "inline", "originals": inline_originals}
    payloads = {path: raw for path, raw in captured.items() if MEDIA_PATH.fullmatch(path)}
    payloads.update(packages)
    payloads["offline.html"] = render_snapshot(inline, root=root).encode("utf-8")
    offline_info = _file_info(payloads["offline.html"], "text/html", "offline")
    data = {**old, "format": FORMAT_V2, "transport": "external", "originals": originals,
            "offline": {"path": "offline.html", "sha256": offline_info["sha256"], "size": offline_info["size"]}}
    payloads["index.html"] = render_snapshot(data, root=root).encode("utf-8")
    files = {**old["assets"], **{path: _file_info(raw, "application/json", "original") for path, raw in packages.items()},
             "index.html": _file_info(payloads["index.html"], "text/html", "index"), "offline.html": offline_info}
    validated = validate_originals(data, payloads)
    output = validate_manifest({"version": 2, "format": FORMAT_V2, "files": dict(sorted(files.items())),
                                "stats": _stats(data, files, validated)})
    payloads[MANIFEST] = (json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return _write_site_payloads(destination, payloads, output, root / "web/app.js")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="existing self-contained snapshot; never executed")
    parser.add_argument("destination", type=Path, help="empty standalone site directory (parent must exist)")
    parser.add_argument("--input-viewer", type=Path, required=True, help="trusted app.js matching the input snapshot's published version")
    parser.add_argument("--upgrade-site", action="store_true", help="input is an existing verified site; preserve all media bytes and mappings")
    parser.add_argument("--archive-root", type=Path, help="read-only runs directory for reviewed originals")
    parser.add_argument("--audit", type=Path, help="hash-bound full-content assistant review JSON")
    parser.add_argument("--input-runtime", type=Path, help="trusted runtime when input is already v2")
    args = parser.parse_args(argv)
    try:
        if args.upgrade_site:
            _require(args.archive_root is not None and args.audit is not None, "upgrade requires --archive-root and --audit")
            stats = upgrade_site(args.snapshot, args.destination, args.input_viewer, args.archive_root,
                                 args.audit, input_runtime=args.input_runtime)
        else:
            _require(args.archive_root is None and args.audit is None and args.input_runtime is None, "original options require --upgrade-site")
            stats = build_site(args.snapshot, args.destination, args.input_viewer)
    except (RuntimeError, OSError, UnicodeError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"destination": str(args.destination), **stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
