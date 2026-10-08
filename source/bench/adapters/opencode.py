"""OpenCode 1.18.33 JSON-event adapter; no CLI/configuration side effects.

Verified Session.getUsage normalizes part.tokens.input to NON-cache input.
Provider cache counters are subsets of original input, not of this normalized
field. part.tokens.total, when supplied, is the authoritative provider total.
"""

from .metrics import (
    add_model, complete_sum, empty_summary, error_text, input_total, mapping, number,
)


def command(model: str, effort: str | None = None) -> list[str]:
    if (not isinstance(model, str) or model.startswith("-")
            or "/" not in model or any(not piece.strip() for piece in model.split("/", 1))):
        raise ValueError("OpenCode model must be provider/model")
    # The main runner injects isolated config/permissions/environment and stdin.
    # A non-default manual title suppresses the automatic title-model request.
    command = ["/usr/local/bin/opencode", "run", "--model", model, "--format", "json",
               "--dir", "/workspace/output", "--pure", "--title", "llm-bench-fixed-session"]
    if effort is not None:
        # OpenCode spells the effort level "variant"; run --help documents it as
        # "model variant (provider-specific reasoning effort ...)".
        command += ["--variant", effort]
    return command


def _models(summary, value):
    if isinstance(value, str):
        add_model(summary, value)
        return
    value = mapping(value)
    model = value.get("modelID")
    provider = value.get("providerID")
    if isinstance(model, str):
        add_model(summary, f"{provider}/{model}" if isinstance(provider, str) else model)


def _step_metrics(part):
    tokens = mapping(part.get("tokens"))
    cache = mapping(tokens.get("cache"))
    inp = number(tokens.get("input"))
    out = number(tokens.get("output"))
    read = number(cache.get("read"))
    write = number(cache.get("write"))
    total_input = input_total(inp, read, write)
    total = number(tokens.get("total"))
    if total is None:
        # Never add reasoning on top of output; preserve the original counters.
        total = complete_sum([total_input, out])
    return {
        "input_tokens": inp, "input_total": total_input, "output_tokens": out,
        "cache_read_tokens": read, "cache_write_tokens": write,
        "reasoning_tokens": number(tokens.get("reasoning")),
        "total_tokens": total, "cost_usd": number(part.get("cost")),
    }


def summarize(events: list[dict]) -> dict:
    summary = empty_summary()
    steps = []
    seen_steps = set()
    text_parts = {}
    errors = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        part = mapping(event.get("part"))
        _models(summary, event.get("model"))
        _models(summary, part.get("model"))
        _models(summary, part)
        info = mapping(event.get("info"))
        _models(summary, info)
        kind = event.get("type")
        if kind == "step_finish":
            identity = ((event.get("sessionID"), "id", part["id"]) if part.get("id") else
                        (event.get("sessionID"), "index", part.get("messageID"),
                         part.get("index", event.get("index", index))))
            if identity in seen_steps:
                continue
            seen_steps.add(identity)
            steps.append(part)
        elif kind == "text" and isinstance(part.get("text"), str):
            identity = ((event.get("sessionID"), part["id"]) if part.get("id") else
                        (event.get("sessionID"), part.get("messageID"), index))
            # Repeated part updates replace, not append, their final snapshot.
            text_parts[identity] = part["text"]
        elif kind == "error":
            errors.append(event.get("error", event.get("message")))
        elif kind in ("report", "result"):
            if event.get("error") or event.get("is_error"):
                errors.append(event.get("error") or event.get("message") or "OpenCode report failed")
    summary["final_text"] = "\n".join(text_parts.values())
    if steps:
        measured = [_step_metrics(step) for step in steps]
        for key in measured[0]:
            summary["metrics"][key] = complete_sum(item[key] for item in measured)
        if summary["metrics"]["cost_usd"] is not None:
            summary["metrics"]["cost_source"] = "cli_reported_unverified"
        summary["metrics"]["num_turns"] = len(steps)
        last_reason = steps[-1].get("reason")
        if last_reason in ("stop", "end_turn"):
            summary["result_status"] = "completed"
        elif last_reason in ("error", "content-filter"):
            summary["result_status"] = "failed"
            summary["error"] = f"OpenCode step finish: {last_reason}"
        summary["usage_raw"] = {
            "steps": steps,
            "input_semantics": "part.tokens.input_excludes_cache; input_total_includes_cache",
            "cache_semantics": "subset_of_provider_input_total_not_normalized_input",
            "total_semantics": "prefer_part.tokens.total; otherwise_input_total_plus_output",
            "reasoning_semantics": "not_added_to_output_or_total",
        }
    if errors:
        summary["result_status"] = "failed"
        summary["error"] = error_text(errors) or "OpenCode error event"
    return summary
