"""
Claude vision fallback for BV-table extraction.

When the deterministic parser can't handle an article (value_ci, tabular_plain,
rotated, or unknown formats), this module renders the selected page region as
an image and asks Claude to read the biological-variation table directly.

The flow is:

    render page (or bbox region) -> PNG  ->  Claude vision (structured output)
        -> ExtractionResult with source="llm" on every field

It reads the API key from the ANTHROPIC_API_KEY environment variable; the key
is never written to disk or hard-coded. The model is Opus 4.8 by default, which
supports the structured-output (`output_config.format`) and high-resolution
vision used here.

Design rules carried over from the deterministic pipeline:

  * Never guess a missing field. If Claude can't find a value it returns null;
    the field stays empty with a warning rather than being invented.
  * Every produced FieldValue records source="llm" and the raw row text Claude
    read it from, so the output stays auditable.

This module is standalone and does not touch table_finder / row_parser, so the
Yang 2018 regression suite is unaffected.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Optional, Tuple

import pypdfium2 as pdfium

from .schema import (
    AnalyteRecord,
    DatasetDetails,
    ExtractionResult,
    FieldValue,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-4-8"

# Opus 4.8 standard pricing (USD per million tokens), for a rough cost estimate.
# Update if the model or rates change.
PRICE_INPUT_PER_MTOK = 5.0
PRICE_OUTPUT_PER_MTOK = 25.0


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Rough USD cost for the given token counts at Opus 4.8 standard rates."""
    return (
        input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
        + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK
    )

# Long edge (in pixels) of the rendered image. Opus 4.8 supports high-res
# vision; ~2200 px keeps tables legible without inflating the token cost.
_TARGET_LONG_EDGE = 2200

# Hard cap on the pdfium render scale so a small page isn't blown up absurdly.
_MAX_RENDER_SCALE = 4.0


# Bbox is (x0, y0, x1, y1) in PDF points, top-left origin (pdfplumber /
# pypdfium2 convention) — exactly what interactive_picker writes.
Bbox = Tuple[float, float, float, float]


# ---------------------------------------------------------------------------
# Output schema (JSON Schema for output_config.format)
# ---------------------------------------------------------------------------
#
# Structured-output constraints: every object needs additionalProperties=false
# and all properties listed in `required`. Structured outputs also cap the
# number of union-typed (nullable / anyOf) parameters at 16, so instead of
# nullable numbers we make every field a plain string: the model writes the
# number as printed, or an empty string when the value is absent. Python then
# parses the strings (empty -> None). This keeps the schema union-free and
# lets the model preserve the raw token it read.

_NUM_STR = {
    "type": "string",
    "description": "Numeric value exactly as printed (digits only, e.g. "
                   "'5.2'); empty string if not reported in the table.",
}
_TEXT_STR = {
    "type": "string",
    "description": "Value as printed; empty string if not reported.",
}

_ANALYTE_PROPS = {
    "name": {"type": "string"},
    "abbreviation": {"type": "string"},
    "unit": {"type": "string"},
    "method": {"type": "string"},
    "cvi": _NUM_STR,
    "cvi_ci_lower": _NUM_STR,
    "cvi_ci_upper": _NUM_STR,
    "cvg": _NUM_STR,
    "cvg_ci_lower": _NUM_STR,
    "cvg_ci_upper": _NUM_STR,
    "analytical_cv": _NUM_STR,
    "measurand_mean": _NUM_STR,
    "measurand_sd": _NUM_STR,
    "source_text": {
        "type": "string",
        "description": "The exact row text in the table this analyte was read "
                       "from, for auditing. Empty string if not applicable.",
    },
}

