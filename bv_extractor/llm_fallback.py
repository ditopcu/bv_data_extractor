"""
LLM-assisted fallback (architectural placeholder).

When the deterministic pipeline leaves fields blank, this module is the
designated place to prompt an LLM for help. v1 ships only the architecture:

  * `request_fallback(blank_fields, context_text)` : a no-op that records
    a note explaining that LLM help was requested but not invoked.

Future work
-----------
A working implementation should:

  1. Accept the list of blank-field tags (e.g. "apo-B.cvi_ci_lower") and
     a snippet of relevant article text or table cell.
  2. Construct a prompt that asks the model for one numeric value per
     field, with explicit "leave blank if not reported" instructions.
  3. Parse the LLM response, validate each value against the field's
     expected type and range, and merge it back into the AnalyteRecord
     with `source='llm'` so downstream consumers can distinguish.
  4. Never overwrite a deterministic value with an LLM value.

Cost discipline
---------------
Always pass the *minimum* useful context (a few lines of article text
plus the blank-field list), never the whole PDF.
"""

from __future__ import annotations

from typing import List


def request_fallback(blank_field_tags: List[str], context_text: str) -> dict:
    """Return a dict mapping field tags to filled values.

    v1 implementation: returns an empty dict and signals that no LLM
    help was actually requested.
    """
    # Intentionally empty in v1. The CLI flag --use-llm wires this in
    # but the function makes no network calls.
    _ = blank_field_tags, context_text
    return {}