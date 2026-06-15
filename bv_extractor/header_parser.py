"""
Build a map from "table column" -> ox-range, by reading the multi-line
header rows that sit above the data.

The header structure of a typical BV results table is hierarchical:

                            +------- "Current study" -------+ +-Chen [26]-+ ...
    +---------+ +- Mean±SD -+- CV_A -+- CV_I -+- CV_G ------+ | CVI | CVG | ...
    | Analyte |
    +---------+

The extractor cares almost exclusively about the *Current study* sub-columns
(Mean±SD, CV_A, CV_I, CV_G). Comparison-study columns are recorded only
so their data values are not mistakenly attributed to Current study fields.

Algorithm
---------
  1.  Cluster header words into oy-lines and stitch fragmented tokens
      ("CV" + "A" -> "CV_A").
  2.  Locate the "Current study" parent anchor, which sets a horizontal
      band [study_ox_min, study_ox_max].
  3.  Inside that band, find sub-column anchors classified as one of
      mean_sd/cva/cvi/cvg.
  4.  Outside that band, anchors classified as "study" become single-
      column "comparison" columns.
  5.  Bound each column's ox-range using its neighbours' centres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .patterns import classify_header
from .rotation import UnrotatedWord


# ---------------------------------------------------------------------------
# Column descriptor
# ---------------------------------------------------------------------------

@dataclass
class Column:
    """A logical table column with its on-page extent."""
    role: str                       # 'analyte','mean_sd','cva','cvi','cvg',
                                    # 'comparison','other'
    label: str                      # human-readable header text
    ox_min: float
    ox_max: float
    is_current_study: bool = True   # False for comparison-study columns

    def contains(self, ox: float, slack: float = 1.0) -> bool:
        return (self.ox_min - slack) <= ox <= (self.ox_max + slack)

    @property
    def width(self) -> float:
        return self.ox_max - self.ox_min


# ---------------------------------------------------------------------------
# Header parser entry point
# ---------------------------------------------------------------------------

def parse_columns(
    words: List[UnrotatedWord],
    data_top_oy: float,
) -> List[Column]:
    """Build the column map from words sitting above `data_top_oy`."""
    header_words = [w for w in words if w.oy_center < data_top_oy - 2.0]
    if not header_words:
        return []

    # 1. Cluster header words into oy-lines and stitch fragmented tokens
    lines = _group_into_lines(header_words, oy_tol=4.0)
    stitched_lines = [_stitch_line(line) for line in lines]

    all_tokens: List[_Token] = [t for line in stitched_lines for t in line]
    if not all_tokens:
        return []

    # 2. Locate the "Current study" parent (text-based; tolerant of "*")
    cs_anchor = _find_current_study_anchor(all_tokens)

    # 3. Locate study (comparison) anchors. Use 'et al [N]' tokens so we
    #    don't mistake stray words like 'biological' for a study citation.
    study_anchors = _find_study_anchors(all_tokens)

    # 4. Locate the analyte anchor (left-most "Analyte" header)
    analyte_anchor = _find_token_by_role(all_tokens, "analyte")

    # 5. Determine the Current-study horizontal band.
    #    It runs from cs_anchor.ox_min on the left up to the first study
    #    anchor (or page edge) on the right.
    cs_band_min, cs_band_max = _compute_current_study_band(
        cs_anchor, study_anchors, all_tokens
    )

    # 6. Inside the Current-study band, collect the recognised sub-columns
    sub_anchors = _find_sub_column_anchors(all_tokens, cs_band_min, cs_band_max)

    # 7. Build the final Column list, sorted by ox.
    columns: List[Column] = []

    if analyte_anchor is not None:
        columns.append(
            Column(
                role="analyte",
                label="Analyte",
                # Use the anchor's centre for sensible neighbour-midpoint
                # arithmetic in _assign_bounds; the actual ox_max will be
                # assigned later.
                ox_min=analyte_anchor.ox_center,
                ox_max=analyte_anchor.ox_center,
                is_current_study=False,
            )
        )

    for sub in sub_anchors:
        columns.append(
            Column(
                role=sub.role,
                label=sub.text,
                ox_min=sub.ox_center,    # placeholder; bounds set below
                ox_max=sub.ox_center,
                is_current_study=True,
            )
        )

    for stu in study_anchors:
        columns.append(
            Column(
                role="comparison",
                label=stu.text,
                ox_min=stu.ox_center,
                ox_max=stu.ox_center,
                is_current_study=False,
            )
        )

    # 8. Sort and assign neighbour-midpoint bounds
    columns.sort(key=lambda c: c.ox_min)
    columns = _assign_bounds(columns)

    return columns


# ---------------------------------------------------------------------------
# Helpers: token type
# ---------------------------------------------------------------------------

@dataclass
class _Token:
    text: str
    ox0: float
    ox1: float
    oy0: float
    oy1: float
    role: str = ""

    @property
    def ox_center(self) -> float:
        return (self.ox0 + self.ox1) / 2.0


# ---------------------------------------------------------------------------
# Line grouping and token stitching
# ---------------------------------------------------------------------------

def _group_into_lines(
    words: List[UnrotatedWord],
    oy_tol: float = 4.0,
) -> List[List[UnrotatedWord]]:
    """Cluster words into horizontal text lines by oy proximity."""
    sorted_w = sorted(words, key=lambda w: w.oy_center)
    lines: List[List[UnrotatedWord]] = []
    for w in sorted_w:
        if lines and abs(w.oy_center - lines[-1][0].oy_center) <= oy_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w.ox_center)
    return lines


def _stitch_line(line: List[UnrotatedWord], gap_tol: float = 4.0) -> List[_Token]:
    """Merge horizontally-adjacent words on a single line.

    pdfplumber sometimes splits 'CV_A' into 'CV' and 'A' — we glue them
    back together when the horizontal gap is small. We also merge things
    like 'et' + 'al' + '[26]'.
    """
    out: List[_Token] = []
    for w in line:
        if out and (w.ox0 - out[-1].ox1) <= gap_tol:
            out[-1].text = (out[-1].text + " " + w.text).strip()
            out[-1].ox1 = w.ox1
        else:
            out.append(_Token(text=w.text, ox0=w.ox0, ox1=w.ox1,
                              oy0=w.oy0, oy1=w.oy1))
    # Classify after stitching
    for t in out:
        t.role = classify_header(t.text)
    return out


# ---------------------------------------------------------------------------
# Anchor finding
# ---------------------------------------------------------------------------

def _find_current_study_anchor(tokens: List[_Token]) -> Optional[_Token]:
    """Return the token labelling the 'Current study' parent column."""
    for t in tokens:
        text_low = t.text.lower().replace("*", "").strip()
        if "current" in text_low and "study" in text_low:
            return t
    return None


def _find_study_anchors(tokens: List[_Token]) -> List[_Token]:
    """Return tokens that are study citations like 'et al [26]'."""
    out: List[_Token] = []
    for t in tokens:
        if "et al" in t.text.lower():
            out.append(t)
    out.sort(key=lambda x: x.ox_center)
    return out


def _find_token_by_role(tokens: List[_Token], role: str) -> Optional[_Token]:
    for t in tokens:
        if t.role == role:
            return t
    return None


def _find_sub_column_anchors(
    tokens: List[_Token],
    band_min: float,
    band_max: float,
) -> List[_Token]:
    """Inside the Current-study band, find the four sub-column anchors.

    Sub-columns we care about: mean_sd, cva, cvi, cvg. We deduplicate by
    role so that, e.g., the table-title word "between-subject" doesn't
    create a second "cvg" anchor.
    """
    seen_roles = set()
    out: List[_Token] = []
    for t in sorted(tokens, key=lambda x: x.ox_center):
        if t.role not in {"mean_sd", "cva", "cvi", "cvg"}:
            continue
        if not (band_min <= t.ox_center <= band_max):
            continue
        if t.role in seen_roles:
            continue
        seen_roles.add(t.role)
        out.append(t)
    out.sort(key=lambda x: x.ox_center)
    return out


def _compute_current_study_band(
    cs_anchor: Optional[_Token],
    study_anchors: List[_Token],
    all_tokens: List[_Token],
) -> tuple[float, float]:
    """Return the (ox_min, ox_max) of the Current-study parent column.

    The Current-study label is usually centred above its sub-columns, so we
    cannot use its own ox-extent as the band. Instead we anchor the band
    between the Analyte column on the left and the first comparison-study
    citation on the right.
    """
    # Right edge: first study citation that comes after the Current-study
    # anchor (or after the analyte column if no Current-study anchor).
    reference_x = cs_anchor.ox_center if cs_anchor else 0.0
    right_edge = max((t.ox1 for t in all_tokens), default=reference_x + 200.0)
    for sa in study_anchors:
        if sa.ox_center > reference_x:
            right_edge = sa.ox0 - 5.0
            break

    # Left edge: just after the Analyte header (if present), otherwise 0.
    left_edge = 0.0
    for t in all_tokens:
        if t.role == "analyte":
            left_edge = t.ox1 + 1.0
            break

    return (left_edge, right_edge)


# ---------------------------------------------------------------------------
# Column boundary assignment
# ---------------------------------------------------------------------------

def _assign_bounds(columns: List[Column]) -> List[Column]:
    """Assign each column an [ox_min, ox_max] using neighbour midpoints."""
    if not columns:
        return columns

    centers = [c.ox_min for c in columns]   # currently ox_min == ox_max == centre
    for i, col in enumerate(columns):
        center = centers[i]
        prev_center = centers[i - 1] if i > 0 else center - 30.0
        next_center = centers[i + 1] if i + 1 < len(columns) else center + 50.0
        col.ox_min = (prev_center + center) / 2.0 if i > 0 else 0.0
        col.ox_max = (center + next_center) / 2.0
    return columns


# ---------------------------------------------------------------------------
# Convenience: find the first data-row oy
# ---------------------------------------------------------------------------

def find_data_top_oy(
    words: List[UnrotatedWord],
    known_analytes: set,
) -> Optional[float]:
    """Return the smallest oy at which a known analyte name appears."""
    candidates = [
        w.oy_center
        for w in words
        if w.text.lower() in known_analytes
    ]
    return min(candidates) if candidates else None
