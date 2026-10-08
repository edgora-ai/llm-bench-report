"""Maximum reasoning effort as a frozen, recorded benchmark condition.

Effort is not a free parameter. Every route has its own vocabulary, its
ceiling differs, and several routes have no effort control at all. Sending one
hard-coded string would either be rejected or, worse, be silently ignored --
and a silent default means the comparison measures each vendor's idea of a
default rather than the model. So effort is resolved per model, recorded in
three separate fields, and never inferred after the fact.
"""

import json
from pathlib import Path

from .adapters.metrics import mapping

# "max" is the ceiling name only when the model lists it. A model whose
# vocabulary stops at "xhigh" must be asked for "xhigh", and a model with no
# effort vocabulary at all must be recorded as uncontrolled rather than
# assumed to be running at its best.
CEILING_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def effort_vocabulary(entry: dict) -> list[str]:
    """Return the ordered effort values a model entry declares.

    An entry may declare several reasoning options; only the ``effort`` typed
    one is the effort ladder. Missing or malformed data yields an empty list,
    which callers must treat as "no effort control", never as "no effort".
    """
    options = entry.get("reasoning_options")
    if not isinstance(options, list):
        return []
    values = []
    for option in options:
        option = mapping(option)
        if option.get("type") != "effort":
            continue
        declared = option.get("values")
        if isinstance(declared, list):
            values.extend(v for v in declared if isinstance(v, str) and v)
    return values


def ceiling(vocabulary: list[str]) -> str | None:
    """Return the highest declared effort level, or None when none applies.

    The ladder is ordered by ``CEILING_ORDER``, not alphabetically and not by
    position in the declaration, because declarations omit levels a provider
    skips (``low, high, max``) and the order a provider lists them in is not
    promised to be an order at all.
    """
    usable = [v for v in vocabulary if v in CEILING_ORDER]
    return max(usable, key=CEILING_ORDER.index) if usable else None


def lookup(catalogue: dict, model: str) -> dict:
    """Find one model's entry by provider-qualified or bare model ID.

    Accepts the same shapes ``discover_free`` does: provider-keyed snapshots
    (the real ``{"opencode": {"models": ...}}`` file) and bare ``{"models": ...}``
    ones. Claude Code is given bare model IDs while the catalogue is
    provider-keyed, so a bare lookup is required; a bare ID matching several
    providers is reported as ambiguous rather than guessed.
    """
    data = mapping(catalogue)
    models = data.get("models")
    if models is None:
        # The frozen batch catalogue is provider-keyed; fall back to the
        # provider that owns these routes rather than reading it as empty.
        providers = mapping(data.get("provider", data.get("providers", data)))
        for provider in ("opencode", "anthropic", "custom"):
            candidate = mapping(providers.get(provider)).get("models")
            if candidate is not None:
                models = candidate
                break
    if isinstance(models, dict):
        items = list(models.items())
    elif isinstance(models, list):
        items = [(mapping(m).get("id"), m) for m in models]
    else:
        return {"status": "catalogue_unreadable"}

    bare = model.split("/", 1)[1] if model.startswith("opencode/") else model
    matches = [mapping(value) for key, value in items if key == model or key == bare]
    if not matches:
        return {"status": "not_in_catalogue"}
    if len(matches) > 1 and any(mapping(v).get("id", k) != matches[0].get("id", k)
                                 for k, v in items if k in (model, bare)):
        return {"status": "ambiguous_in_catalogue"}
    entry = matches[0]
    vocabulary = effort_vocabulary(entry)
    return {
        "status": "declared" if vocabulary else "no_effort_vocabulary",
        "vocabulary": vocabulary,
        "ceiling": ceiling(vocabulary),
        "reasoning_declared": entry.get("reasoning") is True,
    }


def resolve(catalogue: dict, model: str, *, allowed: list[str]) -> dict:
    """Decide the effort argument for one run.

    Returns the argument to pass plus the record of how it was chosen. A
    requested level absent from the model's own vocabulary is downgraded to
    that model's ceiling rather than sent as a string the route will reject,
    and the downgrade is recorded so the archive never claims effort it did
    not get. An empty ``allowed`` disables the control and is recorded as such
    -- it must never fall through to whatever a vendor defaults to.
    """
    if not allowed:
        return {"requested": None, "applied": None, "supported": None,
                "control": "disabled_by_operator", "ceiling": None}
    found = lookup(catalogue, model)
    record = {
        "requested": max(allowed, key=CEILING_ORDER.index),
        "supported": found.get("vocabulary") or None,
        "applied": None,
        "control": found["status"],
        "ceiling": found.get("ceiling"),
    }
    if found["status"] == "declared" and record["ceiling"]:
        record["applied"] = record["ceiling"]
        record["downgraded_from"] = (record["requested"] if record["ceiling"] != record["requested"] else None)
    elif found["status"] == "no_effort_vocabulary" and found.get("reasoning_declared"):
        # The model reasons, but exposes no effort ladder. Effort is fixed
        # inside the model; saying "unsupported" is the honest record.
        record["control"] = "fixed_unspecified_effort"
    return record


def read_catalogue(path: Path) -> dict:
    """Read a models.json snapshot; unreadable input must not fake control."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def condition_fields(record: dict) -> dict:
    """Flatten an effort record into manifest condition fields."""
    return {
        "reasoning_effort_requested": record["requested"],
        "reasoning_effort_applied": record["applied"],
        "reasoning_effort_supported": record["supported"],
        "reasoning_effort_control": record["control"],
    }
