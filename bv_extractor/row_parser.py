"""
Row parser: assemble one analyte's BV-form data using the column map.

For each analyte:
  1. Locate every word that shares the analyte's oy line.
  2. Bucket those words into the columns produced by header_parser.
  3. Concatenate words within a bucket (in ox order) to form a "cell".
  4. Apply pattern parsing depending on the column's role.

The output is an AnalyteRecord populated with FieldValue objects whose
`raw_text` traces back to the cell text we parsed. Missing or footnote-
flagged values produce explicit warnings rather than silent zeros.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .header_parser import Column
from .patterns import (
    is_missing_token,
    parse_estimate_with_ci,
    parse_mean_sd,
)
from .rotation import UnrotatedWord
from .schema import AnalyteRecord, FieldValue


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_analyte_row(
    analyte_word: UnrotatedWord,
    all_words: List[UnrotatedWord],
    columns: List[Column],
    oy_tol: float = 4.0,
) -> AnalyteRecord:
    """Build an AnalyteRecord for the given analyte from one row of words."""
    rec = AnalyteRecord(
        name=analyte_word.text,
        abbreviation=analyte_word.text,
    )

    # 1. Find all words on the same oy-line as the analyte
    row = [
        w for w in all_words
        if abs(w.oy_center - analyte_word.oy_center) <= oy_tol
    ]
    row.sort(key=lambda w: w.ox_center)

    # 2. Bucket words into columns
    cells: Dict[int, List[UnrotatedWord]] = {i: [] for i in range(len(columns))}
    for w in row:
        for i, col in enumerate(columns):
            if col.contains(w.ox_center):
                cells[i].append(w)
                break

    # 3. For each Current-study column, parse and store the result
    for i, col in enumerate(columns):
        if not col.is_current_study:
            continue
        cell_words = cells[i]
        if not cell_words:
            _set_blank_for_role(rec, col.role,
                                "No value found in cell after row parsing.")
            continue
        cell_text = " ".join(w.text for w in cell_words).strip()
        _apply_cell_to_record(rec, col.role, cell_text)

    return rec


# ---------------------------------------------------------------------------
# Mapping a cell's text to record fields, by role
# ---------------------------------------------------------------------------

def _apply_cell_to_record(rec: AnalyteRecord, role: str, cell_text: str) -> None:
    """Dispatch parsing for one cell based on the column role."""
    if role == "mean_sd":
        _parse_into_mean_sd(rec, cell_text)
    elif role == "cva":
        _parse_into_cva(rec, cell_text)
    elif role == "cvi":
        _parse_into_cvi(rec, cell_text)
    elif role == "cvg":
        _parse_into_cvg(rec, cell_text)
    # Other roles (analyte label, comparison) are ignored intentionally.


def _parse_into_mean_sd(rec: AnalyteRecord, text: str) -> None:
    if is_missing_token(text):
        rec.measurand_mean = FieldValue(
            warning="Mean±SD cell present but reported as missing.", raw_text=text)
        rec.measurand_sd = FieldValue(
            warning="Mean±SD cell present but reported as missing.", raw_text=text)
        return
    mean, sd = parse_mean_sd(text)
    if mean is None or sd is None:
        msg = ("Could not parse Mean±SD pattern from this cell. "
               "The article may use a non-standard format.")
        rec.measurand_mean = FieldValue(warning=msg, raw_text=text)
        rec.measurand_sd = FieldValue(warning=msg, raw_text=text)
    else:
        rec.measurand_mean = FieldValue(value=mean, raw_text=text)
        rec.measurand_sd = FieldValue(value=sd, raw_text=text)


def _parse_into_cva(rec: AnalyteRecord, text: str) -> None:
    if is_missing_token(text):
        rec.analytical_cv = FieldValue(warning="CV_A reported as missing.", raw_text=text)
        return
    est, lo, hi, footnote_reason = parse_estimate_with_ci(text)
    if est is None:
        rec.analytical_cv = FieldValue(
            warning="Could not parse CV_A estimate from cell.", raw_text=text)
    else:
        warn = footnote_reason
        rec.analytical_cv = FieldValue(value=est, warning=warn, raw_text=text)


def _parse_into_cvi(rec: AnalyteRecord, text: str) -> None:
    if is_missing_token(text):
        rec.cvi = FieldValue(warning="CV_I reported as missing.", raw_text=text)
        return
    est, lo, hi, footnote_reason = parse_estimate_with_ci(text)
    rec.cvi = FieldValue(
        value=est,
        raw_text=text,
        warning=("Could not parse CV_I estimate." if est is None else footnote_reason),
    )
    if lo is None and est is not None:
        rec.cvi_ci_lower = FieldValue(
            warning=(footnote_reason or
                     "CV_I lower 95% CI not reported in this cell."),
            raw_text=text,
        )
    else:
        rec.cvi_ci_lower = FieldValue(value=lo, raw_text=text)
    if hi is None and est is not None:
        rec.cvi_ci_upper = FieldValue(
            warning=(footnote_reason or
                     "CV_I upper 95% CI not reported in this cell."),
            raw_text=text,
        )
    else:
        rec.cvi_ci_upper = FieldValue(value=hi, raw_text=text)


def _parse_into_cvg(rec: AnalyteRecord, text: str) -> None:
    if is_missing_token(text):
        rec.cvg = FieldValue(warning="CV_G reported as missing.", raw_text=text)
        return
    est, lo, hi, footnote_reason = parse_estimate_with_ci(text)
    rec.cvg = FieldValue(
        value=est,
        raw_text=text,
        warning=("Could not parse CV_G estimate." if est is None else footnote_reason),
    )
    if lo is None and est is not None:
        rec.cvg_ci_lower = FieldValue(
            warning=(footnote_reason or
                     "CV_G lower 95% CI not reported in this cell."),
            raw_text=text,
        )
    else:
        rec.cvg_ci_lower = FieldValue(value=lo, raw_text=text)
    if hi is None and est is not None:
        rec.cvg_ci_upper = FieldValue(
            warning=(footnote_reason or
                     "CV_G upper 95% CI not reported in this cell."),
            raw_text=text,
        )
    else:
        rec.cvg_ci_upper = FieldValue(value=hi, raw_text=text)


def _set_blank_for_role(rec: AnalyteRecord, role: str, reason: str) -> None:
    """Mark every field associated with `role` as blank with a warning."""
    if role == "mean_sd":
        rec.measurand_mean = FieldValue(warning=reason)
        rec.measurand_sd = FieldValue(warning=reason)
    elif role == "cva":
        rec.analytical_cv = FieldValue(warning=reason)
    elif role == "cvi":
        rec.cvi = FieldValue(warning=reason)
        rec.cvi_ci_lower = FieldValue(warning=reason)
        rec.cvi_ci_upper = FieldValue(warning=reason)
    elif role == "cvg":
        rec.cvg = FieldValue(warning=reason)
        rec.cvg_ci_lower = FieldValue(warning=reason)
        rec.cvg_ci_upper = FieldValue(warning=reason)


# ---------------------------------------------------------------------------
# Convenience: get analyte rows from un-rotated words
# ---------------------------------------------------------------------------

def find_analyte_rows(
    words: List[UnrotatedWord],
    known_analytes: set,
    analyte_col_ox_max: Optional[float] = None,
    data_top_oy: Optional[float] = None,
    max_row_gap: float = 25.0,
) -> List[UnrotatedWord]:
    """Return one UnrotatedWord per analyte name found in the BV table.

    Three filters guard against false positives — words that match an analyte
    name but appear in surrounding prose (figure caption, methods, etc.):

      * `analyte_col_ox_max`: if given, the candidate's `ox_center` must lie
        below that bound (i.e., it is in the leftmost "Analyte" column).
      * `data_top_oy`: if given, the candidate's `oy_center` must lie below
        the top of the data area (i.e., it is in the data rows, not in
        the header/title strip above).
      * `max_row_gap`: once a contiguous block of rows is established, a
        candidate whose oy jumps ahead by more than `max_row_gap` is treated
        as belonging to a different text block (e.g., the footnote that
        spells out abbreviations) and is rejected.
    """
    out: List[UnrotatedWord] = []
    seen: set = set()
    candidates: List[UnrotatedWord] = []

    for w in words:
        key = w.text.lower()
        if key not in known_analytes or key in seen:
            continue
        if analyte_col_ox_max is not None and w.ox_center > analyte_col_ox_max:
            continue
        if data_top_oy is not None and w.oy_center < data_top_oy - 2.0:
            continue
        candidates.append(w)
        seen.add(key)

    candidates.sort(key=lambda w: w.oy_center)

    # Keep contiguous candidates; stop at the first big gap.
    for w in candidates:
        if out and (w.oy_center - out[-1].oy_center) > max_row_gap:
            break
        out.append(w)
    return out
