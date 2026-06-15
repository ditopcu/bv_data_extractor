"""
Excel writer for an ExtractionResult.

Produces a workbook with three sheets:

  1. "BV values"         : rows = web-form field names, columns = analytes.
                           Cells contain the extracted numeric values, or
                           are left blank when the deterministic pipeline
                           could not extract the field.
  2. "Dataset details"   : key/value pairs from DatasetDetails.
  3. "Extraction report" : the same content as the human-readable .txt
                           report, but in tabular form for record-keeping.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pandas as pd

from ..schema import AnalyteRecord, ExtractionResult, FieldValue


# Display order for the "BV values" sheet, mirroring the web form
FIELD_ROWS: List[tuple[str, str]] = [
    ("Estimates of CVi",                 "cvi"),
    ("Lower reported CI of CVi",         "cvi_ci_lower"),
    ("Upper reported CI of CVi",         "cvi_ci_upper"),
    ("Estimates of CVg",                 "cvg"),
    ("Lower reported CI of CVg",         "cvg_ci_lower"),
    ("Upper reported CI of CVg",         "cvg_ci_upper"),
    ("Analytical CV",                    "analytical_cv"),
    ("Measurand Mean",                   "measurand_mean"),
    ("Measurand SD",                     "measurand_sd"),
    ("Measurand Min",                    "measurand_min"),
    ("Measurand Max",                    "measurand_max"),
    ("Measurand Standard Unit Mean",     "measurand_std_unit_mean"),
    ("Measurand Standard Unit SD",       "measurand_std_unit_sd"),
    ("Measurand Standard Unit Min",      "measurand_std_unit_min"),
    ("Measurand Standard Unit Max",      "measurand_std_unit_max"),
    ("Unit",                             "_unit"),
    ("Method",                           "_method"),
    ("Analyte (full name)",              "_name"),
]


def write_excel(result: ExtractionResult, output_path: str | Path) -> Path:
    """Write the ExtractionResult to `output_path` and return the Path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    bv_df = _build_bv_dataframe(result.analytes)
    ds_df = _build_dataset_dataframe(result)
    rep_df = _build_report_dataframe(result)

    with pd.ExcelWriter(output_path, engine="openpyxl") as xw:
        bv_df.to_excel(xw, sheet_name="BV values", index_label="Field")
        ds_df.to_excel(xw, sheet_name="Dataset details", index=False)
        rep_df.to_excel(xw, sheet_name="Extraction report", index=False)

    return output_path


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------

def _build_bv_dataframe(analytes: List[AnalyteRecord]) -> pd.DataFrame:
    """Wide table: rows are form fields, columns are analyte abbreviations."""
    cols = [a.abbreviation for a in analytes]
    rows = [label for label, _ in FIELD_ROWS]
    df = pd.DataFrame(index=rows, columns=cols, dtype=object)

    for a in analytes:
        for label, attr in FIELD_ROWS:
            df.at[label, a.abbreviation] = _value_for(a, attr)
    return df


def _value_for(a: AnalyteRecord, attr: str) -> object:
    """Extract the displayable value for one (analyte, field) cell."""
    if attr == "_name":
        return a.name or ""
    if attr == "_unit":
        return a.unit or ""
    if attr == "_method":
        return a.method or ""

    fv: FieldValue = getattr(a, attr)
    return fv.value if fv.value is not None else ""


def _build_dataset_dataframe(result: ExtractionResult) -> pd.DataFrame:
    """Dataset details as a two-column key/value table."""
    ds = result.dataset
    rep = result.report

    rows = [
        ("Source file",                       rep.source_file),
        ("Article title",                     rep.article_title or ""),
        ("Primary BV table",                  rep.primary_table_label or ""),
        ("Primary BV table page",             rep.primary_table_page or ""),
        ("Table was rotated on the page",     rep.primary_table_was_rotated),
        ("Matrix",                            ds.matrix or ""),
        ("Number of subjects",                ds.number_of_subjects or ""),
        ("Number of males",                   ds.number_of_males or ""),
        ("Number of females",                 ds.number_of_females or ""),
        ("Number subjects in BV estimation",  ds.number_subjects_in_bv_estimation or ""),
        ("Ethnicity",                         ds.ethnicity or ""),
        ("State of well-being",               ds.state_of_well_being or ""),
        ("Disease state",                     ds.disease_state or ""),
        ("Age mean",                          ds.age_mean or ""),
        ("Age SD",                            ds.age_sd or ""),
        ("Age min",                           ds.age_min or ""),
        ("Age max",                           ds.age_max or ""),
        ("Total study duration",              ds.total_study_duration or ""),
        ("Study duration units",              ds.study_duration_units or ""),
        ("Samples per participant",           ds.samples_per_participant or ""),
        ("Sampling intervals",                ds.sampling_intervals or ""),
        ("Sampling start time",               ds.sampling_start_time or ""),
        ("Sampling end time",                 ds.sampling_end_time or ""),
        ("Avg samples used for BV",           ds.avg_samples_used_for_bv or ""),
        ("Avg replicates",                    ds.avg_replicates or ""),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def _build_report_dataframe(result: ExtractionResult) -> pd.DataFrame:
    """Tabular form of the extraction report contents."""
    rep = result.report

    rows: List[tuple[str, str]] = []
    rows.append(("Detected analytes", ", ".join(rep.detected_analytes)))
    rows.append(("Used LLM fallback", str(rep.used_llm_fallback)))

    for tag in rep.fields_extracted:
        rows.append(("Extracted", tag))
    for tag in rep.fields_blank:
        rows.append(("Blank", tag))
    for w in rep.warnings:
        rows.append(("Warning", w))
    for note in rep.manual_review:
        rows.append(("Manual review", note))

    # Per-analyte QC notes
    for a in result.analytes:
        for note in a.notes:
            rows.append((f"QC note ({a.abbreviation})", note))

    return pd.DataFrame(rows, columns=["Type", "Detail"])