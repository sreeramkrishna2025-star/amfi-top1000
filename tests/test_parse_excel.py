import os
import tempfile
import unittest

import _helpers  # noqa: F401
import parse_excel as pe
import openpyxl

HEADER = ["Sr. No.", "Company name", "ISIN", "BSE Symbol", "BSE cap", "NSE Symbol", "NSE cap",
          "MSEI Symbol", "MSEI cap", "Average of All Exchanges (Rs. Cr.)",
          "Categorization as per SEBI Circular dated Oct 6, 2017"]


def write_book(rows, header=HEADER):
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FINAL"
    ws.append(["Average market capitalisation (synthetic)"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def row(rank, isin="INE000000001", name="Synthetic Co", cap=1000.0, nse="SYN", bse="500001"):
    return [rank, name, isin, bse, cap, nse, cap, None, None, cap, "Large Cap"]


class ParseHelperTests(unittest.TestCase):
    def test_rank_ok_integers(self):
        self.assertEqual(pe.parse_rank(7), (7, "ok"))
        self.assertEqual(pe.parse_rank("12"), (12, "ok"))
        self.assertEqual(pe.parse_rank(3.0), (3, "ok"))

    def test_rank_fractional_never_truncated(self):
        self.assertEqual(pe.parse_rank(1.5), (None, "invalid"))
        self.assertEqual(pe.parse_rank("1.5"), (None, "invalid"))

    def test_rank_text_and_blank(self):
        self.assertEqual(pe.parse_rank("Note:"), (None, "text"))
        self.assertEqual(pe.parse_rank(None), (None, "blank"))
        self.assertEqual(pe.parse_rank("  "), (None, "blank"))

    def test_rank_bool_and_nan_invalid(self):
        self.assertEqual(pe.parse_rank(True)[1], "invalid")
        self.assertEqual(pe.parse_rank(float("nan"))[1], "invalid")
        self.assertEqual(pe.parse_rank("1.0")[1], "invalid")

    def test_money_parsing(self):
        self.assertEqual(pe.parse_money("1,234.5"), (1234.5, "ok"))
        self.assertEqual(pe.parse_money("-"), (None, "blank"))
        self.assertEqual(pe.parse_money("abc")[1], "invalid")
        self.assertEqual(pe.parse_money(float("inf"))[1], "invalid")

    def test_header_synonyms_match(self):
        cmap, matched = pe.match_header_row(HEADER)
        for key in pe.REQUIRED:
            self.assertIn(key, cmap)

    def test_header_missing_required_column_detected(self):
        cmap, _ = pe.match_header_row(["Company name", "ISIN"])
        self.assertNotIn("rank", cmap)


class ParseWorkbookTests(unittest.TestCase):
    def test_parses_synthetic_rows_and_keeps_missing_symbols(self):
        path = write_book([row(1, nse=None, bse="500001"), row(2, isin="INE000000002", nse="B")])
        try:
            res = pe.parse(path)
            self.assertIsNotNone(res["header_row"])
            self.assertEqual(len(res["records"]), 2)
            self.assertIsNone(res["records"][0]["nse_symbol"])
        finally:
            os.remove(path)

    def test_file_that_is_not_a_workbook_reports_error_without_crash(self):
        fd, path = tempfile.mkstemp(suffix=".xlsx")
        os.write(fd, b"garbage")
        os.close(fd)
        try:
            res = pe.parse(path)
            self.assertTrue(res["errors"])
        finally:
            os.remove(path)

    def test_missing_header_reports_error(self):
        path = write_book([row(1)], header=["Foo", "Bar"])
        try:
            res = pe.parse(path)
            self.assertTrue(res["errors"])
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
