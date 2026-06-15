# bv_extractor

A Python tool for extracting biological variation (BV) data from scientific PDF
articles into structured form-ready output (Excel + JSON).

## Quick start

```bash
# Set up a virtual environment in PyCharm (or):
python -m venv .venv
.venv/bin/activate          # macOS/Linux
.venv\Scripts\activate      # Windows

pip install -r requirements.txt

# Run extraction on one PDF
python -m bv_extractor.cli path/to/article.pdf -o output_dir/

# Inspect a PDF before deciding to extract (recommended for new articles)
python -m bv_extractor.diagnose path/to/article.pdf
```

## Diagnostic tool

The `diagnose` module inspects a PDF and reports its structure. Useful when
deciding whether the extractor will work on a new article without parser
changes. Three modes:

```bash
# Per-page summary: word counts, rotation, BV-keyword density,
# candidate analyte locations, "Table N" labels, column layout.
# Saves <pdf>_diagnose.txt next to the PDF.
python -m bv_extractor.diagnose path/to/article.pdf

# Word-level CSV dump: every word with bounding box and rotation flag.
# Useful for debugging tricky table geometry.
python -m bv_extractor.diagnose path/to/article.pdf --mode dump

# Batch: diagnose every PDF in a folder, produce one aggregated CSV.
python -m bv_extractor.diagnose path/to/pdf_folder/ --mode batch
```

The aggregated batch CSV has one row per file:
`pdf_name, pages, candidate_table_pages, has_rotation, analytes_found, score,
primary_table_label, primary_table_page, mean_sd_matches, estimate_ci_matches,
verdict`.

### Verdict labels

Each diagnostic report carries a single tag that summarises whether the
article is worth running through the full extractor:

| Verdict | Meaning |
|---------|---------|
| `LIKELY_EXTRACTABLE` | Standard BV table, Mean±SD and 95% CI present. Run the extractor. |
| `NON_STANDARD_FORMAT` | A BV-like table was found but reporting style is non-standard (e.g. parenthesised SDs without `±`, missing CIs, pre-modern methodology). Manual review recommended. |
| `NOT_A_BV_TABLE` | No table on any page meets the BV-table threshold. |
| `NO_TABLE_FOUND` | No candidate table identified anywhere. |

Use the verdict column in the batch CSV to triage 10+ articles quickly:
filter to `LIKELY_EXTRACTABLE` for the ones that should "just work", and
inspect the rest case-by-case.

## Project layout

```
bv_extractor/
├── requirements.txt
├── README.md
├── bv_extractor/
│   ├── __init__.py
│   ├── cli.py                   # Command-line entry point for extraction
│   ├── diagnose.py              # Diagnostic tool (summary / dump / batch)
│   ├── pipeline.py              # Orchestrates the full extraction pipeline
│   ├── pdf_io.py                # PDF loading + word/text extraction
│   ├── rotation.py              # Detect & un-rotate sideways tables
│   ├── table_finder.py          # Locate the BV results table
│   ├── header_parser.py         # Build column-header → field map
│   ├── row_parser.py            # Parse one analyte row
│   ├── patterns.py              # Regex patterns for mean±SD, est (CI), missing
│   ├── dataset_extractor.py     # Methods/Table 1 / cohort details
│   ├── methods_extractor.py     # Per-analyte analytical method
│   ├── validator.py             # QC checks
│   ├── schema.py                # Dataclasses (AnalyteRecord, etc.)
│   ├── llm_fallback.py          # Optional LLM step (architectural placeholder)
│   └── outputs/
│       ├── __init__.py
│       ├── excel_writer.py
│       ├── json_writer.py
│       └── report_writer.py
├── sample_data/
│   └── yang2018.pdf             # Test article
└── tests/
    └── test_yang_2018.py        # Gold-standard regression test
```

## Running the test

The regression test verifies every gold-standard value from the original
handoff document against `sample_data/yang2018.pdf`:

```bash
python tests/test_yang_2018.py        # 11 tests, all should pass
```

## Design principles

1. **Deterministic first.** Pure regex + geometry. LLM is opt-in fallback.
2. **No column-order assumptions across articles.** Columns are anchored
   by their header text and x-position within each PDF.
3. **Never guess.** Missing or ambiguous fields are left blank with an
   explicit warning in the extraction report.
4. **Modular.** Each module has a single responsibility and is testable
   in isolation.

## Test article

Yang D, Cai Q, Qi X, Zhou Y. *Postprandial Lipid Concentrations and
Daytime Biological Variation of Lipids in a Healthy Chinese Population.*
Ann Lab Med. 2018;38:431–439.

Note: This PDF prints Table 4 rotated 90° on the page. The extractor
detects rotation and reconstructs the table geometry automatically.
