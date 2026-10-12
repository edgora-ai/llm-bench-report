"""Deterministic entry selection for the standalone SVG delivery protocol."""
from pathlib import PurePosixPath


SVG_DELIVERY = "standalone-svg-v1"


def svg_entrypoint(names):
    candidates = sorted({name for name in names
                         if isinstance(name, str) and name not in {".", ".."}
                         and "/" not in name and "\\" not in name
                         and PurePosixPath(name).suffix.lower() == ".svg"})
    if "index.svg" in candidates:
        return "index.svg"
    return candidates[0] if len(candidates) == 1 else None