_DATASET_PROPS = {
    "matrix": _TEXT_STR,
    "number_of_subjects": _NUM_STR,
    "number_of_males": _NUM_STR,
    "number_of_females": _NUM_STR,
    "ethnicity": _TEXT_STR,
    "state_of_well_being": _TEXT_STR,
    "age_mean": _NUM_STR,
    "age_min": _NUM_STR,
    "age_max": _NUM_STR,
    "total_study_duration": _NUM_STR,
    "study_duration_units": _TEXT_STR,
    "samples_per_participant": _NUM_STR,
    "sampling_intervals": _TEXT_STR,
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "analytes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _ANALYTE_PROPS,
                "required": list(_ANALYTE_PROPS.keys()),
                "additionalProperties": False,
            },
        },
        "dataset": {
            "type": "object",
            "properties": _DATASET_PROPS,
            "required": list(_DATASET_PROPS.keys()),
            "additionalProperties": False,
        },
        "table_notes": {
            "type": "string",
            "description": "Brief note on anything ambiguous or worth manual "
                           "review (units, footnotes, mixed conventions).",
        },
    },
    "required": ["analytes", "dataset", "table_notes"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a meticulous data-extraction assistant for laboratory-medicine "
    "biological-variation (BV) studies. You read a table image and return "
    "structured values. BV terminology: CVI = within-subject biological "
    "variation, CVG = between-subject biological variation, CVA = analytical "
    "variation. Values are usually reported as percentages (%); report the "
    "numeric value as printed (e.g. 5.2 for 5.2%). When a value is shown with "
    "a 95% confidence interval like '5.2 (4.8-5.9)', the estimate is 5.2 and "
    "the CI lower/upper are 4.8 and 5.9. When a value is 'mean ± SD' like "
    "'3.99 ± 0.58', the mean is 3.99 and the SD is 0.58.\n\n"
    "CRITICAL RULE: Never guess. If a value is not present in the table, "
    "return null for it. Do not infer, compute, or fill values that are not "
    "explicitly printed. Accuracy and honesty about missing data matter more "
    "than completeness."
)

_USER_PROMPT = (
    "This image shows a biological-variation results table from a scientific "
    "article. Extract every analyte row.\n\n"
    "For each analyte return: name, abbreviation (as printed), unit, "
    "measurement method (if shown), CVI with its 95% CI lower/upper, CVG with "
    "its 95% CI lower/upper, analytical CV (CVA), and the measurand mean and "
    "SD if reported. Put any value you cannot find as null. In source_text, "
    "copy the analyte's row text verbatim.\n\n"
    "IMPORTANT: every table row has its OWN distinct values — never copy one "
    "row's numbers into another row. If the rows are conditions or time "
    "points (e.g. 'h 6.00', 'h 23.00') rather than different analytes, still "
    "extract each row separately and put its label in the name field.\n\n"
    "Also extract study-level dataset fields (sample matrix, number of "
    "subjects/males/females, ethnicity, health state, age mean/min/max, study "
    "duration and units, samples per participant, sampling intervals) if they "
    "are visible; otherwise null.\n\n"
    "If the image contains multiple tables, extract only the biological-"
    "variation results table (the one with CVI / CVG / CVA columns)."
)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _apply_rotation(img, rotation: int):
    """Rotate a PIL image counter-clockwise by a multiple of 90°.

    Uses the same call the picker uses so the cropped region matches exactly
    what the user saw and boxed. expand=True keeps the whole rotated page.
    """
    if rotation % 360 == 0:
        return img
    return img.rotate(rotation, expand=True)


def render_region_png(
    pdf_path: Path,
    page_index: int,
    rotation: int = 0,
    bbox_frac: Optional[Bbox] = None,
    bbox: Optional[Bbox] = None,
) -> bytes:
    """Render a PDF page (optionally rotated and cropped) to PNG bytes.

    `rotation` rotates the rendered page (0/90/180/270) so sideways tables can
    be sent upright — apply the same value the picker displayed.

    Region selection (after rotation is applied):
      * `bbox_frac` — (fx0, fy0, fx1, fy1) as fractions [0,1] of the *rotated*
        image. This is what the picker now returns; it is rotation-safe.
      * `bbox` — (x0, y0, x1, y1) in PDF points, top-left origin. Only honoured
        when rotation == 0 (legacy/upright path).

    With neither, the whole (rotated) page is rendered.
    """
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_index]
        long_edge_pts = max(page.get_width(), page.get_height())
        scale = min(_MAX_RENDER_SCALE, _TARGET_LONG_EDGE / long_edge_pts)
        scale = max(scale, 1.0)

        img = _apply_rotation(page.render(scale=scale).to_pil(), rotation)

        crop = None
        if bbox_frac is not None:
            fx0, fy0, fx1, fy1 = bbox_frac
            lx, rx = sorted((fx0, fx1))
            ty, by = sorted((fy0, fy1))
            crop = (
                int(lx * img.width), int(ty * img.height),
                int(rx * img.width), int(by * img.height),
            )
        elif bbox is not None and rotation % 360 == 0:
            x0, y0, x1, y1 = bbox
            lx, rx = sorted((x0, x1))
            ty, by = sorted((y0, y1))
            crop = (
                int(lx * scale), int(ty * scale),
                int(rx * scale), int(by * scale),
            )

        if crop is not None:
            cx0, cy0 = max(0, crop[0]), max(0, crop[1])
            cx1, cy1 = min(img.width, crop[2]), min(img.height, crop[3])
            if cx1 > cx0 and cy1 > cy0:
                img = img.crop((cx0, cy0, cx1, cy1))

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _call_claude(png_bytes: bytes, model: str) -> dict:
    """Send the image to Claude and return the parsed structured output."""
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment "
            "(e.g. setx ANTHROPIC_API_KEY \"sk-ant-...\") and restart the "
            "terminal before using the Claude fallback."
        )

    client = anthropic.Anthropic()
    image_b64 = base64.standard_b64encode(png_bytes).decode("utf-8")

    # Stream the request. A large table (e.g. 0561 with 39 analytes) plus
    # adaptive thinking needs a high max_tokens to avoid truncating the JSON;
    # the SDK requires streaming for requests that may run long, so we stream
    # and assemble the final message at the end.
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        # No extended thinking: for reading a table it adds ~2x latency with
        # no accuracy gain (verified on 0561: 39/39 either way, 83s vs 188s).
        system=_SYSTEM_PROMPT,
        output_config={
            "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}
        },
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": _USER_PROMPT},
            ],
        }],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError(
            "Claude declined to process this request (stop_reason=refusal)."
        )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("Claude returned no text content to parse.")
    usage = (
        getattr(response.usage, "input_tokens", 0) or 0,
        getattr(response.usage, "output_tokens", 0) or 0,
    )
    return json.loads(text), usage


