import datetime as dt
import json
import os
import tempfile
import unittest

import _helpers  # noqa: F401
import validate_history as vh

REPO = _helpers.REPO


def rec(rank, isin=None, name=None, cap=None, nse="SYM", bse="500000"):
    return {
        "rank": rank,
        "isin": isin or ("INE%09d" % rank)[:12],
        "company_name": name or f"Company {rank}",
        "avg_mcap_cr": cap if cap is not None else float(100000 - rank),
        "nse_symbol": nse if nse is not None else None,
        "bse_symbol": bse,
    }


def good_top1000():
    return [rec(i, nse=f"S{i}") for i in range(1, 1001)]


def codes(report):
    return report["errors"]


class PeriodCoverageTests(unittest.TestCase):
    def test_exactly_18_expected_periods(self):
        p = vh.expected_periods()
        self.assertEqual(len(p), 18)
        self.assertEqual(p[0][0], "2017H2")
        self.assertEqual(p[-1][0], "2026H1")
        self.assertNotIn("2017H1", [x for x, _ in p])
        self.assertNotIn("2026H2", [x for x, _ in p])

    def test_period_end_dates(self):
        d = dict(vh.expected_periods())
        self.assertEqual(d["2017H2"], dt.date(2017, 12, 31))
        self.assertEqual(d["2024H1"], dt.date(2024, 6, 30))
        self.assertEqual(d["2026H1"], dt.date(2026, 6, 30))

    def test_no_gaps_or_duplicates(self):
        labels = [x for x, _ in vh.expected_periods()]
        self.assertEqual(len(set(labels)), 18)


class RepoManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "sources", "amfi_manifest.json"), encoding="utf-8") as fh:
            cls.manifest = json.load(fh)

    def test_repo_manifest_is_valid(self):
        r = vh.validate_manifest(self.manifest)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["period_count"], 18)

    def test_all_18_labels_present(self):
        labels = {e["period_label"] for e in self.manifest["entries"]}
        self.assertEqual(labels, {x for x, _ in vh.expected_periods()})

    def test_no_publication_or_published_dates_present(self):
        for e in self.manifest["entries"]:
            self.assertEqual(e["publication_status"], "UNPUBLISHED")
            self.assertIsNone(e["published_date"])

    def test_no_fabricated_checksums_or_sizes(self):
        for e in self.manifest["entries"]:
            self.assertIsNone(e["sha256"])
            self.assertIsNone(e["size_bytes"])

    def test_only_phase_a_files_marked_downloaded(self):
        verified = sorted(e["period_label"] for e in self.manifest["entries"] if e["download_status"] == "DOWNLOAD_VERIFIED")
        self.assertEqual(verified, ["2020H2", "2024H1"])
        for e in self.manifest["entries"]:
            if e["download_status"] == "DOWNLOAD_VERIFIED":
                self.assertIn("43848842044", e["evidence"])

    def test_phase_a_urls_not_claimed_as_browser_confirmed(self):
        for lbl in ("2020H2", "2024H1"):
            e = next(x for x in self.manifest["entries"] if x["period_label"] == lbl)
            self.assertEqual(e["url_status"], "PHASE_A_DOWNLOAD_VERIFIED")
            self.assertIn("NEEDS_INVESTIGATION", e["notes"])
            self.assertIn("not a browser-DOM confirmation", e["notes"])

    def test_not_found_and_not_due_recorded_in_coverage(self):
        cov = self.manifest["coverage"]
        self.assertEqual(cov["missing"][0]["period_label"], "2017H1")
        self.assertEqual(cov["missing"][0]["status"], "NOT_FOUND")
        self.assertIn("does not prove", cov["missing"][0]["note"])
        self.assertTrue(any("2026H2" in x for x in cov["not_yet_due"]))

    def test_statuses_are_separate_fields(self):
        keys = {"discovery_status", "url_status", "download_status", "parse_status",
                "validation_status", "publication_status"}
        for e in self.manifest["entries"]:
            self.assertTrue(keys.issubset(e.keys()))

    def test_manifest_mutations_are_caught(self):
        def mutate(fn):
            m = json.loads(json.dumps(self.manifest))
            fn(m)
            return vh.validate_manifest(m)["errors"]

        self.assertTrue(mutate(lambda m: m["entries"].pop()))  # missing period
        self.assertTrue(mutate(lambda m: m["entries"][0].update(publication_status="PUBLISHED")))
        self.assertTrue(mutate(lambda m: m["entries"][0].update(published_date="2026-07-01")))
        self.assertTrue(mutate(lambda m: m["entries"][0].update(sha256="a" * 64)))  # checksum w/o verified download
        self.assertTrue(mutate(lambda m: m["entries"][0].update(download_status="DOWNLOAD_VERIFIED", evidence=None)))
        self.assertTrue(mutate(lambda m: m["entries"][0].update(period_end_date="2099-01-01")))
        self.assertTrue(mutate(lambda m: m["coverage"]["missing"][0].update(note="never existed")))
        self.assertTrue(mutate(lambda m: m["entries"][0].update(url_status="BOGUS")))

    def test_valid_checksum_format_checked(self):
        m = json.loads(json.dumps(self.manifest))
        m["entries"][0].update(download_status="DOWNLOAD_VERIFIED", evidence="x", sha256="NOTHEX")
        self.assertTrue(any("sha256 malformed" in e for e in vh.validate_manifest(m)["errors"]))


