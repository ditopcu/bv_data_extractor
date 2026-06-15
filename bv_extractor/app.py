"""
BV extractor — desktop GUI wizard.

A small Tk application that walks the user through extracting a biological-
variation table from a PDF:

    1. Open a PDF (menu / button). Pre-analysis runs and the result is shown.
    2. Click "Extract". With "Use Claude (LLM)" off, the deterministic parser
       runs first; if it finds nothing the user is asked whether to send the
       table to Claude. With the box on, it goes straight to Claude.
    3. On the Claude path the table picker opens — the user can box one or
       several tables (Add table), rotate sideways tables, across pages. Each
       table is sent to Claude separately and the results are merged.
    4. The extracted analytes are shown in a preview table; the user reviews
       them and clicks Save to write Excel + JSON + text-report files.

Launch with:  python -m bv_extractor       (or python -m bv_extractor.app)

The Claude path needs ANTHROPIC_API_KEY in the environment; if it's missing a
clear error dialog explains how to set it.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import pypdfium2 as pdfium
from PIL import ImageTk

from .outputs.excel_writer import write_excel
from .outputs.json_writer import write_json
from .outputs.report_writer import write_report
from .pipeline import extract
from .preanalyzer import analyze, format_profile
from .schema import ExtractionResult


class _Region:
    """A minimal region descriptor accepted by extract_with_claude_regions.

    Represents either a whole page (bbox_frac=None) or a picker selection.
    """

    def __init__(self, page_index: int, rotation: int = 0, bbox_frac=None):
        self.page_index = page_index
        self.page_number = page_index + 1
        self.rotation = rotation
        self.bbox_frac = bbox_frac


class BVApp:
    """Main wizard window."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.pdf_path: Optional[Path] = None
        self.profile = None
        self.default_page_index = 0
        self.use_llm = tk.BooleanVar(value=False)

        self._page_image_ref = None  # keep ref so Tk doesn't GC the preview
        self._busy = None            # modal "working…" dialog while Claude runs
        self._last_selections: list = []  # remembered picker boxes for reuse

        root.title("BV Extractor")
        root.geometry("1040x640")

        self._build_menu()
        self._build_body()

    # ----- UI construction ------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open PDF…", command=self.open_pdf)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=filemenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.root.config(menu=menubar)

    def _build_body(self) -> None:
        top = tk.Frame(self.root, padx=12, pady=12)
        top.pack(side=tk.TOP, fill=tk.X)

        tk.Button(top, text="Open PDF…", command=self.open_pdf).pack(
            side=tk.LEFT
        )
        self.file_label = tk.Label(top, text="No PDF selected", anchor="w")
        self.file_label.pack(side=tk.LEFT, padx=12)

        # Middle: pre-analysis summary (left) + detected page preview (right)
        mid = tk.Frame(self.root)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        left = tk.LabelFrame(mid, text="Pre-analysis", padx=10, pady=8)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.profile_text = tk.Text(left, width=46, wrap="word")
        self.profile_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.profile_text.configure(state="disabled")

        right = tk.LabelFrame(mid, text="Detected table page", padx=4, pady=4)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        self.page_canvas = tk.Canvas(right, bg="gray85")
        pv = ttk.Scrollbar(
            right, orient="vertical", command=self.page_canvas.yview
        )
        ph = ttk.Scrollbar(
            right, orient="horizontal", command=self.page_canvas.xview
        )
        self.page_canvas.configure(
            yscrollcommand=pv.set, xscrollcommand=ph.set
        )
        pv.pack(side=tk.RIGHT, fill=tk.Y)
        ph.pack(side=tk.BOTTOM, fill=tk.X)
        self.page_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Controls
        ctrl = tk.Frame(self.root, padx=12, pady=4)
        ctrl.pack(side=tk.TOP, fill=tk.X)
        tk.Checkbutton(
            ctrl, text="Use Claude (LLM)", variable=self.use_llm,
        ).pack(side=tk.LEFT)
        tk.Label(
            ctrl,
            text="Wrong page above? Use 'Pick table manually'.",
            fg="gray35",
        ).pack(side=tk.LEFT, padx=12)
        self.extract_btn = tk.Button(
            ctrl, text="Extract ▶", command=self.run_extract, state="disabled",
        )
        self.extract_btn.pack(side=tk.RIGHT)
        self.manual_btn = tk.Button(
            ctrl, text="Pick table manually…", command=self.pick_manually,
            state="disabled",
        )
        self.manual_btn.pack(side=tk.RIGHT, padx=(0, 8))

        # Status bar
        self.status = tk.Label(
            self.root, text="Open a PDF to begin.", anchor="w",
            relief=tk.SUNKEN, padx=8,
        )
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    # ----- helpers --------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)
        self.root.update_idletasks()

    def _set_profile_text(self, text: str) -> None:
        self.profile_text.configure(state="normal")
        self.profile_text.delete("1.0", tk.END)
        self.profile_text.insert(tk.END, text)
        self.profile_text.configure(state="disabled")

    def _render_detected_page(self) -> None:
        """Show the located table page so the user can see what was found."""
        self.page_canvas.delete("all")
        self._page_image_ref = None
        page_no = self.profile.primary_table_page if self.profile else None
        if not page_no:
            self.page_canvas.create_text(
                10, 10, anchor="nw",
                text="(no table page detected — use 'Use Claude' and pick it)",
            )
            return
        try:
            pdf = pdfium.PdfDocument(str(self.pdf_path))
            try:
                page = pdf[page_no - 1]
                long_edge = max(page.get_width(), page.get_height())
                scale = max(1.0, min(3.0, 1100.0 / long_edge))
                img = page.render(scale=scale).to_pil()
            finally:
                pdf.close()
            self._page_image_ref = ImageTk.PhotoImage(img, master=self.root)
            self.page_canvas.config(scrollregion=(0, 0, img.width, img.height))
            self.page_canvas.create_image(
                0, 0, anchor="nw", image=self._page_image_ref
            )
        except Exception as exc:  # noqa: BLE001
            self.page_canvas.create_text(
                10, 10, anchor="nw", text=f"(preview failed: {exc})"
            )

    def _about(self) -> None:
        messagebox.showinfo(
            "About",
            "BV Extractor\n\nExtracts biological-variation tables from PDF "
            "articles into Excel / JSON / text reports. Deterministic parser "
            "with a Claude vision fallback for non-standard tables.",
        )

    # ----- step 1: open ---------------------------------------------------

    def open_pdf(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Select a BV article PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self.pdf_path = Path(chosen)
        self.file_label.config(text=self.pdf_path.name)
        self._last_selections = []  # forget boxes from the previous PDF
        self.extract_btn.config(state="disabled")
        self._set_status("Analysing…")
        try:
            self.profile = analyze(self.pdf_path)
            self.default_page_index = (
                (self.profile.primary_table_page - 1)
                if self.profile.primary_table_page else 0
            )
            self._set_profile_text(format_profile(self.profile))
            self._set_status(
                f"Ready. Suggested table page: "
                f"{self.profile.primary_table_page or '—'}. "
                "Tick 'Use Claude' to force the LLM, or just press Extract."
            )
        except Exception as exc:  # noqa: BLE001
            self.profile = None
            self.default_page_index = 0
            self._set_profile_text(f"Pre-analysis failed: {exc}")
            self._set_status("Pre-analysis failed — you can still Extract.")
        self._render_detected_page()
        self.extract_btn.config(state="normal")
        self.manual_btn.config(state="normal")

    # ----- step 2/3: extract ---------------------------------------------

    def run_extract(self) -> None:
        if self.pdf_path is None:
            return

        if self.use_llm.get():
            self._claude_flow(use_picker=False)
            return

        # Deterministic first
        self._set_status("Trying the deterministic parser…")
        try:
            result = extract(self.pdf_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Parser error", str(exc))
            self._set_status("Parser failed.")
            return

        # "Success" means real values came out — not just matched analyte
        # names with every field blank (a common parser miss on non-mean_sd
        # tables). fields_extracted is populated only for non-None values.
        if result.analytes and result.report.fields_extracted:
            self._set_status(
                f"Deterministic parser found {len(result.analytes)} analyte(s)."
            )
            self._show_results(result)
            return

        if result.analytes:
            detail = (
                f"The parser matched {len(result.analytes)} analyte name(s) "
                "but couldn't read any values (the table format isn't "
                "parser-friendly).\n\nSend it to Claude (LLM) instead?"
            )
            self._set_status("Parser found names but no values.")
        else:
            detail = (
                "The deterministic parser couldn't extract this table.\n\n"
                "Send it to Claude (LLM) instead?"
            )
            self._set_status("Parser found nothing.")
        if messagebox.askyesno("No values extracted", detail):
            self._claude_flow(use_picker=False)

    def pick_manually(self) -> None:
        """User wants to choose the table by hand (wrong page / multi-table)."""
        if self.pdf_path is None:
            return
        self._claude_flow(use_picker=True)

    def _claude_flow(self, use_picker: bool) -> None:
        """Run the Claude path.

        Default (use_picker=False): send the whole *detected* page — simplest
        and, for single-table results pages, the most reliable (no risk of the
        user cropping out the analyte-name column). The picker is only opened
        when the user explicitly asks to refine (wrong page / multiple tables).
        """
        if not use_picker:
            region = _Region(self.default_page_index)
            self._run_claude_async([region])
            return

        from .interactive_picker import pick_multiple

        # Offer to reuse the boxes picked earlier this session.
        selections = None
        if self._last_selections:
            pages = ", ".join(
                str(s.page_number) for s in self._last_selections
            )
            if messagebox.askyesno(
                "Reuse previous selection",
                f"Use the {len(self._last_selections)} table(s) you already "
                f"selected (page(s) {pages})?\n\n"
                "Yes = reuse them.   No = pick again.",
            ):
                selections = self._last_selections

        if selections is None:
            self._set_status("Pick the table(s) in the picker window…")
            selections = pick_multiple(
                self.pdf_path, initial_page=self.default_page_index
            )
        if not selections:
            self._set_status("No table selected.")
            return
        self._last_selections = selections
        self._run_claude_async(selections)

    # ----- busy dialog + threaded Claude call ----------------------------

    def _show_busy(self, text: str) -> None:
        """Show a modal indeterminate-progress dialog (UI stays responsive)."""
        win = tk.Toplevel(self.root)
        win.title("Working…")
        win.transient(self.root)
        win.resizable(False, False)
        tk.Label(win, text=text, padx=20, pady=12, wraplength=360).pack()
        pb = ttk.Progressbar(win, mode="indeterminate", length=360)
        pb.pack(padx=20, pady=(0, 14))
        pb.start(12)
        # Block the close button so the dialog can't be dismissed mid-call.
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        win.grab_set()
        win.update_idletasks()
        self._busy = win
        self._busy_pb = pb

    def _close_busy(self) -> None:
        if self._busy is not None:
            try:
                self._busy_pb.stop()
                self._busy.grab_release()
                self._busy.destroy()
            except tk.TclError:
                pass
            self._busy = None

    def _run_claude_async(self, selections) -> None:
        """Run the Claude extraction off the main thread; UI shows progress.

        Only the network call runs in the worker thread (it never touches Tk).
        The result is handed back to the main thread via root.after(), which is
        the only place widgets are created/updated.
        """
        from .claude_extractor import extract_with_claude_regions

        pdf = self.pdf_path
        box: dict = {}

        def work() -> None:
            try:
                box["result"] = extract_with_claude_regions(pdf, selections)
            except Exception as exc:  # noqa: BLE001 - reported on main thread
                box["error"] = exc

        self._set_status(
            f"Sending {len(selections)} table(s) to Claude — please wait…"
        )
        self._show_busy(
            f"Sending {len(selections)} table(s) to Claude…\n"
            "This usually takes 10–40 seconds."
        )
        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def poll() -> None:
            if thread.is_alive():
                self.root.after(120, poll)
                return
            self._close_busy()
            if "error" in box:
                messagebox.showerror("Claude error", str(box["error"]))
                self._set_status("Claude extraction failed.")
                return
            result = box["result"]
            self._set_status(
                f"Claude extracted {len(result.analytes)} analyte(s)."
            )
            self._show_results(result)

        self.root.after(120, poll)

    # ----- step 4: review + save -----------------------------------------

    def _show_results(self, result: ExtractionResult) -> None:
        win = tk.Toplevel(self.root)
        win.title("Extraction result — review before saving")
        win.geometry("980x460")

        cols = (
            "name", "abbr", "unit", "cvi", "cvi_ci", "cvg", "cvg_ci",
            "cva", "mean", "sd", "method",
        )
        headings = {
            "name": "Analyte", "abbr": "Abbr", "unit": "Unit",
            "cvi": "CVI", "cvi_ci": "CVI 95% CI", "cvg": "CVG",
            "cvg_ci": "CVG 95% CI", "cva": "CVA", "mean": "Mean",
            "sd": "SD", "method": "Method",
        }
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for c in cols:
            tree.heading(c, text=headings[c])
            tree.column(c, width=90, anchor="w")
        tree.column("name", width=150)
        tree.column("method", width=140)

        for a in result.analytes:
            tree.insert("", tk.END, values=(
                a.name or a.abbreviation or "—",
                a.abbreviation or "",
                a.unit or "",
                _fmt(a.cvi.value),
                _fmt_ci(a.cvi_ci_lower.value, a.cvi_ci_upper.value),
                _fmt(a.cvg.value),
                _fmt_ci(a.cvg_ci_lower.value, a.cvg_ci_upper.value),
                _fmt(a.analytical_cv.value),
                _fmt(a.measurand_mean.value),
                _fmt(a.measurand_sd.value),
                a.method or "",
            ))

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        info_text = f"{len(result.analytes)} analyte(s). "
        if result.report.used_llm_fallback:
            info_text += "LLM used. "
            tok_in = result.report.input_tokens
            tok_out = result.report.output_tokens
            if tok_in or tok_out:
                from .claude_extractor import estimate_cost_usd
                cost = estimate_cost_usd(tok_in, tok_out)
                info_text += (
                    f"Tokens: {tok_in} in / {tok_out} out (~${cost:.3f}). "
                )
        if result.report.manual_review:
            info_text += "See notes in the saved report."
        info = tk.Label(win, anchor="w", padx=8, pady=4, text=info_text)
        info.pack(side=tk.TOP, fill=tk.X)

        btns = tk.Frame(win, padx=8, pady=8)
        btns.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Button(
            btns, text="Save…", command=lambda: self._save(result, win),
        ).pack(side=tk.RIGHT)
        tk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.RIGHT)
        # If the auto (whole-page) result looks wrong or incomplete, let the
        # user refine by picking the exact table(s) by hand.
        tk.Button(
            btns, text="Pick table manually…",
            command=lambda: self._retry_with_claude(win),
        ).pack(side=tk.LEFT)

    def _retry_with_claude(self, win: tk.Toplevel) -> None:
        """Close the current result view and re-extract via the picker + Claude."""
        win.destroy()
        self._claude_flow(use_picker=True)

    def _save(self, result: ExtractionResult, win: tk.Toplevel) -> None:
        out_dir = filedialog.askdirectory(
            title="Choose output folder", initialdir="output",
        )
        if not out_dir:
            return
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = self.pdf_path.stem
        xlsx = write_excel(result, out / f"{stem}.xlsx")
        js = write_json(result, out / f"{stem}.json")
        txt = write_report(result, out / f"{stem}.txt")
        messagebox.showinfo(
            "Saved",
            f"Saved:\n{xlsx}\n{js}\n{txt}",
        )
        self._set_status(f"Saved to {out}")
        win.destroy()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt(v) -> str:
    return "" if v is None else (f"{v:g}")


def _fmt_ci(lo, hi) -> str:
    if lo is None and hi is None:
        return ""
    return f"{_fmt(lo)}–{_fmt(hi)}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    root = tk.Tk()
    BVApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