# ---------------------------------------------------------------------------
# Mapping to schema dataclasses
# ---------------------------------------------------------------------------

def _parse_float(raw) -> Optional[float]:
    """Parse a model-returned string/number into a float, or None if blank."""
    if raw is None:
        return None
    s = str(raw).strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(raw) -> Optional[int]:
    """Parse a model-returned value into an int, or None if blank/invalid."""
    f = _parse_float(raw)
    return int(f) if f is not None else None


def _fv(raw, raw_text: Optional[str]) -> FieldValue:
    """Build an llm-sourced FieldValue, warning when the value is missing."""
    value = _parse_float(raw)
    if value is None:
        return FieldValue(
            value=None,
            source="llm",
            raw_text=raw_text or None,
            warning="Not reported in the table (per Claude).",
        )
    return FieldValue(
        value=value,
        source="llm",
        raw_text=raw_text or None,
        warning=None,
    )


def _build_analyte(rec: dict) -> AnalyteRecord:
    src = rec.get("source_text") or None
    a = AnalyteRecord(
        name=rec.get("name") or "",
        abbreviation=rec.get("abbreviation") or "",
        unit=rec.get("unit") or "",
        method=rec.get("method") or "",
    )
    a.cvi = _fv(rec.get("cvi"), src)
    a.cvi_ci_lower = _fv(rec.get("cvi_ci_lower"), src)
    a.cvi_ci_upper = _fv(rec.get("cvi_ci_upper"), src)
    a.cvg = _fv(rec.get("cvg"), src)
    a.cvg_ci_lower = _fv(rec.get("cvg_ci_lower"), src)
    a.cvg_ci_upper = _fv(rec.get("cvg_ci_upper"), src)
    a.analytical_cv = _fv(rec.get("analytical_cv"), src)
    a.measurand_mean = _fv(rec.get("measurand_mean"), src)
    a.measurand_sd = _fv(rec.get("measurand_sd"), src)
    return a


def _text_or_none(raw) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _build_dataset(ds: dict) -> DatasetDetails:
    out = DatasetDetails()
    out.matrix = _text_or_none(ds.get("matrix"))
    out.number_of_subjects = _parse_int(ds.get("number_of_subjects"))
    out.number_of_males = _parse_int(ds.get("number_of_males"))
    out.number_of_females = _parse_int(ds.get("number_of_females"))
    out.ethnicity = _text_or_none(ds.get("ethnicity"))
    out.state_of_well_being = _text_or_none(ds.get("state_of_well_being"))
    out.age_mean = _parse_float(ds.get("age_mean"))
    out.age_min = _parse_float(ds.get("age_min"))
    out.age_max = _parse_float(ds.get("age_max"))
    out.total_study_duration = _parse_float(ds.get("total_study_duration"))
    out.study_duration_units = _text_or_none(ds.get("study_duration_units"))
    out.samples_per_participant = _parse_int(ds.get("samples_per_participant"))
    out.sampling_intervals = _text_or_none(ds.get("sampling_intervals"))
    return out


