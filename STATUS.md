# STATUS — bv_extractor

_Last updated: this session. Keep the "Current results" table and "Next
steps" current; move resolved decisions into the log at the bottom._

## Desktop GUI wizard + multi-table (this session)

`app.py` is a Tk wizard launched with **`python -m bv_extractor`** (new
`__main__.py`). Flow: Open PDF → pre-analysis summary shown → "Use Claude
(LLM)" checkbox (default **off**) → Extract. With the box off the deterministic
parser runs first and, if it finds nothing, a dialog asks whether to send the
table to Claude; with the box on it goes straight to Claude. Extracted analytes
are shown in a **review table** (Treeview) before the user saves xlsx/json/txt.
The command-line `cli.py` is unchanged for scripting/automation.

GUI refinements after first live runs: the main window shows the **detected
table page** as an image (so the user sees what was found); a **"Pick table
manually…"** button opens the picker directly when the detected page is wrong
(e.g. 0561, where `find_bv_table` lands on a text page); deterministic
"success" now requires real values, not just matched analyte names, before
skipping the LLM offer; and the result window has a **"Try with Claude…"**
button to escalate to the LLM even after a parser run. Picker launched from
the app is now a modal **Toplevel** (not a second `tk.Tk()`) — this fixed the
blank-canvas "image pyimageN doesn't exist" bug so PDF pages render. Validated
on **0561** (rotated amino-acid table): 40 analytes with CVI/CVG + 95% CIs +
CVA + mean extracted via manual pick → rotate → Claude.

**Multi-table picking:** `interactive_picker.pick_multiple()` lets the user box
several tables (Add table ➕ button), across pages/rotations; returns a list.
`claude_extractor.extract_with_claude_regions()` sends **one Claude call per
table** and merges the results (analytes concatenated, dataset fields filled
from the first region that reports them).

Validated on **0650 Danese 2024** (two tables, Cortisol + Cortisone, rows =
time-of-day h6–h23). One combined image previously made Claude copy a single
CV into all six rows; with per-table calls + a stronger "every row has its own
values" prompt, all 12 rows now extract with correct distinct CVa/CVi/CVg. II
and RCV columns are intentionally **not** in the schema yet (user deferred).

**Selection reuse:** the app remembers the picker boxes for the current PDF;
when the user retries with Claude it asks "reuse the N table(s) on page(s) …?"
instead of forcing a fresh pick (reset when a new PDF is opened). The Claude
call also runs in a background thread behind a modal indeterminate-progress
dialog, so the window no longer freezes during the network request.

### Batch pre-analysis + smarter relocation (this session)

`batch_analyze.py` (repo root) runs the pre-analyzer over a folder with **no
LLM calls** and writes a CSV (verdict/format/geometry/page/score/hits per PDF)
plus a console summary. `--min-year N` filters by the year parsed from the
filename. Used to profile the corpus before building parsers.

On the 2019+ subset (98 papers) value_ci is the dominant recognised format;
~40 of the "unknown" are genuinely plain-column (no ± / no CI anywhere) →
Claude territory; ~14 were just wrong-page picks.

`preanalyzer._maybe_relocate_table` was strengthened (two passes, still
**no change to table_finder**): Pass 1 relocates on inline-format *dominance*
(absolute floor + 3× ratio) instead of only when the candidate had 0 hits;
Pass 2 relocates to the densest *numeric* page carrying a BV signal when no
inline format exists (plain-column tables), guarded so a real paren_sd/format
page is never moved. Effect on 2019+: 22 pages relocated, 5 unknown→recognised,
**0 regressions**, Yang still 11/11. Compare `batch_2019plus.csv` (before) vs
`batch_2019plus_v3.csv` (after). Example: 0561 now auto-locates the real
amino-acid table (p3 text → p4, 156 value_ci hits) instead of needing a manual pick.

### Phase-2 LLM-prompt gaps (deferred until after the article scan)

Observed on real papers; fix by optimising the Claude prompt/schema later:

* **Transposed tables** (analytes in columns, not rows) — e.g. 0538 Todd
  2013. The current prompt assumes one analyte per row; transposed layouts
  get mis-attributed. Claude flags it in `table_notes` but doesn't model it.
* **Short-term vs long-term BV** (e.g. 0538: 6-week vs 9-month CVI/CVG) — only
  captured as text in the analyte name; needs first-class handling.
