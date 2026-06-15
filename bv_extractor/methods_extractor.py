"""
Per-analyte analytical-method extraction.

Browses the article text for sentence patterns like:

    "TG and TC were measured using an enzymatic colorimetric test method"
    "HDL-C and LDL-C were measured using homogeneous enzymatic colorimetric
     test methods"
    "Apo-A1 and apo-B were determined using an immunoturbidimetric method"

It is intentionally simple: each "X (and Y) ... [were|was] (measured|
determined|analyzed) using ... method" is mined and the captured method
phrase is mapped to every analyte name found in that sentence.

When no method sentence can be linked to an analyte, the field is left
empty and a dataset-level warning is recorded.
"""

from __future__ import annotations

import re
from typing import Dict, List, Set

from .pdf_io import Document
from .table_finder import KNOWN_ANALYTES


# ---------------------------------------------------------------------------
# Sentence patterns
# ---------------------------------------------------------------------------

# Capture group 1: comma/'and'-separated analyte tokens
# Capture group 2: the method phrase up to "method[s]" (no more than ~80 chars,
# no period or semicolon — those would mean we've crossed sentence boundaries).
_METHOD_SENTENCE_RE = re.compile(
    r"""
    \b(
        [A-Za-z][A-Za-z0-9\-/]*                       # first analyte
        (?:\s*(?:,|and)\s*[A-Za-z][A-Za-z0-9\-/]*)*   # more analytes
    )
    \s+(?:were|was)\s+(?:measured|determined|analy[sz]ed|quantified)
    \s+(?:using|with|by)\s+
    (?:an?\s+|the\s+)?
    ([^.;\n]{3,80}?)                                   # method phrase
    \s+method[s]?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_methods_by_analyte(
    doc: Document,
    known_analytes: Set[str] = None,
) -> Dict[str, str]:
    """Return {analyte_abbreviation_or_name_lower: method_phrase}.

    Only analytes in `known_analytes` (defaulting to the project-wide list)
    are considered, which avoids false matches like "points were analysed".
    The first method phrase encountered for each analyte wins.
    """
    if known_analytes is None:
        known_analytes = KNOWN_ANALYTES

    text = _flatten(doc.full_text)
    out: Dict[str, str] = {}

    for m in _METHOD_SENTENCE_RE.finditer(text):
        analytes_blob = m.group(1)
        method_phrase = m.group(2)

        analytes = _split_analyte_list(analytes_blob, known_analytes)
        if not analytes:
            continue
        method_clean = _clean_method_phrase(method_phrase)
        if not method_clean:
            continue

        for a in analytes:
            key = a.lower()
            out.setdefault(key, method_clean)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(text: str) -> str:
    """Collapse soft line-break hyphenation and normalise whitespace."""
    no_wrap = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", no_wrap)


_ANALYTE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-/]*")


def _split_analyte_list(blob: str, known: Set[str]) -> List[str]:
    """Return tokens from `blob` that are listed in `known` (lowercase)."""
    tokens = _ANALYTE_TOKEN_RE.findall(blob)
    return [t for t in tokens if t.lower() in known]


def _clean_method_phrase(phrase: str) -> str:
    """Trim, collapse whitespace and capitalise the method phrase."""
    cleaned = re.sub(r"\s+", " ", phrase).strip(" ,;:")
    cleaned = re.sub(r"^(?:an|a|the)\s+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:]
