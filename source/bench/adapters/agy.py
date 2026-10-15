"""Local AGY 1.3.5 adapter; measured stream-json contract, no Docker claims.

Probed locally on 2026-10-14 (v1.3.5, model gemini-3.8-flash-high):
`-p <prompt>` carries the prompt, `--print-timeout` needs a unit, file writes
follow the process cwd (`--project` only selects conversation ownership),
`--dangerously-skip-permissions` yields init.permission_mode=always-proceed,
`--effort high` is accepted, and the JSON result is one object
{conversation_id, status, response, duration_seconds, num_turns, usage}
while stream-json emits NDJSON events init / step_update / result with
usage keys input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
total_tokens. Any measurement the stream does not report stays None.
"""

from .metrics import add_model, empty_summary, error_text, number

CLI = "/home/ubuntu/.local/bin/agy"
MODELS = frozenset({"gemini-3.8-flash-high", "gemini-3.8-flash-medium",
                    "gemini-3.8-flash-low", "claude-sonnet-4-6",
                    "claude-opus-4-6-thinking", "gpt-oss-120b-medium"})
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
TERMINAL_SUCCESS = {"SUCCESS"}


def command(model, effort=None, *, project, log_file, print_timeout="0s"):
    if model not in MODELS:
        raise ValueError("AGY model must be one of: " + ", ".join(sorted(MODELS)))
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ValueError("Unsupported AGY effort: " + str(effort))
    if not isinstance(project, str) or not project or not isinstance(log_file, str) or not log_file:
        raise ValueError("AGY needs an isolated project directory and log file")
    # The prompt is attached to -p; stdin is not the prompt channel.
    argv = [CLI, "-p", "<PROMPT>", "--model", model, "--output-format", "stream-json",
            "--input-format", "text", "--dangerously-skip-permissions", "--disable-slash-commands",
            "--project", project, "--log-file", log_file, "--print-timeout", str(print_timeout)]
    if effort is not None:
        argv += ["--effort", effort]
    return argv


def _usage_metrics(usage):
    if not isinstance(usage, dict):
        return {}
    return {"input_tokens": number(usage.get("input_tokens")),
            "output_tokens": number(usage.get("output_tokens")),
            "cache_read_tokens": number(usage.get("cache_read_tokens")),
            "reasoning_tokens": number(usage.get("thinking_tokens")),
            "total_tokens": number(usage.get("total_tokens"))}


def _result(event):
    return event.get("result") if isinstance(event.get("result"), dict) else event


def summarize(events: list[dict]) -> dict:
    summary = empty_summary()
    init, result, statuses, errors = None, None, [], []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("event") or event.get("type")
        if kind == "init" and isinstance(event.get("init"), dict):
            init = event["init"]
            add_model(summary, init.get("model"))
        payload = _result(event) if kind == "result" else event
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            result = payload
        if isinstance(payload, dict) and isinstance(payload.get("status"), str):
            statuses.append(payload["status"])
        for key in ("error", "message"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if value:
                errors.append(value)
    if init is None and result is not None:
        add_model(summary, result.get("model"))
    if result is not None:
        summary["final_text"] = result.get("response") if isinstance(result.get("response"), str) else ""
        summary["metrics"].update(_usage_metrics(result.get("usage")))
        duration = number(result.get("duration_seconds"))
        if duration is not None:
            summary["metrics"]["api_duration_ms"] = round(duration * 1000)
        turns = number(result.get("num_turns"))
        if turns is not None:
            summary["metrics"]["num_turns"] = turns
        summary["usage_raw"] = {
            "usage": result.get("usage"),
            "usage_semantics": "agy_stream_json_v1_3_5_unverified_against_provider_bill",
            "thinking_semantics": "thinking_tokens_reported_separately_from_output_tokens",
            "total_semantics": "vendor_reported_total_not_recomputed",
            "cost": "not_reported_by_cli",
            "permission_mode": (init or {}).get("permission_mode"),
            "reported_model": (init or {}).get("model"),
        }
    if errors:
        summary["result_status"] = "failed"
        summary["error"] = error_text(errors) or "AGY error event"
    elif statuses:
        terminal = statuses[-1]
        if terminal in TERMINAL_SUCCESS:
            summary["result_status"] = "completed"
        else:
            summary["result_status"] = "failed"
            summary["error"] = "AGY terminal status: " + terminal
    elif result is None:
        summary["error"] = "No AGY terminal result event was observed"
    return summary