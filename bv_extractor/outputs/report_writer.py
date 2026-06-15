"""
Plain-text extraction report.

This is the human-facing summary printed at the end of every run and
also saved alongside the Excel/JSON files. Its goal is to tell the user
exactly what was extracted, what was left blank, and why — without
forcing them to open the Excel file to see the details.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..schema import ExtractionResult, FieldValue


def write_report(result: ExtractionResult, output_path: str | Path) -> Path:
    """Save the report to `output_path` and return the Path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(format_report(result), encoding="utf-8")
    return output_path


def format_report(result: ExtractionResult) -> str:
    """Build the report as a single string. Used for both file + console."""
    lines: List[str] = []
    rep = result.report

    lines.append("=" * 72)
    lines.append("Biological Variation Extraction Report")
    lines.append("=" * 72)
    lines.append(f"Source file              : {rep.source_file}")
    if rep.article_title:
        lines.append(f"Article title            : {rep.article_title}")
    lines.append(f"Primary BV table         : {rep.primary_table_label or 'not identified'}")
    lines.append(f"Primary BV table page    : {rep.primary_table_page or 'n/a'}")
    lines.append(f"Table was rotated        : {rep.primary_table_was_rotated}")
    lines.append(f"LLM fallback used        : {rep.used_llm_fallback}")
    lines.append("")

    # ---- Per-analyte breakdown -----------------------------------------
    lines.append("-" * 72)
    lines.append("Extracted analytes")
    lines.append("-" * 72)
    if not result.analytes:
        lines.append("  (none)")
    for a in result.analytes:
        lines.append(f"  {a.abbreviation}  ({a.name or 'unknown name'}, "
                     f"unit: {a.unit or 'unknown'})")
        lines.append(f"      Method     : {a.method or '<not extracted>'}")
        lines.append(f"      CVI        : {_show(a.cvi)}")
        lines.append(f"      CVI 95% CI : {_show_ci(a.cvi_ci_lower, a.cvi_ci_upper)}")
        lines.append(f"      CVG        : {_show(a.cvg)}")
        lines.append(f"      CVG 95% CI : {_show_ci(a.cvg_ci_lower, a.cvg_ci_upper)}")
        lines.append(f"      CVA        : {_show(a.analytical_cv)}")
        lines.append(f"      Mean       : {_show(a.measurand_mean)}")
        lines.append(f"      SD         : {_show(a.measurand_sd)}")
        for note in a.notes:
            lines.append(f"      [QC] {note}")
        lines.append("")

    # ---- Dataset details ------------------------------------------------
    lines.append("-" * 72)
    lines.append("Dataset details")
    lines.append("-" * 72)
    ds = result.dataset
    pairs = [
        ("Matrix",                   ds.matrix),
        ("Number of subjects",       ds.number_of_subjects),
        ("Males / Females",          f"{ds.number_of_males} / {ds.number_of_females}"
                                     if ds.number_of_males or ds.number_of_females
                                     else None),
        ("Ethnicity",                ds.ethnicity),
        ("State of well-being",      ds.state_of_well_being),
        ("Samples per participant",  ds.samples_per_participant),
        ("Replicates",               ds.avg_replicates),
        ("Sampling start time",      ds.sampling_start_time),
        ("Sampling end time",        ds.sampling_end_time),
        ("Sampling intervals",       ds.sampling_intervals),
    ]
    for k, v in pairs:
        lines.append(f"  {k:25s}: {v if v not in (None, '', 'None / None') else '<not extracted>'}")
    lines.append("")

    # ---- Field-status summary -------------------------------------------
    lines.append("-" * 72)
    lines.append("Field-status summary")
    lines.append("-" * 72)
    lines.append(f"  Fields extracted successfully: {len(rep.fields_extracted)}")
    lines.append(f"  Fields left blank            : {len(rep.fields_blank)}")
    if rep.fields_blank:
        lines.append("  Blank fields:")
        for tag in rep.fields_blank:
            lines.append(f"    - {tag}")
    lines.append("")

    # ---- Warnings & manual review --------------------------------------
    if rep.warnings:
        lines.append("-" * 72)
        lines.append("Warnings")
        lines.append("-" * 72)
        for w in rep.warnings:
            lines.append(f"  - {w}")
        lines.append("")

    if rep.manual_review:
        lines.append("-" * 72)
        lines.append("Manual review recommended")
        lines.append("-" * 72)
        for note in rep.manual_review:
            lines.append(f"  - {note}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Pretty-printing helpers
# ---------------------------------------------------------------------------

def _show(fv: FieldValue) -> str:
    if fv.value is not None:
        return f"{fv.value}"
    if fv.warning:
        return f"<blank> ({fv.warning})"
    return "<blank>"


def _show_ci(lo: FieldValue, hi: FieldValue) -> str:
    if lo.value is not None and hi.value is not None:
        return f"[{lo.value}, {hi.value}]"
    msg = lo.warning or hi.warning or "no CI reported"
    return f"<blank> ({msg})"