"""
Pre-analysis: build a structured profile of a PDF article before parsing.

This module is the single source of truth for "what kind of BV article is
this and how should we extract it?" It produces an ``ArticleProfile``
which the pipeline uses to pick the right parser strategy.

Three orthogonal classifications are produced:

1. **Verdict** — is this article worth extracting at all?
   (``LIKELY_EXTRACTABLE``, ``NON_STANDARD_FORMAT``, ``NOT_A_BV_TABLE``,
   ``NO_TABLE_FOUND``).

2. **Format class** — how does the candidate table report its numbers?
   (``mean_sd``, ``value_ci``, ``paren_sd``, ``hybrid``, ``unknown``).

3. **Geometry class** — how is the table physically laid out?
   (``upright``, ``rotated_ccw``, ``rotated_cw``, ``unknown``).

These three dimensions are independent: a paper can be ``LIKELY_EXTRACTABLE``
in ``hybrid`` format with ``rotated_ccw`` geometry (Yang 2018), or
``LIKELY_EXTRACTABLE`` in ``value_ci`` format with ``upright`` geometry
(typical modern paper).

Design notes
------------
* The classifications are deterministic heuristics. An LLM fallback hook
  is reserved in ``ArticleProfile.classification_source`` but not yet
  wired in; that is a separate future module.
* The detection works on the *candidate BV table page* identified by
  ``table_finder.find_bv_table``. Document-wide pattern counts (used for
  the verdict) are kept separate from the candidate-page format class.
* Format detection uses regex on the page's reading-order text, not on
  the whole document, because a paper may have non-BV demographic
  tables that use ``value (CI)`` formatting and would otherwise mislead
  the classifier.

The legacy ``diagnose.py`` module is now a thin reporting wrapper around
this module; ``FileDiagnostic`` continues to work but new code should
prefer ``ArticleProfile``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .pdf_io import Document, PageContent, load_pdf
from .rotation import has_rotated_text, unrotate_words
from .table_finder import (
    BV_HEADER_TOKENS,
    KNOWN_ANALYTES,
    TableLocation,
    find_bv_table,
)


# ---------------------------------------------------------------------------
# Verdict labels and thresholds
# ---------------------------------------------------------------------------

VERDICT_EXTRACTABLE = "LIKELY_EXTRACTABLE"
VERDICT_NON_STANDARD = "NON_STANDARD_FORMAT"
VERDICT_NOT_BV = "NOT_A_BV_TABLE"
VERDICT_NO_TABLE = "NO_TABLE_FOUND"

VERDICT_DESCRIPTIONS = {
    VERDICT_EXTRACTABLE:
        "A BV results table was found and the article uses a recognised "
        "numeric reporting format. Full extraction is likely to succeed.",
    VERDICT_NON_STANDARD:
        "A BV-like table was found but the reporting format is not "
        "standard. The full extractor may return partial or empty "
        "results — manual review recommended before relying on the "
        "output.",
    VERDICT_NOT_BV:
        "No table on this page meets the minimum BV-table score. The "
        "article probably does not contain biological variation results "
        "in a recognisable form.",
    VERDICT_NO_TABLE:
        "No candidate table was identified anywhere in the document.",
}

_MIN_EXTRACTABLE_SCORE = 30.0
_MIN_TABLE_SCORE = 5.0
_MIN_PATTERN_MATCHES = 5


# ---------------------------------------------------------------------------
# Format class labels
# ---------------------------------------------------------------------------

FORMAT_MEAN_SD = "mean_sd"        # e.g. "3.99±0.58"
FORMAT_VALUE_CI = "value_ci"      # e.g. "18.7 (16.6–20.9)" or "(16.6, 20.9)"
FORMAT_PAREN_SD = "paren_sd"      # e.g. "3076(218)" — SD in parens, no ±
FORMAT_HYBRID = "hybrid"          # mix of mean_sd and value_ci on same page
FORMAT_UNKNOWN = "unknown"        # nothing matched

FORMAT_DESCRIPTIONS = {
    FORMAT_MEAN_SD:
        "Numbers reported as 'mean ± SD' (with explicit ± symbol).",
    FORMAT_VALUE_CI:
        "Numbers reported as 'value (low–high)' with a 95% CI in "
        "parentheses, separated by en-dash, hyphen, or comma.",
    FORMAT_PAREN_SD:
        "Numbers reported as 'value(SD)' with the SD in parentheses but "
        "no ± symbol — a non-standard pre-modern convention.",
    FORMAT_HYBRID:
        "Mixed formats on the same page: some columns use mean±SD, "
        "others use value (CI). Typical of well-instrumented modern BV "
        "papers (e.g. Yang 2018).",
    FORMAT_UNKNOWN:
        "No recognised numeric reporting format detected on the "
        "candidate page.",
}


# ---------------------------------------------------------------------------
# Geometry class labels
# ---------------------------------------------------------------------------

GEOMETRY_UPRIGHT = "upright"
GEOMETRY_ROTATED_CCW = "rotated_ccw"   # 90° counter-clockwise (Yang Table 4)
GEOMETRY_ROTATED_CW = "rotated_cw"     # 90° clockwise (not yet seen)
GEOMETRY_UNKNOWN = "unknown"

GEOMETRY_DESCRIPTIONS = {
    GEOMETRY_UPRIGHT:
        "Table is printed upright on its page.",
    GEOMETRY_ROTATED_CCW:
        "Table is rotated 90° counter-clockwise (sideways) on a portrait "
        "page; coordinates need to be un-rotated before parsing.",
    GEOMETRY_ROTATED_CW:
        "Table is rotated 90° clockwise on a portrait page. Detection "
        "supported but un-rotation is not yet implemented in v1.",
    GEOMETRY_UNKNOWN:
        "Table geometry could not be determined.",
}


# ---------------------------------------------------------------------------
# Format-detection regexes (compiled once)
# ---------------------------------------------------------------------------
#
# These regexes operate on individual lines of the candidate page's
# reading-order text. They are deliberately **looser** than the strict
# parse_* functions in patterns.py: their job here is to *detect* a
# format's presence, not to extract field values. The strict parsers are
# used downstream by the format-specific row parsers.

# mean_sd: "12.34±5.67" or "12.34 ± 5.67". The ± is the discriminator.
_RE_MEAN_SD = re.compile(
    r"\d+\.?\d*\s*[±]\s*\d+\.?\d*"
)

# value_ci: "12.34 (10.5, 14.2)" or "12.34 (10.5–14.2)" or "(10.5-14.2)".
# Allows comma, en-dash (U+2013), em-dash (U+2014), or hyphen as separator.
_RE_VALUE_CI = re.compile(
    r"\d+\.?\d*\s*\(\s*\d+\.?\d*\s*[,\u2013\u2014\-]\s*\d+\.?\d*\s*\)"
)

# paren_sd: "3076(218)" or "12.5 (1.4)" — value followed by a single
# parenthesised number with NO comma/dash inside. We exclude lines that
# also match value_ci on the same span by requiring a single number
# between the parens.
_RE_PAREN_SD = re.compile(
    r"\d+\.?\d*\s*\(\s*\d+\.?\d*\s*\)"
)

# A standalone number (integer or decimal). Used to gauge how "tabular"
# (dense numeric) a page is when no inline format is detected — the signal
# for relocating to a plain-column results table.
_RE_NUMERIC = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


# ---------------------------------------------------------------------------
# Article profile dataclass
# ---------------------------------------------------------------------------

@dataclass
class ArticleProfile:
    """Structured profile produced by pre-analysis.

    Downstream code (pipeline, parsers) should treat this as the single
    source of truth about what the PDF contains and how to handle it.
    """
    # Source
    path: Path
    page_count: int

    # Verdict (overall extractability)
    verdict: str = VERDICT_NO_TABLE

    # Format and geometry
    format_class: str = FORMAT_UNKNOWN
    geometry_class: str = GEOMETRY_UNKNOWN

    # Format-detection raw counts on the candidate page only
    candidate_mean_sd_hits: int = 0
    candidate_value_ci_hits: int = 0
    candidate_paren_sd_hits: int = 0

    # Document-wide pattern counts (used for the verdict, kept for the
    # report and for back-compat with the diagnose module)
    doc_mean_sd_match_count: int = 0
    doc_estimate_ci_match_count: int = 0

    # Candidate table location (None if no table was found)
    primary_table_label: Optional[str] = None
    primary_table_page: Optional[int] = None
    primary_table_score: float = 0.0
    has_rotation: bool = False

    # If the BV-table page was reassigned by smart relocation (see
    # _maybe_relocate_table), these record the original choice so the
    # report can explain what happened. Both are None when no
    # relocation occurred.
    relocated_from_page: Optional[int] = None
    relocation_reason: Optional[str] = None

    # Analytes recognised anywhere in the document
    all_analytes: List[str] = field(default_factory=list)

    # How the classification was produced. Currently always
    # "heuristic"; reserved for an LLM fallback in a future iteration.
    classification_source: str = "heuristic"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(pdf_path: Path) -> ArticleProfile:
    """Run the full pre-analysis pipeline on a PDF and return its profile.

    This is the function downstream code (pipeline, batch processors)
    should call. It is deterministic and side-effect-free apart from
    reading the PDF.
    """
    doc = load_pdf(pdf_path)
    profile = ArticleProfile(path=pdf_path, page_count=len(doc.pages))

    # Document-wide pattern counts (also used for the verdict)
    profile.doc_mean_sd_match_count = _count_doc_mean_sd(doc)
    profile.doc_estimate_ci_match_count = _count_doc_value_ci(doc)
    profile.has_rotation = any(has_rotated_text(p) for p in doc.pages)

    # Document-wide analyte set (uses same lower-case match as table_finder)
    profile.all_analytes = sorted(_collect_analytes(doc))

    # Locate the candidate BV table. table_finder uses keyword/analyte
    # heuristics, which can pick a discussion page that talks about BV
    # rather than the actual results table. _maybe_relocate_table
    # consults format-hit density across all pages and overrides the
    # choice when there's clear evidence the data lives elsewhere.
    table_loc = find_bv_table(doc)
    if table_loc is not None:
        original_page = table_loc.page_index + 1
        table_loc = _maybe_relocate_table(doc, table_loc, profile)
        if table_loc.page_index + 1 != original_page:
            profile.relocated_from_page = original_page

        profile.primary_table_label = table_loc.label
        profile.primary_table_page = table_loc.page_index + 1
        profile.primary_table_score = table_loc.score
        profile.geometry_class = _classify_geometry(table_loc)
        profile.format_class = _classify_format_on_page(
            doc.pages[table_loc.page_index], profile
        )

    profile.verdict = _compute_verdict(profile)
    return profile


# ---------------------------------------------------------------------------
# Smart table relocation
# ---------------------------------------------------------------------------

def _maybe_relocate_table(
    doc: Document, table_loc: TableLocation, profile: ArticleProfile
) -> TableLocation:
    """Override table_finder's choice when format-hit density disagrees.

    Rationale: ``table_finder`` ranks by keyword/analyte density and
    label proximity, but a paper's *discussion section* often hits
    those signals while the actual numeric data lives on a different
    page. Pages with dense numeric formats (mean±SD or value (CI))
    are very likely the real results table.

    Conservative override policy:

    * The candidate page must have **zero** format hits (mean_sd
      and value_ci both 0). If the candidate has any format
      density, we trust ``table_finder`` and don't touch it.
    * Some other page must have **at least 5** format hits.
    * Among qualifying pages, pick the one with the highest total
      format hits. Ties are broken by ``table_finder`` score.

    These thresholds are deliberately strict so we only override
    obvious mistakes; well-formed papers like Yang and 647 are
    untouched (a format-rich candidate can never be dominated).

    Two passes:

    * **Pass 1 — inline-format dominance:** relocate to the page whose
      mean_sd/value_ci hit count clearly beats the candidate's (absolute
      floor + dominance ratio). This generalises the old "candidate has 0
      hits" rule so a couple of stray hits on the wrong page no longer
      block relocation.
    * **Pass 2 — tabular numeric density:** when no page has real inline
      format, relocate to the densest *numeric* page that also carries a
      BV signal (keyword/analyte). This points plain-column results tables
      (which the regex can't read) at the right page for the picker / Claude.
    """
    stats = []
    for page in doc.pages:
        lines = _page_lines(page)
        msd = sum(len(_RE_MEAN_SD.findall(ln)) for ln in lines)
        vci = sum(len(_RE_VALUE_CI.findall(ln)) for ln in lines)
        paren = sum(
            len(_RE_PAREN_SD.findall(ln)) for ln in lines
            if not _RE_VALUE_CI.search(ln)
        )
        nums = sum(len(_RE_NUMERIC.findall(ln)) for ln in lines)
        stats.append({
            "idx": page.page_index,
            "msd": msd, "vci": vci, "fmt": msd + vci, "paren": paren,
            "nums": nums, "kw": _page_has_bv_signal(lines),
        })

    cand_idx = table_loc.page_index
    cand = next((s for s in stats if s["idx"] == cand_idx), None)
    others = [s for s in stats if s["idx"] != cand_idx]
    if cand is None or not others:
        return table_loc

    # --- Pass 1: inline-format dominance --------------------------------
    # Only relocate away from a *weak* candidate. A format-rich candidate
    # (e.g. Yang's rotated Table 4) is a real results table and must never be
    # moved, even if another page happens to have more hits.
    FORMAT_ABS_MIN, FORMAT_RATIO, CAND_WEAK = 5, 3, 5
    best_fmt = max(others, key=lambda s: s["fmt"])
    if (cand["fmt"] < CAND_WEAK
            and best_fmt["fmt"] >= FORMAT_ABS_MIN
            and best_fmt["fmt"] > cand["fmt"]
            and best_fmt["fmt"] >= cand["fmt"] * FORMAT_RATIO):
        reason = (
            f"page {best_fmt['idx'] + 1} has {best_fmt['fmt']} format hits "
            f"(mean_sd={best_fmt['msd']}, value_ci={best_fmt['vci']}) vs "
            f"{cand['fmt']} on the candidate"
        )
        return _build_relocated(
            doc, best_fmt["idx"], table_loc, profile, cand_idx, reason,
            bonus=min(best_fmt["fmt"], 25),
        )

    # --- Pass 2: tabular numeric density (plain-column tables) -----------
    # Only when the candidate has NO recognised inline format at all
    # (incl. paren_sd) — otherwise we'd move away from a real table.
    if cand["fmt"] < 3 and cand["paren"] < 3:
        NUMERIC_ABS_MIN, NUMERIC_RATIO = 40, 2
        numeric_pages = [
            s for s in others if s["kw"] and s["nums"] >= NUMERIC_ABS_MIN
        ]
        if numeric_pages:
            best_num = max(numeric_pages, key=lambda s: s["nums"])
            if (best_num["nums"] > cand["nums"]
                    and best_num["nums"] >= max(
                        NUMERIC_ABS_MIN, cand["nums"] * NUMERIC_RATIO)):
                reason = (
                    f"plain-column relocation: page {best_num['idx'] + 1} has "
                    f"{best_num['nums']} numeric tokens vs {cand['nums']} on "
                    f"the candidate (no inline format detected)"
                )
                return _build_relocated(
                    doc, best_num["idx"], table_loc, profile, cand_idx, reason,
                    bonus=5,
                )

    return table_loc


def _page_lines(page: PageContent) -> List[str]:
    """Rotation-aware reading-order lines for a page."""
    if has_rotated_text(page):
        return _reconstruct_lines_from_rotated(page)
    return [ln for ln in page.text_columnized.splitlines() if ln.strip()]


# BV column keywords that mark a page as carrying biological-variation data.
_BV_SIGNAL_TOKENS = (
    "cvi", "cvg", "cva", "cv i", "cv g", "within-subject",
    "between-subject", "within subject", "between subject",
    "biological variation",
)


def _page_has_bv_signal(lines: List[str]) -> bool:
    """True if the page mentions a BV column keyword or a known analyte."""
    text = " ".join(lines).lower()
    if any(tok in text for tok in _BV_SIGNAL_TOKENS):
        return True
    tokens = set(text.split())
    return any(a in tokens for a in KNOWN_ANALYTES)


def _build_relocated(
    doc: Document,
    best_idx: int,
    table_loc: TableLocation,
    profile: ArticleProfile,
    cand_idx: int,
    reason: str,
    bonus: float,
) -> TableLocation:
    """Re-score the chosen page and return a relocated TableLocation.

    The new page is re-scored via ``table_finder._score_page`` so its score
    reflects its own content; a small ``bonus`` reflects the evidence that
    justified the move. ``relocation_reason`` records what happened.
    """
    from .table_finder import _score_page

    new_page = doc.pages[best_idx]
    rescored = _score_page(new_page)
    if rescored is None:
        from .table_finder import _to_unrotated as _wrap_upright
        is_rotated = has_rotated_text(new_page)
        if is_rotated:
            from .rotation import unrotate_words as _unrot
            words = _unrot(new_page)
        else:
            words = [
                _wrap_upright(w, new_page)
                for w in new_page.words if w.upright
            ]
        new_loc = TableLocation(
            page_index=best_idx,
            is_rotated=is_rotated,
            words=words,
            label=None,
            score=_MIN_TABLE_SCORE,
        )
    else:
        new_loc = rescored

    new_loc.score += bonus
    profile.relocation_reason = (
        f"{reason}; original page {cand_idx + 1} -> page {best_idx + 1}; "
        f"score {new_loc.score:.0f}"
    )
    return new_loc


# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------

def _compute_verdict(profile: ArticleProfile) -> str:
    """Classify the profile into one of the four verdict labels."""
    if profile.primary_table_page is None:
        return VERDICT_NO_TABLE
    if profile.primary_table_score < _MIN_TABLE_SCORE:
        return VERDICT_NOT_BV
    # Either format is OK — papers vary in convention. We accept the
    # candidate page's local format detection as the authoritative
    # signal: if the page has a recognised format AND the score is
    # high enough AND document-wide patterns are dense, the article is
    # extractable.
    has_dense_patterns = (
        profile.doc_mean_sd_match_count >= _MIN_PATTERN_MATCHES
        or profile.doc_estimate_ci_match_count >= _MIN_PATTERN_MATCHES
    )
    has_recognised_format = profile.format_class in (
        FORMAT_MEAN_SD, FORMAT_VALUE_CI, FORMAT_HYBRID
    )
    if (profile.primary_table_score >= _MIN_EXTRACTABLE_SCORE
            and has_dense_patterns
            and has_recognised_format):
        return VERDICT_EXTRACTABLE
    return VERDICT_NON_STANDARD


# ---------------------------------------------------------------------------
# Geometry classification
# ---------------------------------------------------------------------------

def _classify_geometry(table_loc: TableLocation) -> str:
    """Return the geometry class for the located table.

    v1 only distinguishes upright vs rotated_ccw because that's what we
    have evidence for. ROTATED_CW is reserved for a future iteration
    where we have a real example to calibrate against.
    """
    if not table_loc.is_rotated:
        return GEOMETRY_UPRIGHT
    # The rotation module currently only un-rotates 90° CCW; if a CW
    # rotation appeared, the un-rotated coords would be visibly wrong
    # but the flag would still say is_rotated=True. For now we assume
    # CCW; future work should split these cases.
    return GEOMETRY_ROTATED_CCW


# ---------------------------------------------------------------------------
# Format classification
# ---------------------------------------------------------------------------

def _classify_format_on_page(
    page: PageContent, profile: ArticleProfile
) -> str:
    """Classify the numeric reporting format on the candidate page.

    For upright pages we use the page's reading-order text directly.
    For rotated pages, the page's ``text_columnized`` is unreliable
    (pdfplumber sees the words sideways and can't column-extract
    correctly), so we reconstruct lines from the un-rotated word list.

    Counts how many segments on the candidate page match each format's
    regex, then picks the dominant one. Hybrid is returned when both
    mean_sd and value_ci appear in significant numbers.
    """
    if has_rotated_text(page):
        lines = _reconstruct_lines_from_rotated(page)
    else:
        lines = [ln for ln in page.text_columnized.splitlines() if ln.strip()]

    mean_sd_lines = sum(len(_RE_MEAN_SD.findall(ln)) for ln in lines)
    value_ci_lines = sum(len(_RE_VALUE_CI.findall(ln)) for ln in lines)
    # paren_sd is only counted on lines that have NO value_ci match,
    # because '(low, high)' would otherwise also match '(value)'.
    paren_sd_lines = sum(
        len(_RE_PAREN_SD.findall(ln)) for ln in lines
        if not _RE_VALUE_CI.search(ln)
    )

    # Stash the raw counts on the profile for the report
    profile.candidate_mean_sd_hits = mean_sd_lines
    profile.candidate_value_ci_hits = value_ci_lines
    profile.candidate_paren_sd_hits = paren_sd_lines

    # Decision logic:
    # - need at least 3 hits to claim a format (avoids one-off matches)
    # - if both mean_sd and value_ci are well-represented, it's hybrid
    # - paren_sd only wins if the others are absent (it's the weakest
    #   signal because it overlaps with non-BV constructs)
    THRESHOLD = 3
    has_mean_sd = mean_sd_lines >= THRESHOLD
    has_value_ci = value_ci_lines >= THRESHOLD
    has_paren_sd = paren_sd_lines >= THRESHOLD

    if has_mean_sd and has_value_ci:
        return FORMAT_HYBRID
    if has_mean_sd:
        return FORMAT_MEAN_SD
    if has_value_ci:
        return FORMAT_VALUE_CI
    if has_paren_sd:
        return FORMAT_PAREN_SD
    return FORMAT_UNKNOWN


def _reconstruct_lines_from_rotated(page: PageContent) -> List[str]:
    """Reconstruct text lines from a rotated page using un-rotated coords.

    Words are clustered into rows by their ``oy_center`` (un-rotated
    vertical centre); within each row, words are sorted left-to-right
    by ``ox0``. Tolerance for "same row" is the median word height,
    which adapts to the font size used in the table.

    Returns a list of strings, one per row, that downstream regex can
    operate on as if the table were upright.
    """
    words = unrotate_words(page)
    if not words:
        return []

    # Median word height as the row-cluster tolerance
    heights = sorted(abs(w.oy1 - w.oy0) for w in words)
    median_h = heights[len(heights) // 2] if heights else 8.0
    tol = max(median_h * 0.6, 3.0)

    # Sort words by oy_center then ox0 so we can sweep top-to-bottom
    sorted_words = sorted(words, key=lambda w: (w.oy_center, w.ox0))
    rows: List[List] = []
    current_row: List = []
    current_y: float = sorted_words[0].oy_center

    for w in sorted_words:
        if abs(w.oy_center - current_y) <= tol:
            current_row.append(w)
        else:
            rows.append(current_row)
            current_row = [w]
            current_y = w.oy_center
    if current_row:
        rows.append(current_row)

    # Build line strings, words sorted left-to-right within each row
    out: List[str] = []
    for row in rows:
        row_sorted = sorted(row, key=lambda w: w.ox0)
        line = " ".join(w.text for w in row_sorted).strip()
        if line:
            out.append(line)
    return out


def _to_unrotated_text_for_doc(doc: Document) -> dict:
    """Map page_index -> rotation-aware list of line strings.

    Cached on first use; returns ``text_columnized.splitlines()`` for
    upright pages and a reconstructed line list for rotated pages.
    """
    out: dict = {}
    for page in doc.pages:
        if has_rotated_text(page):
            out[page.page_index] = _reconstruct_lines_from_rotated(page)
        else:
            out[page.page_index] = [
                ln for ln in page.text_columnized.splitlines() if ln.strip()
            ]
    return out


# ---------------------------------------------------------------------------
# Document-wide helpers
# ---------------------------------------------------------------------------

def _count_doc_mean_sd(doc: Document) -> int:
    """Total Mean±SD pattern matches across all pages of a document.

    Uses ``findall`` so a line containing multiple ``X±Y`` segments
    (common in compact summary tables) contributes its full count.
    For rotated pages, the line list is reconstructed from un-rotated
    word coordinates so the rotated table's matches aren't lost.
    """
    total = 0
    page_lines = _to_unrotated_text_for_doc(doc)
    for lines in page_lines.values():
        for line in lines:
            total += len(_RE_MEAN_SD.findall(line))
    return total


def _count_doc_value_ci(doc: Document) -> int:
    """Total value(CI) pattern matches across all pages of a document.

    Uses ``findall`` so a single line with several ``value (low–high)``
    segments (e.g. Jabor 2024 Table 2) is fully counted. Rotation-aware
    via ``_to_unrotated_text_for_doc``.
    """
    total = 0
    page_lines = _to_unrotated_text_for_doc(doc)
    for lines in page_lines.values():
        for line in lines:
            total += len(_RE_VALUE_CI.findall(line))
    return total


def _collect_analytes(doc: Document) -> set:
    """Return the set of recognised analyte names found anywhere in the doc."""
    found: set = set()
    for page in doc.pages:
        for w in page.words:
            t = w.text.lower()
            if t in KNOWN_ANALYTES:
                found.add(t)
    return found


# ---------------------------------------------------------------------------
# Profile formatting (for human-readable reports)
# ---------------------------------------------------------------------------

def format_profile(profile: ArticleProfile) -> str:
    """Render an ArticleProfile as a concise plain-text block.

    Used by ``diagnose.py`` to embed the profile in its summary report.
    """
    import textwrap

    lines: List[str] = []
    lines.append(f"Verdict           : {profile.verdict}")
    desc = VERDICT_DESCRIPTIONS[profile.verdict]
    indent = " " * 20
    for w in textwrap.wrap(desc, width=72 - len(indent)):
        lines.append(f"{indent}{w}")
    lines.append(f"Format class      : {profile.format_class}")
    fdesc = FORMAT_DESCRIPTIONS[profile.format_class]
    for w in textwrap.wrap(fdesc, width=72 - len(indent)):
        lines.append(f"{indent}{w}")
    lines.append(f"Geometry class    : {profile.geometry_class}")
    gdesc = GEOMETRY_DESCRIPTIONS[profile.geometry_class]
    for w in textwrap.wrap(gdesc, width=72 - len(indent)):
        lines.append(f"{indent}{w}")
    lines.append(
        f"Candidate page hits: mean_sd={profile.candidate_mean_sd_hits}  "
        f"value_ci={profile.candidate_value_ci_hits}  "
        f"paren_sd={profile.candidate_paren_sd_hits}"
    )
    if profile.relocated_from_page is not None:
        lines.append(
            f"Page relocation   : page {profile.relocated_from_page} "
            f"-> page {profile.primary_table_page}"
        )
        if profile.relocation_reason:
            for w in textwrap.wrap(
                profile.relocation_reason, width=72 - len(indent)
            ):
                lines.append(f"{indent}{w}")
    return "\n".join(lines)
