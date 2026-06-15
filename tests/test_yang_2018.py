"""
Regression test: extract Yang et al. 2018 and verify every gold-standard
value from the handoff document.

Run with:
    python -m unittest tests.test_yang_2018
or simply:
    python tests/test_yang_2018.py
"""

import sys
import unittest
from pathlib import Path

# Make the package importable when running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bv_extractor.pipeline import extract


# Path to the test PDF
PDF = Path(__file__).resolve().parent.parent / "sample_data" / "yang2018.pdf"

# Gold-standard values from the handoff (table 4, Current study)
GOLD = {
    "TG":     {"cvi": 25.0, "cvi_lo": 22.2, "cvi_hi": 28.6,
               "cvg": 35.9, "cvg_lo": 25.5, "cvg_hi": 47.7,
               "cva": 2.1,  "mean": 1.38, "sd": 0.69},
    "TC":     {"cvi": 3.5,  "cvi_lo": 3.0,  "cvi_hi": 4.0,
               "cvg": 11.8, "cvg_lo": 9.3,  "cvg_hi": 15.8,
               "cva": 1.1,  "mean": 3.99, "sd": 0.58},
    "LDL-C":  {"cvi": 4.4,  "cvi_lo": 3.8,  "cvi_hi": 5.0,
               "cvg": 18.7, "cvg_lo": 14.8, "cvg_hi": 25.0,
               "cva": 1.1,  "mean": 2.12, "sd": 0.61},
    "HDL-C":  {"cvi": 3.7,  "cvi_lo": 3.0,  "cvi_hi": 4.1,
               "cvg": 15.8, "cvg_lo": 12.4, "cvg_hi": 21.1,
               "cva": 1.8,  "mean": 1.15, "sd": 0.21},
    "apo-A1": {"cvi": 2.3,  "cvi_lo": 1.6,  "cvi_hi": 2.4,
               "cvg": 12.8, "cvg_lo": 10.1, "cvg_hi": 17.1,
               "cva": 1.7,  "mean": 1.58, "sd": 0.22},
    "apo-B":  {"cvi": 2.4,  "cvi_lo": None, "cvi_hi": None,
               "cvg": 14.8, "cvg_lo": 11.7, "cvg_hi": 19.8,
               "cva": 3.7,  "mean": 0.65, "sd": 0.11},
}


class YangRegressionTest(unittest.TestCase):
    """Lock-in regression test against the handoff's gold-standard values."""

    @classmethod
    def setUpClass(cls):
        if not PDF.exists():
            raise unittest.SkipTest(f"Test PDF not found: {PDF}")
        cls.result = extract(PDF)
        cls.by_abbr = {a.abbreviation: a for a in cls.result.analytes}

    # ---- Table-level checks -------------------------------------------

    def test_table_was_found_and_rotated(self):
        self.assertEqual(self.result.report.primary_table_label, "Table 4")
        self.assertEqual(self.result.report.primary_table_page, 7)
        self.assertTrue(self.result.report.primary_table_was_rotated)

    def test_all_six_analytes_detected(self):
        for ab in GOLD:
            self.assertIn(ab, self.by_abbr,
                          f"Analyte {ab} not detected in extraction.")

    # ---- Analyte-level value checks -----------------------------------

    def test_cvi_estimates(self):
        for ab, gold in GOLD.items():
            with self.subTest(analyte=ab):
                self.assertEqual(self.by_abbr[ab].cvi.value, gold["cvi"])

    def test_cvi_confidence_intervals(self):
        for ab, gold in GOLD.items():
            with self.subTest(analyte=ab):
                self.assertEqual(self.by_abbr[ab].cvi_ci_lower.value, gold["cvi_lo"])
                self.assertEqual(self.by_abbr[ab].cvi_ci_upper.value, gold["cvi_hi"])

    def test_cvg_estimates(self):
        for ab, gold in GOLD.items():
            with self.subTest(analyte=ab):
                self.assertEqual(self.by_abbr[ab].cvg.value, gold["cvg"])

    def test_cvg_confidence_intervals(self):
        for ab, gold in GOLD.items():
            with self.subTest(analyte=ab):
                self.assertEqual(self.by_abbr[ab].cvg_ci_lower.value, gold["cvg_lo"])
                self.assertEqual(self.by_abbr[ab].cvg_ci_upper.value, gold["cvg_hi"])

    def test_analytical_cv(self):
        for ab, gold in GOLD.items():
            with self.subTest(analyte=ab):
                self.assertEqual(self.by_abbr[ab].analytical_cv.value, gold["cva"])

    def test_mean_and_sd(self):
        for ab, gold in GOLD.items():
            with self.subTest(analyte=ab):
                self.assertEqual(self.by_abbr[ab].measurand_mean.value, gold["mean"])
                self.assertEqual(self.by_abbr[ab].measurand_sd.value, gold["sd"])

    def test_apo_b_cvi_ci_blank_with_warning(self):
        """apo-B should have CVI=2.4 with both CIs blank and a footnote warning."""
        rec = self.by_abbr["apo-B"]
        self.assertEqual(rec.cvi.value, 2.4)
        self.assertIsNone(rec.cvi_ci_lower.value)
        self.assertIsNone(rec.cvi_ci_upper.value)
        self.assertIsNotNone(rec.cvi_ci_lower.warning)
        self.assertIn("footnote", rec.cvi_ci_lower.warning.lower())

    # ---- Dataset-level checks -----------------------------------------

    def test_dataset_details(self):
        ds = self.result.dataset
        self.assertEqual(ds.matrix, "Serum")
        self.assertEqual(ds.number_of_subjects, 41)
        self.assertEqual(ds.number_of_males, 21)
        self.assertEqual(ds.number_of_females, 20)
        self.assertEqual(ds.ethnicity, "Han Chinese")
        self.assertEqual(ds.state_of_well_being, "Ostensibly healthy")
        self.assertEqual(ds.samples_per_participant, 5)
        self.assertEqual(ds.avg_replicates, 2)
        self.assertEqual(ds.sampling_start_time, "06:30")
        self.assertEqual(ds.sampling_end_time, "18:30")

    # ---- Method assignments ------------------------------------------

    def test_methods(self):
        self.assertIn("colorimetric", self.by_abbr["TG"].method.lower())
        self.assertIn("colorimetric", self.by_abbr["TC"].method.lower())
        self.assertIn("homogeneous", self.by_abbr["HDL-C"].method.lower())
        self.assertIn("homogeneous", self.by_abbr["LDL-C"].method.lower())
        self.assertIn("immunoturbidimetric", self.by_abbr["apo-A1"].method.lower())
        self.assertIn("immunoturbidimetric", self.by_abbr["apo-B"].method.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)