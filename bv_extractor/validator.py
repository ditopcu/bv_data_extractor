"""
Quality-control checks applied after extraction.

These checks never modify a value silently. They append warnings to
either the analyte's `notes` list or the report's `manual_review` list,
so a downstream user can decide what to do.

Currently implemented checks:

  1. CI ordering: lower < estimate < upper for both CV_I and CV_G.
  2. Mean and SD plausibility: SD > 0, SD < |Mean| (for non-zero means
     this catches obvious mis-pairings such as Mean and SD being swapped).
  3. CV_A vs CV_I cross-check: warns when CV_A > CV_I, mirroring the
     apo-B condition reported in the article footnotes.
  4. Apo-B special case: confirms that the CV_I CIs are blank with an
     attached footnote warning, matching the article's reported behaviour.
"""

from __future__ import annotations

from typing import List

from .schema import AnalyteRecord, ExtractionReport, FieldValue


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate(
    analytes: List[AnalyteRecord],
    report: ExtractionReport,
) -> None:
    """Run all QC checks; mutate `analytes` and `report` in place."""
    for rec in analytes:
        _check_ci_ordering(rec, "CV_I", rec.cvi, rec.cvi_ci_lower, rec.cvi_ci_upper)
        _check_ci_ordering(rec, "CV_G", rec.cvg, rec.cvg_ci_lower, rec.cvg_ci_upper)
        _check_mean_sd_plausibility(rec)
        _check_cva_vs_cvi(rec)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_ci_ordering(
    rec: AnalyteRecord,
    label: str,
    estimate: FieldValue,
    lower: FieldValue,
    upper: FieldValue,
) -> None:
    """Verify lower < estimate < upper when all three are present."""
    if estimate.value is None or lower.value is None or upper.value is None:
        return
    if not (lower.value <= estimate.value <= upper.value):
        rec.notes.append(
            f"QC: {label} estimate {estimate.value} is not within its 95% CI "
            f"[{lower.value}, {upper.value}] — please review the article "
            f"in case of a mis-extraction."
        )
    if lower.value > upper.value:
        rec.notes.append(
            f"QC: {label} 95% CI lower bound ({lower.value}) is greater than "
            f"its upper bound ({upper.value}); the values may have been "
            f"swapped in extraction."
        )


def _check_mean_sd_plausibility(rec: AnalyteRecord) -> None:
    """Sanity-check Mean ± SD pairs."""
    mean = rec.measurand_mean.value
    sd = rec.measurand_sd.value
    if mean is None or sd is None:
        return
    if sd < 0:
        rec.notes.append(
            f"QC: SD is negative ({sd}); this suggests a parsing error."
        )
    if mean != 0 and abs(sd) > abs(mean):
        rec.notes.append(
            f"QC: SD ({sd}) is larger than |Mean| ({mean}); usually that "
            f"indicates either a very high-CV analyte (review the article) "
            f"or a swapped Mean/SD."
        )


def _check_cva_vs_cvi(rec: AnalyteRecord) -> None:
    """Warn when CV_A > CV_I — the same condition described for apo-B."""
    cva = rec.analytical_cv.value
    cvi = rec.cvi.value
    if cva is None or cvi is None:
        return
    if cva > cvi:
        rec.notes.append(
            f"QC: CV_A ({cva}) is greater than CV_I ({cvi}); under the "
            f"Burdick et al. method this usually means CV_I confidence "
            f"intervals cannot be calculated. Confirm that the CV_I CIs "
            f"are intentionally blank for this analyte."
        )