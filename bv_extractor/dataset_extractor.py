"""
Extract study-level dataset details from the upright text of the article.

Unlike the BV results table (which often uses a rotated geometric layout),
the methods section, abstract, and small descriptive tables (e.g. Table 1
"General characteristics") are normal flowing prose. We work on the raw
text returned by pdfplumber and use targeted regex patterns.

Each extractor function is intentionally narrow: it tries one or two
specific phrasings and returns None on failure. The caller assembles the
DatasetDetails record from these partial results and records a warning
for every field that came back None.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .pdf_io import Document
from .schema import DatasetDetails


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_dataset_details(doc: Document) -> DatasetDetails:
    """Best-effort dataset-level extraction; missing fields stay None."""
    text = doc.full_text
    ds = DatasetDetails()

    ds.matrix = _extract_matrix(text)
    ds.number_of_subjects, ds.number_of_males, ds.number_of_females = (
        _extract_subject_counts(text)
    )
    ds.ethnicity = _extract_ethnicity(text)
    ds.state_of_well_being = _extract_state_of_wellbeing(text)
    ds.samples_per_participant = _extract_samples_per_participant(text)
    ds.avg_replicates = _extract_replicates(text)
    ds.sampling_start_time, ds.sampling_end_time, sampling_times = (
        _extract_sampling_times(text)
    )
    if sampling_times:
        ds.sampling_intervals = ", ".join(sampling_times)

    # Note any field we could not extract
    _record_warnings(ds)
    return ds


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

_MATRIX_RE = re.compile(
    r"\b(serum|plasma|whole\s+blood|urine|cerebrospinal\s+fluid|csf|saliva)\b",
    re.IGNORECASE,
)


def _extract_matrix(text: str) -> Optional[str]:
    """Return the most-mentioned biological matrix, capitalised."""
    counts: dict[str, int] = {}
    for m in _MATRIX_RE.finditer(text):
        key = m.group(1).lower()
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    best = max(counts.items(), key=lambda kv: kv[1])[0]
    return best.title()  # "Serum", "Plasma"...


_SUBJECTS_RE = re.compile(
    r"""(?:                                  # Common phrasings:
            (\d+)\s+(?:ostensibly\s+)?healthy\s+(?:volunteers|subjects|participants)
          | (?:total\s+of\s+)?(\d+)\s+subjects
          | from\s+(\d+)\s+(?:ostensibly\s+)?healthy
        )""",
    re.IGNORECASE | re.VERBOSE,
)

_MF_RE = re.compile(
    r"""(?:
            \(\s*(\d+)\s+(?:males|men)\s+and\s+(\d+)\s+(?:females|women)\s*\)
          | \bMales?\s*\(\s*N\s*=\s*(\d+)\s*\)\s*[A-Za-z\s]*?
            \bFemales?\s*\(\s*N\s*=\s*(\d+)\s*\)
          | \b(\d+)\s+(?:males|men)\s+and\s+(\d+)\s+(?:females|women)\b
        )""",
    re.IGNORECASE | re.VERBOSE,
)


def _dehyphenate(text: str) -> str:
    """Remove soft line-break hyphenation: 'fe-\\njects' -> 'fejects'.

    PDF text extraction often preserves the visual hyphen + newline that
    splits a word at the end of a line. This breaks regex matches that
    span the wrap. We collapse 'word-\\n' -> 'word' and replace remaining
    newlines with spaces for easier matching.
    """
    no_wrap = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", no_wrap)


def _extract_subject_counts(
    text: str,
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Return (n_total, n_male, n_female).

    The text is de-hyphenated first because PDF wraps frequently break
    phrases like '21 males and 20 fe-\\nmales'.
    """
    flat = _dehyphenate(text)

    n_total: Optional[int] = None
    for m in _SUBJECTS_RE.finditer(flat):
        for g in m.groups():
            if g:
                n_total = int(g)
                break
        if n_total is not None:
            break

    n_male = n_female = None
    mf = _MF_RE.search(flat)
    if mf:
        # The pattern has multiple alternative branches; pick the first
        # consecutive pair of non-None integer groups.
        groups = [g for g in mf.groups() if g is not None]
        if len(groups) >= 2:
            n_male = int(groups[0])
            n_female = int(groups[1])

    if n_total is None and n_male is not None and n_female is not None:
        n_total = n_male + n_female
    return n_total, n_male, n_female


