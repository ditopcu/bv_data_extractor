"""
Diagnostic tool for inspecting PDF articles before (or instead of) running
the full extraction pipeline.

Three modes
-----------
* ``summary`` (default): Prints a per-page overview of the PDF — word counts,
  rotation status, BV-keyword density, candidate analyte locations, "Table N"
  labels, two/single-column layout, and pattern-match counts (Mean±SD,
  estimate (CI)). Output is also saved to ``<pdf>_diagnose.txt`` next to the
  PDF.

* ``dump``: Writes a CSV of every word in the PDF with its bounding box and
  rotation flag, ready for manual inspection in Excel or for sharing during
  a debug session. Output: ``<pdf>_words.csv``.

* ``batch``: Iterates a directory of PDFs, runs ``summary`` on each, and
  produces a single ``batch_summary.csv`` aggregating one row per file:
  ``pdf_name, pages, candidate_table_pages, has_rotation, analytes_found,
  score``. The per-file ``_diagnose.txt`` files are also produced.

Visual mode (overlaying word boxes on a page image) is intentionally not
implemented yet but would slot in cleanly as a fourth mode.
"""

from __future__ import annotations

import argparse
import csv
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .pdf_io import PageContent, load_pdf
from .patterns import parse_estimate_with_ci, parse_mean_sd
from .rotation import has_rotated_text, unrotate_words
from .table_finder import BV_HEADER_TOKENS, KNOWN_ANALYTES, find_bv_table


# ---------------------------------------------------------------------------
# Verdict labels and thresholds
# ---------------------------------------------------------------------------
#
# A verdict is a single tag that summarises the diagnostic result so the
# user can decide at a glance whether the article is worth running through
# the full extractor. Thresholds were calibrated against the Yang 2018
# article (the well-formed reference, score 50, 52 mean-SD matches, 5 CI
# matches) and the Hongo 1993 article (a non-standard pre-modern study,
# score 24, 0 pattern matches).

VERDICT_EXTRACTABLE = "LIKELY_EXTRACTABLE"
VERDICT_NON_STANDARD = "NON_STANDARD_FORMAT"
VERDICT_NOT_BV = "NOT_A_BV_TABLE"
VERDICT_NO_TABLE = "NO_TABLE_FOUND"

_MIN_EXTRACTABLE_SCORE = 30.0
_MIN_TABLE_SCORE = 5.0
_MIN_PATTERN_MATCHES = 5

# Human-readable explanations, one per verdict
VERDICT_DESCRIPTIONS = {
    VERDICT_EXTRACTABLE:
        "A BV results table was found and the article uses standard "
        "Mean±SD and estimate (95% CI) reporting. Full extraction is "
        "likely to succeed.",
    VERDICT_NON_STANDARD:
        "A BV-like table was found but the reporting format is not "
        "standard (few or no Mean±SD or 95% CI patterns matched). "
        "The full extractor may return partial or empty results — "
        "manual review recommended before relying on the output.",
    VERDICT_NOT_BV:
        "No table on this page meets the minimum BV-table score. The "
        "article probably does not contain biological variation results "
        "in a recognisable form.",
    VERDICT_NO_TABLE:
        "No candidate table was identified anywhere in the document.",
}


# ---------------------------------------------------------------------------
# Per-page diagnostic record
# ---------------------------------------------------------------------------

@dataclass
class PageDiagnostic:
    """All metrics computed for one page during summary mode."""
    page_index: int                       # 0-based
    page_number: int                      # 1-based for human display
    width: float
    height: float
    word_count: int
    upright_count: int
    rotated_count: int
    bv_header_hits: int                   # words matching BV_HEADER_TOKENS
    analytes_found: List[str] = field(default_factory=list)
    table_labels: List[str] = field(default_factory=list)
    column_layout: str = "unknown"        # "single", "two", "unknown"
    is_candidate_bv_page: bool = False
    candidate_score: float = 0.0


@dataclass
class FileDiagnostic:
    """Top-level summary for one PDF file."""
    path: Path
    page_count: int
    pages: List[PageDiagnostic] = field(default_factory=list)
    has_rotation: bool = False
    primary_table_label: Optional[str] = None
    primary_table_page: Optional[int] = None
    primary_table_rotated: bool = False
    primary_table_score: float = 0.0
    all_analytes: List[str] = field(default_factory=list)
    mean_sd_match_count: int = 0
    estimate_ci_match_count: int = 0
    verdict: str = VERDICT_NO_TABLE        # filled in by compute_verdict()


# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------

