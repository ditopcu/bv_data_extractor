# bv_extractor

## Context [REQUIRED]

`bv_extractor` is a Python tool that extracts biological variation (BV)
estimates — CVI, CVG, CVA, Mean±SD, 95% CIs — from scientific PDF
articles into structured, form-ready output (Excel + JSON + text
report). It is built for a laboratory-medicine specialist who feeds a
web-based BV database and needs to turn published BV papers into clean
tabular data without transcribing every table by hand. The hard part is
that BV papers report their numbers in wildly different conventions and
layouts (upright tables, sideways tables, mean±SD, value-with-CI, plain
numeric columns), so the tool has to recognise *what kind* of article it
is before it can parse it.

## Stack [REQUIRED]

- Language: Python 3.12
- Key packages: pdfplumber, pandas, openpyxl
- Extra packages used by `interactive_picker.py` (already in the venv,
  not yet promoted to `requirements.txt`): pypdfium2, Pillow, tkinter
  (stdlib).
- Environment: PyCharm + venv on Windows
  (`C:\Users\ditop\OneDrive\filesDT\PythonProjects\bv_extractor\`)
- The filesystem in the assistant's sandbox resets between sessions;
  the canonical copy of the code lives on the user's Windows machine.
  Delivered files are single `.py` files the user copies into the
  project (see Working rules).

## Folder structure [OPTIONAL]

```
bv_extractor/
├── README.md                       # Do NOT edit without explicit permission
├── requirements.txt                # pdfplumber, pandas, openpyxl
├── bv_extractor/
│   ├── cli.py                      # python -m bv_extractor.cli <pdf> -o <dir>
│   ├── diagnose.py                 # Legacy human-readable report wrapper
│   ├── interactive_picker.py       # Tk GUI: user draws bbox around the BV table
│   ├── preanalyzer.py              # Pre-analysis: profile a PDF before parsing
│   ├── pipeline.py                 # extract() orchestrator
│   ├── pdf_io.py                   # PDF loading, column-aware text extraction
│   ├── rotation.py                 # Detects/un-rotates 90° CCW rotated tables
│   ├── table_finder.py             # Locates BV table; KNOWN_ANALYTES list
│   ├── header_parser.py
│   ├── row_parser.py               # Yang / mean_sd-format row parser
│   ├── patterns.py                 # Regex for Mean±SD, est(CI), missing tokens
│   ├── dataset_extractor.py
│   ├── methods_extractor.py
│   ├── validator.py
│   ├── schema.py                   # Dataclasses: AnalyteRecord, FieldValue, etc.
│   ├── llm_fallback.py             # Architectural placeholder (not wired)
│   └── outputs/{excel,json,report}_writer.py
├── sample_data/                    # Test PDFs (yang2018, hongo, 322, 552, ...)
└── tests/
    ├── test_yang_2018.py           # 11 regression tests — must stay green
    └── test.py                     # Ad-hoc local test scratchpad
```

## Working rules [REQUIRED]

- **Single files only, no zip.** Each delivery is one `.py` file the user
  copies into the project. The user explicitly asked for this to save
  tokens.
- **Never edit `README.md`** until the user explicitly says so.
- Never make assumptions about data structure or intent — ask first.
- Do not refactor or change code that wasn't part of the request.
- Code and code comments in English; conversation in Turkish unless
  specified otherwise.
- No unsolicited commentary on style, performance, or architecture.
- Preserve existing variable and function names unless explicitly asked
  to rename.
- The Yang 2018 regression suite (`tests/test_yang_2018.py`, 11 tests)
  must stay green after every change. Run it before delivering.
- Prefer adding logic in `preanalyzer.py` over touching `table_finder.py`
  — the table finder is calibrated against Yang and should not drift.

## Domain rules [OPTIONAL]

- Never guess a missing field. Every `FieldValue` carries `value`,
  `source`, `raw_text`, and `warning`. If a value isn't present, it
  stays empty with a warning rather than being invented.
- BV terminology: CVI = within-subject variation, CVG = between-subject
  variation, CVA = analytical variation, RCV = reference change value,
  II = index of individuality.
- Follow EFLM / BIVAC conventions when interpreting BV study tables.

## Data notes [OPTIONAL]

- **Three numeric reporting formats seen in the wild:**
  - `mean_sd` — "3.99±0.58" (explicit ± symbol)
  - `value_ci` — "18.7 (16.6–20.9)" (CI in parens; comma, en-dash,
    em-dash, or hyphen separator; may be several segments per line)
  - `tabular_plain` — dense numeric columns, no ± and no parenthesised
    CI; estimate / uncertainty / CI live in separate adjacent columns
- `hybrid` = a page using both mean_sd and value_ci (e.g. Yang Table 4).
- **Rotated tables:** Yang 2018 prints Table 4 sideways (90° CCW).
  pdfplumber returns its words with `upright=False` and characters
  within each word reversed. `rotation.py` detects and un-rotates these.
  For rotated pages, `text_columnized` is unreliable — reconstruct lines
  from un-rotated word coordinates instead.
- **`find_bv_table` can pick the wrong page** — it ranks by keyword /
  analyte density, so a discussion section that *talks about* BV can
  outscore the actual results table. `preanalyzer` corrects this with
  smart relocation (see STATUS.md).

## Open questions / decisions [OPTIONAL]

- [ ] `value_ci` parser does not yet exist — `row_parser.py` only handles
      mean_sd. Multi-segment CI lines (e.g. Jabor 2024:
      `X (lo–hi) X (lo–hi) X (lo–hi)`) need a dedicated parser.
- [ ] `tabular_plain` articles (e.g. Røys 2024) cannot be parsed by
      regex at all — they need either a geometry-aware cell parser or
      the LLM fallback.
- [ ] `ROTATED_CW` geometry is defined but never observed; un-rotation
      for clockwise tables is not implemented.

## Current state

See `STATUS.md` for the latest progress and next steps.

## How to run [OPTIONAL]

```bash
# Full extraction pipeline:
python -m bv_extractor.cli <pdf> -o <output_dir>

# Legacy human-readable diagnostic:
python -m bv_extractor.diagnose <pdf>

# Pre-analysis profile (programmatic):
#   from bv_extractor.preanalyzer import analyze, format_profile
#   profile = analyze(Path("sample_data/yang2018.pdf"))
#   print(format_profile(profile))

# Interactive table picker (Tk GUI; writes <pdf>.selection.json):
python -m bv_extractor.interactive_picker <pdf>

# Regression suite (must stay green):
python tests/test_yang_2018.py
```

## Out of scope

- No production deployment — research code only.
- LLM integration is deferred (two future uses noted in STATUS.md:
  format fallback and output verification). Do not wire it in until
  explicitly requested.
- Do not optimise for performance unless asked — correctness and
  transparency first.
