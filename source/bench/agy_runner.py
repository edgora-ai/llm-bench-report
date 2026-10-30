"""Local AGY benchmark path: one isolated host session, same archive and evaluation.

This is NOT the docker-network-none-unix-inference-bridge-v1 strategy. The AGY
CLI runs on the host with its own project directory and cwd set to this run's
output directory. This is not an OS-level filesystem boundary. No inference
gateway exists for this tool: provider_route stays unknown and unreported
metrics stay null.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bench import runner as runner_module  # noqa: E402
from bench.adapters import agy  # noqa: E402
from bench.cli import load_config  # noqa: E402
from bench.storage import Store  # noqa: E402

REDACTIONS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"AIza[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED]"),
]
ENV_SECRETS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENCODE_API_KEY",
               "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY")
STRATEGY = "local-isolated-agy-project-directory-v1"
PRINT_TIMEOUT = "3h"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    with Path(path).open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def sanitizing_text(extra_secrets=()):
    secrets = [value for value in (os.environ.get(name) for name in ENV_SECRETS)
               if isinstance(value, str) and len(value) > 8]
    secrets += [value for value in extra_secrets if isinstance(value, str) and len(value) > 8]

    def sanitize(text):
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern, replacement in REDACTIONS:
            text = pattern.sub(replacement, text)
        return text
    return sanitize


def session_environment():
    """Only the base environment is inherited; bench/provider secrets are not."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_RUNTIME_DIR", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "TMPDIR")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(Path.home())
    return env


def stop_process_group(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=20)
    except (ProcessLookupError, PermissionError):
        pass
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=10)
        except (ProcessLookupError, PermissionError):
            pass


def agy_version():
    result = subprocess.run([agy.CLI, "--version"], capture_output=True, text=True, timeout=30)
    return result.stdout.strip() or result.stderr.strip()


def build_manifest(root, task, purpose, tool_model, run_id, date, batch_id, prompt):
    model = tool_model
    variant = model.rsplit("-", 1)[-1]
    effort = variant if variant in ("low", "medium", "high") else None
    # The AGY session runs with its cwd at this run's output directory, so the
    # project flag only needs the relative layout, never a host path.
    project = f"runs/{date}/{run_id}/agy-project"
    cli = agy.command(model, None, project=project, log_file="agy-cli.log", print_timeout=PRINT_TIMEOUT)
    conditions = {
        "task_id": task["id"],
        "runner_sha256": hashlib.sha256(Path(runner_module.__file__).read_bytes()).hexdigest(),
        "runner_path": "bench/agy_runner.py",
        "provider_connection_sha256": None,
        "prompt_hash": runner_module.digest(prompt),
        "rubric_version": task["rubric_version"],
        "tool": "agy",
        "cli_version": agy_version(),
        "image_id": None,
        "profile": "host-local",
        "isolation": STRATEGY,
        "renderer": None,
        "adapter_sha256": hashlib.sha256(Path(agy.__file__).read_bytes()).hexdigest(),
        "runtime_policy_sha256": None,
        "egress_policy_sha256": hashlib.sha256((root / "bench/egress.py").read_bytes()).hexdigest(),
        "command_sha256": runner_module.digest(["<PROMPT>" if arg == "<PROMPT>" else arg for arg in cli]),
        "task_definition_sha256": runner_module.digest(task),
        "model_directory_sha256": None,
        "reasoning_effort_requested": effort,
        "reasoning_effort_applied": None,
        "reasoning_effort_supported": None,
        "reasoning_effort_control": ("vendor_variant_in_model_name_not_bench_effort_ladder" if effort else
                                     "vendor_managed_thinking_no_explicit_effort"),
        "print_timeout_s": PRINT_TIMEOUT,
    }
    manifest = {
        "id": run_id, "batch_id": batch_id, "date": date, "purpose": purpose, "tool": "agy",
        "model": model, "task_id": task["id"], "task_name": task["name"], "prompt_version": task["version"],
        "prompt_hash": conditions["prompt_hash"], "condition_fingerprint": runner_module.digest(conditions),
        "conditions": conditions, "started_at": utcnow(), "finished_at": None, "status": "running",
        "error": None, "metrics": {}, "checks": [], "evaluation": {"status": "pending", "evidence": []},
        "artifacts": [], "archive_dir": f"runs/{date}/{run_id}", "requested_model": model,
        "reported_models": [], "backend_weights_version": None, "duration_ms": None,
        "attempt": 0, "retry_of": None, "retry_reason": None,
        # AGY run facts that are only knowable after the session live in a
        # separate closed schema: storage freezes every top-level field outside
        # its known mutable sets, and conditions are themselves frozen at
        # creation, so neither can grow a key later.
        "agy_facts": {"stdout_non_json_lines": None, "agy_project_residue": None,
                      "agy_cwd": None, "agy_project_dir": None},
    }
    return manifest, cli


