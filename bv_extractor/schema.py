"""
Data schemas for the BV extractor.

All extracted data flow through these dataclasses. Every numeric field is
Optional[float] so that "not reported" maps unambiguously to None rather
than zero or NaN. Every record carries a list of warnings so the user
knows exactly what was uncertain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Field-level result
# ---------------------------------------------------------------------------

@dataclass
class FieldValue:
    """A single extracted field with provenance and confidence."""
    value: Optional[float] = None
    source: str = "deterministic"   # "deterministic" | "llm" | "manual"
    raw_text: Optional[str] = None  # The exact PDF substring this was parsed from
    warning: Optional[str] = None   # Human-readable note if uncertain/blank


# ---------------------------------------------------------------------------
# Per-analyte record
# ---------------------------------------------------------------------------

@dataclass
class AnalyteRecord:
    """All BV-form fields for one analyte."""

    # Identity
    name: str = ""                  # e.g. "Triglyceride"
    abbreviation: str = ""          # e.g. "TG"
    unit: str = ""                  # e.g. "mmol/L"
    method: str = ""                # e.g. "Enzymatic colorimetric test"

    # Within-subject biological variation (CVI)
    cvi: FieldValue = field(default_factory=FieldValue)
    cvi_ci_lower: FieldValue = field(default_factory=FieldValue)
    cvi_ci_upper: FieldValue = field(default_factory=FieldValue)

    # Between-subject biological variation (CVG)
    cvg: FieldValue = field(default_factory=FieldValue)
    cvg_ci_lower: FieldValue = field(default_factory=FieldValue)
    cvg_ci_upper: FieldValue = field(default_factory=FieldValue)

    # Analytical CV
    analytical_cv: FieldValue = field(default_factory=FieldValue)

    # Measurand summary statistics (in reporting unit)
    measurand_mean: FieldValue = field(default_factory=FieldValue)
    measurand_sd: FieldValue = field(default_factory=FieldValue)
    measurand_min: FieldValue = field(default_factory=FieldValue)
    measurand_max: FieldValue = field(default_factory=FieldValue)

    # Standard-unit equivalents (left blank unless the article reports them)
    measurand_std_unit_mean: FieldValue = field(default_factory=FieldValue)
    measurand_std_unit_sd: FieldValue = field(default_factory=FieldValue)
    measurand_std_unit_min: FieldValue = field(default_factory=FieldValue)
    measurand_std_unit_max: FieldValue = field(default_factory=FieldValue)

    # Analyte-level free-form notes
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dataset / study-level details
# ---------------------------------------------------------------------------

@dataclass
class DatasetDetails:
    """Study-level fields (matrix, subjects, sampling design, etc.)."""

    # Sample matrix
    matrix: Optional[str] = None                # "Serum", "Plasma", ...

    # Cohort
    number_of_subjects: Optional[int] = None
    number_of_males: Optional[int] = None
    number_of_females: Optional[int] = None
    number_subjects_in_bv_estimation: Optional[int] = None
    ethnicity: Optional[str] = None
    state_of_well_being: Optional[str] = None   # "Healthy" / "Disease" / ...
    disease_state: Optional[str] = None

    # Age
    age_mean: Optional[float] = None
    age_median: Optional[float] = None
    age_min: Optional[float] = None
    age_max: Optional[float] = None
    age_sd: Optional[float] = None

    # Sampling design
    total_study_duration: Optional[float] = None
    study_duration_units: Optional[str] = None
    samples_per_participant: Optional[int] = None
    sampling_intervals: Optional[str] = None
    sampling_interval_units: Optional[str] = None
    sampling_start_time: Optional[str] = None
    sampling_end_time: Optional[str] = None
    avg_samples_used_for_bv: Optional[float] = None
    avg_replicates: Optional[int] = None

    # Free-text notes per field that could not be confidently extracted
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction-level metadata
# ---------------------------------------------------------------------------

@dataclass
class ExtractionReport:
    """Everything we want to tell the human after a run."""

    source_file: str = ""
    article_title: Optional[str] = None
    primary_table_label: Optional[str] = None      # e.g. "Table 4"
    primary_table_page: Optional[int] = None       # 1-based for humans
    primary_table_was_rotated: bool = False

    detected_analytes: List[str] = field(default_factory=list)
    fields_extracted: List[str] = field(default_factory=list)
    fields_blank: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    manual_review: List[str] = field(default_factory=list)

    used_llm_fallback: bool = False

    # LLM token usage (0 for the deterministic path)
    input_tokens: int = 0
    output_tokens: int = 0


# ---------------------------------------------------------------------------
# Top-level container
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """The full result of running the extractor on one PDF."""
    report: ExtractionReport = field(default_factory=ExtractionReport)
    dataset: DatasetDetails = field(default_factory=DatasetDetails)
    analytes: List[AnalyteRecord] = field(default_factory=list)