class Top1000Tests(unittest.TestCase):
    def test_good_top1000_passes(self):
        r = vh.validate_top1000(good_top1000())
        self.assertEqual(codes(r), [])
        self.assertEqual(r["valid_count"], 1000)

    def test_missing_rank_blocks(self):
        recs = good_top1000()
        recs.pop(500)
        r = vh.validate_top1000(recs)
        self.assertTrue(any("exactly 1000" in e for e in codes(r)))
        self.assertTrue(any("missing ranks" in e for e in codes(r)))
        self.assertEqual(r["valid_count"], 0)

    def test_duplicate_rank_blocks(self):
        recs = good_top1000()
        recs[10]["rank"] = 5  # rank 5 now appears twice; rank 11 is missing
        r = vh.validate_top1000(recs)
        self.assertTrue(any("duplicate ranks" in e for e in codes(r)))
        self.assertTrue(any("missing ranks" in e for e in codes(r)))

    def test_fractional_rank_is_not_counted_inside(self):
        recs = good_top1000()
        recs[0]["rank"] = 1.5
        r = vh.validate_top1000(recs)
        self.assertTrue(codes(r))
        self.assertTrue(any("exactly 1000" in e for e in codes(r)))

    def test_boolean_rank_not_counted(self):
        recs = good_top1000()
        recs[0]["rank"] = True
        self.assertTrue(codes(vh.validate_top1000(recs)))

    def test_duplicate_isin_inside_top1000_blocks(self):
        recs = good_top1000()
        recs[1]["isin"] = recs[0]["isin"]
        self.assertTrue(any("duplicate ISIN" in e for e in codes(vh.validate_top1000(recs))))

    def test_invalid_isin_blocks(self):
        for bad in ("IN1234567890", "INE12345678", "ine123456789", None, ""):
            recs = good_top1000()
            recs[5]["isin"] = bad
            self.assertTrue(any("ISIN format" in e for e in codes(vh.validate_top1000(recs))), bad)

    def test_missing_company_name_blocks(self):
        recs = good_top1000()
        recs[7]["company_name"] = "   "
        self.assertTrue(any("company name" in e for e in codes(vh.validate_top1000(recs))))

    def test_zero_negative_or_nan_cap_blocks(self):
        for bad in (0, -5.0, float("nan"), None, "100", True):
            recs = good_top1000()
            recs[3]["avg_mcap_cr"] = bad
            self.assertTrue(any("market cap" in e for e in codes(vh.validate_top1000(recs))), repr(bad))

    def test_cap_must_be_descending_with_rank(self):
        recs = good_top1000()
        recs[0]["avg_mcap_cr"] = 1.0  # rank 1 smaller than rank 2
        self.assertTrue(any("not descending" in e for e in codes(vh.validate_top1000(recs))))

    def test_equal_caps_allowed(self):
        recs = good_top1000()
        for r in recs:
            r["avg_mcap_cr"] = 500.0
        self.assertEqual(codes(vh.validate_top1000(recs)), [])

    def test_missing_nse_bse_symbols_are_warnings_only(self):
        recs = good_top1000()
        recs[2]["nse_symbol"] = None
        recs[3]["bse_symbol"] = None
        r = vh.validate_top1000(recs)
        self.assertEqual(codes(r), [])
        self.assertEqual(r["missing_nse_ranks"], [3])
        self.assertEqual(r["missing_bse_ranks"], [4])
        self.assertTrue(any("without NSE" in w for w in r["warnings"]))

    def test_company_with_missing_symbols_is_kept(self):
        recs = good_top1000()
        recs[9]["nse_symbol"] = None
        r = vh.validate_top1000(recs)
        self.assertEqual(r["inside_count"], 1000)

    def test_records_outside_top1000_are_report_only(self):
        recs = good_top1000() + [rec(1001, isin="INE000000001", cap=5.0), rec(1500, isin="INE000000002", cap=1.0)]
        r = vh.validate_top1000(recs)
        self.assertEqual(codes(r), [])
        self.assertEqual(r["valid_count"], 1000)
        self.assertEqual([x["rank"] for x in r["outside_top1000"]], [1001, 1500])

    def test_outside_records_with_bad_isin_do_not_block(self):
        recs = good_top1000() + [rec(1001, isin="BAD")]
        self.assertEqual(codes(vh.validate_top1000(recs)), [])

    def test_non_numeric_outside_rank_does_not_crash(self):
        recs = good_top1000() + [{"rank": "A", "isin": "x"}]
        r = vh.validate_top1000(recs)
        self.assertEqual(codes(r), [])


