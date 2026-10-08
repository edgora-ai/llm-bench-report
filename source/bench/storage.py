"""SQLite run index, immutable final manifests, and append-only human reviews."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from typing import Iterator
import uuid

RUBRIC = ("compliance", "recognizability", "motion", "visual_detail", "interaction", "completeness")
STATUSES = ("queued", "running", "generated", "completed", "failed", "interrupted", "unavailable", "blocked")
# A run in one of these states has no result yet. Its measurements are a
# placeholder the session will fill in, not a claim about the sample.
UNFINISHED_STATUSES = frozenset({"queued", "running"})
# Final statuses describe the generation verdict, not the evaluation verdict.
FINAL_STATUSES = frozenset({"completed", "failed", "interrupted", "unavailable", "blocked"})
# Only these post-generation fields may change during evaluation/re-evaluation.
EVALUATION_FIELDS = frozenset({"evaluation", "checks", "artifacts", "archive_status",
                               "archive_error", "evaluation_finished_at"})
SOURCE_METRICS = frozenset({"source_bytes", "source_file_count"})
# Placeholders may be completed while queued/running. Everything else (including
# unknown identity/lineage fields) is fixed from creation, not merely at sealing.
GENERATION_FIELDS = frozenset({"error", "error_category", "metrics", "duration_ms",
                              "first_model_event_ms", "final_response", "reported_models",
                              "backend_weights_version", "cli_command", "usage_raw",
                              "exit_code", "finished_at", "generation_finished_at",
                              "generation_artifacts", "generation_status"})
FILTER_COLUMNS = ("tool", "model", "task_id", "purpose", "prompt_version", "status")
METRICS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens", "cost_usd", "api_duration_ms", "num_turns", "source_bytes", "source_file_count")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class RunConflict(ValueError):
    """An import would overwrite an immutable run."""


def validate_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ValueError("Invalid run id")
    return run_id


def safe_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
        raise ValueError("Invalid relative path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or relative.startswith("/"):
        raise ValueError("Invalid relative path")
    return tuple(parts)


@contextmanager
def safe_directory(root: Path, relative: str = "", create: bool = False) -> Iterator[int]:
    """Walk directories with dirfd/O_NOFOLLOW, including during file creation."""
    parts = safe_parts(relative) if relative else ()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass  # The subsequent no-follow open validates an existing directory.
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def safe_open(root: Path, relative: str):
    """Open a regular file without symlink traversal or check/open races."""
    parts = safe_parts(relative)
    with safe_directory(root, "/".join(parts[:-1])) as directory:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Not a regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            yield stream
    finally:
        if fd != -1:
            os.close(fd)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _valid_date(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Date must be YYYY-MM-DD")
    date.fromisoformat(value)


def _manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be an object")
    result = json.loads(_json(manifest))  # Detach caller-owned nested objects; reject NaN.
    validate_id(result.get("id"))
    _valid_date(result.get("date"))
    if result.get("status") not in STATUSES:
        raise ValueError("Invalid run status")
    if result.get("purpose") not in ("smoke", "benchmark", "fixture"):
        raise ValueError("Invalid run purpose")
    if result.get("tool") not in ("claude-code", "opencode"):
        raise ValueError("Invalid tool")
    for field in ("model", "task_id", "task_name", "prompt_version"):
        if not isinstance(result.get(field), str):
            raise ValueError(f"{field} must be a string")
    expected = f"runs/{result['date']}/{result['id']}"
    if result.get("archive_dir") != expected:
        raise ValueError("archive_dir must be runs/<date>/<id>")
    safe_parts(result["archive_dir"])
    if not isinstance(result.get("metrics"), dict):
        raise ValueError("metrics must be an object")
    for key in METRICS:
        value = result["metrics"].get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise ValueError(f"Invalid metric: {key}")
    if not isinstance(result.get("artifacts"), list):
        raise ValueError("artifacts must be an array")
    for field in ("artifacts", "generation_artifacts"):
        if field not in result:
            continue
        if not isinstance(result[field], list):
            raise ValueError(f"{field} must be an array")
        paths = set()
        for artifact in result[field]:
            if not isinstance(artifact, dict):
                raise ValueError("Invalid artifact")
            safe_parts(artifact.get("path"))
            if artifact["path"] in paths:
                raise ValueError(f"Duplicate artifact path in {field}")
            paths.add(artifact["path"])
    if "generation_status" in result:
        verdict = result["generation_status"]
        if verdict not in FINAL_STATUSES:
            raise ValueError("Invalid generation status")
        if result["status"] in FINAL_STATUSES and result["status"] != verdict:
            raise RunConflict("Cannot change the generation verdict")
    evaluation = result.get("evaluation", {})
    if not isinstance(evaluation, dict) or not isinstance(evaluation.get("evidence", []), list):
        raise ValueError("Invalid evaluation")
    for path in evaluation.get("evidence", []):
        safe_parts(path)
    return result


def _generation_artifacts(artifacts: list[dict]) -> dict:
    """Compare generation files by path; evidence and evaluator logs are derived."""
    return {artifact["path"]: artifact for artifact in artifacts
            if not artifact["path"].startswith("evidence/")
            and artifact["path"] not in {"evaluation-stdout.txt", "evaluation-stderr.txt"}}


def _validate_transition(previous: dict, manifest: dict) -> None:
    if previous == manifest:
        return  # Identical historical imports remain legitimate, without migration.
    unfinished = previous["status"] in UNFINISHED_STATUSES
    mutable = EVALUATION_FIELDS | {"status", "metrics", "generation_status"}
    if unfinished:
        mutable |= GENERATION_FIELDS
    frozen = lambda record: {key: value for key, value in record.items() if key not in mutable}
    if frozen(previous) != frozen(manifest):
        if unfinished:
            raise RunConflict("Cannot change run identity or conditions")
        raise RunConflict("Cannot change identity or generation facts of a sealed run")

    old_status, new_status = previous["status"], manifest["status"]
    if (old_status == "running" and new_status == "queued"
            or not unfinished and new_status in UNFINISHED_STATUSES
            or old_status in FINAL_STATUSES and new_status != old_status):
        raise RunConflict("Cannot move a run backwards or change its generation verdict")
    if unfinished:
        return

    old_verdict = previous.get("generation_status")
    new_verdict = manifest.get("generation_status")
    if old_status == "generated":
        if old_verdict is not None:
            # Legacy evaluation consumed this field when promoting generated to
            # its matching final status. Retaining it is preferred for new runs.
            consumed = "generation_status" not in manifest and new_status == old_verdict
            if new_verdict != old_verdict and not consumed:
                raise RunConflict("Cannot change the generation verdict")
            if new_status != "generated" and new_status != old_verdict:
                raise RunConflict("Cannot change the generation verdict")
        elif "generation_status" in manifest:
            raise RunConflict("Cannot invent a generation verdict for a sealed run")
    elif ("generation_status" in previous) != ("generation_status" in manifest) or old_verdict != new_verdict:
        raise RunConflict("Cannot change the generation verdict")

    generation_metrics = lambda record: {key: value for key, value in record["metrics"].items()
                                          if key not in SOURCE_METRICS}
    if generation_metrics(previous) != generation_metrics(manifest):
        raise RunConflict("Cannot change the generation metrics of a sealed run")

    baseline = _generation_artifacts(previous.get("generation_artifacts", previous["artifacts"]))
    incoming = _generation_artifacts(manifest["artifacts"])
    # New generated records already carry an explicit generation inventory,
    # even when the full evaluated artifact list has not yet been populated.
    pending_inventory = (old_status == new_status == "generated" and not incoming
                         and not _generation_artifacts(previous["artifacts"]))
    if incoming != baseline and not pending_inventory:
        raise RunConflict("Cannot change the generation artifacts of a sealed run")


def _review(payload: dict, run_id: str, *, restoring: bool = False) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Review must be an object")
    result = json.loads(_json(payload))
    if not isinstance(result.get("reviewer"), str) or not result["reviewer"].strip():
        raise ValueError("reviewer is required")
    if not isinstance(result.get("note"), str):
        raise ValueError("note is required (may be empty)")
    scores = result.get("scores", result.get("rubric", {}))
    if not isinstance(scores, dict) or set(scores) - set(RUBRIC):
        raise ValueError("Invalid score dimensions")
    normalized = {}
    for dimension in RUBRIC:
        score = scores.get(dimension)
        if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 4):
            raise ValueError("Scores must be null or numbers from 0 to 4")
        normalized[dimension] = score
    result["scores"] = normalized
    if restoring:
        validate_id(result.get("id"))
        if result.get("run_id") != run_id or not isinstance(result.get("created_at"), str):
            raise ValueError("Invalid stored review")
    else:
        result.update(id=uuid.uuid4().hex, run_id=run_id, created_at=datetime.now(timezone.utc).isoformat())
    return result


def summary(runs: list[dict]) -> dict:
    """Aggregate exactly the supplied slice; unknown quantities remain unknown."""
    metrics = {}
    coverage = {}
    for key in METRICS:
        values = [run.get("metrics", {}).get(key) for run in runs]
        known = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)]
        metrics[key] = sum(known) if known else None
        coverage[key] = {"known": len(known), "unknown": len(runs) - len(known), "fraction": len(known) / len(runs) if runs else 0.0}
    costs = coverage["cost_usd"]
    return {
        "count": len(runs), "total_runs": len(runs),
        "statuses": dict(Counter(run.get("status") for run in runs)),
        "tools": dict(Counter(run.get("tool") for run in runs)),
        "models": dict(Counter(run.get("model") for run in runs)),
        "metrics": metrics, "metric_coverage": coverage,
        "cost_usd_known_subtotal": metrics["cost_usd"],
        "cost_known_count": costs["known"], "cost_unknown_count": costs["unknown"],
        "cost_coverage": costs["fraction"],
    }


class Store:
    def __init__(self, db_path: Path, project_root: Path):
        self.db_path = Path(db_path)
        self.project_root = Path(project_root).absolute()
        self._write_lock = threading.RLock()
        self.init()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, date TEXT NOT NULL, tool TEXT NOT NULL,
                    model TEXT NOT NULL, task_id TEXT NOT NULL, task_name TEXT NOT NULL,
                    purpose TEXT NOT NULL, prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL, archive_dir TEXT NOT NULL, manifest_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    created_at TEXT NOT NULL, review_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reviews_run ON reviews(run_id, created_at, id);
            """)
            for column in ("date",) + FILTER_COLUMNS:
                db.execute(f"CREATE INDEX IF NOT EXISTS runs_{column} ON runs({column})")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS runs_search USING fts5(id UNINDEXED, fulljson)")

    def _validate_run(self, db: sqlite3.Connection, manifest: dict) -> dict | None:
        old = db.execute("SELECT manifest_json FROM runs WHERE id=?", (manifest["id"],)).fetchone()
        previous = json.loads(old[0]) if old else None
        if previous is not None:
            _validate_transition(previous, manifest)
        return previous

    def validate_run(self, manifest: dict) -> dict:
        """Return a detached validated manifest without writing files or rows.

        Intended for runner pre-seal checks. This is not a reservation: upsert
        rechecks the same rules under BEGIN IMMEDIATE against the current row.
        """
        manifest = _manifest(manifest)
        with self._write_lock, self.connection() as db:
            self._validate_run(db, manifest)
        return manifest

    def _upsert(self, db: sqlite3.Connection, manifest: dict) -> None:
        previous = self._validate_run(db, manifest)
        if previous == manifest:
            return
        data = _json(manifest)
        columns = ("id", "date", "tool", "model", "task_id", "task_name", "purpose", "prompt_version", "status", "archive_dir")
        db.execute(
            "INSERT INTO runs (" + ",".join(columns) + ",manifest_json) VALUES (" + ",".join("?" for _ in range(11)) + ") ON CONFLICT(id) DO UPDATE SET status=excluded.status, manifest_json=excluded.manifest_json",
            tuple(manifest[key] for key in columns) + (data,),
        )
        db.execute("DELETE FROM runs_search WHERE id=?", (manifest["id"],))
        db.execute("INSERT INTO runs_search(id,fulljson) VALUES (?,?)", (manifest["id"], data))

    def upsert_run(self, manifest: dict) -> None:
        manifest = _manifest(manifest)
        with self._write_lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._upsert(db, manifest)

    def _decode(self, db: sqlite3.Connection, row) -> dict:
        run = json.loads(row["manifest_json"])
        run["reviews"] = [json.loads(review[0]) for review in db.execute("SELECT review_json FROM reviews WHERE run_id=? ORDER BY created_at,id", (run["id"],))]
        return run

    def get_run(self, run_id: str) -> dict | None:
        validate_id(run_id)
        with self.connection() as db:
            row = db.execute("SELECT manifest_json FROM runs WHERE id=?", (run_id,)).fetchone()
            return self._decode(db, row) if row else None

    def list_runs(self, filters: dict | None = None) -> list[dict]:
        filters = filters or {}
        clauses, values = [], []
        for column in FILTER_COLUMNS:
            if filters.get(column) not in (None, ""):
                clauses.append(f"{column}=?")
                values.append(filters[column])
        for name, operator in (("date_from", ">="), ("date_to", "<=")):
            if filters.get(name):
                _valid_date(filters[name])
                clauses.append(f"date {operator} ?")
                values.append(filters[name])
        if filters.get("q"):
            # q is a literal, case-insensitive substring, matching snapshot search.
            # FTS token AND is unsuitable here: an ID's tokens can match different
            # fields, and partial words/punctuation would have different semantics.
            clauses.append("instr(lower(manifest_json),lower(?)) > 0")
            values.append(str(filters["q"]))
        sql = "SELECT manifest_json FROM runs" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY date DESC,id DESC"
        with self.connection() as db:
            return [self._decode(db, row) for row in db.execute(sql, values).fetchall()]

    def add_review(self, run_id: str, payload: dict) -> dict:
        validate_id(run_id)
        review = _review(payload, run_id)
        with self._write_lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT archive_dir FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Run not found")
            # File first: if a later DB commit fails, rebuild recovers this append.
            with safe_directory(self.project_root, row[0] + "/reviews", create=True) as directory:
                name = review["id"] + ".json"
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(_json(review))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory)
            db.execute("INSERT INTO reviews(id,run_id,created_at,review_json) VALUES (?,?,?,?)", (review["id"], run_id, review["created_at"], _json(review)))
        return review

    def rebuild(self) -> dict:
        """Import archived manifests/reviews without dropping existing valid records."""
        counts = {"runs": 0, "reviews": 0, "errors": 0, "skipped": 0}
        root = self.project_root / "runs"
        if not root.exists():
            return counts
        if root.is_symlink():
            raise ValueError("runs directory cannot be a symlink")
        with self._write_lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink() and d != "reviews")
                if "manifest.json" not in files:
                    continue
                relative = (Path(directory) / "manifest.json").relative_to(self.project_root).as_posix()
                try:
                    with safe_open(self.project_root, relative) as stream:
                        manifest = _manifest(json.load(stream))
                    if relative != manifest["archive_dir"] + "/manifest.json":
                        raise ValueError("Manifest archive location does not match")
                    self._upsert(db, manifest)
                    counts["runs"] += 1
                except RunConflict:
                    counts["skipped"] += 1
                    continue
                except (OSError, ValueError, TypeError):
                    counts["errors"] += 1
                    continue
                review_dir = Path(directory) / "reviews"
                try:
                    with safe_directory(self.project_root, manifest["archive_dir"] + "/reviews") as review_fd:
                        names = sorted(os.listdir(review_fd))
                except FileNotFoundError:
                    continue
                except OSError:
                    counts["errors"] += 1
                    continue
                for name in names:
                    if not name.endswith(".json"):
                        continue
                    try:
                        with safe_open(self.project_root, manifest["archive_dir"] + "/reviews/" + name) as stream:
                            review = _review(json.load(stream), manifest["id"], restoring=True)
                        if name != review["id"] + ".json":
                            raise ValueError("Review filename does not match id")
                        old = db.execute("SELECT review_json FROM reviews WHERE id=?", (review["id"],)).fetchone()
                        if old and old[0] != _json(review):
                            raise ValueError("Cannot overwrite a review")
                        db.execute("INSERT OR IGNORE INTO reviews(id,run_id,created_at,review_json) VALUES (?,?,?,?)", (review["id"], review["run_id"], review["created_at"], _json(review)))
                        counts["reviews"] += 1
                    except (OSError, ValueError, TypeError):
                        counts["errors"] += 1
        return counts

    def summary(self, runs: list[dict]) -> dict:
        return summary(runs)
