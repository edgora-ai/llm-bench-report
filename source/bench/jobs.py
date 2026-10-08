"""Session-independent finite batch jobs with private logs and scoped cancellation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

_processes = {}
TERMINAL = frozenset({"completed", "failed", "interrupted", "unexpected_exit"})


def _job_folder(root, job_id):
    if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("Invalid job ID")
    return Path(root) / "data/jobs" / job_id


def _write_json(path, record):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def launch_lock(root):
    folder = Path(root) / "data/jobs"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / ".launch.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _identity(pid):
    proc = Path(f"/proc/{pid}")
    command = (proc / "cmdline").read_bytes()
    # comm can contain spaces and parentheses; fields after its final ')' are fixed.
    start = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
    return {"process_start": start, "command_sha256": hashlib.sha256(command).hexdigest()}


def start(root, purpose="benchmark", seed=None, tool=None, models=None, task=None,
          config_path=None, concurrency=1, pairs=None, retry_runs=None):
    from .recovery import validate_request

    root = Path(root).resolve()
    validate_request(root, purpose, tool, models, task, concurrency, pairs, retry_runs)
    with launch_lock(root):
        # Recheck under the lock: another launcher may have claimed a parent.
        validate_request(root, purpose, tool, models, task, concurrency, pairs, retry_runs)
        job_id = uuid.uuid4().hex
        folder = _job_folder(root, job_id)
        folder.mkdir(mode=0o700)
        args = [sys.executable, "-m", "bench.cli"]
        if config_path:
            args += ["--config", str(Path(config_path).resolve())]
        args += ["run", "--purpose", purpose, "--job-id", job_id, "--concurrency", str(concurrency)]
        if seed is not None:
            args += ["--seed", str(seed)]
        for entry in pairs or []:
            text = ",".join(entry) if isinstance(entry, (tuple, list)) else str(entry)
            args += ["--pair", text]
        for run_id in retry_runs or []:
            args += ["--retry-run", run_id]
        if tool:
            args += ["--tool", tool]
        for model in models or []:
            args += ["--model", model]
        if task:
            args += ["--task", task]
        log_path = folder / "worker.jsonl"
        manifest = {"id": job_id, "pid": None, "purpose": purpose,
                    "created_at": datetime.now(timezone.utc).isoformat(), "command": args,
                    "log": log_path.relative_to(root).as_posix(), "status": "launching",
                    "concurrency": concurrency, "retry_runs": list(retry_runs or []),
                    "launcher_pid": os.getpid(), "launcher_identity": _identity(os.getpid())}
        # The claim exists before the child can launch a session or another
        # process can recover this same parent. State lives in a separate file.
        _write_json(folder / "job.json", manifest)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(args, cwd=root, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True,
                                           env={**os.environ, "PYTHONPATH": str(root), "PYTHONUNBUFFERED": "1"})
            _processes[job_id] = process
            manifest.update(pid=process.pid, status="launched")
            try:
                manifest.update(_identity(process.pid))
            except (FileNotFoundError, ProcessLookupError):
                # A worker that already exited is handled by status(), not
                # mistaken for a different process reusing its PID.
                manifest["identity_unavailable"] = True
            _write_json(folder / "job.json", manifest)
        except Exception as exc:
            manifest.update(status="failed", error_type=type(exc).__name__)
            _write_json(folder / "job.json", manifest)
            raise
    return manifest


@contextmanager
def foreground_claim(root, retry_runs, purpose="benchmark", concurrency=1, pairs=None):
    """Give a foreground recovery the same durable claim as a detached worker."""
    from .recovery import validate_request

    root = Path(root).resolve()
    with launch_lock(root):
        validate_request(root, purpose, concurrency=concurrency, pairs=pairs, retry_runs=retry_runs)
        job_id = uuid.uuid4().hex
        folder = _job_folder(root, job_id)
        folder.mkdir(mode=0o700)
        record = {"id": job_id, "pid": os.getpid(), "purpose": purpose,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "status": "running", "retry_runs": list(retry_runs),
                  "concurrency": concurrency, "foreground": True, **_identity(os.getpid())}
        _write_json(folder / "job.json", record)
    try:
        yield job_id
    except KeyboardInterrupt:
        write_state(root, job_id, "interrupted")
        raise
    except Exception as exc:
        write_state(root, job_id, "failed", error_type=type(exc).__name__)
        raise
    finally:
        # Normal completion is recorded by the CLI, with its batch information.
        if not (folder / "state.json").exists():
            write_state(root, job_id, "completed")


def status(root, job_id):
    folder = _job_folder(root, job_id)
    record = json.loads((folder / "job.json").read_text())
    state = folder / "state.json"
    if state.exists():
        record.update(json.loads(state.read_text()))
    cancel = folder / "cancel.json"
    if cancel.exists():
        record["cancellation_requested"] = True
        record["cancellation_requested_at"] = json.loads(cancel.read_text())["requested_at"]
    alive = False
    if isinstance(record.get("pid"), int) and record["pid"] > 0:
        try:
            command = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b"\x00")
            identity = _identity(record["pid"])
            if record.get("foreground"):
                alive = all(record.get(key) == value for key, value in identity.items())
            else:
                alive = (b"bench.cli" in command and b"run" in command
                         and job_id.encode() in command
                         and not record.get("identity_unavailable")
                         and all(record.get(key, value) == value for key, value in identity.items()))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            alive = False
    record["process_alive"] = alive
    record["launching_active"] = False
    if record["status"] == "launching" and record.get("launcher_pid"):
        try:
            record["launching_active"] = _identity(record["launcher_pid"]) == record.get("launcher_identity")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            record["launching_active"] = False
    if not alive and not record["launching_active"] and record["status"] not in TERMINAL:
        record["status"] = "unexpected_exit"
    return record


def _owned_container_names(root, record):
    from .storage import validate_id

    batch_id = record.get("batch_id")
    if not batch_id:
        return set()
    validate_id(batch_id)
    batch = json.loads((Path(root) / "data/batches" / batch_id / "batch.json").read_text())
    if batch.get("id") != batch_id or (batch.get("job_id") and batch["job_id"] != record["id"]):
        raise ValueError("Batch identity or job ownership mismatch")
    names = set()
    for entry in batch.get("queue", []):
        run_id = entry.get("run_id")
        if not run_id:
            continue
        validate_id(run_id)
        expected = {"llm-bench-" + run_id, "llm-bench-eval-" + run_id}
        for key in ("container_name", "generation_container_name", "evaluation_container_name"):
            if entry.get(key) and entry[key] not in expected:
                raise ValueError("Container name does not match queue run ID")
        names.update(expected)
    return names


def stop(root, job_id):
    record = status(root, job_id)
    if not record["process_alive"] or record["status"] in TERMINAL:
        raise ValueError("Job is not running; no signal sent")
    folder = _job_folder(root, job_id)
    # Persist intent before signaling or invoking Docker. Failures leave the
    # coordinator able to observe cancellation and finish its own scoped cleanup.
    cancel = folder / "cancel.json"
    if not cancel.exists():
        _write_json(cancel, {"id": job_id, "requested_at": datetime.now(timezone.utc).isoformat()})
    owned = _owned_container_names(root, record)
    # Re-read identity immediately before signaling to reject PID reuse.
    current = status(root, job_id)
    if current["process_alive"] and current["status"] not in TERMINAL:
        try:
            os.kill(current["pid"], signal.SIGINT)
        except ProcessLookupError:
            # Durable intent still reaches the runner if it is shutting down.
            current["process_alive"] = False
    stopped = []
    errors = []
    if owned:
        for name in running_containers():
            if name not in owned:
                continue
            result = subprocess.run(["docker", "stop", "--time", "3", name],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                errors.append({"container": name, "error": result.stderr[-500:]})
            else:
                stopped.append(name)
    return {"id": job_id, "cancellation_requested": True,
            "stopped_containers": stopped, "cleanup_errors": errors}


def running_containers():
    """Live benchmark names; stop() intersects this list with its exact queue."""
    result = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("docker ps failed: " + result.stderr[-500:])
    return [name for name in result.stdout.split() if name.startswith("llm-bench-")]


def write_state(root, job_id, state, **extra):
    folder = _job_folder(root, job_id)
    path = folder / "state.json"
    record = json.loads(path.read_text()) if path.exists() else {}
    record.update(status=state, updated_at=datetime.now(timezone.utc).isoformat(), **extra)
    _write_json(path, record)
    print(json.dumps({"event": "job_state", "job_id": job_id, **record}, ensure_ascii=False), flush=True)