class IdentityTests(unittest.TestCase):
    def test_symbol_change_same_isin_becomes_candidate_not_approved(self):
        prev = [{"isin": "INE000000001", "nse_symbol": "OLDX", "company_name": "A"}]
        curr = [{"isin": "INE000000001", "nse_symbol": "NEWX", "company_name": "A"}]
        rows = vh.identity_candidates(prev, curr)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_type"], "symbol_change_same_isin")
        self.assertEqual(rows[0]["status"], "PENDING_REVIEW")

    def test_same_symbol_two_isins_flagged(self):
        curr = [{"isin": "INE000000001", "nse_symbol": "DUP"}, {"isin": "INE000000002", "nse_symbol": "DUP"}]
        rows = vh.identity_candidates([], curr)
        self.assertTrue(any(r["candidate_type"] == "symbol_shared_by_multiple_isins" for r in rows))

    def test_no_change_produces_no_candidates(self):
        recs = [{"isin": "INE000000001", "nse_symbol": "SAME"}]
        self.assertEqual(vh.identity_candidates(recs, recs), [])

    def test_missing_symbol_not_treated_as_change(self):
        prev = [{"isin": "INE000000001", "nse_symbol": "OLDX"}]
        curr = [{"isin": "INE000000001", "nse_symbol": None}]
        self.assertEqual(vh.identity_candidates(prev, curr), [])