def compute_verdict(diag: "FileDiagnostic") -> str:
    """Classify the diagnostic into one of the four verdict labels.

    Decision tree (intentionally simple — ordering matters):

      1. If no candidate table was found at all -> NO_TABLE_FOUND.
      2. If the candidate table's score is below the BV-table floor
         -> NOT_A_BV_TABLE (the article probably doesn't report BV at all).
      3. If both pattern-match counts are healthy AND the score is high
         -> LIKELY_EXTRACTABLE.
      4. Otherwise (a BV-like table is present but the reporting style is
         off) -> NON_STANDARD_FORMAT.
    """
    if diag.primary_table_page is None:
        return VERDICT_NO_TABLE
    if diag.primary_table_score < _MIN_TABLE_SCORE:
        return VERDICT_NOT_BV
    if (diag.primary_table_score >= _MIN_EXTRACTABLE_SCORE
            and diag.mean_sd_match_count >= _MIN_PATTERN_MATCHES
            and diag.estimate_ci_match_count >= _MIN_PATTERN_MATCHES):
        return VERDICT_EXTRACTABLE
    return VERDICT_NON_STANDARD


# ---------------------------------------------------------------------------
# Summary mode
# ---------------------------------------------------------------------------

def diagnose_summary(pdf_path: Path) -> FileDiagnostic:
    """Run a summary diagnostic on one PDF and return a FileDiagnostic."""
    doc = load_pdf(pdf_path)
    diag = FileDiagnostic(path=pdf_path, page_count=len(doc.pages))

    # Full-document analyte set (uses un-rotated coords on rotated pages so
    # that sideways tables also contribute their analyte names).
    seen_analytes: set[str] = set()
    mean_sd_total = 0
    estimate_ci_total = 0

    for page in doc.pages:
        page_diag = _diagnose_page(page)
        diag.pages.append(page_diag)
        if page_diag.rotated_count > 0:
            diag.has_rotation = True
        seen_analytes.update(a.lower() for a in page_diag.analytes_found)

        # Pattern match counts on the columnized text
        mean_sd_total += _count_mean_sd_matches(page.text_columnized)
        estimate_ci_total += _count_estimate_ci_matches(page.text_columnized)

    diag.all_analytes = sorted(seen_analytes)
    diag.mean_sd_match_count = mean_sd_total
    diag.estimate_ci_match_count = estimate_ci_total

    # Locate the primary BV table (same algorithm as the production pipeline)
    table_loc = find_bv_table(doc)
    if table_loc is not None:
        diag.primary_table_label = table_loc.label
        diag.primary_table_page = table_loc.page_index + 1
        diag.primary_table_rotated = table_loc.is_rotated
        diag.primary_table_score = table_loc.score
        # Mark the candidate page in the per-page list
        for pd in diag.pages:
            if pd.page_index == table_loc.page_index:
                pd.is_candidate_bv_page = True
                pd.candidate_score = table_loc.score

    # Final classification
    diag.verdict = compute_verdict(diag)
    return diag


def _diagnose_page(page: PageContent) -> PageDiagnostic:
    """Compute per-page metrics."""
    upright_words = [w for w in page.words if w.upright]
    rotated_words = [w for w in page.words if not w.upright]

    # Use un-rotated coords for rotated pages so all words live in one frame
    if rotated_words:
        unrotated = unrotate_words(page)
        candidate_words = [w.text for w in unrotated]
    else:
        candidate_words = [w.text for w in upright_words]

    lower_texts = [t.lower() for t in candidate_words]
    bv_hits = sum(1 for t in lower_texts if t in BV_HEADER_TOKENS)
    analyte_hits: set[str] = {
        t for t in lower_texts if t in KNOWN_ANALYTES
    }

    # "Table N" labels by physical adjacency on upright text
    table_labels = _find_table_labels(page)

    # Two-column vs single-column heuristic (same idea as pdf_io)
    col_layout = _classify_column_layout(upright_words, page.width)

    return PageDiagnostic(
        page_index=page.page_index,
        page_number=page.page_index + 1,
        width=page.width,
        height=page.height,
        word_count=len(page.words),
        upright_count=len(upright_words),
        rotated_count=len(rotated_words),
        bv_header_hits=bv_hits,
        analytes_found=sorted(analyte_hits),
        table_labels=table_labels,
        column_layout=col_layout,
    )


def _find_table_labels(page: PageContent) -> List[str]:
    """Return e.g. ['Table 1', 'Table 4'] found on the page."""
    out: List[str] = []
    upright = [w for w in page.words if w.upright]
    table_words = [w for w in upright if w.text.lower() == "table"]
    for tw in table_words:
        for cand in upright:
            t = cand.text.rstrip(".:")
            if not t.isdigit():
                continue
            if (abs(((cand.top + cand.bottom) / 2.0)
                    - ((tw.top + tw.bottom) / 2.0)) < 6.0
                    and 0 < cand.x0 - tw.x1 < 25.0):
                label = f"Table {t}"
                if label not in out:
                    out.append(label)
                break
    return out