_ETHNICITY_PATTERNS = [
    # Order matters: more-specific compounds first.
    re.compile(r"\b(Han\s+Chinese|African\s+American)\b", re.IGNORECASE),
    re.compile(
        r"\b(Chinese|Caucasian|African|Hispanic|Asian|European|"
        r"Korean|Japanese)\b",
        re.IGNORECASE,
    ),
]


def _extract_ethnicity(text: str) -> Optional[str]:
    flat = _dehyphenate(text)
    for pat in _ETHNICITY_PATTERNS:
        m = pat.search(flat)
        if m:
            return " ".join(p.capitalize() for p in m.group(1).split())
    return None


def _extract_state_of_wellbeing(text: str) -> Optional[str]:
    """Detect "ostensibly healthy" / "healthy volunteers" etc."""
    if re.search(r"ostensibly\s+healthy", text, re.IGNORECASE):
        return "Ostensibly healthy"
    if re.search(r"healthy\s+(?:volunteers|subjects|participants|adults)",
                 text, re.IGNORECASE):
        return "Healthy"
    return None


_SAMPLES_PER_PARTICIPANT_RE = re.compile(
    r"""(?:
            blood\s+samples?\s+were\s+collected[^.]{0,80}?
            \bat\s+(\w+)\s+time\s+points?
          | collected\s+from\s+each\s+subject\s+(\w+)\s+times?
          | (\w+)\s+blood\s+sampling\s+time\s+points?
        )""",
    re.IGNORECASE | re.VERBOSE,
)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _extract_samples_per_participant(text: str) -> Optional[int]:
    for m in _SAMPLES_PER_PARTICIPANT_RE.finditer(text):
        for g in m.groups():
            if g is None:
                continue
            n = _to_int(g)
            if n is not None:
                return n
    return None


_REPLICATES_RE = re.compile(
    r"""(?:
            tested\s+in\s+duplicate
          | each\s+sample\s+was\s+tested\s+(?:in\s+)?(\w+)\s+times?
          | tested\s+(\w+)\s+times?\b
        )""",
    re.IGNORECASE | re.VERBOSE,
)


def _extract_replicates(text: str) -> Optional[int]:
    m = _REPLICATES_RE.search(text)
    if not m:
        return None
    if m.group(0).lower().startswith("tested in duplicate"):
        return 2
    for g in m.groups():
        n = _to_int(g) if g else None
        if n is not None:
            return n
    return None


_TIMES_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")


def _extract_sampling_times(text: str) -> tuple[Optional[str], Optional[str], List[str]]:
    """Return (start, end, full_list) of HH:MM sampling times.

    Looks specifically inside parenthesised lists like '(06:30, 09:00, ...)'
    to avoid sweeping in unrelated times mentioned elsewhere.
    """
    candidates: List[List[str]] = []
    for paren in re.finditer(r"\(([^()]{10,200})\)", text):
        block = paren.group(1)
        times = _TIMES_RE.findall(block)
        if len(times) >= 3:
            candidates.append(times)
    # Pick the longest list (likely the sampling-times enumeration)
    candidates.sort(key=len, reverse=True)
    if not candidates:
        return None, None, []
    times = candidates[0]
    return times[0], times[-1], times


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_int(token: Optional[str]) -> Optional[int]:
    if token is None:
        return None
    t = token.lower().strip()
    if t.isdigit():
        return int(t)
    if t in _NUMBER_WORDS:
        return _NUMBER_WORDS[t]
    return None


def _record_warnings(ds: DatasetDetails) -> None:
    """Append a warning string for every field still None."""
    fields_to_check = [
        ("matrix", ds.matrix),
        ("number_of_subjects", ds.number_of_subjects),
        ("number_of_males", ds.number_of_males),
        ("number_of_females", ds.number_of_females),
        ("ethnicity", ds.ethnicity),
        ("state_of_well_being", ds.state_of_well_being),
        ("samples_per_participant", ds.samples_per_participant),
        ("avg_replicates", ds.avg_replicates),
        ("sampling_start_time", ds.sampling_start_time),
        ("sampling_end_time", ds.sampling_end_time),
    ]
    for name, value in fields_to_check:
        if value is None:
            ds.warnings.append(
                f"Dataset field '{name}' could not be extracted automatically; "
                f"please review the article and fill in manually."
            )