def _summarise_field_status(result: ExtractionResult) -> None:
    """Mirror pipeline's blank/extracted inventory so writers stay consistent."""
    monitored = [
        "cvi", "cvi_ci_lower", "cvi_ci_upper",
        "cvg", "cvg_ci_lower", "cvg_ci_upper",
        "analytical_cv", "measurand_mean", "measurand_sd",
    ]
    for rec in result.analytes:
        for fname in monitored:
            fv: FieldValue = getattr(rec, fname)
            tag = f"{rec.abbreviation}.{fname}"
            if fv.value is None:
                result.report.fields_blank.append(tag)
            else:
                result.report.fields_extracted.append(tag)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_with_claude(
    pdf_path: str | Path,
    page_index: int,
    bbox: Optional[Bbox] = None,
    rotation: int = 0,
    bbox_frac: Optional[Bbox] = None,
    model: str = DEFAULT_MODEL,
) -> ExtractionResult:
    """Extract a BV table from `pdf_path` using Claude vision.

    `page_index` is 0-based. `rotation` (0/90/180/270) un-rotates a sideways
    table before sending it. `bbox_frac` (fractions of the rotated page, the
    picker's output) or `bbox` (PDF points, upright only) restrict the region;
    pass neither to send the whole page.

    Returns a fully-populated ExtractionResult with source="llm" on every field.
    """
    pdf_path = Path(pdf_path)
    result = ExtractionResult()
    result.report.source_file = str(pdf_path)
    result.report.primary_table_page = page_index + 1
    result.report.primary_table_was_rotated = bool(rotation % 360)
    result.report.used_llm_fallback = True

    png = render_region_png(
        pdf_path, page_index, rotation=rotation, bbox_frac=bbox_frac, bbox=bbox
    )
    data, usage = _call_claude(png, model)
    result.report.input_tokens, result.report.output_tokens = usage

    for rec in data.get("analytes", []):
        analyte = _build_analyte(rec)
        result.analytes.append(analyte)
        result.report.detected_analytes.append(analyte.abbreviation)

    result.dataset = _build_dataset(data.get("dataset", {}))

    note = data.get("table_notes")
    if note:
        result.report.manual_review.append(f"Claude note: {note}")

    _summarise_field_status(result)
    return result


# ---------------------------------------------------------------------------
# Multi-region extraction (one Claude call per selected table)
# ---------------------------------------------------------------------------

def _merge_dataset(into: DatasetDetails, src: DatasetDetails) -> None:
    """Fill empty fields of `into` from `src` (first non-empty wins)."""
    import dataclasses

    for f in dataclasses.fields(DatasetDetails):
        if f.name == "warnings":
            continue
        if getattr(into, f.name) in (None, ""):
            val = getattr(src, f.name)
            if val not in (None, ""):
                setattr(into, f.name, val)
    into.warnings.extend(src.warnings)


def extract_with_claude_regions(
    pdf_path: str | Path,
    regions: list,
    model: str = DEFAULT_MODEL,
) -> ExtractionResult:
    """Extract one BV table per selected region and merge the results.

    `regions` is a list of objects (e.g. TableSelection) exposing
    ``page_index``, ``rotation`` and ``bbox_frac``. Each region is rendered and
    sent to Claude separately — keeping one table per image avoids the model
    conflating two dense tables — then analytes are concatenated and dataset
    fields are filled from the first region that reports them.
    """
    pdf_path = Path(pdf_path)
    merged = ExtractionResult()
    merged.report.source_file = str(pdf_path)
    merged.report.used_llm_fallback = True

    pages: list = []
    for region in regions:
        res = extract_with_claude(
            pdf_path,
            region.page_index,
            rotation=getattr(region, "rotation", 0),
            bbox_frac=getattr(region, "bbox_frac", None),
            model=model,
        )
        merged.analytes.extend(res.analytes)
        merged.report.detected_analytes.extend(res.report.detected_analytes)
        merged.report.manual_review.extend(res.report.manual_review)
        if res.report.primary_table_page:
            pages.append(res.report.primary_table_page)
        if res.report.primary_table_was_rotated:
            merged.report.primary_table_was_rotated = True
        merged.report.input_tokens += res.report.input_tokens
        merged.report.output_tokens += res.report.output_tokens
        _merge_dataset(merged.dataset, res.dataset)

    merged.report.primary_table_page = pages[0] if pages else None

    cost = estimate_cost_usd(
        merged.report.input_tokens, merged.report.output_tokens
    )
    merged.report.manual_review.append(
        f"Token usage: {merged.report.input_tokens} in + "
        f"{merged.report.output_tokens} out across {len(regions)} call(s) "
        f"(~${cost:.3f} at Opus 4.8 rates)."
    )

    _summarise_field_status(merged)
    return merged