def run_agy(root, purpose, task_id, model, batch_id="agy-batch"):
    root = Path(root).resolve()
    config = load_config(None)
    store = Store(root / config["paths"]["database"], root)
    task = json.loads((root / "bench/tasks" / f"{task_id}.json").read_text())
    if model not in agy.MODELS:
        raise ValueError("AGY model must be one of: " + ", ".join(sorted(agy.MODELS)))
    if purpose not in ("smoke", "benchmark"):
        raise ValueError("purpose must be smoke or benchmark")
    date = datetime.now(timezone.utc).date().isoformat()
    run_id = uuid.uuid4().hex
    run_dir = root / "runs" / date / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o700)
    output = run_dir / "output"
    output.mkdir()
    project = run_dir / "agy-project"
    project.mkdir(mode=0o700)
    manifest, cli = build_manifest(root, task, purpose, model, run_id, date, batch_id, task["prompt"])
    # Every field under conditions is part of the frozen run identity, so the
    # generation facts this session will learn are declared up front, not added
    # afterwards; only their values change below.
    manifest["agy_facts"].update({"stdout_non_json_lines": None, "agy_project_residue": None,
                                  # Run-relative locations only. Absolute host paths and
                                  # the project archive layout never reach a report.
                                  "agy_cwd": "output",
                                  "agy_project_dir": "agy-project"})
    manifest["condition_fingerprint"] = runner_module.digest(manifest["conditions"])
    (run_dir / "prompt.txt").write_text(task["prompt"], encoding="utf-8")
    write_json(run_dir / "task.json", task)
    write_json(run_dir / "manifest.json", manifest)
    store.upsert_run(manifest)
    print(json.dumps({"event": "run_start", "id": run_id, "purpose": purpose, "tool": "agy",
                      "model": model, "task": task_id, "isolation": STRATEGY}, ensure_ascii=False), flush=True)
    # cli[2] is the frozen "<PROMPT>" placeholder; substitute the real prompt there.
    argv = list(cli)
    argv[2] = task["prompt"]
    log_path = run_dir / "agy-cli.log"
    sanitize = sanitizing_text()
    start = time.monotonic()
    events, non_json, stderr_lines = [], 0, []
    process = None
    interrupted = False
    error = None
    try:
        # --log-file is recorded in the frozen command, so the CLI must be told
        # the real path before argv is handed to the process.
        final_argv = [arg.replace("agy-cli.log", str(log_path)) if arg == "agy-cli.log" else arg for arg in argv]
        process = subprocess.Popen(final_argv, cwd=str(output), env=session_environment(), start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
        with (run_dir / "stdout.jsonl").open("w", encoding="utf-8") as out_stream:
            assert process.stdout is not None
            for line in process.stdout:
                out_stream.write(line)
                out_stream.flush()
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    non_json += 1
                    continue
                if isinstance(event, dict):
                    events.append(event)
        process.wait()
        stderr_text = process.stderr.read() if hasattr(process.stderr, "read") else "".join(process.stderr)
        stderr_lines = stderr_text.splitlines()
    except KeyboardInterrupt:
        interrupted = True
        error = "Interrupted by operator; not a model completion/failure verdict"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        manifest["error_category"] = "infrastructure"
    finally:
        stop_process_group(process)
    sanitize = sanitizing_text()
    (run_dir / "stderr.txt").write_text("\n".join(sanitize(line) for line in stderr_lines) + "\n", encoding="utf-8")
    if log_path.exists():
        log_path.write_text(sanitize(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    residue = sorted(item.name for item in project.iterdir())
    shutil.rmtree(project, ignore_errors=True)
    exit_code = process.returncode if process else None
    summarized = agy.summarize(events)
    # Facts only knowable after the session fill the pre-declared agy_facts schema.
    manifest["agy_facts"].update({"stdout_non_json_lines": non_json, "agy_project_residue": residue})
    manifest["duration_ms"] = round((time.monotonic() - start) * 1000)
    manifest["generation_finished_at"] = utcnow()
    manifest["metrics"] = summarized["metrics"]
    manifest["reported_models"] = summarized["reported_models"]
    manifest["usage_raw"] = summarized["usage_raw"]
    manifest["exit_code"] = exit_code
    if interrupted:
        manifest["status"] = "interrupted"
        manifest["error"] = error
    elif error or summarized["result_status"] != "completed" or exit_code != 0:
        message = error or summarized["error"] or "AGY exited without a proven terminal result"
        manifest["error"] = message
        if summarized["result_status"] != "failed" and not error:
            manifest["error_category"] = "no_terminal_result"
            manifest["status"] = "failed"
        elif error and manifest.get("error_category") != "infrastructure":
            manifest["status"] = "failed"
        else:
            manifest["status"], manifest["error_category"] = runner_module.classify_error(message)
    else:
        manifest["status"] = "completed"
    manifest["generation_status"] = manifest["status"]
    manifest["status"] = "generated"
    manifest["finished_at"] = manifest["generation_finished_at"]
    (run_dir / "final.txt").write_text(summarized["final_text"], encoding="utf-8")
    try:
        manifest["generation_artifacts"] = runner_module.generation_artifacts(runner_module.artifact_index(run_dir))
        manifest["artifacts"] = manifest["generation_artifacts"]
    except (OSError, ValueError) as exc:
        manifest["archive_status"] = "integrity_error"
        manifest["archive_error"] = str(exc)
        manifest["generation_artifacts"] = []
    write_json(run_dir / "manifest.json", manifest)
    store.upsert_run(manifest)
    print(json.dumps({"event": "run_generated", "id": run_id, "model": model, "task": task_id,
                      "generation_status": manifest["generation_status"], "duration_ms": manifest["duration_ms"],
                      "metrics": manifest["metrics"], "error": manifest["error"],
                      "error_category": manifest.get("error_category"), "reported_models": manifest["reported_models"]},
                     ensure_ascii=False), flush=True)
    result = runner_module.evaluate_archive(config, store, run_dir, task_id, run_id)
    print(json.dumps({"event": "run_end", "id": run_id, "status": result["status"],
                      "checks": result["checks"], "evaluation_status": result["evaluation"].get("status")},
                     ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description="One isolated local AGY benchmark session")
    parser.add_argument("--purpose", choices=["smoke", "benchmark"], default="benchmark")
    parser.add_argument("--tool", choices=["agy"], default="agy")
    parser.add_argument("--model", required=True)
    parser.add_argument("--task", required=True, choices=["crocodile", "blackhole"])
    args = parser.parse_args()
    try:
        manifest = run_agy(ROOT, args.purpose, args.task, args.model)
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc))
    return 0 if manifest["status"] in ("completed", "failed", "interrupted") else 1


if __name__ == "__main__":
    sys.exit(main())