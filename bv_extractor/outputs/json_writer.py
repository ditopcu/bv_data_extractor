"""
JSON writer for an ExtractionResult.

The output is a single JSON document with three top-level sections:

    {
      "report":   {...},        # ExtractionReport contents
      "dataset":  {...},        # DatasetDetails contents
      "analytes": [ {...}, ... ]  # one object per analyte
    }

Every numeric field is serialised with its provenance object:

    "cvi": {
        "value": 25.0,
        "source": "deterministic",
        "raw_text": "25 (22.2–28.6)",
        "warning": null
    }

This makes it trivial for downstream tools (or a human reviewer) to
distinguish "extracted with confidence" from "blank for a known reason".
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from ..schema import ExtractionResult


def write_json(
    result: ExtractionResult,
    output_path: str | Path,
    indent: int = 2,
) -> Path:
    """Serialise `result` as JSON to `output_path` and return the Path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "report":   asdict(result.report),
        "dataset":  asdict(result.dataset),
        "analytes": [asdict(a) for a in result.analytes],
    }
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=indent, ensure_ascii=False, default=_default)
    return output_path


def _default(o):
    """Fallback for any value that json doesn't natively know about."""
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Object of type {type(o)!r} is not JSON serialisable")