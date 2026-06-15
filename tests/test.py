from pathlib import Path
from bv_extractor.preanalyzer import analyze
from bv_extractor.preanalyzer import _to_unrotated_text_for_doc, _RE_MEAN_SD, _RE_VALUE_CI
from bv_extractor.pdf_io import load_pdf

PROJECT_ROOT = Path(__file__).parent.parent
pdf = PROJECT_ROOT / "sample_data" / "639.pdf"
doc = load_pdf(pdf)
page_lines = _to_unrotated_text_for_doc(doc)

print("Per-page format hits in 639:")
for idx, lines in page_lines.items():
    msd = sum(len(_RE_MEAN_SD.findall(ln)) for ln in lines)
    vci = sum(len(_RE_VALUE_CI.findall(ln)) for ln in lines)
    print(f"  Page {idx+1}: msd={msd}, vci={vci}")