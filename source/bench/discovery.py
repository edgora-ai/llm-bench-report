"""Offline model candidates from an explicitly supplied models.json snapshot."""

import json
from pathlib import Path

from .adapters.metrics import mapping, number

CLAUDE_MODELS = [
    "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-6-luna",
    "deepseek-flash", "glm-5.3-flash", "kimi-k3-256k",
]
# Operator-excluded routes. Excluded runs are still archivable if started, but
# they are never added to a new batch's queue.
EXCLUDED_CLAUDE_MODELS = {
    "gpt-6-astra",     # withdrawn from the comparison set on 2026-09-30
    "glm-5.3-flash",   # operator-confirmed skip: 1 turn, 0 tokens, 0 bytes, exit 1 on both tasks
}
# Routes seen to produce nothing, recorded so the observation is not re-derived
# from scratch. This is deliberately NOT a filter:
# ``[claude-code:unrecognized_model]`` alone does not prove a route is dead --
# kimi-k3-256k emits that same stderr line and then completes normally (5 and
# 15 turns, 16KB and 22KB of output). Only an actual empty result does, and
# removing a model from the comparison set stays an operator decision.
OBSERVED_EMPTY_CLAUDE_MODELS = {
    "glm-5.3-flash": "1 turn, 0 output tokens, no output file, exit 1, on both tasks, 2026-09-30",
}


def claude_model_queue():
    """Models a new batch runs, in declared order."""
    return [model for model in CLAUDE_MODELS
            if model not in EXCLUDED_CLAUDE_MODELS]


def discover_free(cache_path: Path) -> list[dict]:
    """Return metadata-free candidates, not verified callable models.

    Accept the models.dev provider-keyed cache and snapshots wrapped in
    ``provider`` or ``providers``. Missing/invalid files raise instead of
    silently claiming no candidates. This function never refreshes the cache.
    """
    data = mapping(json.loads(Path(cache_path).read_text(encoding="utf-8")))
    providers = mapping(data.get("provider", data.get("providers", data)))
    provider = mapping(providers.get("opencode"))
    models = provider.get("models", {})
    if isinstance(models, dict):
        entries = models.items()
    elif isinstance(models, list):
        entries = ((mapping(model).get("id"), model) for model in models)
    else:
        raise ValueError("opencode.models must be a mapping or list")
    candidates = []
    for key, value in entries:
        model = mapping(value)
        model_id = model.get("id", key)
        if not isinstance(model_id, str) or not model_id:
            continue
        if model.get("deprecated") or model.get("status") == "deprecated":
            continue
        cost = mapping(model.get("cost"))
        if number(cost.get("input")) != 0 or number(cost.get("output")) != 0:
            continue
        if any(field in cost and number(cost[field]) != 0
               for field in ("cache_read", "cache_write")):
            continue
        candidates.append({
            "id": f"opencode/{model_id}", "model_id": model_id, "provider": "opencode",
            "label": model.get("name") or model_id, "cost": cost,
            "limit": model.get("limit"), "source": "models.json",
            "status": "candidate",
        })
    return sorted(candidates, key=lambda model: model["id"])
