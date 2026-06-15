"""
Interactive PDF table picker (prototype).

Standalone CLI tool. Opens a PDF in a Tk window, renders the page that
``preanalyzer`` thinks contains the BV table, and lets the user draw a
bounding box around the actual table. Arrow keys page through the
document; Enter confirms; Esc cancels.

The result is written as JSON next to the PDF and also printed to
stdout:

    {
      "pdf": "...",
      "page_index": 5,
      "page_number": 6,
      "bbox_pdf": [x0, y0, x1, y1],
      "pdf_size": [width, height]
    }

Coordinates are PDF points with the top-left origin convention used by
pdfplumber (i.e. y grows downward), so downstream code can filter
``page.words`` by ``w.x0 / w.y0 / w.x1 / w.y1`` directly.

Pipeline integration is deferred — for now this is a separate tool
intended for cases where the automatic pipeline picks the wrong page or
the wrong table on a page with multiple candidates.

Usage:
    python -m bv_extractor.interactive_picker <pdf>
    python -m bv_extractor.interactive_picker <pdf> --page 6
    python -m bv_extractor.interactive_picker <pdf> -o selection.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pypdfium2 as pdfium
from PIL import ImageTk

from .preanalyzer import analyze


# ---------------------------------------------------------------------------
# Selection data structure
# ---------------------------------------------------------------------------

@dataclass
class TableSelection:
    """The user's choice of (page, rotation, bounding-box) for a BV table.

    The box is stored as ``bbox_frac`` — fractions [0,1] of the *rotated*
    page image — which is rotation-safe and what downstream rendering uses.
    ``rotation`` is the view rotation the user applied (0/90/180/270).
    ``bbox_pdf`` (PDF points) is kept only for upright (rotation==0) selections
    for backward compatibility; it is None when the page was rotated.
    """
    pdf_path: Path
    page_index: int                       # 0-indexed
    page_number: int                      # 1-indexed (for display / JSON)
    bbox_frac: Tuple[float, float, float, float]  # fx0, fy0, fx1, fy1 of rotated view
    rotation: int                         # 0 / 90 / 180 / 270
    pdf_width: float
    pdf_height: float
    bbox_pdf: Optional[Tuple[float, float, float, float]] = None  # points, upright only

    def to_dict(self) -> dict:
        return {
            "pdf": str(self.pdf_path),
            "page_index": self.page_index,
            "page_number": self.page_number,
            "rotation": self.rotation,
            "bbox_frac": list(self.bbox_frac),
            "bbox_pdf": list(self.bbox_pdf) if self.bbox_pdf else None,
            "pdf_size": [self.pdf_width, self.pdf_height],
        }


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class PickerApp:
    """Tk window that renders one PDF page at a time and captures a bbox.

    Rendering uses pypdfium2 (already in requirements). The page is
    rasterised once per navigation event at a scale that fits within
    ``MAX_CANVAS_PX`` so very large pages don't push the window off
    screen. The scale is stored on the instance so canvas-pixel <->
    PDF-point conversion is exact.
    """

    # Fraction of the screen the canvas viewport may occupy. The render
    # itself can be larger than this — scrollbars handle the rest. These
    # are chosen so the toolbar and hint line still fit comfortably on a
    # 1080p display.
    SCREEN_W_FRAC = 0.85
    SCREEN_H_FRAC = 0.75
    # Hard cap on the render scale. Raised so tables render crisply and the
    # user can zoom in for precise boxing; scrollbars handle the overflow.
    MAX_SCALE = 6.0

    def __init__(self, root: tk.Tk, pdf_path: Path, initial_page: int):
        self.root = root
        self.pdf_path = pdf_path
        self.pdf = pdfium.PdfDocument(str(pdf_path))
        self.page_count = len(self.pdf)
        self.page_index = max(0, min(initial_page, self.page_count - 1))

        # Filled in per page
        self.scale: float = 1.0
        self.pdf_width: float = 0.0
        self.pdf_height: float = 0.0

        # View rotation (0/90/180/270) and the displayed (rotated) image size
        self.rotation: int = 0
        self.disp_w: int = 0
        self.disp_h: int = 0

        # User zoom factor on top of the fit-to-width base scale
        self.zoom: float = 1.0

        # Current pending selection (None until the user releases a real drag)
        self.selection: Optional[TableSelection] = None
        # Confirmed selections (for multi-table picking via "Add table")
        self.selections: list = []

        # Drag state
        self._drag_start: Optional[Tuple[float, float]] = None
        self._rect_id: Optional[int] = None

        # Keep a reference to the PhotoImage to prevent garbage
        # collection — Tk only holds a weak ref.
        self._image_ref = None

        # Screen-relative viewport caps (used by _render_page to size
        # the canvas viewport and pick a fit-to-window render scale).
        self.max_viewport_w = int(
            root.winfo_screenwidth() * self.SCREEN_W_FRAC
        )
        self.max_viewport_h = int(
            root.winfo_screenheight() * self.SCREEN_H_FRAC
        )

        root.title(f"BV table picker — {pdf_path.name}")

        # --- toolbar ---
        bar = tk.Frame(root)
        bar.pack(side=tk.TOP, fill=tk.X)
        tk.Button(bar, text="◀ Prev", command=self.prev_page).pack(
            side=tk.LEFT
        )
        tk.Button(bar, text="Next ▶", command=self.next_page).pack(
            side=tk.LEFT
        )
        tk.Button(bar, text="Rotate ⟳", command=self.rotate).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        tk.Button(bar, text="Add table ➕", command=self.add_table).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        tk.Button(bar, text="Zoom +", command=self.zoom_in).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        tk.Button(bar, text="Zoom −", command=self.zoom_out).pack(
            side=tk.LEFT
        )
        self.page_label = tk.Label(bar, text="")
        self.page_label.pack(side=tk.LEFT, padx=10)
        self.count_label = tk.Label(bar, text="0 tables added")
        self.count_label.pack(side=tk.LEFT, padx=10)
        tk.Button(bar, text="Cancel", command=self.cancel).pack(side=tk.RIGHT)
        tk.Button(bar, text="Finish ✓", command=self.confirm).pack(
            side=tk.RIGHT
        )

        # --- hint / status ---
        self.hint = tk.Label(
            root,
            text=(
                "Drag a box around a BV table. ◀ / ▶ switch pages; Rotate ⟳ "
                "('r'); Zoom +/− for detail. Add table ➕ to pick more than "
                "one; Finish ✓ (Enter) when done, Esc cancels."
            ),
            anchor="w",
            justify="left",
        )
        self.hint.pack(side=tk.TOP, fill=tk.X)

        # --- canvas with scrollbars ---
        canvas_frame = tk.Frame(root)
        canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(
            canvas_frame, bg="white", cursor="crosshair",
        )
        h_scroll = tk.Scrollbar(
            canvas_frame, orient=tk.HORIZONTAL, command=self.canvas.xview,
        )
        v_scroll = tk.Scrollbar(
            canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview,
        )
        self.canvas.configure(
            xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set,
        )
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_mouse_down)
        self.canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_mouse_up)
        # Mouse-wheel scroll: vertical by default, horizontal with Shift.
        # Windows fires <MouseWheel> with delta in multiples of 120.
        self.canvas.bind(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"),
        )
        self.canvas.bind(
            "<Shift-MouseWheel>",
            lambda e: self.canvas.xview_scroll(int(-e.delta / 120), "units"),
        )

        # --- keyboard shortcuts ---
        root.bind("<Left>", lambda e: self.prev_page())
        root.bind("<Right>", lambda e: self.next_page())
        root.bind("<r>", lambda e: self.rotate())
        root.bind("<R>", lambda e: self.rotate())
        root.bind("<plus>", lambda e: self.zoom_in())
        root.bind("<KP_Add>", lambda e: self.zoom_in())
        root.bind("<minus>", lambda e: self.zoom_out())
        root.bind("<KP_Subtract>", lambda e: self.zoom_out())
        root.bind("<Return>", lambda e: self.confirm())
        root.bind("<Escape>", lambda e: self.cancel())

        self._render_page()

    # ----- rendering -----

    def _render_page(self) -> None:
        page = self.pdf[self.page_index]
        self.pdf_width = page.get_width()
        self.pdf_height = page.get_height()

        # Fit the page to the viewport *width* (not height) and scroll
        # vertically. Fitting both dimensions shrank portrait pages to ~0.95×
        # and made the text blurry; fitting width keeps it crisp. The user can
        # zoom further with the Zoom buttons / + - keys.
        base = self.max_viewport_w / self.pdf_width
        self.scale = max(0.75, min(self.MAX_SCALE, base * self.zoom))

        bitmap = page.render(scale=self.scale)
        img = bitmap.to_pil()
        if self.rotation % 360:
            img = img.rotate(self.rotation, expand=True)
        # Displayed (rotated) image size — selection fractions are taken of this.
        self.disp_w = img.width
        self.disp_h = img.height
        # Bind the image to THIS window's interpreter. Without master, Tk uses
        # the default (first) root; when the picker runs as a second Tk() from
        # the app, the image lands in the wrong interpreter and the canvas
        # shows nothing ("image pyimageN doesn't exist").
        self._image_ref = ImageTk.PhotoImage(img, master=self.root)

        self.canvas.delete("all")
        # Viewport caps to the screen-relative size; scrollregion is the
        # full bitmap so the user can scroll the parts that don't fit.
        viewport_w = min(img.width, self.max_viewport_w)
        viewport_h = min(img.height, self.max_viewport_h)
        self.canvas.config(
            width=viewport_w,
            height=viewport_h,
            scrollregion=(0, 0, img.width, img.height),
        )
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.canvas.create_image(0, 0, anchor="nw", image=self._image_ref)

        # Reset any drag-in-progress from a previous page
        self._rect_id = None
        self._drag_start = None

        rot = f"  (rotated {self.rotation}°)" if self.rotation % 360 else ""
        self.page_label.config(
            text=f"Page {self.page_index + 1} / {self.page_count}{rot}"
        )

    # ----- navigation -----

    def prev_page(self) -> None:
        if self.page_index > 0:
            self.page_index -= 1
            self._render_page()
            self.selection = None

    def next_page(self) -> None:
        if self.page_index < self.page_count - 1:
            self.page_index += 1
            self._render_page()
            self.selection = None

    def rotate(self) -> None:
        """Rotate the view 90° counter-clockwise so sideways tables read upright."""
        self.rotation = (self.rotation + 90) % 360
        self._render_page()
        self.selection = None

    def zoom_in(self) -> None:
        self.zoom = min(4.0, self.zoom * 1.25)
        self._render_page()
        self.selection = None

    def zoom_out(self) -> None:
        self.zoom = max(0.4, self.zoom / 1.25)
        self._render_page()
        self.selection = None

    # ----- mouse handling -----

    def _on_mouse_down(self, ev) -> None:
        # Convert viewport-relative event coords to canvas-internal
        # coords. With scrollbars these differ whenever the canvas has
        # been scrolled — create_rectangle and our PDF-coord conversion
        # both need canvas-internal coords.
        cx, cy = self.canvas.canvasx(ev.x), self.canvas.canvasy(ev.y)
        self._drag_start = (cx, cy)
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            cx, cy, cx, cy,
            outline="red", width=2,
        )

    def _on_mouse_drag(self, ev) -> None:
        if self._drag_start is None or self._rect_id is None:
            return
        x0, y0 = self._drag_start
        cx, cy = self.canvas.canvasx(ev.x), self.canvas.canvasy(ev.y)
        self.canvas.coords(self._rect_id, x0, y0, cx, cy)

    def _on_mouse_up(self, ev) -> None:
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        x1 = self.canvas.canvasx(ev.x)
        y1 = self.canvas.canvasy(ev.y)
        self._drag_start = None

        # Ignore accidental clicks
        if abs(x1 - x0) < 5 or abs(y1 - y0) < 5:
            if self._rect_id is not None:
                self.canvas.delete(self._rect_id)
                self._rect_id = None
            return

        lx, rx = sorted((x0, x1))
        ty, by = sorted((y0, y1))

        # Canvas pixels -> fractions of the displayed (rotated) image. This is
        # rotation-safe: downstream rendering applies the same rotation, then
        # crops by these fractions.
        def _clamp(v: float) -> float:
            return max(0.0, min(1.0, v))

        fx0 = _clamp(lx / self.disp_w)
        fy0 = _clamp(ty / self.disp_h)
        fx1 = _clamp(rx / self.disp_w)
        fy1 = _clamp(by / self.disp_h)

        # For upright views, also record PDF-point coords for backward compat.
        if self.rotation % 360 == 0:
            bbox_pdf = (
                lx / self.scale, ty / self.scale,
                rx / self.scale, by / self.scale,
            )
        else:
            bbox_pdf = None

        self.selection = TableSelection(
            pdf_path=self.pdf_path,
            page_index=self.page_index,
            page_number=self.page_index + 1,
            bbox_frac=(fx0, fy0, fx1, fy1),
            rotation=self.rotation,
            pdf_width=self.pdf_width,
            pdf_height=self.pdf_height,
            bbox_pdf=bbox_pdf,
        )
        rot = f", rotated {self.rotation}°" if self.rotation % 360 else ""
        self.hint.config(
            text=(
                f"Selection: page {self.page_index + 1}{rot}. "
                "Drag again to redo, or press Enter to confirm."
            )
        )

    # ----- finish -----

    def add_table(self) -> None:
        """Commit the current box and clear the canvas for another table."""
        if self.selection is None:
            self.hint.config(
                text="Draw a box around a table first, then click Add table."
            )
            return
        self.selections.append(self.selection)
        self.selection = None
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
            self._rect_id = None
        self.count_label.config(text=f"{len(self.selections)} tables added")
        self.hint.config(
            text=(
                f"Added — {len(self.selections)} table(s) so far. Draw the next "
                "table (any page / rotation) and Add table, or Finish ✓ when done."
            )
        )

    def confirm(self) -> None:
        # Fold any pending (un-added) box into the result so a single-table
        # pick still works without clicking "Add table".
        if self.selection is not None:
            self.selections.append(self.selection)
            self.selection = None
        if not self.selections:
            self.hint.config(
                text="No selection yet — drag a rectangle around the table first."
            )
            return
        self.root.destroy()

    def cancel(self) -> None:
        self.selection = None
        self.selections = []
        self.root.destroy()

    def close(self) -> None:
        try:
            self.pdf.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Programmatic entry point
# ---------------------------------------------------------------------------

def pick_multiple(pdf_path: Path, initial_page: int = 0) -> list:
    """Open the picker GUI and return the list of confirmed selections.

    The user can add several tables (across pages / rotations) with the
    "Add table" button. Returns an empty list if the user cancels.

    If a Tk root already exists (e.g. launched from the desktop app) the
    picker is a modal Toplevel sharing that interpreter — this avoids the
    two-Tk()/"image doesn't exist" problem. Standalone, it owns its own root.
    """
    existing = tk._default_root
    owns_root = existing is None
    if owns_root:
        root = tk.Tk()
    else:
        root = tk.Toplevel(existing)

    app = PickerApp(root, pdf_path, initial_page)
    try:
        root.grab_set()
    except tk.TclError:
        pass

    if owns_root:
        root.mainloop()          # ends when the window is destroyed
    else:
        root.wait_window()       # reuse the app's running mainloop

    try:
        if owns_root:
            root.destroy()
    except tk.TclError:
        pass
    app.close()
    return list(app.selections)


def pick(pdf_path: Path, initial_page: int = 0) -> Optional[TableSelection]:
    """Open the picker GUI and return the user's first selection.

    Returns ``None`` if the user cancels (Esc / Cancel / window close).
    Kept for callers that only need a single table.
    """
    selections = pick_multiple(pdf_path, initial_page)
    return selections[0] if selections else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_start_page(pdf_path: Path) -> int:
    """Ask the preanalyzer where it would start; fall back to page 0."""
    try:
        profile = analyze(pdf_path)
    except Exception as exc:
        print(
            f"(preanalyzer failed: {exc}; starting at page 1)",
            file=sys.stderr,
        )
        return 0
    if profile.primary_table_page is not None:
        return profile.primary_table_page - 1
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Open a PDF and let the user draw a bounding box around the "
            "BV results table. The selection is saved as JSON."
        )
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF")
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
        help=(
            "Where to save the selection JSON "
            "(default: <pdf>.selection.json next to the PDF)."
        ),
    )
    parser.add_argument(
        "--page", type=int, default=None,
        help=(
            "1-indexed initial page. "
            "Default: the page the preanalyzer thinks contains the BV table."
        ),
    )
    args = parser.parse_args(argv)

    if not args.pdf.exists():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    if args.page is not None:
        initial_page = args.page - 1
    else:
        initial_page = _default_start_page(args.pdf)

    selection = pick(args.pdf, initial_page)
    if selection is None:
        print("Cancelled — no selection saved.", file=sys.stderr)
        return 1

    out_path = (
        args.out
        if args.out is not None
        else args.pdf.with_suffix(args.pdf.suffix + ".selection.json")
    )
    out_path.write_text(json.dumps(selection.to_dict(), indent=2))
    print(json.dumps(selection.to_dict(), indent=2))
    print(f"Saved selection to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