* **Literature-comparison tables** (other studies' CVs) can be picked by the
  user and mixed into results — the prompt should distinguish the study's own
  data from cited reference values.
* II / RCV / ICC columns are reported by several papers but not in the schema.

## Claude vision fallback + smart routing (this session)

The pipeline now has a **second extraction engine**: `claude_extractor.py`
renders the selected page/region to PNG and asks Claude (Opus 4.8, vision +
structured output) to read the BV table directly. This covers the formats the
deterministic parser can't (`value_ci`, `tabular_plain`, rotated, unknown).

`cli.py` runs a **smart router** (default mode):

1. `preanalyzer.analyze` locates the table and classifies the format.
2. If `LIKELY_EXTRACTABLE` and format ∈ {`mean_sd`, `hybrid`} → deterministic
   parser (fast, free; keeps Yang automatic). If it returns no analytes, fall
   through to Claude.
3. Otherwise → interactive picker GUI (default on) lets the user box the real
   table → `extract_with_claude` on that region.

Flags: `--engine {auto,deterministic,claude}`, `--no-interactive`, `--model`.
API key read from `ANTHROPIC_API_KEY` env var (never hard-coded). New deps in
`requirements.txt`: `anthropic`, `pypdfium2`, `Pillow`.

Validated end-to-end on **322** (`tabular_plain`, Table 2): with the picker's
saved bbox, Claude returned Betaine CVI 17.0 / CVG 30.5, Choline 11.5 / 15.1,
TMAO 46.7 / 24.7 — correct, with CVA/CIs/mean honestly left null. On the wrong
page (demographic Table 3) it extracted nothing and said so, rather than
hallucinating. Every Claude-sourced field carries `source="llm"` + the row
text it was read from. Yang suite still **11/11**; `table_finder` untouched.

Rotated tables are now handled in the GUI: the picker has a **Rotate ⟳**
button (key `r`) that turns the view 90° per click so a sideways table reads
upright; the box is stored as a fraction of the rotated view and Claude
receives the same upright crop (validated on 644 at 270°). The CLI is now a
guided wizard — STEP 1 file picker (no PDF arg → dialog), STEP 2 table pick
(only when Claude is used), STEP 3 send to Claude, STEP 4 save xlsx/json/txt.

Not yet done: the deterministic `value_ci` / `tabular_plain` parsers (Claude
covers these for now).

## Where we are

The deterministic extraction pipeline works end-to-end on the gold
standard (Yang 2018). The big addition this session is a **pre-analysis
layer** (`preanalyzer.py`) that profiles a PDF *before* parsing, so the
pipeline can eventually route each article to the right parser instead
of forcing one parser on everything.

`preanalyzer.analyze(pdf_path)` returns an `ArticleProfile` with three
independent classifications plus transparency fields.

### The three classifications

1. **Verdict** (overall extractability):
   - `LIKELY_EXTRACTABLE` — recognised inline format, ready to parse
   - `NON_STANDARD_FORMAT` — BV-like table, format doesn't fit cleanly
   - `BV_TABLE_NON_PARSEABLE` — dense BV numbers but plain-column layout;
     open the page and fill by hand (LLM fallback is future work)
   - `NOT_A_BV_TABLE` — score below the BV floor
   - `NO_TABLE_FOUND` — no candidate anywhere

2. **Format class:** `mean_sd`, `value_ci`, `paren_sd`, `hybrid`,
   `tabular_plain`, `unknown`

3. **Geometry class:** `upright`, `rotated_ccw`, `rotated_cw`, `unknown`

### Smart table relocation

`find_bv_table` ranks by keyword/analyte density and sometimes picks a
discussion page instead of the results table. `preanalyzer` corrects
this in two passes, but only when the candidate page itself has zero
inline-format density (so well-located tables like Yang are never
disturbed):

- **Pass 1 (format density):** if another page has ≥5 mean_sd/value_ci
  hits, relocate there and re-score. Fixes 322 (5→6), 552 (5→2),
  645 (5→4), 644 (1→6).
- **Pass 2 (tabular density):** if no format-hit page qualifies but a
  page has ≥30 numeric tokens and ≥1 BV keyword, relocate there and tag
  it `tabular_plain`. Fixes the "data is in plain columns" case.

Relocation is fully transparent: `relocated_from_page` and
`relocation_reason` record what moved and why.

## Interactive table picker (prototype)

`interactive_picker.py` (new this session) is a standalone Tk GUI for
the cases the deterministic pipeline can't handle on its own:

- **Wrong table on the right page** — e.g. 322, where the chosen page
  has both demographic value_ci tables and the real BV table.
- **`BV_TABLE_NON_PARSEABLE`** — tabular_plain layouts (Røys 639) where
  the user currently fills by hand.
- **Borderline scores** — 644 sits at score 27 vs the 30 threshold.

The picker renders pages with pypdfium2, lets the user draw a bounding
box on a Tk canvas, and writes `(page, bbox_pdf)` to a sidecar JSON
next to the PDF. Coordinates are PDF points in pdfplumber's top-left
convention, so downstream parsers can filter `page.words` by the bbox
directly when integration happens.

Currently standalone — not yet wired into the pipeline. Rotated pages
render sideways (the user can still draw a box, but coordinates would
need un-rotation before being usable by row parsers).

Run:
```
python -m bv_extractor.interactive_picker <pdf>
python -m bv_extractor.interactive_picker <pdf> --page 6 -o sel.json
```

## Current results (12-PDF test set)

| Article  | Verdict                  | Format        | Geometry     | Page | Reloc | Notes |
|----------|--------------------------|---------------|--------------|------|-------|-------|
| yang2018 | LIKELY_EXTRACTABLE       | hybrid        | rotated_ccw  | 7    | —     | gold standard |
| hongo    | NON_STANDARD_FORMAT      | unknown       | upright      | 2    | —     | 1993 non-standard |
| 322      | LIKELY_EXTRACTABLE       | value_ci      | upright      | 6    | 5→6   | Kühn TMAO |
| 552      | LIKELY_EXTRACTABLE       | mean_sd       | upright      | 2    | 5→2   | Sakaki salt |
| 639      | BV_TABLE_NON_PARSEABLE   | tabular_plain | upright      | 8    | —     | Røys RCV/LIS |
| 644      | NON_STANDARD_FORMAT      | value_ci      | rotated_ccw  | 6    | 1→6   | borderline, score 27 |
| 645      | LIKELY_EXTRACTABLE       | value_ci      | upright      | 4    | 5→4   | Jabor PIVKA-II |
| 647      | NON_STANDARD_FORMAT      | mean_sd       | upright      | 8    | —     | not really BV |
| 557 / 633 / 654 / 662 | (re-run locally) | — | — | — | — | low score, likely NOT_BV or tabular |

Yang regression suite: **11/11 passing** throughout all changes.
`table_finder.py` has not been touched.

> Action: re-run the full 12-PDF batch locally with the latest
> `preanalyzer.py` and paste the table back in here, especially for
> 557/633/654/662 which haven't been re-checked since `tabular_plain`
> landed.

## Known edge cases

- **644** lands at `NON_STANDARD_FORMAT` with score 27 (just under the
  30 threshold) despite 20 value_ci hits. Borderline — may be a real
  value_ci paper the score under-rates. Revisit when writing the
  value_ci parser.
- **322** has a mixed reporting style: the real BV table (Table 2) is
  plain numeric, while demographic tables (Table 1/3) use value_ci. The
  pre-analyzer currently locks onto the value_ci pages. Extraction of
  the *actual* BV numbers will need care.
- **647** is correctly `NON_STANDARD` — it's a microbiome variability
  paper, not a clinical-chemistry BV paper.

## Next steps (agreed order)

1. **value_ci parser.** Write `parsers/value_ci_parser.py`; rename
   `row_parser.py` → `parsers/mean_sd_parser.py`; route via
   `pipeline.py` based on `profile.format_class`. Must handle
   multi-segment CI lines (`X (lo–hi) X (lo–hi) ...`). Unlocks
   322 / 645 / 644.
2. **Wire pre-analysis into the pipeline.** `pipeline.py` calls
   `analyze()` and dispatches to the right parser. (Optionally also let
   `diagnose.py` surface the profile in its report.)
3. **Wire the interactive picker into the pipeline.** Add an
   `--interactive` flag to `cli.py` that triggers
   `interactive_picker.pick()` on low-confidence verdicts
   (`NON_STANDARD_FORMAT`, `BV_TABLE_NON_PARSEABLE`, score < 30) and
   feeds the bbox to the parser so it only considers words inside the
   selection. Needs un-rotation handling for rotated pages.
4. **LLM — deferred, two uses noted:**
   - **Format fallback** for `tabular_plain` / `unknown` (parse plain
     column tables the regex can't).
   - **Verification** — have an LLM check parser output against the PDF
     to catch wrong extractions, especially on borderline cases.

## Decision log

- Single-file deliveries, no zip (token economy). — user
- Never edit README until told. — user
- Verdict label for plain-column tables = `BV_TABLE_NON_PARSEABLE`
  (explicit about the limitation). — agreed
- `tabular_plain` triggers relocation (find the densest numeric page).
  — agreed
- Format-detection threshold stays at 3 (not lowered) to avoid noise.
  — agreed
- Keep all new routing/classification logic in `preanalyzer.py`; do not
  modify `table_finder.py`. — agreed
- A failed earlier attempt to fix this by editing `table_finder.py`'s
  score formula was reverted; the pre-analysis layer is the chosen
  approach instead. — this session
- Interactive picker is the fallback for cases the deterministic
  pipeline can't reach (wrong table on the page, tabular_plain,
  borderline scores). Prototype is standalone first; pipeline
  integration is step 3 in Next steps. — agreed
- pypdfium2 + Pillow + tkinter chosen for the picker. pypdfium2 and
  Pillow were already in the venv; tkinter is stdlib. — agreed