def _classify_column_layout(upright_words, page_width: float) -> str:
    """Return 'single' or 'two' based on whether the page has a column gutter.

    Uses the same histogram-of-x0 approach as pdf_io._detect_columns:
    a clear vertical band with very few words near the page centre means
    a two-column layout.
    """
    if not upright_words:
        return "unknown"
    bin_w = 10.0
    counts: dict[int, int] = {}
    for w in upright_words:
        b = int(w.x0 // bin_w)
        counts[b] = counts.get(b, 0) + 1
    if not counts:
        return "unknown"

    midpoint = page_width / 2.0
    centre_bins = [
        b for b in counts
        if abs(b * bin_w + bin_w / 2.0 - midpoint) <= 60.0
    ]
    if not centre_bins:
        return "single"

    avg_density = sum(counts.values()) / max(1, len(counts))
    min_centre_count = min(counts[b] for b in centre_bins)
    if min_centre_count < avg_density * 0.4:
        return "two"
    return "single"


# ---------------------------------------------------------------------------
# Pattern-match counters (sanity checks)
# ---------------------------------------------------------------------------

def _count_mean_sd_matches(text: str) -> int:
    """Count textual occurrences of a Mean±SD pattern (simple sanity stat)."""
    count = 0
    # Walk substrings of reasonable length
    for line in text.splitlines():
        m, s = parse_mean_sd(line)
        if m is not None and s is not None:
            count += 1
    return count


def _count_estimate_ci_matches(text: str) -> int:
    """Count textual occurrences of an estimate (CI) pattern."""
    count = 0
    for line in text.splitlines():
        est, lo, hi, _ = parse_estimate_with_ci(line.strip())
        if est is not None and lo is not None and hi is not None:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------

def format_summary(diag: FileDiagnostic) -> str:
    """Return a human-readable diagnostic report as a single string."""
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("PDF Diagnostic Report")
    lines.append("=" * 72)
    lines.append(f"File              : {diag.path}")
    lines.append(f"Verdict           : {diag.verdict}")
    # Wrap the long verdict description across multiple lines so the
    # report stays within ~72 columns even on narrow terminals.
    desc = VERDICT_DESCRIPTIONS[diag.verdict]
    indent = " " * 20
    wrapped = textwrap.wrap(desc, width=72 - len(indent))
    for w in wrapped:
        lines.append(f"{indent}{w}")
    lines.append(f"Page count        : {diag.page_count}")
    lines.append(f"Has rotation      : {diag.has_rotation}")
    lines.append("")
    lines.append("Primary BV table candidate:")
    if diag.primary_table_label or diag.primary_table_page:
        lines.append(f"  Label           : {diag.primary_table_label or '<unlabelled>'}")
        lines.append(f"  Page            : {diag.primary_table_page}")
        lines.append(f"  Rotated         : {diag.primary_table_rotated}")
        lines.append(f"  Score           : {diag.primary_table_score:.1f}")
    else:
        lines.append("  None found (no page meets the minimum BV-table score).")
    lines.append("")
    lines.append(f"All analytes detected anywhere in document: "
                 f"{', '.join(diag.all_analytes) or '(none)'}")
    lines.append(f"Mean±SD pattern matches (whole doc) : "
                 f"{diag.mean_sd_match_count}")
    lines.append(f"Estimate (CI) pattern matches       : "
                 f"{diag.estimate_ci_match_count}")
    lines.append("")
    lines.append("-" * 72)
    lines.append("Per-page breakdown")
    lines.append("-" * 72)
    lines.append(
        f"{'Pg':>3}  {'Size (WxH)':>13}  {'Words':>5}  "
        f"{'Up':>4}  {'Rot':>4}  {'BV':>3}  {'Cols':>5}  "
        f"{'Cand':>4}  {'Score':>5}  Labels / Analytes"
    )
    for p in diag.pages:
        size = f"{int(p.width)}x{int(p.height)}"
        cand = "Y" if p.is_candidate_bv_page else "."
        score = f"{p.candidate_score:.1f}" if p.is_candidate_bv_page else ""
        labels_part = ",".join(p.table_labels) if p.table_labels else ""
        analytes_part = ",".join(p.analytes_found) if p.analytes_found else ""
        suffix = labels_part
        if analytes_part:
            suffix = (suffix + " | " if suffix else "") + analytes_part
        lines.append(
            f"{p.page_number:>3}  {size:>13}  {p.word_count:>5}  "
            f"{p.upright_count:>4}  {p.rotated_count:>4}  "
            f"{p.bv_header_hits:>3}  {p.column_layout:>5}  "
            f"{cand:>4}  {score:>5}  {suffix}"
        )
    lines.append("")
    lines.append("Legend: BV = BV-header keyword hits (CV, mean, sd, biological, ...)")
    lines.append("        Cols = detected column layout (single / two)")
    lines.append("        Cand = Y means this page was selected as the primary BV table")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Dump mode
# ---------------------------------------------------------------------------

def dump_words(pdf_path: Path, csv_path: Path) -> int:
    """Write every word with its bounding box to a CSV. Returns row count."""
    doc = load_pdf(pdf_path)
    rows = 0
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "page", "word_index", "text",
            "x0", "x1", "top", "bottom",
            "upright", "width", "height",
        ])
        for page in doc.pages:
            for idx, w in enumerate(page.words):
                writer.writerow([
                    page.page_index + 1, idx, w.text,
                    f"{w.x0:.2f}", f"{w.x1:.2f}",
                    f"{w.top:.2f}", f"{w.bottom:.2f}",
                    "1" if w.upright else "0",
                    f"{w.width:.2f}", f"{w.height:.2f}",
                ])
                rows += 1
    return rows


# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------

def diagnose_batch(folder: Path, output_csv: Path) -> List[FileDiagnostic]:
    """Run summary on every PDF in `folder` and aggregate into one CSV."""
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        return []

    diagnostics: List[FileDiagnostic] = []
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "pdf_name", "pages",
            "candidate_table_pages", "has_rotation",
            "analytes_found", "score",
            "primary_table_label", "primary_table_page",
            "mean_sd_matches", "estimate_ci_matches",
            "verdict",
        ])
        for pdf in pdfs:
            try:
                diag = diagnose_summary(pdf)
            except Exception as exc:  # noqa: BLE001
                print(f"  [ERROR] {pdf.name}: {exc}", file=sys.stderr)
                continue
            diagnostics.append(diag)

            # Save per-file summary text alongside the PDF
            txt_path = pdf.with_name(pdf.stem + "_diagnose.txt")
            txt_path.write_text(format_summary(diag), encoding="utf-8")

            # Aggregate row
            candidate_pages = [
                str(p.page_number) for p in diag.pages
                if p.is_candidate_bv_page
            ]
            writer.writerow([
                pdf.name,
                diag.page_count,
                ",".join(candidate_pages) or "",
                "yes" if diag.has_rotation else "no",
                ",".join(diag.all_analytes),
                f"{diag.primary_table_score:.1f}",
                diag.primary_table_label or "",
                diag.primary_table_page or "",
                diag.mean_sd_match_count,
                diag.estimate_ci_match_count,
                diag.verdict,
            ])
    return diagnostics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bv-diagnose",
        description=(
            "Inspect PDF articles before running the full BV extractor. "
            "Useful for triaging which articles will work out of the box "
            "and which need parser tweaks."
        ),
    )
    p.add_argument(
        "input",
        type=Path,
        help="Path to a PDF file (summary/dump modes) or a folder of PDFs (batch mode).",
    )
    p.add_argument(
        "--mode",
        choices=["summary", "dump", "batch"],
        default="summary",
        help="Diagnostic mode (default: summary).",
    )
    p.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help=(
            "Optional output path. summary -> .txt; dump -> .csv; "
            "batch -> aggregate .csv. Defaults are derived from the input."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    if args.mode == "summary":
        if not args.input.is_file():
            print("ERROR: summary mode expects a single PDF file.", file=sys.stderr)
            return 1
        diag = diagnose_summary(args.input)
        text = format_summary(diag)
        print(text)
        out_path = args.output or args.input.with_name(
            args.input.stem + "_diagnose.txt"
        )
        out_path.write_text(text, encoding="utf-8")
        print(f"Wrote: {out_path}")
        return 0

    if args.mode == "dump":
        if not args.input.is_file():
            print("ERROR: dump mode expects a single PDF file.", file=sys.stderr)
            return 1
        out_path = args.output or args.input.with_name(
            args.input.stem + "_words.csv"
        )
        n = dump_words(args.input, out_path)
        print(f"Wrote {n} word rows to: {out_path}")
        return 0

    # batch
    if not args.input.is_dir():
        print("ERROR: batch mode expects a folder of PDFs.", file=sys.stderr)
        return 1
    out_path = args.output or args.input / "batch_summary.csv"
    diags = diagnose_batch(args.input, out_path)
    if not diags:
        print(f"No PDFs found in {args.input}.")
        return 0
    print(f"Diagnosed {len(diags)} PDF(s).")
    print(f"Aggregate summary: {out_path}")
    print(f"Per-file summaries: <stem>_diagnose.txt next to each PDF.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