class AliasTests(unittest.TestCase):
    base = {"isin": "INE000000001", "old_symbol": "OLDX", "new_symbol": "NEWX",
            "effective_date": "2025-01-01", "source": "NSE circular", "approved_by": "reviewer"}

    def test_complete_row_accepted(self):
        self.assertEqual(vh.approve_alias(self.base), (True, "ok"))

    def test_missing_approver_rejected(self):
        row = dict(self.base, approved_by="")
        ok, why = vh.approve_alias(row)
        self.assertFalse(ok)
        self.assertIn("approved_by", why)

    def test_missing_source_rejected(self):
        self.assertFalse(vh.approve_alias(dict(self.base, source=" "))[0])

    def test_identical_symbols_rejected(self):
        self.assertFalse(vh.approve_alias(dict(self.base, new_symbol="OLDX"))[0])

    def test_invalid_isin_rejected(self):
        self.assertFalse(vh.approve_alias(dict(self.base, isin="X"))[0])

    def test_header_only_csv_loads_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("isin,old_symbol,new_symbol,effective_date,source,approved_by\n")
        try:
            approved, rejected = vh.load_aliases(fh.name)
            self.assertEqual(approved, [])
            self.assertEqual(rejected, [])
        finally:
            os.remove(fh.name)

    def test_repo_alias_file_has_no_unapproved_rows(self):
        approved, rejected = vh.load_aliases(os.path.join(REPO, "symbol_aliases.csv"))
        self.assertEqual(approved, [])
        self.assertEqual(rejected, [])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="amfi_snap_")

    def test_created_then_unchanged_on_same_content(self):
        payload = {"period": "2026H1", "records": [1, 2]}
        self.assertEqual(vh.write_snapshot(self.root, "2026H1", payload), "CREATED")
        self.assertEqual(vh.write_snapshot(self.root, "2026H1", payload), "UNCHANGED")

    def test_conflicting_content_never_overwrites(self):
        vh.write_snapshot(self.root, "2026H1", {"records": [1]})
        path = vh.snapshot_path(self.root, "2026H1")
        with open(path, "rb") as fh:
            before = fh.read()
        with self.assertRaises(vh.SnapshotConflict):
            vh.write_snapshot(self.root, "2026H1", {"records": [2]})
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_layout_is_snapshots_period_universe_json(self):
        vh.write_snapshot(self.root, "2024H1", {"x": 1})
        self.assertTrue(os.path.isfile(os.path.join(self.root, "snapshots", "2024H1", "universe.json")))

    def test_path_traversal_periods_rejected_and_nothing_written(self):
        bad = ["../escaped", "2026H1/../escaped", "/abs", "..", "2017H1", "2026H2", "", "2026H1 ", "2026h1"]
        for period in bad:
            with self.subTest(period=period):
                with self.assertRaises(ValueError):
                    vh.write_snapshot(self.root, period, {"x": 1})
        self.assertFalse(os.path.exists(os.path.join(self.root, "escaped")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "snapshots")))

    def test_every_expected_period_accepted(self):
        for lbl, _ in vh.expected_periods():
            self.assertTrue(vh.snapshot_path(self.root, lbl).endswith(os.path.join("snapshots", lbl, "universe.json")))

    def test_key_order_does_not_change_identity(self):
        vh.write_snapshot(self.root, "2024H2", {"a": 1, "b": 2})
        self.assertEqual(vh.write_snapshot(self.root, "2024H2", {"b": 2, "a": 1}), "UNCHANGED")


class PointInTimeTests(unittest.TestCase):
    def test_verified_publication_used_as_base(self):
        r = vh.effective_dates(dt.date(2026, 6, 30), published=dt.date(2026, 7, 10))
        self.assertEqual(r["published_date"], "2026-07-10")
        self.assertEqual(r["effective_base_source"], "verified_publication_date")
        self.assertEqual(r["effective_from_estimated"], "2026-07-10")
        self.assertFalse(r["look_ahead_risk"])

    def test_no_publication_date_is_never_invented(self):
        r = vh.effective_dates(dt.date(2026, 6, 30))
        self.assertIsNone(r["published_date"])
        self.assertEqual(r["effective_base_source"], "period_end_fallback")
        self.assertEqual(r["effective_confidence"], "low")
        self.assertTrue(r["look_ahead_risk"])

    def test_estimates_always_flagged(self):
        for pub in (None, dt.date(2026, 7, 1)):
            self.assertTrue(vh.effective_dates(dt.date(2026, 6, 30), published=pub)["is_estimated"])

    def test_sensitivity_0_30_60_90(self):
        r = vh.effective_dates(dt.date(2026, 6, 30))
        self.assertEqual(sorted(r["sensitivity_dates"]), ["+0d", "+30d", "+60d", "+90d"])
        self.assertEqual(r["sensitivity_dates"]["+90d"], "2026-09-28")

    def test_period_end_kept_separate_from_first_detected(self):
        r = vh.effective_dates(dt.date(2026, 6, 30), first_detected=dt.date(2026, 7, 15))
        self.assertEqual(r["period_end_date"], "2026-06-30")
        self.assertEqual(r["first_detected_date"], "2026-07-15")


if __name__ == "__main__":
    unittest.main()
