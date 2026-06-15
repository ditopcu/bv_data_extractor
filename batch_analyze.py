"""
Batch pre-analysis of a folder of PDFs — NO LLM calls.

Runs the deterministic pre-analyzer (`bv_extractor.preanalyzer.analyze`) on
every PDF in a directory and writes a CSV summary plus a console table. This
is a diagnostic to understand the corpus (which papers are parser-friendly,
which formats/geometries appear, where the table is, where detection is
wrong) BEFORE doing any extraction. It never contacts Claude.

Usage:
    python batch_analyze.py                         # scans sample_data/All_PDFs
    python batch_analyze.py <folder>
    python batch_analyze.py <folder> -o report.csv
    python batch_analyze.py <folder> --recursive

For each PDF the CSV records: page count, verdict, format class, geometry,
detected table page, score, whether the page was relocated, per-candidate-page
format-hit counts, document-wide pattern counts, and the number of recognised
analytes. Files that fail to load get an `error` row instead of crashing the run.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import List, Optional

from bv_extractor.preanalyzer import analyze


# Matches a 4-digit publication year (1900–2099) embedded in a filename.
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def extract_year(name: str) -> Optional[int]:
    """Best-effort publication year from a filename (the latest year found)."""
    years = [int(m.group()) for m in _YEAR_RE.finditer(name)]
    return max(years) if years else None


# CSV column order
FIELDS = [
    "file",
    "year",
    "pages",
    "verdict",
    "format",
    "geometry",
    "table_page",
    "score",
    "relocated_from",
    "cand_mean_sd",
    "cand_value_ci",
    "cand_paren_sd",
    "doc_mean_sd",
    "doc_value_ci",
    "n_analytes",
    "has_rotation",
    "error",
]


def _row_for(pdf: Path) -> dict:
    """Run pre-analysis on one PDF and return a flat dict of its profile.

    Any exception is captured into the 'error' column so one bad file does
    not abort the whole batch.
    """
    row = {k: "" for k in FIELDS}
    row["file"] = pdf.name
    year = extract_year(pdf.name)
    row["year"] = year if year else ""
    try:
        p = analyze(pdf)
        row.update(
            pages=p.page_count,
            verdict=p.verdict,
            format=p.format_class,
            geometry=p.geometry_class,
            table_page=p.primary_table_page if p.primary_table_page else "",
            score=f"{p.primary_table_score:.0f}",
            relocated_from=p.relocated_from_page if p.relocated_from_page else "",
            cand_mean_sd=p.candidate_mean_sd_hits,
            cand_value_ci=p.candidate_value_ci_hits,
            cand_paren_sd=p.candidate_paren_sd_hits,
            doc_mean_sd=p.doc_mean_sd_match_count,
            doc_value_ci=p.doc_estimate_ci_match_count,
            n_analytes=len(p.all_analytes),
            has_rotation=p.has_rotation,
        )
    except Exception as exc:  # noqa: BLE001 - record and continue
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def _collect_pdfs(folder: Path, recursive: bool) -> List[Path]:
    pattern = "**/*.pdf" if recursive else "*.pdf"
    return sorted(folder.glob(pattern), key=lambda p: p.name.lower())


def _print_summary(rows: List[dict]) -> None:
    """Print per-verdict and per-format tallies."""
    def tally(key: str) -> dict:
        counts: dict = {}
        for r in rows:
            v = r["error"] and "ERROR" or (r[key] or "—")
            counts[v] = counts.get(v, 0) + 1
        return counts

    print("\n=== Verdict counts ===")
    for k, n in sorted(tally("verdict").items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {k}")
    print("\n=== Format counts ===")
    for k, n in sorted(tally("format").items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {k}")

    n_err = sum(1 for r in rows if r["error"])
    n_rot = sum(1 for r in rows if r["has_rotation"] is True)
    print(f"\nTotal PDFs : {len(rows)}")
    print(f"Errors     : {n_err}")
    print(f"Rotated    : {n_rot}")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch pre-analysis of PDFs (no LLM). Writes a CSV summary."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        type=Path,
        default=Path("sample_data/All_PDFs"),
        help="Folder of PDFs (default: sample_data/All_PDFs).",
    )
    parser.add_argument(
        "-o", "--out",
        type=Path,
        default=Path("batch_analysis.csv"),
        help="CSV output path (default: batch_analysis.csv).",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recurse into subfolders.",
    )
    parser.add_argument(
        "--min-year",
        type=int,
        default=None,
        help=(
            "Only analyse papers whose filename year is >= this (e.g. 2019). "
            "Files with no detectable year are skipped."
        ),
    )
    args = parser.parse_args(argv)

    # Console may be a legacy code page (e.g. cp1254) that can't encode the
    # Unicode characters in some filenames; force UTF-8 and never crash on print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if not args.folder.is_dir():
        print(f"ERROR: not a folder: {args.folder}", file=sys.stderr)
        return 1

    pdfs = _collect_pdfs(args.folder, args.recursive)
    if not pdfs:
        print(f"No PDFs found in {args.folder}", file=sys.stderr)
        return 1

    if args.min_year is not None:
        kept, no_year, too_old = [], 0, 0
        for p in pdfs:
            y = extract_year(p.name)
            if y is None:
                no_year += 1
            elif y < args.min_year:
                too_old += 1
            else:
                kept.append(p)
        print(
            f"Year filter >= {args.min_year}: kept {len(kept)}, "
            f"skipped {too_old} older + {no_year} with no detectable year."
        )
        pdfs = kept
        if not pdfs:
            print("Nothing to analyse after the year filter.", file=sys.stderr)
            return 1

    print(f"Analysing {len(pdfs)} PDF(s) in {args.folder} (no LLM)…\n")
    rows: List[dict] = []
    for i, pdf in enumerate(pdfs, 1):
        row = _row_for(pdf)
        rows.append(row)
        status = row["error"] or (
            f"{row['verdict']} / {row['format']} / {row['geometry']} "
            f"/ p{row['table_page']} / score {row['score']}"
        )
        # Trim long filenames for the console; full name is in the CSV.
        name = pdf.name if len(pdf.name) <= 50 else pdf.name[:47] + "…"
        print(f"[{i:3d}/{len(pdfs)}] {name:<51} {status}")

    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    _print_summary(rows)
    print(f"\nWrote: {args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
