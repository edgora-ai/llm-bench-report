"""Small, lossless helpers shared by the two CLI event adapters."""

import json
import math
from collections.abc import Mapping


METRIC_KEYS = (
    "input_tokens", "input_total", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens", "total_tokens", "cost_usd",
    "cost_source", "api_duration_ms", "num_turns",
)


def empty_summary() -> dict:
    return {
        "metrics": dict.fromkeys(METRIC_KEYS),
        "reported_models": [],
        "final_text": "",
        "result_status": "unknown",
        "error": None,
        "usage_raw": None,
    }


def mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value >= 0:
            return value
    return None


def complete_sum(values):
    """Do not turn absent measurements into zero or report partial totals."""
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def input_total(noncache, read, write):
    # Omitted optional cache counters are zero only with measured input.
    if noncache is None:
        return None
    return noncache + (read if read is not None else 0) + (write if write is not None else 0)


def add_model(summary: dict, model):
    if isinstance(model, str) and model and model not in summary["reported_models"]:
        summary["reported_models"].append(model)


def error_text(value):
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        parts = [error_text(item) for item in value]
        return "\n".join(part for part in parts if part) or None
    if isinstance(value, Mapping):
        data = mapping(value.get("data"))
        return (error_text(data.get("message")) or error_text(value.get("message"))
                or error_text(value.get("name")) or json.dumps(value, ensure_ascii=False))
    if value is not None:
        return str(value)
    return None
