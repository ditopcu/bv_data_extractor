"""
PDF I/O: load PDFs and extract words/text per page.

Wraps pdfplumber so the rest of the code does not depend on the library
directly. Also pulls together the full document text for downstream
text-based extraction.

Two text streams are produced for each page:

  * `text`               : pdfplumber's default per-page reading order.
  * `text_columnized`    : a column-aware reconstruction that treats a
                           two-column scientific layout correctly. Only
                           upright words are used for this stream.

`Document.full_text` is built from `text_columnized` so that downstream
regex extractors don't trip over interleaved two-column prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import pdfplumber


# ---------------------------------------------------------------------------
# Lightweight word record
# ---------------------------------------------------------------------------

@dataclass
class Word:
    """A single word with bounding box and rotation flag."""
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page_index: int      # 0-based
    upright: bool        # False -> rotated text

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class PageContent:
    """All extracted content from one page."""
    page_index: int      # 0-based
    width: float
    height: float
    words: List[Word]
    text: str            # raw text via pdfplumber (may have rotation artefacts)
    text_columnized: str # column-aware text (left column first, then right)


@dataclass
class Document:
    """The whole PDF parsed into pages."""
    path: Path
    pages: List[PageContent]
    full_text: str       # concatenated text_columnized across pages


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_pdf(pdf_path: str | Path) -> Document:
    """Open a PDF and extract per-page words + raw text + columnized text."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    pages: List[PageContent] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            raw_words = page.extract_words(extra_attrs=["upright"])
            words = [
                Word(
                    text=w["text"],
                    x0=float(w["x0"]),
                    x1=float(w["x1"]),
                    top=float(w["top"]),
                    bottom=float(w["bottom"]),
                    page_index=i,
                    upright=bool(w.get("upright", True)),
                )
                for w in raw_words
            ]
            text = page.extract_text() or ""
            text_columnized = _build_columnized_text(words, float(page.width))
            pages.append(
                PageContent(
                    page_index=i,
                    width=float(page.width),
                    height=float(page.height),
                    words=words,
                    text=text,
                    text_columnized=text_columnized,
                )
            )

    full_text = "\n\n".join(p.text_columnized for p in pages)
    return Document(path=path, pages=pages, full_text=full_text)


# ---------------------------------------------------------------------------
# Column-aware text reconstruction
# ---------------------------------------------------------------------------

def _build_columnized_text(words: List[Word], page_width: float) -> str:
    """Reconstruct page text honouring a possible two-column layout.

    Strategy:
      1. Use only upright words (rotated tables are handled separately).
      2. Decide whether the page is one or two columns by looking at the
         distribution of word x0-positions.
      3. For each column, sort words by (top, x0), then emit lines.

    Single-column fallback: if no clear gap is detected, words are sorted
    by reading order across the whole page.
    """
    upright = [w for w in words if w.upright]
    if not upright:
        return ""

    columns = _detect_columns(upright, page_width)
    column_texts: List[str] = []
    for col_min, col_max in columns:
        col_words = [w for w in upright if col_min <= w.x0 < col_max]
        column_texts.append(_words_to_text(col_words))
    return "\n\n".join(t for t in column_texts if t.strip())


def _detect_columns(
    words: List[Word],
    page_width: float,
) -> List[tuple[float, float]]:
    """Return a list of (x_min, x_max) ranges, one per detected column.

    Looks for an empty vertical band straddling the page midpoint by
    bucketing word x0-values into 10-pt bins and finding the bin with
    the fewest words near the centre. If the emptiest centre-bin has
    very few words it is treated as the column gutter.
    """
    if not words:
        return [(0.0, page_width)]

    midpoint = page_width / 2.0
    bin_w = 10.0
    counts: dict[int, int] = {}
    for w in words:
        b = int(w.x0 // bin_w)
        counts[b] = counts.get(b, 0) + 1
    if not counts:
        return [(0.0, page_width)]

    # Inspect bins within +/- 60 pt of the midpoint
    centre_bins = [
        b for b in counts
        if abs(b * bin_w + bin_w / 2.0 - midpoint) <= 60.0
    ]
    if not centre_bins:
        return [(0.0, page_width)]

    # Largest bin density and the lightest centre bin
    avg_density = sum(counts.values()) / max(1, len(counts))
    min_centre_bin = min(centre_bins, key=lambda b: counts[b])
    if counts[min_centre_bin] >= avg_density * 0.4:
        # No clear gutter -> single column
        return [(0.0, page_width)]

    gutter_x = min_centre_bin * bin_w + bin_w / 2.0
    return [(0.0, gutter_x), (gutter_x, page_width)]


def _words_to_text(words: List[Word], line_tol: float = 3.0) -> str:
    """Group words into lines by `top` and join each line with single spaces."""
    if not words:
        return ""
    sorted_w = sorted(words, key=lambda w: (w.top, w.x0))
    lines: List[List[Word]] = []
    for w in sorted_w:
        if lines and abs(w.top - lines[-1][-1].top) <= line_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    out_lines: List[str] = []
    for line in lines:
        line.sort(key=lambda w: w.x0)
        out_lines.append(" ".join(w.text for w in line))
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_text_position(doc: Document, needle: str) -> Optional[tuple[int, int]]:
    """Return (page_index_0based, char_offset) for the first occurrence of `needle`."""
    needle_low = needle.lower()
    for p in doc.pages:
        idx = p.text_columnized.lower().find(needle_low)
        if idx >= 0:
            return p.page_index, idx
    return None
