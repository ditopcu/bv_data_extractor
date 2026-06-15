"""
Locate the table that contains the biological variation results.

Strategy: scan every page (rotated or not) for a dense cluster of words
that together signal a BV results table, namely:
  - column-header tokens: "CV_A", "CV_I", "CV_G", "Mean±SD", "95% CI"
  - row-anchor tokens: at least two recognised analyte names

Returns the page index, an "is_rotated" flag, and the un-rotated word list
ready for downstream geometric parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Set

from .pdf_io import Document, PageContent, Word
from .rotation import UnrotatedWord, has_rotated_text, unrotate_words


# ---------------------------------------------------------------------------
# Identification heuristics
# ---------------------------------------------------------------------------

# Words/fragments that strongly indicate a BV results table
BV_HEADER_TOKENS = {
    "cv", "cva", "cvi", "cvg",
    "mean", "sd",
    "ci", "cis",
    "biological", "variation",
    "within-subject", "between-subject",
    "analyte",
    "diurnal", "long-term",
}

# Common analyte abbreviations / names (case-insensitive). Extend as needed.
KNOWN_ANALYTES = {
    "tg", "tc", "ldl-c", "hdl-c", "apo-a1", "apo-b",
    "triglyceride", "cholesterol",
    "creatinine", "urea", "glucose",
    "alt", "ast", "alp", "ggt",
    "albumin", "bilirubin",
    "sodium", "potassium", "chloride",
    "calcium", "magnesium", "phosphate",
    "hemoglobin", "haemoglobin", "hba1c",
    "crp", "ferritin",
    "tsh", "ft4", "ft3",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class TableLocation:
    """Where the BV results table lives in the document."""
    page_index: int                 # 0-based
    is_rotated: bool
    words: List[UnrotatedWord]      # un-rotated if rotated, plain otherwise
    label: Optional[str] = None     # e.g. "Table 4"
    score: float = 0.0              # heuristic confidence score


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_bv_table(doc: Document) -> Optional[TableLocation]:
    """Identify the page most likely to contain the primary BV results table.

    The function returns at most one location. If the page contains rotated
    text the words are returned in un-rotated form so that downstream code
    can work in a single coordinate frame.

    The heuristic score combines: number of BV header tokens present,
    number of distinct analyte names found, and whether the page also
    contains a "Table N" label — but no individual signal is required.
    """
    candidates: List[TableLocation] = []

    for page in doc.pages:
        loc = _score_page(page)
        if loc is not None:
            candidates.append(loc)

    if not candidates:
        return None

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _score_page(page: PageContent) -> Optional[TableLocation]:
    """Compute a heuristic score for one page.

    Words are taken in their natural form (un-rotated if the page has a
    rotated table). The score is a weighted sum of header-token hits,
    distinct analyte hits, and a small bonus for a 'Table N' label.
    """
    rotated = has_rotated_text(page)
    if rotated:
        words: List[UnrotatedWord] = unrotate_words(page)
        texts = [w.text for w in words]
    else:
        # For upright pages, wrap raw words in a thin shim so the rest of
        # the pipeline can use the same `oy0/ox0` field names.
        words = [_to_unrotated(w, page) for w in page.words if w.upright]
        texts = [w.text for w in words]

    if not texts:
        return None

    lower_texts = [t.lower() for t in texts]
    header_hits = sum(1 for t in lower_texts if t in BV_HEADER_TOKENS)
    analyte_hits: Set[str] = {
        t.lower() for t in lower_texts if t.lower() in KNOWN_ANALYTES
    }

    # Find a "Table N" label by physical adjacency, not by reading order.
    label = None
    table_words = [w for w, t in zip(words, lower_texts) if t == "table"]
    for tw in table_words:
        # Look for a digit token within ~25 units of `tw` (just to its
        # right or sharing its row) — typical figure-label spacing.
        for cand in words:
            t = cand.text.rstrip(".:")
            if not t.isdigit():
                continue
            if (abs(cand.oy_center - tw.oy_center) < 8.0 and
                    0 < cand.ox0 - tw.ox1 < 25.0):
                label = f"Table {t}"
                break
        if label:
            break

    score = header_hits + 2.0 * len(analyte_hits) + (1.0 if label else 0.0)
    if score < 5.0:
        return None  # too weak to be a BV table

    return TableLocation(
        page_index=page.page_index,
        is_rotated=rotated,
        words=words,
        label=label,
        score=score,
    )


def _to_unrotated(w: Word, page: PageContent) -> UnrotatedWord:
    """Wrap an upright word as if it had been un-rotated (identity map)."""
    return UnrotatedWord(
        text=w.text,
        ox0=w.x0,
        ox1=w.x1,
        oy0=w.top,
        oy1=w.bottom,
        page_index=w.page_index,
        raw=w,
    )
