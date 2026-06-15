"""
Command-line interface for the BV extractor.

Usage:
    python -m bv_extractor.cli path/to/article.pdf -o output_dir/

By default the CLI runs in *smart* routing mode:

    1. Pre-analyse the PDF (preanalyzer) to locate the BV table and classify
       its reporting format.
    2. If the format is one the deterministic parser handles well (mean_sd /
       hybrid, e.g. Yang 2018), parse it deterministically — fast and free.
    3. Otherwise (value_ci, tabular_plain, rotated, unknown, or the parser
       returns nothing), open the interactive picker so the user draws a box
       around the real table, then extract that region with Claude vision.

Engines can be forced with --engine. Interactive table picking is on by
default; pass --no-interactive to skip the GUI and let Claude read the whole
detected page instead.

Produces three files in the output directory (default: ./output):
    <stem>.xlsx  : Wide BV-values sheet + Dataset + Report sheets
    <stem>.json  : Long-form JSON with provenance per field
    <stem>.txt   : Plain-text human-readable extraction report

Exit codes:
    0 : success (even if some fields were left blank with warnings)
    1 : a fatal error occurred (e.g., the PDF could not be opened)
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Optional, Tuple

from .outputs.excel_writer import write_excel
from .outputs.json_writer import write_json
from .outputs.report_writer import format_report, write_report
from .pipeline import extract
from .preanalyzer import (
    FORMAT_HYBRID,
    FORMAT_MEAN_SD,
    VERDICT_EXTRACTABLE,
    analyze,
)
from .schema import ExtractionResult


# Formats the deterministic row parser handles reliably today.
_DETERMINISTIC_FORMATS = {FORMAT_MEAN_SD, FORMAT_HYBRID}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bv-extract",
        description=(
            "Extract biological variation data from a scientific PDF article "
            "into Excel + JSON + text-report files ready for web-form data entry."
        ),
    )
    p.add_argument(
        "pdf",
        type=Path,
        nargs="?",
        default=None,
        help="Path to the input PDF file. If omitted, a file picker opens.",
    )
    p.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("output"),
        help="Directory to write outputs into (default: ./output).",
    )
    p.add_argument(
        "--engine",
        choices=["auto", "deterministic", "claude"],
        default="auto",
        help=(
            "Extraction engine. 'auto' (default): deterministic parser for "
            "recognised formats, Claude vision otherwise. 'deterministic': "
            "force the regex parser. 'claude': force Claude vision."
        ),
    )
    p.add_argument(
        "--no-interactive",
        action="store_true",
        help=(
            "Disable the table-picker GUI. On the Claude path the whole "
            "detected page is sent instead of a user-selected region."
        ),
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the Claude model id (default: claude-opus-4-8).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable report on stdout.",
    )
    return p


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _resolve_region(
    pdf: Path, default_page_index: int, interactive: bool
) -> Tuple[int, int, Optional[Tuple[float, float, float, float]]]:
    """Return (page_index, rotation, bbox_frac) for the Claude path.

    With `interactive`, open the picker GUI (starting on the detected page) and
    use whatever region/rotation the user chooses. If the user cancels, or
    interactive is off, fall back to the whole detected page upright.
    """
    if not interactive:
        return default_page_index, 0, None

    # Lazy import: only pull in tkinter when the GUI is actually needed.
    from .interactive_picker import pick

    print(
        "STEP 2/4 — Pick the table: draw a box around the BV table "
        "(use Rotate ⟳ / 'r' for sideways tables), then press Enter. "
        "Esc uses the whole page.",
        file=sys.stderr,
    )
    selection = pick(pdf, initial_page=default_page_index)
    if selection is None:
        print(
            "No selection made; sending the whole detected page to Claude.",
            file=sys.stderr,
        )
        return default_page_index, 0, None
    return selection.page_index, selection.rotation, tuple(selection.bbox_frac)


def _claude_path(
    pdf: Path, default_page_index: int, interactive: bool, model: Optional[str]
) -> ExtractionResult:
    from .claude_extractor import DEFAULT_MODEL, extract_with_claude

    page_index, rotation, bbox_frac = _resolve_region(
        pdf, default_page_index, interactive
    )
    print("STEP 3/4 — Sending the table to Claude for extraction...",
          file=sys.stderr)
    return extract_with_claude(
        pdf, page_index, rotation=rotation, bbox_frac=bbox_frac,
        model=model or DEFAULT_MODEL,
    )


def _route_and_extract(args) -> ExtractionResult:
    """Pick an engine per the smart routing rules and run it."""
    pdf = args.pdf
    interactive = not args.no_interactive

    if args.engine == "deterministic":
        return extract(pdf)

    # Pre-analyse to locate the table and classify the format. Used by both
    # the 'claude' (for the starting page) and 'auto' branches.
    try:
        profile = analyze(pdf)
        default_page = (
            (profile.primary_table_page - 1)
            if profile.primary_table_page else 0
        )
    except Exception as exc:  # noqa: BLE001
        print(f"(pre-analysis failed: {exc}; starting at page 1)",
              file=sys.stderr)
        profile = None
        default_page = 0

    if args.engine == "claude":
        return _claude_path(pdf, default_page, interactive, args.model)

    # --- auto -----------------------------------------------------------
    deterministic_ok = (
        profile is not None
        and profile.verdict == VERDICT_EXTRACTABLE
        and profile.format_class in _DETERMINISTIC_FORMATS
    )
    if deterministic_ok:
        print(
            f"Format '{profile.format_class}' is parser-friendly; trying the "
            "deterministic engine.",
            file=sys.stderr,
        )
        result = extract(pdf)
        if result.analytes:
            return result
        print(
            "Deterministic parser found no analytes; falling back to Claude.",
            file=sys.stderr,
        )
    else:
        reason = (
            f"format '{profile.format_class}', verdict '{profile.verdict}'"
            if profile else "pre-analysis unavailable"
        )
        print(
            f"Deterministic parser not suitable ({reason}); using Claude.",
            file=sys.stderr,
        )

    return _claude_path(pdf, default_page, interactive, args.model)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _prompt_for_pdf() -> Optional[Path]:
    """Open a file-open dialog and return the chosen PDF (step 1)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:  # noqa: BLE001
        return None
    root = tk.Tk()
    root.withdraw()
    chosen = filedialog.askopenfilename(
        title="Select a BV article PDF",
        filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
    )
    root.destroy()
    return Path(chosen) if chosen else None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --- STEP 1/4: choose the PDF ---------------------------------------
    if args.pdf is None:
        print("STEP 1/4 — Select a PDF...", file=sys.stderr)
        args.pdf = _prompt_for_pdf()
        if args.pdf is None:
            print("No PDF selected.", file=sys.stderr)
            return 1
    if not args.pdf.exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    try:
        result = _route_and_extract(args)
    except Exception as exc:  # noqa: BLE001 - surface anything that escapes
        print(f"ERROR: extraction failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    # --- STEP 4/4: save copyable results --------------------------------
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.pdf.stem

    xlsx_path  = write_excel(result,  args.output_dir / f"{stem}.xlsx")
    json_path  = write_json(result,   args.output_dir / f"{stem}.json")
    txt_path   = write_report(result, args.output_dir / f"{stem}.txt")

    if not args.quiet:
        print(format_report(result))

    print("STEP 4/4 — Saved (xlsx for the form, json for data, txt to copy):",
          file=sys.stderr)
    print(f"Wrote: {xlsx_path}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {txt_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
