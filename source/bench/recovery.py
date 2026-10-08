"""Read-only validation for exact recovery queues and archived retry parents."""
import hashlib
import json
from pathlib import Path

from .storage import safe_open, validate_id

TOOLS = frozenset({"claude-code", "opencode"})


def _read_json(root, relative):
    with safe_open(Path(root), relative) as stream:
        return json.load(stream)


def _tasks(root):
    return {task["id"]: task for task in
            (json.loads(path.read_text()) for path in sorted((Path(root) / "bench/tasks").glob("*.json")))}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def resolve_retry_runs(root, run_ids, exclude_job_id=None):
    """Resolve exact sealed first-attempt transport parents, never an index guess.

    No archive or database is changed. Callers launching jobs hold the jobs
    launch lock across this check and persistence of the durable claim.
    """
    from .runner import artifact_index, classify_error
    from .jobs import status

    root = Path(root).resolve()
    ids = list(run_ids or [])
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate --retry-run ID")
    for run_id in ids:
        validate_id(run_id)
    if not ids:
        return []
    tasks = _tasks(root)
    manifests = []
    by_id = {}
    for path in sorted((root / "runs").glob("*/*/manifest.json")):
        manifest = _read_json(root, path.relative_to(root).as_posix())
        manifests.append(manifest)
        by_id.setdefault(manifest.get("id"), []).append((path, manifest))
    batches = [_read_json(root, path.relative_to(root).as_posix())
               for path in (root / "data/batches").glob("*/batch.json")]
    parents = []
    triples = set()
    for run_id in ids:
        matches = by_id.get(run_id, [])
        if len(matches) != 1:
            raise ValueError(f"Retry parent {run_id}: expected exactly one archived run")
        path, parent = matches[0]
        expected = f"runs/{parent.get('date')}/{run_id}"
        if parent.get("archive_dir") != expected or path.parent.relative_to(root).as_posix() != expected:
            raise ValueError(f"Retry parent {run_id}: archive identity mismatch")
        if parent.get("status") != "failed" or parent.get("archive_status") != "complete":
            raise ValueError(f"Retry parent {run_id}: must be a complete failed archive")
        if parent.get("attempt", 0) != 0 or parent.get("retry_of"):
            raise ValueError(f"Retry parent {run_id}: a retry cannot itself be retried")
        error = parent.get("error")
        if not isinstance(error, str) or not error.strip() or classify_error(error)[1] != "transport_interrupted":
            raise ValueError(f"Retry parent {run_id}: not a transport interruption")
        if parent.get("tool") not in TOOLS or not isinstance(parent.get("model"), str) or not parent["model"].strip():
            raise ValueError(f"Retry parent {run_id}: invalid tool/model")
        artifacts = parent.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError(f"Retry parent {run_id}: missing artifact index")
        archived = {entry["path"]: entry["sha256"] for entry in artifacts}
        if len(archived) != len(artifacts):
            raise ValueError(f"Retry parent {run_id}: duplicate artifact paths")
        actual = {entry["path"]: entry["sha256"] for entry in artifact_index(path.parent)}
        checksums = _read_json(root, expected + "/checksums.json")
        if not archived or actual != archived or checksums != archived:
            raise ValueError(f"Retry parent {run_id}: artifact checksum mismatch")
        task = tasks.get(parent.get("task_id"))
        archived_task = _read_json(root, expected + "/task.json")
        conditions = parent.get("conditions", {})
        if (task is None or archived_task != task or parent.get("prompt_version") != task.get("version")
                or parent.get("prompt_hash") != _digest(task.get("prompt"))
                or conditions.get("task_definition_sha256") != _digest(task)):
            raise ValueError(f"Retry parent {run_id}: task version/definition mismatch")
        triple = (parent["tool"], parent["model"], parent["task_id"])
        if triple in triples:
            raise ValueError("Duplicate recovery queue combination")
        triples.add(triple)
        if any(item.get("retry_of") == run_id and item.get("id") != run_id for item in manifests):
            raise ValueError(f"Retry parent {run_id}: already has an actual child")
        # Historical batch metadata may record lineage only in the queue.
        # A queued placeholder is not an actual attempt; a run_id is.
        for batch in batches:
            for item in batch.get("queue", []):
                child = item.get("run_id")
                if item.get("retry_of") == run_id and child and child != run_id and child in by_id:
                    raise ValueError(f"Retry parent {run_id}: already has an actual child")
        parents.append(parent)
    for job_path in (root / "data/jobs").glob("*/job.json"):
        job_id = job_path.parent.name
        if job_id == exclude_job_id:
            continue
        job = status(root, job_id)
        claimed = set(job.get("retry_runs", []))
        for batch in batches:
            if batch.get("id") == job.get("batch_id"):
                claimed.update(item["retry_of"] for item in batch.get("queue", [])
                               if item.get("retry_of") and item.get("run_id") != item["retry_of"])
        if claimed & set(ids):
            if ((job["process_alive"] and job.get("status") not in {"completed", "failed", "interrupted", "unexpected_exit"})
                    or job.get("launching_active")):
                raise ValueError("Retry parent has an active recovery claim: " + job_id)
    return parents


def resolve_retries(root, config, retry_runs, tasks=None, job_id=None):
    """Queue metadata adapter for callers that need entries rather than parents."""
    parents = resolve_retry_runs(root, retry_runs, exclude_job_id=job_id)
    if tasks is not None:
        available = {task["id"] for task in tasks}
        if any(parent["task_id"] not in available for parent in parents):
            raise ValueError("Retry task is outside the supplied task set")
    return [{"tool": parent["tool"], "model": parent["model"], "task_id": parent["task_id"],
             "attempt": 1, "retry_of": parent["id"], "retry_reason": parent["error"]}
            for parent in parents]


def validate_request(root, purpose="benchmark", tool=None, models=None, task=None,
                     concurrency=1, pairs=None, retry_runs=None, job_id=None):
    """Validate user queue selectors before directories, workers or Docker calls."""
    from .cli import parse_pairs

    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("--concurrency must be a positive integer")
    if purpose not in {"smoke", "benchmark"}:
        raise ValueError("Invalid purpose")
    if tool is not None and tool not in TOOLS:
        raise ValueError("Unknown tool: " + str(tool))
    if models and not tool:
        raise ValueError("--model requires --tool")
    if (pairs or retry_runs) and (tool or models or task):
        raise ValueError("--pair/--retry-run are the whole queue; cannot combine with --task, --tool or --model")
    normalized = [",".join(entry) if isinstance(entry, (tuple, list)) else entry for entry in pairs or []]
    parsed = parse_pairs(normalized)
    if len(set(parsed)) != len(parsed):
        raise ValueError("Duplicate --pair queue entry")
    available = _tasks(root)
    if task and task not in available:
        raise ValueError("Unknown task id: " + str(task))
    for _, _, task_id in parsed:
        if task_id not in available:
            raise ValueError("Unknown task id: " + task_id)
    for model in models or []:
        if not isinstance(model, str) or not model.strip() or model != model.strip():
            raise ValueError("Model must be a nonempty trimmed string")
    if len(set(models or [])) != len(models or []):
        raise ValueError("Duplicate --model")
    parents = resolve_retry_runs(root, retry_runs, exclude_job_id=job_id)
    combinations = set(parsed)
    for parent in parents:
        if parent.get("purpose") != purpose:
            raise ValueError("Retry parent purpose does not match requested purpose")
        triple = (parent["tool"], parent["model"], parent["task_id"])
        if triple in combinations:
            raise ValueError("Duplicate recovery queue combination")
        combinations.add(triple)
    return parsed
