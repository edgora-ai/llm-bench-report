"""Single-session orchestration; no continuation, model fallback, or external repair."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import stat
import subprocess
import threading
import time
import uuid

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .adapters import claude_code, opencode
from .discovery import CLAUDE_MODELS, claude_model_queue, discover_free
from .effort import CEILING_ORDER, condition_fields as effort_condition, read_catalogue, resolve as resolve_effort
from .egress import Gateway, connections
from .isolation import generation_command, evaluate_command, image_id, preflight
from .storage import Store
from urllib.parse import urlsplit


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    # A concurrent batch writes this file from several threads. The temporary
    # name must be unique per writer: a fixed ".tmp" lets one thread replace
    # the file out from under another's replace(), which either fails or
    # leaves a half-written file behind.
    path = Path(path)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def artifact_index(run_dir):
    artifacts = []
    for path in sorted(run_dir.rglob("*")):
        if path.name in {"manifest.json", "checksums.json"} or "reviews" in path.relative_to(run_dir).parts:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("Unsafe generated file: " + str(path.relative_to(run_dir)))
        if path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    hasher.update(chunk)
            rel = path.relative_to(run_dir).as_posix()
            kind = "evidence" if rel.startswith("evidence/") else "output" if rel.startswith("output/") else "process"
            artifacts.append({"path": rel, "size": info.st_size, "sha256": hasher.hexdigest(), "kind": kind})
    return artifacts


def connection_fingerprint(provider):
    def endpoint(value):
        parsed = urlsplit(value or "")
        return {"scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port, "path": parsed.path}
    # Authentication, URL userinfo and query values never enter run conditions.
    return digest({"base": endpoint(provider["base"]), "proxy": endpoint(provider.get("proxy"))})


def policy_hashes(root):
    paths = sorted((root / "bench").rglob("*.py")) + [root / "runtime/launch.py", root / "runtime/evaluate_worker.py"]
    paths += [path for path in [root / "docs/methodology.md", root / "containers/runtime.Dockerfile"] if path.is_file()]
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def archive_policy(root, destination, hashes):
    for relative, expected in hashes.items():
        data = (root / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("Source policy changed while archiving")
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def source_metrics(artifacts):
    sources = [a for a in artifacts if a["kind"] == "output" and Path(a["path"]).suffix.lower() in {".html", ".svg", ".css", ".js"}]
    return {"source_bytes": sum(a["size"] for a in sources), "source_file_count": len(sources)}


def effort_record(catalogue_path, model, allowed):
    """Resolve one run's effort level against the batch's frozen catalogue."""
    if not allowed:
        # An operator turning effort off is a recorded condition too; it must
        # not fall through to an unrecorded per-vendor default.
        return {"requested": None, "applied": None, "supported": None,
                "control": "disabled_by_operator", "ceiling": None}
    return resolve_effort(read_catalogue(Path(catalogue_path)), model, allowed=allowed)


def classify_error(error):
    text = (error or "").lower()
    if any(x in text for x in ["modelnotfound", "model not found", "unknown model", "unsupported model", "model does not exist"]):
        return "unavailable", "model_unavailable"
    if any(x in text for x in ["endpoint is unavailable", "endpoint unavailable", "service unavailable"]):
        return "unavailable", "provider_unavailable"
    if any(x in text for x in ["429", "rate limit", "ratelimit"]):
        return "failed", "rate_limit"
    if any(x in text for x in ["401", "403", "unauthorized", "authentication"]):
        return "failed", "authentication_or_permission"
    # A dropped stream is a transport failure, not a model giving up: the
    # session ended before the model produced an answer. Counting it as a model
    # failure would charge the model for a broken connection.
    if any(x in text for x in ["stream disconnected", "stream closed before",
                                "response.completed", "connection reset", "econnreset",
                                "socket hang up", "premature close", "incomplete chunked"]):
        return "failed", "transport_interrupted"
    return "failed", "session_error"


# A software-WebGL page can take ~166s for one CDP capture; four captures plus
# video and the mobile pass exceed the old 180s cap, which silently destroyed
# the evidence of the strongest outputs. This is infrastructure headroom, not a
# model budget: the model session is already finished when it applies.
EVALUATE_TIMEOUT = 1800


def evaluate_run(config, run_dir, task_id, name=None):
    evidence = run_dir / "evidence"
    evidence.mkdir(exist_ok=True)
    command = evaluate_command(config, run_dir / "output", evidence, task_id, name)
    # A named container can be stopped on operator cancellation; a long
    # software-WebGL capture must not hold the batch open indefinitely.
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=EVALUATE_TIMEOUT)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        if name:
            subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True, timeout=30)
        raise
    (run_dir / "evaluation-stdout.txt").write_text(result.stdout)
    (run_dir / "evaluation-stderr.txt").write_text(result.stderr)
    report_path = evidence / "evaluation.json"
    if result.returncode or not report_path.exists():
        return {"status": "infrastructure_error", "error": result.stderr[-2000:], "checks": [], "evidence": []}
    return json.loads(report_path.read_text())


def run_one(root, config, store, provider, catalogue_dir, facts, batch_id, purpose, tool, model, task, defer_evaluation=False, run_id=None, attempt=0, retry_of=None, retry_reason=None, cancel_event=None):
    date = datetime.now(timezone.utc).date().isoformat()
    run_id = run_id or uuid.uuid4().hex
    run_dir = root / "runs" / date / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o700)
    output = run_dir / "output"
    output.mkdir()
    bridge = root / "runtime" / run_id
    bridge.mkdir(mode=0o700)
    adapter = claude_code if tool == "claude-code" else opencode
    # Effort is resolved from the batch's own frozen catalogue snapshot, not
    # from a live lookup, so the level asked for cannot drift mid-batch.
    effort = effort_record(catalogue_dir / "models.json", model, facts["effort_levels"])
    cli = adapter.command(model, effort["applied"])
    condition = {"task_id": task["id"], "runner_sha256": facts["source_hashes"]["bench/runner.py"], "provider_connection_sha256": connection_fingerprint(provider), "prompt_hash": digest(task["prompt"]), "rubric_version": task["rubric_version"], "tool": tool, "cli_version": facts["versions"].get("claude" if tool == "claude-code" else "opencode"), "image_id": facts["image_id"], "profile": config["runtime"]["profile"], "isolation": facts["strategy"], "renderer": "chromium-software-v1234", "adapter_sha256": hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest(), "runtime_policy_sha256": hashlib.sha256((root / "runtime/launch.py").read_bytes()).hexdigest(), "egress_policy_sha256": hashlib.sha256((root / "bench/egress.py").read_bytes()).hexdigest(), "command_sha256": digest(["<requested-model>" if arg == model else arg for arg in cli]), "task_definition_sha256": digest(task), "model_directory_sha256": hashlib.sha256((catalogue_dir / "models.json").read_bytes()).hexdigest(), **effort_condition(effort)}
    manifest = {"id": run_id, "batch_id": batch_id, "date": date, "purpose": purpose, "tool": tool, "model": model, "task_id": task["id"], "task_name": task["name"], "prompt_version": task["version"], "prompt_hash": condition["prompt_hash"], "condition_fingerprint": digest(condition), "conditions": condition, "started_at": utcnow(), "finished_at": None, "status": "running", "error": None, "metrics": {}, "checks": [], "evaluation": {"status": "pending", "evidence": []}, "artifacts": [], "archive_dir": run_dir.relative_to(root).as_posix(), "requested_model": model, "reported_models": [], "backend_weights_version": None, "cli_command": cli, "duration_ms": None}
    manifest.update({"attempt": attempt, "retry_of": retry_of, "retry_reason": retry_reason})
    (run_dir / "prompt.txt").write_text(task["prompt"], encoding="utf-8")
    write_json(run_dir / "task.json", task)
    write_json(run_dir / "manifest.json", manifest)
    store.upsert_run(manifest)
    events = []
    interrupted = False
    process = None
    name = "llm-bench-" + run_id
    start = time.monotonic()
    first_event = None
    print(json.dumps({"event": "run_start", "id": run_id, "purpose": purpose, "tool": tool, "model": model, "task": task["id"]}, ensure_ascii=False), flush=True)
    secrets = [provider["secret"]] if len(provider.get("secret", "")) > 8 else []

    def sanitize(text):
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def collect_stderr(pipe):
        with (run_dir / "stderr.txt").open("w", encoding="utf-8") as f:
            for line in pipe:
                f.write(sanitize(line))
                f.flush()

    try:
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt
        catalogue = json.loads((catalogue_dir / "models.json").read_text())
        model_info = catalogue.get("opencode", {}).get("models", {}).get(model.rsplit("/", 1)[-1], {})
        api_model = model_info.get("api", {}).get("id") if isinstance(model_info.get("api"), dict) else None
        with Gateway(bridge / "gateway.sock", provider, tool, model, run_dir / "gateway.jsonl", api_model):
            command = generation_command(config, output.resolve(), bridge.resolve(), catalogue_dir.resolve(), tool, model, cli, name)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            thread = threading.Thread(target=collect_stderr, args=(process.stderr,), daemon=True)
            thread.start()
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
            process.stdin.write(task["prompt"] + "\n")
            process.stdin.close()
            with (run_dir / "stdout.jsonl").open("w", encoding="utf-8") as f:
                for line in process.stdout:
                    line = sanitize(line)
                    f.write(line)
                    f.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
                        if first_event is None and event.get("type") in {"assistant", "text", "reasoning", "tool_use"}:
                            first_event = round((time.monotonic() - start) * 1000)
            process.wait()
            thread.join(timeout=10)
    except KeyboardInterrupt:
        interrupted = True
        subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True)
        if process:
            process.wait(timeout=15)
        manifest["error"] = "Interrupted by operator; not a model completion/failure verdict"
    except Exception as exc:
        manifest["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        manifest["error_category"] = "infrastructure"
        if process and process.poll() is None:
            subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True)
            process.wait(timeout=15)
    if cancel_event is not None and cancel_event.is_set():
        interrupted = True
        manifest["error"] = "Interrupted by operator; not a model completion/failure verdict"
        manifest["error_category"] = "operator_interrupted"
    manifest["duration_ms"] = round((time.monotonic() - start) * 1000)
    manifest["first_model_event_ms"] = first_event
    manifest["generation_finished_at"] = utcnow()
    summarized = adapter.summarize(events)
    manifest.update({"metrics": summarized["metrics"], "reported_models": summarized["reported_models"], "usage_raw": summarized["usage_raw"], "exit_code": process.returncode if process else None})
    (run_dir / "final.txt").write_text(summarized["final_text"], encoding="utf-8")
    if interrupted:
        manifest["status"] = "interrupted"
    elif manifest["error"] or summarized["result_status"] == "failed" or manifest["exit_code"] != 0:
        manifest["error"] = manifest["error"] or summarized["error"] or "CLI exited without a successful result"
        if manifest.get("error_category") == "infrastructure":
            manifest["status"] = "blocked"
        else:
            manifest["status"], manifest["error_category"] = classify_error(manifest["error"])
    else:
        manifest["status"] = "completed"
    manifest["generation_status"] = manifest["status"]
    manifest["status"] = "generated"
    manifest["finished_at"] = manifest["generation_finished_at"]
    try:
        manifest["generation_artifacts"] = generation_artifacts(artifact_index(run_dir))
        manifest["artifacts"] = manifest["generation_artifacts"]
    except (OSError, ValueError) as exc:
        manifest["archive_status"] = "integrity_error"
        manifest["archive_error"] = str(exc)
        manifest["generation_artifacts"] = []
    write_json(run_dir / "manifest.json", manifest)
    store.upsert_run(manifest)
    bridge.rmdir()
    print(json.dumps({"event": "run_generated", "id": run_id, "model": model,
                      "task": task["id"], "generation_status": manifest["generation_status"],
                      "duration_ms": manifest["duration_ms"], "metrics": manifest["metrics"],
                      "error": manifest["error"], "error_category": manifest.get("error_category"),
                      "attempt": attempt, "retry_of": retry_of}, ensure_ascii=False), flush=True)
    if defer_evaluation:
        return manifest
    result = evaluate_archive(config, store, run_dir, task["id"], run_id)
    if interrupted:
        raise KeyboardInterrupt
    return result


def generation_artifacts(artifacts):
    return [a for a in artifacts if not a["path"].startswith("evidence/")
            and a["path"] not in {"evaluation-stdout.txt", "evaluation-stderr.txt"}]


def verify_generation(run_dir, manifest):
    actual = artifact_index(run_dir)
    baseline = manifest.get("generation_artifacts", generation_artifacts(manifest.get("artifacts", [])))
    expected = {a["path"]: a["sha256"] for a in baseline}
    current = {a["path"]: a["sha256"] for a in generation_artifacts(actual)}
    if current != expected:
        raise ValueError("Generation archive changed after the session ended")
    return actual


# This archived implementation always wrote request_id after an upstream response,
# including null IDs. Unknown historical implementations cannot use that inference.
LEGACY_EGRESS_UPSTREAM_MARKERS = frozenset({
    "2d3c00abf7ebf12fc189935654ee87f43b4aa3867f43ef5ac6ebe75e759a8a5b",
})


def provider_route_check(events, *, legacy_upstream_markers=False):
    rejected = upstream_errors = upstream_403 = gateway_errors = unknown_403 = unverified = 0
    reasons = {}
    for event in events:
        origin = event.get("status_origin")
        status = event.get("status")
        if origin is None and legacy_upstream_markers and (
                "request_id" in event or "read_timeout_override" in event):
            origin = "upstream"
        if origin == "local_policy":
            if event.get("policy_rejected") is True and status == 403:
                rejected += 1
                reason = event.get("reason_code")
                if not isinstance(reason, str) or reason not in {"route_not_allowed", "query_not_allowed", "model_not_allowed", "tool_not_allowed"}:
                    reason = "unspecified"
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                unverified += 1
        elif origin == "upstream":
            if isinstance(status, int) and status >= 400:
                upstream_errors += 1
            upstream_403 += status == 403
        elif origin == "local_gateway":
            gateway_errors += 1
        elif status == 403:
            unknown_403 += 1
        elif not isinstance(status, int):
            unverified += 1
    verdict = "fail" if rejected else "unknown" if not events or unknown_403 or unverified else "pass"
    reason_counts = ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items())) or "none"
    return {"name": "provider_route", "status": verdict,
            "detail": f"Inference requests: {len(events)}; restricted route rejections: {rejected}; "
                      f"rejection reasons: {reason_counts}; upstream HTTP errors: {upstream_errors}; "
                      f"upstream 403: {upstream_403}; gateway errors: {gateway_errors}; "
                      f"unknown origin 403: {unknown_403}; unverified events: {unverified}. "
                      "Route compliance does not imply provider availability or successful delivery."}


def evaluate_archive(config, store, run_dir, task_id, run_id):
    """Evaluate immutable generation files; only derived evidence can change."""
    manifest = json.loads((run_dir / "manifest.json").read_text())
    output = run_dir / "output"
    manifest["checks"] = []
    try:
        verify_generation(run_dir, manifest)
        if (output / "index.html").is_file():
            manifest["evaluation"] = evaluate_run(config, run_dir, task_id, "llm-bench-eval-" + run_id)
        else:
            manifest["evaluation"] = {"status": "completed", "evidence": [], "checks": [{
                "name": "entrypoint", "status": "fail", "detail": "No index.html was delivered"}]}
        manifest["checks"] = list(manifest["evaluation"].get("checks", []))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        manifest["evaluation"] = {"status": "infrastructure_error", "error": str(exc), "checks": [], "evidence": []}
    gateway_events = []
    try:
        if (run_dir / "gateway.jsonl").exists():
            gateway_events = [json.loads(x) for x in (run_dir / "gateway.jsonl").read_text().splitlines() if x]
            if any(not isinstance(event, dict) for event in gateway_events):
                raise ValueError("Invalid inference event record")
    except (OSError, ValueError) as exc:
        manifest["evaluation"] = {"status": "infrastructure_error", "error": str(exc), "checks": [], "evidence": []}
        manifest["checks"] = []
        gateway_events = []
    manifest["checks"] = [c for c in manifest["checks"] if c["name"] != "provider_route"]
    legacy_markers = manifest.get("conditions", {}).get("egress_policy_sha256") in LEGACY_EGRESS_UPSTREAM_MARKERS
    manifest["checks"].append(provider_route_check(gateway_events, legacy_upstream_markers=legacy_markers))
    manifest["evaluation_finished_at"] = utcnow()
    if manifest["status"] == "generated":
        manifest["status"] = manifest["generation_status"]
    return seal(store, run_dir, manifest)


def seal(store, run_dir, manifest):
    try:
        manifest["artifacts"] = verify_generation(run_dir, manifest)
        manifest["metrics"].update(source_metrics(manifest["artifacts"]))
        manifest["archive_status"] = "complete"
        manifest.pop("archive_error", None)
    except (OSError, ValueError) as exc:
        manifest["archive_status"] = "integrity_error"
        manifest["archive_error"] = str(exc)
    # Reject an illegal state/fact change before replacing the authoritative disk manifest.
    manifest = store.validate_run(manifest)
    write_json(run_dir / "checksums.json", {a["path"]: a["sha256"] for a in manifest["artifacts"]})
    write_json(run_dir / "manifest.json", manifest)
    store.upsert_run(manifest)
    print(json.dumps({"event": "run_end", "id": manifest["id"], "model": manifest["model"],
                      "task": manifest["task_id"], "status": manifest["status"],
                      "duration_ms": manifest["duration_ms"], "metrics": manifest["metrics"],
                      "checks": manifest["checks"], "error": manifest["error"]}, ensure_ascii=False), flush=True)
    return manifest


def run_batch(root, config, purpose="benchmark", models=None, tasks=None, seed=None, concurrency=1, pairs=None, retry_runs=None, job_id=None):
    root = Path(root).resolve()
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ValueError("Concurrency must be a positive integer")
    if (pairs is not None or retry_runs) and models is not None:
        raise ValueError("Exact pairs/retries cannot be combined with a model product")
    tasks = tasks or [json.loads(p.read_text()) for p in sorted((root / "bench/tasks").glob("*.json"))]
    by_id = {task["id"]: task for task in tasks}
    free = discover_free(Path(config["paths"]["models_cache"]))
    entries = []
    if retry_runs:
        from .recovery import resolve_retries
        entries.extend(resolve_retries(root, config, retry_runs, tasks=tasks, job_id=job_id))
    if pairs is not None:
        entries.extend({"tool": tool, "model": model, "task_id": task_id, "attempt": 0}
                       for tool, model, task_id in pairs)
    elif not retry_runs:
        if models is None:
            models = [("claude-code", x) for x in claude_model_queue()] + [("opencode", x["id"]) for x in free]
        entries.extend({"tool": tool, "model": model, "task_id": task["id"], "attempt": 0}
                       for tool, model in models for task in tasks)
    seen = set()
    for entry in entries:
        if entry["tool"] not in {"claude-code", "opencode"}:
            raise ValueError(f"Unknown tool: {entry['tool']}")
        if entry["task_id"] not in by_id:
            raise ValueError(f"Unknown task id: {entry['task_id']}")
        key = (entry["tool"], entry["model"], entry["task_id"])
        if not entry["model"] or key in seen:
            raise ValueError(f"Empty model or duplicate queue entry: {key}")
        seen.add(key)
    if not entries:
        raise ValueError("Batch queue is empty")
    facts = preflight(config, root)
    facts["source_hashes"] = policy_hashes(root)
    runtime_config = {**config, "runtime": {**config.get("runtime", {}), "image": facts["image_id"]}}
    providers = connections(config)
    if any(entry["tool"] not in providers for entry in entries):
        raise ValueError("Queue tool has no configured provider")
    store = Store(root / config["paths"]["database"], root)
    batch_id = purpose + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    batch_dir = root / "data/batches" / batch_id
    batch_dir.mkdir(parents=True)
    os.chmod(batch_dir, 0o700)
    archive_policy(root, batch_dir / "protocol", facts["source_hashes"])
    catalogue_dir = batch_dir / "catalogue"
    catalogue_dir.mkdir()
    raw_catalogue = json.loads(Path(config["paths"]["models_cache"]).read_text())
    write_json(catalogue_dir / "models.json", {"opencode": raw_catalogue["opencode"]})
    seed = seed if seed is not None else random.SystemRandom().randrange(2**32)
    random.Random(seed).shuffle(entries)
    for entry in entries:
        entry.update({"status": "queued", "run_id": uuid.uuid4().hex})
    queue = [(entry["tool"], entry["model"], by_id[entry["task_id"]]) for entry in entries]
    batch = {"id": batch_id, "job_id": job_id, "purpose": purpose, "created_at": utcnow(),
             "seed": seed, "preflight": facts, "free_candidates": free, "tasks": tasks,
             "queue": entries, "run_ids": [], "status": "running", "concurrency": concurrency}
    batch_lock = threading.Lock()
    cancel_event = threading.Event()
    failure = None
    futures = {}

    def save():
        with batch_lock:
            write_json(batch_dir / "batch.json", batch)

    save()
    if job_id:
        from .jobs import write_state
        write_state(root, job_id, "running", batch_id=batch_id)
    print(json.dumps({"event": "batch_start", "batch_id": batch_id, "planned": len(queue),
                      "seed": seed, "concurrency": concurrency}), flush=True)

    def frozen():
        if image_id(config) != facts["image_id"]:
            raise RuntimeError("Runtime image changed during frozen batch")
        if policy_hashes(root) != facts["source_hashes"]:
            raise RuntimeError("Benchmark source policy changed during frozen batch")

    def cancelled():
        return cancel_event.is_set() or bool(job_id and (root / "data/jobs" / job_id / "cancel.json").exists())

    def fail(exc):
        nonlocal failure
        if failure is None:
            failure = exc
            batch["error"] = f"{type(exc).__name__}: {exc}"
        batch["status"] = "interrupted" if isinstance(failure, KeyboardInterrupt) else "blocked"
        if isinstance(exc, KeyboardInterrupt):
            cancel_event.set()
            for future, index in list(futures.items()):
                future.cancel()
                name = "llm-bench-" + entries[index]["run_id"]
                try:
                    subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True, timeout=30)
                except (OSError, subprocess.TimeoutExpired) as cleanup:
                    batch.setdefault("cleanup_errors", []).append(str(cleanup))
        save()

    def finish(run, index):
        entry = entries[index]
        if run["id"] not in batch["run_ids"]:
            batch["run_ids"].append(run["id"])
        entry.update({"run_id": run["id"], "status": run["status"], "archive": run["archive_dir"],
                      "generation_status": run.get("generation_status", run["status"])})
        if (failure is None and not cancelled() and run.get("error_category") == "transport_interrupted"
                and entry["attempt"] == 0 and not entry.get("retried")):
            child_id = uuid.uuid4().hex
            entry.update({"retried": True, "retry_run_id": child_id, "retry_reason": run.get("error")})
            tool, model, task = queue[index]
            queue.append((tool, model, task))
            entries.append({"tool": tool, "model": model, "task_id": task["id"], "status": "queued",
                            "run_id": child_id, "attempt": 1, "retry_of": run["id"], "retry_reason": run.get("error")})
            print(json.dumps({"event": "run_retry_queued", "model": model, "task_id": task["id"],
                              "retry_of": run["id"], "run_id": child_id, "reason": run.get("error")}), flush=True)
        save()

    def collect(future, index):
        if future.cancelled():
            entries[index]["status"] = "interrupted"
            save()
            return
        try:
            finish(future.result(), index)
        except BaseException as exc:
            fail(exc)
            # A worker may fail after writing its generated manifest; recover that exact ID.
            paths = list((root / "runs").glob("*/" + entries[index]["run_id"] + "/manifest.json"))
            if len(paths) == 1:
                run = json.loads(paths[0].read_text())
                if run.get("status") not in {"running", "queued"}:
                    finish(run, index)
            entries[index]["worker_error"] = f"{type(exc).__name__}: {exc}"
            if entries[index]["status"] == "running":
                entries[index]["status"] = "blocked"
            save()

    # A list iterator can see append; the historical miss occurred because the
    # last completion drain queued retries after submission had already ended.
    submitted = 0
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while submitted < len(queue) or futures:
                try:
                    if cancelled() and failure is None:
                        raise KeyboardInterrupt
                    while failure is None and submitted < len(queue) and len(futures) < concurrency:
                        frozen()
                        index = submitted
                        tool, model, task = queue[index]
                        entry = entries[index]
                        entry["status"] = "running"
                        save()
                        future = pool.submit(run_one, root, runtime_config, store, providers[tool], catalogue_dir,
                                             facts, batch_id, purpose, tool, model, task, True,
                                             run_id=entry["run_id"], attempt=entry["attempt"],
                                             retry_of=entry.get("retry_of"), retry_reason=entry.get("retry_reason"),
                                             cancel_event=cancel_event)
                        futures[future] = index
                        submitted += 1
                    if not futures:
                        break
                    done, _ = wait(list(futures), timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        collect(future, futures.pop(future))
                except (KeyboardInterrupt, Exception) as exc:
                    fail(exc)
        if cancelled() and failure is None:
            fail(KeyboardInterrupt())
        # Failed siblings do not prevent the remaining generated archives from sealing.
        for index, entry in enumerate(entries):
            if entry["status"] != "generated":
                continue
            try:
                frozen()
                run = evaluate_archive(runtime_config, store, root / entry["archive"], queue[index][2]["id"], entry["run_id"])
                entry["status"] = run.get("status", entry["generation_status"])
                entry["evaluation_status"] = run["evaluation"].get("status")
                if entry["evaluation_status"] != "completed":
                    batch.setdefault("evaluation_errors", []).append(entry["run_id"])
            except (KeyboardInterrupt, Exception) as exc:
                entry["evaluation_error"] = f"{type(exc).__name__}: {exc}"
                fail(exc)
            save()
        if failure is None:
            batch["status"] = "blocked" if batch.get("evaluation_errors") else "completed"
    finally:
        batch["finished_at"] = utcnow()
        save()
    if failure is not None:
        raise failure
    return batch
