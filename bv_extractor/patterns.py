"""
Pattern library for parsing biological-variation expressions.

Centralised here so that every place in the code uses the same definitions.
Different articles use different abbreviations, dash characters, decimal
separators, and CI delimiters; the patterns below try to absorb that
variability.

Public functions:

    parse_mean_sd(text)           -> (mean, sd) or (None, None)
    parse_estimate_with_ci(text)  -> (estimate, lower, upper, missing_ci_reason)
    is_missing_token(text)        -> bool
    classify_header(text)         -> one of {'mean_sd','cva','cvi','cvg','ci',
                                             'analyte','study','other'}
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Character-class building blocks
# ---------------------------------------------------------------------------

# Any "dash" character: ASCII hyphen, en-dash, em-dash, minus, figure dash
_DASH_CHARS = r"\u2010-\u2015\u2212-"
# Plus-minus: ASCII '±' (U+00B1) plus the textual '+/-' fallback
_PM = r"(?:±|\+/?-|\+\\-)"
# Decimal number: optional minus, integer part, optional decimal part
_NUM = r"-?\d+(?:[.,]\d+)?"

# Tokens that mean "value not reported"
_MISSING_TOKENS_RE = re.compile(
    r"""^\s*(?:
            /                        # bare slash
          | n/?a                     # N/A, NA
          | not\s+available
          | not\s+reported
          | n[.\s]?r[.\s]?           # NR, n.r.
          | could\s+not\s+be\s+calculated
          | --
          | -                        # bare hyphen used as 'missing'
          | nd                       # not determined
        )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Header classification
# ---------------------------------------------------------------------------

_HEADER_PATTERNS = [
    # Order matters: most specific first.
    ("mean_sd", re.compile(r"mean\s*[±+/-]+\s*sd|mean\s*\(\s*sd\s*\)", re.I)),
    ("cva",     re.compile(r"\bcv\s*[-_]?\s*a\b|analytical\s*(?:cv|coefficient)", re.I)),
    ("cvi",     re.compile(r"\bcv\s*[-_]?\s*i\b|\bcv\s*[-_]?\s*w\b|"
                           r"within[-\s]?(?:subject|person)|intra[-\s]?individual", re.I)),
    ("cvg",     re.compile(r"\bcv\s*[-_]?\s*g\b|"
                           r"between[-\s]?(?:subject|person)|inter[-\s]?individual", re.I)),
    ("ci",      re.compile(r"95\s*%\s*ci|confidence\s*interval", re.I)),
    ("analyte", re.compile(r"\banalyte\b|\bparameter\b", re.I)),
    ("study",   re.compile(r"current\s*study|reference|et\s+al", re.I)),
]


def classify_header(text: str) -> str:
    """Classify a header cell into a canonical role.

    Returns one of:
        'mean_sd', 'cva', 'cvi', 'cvg', 'ci', 'analyte', 'study', 'other'
    """
    t = (text or "").strip()
    if not t:
        return "other"
    for label, pat in _HEADER_PATTERNS:
        if pat.search(t):
            return label
    return "other"


# ---------------------------------------------------------------------------
# Mean ± SD parsing
# ---------------------------------------------------------------------------

_MEAN_SD_RE = re.compile(
    rf"({_NUM})\s*{_PM}\s*({_NUM})"
)


def parse_mean_sd(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Parse 'mean±sd', 'mean ± sd', 'mean+/-sd' style expressions.

    Returns (mean, sd) or (None, None) if no match.
    """
    if not text:
        return None, None
    norm = unicodedata.normalize("NFKC", text)
    m = _MEAN_SD_RE.search(norm)
    if not m:
        return None, None
    return _to_float(m.group(1)), _to_float(m.group(2))


# ---------------------------------------------------------------------------
# Estimate + 95% CI parsing
# ---------------------------------------------------------------------------

# Lower-upper CI delimiter: dash family, comma, or the word "to"
_CI_SEP = rf"(?:[{_DASH_CHARS}]|,|to)"

# Just the leading estimate
_ESTIMATE_ONLY_RE = re.compile(rf"^\s*({_NUM})\b")

# A CI block in any of several styles. Each pattern has exactly two
# capturing groups: lower and upper.
_CI_PATTERNS = [
    re.compile(rf"\(\s*({_NUM})\s*{_CI_SEP}\s*({_NUM})\s*\)", re.I),     # (l–u)
    re.compile(rf"\[\s*({_NUM})\s*{_CI_SEP}\s*({_NUM})\s*\]", re.I),     # [l, u]
    re.compile(rf"95\s*%\s*ci\s*:?\s*\(?\s*({_NUM})\s*{_CI_SEP}\s*"
               rf"({_NUM})\s*\)?", re.I),                                # 95% CI l to u
]


def parse_estimate_with_ci(
    text: str,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """Parse 'estimate (lower–upper)' style expressions.

    Handles many delimiter variants and recognises footnote markers
    (e.g. '2.4†') that signal "CI could not be calculated".

    Returns (estimate, lower_ci, upper_ci, missing_ci_reason).
    Any element may be None.
    """
    if not text:
        return None, None, None, None

    norm = unicodedata.normalize("NFKC", text).strip()

    # Detect footnote marker indicating missing CI
    has_footnote = any(c in norm for c in "†‡§¶*")
    cleaned = re.sub(r"[†‡§¶*]", "", norm).strip()

    if is_missing_token(cleaned):
        return None, None, None, None

    # First, the leading estimate
    m_est = _ESTIMATE_ONLY_RE.match(cleaned)
    if not m_est:
        return None, None, None, None
    estimate = _to_float(m_est.group(1))

    # Then, look for any CI variant in the rest of the string
    rest = cleaned[m_est.end():]
    lower = upper = None
    for pat in _CI_PATTERNS:
        m_ci = pat.search(rest)
        if m_ci:
            lower = _to_float(m_ci.group(1))
            upper = _to_float(m_ci.group(2))
            break

    reason = None
    if estimate is not None and lower is None and has_footnote:
        reason = (
            "Footnote marker present alongside the estimate; the article "
            "footnote should be checked but CI was not numerically reported."
        )

    return estimate, lower, upper, reason


# ---------------------------------------------------------------------------
# Missing-value detection
# ---------------------------------------------------------------------------

def is_missing_token(text: str) -> bool:
    """Return True if `text` is a 'not reported' style token."""
    if text is None:
        return True
    return bool(_MISSING_TOKENS_RE.match(text.strip()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(s: Optional[str]) -> Optional[float]:
    """Lenient float conversion: accepts comma decimals."""
    if s is None:
        return None
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None
