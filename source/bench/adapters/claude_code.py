"""Claude Code 2.1.285 command and stream-json result normalization.

The caller supplies stdin and an isolated container environment; this module
never reads configuration, credentials, or invokes a CLI.
"""

import json

from .metrics import (
    add_model, complete_sum, empty_summary, error_text, input_total, mapping, number,
)

TOOLS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep")
# The levels claude --help documents for --effort. A level outside this set is
# rejected here rather than passed through to fail inside the session.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DENIED_TOOLS = (
    "Agent", "Task", "TaskOutput", "TaskStop", "WebFetch", "WebSearch",
    "Skill", "ToolSearch", "mcp__*",
)


def command(model: str, effort: str | None = None) -> list[str]:
    if not isinstance(model, str) or not model.strip() or model.startswith("-"):
        raise ValueError("model must be a nonempty model ID")
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ValueError(f"unsupported effort level: {effort!r}")
    settings = {
        "disableAllHooks": True,
        "enableAllProjectMcpServers": False,
        "permissions": {"allow": list(TOOLS), "deny": list(DENIED_TOOLS),
                        "defaultMode": "dontAsk"},
        # The runner supplies the OS sandbox (isolated Docker). Nested bwrap
        # is unavailable in that runtime; this does not bypass tool permissions.
        "sandbox": {"enabled": False, "autoAllowBashIfSandboxed": True,
                    "allowUnsandboxedCommands": False},
    }
    command = [
        "/usr/local/bin/claude", "-p", "--model", model,
        "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--safe-mode", "--restricted", "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
        "--tools", ",".join(TOOLS), "--allowedTools", ",".join(TOOLS),
        "--permission-mode", "dontAsk", "--settings",
        json.dumps(settings, separators=(",", ":")),
    ]
    if effort is not None:
        # Maximum effort is a benchmark condition: without it the run measures
        # the vendor's default tier instead of the model.
        command += ["--effort", effort]
    return command


def _usage_metrics(usage: dict) -> dict:
    inp = number(usage.get("input_tokens"))
    out = number(usage.get("output_tokens"))
    read = number(usage.get("cache_read_input_tokens"))
    write = number(usage.get("cache_creation_input_tokens"))
    total_input = input_total(inp, read, write)
    return {
        "input_tokens": inp, "input_total": total_input, "output_tokens": out,
        "cache_read_tokens": read, "cache_write_tokens": write,
        "reasoning_tokens": number(usage.get("reasoning_tokens")),
        "total_tokens": complete_sum([total_input, out]),
    }


def summarize(events: list[dict]) -> dict:
    summary = empty_summary()
    assistants = []
    result = None
    stream_errors = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            message = mapping(event.get("message"))
            assistants.append(message)
            add_model(summary, message.get("model"))
            content = message.get("content")
            if isinstance(content, list):
                text = "".join(block.get("text", "") for block in content
                               if isinstance(block, dict) and block.get("type") == "text"
                               and isinstance(block.get("text"), str))
                if text:
                    summary["final_text"] = text
            if event.get("error"):
                stream_errors.append(event["error"])
        elif event.get("type") == "result":
            result = event
            for model in mapping(event.get("modelUsage")):
                add_model(summary, model)
        elif event.get("type") == "error":
            stream_errors.append(event.get("error", event.get("message")))

    # A result is cumulative and authoritative, even when its usage is empty.
    # Without it, the last assistant's usage is only a partial observation.
    usage = mapping(result.get("usage")) if result is not None else (
        mapping(assistants[-1].get("usage")) if assistants else {})
    summary["metrics"].update(_usage_metrics(usage))
    if result is not None:
        metrics = summary["metrics"]
        metrics["cost_usd"] = number(result.get("total_cost_usd"))
        if metrics["cost_usd"] is not None:
            metrics["cost_source"] = "cli_reported_unverified"
        metrics["api_duration_ms"] = number(result.get("duration_api_ms"))
        metrics["num_turns"] = number(result.get("num_turns"))
        if isinstance(result.get("result"), str):
            summary["final_text"] = result["result"]
        subtype = result.get("subtype")
        failed = bool(result.get("is_error")) or (
            isinstance(subtype, str) and subtype.startswith("error"))
        if failed:
            summary["result_status"] = "failed"
            summary["error"] = (error_text(result.get("errors"))
                                or error_text(result.get("error"))
                                or error_text(result.get("result"))
                                or error_text(subtype) or "Claude Code result failed")
        elif subtype == "success" or result.get("is_error") is False:
            summary["result_status"] = "completed"
    if stream_errors:
        summary["result_status"] = "failed"
        summary["error"] = summary["error"] or error_text(stream_errors)
    if result is not None or assistants:
        summary["usage_raw"] = {
            "usage": result.get("usage") if result is not None else usage,
            "modelUsage": result.get("modelUsage") if result is not None else None,
            "assistant_usage": [message.get("usage") for message in assistants],
            "input_semantics": "input_tokens_excludes_cache; input_total_includes_cache",
            "reasoning_semantics": "not_added_to_output_or_total",
            "scope": "cumulative_result" if result is not None else "last_assistant_partial",
            "duration_ms": result.get("duration_ms") if result is not None else None,
            "subtype": result.get("subtype") if result is not None else None,
            "is_error": result.get("is_error") if result is not None else None,
            "permission_denials": result.get("permission_denials") if result is not None else None,
        }
    return summary
