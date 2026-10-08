"""Project CLI: protocols, model discovery, isolation, batches, and dashboard."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    config = tomllib.loads(Path(path or ROOT / "config/bench.toml").read_text())
    config["server"]["host"] = os.environ.get("BENCH_HOST", config["server"]["host"])
    config["server"]["port"] = int(os.environ.get("BENCH_PORT", config["server"]["port"]))
    config["runtime"]["image"] = os.environ.get("BENCH_IMAGE", config["runtime"]["image"])
    latest = ROOT / "data/discovery/latest-models.json"
    if latest.exists():
        config["paths"]["models_cache"] = str(latest)
    return config


def dashboard_token(config):
    path = ROOT / config["paths"]["token_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def parse_pairs(values):
    """Parse --pair entries into (tool, model, task_id) triples.

    An exact queue is the only way to resume a batch without generating the
    pairs it already completed, so each entry is validated here rather than
    producing a half-formed batch that fails later.
    """
    pairs = []
    for value in values or []:
        parts = value.split(",")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise ValueError(f"--pair must be TOOL,MODEL,TASK: {value!r}")
        tool, model, task_id = (p.strip() for p in parts)
        if tool not in ("claude-code", "opencode"):
            raise ValueError(f"--pair tool must be claude-code or opencode: {tool!r}")
        pairs.append((tool, model, task_id))
    return pairs


def discover(config, refresh):
    from .discovery import discover_free, CLAUDE_MODELS
    from .egress import resolve_egress
    cache = Path(config["paths"]["models_cache"])
    if refresh:
        folder = ROOT / "data/discovery"
        home = folder / "home"
        home.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"), "XDG_STATE_HOME": str(home / "state"), "OPENCODE_DISABLE_CLAUDE_CODE": "true", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true", "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true"}
        proxy = resolve_egress(config["paths"]["opencode_egress_config"], config["paths"]["opencode_egress_list"])
        if proxy:
            env["https_proxy"] = proxy
            env["HTTPS_PROXY"] = proxy
        result = subprocess.run(["/home/ubuntu/.opencode/bin/opencode", "models", "opencode", "--verbose", "--refresh", "--pure"], env=env, capture_output=True, text=True, timeout=120)
        # Persist discovery output privately; do not copy user settings/auth.
        (folder / "refresh-stdout.txt").write_text(result.stdout)
        (folder / "refresh-stderr.txt").write_text(result.stderr)
        refreshed = home / "cache/opencode/models.json"
        if result.returncode != 0 or not refreshed.is_file():
            raise RuntimeError("Isolated directory refresh failed; inspect data/discovery/refresh-stderr.txt. Existing cache is not silently presented as refreshed.")
        catalogue = json.loads(refreshed.read_text())
        if "opencode" not in catalogue:
            raise RuntimeError("Refreshed catalogue has no OpenCode provider")
        cache = folder / "latest-models.json"
        cache.write_text(json.dumps({"opencode": catalogue["opencode"]}, ensure_ascii=False, indent=2))
    report = {"claude_code_requested": list(CLAUDE_MODELS), "opencode_free_candidates": discover_free(cache), "directory_source": str(cache), "availability": "candidate_not_call_verified"}
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Fixed, isolated single-session model benchmarks")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="Prove OS file/network isolation without a model call")
    d = sub.add_parser("discover", help="List free metadata candidates; not a generation health check")
    d.add_argument("--refresh", action="store_true")
    sub.add_parser("tasks")
    sub.add_parser("serve", help="Start authenticated local dashboard")
    sub.add_parser("reindex", help="Rebuild SQLite indexes from immutable archives")
    sub.add_parser("token", help="Show the private dashboard login token")
    r = sub.add_parser("run", help="One frozen serial batch; no budget/turn caps or fallback")
    r.add_argument("--purpose", choices=["smoke", "benchmark"], default="benchmark")
    r.add_argument("--tool", choices=["claude-code", "opencode"])
    r.add_argument("--model", action="append")
    r.add_argument("--task", choices=["crocodile", "blackhole"])
    r.add_argument("--seed", type=int)
    r.add_argument("--concurrency", type=int, default=1, help="Concurrent sessions; evaluation stays serial")
    r.add_argument("--job-id", help=argparse.SUPPRESS)
    r.add_argument("--pair", action="append", metavar="TOOL,MODEL,TASK",
                   help="Exact queue entry, repeatable; replaces the model x task product")
    r.add_argument("--retry-run", action="append", metavar="RUN_ID",
                   help="Retry an exact archived first-attempt transport failure; repeatable")
    s = sub.add_parser("start", help="Launch a finite serial batch independently of this CLI session")
    s.add_argument("--purpose", choices=["smoke", "benchmark"], default="benchmark")
    s.add_argument("--tool", choices=["claude-code", "opencode"])
    s.add_argument("--model", action="append")
    s.add_argument("--task", choices=["crocodile", "blackhole"])
    s.add_argument("--seed", type=int)
    s.add_argument("--concurrency", type=int, default=1, help="Concurrent sessions; evaluation stays serial")
    s.add_argument("--pair", action="append", metavar="TOOL,MODEL,TASK",
                   help="Exact queue entry, repeatable; replaces the model x task product")
    s.add_argument("--retry-run", action="append", metavar="RUN_ID",
                   help="Retry an exact archived first-attempt transport failure; repeatable")
    j = sub.add_parser("jobs", help="Inspect a durable batch job")
    j.add_argument("id")
    stop = sub.add_parser("stop", help="Cancel a durable batch; preserves interrupted evidence")
    stop.add_argument("id")
    sub.add_parser("verify", help="Verify final artifact hashes against disk")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command in {"start", "run"}:
        from .recovery import validate_request
        try:
            pairs = validate_request(ROOT, args.purpose, args.tool, args.model, args.task,
                                     args.concurrency, args.pair, args.retry_run,
                                     getattr(args, "job_id", None))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.command == "preflight":
        from .isolation import preflight
        print(json.dumps(preflight(config, ROOT), ensure_ascii=False, indent=2))
    elif args.command == "discover":
        discover(config, args.refresh)
    elif args.command == "tasks":
        print(json.dumps([json.loads(p.read_text()) for p in sorted((ROOT / "bench/tasks").glob("*.json"))], ensure_ascii=False, indent=2))
    elif args.command == "token":
        print(dashboard_token(config))
    elif args.command == "serve":
        from .server import serve
        token = dashboard_token(config)
        print(f"Dashboard: http://{config['server']['host']}:{config['server']['port']} — login token at {config['paths']['token_file']}", flush=True)
        serve(ROOT, ROOT / config["paths"]["database"], config["server"]["host"], config["server"]["port"], token)
    elif args.command == "reindex":
        from .storage import Store
        print(json.dumps(Store(ROOT / config["paths"]["database"], ROOT).rebuild(), ensure_ascii=False))
    elif args.command == "start":
        from .jobs import start
        try:
            record = start(ROOT, args.purpose, args.seed, args.tool, args.model, args.task,
                           args.config, args.concurrency, args.pair, retry_runs=args.retry_run)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        print(json.dumps(record, ensure_ascii=False))
    elif args.command in {"jobs", "stop"}:
        from .jobs import status, stop
        print(json.dumps((status if args.command == "jobs" else stop)(ROOT, args.id), ensure_ascii=False))
    elif args.command == "run":
        def interrupt_run(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, interrupt_run)
        signal.signal(signal.SIGTERM, interrupt_run)
        from .discovery import claude_model_queue, discover_free
        from .runner import run_batch
        models = None
        if pairs or args.retry_run:
            pass  # Exact recovery selectors must not trigger discovery/product expansion.
        elif args.model:
            if not args.tool:
                parser.error("--model requires --tool")
            models = [(args.tool, m) for m in args.model]
        elif args.tool:
            models = [(args.tool, m) for m in (claude_model_queue() if args.tool == "claude-code" else [m["id"] for m in discover_free(Path(config["paths"]["models_cache"]))])]
        elif args.purpose == "smoke":
            free = discover_free(Path(config["paths"]["models_cache"]))
            if not free:
                raise RuntimeError("No metadata-confirmed free candidates")
            preferred = next((m for m in free if m["id"] == "opencode/ling-3.0-flash-fin-free"), free[0])
            models = [("claude-code", "gpt-6.1-sol"), ("opencode", preferred["id"])]
        tasks = None
        if args.task:
            tasks = [json.loads((ROOT / "bench/tasks" / (args.task + ".json")).read_text())]
        from .jobs import foreground_claim, write_state
        claim = (foreground_claim(ROOT, args.retry_run, args.purpose, args.concurrency, pairs)
                 if args.retry_run and not args.job_id else nullcontext(args.job_id))
        with claim as job_id:
            if job_id:
                write_state(ROOT, job_id, "running")
            try:
                batch = run_batch(ROOT, config, args.purpose, models, tasks, args.seed,
                                  args.concurrency, pairs or None,
                                  retry_runs=args.retry_run, job_id=job_id)
            except KeyboardInterrupt:
                if job_id:
                    write_state(ROOT, job_id, "interrupted")
                raise
            except Exception as exc:
                if job_id:
                    write_state(ROOT, job_id, "failed", error_type=type(exc).__name__)
                raise
            if job_id:
                state = "completed" if batch["status"] == "completed" else "failed"
                write_state(ROOT, job_id, state, batch_id=batch["id"],
                            batch_status=batch["status"], run_count=len(batch["run_ids"]))
        print(json.dumps({"batch_id": batch["id"], "status": batch["status"], "run_count": len(batch["run_ids"])}))
        if batch["status"] != "completed":
            return 1
    elif args.command == "verify":
        from .runner import artifact_index
        checked = 0
        active_skipped = 0
        errors = []
        for path in sorted((ROOT / "runs").glob("*/*/manifest.json")):
            run = json.loads(path.read_text())
            if run.get("status") in {"running", "queued"}:
                active_skipped += 1
                continue
            if run.get("archive_status") != "complete":
                errors.append({"id": run["id"], "error": "Archive not complete"})
                continue
            actual = {a["path"]: a["sha256"] for a in artifact_index(path.parent)}
            expected = {a["path"]: a["sha256"] for a in run["artifacts"]}
            if actual != expected:
                errors.append({"id": run["id"], "error": "Artifact checksum mismatch"})
            checked += 1
        print(json.dumps({"archives_checked": checked, "active_skipped": active_skipped, "errors": errors}, ensure_ascii=False))
        if errors:
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted; existing archives preserved", file=sys.stderr)
        sys.exit(130)
