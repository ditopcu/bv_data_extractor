"""
Top-level extraction pipeline.

The `extract` function ties together the deterministic stages:

    load_pdf
        -> find_bv_table          (locate the BV results table)
        -> parse_columns          (build column-role -> ox-range map)
        -> find_analyte_rows      (one anchor word per analyte)
        -> parse_analyte_row * N  (per-analyte field extraction)
        -> extract_methods_by_analyte  (assign analytical method per analyte)
        -> extract_dataset_details     (study-level fields)
        -> validate                    (QC checks)

The result is a fully-populated `ExtractionResult` ready to be written
out as Excel / JSON / report. The function never raises on missing fields;
they are surfaced as warnings in the result instead.

LLM fallback (if requested) is invoked here at the end. v1 ships only a
stub so the architecture is in place but no network calls are made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .dataset_extractor import extract_dataset_details
from .header_parser import find_data_top_oy, parse_columns
from .methods_extractor import extract_methods_by_analyte
from .pdf_io import load_pdf
from .row_parser import find_analyte_rows, parse_analyte_row
from .schema import (
    AnalyteRecord,
    ExtractionReport,
    ExtractionResult,
    FieldValue,
)
from .table_finder import KNOWN_ANALYTES, find_bv_table
from .validator import validate


# Canonical names per known analyte abbreviation
ANALYTE_FULL_NAMES = {
    "tg": "Triglyceride",
    "tc": "Total cholesterol",
    "ldl-c": "Low-density lipoprotein cholesterol",
    "hdl-c": "High-density lipoprotein cholesterol",
    "apo-a1": "Apolipoprotein A1",
    "apo-b": "Apolipoprotein B",
    "triglyceride": "Triglyceride",
    "cholesterol": "Cholesterol",
    "creatinine": "Creatinine",
    "urea": "Urea",
    "glucose": "Glucose",
}

# Canonical reporting unit per known analyte abbreviation. v1 only knows
# lipid-panel units; for unknown analytes the unit is left blank.
ANALYTE_UNITS = {
    "tg": "mmol/L",
    "tc": "mmol/L",
    "ldl-c": "mmol/L",
    "hdl-c": "mmol/L",
    "apo-a1": "g/L",
    "apo-b": "g/L",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(
    pdf_path: str | Path,
    use_llm_fallback: bool = False,
) -> ExtractionResult:
    """Run the full deterministic pipeline on `pdf_path`.

    `use_llm_fallback` is reserved for a future LLM-assisted step that
    fills in fields the deterministic pipeline could not extract. v1
    ships the flag but does not yet call out to an LLM.
    """
    pdf_path = Path(pdf_path)
    result = ExtractionResult()
    result.report.source_file = str(pdf_path)

    # ---- 1. Load PDF -----------------------------------------------------
    doc = load_pdf(pdf_path)

    # ---- 2. Locate the BV results table ---------------------------------
    table_loc = find_bv_table(doc)
    if table_loc is None:
        result.report.warnings.append(
            "No biological-variation results table could be identified in "
            "this PDF. Check that the article reports CV_I / CV_G values "
            "for at least two analytes; the extractor cannot proceed."
        )
        # Still try to extract dataset / methods
        _extract_study_level(doc, result)
        return result

    result.report.primary_table_label = table_loc.label
    result.report.primary_table_page = table_loc.page_index + 1
    result.report.primary_table_was_rotated = table_loc.is_rotated

    # ---- 3. Build the column map ---------------------------------------
    words = table_loc.words
    data_top = find_data_top_oy(words, KNOWN_ANALYTES)
    if data_top is None:
        result.report.warnings.append(
            "The BV results table was located but no recognised analyte "
            "names were found in its data area. Extraction cannot proceed."
        )
        _extract_study_level(doc, result)
        return result

    columns = parse_columns(words, data_top)
    if not _has_minimum_columns(columns):
        result.report.warnings.append(
            "The header parser did not find the four core Current-study "
            "sub-columns (Mean±SD, CV_A, CV_I, CV_G). Extracted values "
            "for this table may be incomplete."
        )

    # ---- 4. Iterate analyte rows ---------------------------------------
    analyte_col_max = next(
        (c.ox_max for c in columns if c.role == "analyte"),
        None,
    )
    analyte_words = find_analyte_rows(
        words, KNOWN_ANALYTES,
        analyte_col_ox_max=analyte_col_max,
        data_top_oy=data_top,
    )

    for aw in analyte_words:
        rec = parse_analyte_row(aw, words, columns)
        _enrich_analyte(rec)
        result.analytes.append(rec)
        result.report.detected_analytes.append(rec.abbreviation)

    # ---- 5. Methods (per analyte) ---------------------------------------
    methods = extract_methods_by_analyte(doc)
    for rec in result.analytes:
        method = methods.get(rec.abbreviation.lower(), "")
        rec.method = method

    # ---- 6. Dataset details --------------------------------------------
    _extract_study_level(doc, result)

    # ---- 7. QC validation ----------------------------------------------
    validate(result.analytes, result.report)

    # ---- 8. Build the human-readable inventory of extracted/blank fields
    _summarise_field_status(result)

    # ---- 9. LLM fallback (placeholder) ---------------------------------
    if use_llm_fallback:
        _try_llm_fallback(result, doc)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_minimum_columns(columns) -> bool:
    """True when at least the four current-study sub-columns are present."""
    roles = {c.role for c in columns if c.is_current_study}
    return roles >= {"mean_sd", "cva", "cvi", "cvg"}


def _enrich_analyte(rec: AnalyteRecord) -> None:
    """Fill name and reporting unit from canonical lookup tables.

    Leaves both blank for unknown abbreviations rather than guessing.
    """
    key = rec.abbreviation.lower()
    if key in ANALYTE_FULL_NAMES:
        rec.name = ANALYTE_FULL_NAMES[key]
    if key in ANALYTE_UNITS:
        rec.unit = ANALYTE_UNITS[key]


def _extract_study_level(doc, result: ExtractionResult) -> None:
    """Populate dataset-level fields and propagate their warnings."""
    ds = extract_dataset_details(doc)
    result.dataset = ds
    for w in ds.warnings:
        result.report.manual_review.append(w)


def _summarise_field_status(result: ExtractionResult) -> None:
    """Build the report.fields_extracted / fields_blank inventories."""
    monitored = [
        "cvi", "cvi_ci_lower", "cvi_ci_upper",
        "cvg", "cvg_ci_lower", "cvg_ci_upper",
        "analytical_cv",
        "measurand_mean", "measurand_sd",
    ]
    for rec in result.analytes:
        for fname in monitored:
            fv: FieldValue = getattr(rec, fname)
            tag = f"{rec.abbreviation}.{fname}"
            if fv.value is None:
                result.report.fields_blank.append(tag)
                if fv.warning:
                    result.report.warnings.append(f"{tag}: {fv.warning}")
            else:
                result.report.fields_extracted.append(tag)


def _try_llm_fallback(result: ExtractionResult, doc) -> None:
    """LLM-assisted fallback (not implemented in v1).

    The architecture is in place: when implemented, this function would
    call out to an LLM with the list of blank fields and relevant text
    snippets, and merge the LLM's responses back into the result with
    `source='llm'`. Until then, it is a no-op that records a note.
    """
    result.report.warnings.append(
        "LLM fallback was requested but is not implemented in this "
        "version of the extractor."
    )