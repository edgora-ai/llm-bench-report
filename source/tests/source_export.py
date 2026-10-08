"""Deterministic, fail-closed export of the public source allowlist.

The destination is the source directory itself, not its parent. Source bytes
are validated and captured before any destination writes. The hash manifest
allows subsequent updates only when previously exported files are untouched;
it is not a way to adopt an existing arbitrary directory.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
from urllib.parse import parse_qsl, unquote, urlsplit


MANIFEST_NAME = ".source-manifest.json"
REQUIRED_FILES = (
    ".dockerignore", ".gitignore", "README.md", "pyproject.toml",
    "bench/tasks/blackhole.json", "bench/tasks/crocodile.json",
    "docs/methodology.md", "web/index.html", "web/app.js", "web/styles.css",
    "containers/runtime.Dockerfile", "runtime/launch.py",
    "runtime/evaluate_worker.py", "config/bench.example.toml",
)
OPTIONAL_HELPERS = (
    "publish.py", "make_snapshot.py", "dashboard_e2e.py", "evaluate_e2e.py",
    "gateway_e2e.py", "verify_archives.py", "reconcile_batches.py",
    "source_export.py", "verify_public_snapshot.py",
)

# Signature lengths distinguish real tokens from documented prefixes and
# deliberately short fixtures. Values never appear in rejection messages.
TOKEN_SIGNATURE = re.compile(
    r"(?<![\w-])(?:"
    r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}"
    r")(?![\w-])"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|passwd|secret|token|authorization|"
    r"[A-Z][A-Z0-9_]*(?:_KEY|_TOKEN|_SECRET|_PASSWORD))\Z", re.I
)
LITERAL_CREDENTIAL = re.compile(
    r"(?<![\w])['\"]?([A-Za-z][A-Za-z0-9_-]*)['\"]?\s*[:=]\s*"
    r"(['\"])([^\r\n]*?)\2"
)
ENV_CREDENTIAL = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*"
    r"([A-Za-z0-9_./+@:-]+)[ \t]*(?:#[^\r\n]*)?$", re.M
)
URL = re.compile(r"(?:https?|postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?)://[^\s'\"<>`]+", re.I)
PLACEHOLDERS = {
    "", "none", "null", "wrong", "invalid", "dummy", "test", "example",
    "placeholder", "changeme", "change-me", "replace-me", "your-api-key", "bearer",
    "your-token", "secret", "hidden", "changed", "key", "token", "password",
    "not-a-real-key", "not-a-real-token", "bench-local-bridge",
}


def _fail(reason: str) -> None:
    raise RuntimeError("refusing source export: " + reason)


def _relative(relative) -> str:
    value = str(relative)
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or "\\" in value or "\x00" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or path.as_posix() != value):
        _fail("invalid relative path")
    return value


def _allowed(relative: str) -> bool:
    if relative in REQUIRED_FILES:
        return True
    parts = PurePosixPath(relative).parts
    if len(parts) == 2 and parts[0] == "bench":
        return parts[1].endswith(".py")
    if len(parts) == 3 and parts[:2] == ("bench", "adapters"):
        return parts[2].endswith(".py")
    if len(parts) == 2 and parts[0] == "tests":
        return (parts[1] in OPTIONAL_HELPERS
                or (parts[1].startswith("test_") and parts[1].endswith(".py")))
    return False


def _absolute(path: Path) -> Path:
    path = Path(path)
    if ".." in path.parts:
        _fail("parent traversal in root or destination")
    return path.absolute()


def _check_chain(path: Path, *, file: bool = False, missing: bool = False) -> None:
    """lstat every component, including ancestors outside the project."""
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if missing:
                return
            _fail("required source or directory is missing")
        if stat.S_ISLNK(mode):
            _fail("symlink in source or destination path")
        last = index == len(path.parts) - 2
        if last and file:
            if not stat.S_ISREG(mode):
                _fail("selected path is not a regular file")
        elif not stat.S_ISDIR(mode):
            _fail("path ancestor is not a directory")


def validate_selected_source(root: Path, relative) -> Path:
    """Validate an allowlisted regular source file without following symlinks."""
    relative = _relative(relative)
    if not _allowed(relative):
        _fail("path is outside the public source allowlist")
    root = _absolute(root)
    _check_chain(root)
    source = root / relative
    _check_chain(source, file=True)
    return source


def _read_regular(path: Path) -> bytes:
    # O_NONBLOCK ensures a concurrent replacement with a FIFO cannot hang.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            _fail("selected path is not a regular file")
        return handle.read()


def _placeholder(value: str) -> bool:
    value = value.strip().strip("'\"")
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    lowered = value.lower()
    return (lowered in PLACEHOLDERS
            or lowered.startswith(("fixture-", "unit-test-", "test-", "dummy-", "example-"))
            or bool(re.fullmatch(r"\$\{?[A-Z][A-Z0-9_]*\}?|<[^>]+>", value)))


def _scan_content(relative: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("source is not UTF-8 text")
    if TOKEN_SIGNATURE.search(text) or PRIVATE_KEY.search(text):
        _fail("credential signature detected in selected source")
    for match in LITERAL_CREDENTIAL.finditer(text):
        key, _, value = match.groups()
        if SENSITIVE_KEY.fullmatch(key) and not _placeholder(value):
            _fail("literal credential detected in selected source")
    for match in ENV_CREDENTIAL.finditer(text):
        key, value = match.groups()
        if SENSITIVE_KEY.fullmatch(key) and not _placeholder(value):
            _fail("environment credential detected in selected source")
    for match in URL.finditer(text):
        try:
            url = urlsplit(match.group())
            if url.password is not None and not _placeholder(unquote(url.password)):
                _fail("credential-bearing URL detected in selected source")
            if (url.username is not None and url.password is None
                    and not _placeholder(unquote(url.username))):
                _fail("credential-bearing URL detected in selected source")
            for key, value in parse_qsl(url.query):
                sensitive = SENSITIVE_KEY.fullmatch(key) or key.lower() in {"key", "auth", "credential"}
                if sensitive and not _placeholder(value):
                    _fail("credential-bearing URL detected in selected source")
        except ValueError:
            # Source contains incomplete f-string URL templates. They are not
            # credentials; malformed credential-bearing URLs still fail closed.
            raw = match.group()
            if "@" in raw or re.search(r"[?&](?:key|token|password|api_key)=", raw, re.I):
                _fail("invalid credential-bearing URL in selected source")
    if relative == "config/bench.example.toml":
        try:
            config = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            _fail("example configuration is not valid TOML")
        if not any(line.lstrip().startswith("#") for line in text.splitlines()):
            _fail("example configuration needs explanatory comments")
        if re.search(r"/(?:home/[^/\s]+|Users/[^/\s]+)(?:/|\b)", text):
            _fail("example configuration contains a host-specific path")
        _scan_config_values(config)


def _scan_config_values(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE_KEY.fullmatch(key) and isinstance(item, str) and not _placeholder(item):
                _fail("credential value in example configuration")
            _scan_config_values(item)
    elif isinstance(value, list):
        for item in value:
            _scan_config_values(item)


def _selection(root: Path) -> list[str]:
    selected = set(REQUIRED_FILES)
    # Only these immediate directories are enumerated. No private directories,
    # runtime UUID workspaces, archive trees or hidden caches are traversed.
    for directory in ("bench", "bench/adapters", "tests"):
        path = root / directory
        _check_chain(path, missing=True)
        if not path.exists():
            continue
        for child in path.iterdir():
            relative = child.relative_to(root).as_posix()
            if _allowed(relative):
                selected.add(relative)
    return sorted(selected)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _directory_names(paths) -> set[str]:
    directories = set()
    for relative in paths:
        directories.update(parent.as_posix() for parent in PurePosixPath(relative).parents
                           if parent != PurePosixPath("."))
    return directories


def _validate_destination(destination: Path, selected: dict[str, bytes]) -> dict:
    _check_chain(destination, missing=True)
    if not destination.exists():
        return {}
    manifest_path = destination / MANIFEST_NAME
    previous = {}
    if manifest_path.exists() or manifest_path.is_symlink():
        _check_chain(manifest_path, file=True)
        try:
            manifest = json.loads(_read_regular(manifest_path))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _fail("invalid existing source manifest")
        if (not isinstance(manifest, dict) or set(manifest) != {"version", "sha256"}
                or type(manifest["version"]) is not int or manifest["version"] != 1
                or not isinstance(manifest["sha256"], dict)):
            _fail("invalid existing source manifest")
        previous = manifest["sha256"]
        for relative, digest in previous.items():
            relative = _relative(relative)
            if (not _allowed(relative) or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                _fail("invalid existing source manifest entry")
        if set(previous) - set(selected):
            _fail("stale managed source files require explicit cleanup")
    permitted_dirs = _directory_names(previous)
    observed = set()
    for parent, directories, files in os.walk(destination, followlinks=False):
        for name in directories:
            path = Path(parent) / name
            _check_chain(path)
            if path.relative_to(destination).as_posix() not in permitted_dirs:
                _fail("unmanaged destination directory")
        for name in files:
            path = Path(parent) / name
            _check_chain(path, file=True)
            relative = path.relative_to(destination).as_posix()
            if relative == MANIFEST_NAME:
                continue
            if relative not in previous:
                _fail("unmanaged destination file")
            if _hash(_read_regular(path)) != previous[relative]:
                _fail("managed destination file was modified")
            observed.add(relative)
    if observed != set(previous):
        _fail("managed destination file is missing")
    return previous


def export_source(root: Path, destination: Path) -> dict:
    """Export safe source bytes and a deterministic SHA-256 managed manifest.

    Rejections leave the destination untouched. No obsolete managed files are
    deleted, and no existing unrecognized files are ever overwritten.
    """
    root, destination = _absolute(root), _absolute(destination)
    _check_chain(root)
    if destination == root or root.is_relative_to(destination):
        _fail("destination overlaps the source root")
    paths = _selection(root)
    validated = [(relative, validate_selected_source(root, relative)) for relative in paths]
    selected = {}
    for relative, path in validated:
        data = _read_regular(path)
        try:
            _scan_content(relative, data)
        except RuntimeError as error:
            raise RuntimeError(f"{error} ({relative})") from None
        selected[relative] = data
    hashes = {relative: _hash(data) for relative, data in selected.items()}
    previous = _validate_destination(destination, selected)
    manifest_bytes = (json.dumps({"version": 1, "sha256": hashes},
                                 ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    destination.mkdir(parents=True, exist_ok=True)
    for relative, data in selected.items():
        target = destination / relative
        if previous.get(relative) == hashes[relative]:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.exists() or _read_regular(manifest_path) != manifest_bytes:
        manifest_path.write_bytes(manifest_bytes)
    return {"files": len(paths), "paths": paths, "sha256": hashes}
