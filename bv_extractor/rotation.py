"""
Rotation handling for tables printed sideways on a portrait page.

Some journals (notably Annals of Laboratory Medicine) print wide tables
rotated 90° counter-clockwise on a portrait page. pdfplumber reports
each character with `upright=False` and the *characters within each word
are stored in reverse reading order*.

This module:

  1. detects rotated-text regions on a page;
  2. reverses the character order of rotated words so they read normally;
  3. exposes an "original" (rect, x, y) coordinate system that the rest of
     the extractor can use as if the page were unrotated.

Coordinate mapping for 90° CCW rotation
---------------------------------------
Imagine you take a portrait page and tilt it 90° clockwise so the
formerly-sideways table now reads normally. After tilting:

    original-x  (left -> right in original)  came from  -page_top
                                              (small page-top = right side
                                               of original table)
    original-y  (top  -> bottom in original)  came from   page_x0
                                              (small page-x0 = top of
                                               original table)

We add `(page_height)` to `-page_top` so original-x stays positive and
left-to-right ordering is preserved. The absolute origin doesn't matter;
only the relative ordering does, because downstream code clusters by
proximity and sorts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .pdf_io import PageContent, Word


# ---------------------------------------------------------------------------
# Un-rotated word record
# ---------------------------------------------------------------------------

@dataclass
class UnrotatedWord:
    """A word whose characters and coordinates have been un-rotated.

    `text` reads in the natural (left-to-right) direction. `ox0/oy0/ox1/oy1`
    are coordinates in the *original* (un-rotated) frame, where small
    `oy` is the top of the table and small `ox` is the left of the table.
    """
    text: str
    ox0: float          # original-frame x left
    ox1: float          # original-frame x right
    oy0: float          # original-frame y top
    oy1: float          # original-frame y bottom
    page_index: int
    raw: Word           # original word for traceability

    @property
    def ox_center(self) -> float:
        return (self.ox0 + self.ox1) / 2.0

    @property
    def oy_center(self) -> float:
        return (self.oy0 + self.oy1) / 2.0


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def has_rotated_text(page: PageContent, min_words: int = 30) -> bool:
    """Return True if the page contains a substantial rotated-text region.

    Threshold is intentionally conservative: we want to be sure it's a
    rotated *table* and not just a page-margin caption or watermark.
    """
    rotated = [w for w in page.words if not w.upright]
    return len(rotated) >= min_words


# ---------------------------------------------------------------------------
# Un-rotation
# ---------------------------------------------------------------------------

def unrotate_words(page: PageContent) -> List[UnrotatedWord]:
    """Return only the rotated words on the page, mapped to original coords.

    Assumes the dominant rotation is 90° counter-clockwise (the common
    sideways-table layout). For 90° CW or 180° rotation we would need a
    different mapping; v1 only supports CCW because that is what the test
    article uses.

    The character reversal applied here is a property of how pdfplumber
    yields `extract_words` for rotated runs: the bytes come out in
    visual-screen order, so we reverse them to get the logical order.
    """
    out: List[UnrotatedWord] = []
    for w in page.words:
        if w.upright:
            continue
        # Reverse character order to recover natural reading direction
        text = w.text[::-1]

        # Map: smaller page-top -> larger original-x (= further right
        # in the original table); smaller page-x0 -> smaller original-y
        # (= top of the original table).
        ox0 = page.height - w.bottom    # use bottom so ox0 < ox1
        ox1 = page.height - w.top
        oy0 = w.x0
        oy1 = w.x1

        out.append(
            UnrotatedWord(
                text=text,
                ox0=ox0,
                ox1=ox1,
                oy0=oy0,
                oy1=oy1,
                page_index=w.page_index,
                raw=w,
            )
        )
    return out
