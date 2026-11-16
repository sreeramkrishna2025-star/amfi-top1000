"""Validation for the AMFI Top 1000 historical universe (code only, no data shipped).

Covers:
- expected period coverage (exactly 18 periods, 2017H2..2026H1; 2017H1 kept as NOT_FOUND)
- manifest status rules (separate stages; no fabricated checksums, dates or downloads)
- BLOCKING checks on the selected Top 1000 (ranks 1..1000)
- REPORT-ONLY diagnostics for records outside the selected Top 1000
- ISIN as primary identity; identity changes become candidates, never auto-approved
- manually approved symbol aliases only
- immutable snapshots under snapshots/<period>/universe.json
- point-in-time dates kept separate; estimated dates flagged; 0/30/60/90-day sensitivity

Usage:
  python3 scripts/validate_history.py sources/amfi_manifest.json [--root .]
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys

TOP_N = 1000
ISIN_RE = re.compile(r"^INE[A-Z0-9]{9}$")
SENSITIVITY_DAYS = (0, 30, 60, 90)

NOT_FOUND_LABEL = "2017H1"
NOT_DUE_LABEL = "2026H2"

DISCOVERY_STATUSES = {"DISCOVERED", "NOT_FOUND", "NOT_DUE"}
URL_STATUSES = {"BROWSER_LINK_CONFIRMED", "NOT_FOUND", "NEEDS_INVESTIGATION", "NOT_DUE"}
DOWNLOAD_STATUSES = {"DOWNLOAD_VERIFIED", "DOWNLOAD_FAILED", "NOT_DOWNLOADED", "NEEDS_INVESTIGATION", "NOT_FOUND", "NOT_DUE"}
PARSE_STATUSES = {"NOT_PARSED", "PARSED_VALID", "PARSE_FAILED"}
VALIDATION_STATUSES = {"NOT_VALIDATED", "VALID", "BLOCKED"}
PUBLICATION_STATUSES = {"UNPUBLISHED", "PUBLISHED"}


class SnapshotConflict(Exception):
    """Raised when a snapshot already exists with different content."""


def expected_periods():
    """Return the 18 expected period labels in order, with their end dates."""
    out = [("2017H2", dt.date(2017, 12, 31))]
    for y in range(2018, 2026):
        out.append((f"{y}H1", dt.date(y, 6, 30)))
        out.append((f"{y}H2", dt.date(y, 12, 31)))
    out.append(("2026H1", dt.date(2026, 6, 30)))
    assert len(out) == 18
    return out


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_manifest(manifest):
    """Check coverage and per-period status rules. Returns {errors, warnings, periods}."""
    errors, warnings = [], []
    entries = manifest.get("entries", [])
    by_label = {}
    for e in entries:
        label = e.get("period_label")
        if label in by_label:
            errors.append(f"duplicate period label {label}")
        by_label[label] = e
    expected = [lbl for lbl, _ in expected_periods()]
    missing = [lbl for lbl in expected if lbl not in by_label]
    if missing:
        errors.append(f"missing expected periods: {missing}")
    extra = [lbl for lbl in by_label if lbl not in expected]
    if extra:
        errors.append(f"unexpected periods in entries (2017H1/2026H2 belong in coverage): {extra}")
    if len(entries) != 18:
        errors.append(f"entries must contain exactly 18 periods, found {len(entries)}")
    end_dates = dict(expected_periods())
    for lbl in expected:
        e = by_label.get(lbl)
        if e is None:
            continue
        if e.get("period_end_date") != end_dates[lbl].isoformat():
            errors.append(f"{lbl}: period_end_date must be {end_dates[lbl].isoformat()}")
        for field, allowed in (("discovery_status", DISCOVERY_STATUSES), ("url_status", URL_STATUSES),
                               ("download_status", DOWNLOAD_STATUSES), ("parse_status", PARSE_STATUSES),
                               ("validation_status", VALIDATION_STATUSES), ("publication_status", PUBLICATION_STATUSES)):
            if e.get(field) not in allowed:
                errors.append(f"{lbl}: {field} invalid: {e.get(field)!r}")
        if e.get("publication_status") == "PUBLISHED":
            errors.append(f"{lbl}: publication must stay UNPUBLISHED while the permission hold is active")
        if e.get("published_date") is not None:
            errors.append(f"{lbl}: published_date must be null (no verified publication dates yet)")
        if e.get("download_status") == "DOWNLOAD_VERIFIED":
            if not e.get("evidence"):
                errors.append(f"{lbl}: DOWNLOAD_VERIFIED requires an evidence reference")
        else:
            if e.get("sha256") is not None or e.get("size_bytes") is not None:
                errors.append(f"{lbl}: checksum/size present but download is not verified")
        if e.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", e["sha256"]):
            errors.append(f"{lbl}: sha256 malformed")
    coverage = manifest.get("coverage", {})
    missing_notes = {m.get("period_label"): m for m in coverage.get("missing", [])}
    nf = missing_notes.get(NOT_FOUND_LABEL)
    if nf is None or nf.get("status") != "NOT_FOUND":
        errors.append("coverage must record 2017H1 as NOT_FOUND")
    elif "does not prove" not in (nf.get("note") or "").lower():
        errors.append("2017H1 note must say absence does not prove the file never existed")
    if NOT_DUE_LABEL not in " ".join(coverage.get("not_yet_due", [])):
        errors.append("coverage must record 2026H2 as not yet due")
    return {"errors": errors, "warnings": warnings, "period_count": len(entries)}


def validate_top1000(records):
    """BLOCKING checks for ranks 1..1000 of one period. Records are dicts with:
    rank, isin, company_name, avg_mcap_cr, nse_symbol, bse_symbol.

    Returns {errors, warnings, valid_count, outside_top1000 (report-only), missing_nse, missing_bse}.
    """
    errors, warnings = [], []
    def _in_range(r):
        k = r.get("rank")
        return isinstance(k, int) and not isinstance(k, bool) and 1 <= k <= TOP_N

    inside = [r for r in records if _in_range(r)]
    outside = [r for r in records if not _in_range(r)]

    rank_counts = {}
    for r in inside:
        rank_counts[r["rank"]] = rank_counts.get(r["rank"], 0) + 1
    missing_ranks = [k for k in range(1, TOP_N + 1) if k not in rank_counts]
    dup_ranks = sorted(k for k, n in rank_counts.items() if n > 1)
    if len(inside) != TOP_N:
        errors.append(f"expected exactly {TOP_N} ranked records in 1..{TOP_N}, found {len(inside)}")
    if missing_ranks:
        errors.append(f"missing ranks: {missing_ranks[:20]}{' ...' if len(missing_ranks) > 20 else ''}")
    if dup_ranks:
        errors.append(f"duplicate ranks: {dup_ranks[:20]}")

    isin_counts = {}
    for r in inside:
        isin_counts[r.get("isin")] = isin_counts.get(r.get("isin"), 0) + 1
    dup_isins = sorted(i for i, n in isin_counts.items() if n > 1 and i)
    if dup_isins:
        errors.append(f"duplicate ISIN identities inside Top 1000: {dup_isins[:20]}")
    bad_isin = [r.get("rank") for r in inside if not (isinstance(r.get("isin"), str) and ISIN_RE.match(r["isin"]))]
    if bad_isin:
        errors.append(f"invalid ISIN format at ranks: {bad_isin[:20]}")

    no_name = [r.get("rank") for r in inside if not (isinstance(r.get("company_name"), str) and r["company_name"].strip())]
    if no_name:
        errors.append(f"missing company name at ranks: {no_name[:20]}")

    bad_cap = [r.get("rank") for r in inside if not (_is_number(r.get("avg_mcap_cr")) and r["avg_mcap_cr"] > 0)]
    if bad_cap:
        errors.append(f"market cap missing, non-numeric, or non-positive at ranks: {bad_cap[:20]}")

    ordered = sorted((r for r in inside if _is_number(r.get("avg_mcap_cr"))), key=lambda r: r["rank"])
    order_breaks = [(a["rank"], b["rank"]) for a, b in zip(ordered, ordered[1:]) if b["avg_mcap_cr"] > a["avg_mcap_cr"]]
    if order_breaks:
        errors.append(f"market cap not descending with rank at {order_breaks[:10]}")

    missing_nse = sorted(r.get("rank") for r in inside if not r.get("nse_symbol"))
    missing_bse = sorted(r.get("rank") for r in inside if not r.get("bse_symbol"))
    if missing_nse:
        warnings.append(f"{len(missing_nse)} records without NSE symbol (kept; not blocking)")
    if missing_bse:
        warnings.append(f"{len(missing_bse)} records without BSE symbol (kept; not blocking)")

    outside_issues = []
    for r in outside:
        outside_issues.append({"rank": r.get("rank"), "isin": r.get("isin"),
                               "note": "outside Top 1000 (report-only)"})
    return {
        "errors": errors,
        "warnings": warnings,
        "valid_count": len(inside) if not errors else 0,
        "inside_count": len(inside),
        "outside_top1000": outside_issues,
        "missing_nse_ranks": missing_nse,
        "missing_bse_ranks": missing_bse,
    }


def identity_candidates(prev_records, curr_records):
    """Compare two periods by ISIN and symbol. Returns candidate rows for manual review.

    Nothing here is approved automatically.
    """
    prev_by_isin = {r["isin"]: r for r in prev_records if r.get("isin")}
    curr_by_isin = {r["isin"]: r for r in curr_records if r.get("isin")}
    rows = []
    for isin, c in curr_by_isin.items():
        p = prev_by_isin.get(isin)
        if p and p.get("nse_symbol") and c.get("nse_symbol") and p["nse_symbol"] != c["nse_symbol"]:
            rows.append({"isin": isin, "candidate_type": "symbol_change_same_isin",
                         "old_symbol": p["nse_symbol"], "new_symbol": c["nse_symbol"],
                         "company_name": c.get("company_name", ""), "status": "PENDING_REVIEW"})
    curr_sym = {}
    for r in curr_records:
        if r.get("nse_symbol"):
            curr_sym.setdefault(r["nse_symbol"], []).append(r["isin"])
    prev_sym = {}
    for r in prev_records:
        if r.get("nse_symbol"):
            prev_sym.setdefault(r["nse_symbol"], []).append(r["isin"])
    for sym, isins in curr_sym.items():
        if len(set(isins)) > 1:
            rows.append({"isin": ";".join(sorted(set(isins))), "candidate_type": "symbol_shared_by_multiple_isins",
                         "old_symbol": sym, "new_symbol": sym, "company_name": "", "status": "PENDING_REVIEW"})
    return rows


ALIAS_FIELDS = ["isin", "old_symbol", "new_symbol", "effective_date", "source", "approved_by"]


def approve_alias(row):
    """Accept an alias row only if a human approver and a source are recorded."""
    missing = [f for f in ALIAS_FIELDS if not str(row.get(f, "")).strip()]
    if missing:
        return False, f"missing fields: {missing}"
    if not ISIN_RE.match(row["isin"]):
        return False, "invalid ISIN"
    if row["old_symbol"] == row["new_symbol"]:
        return False, "old and new symbol identical"
    return True, "ok"


def load_aliases(path):
    """Read symbol_aliases.csv; return (approved_rows, rejected_rows)."""
    approved, rejected = [], []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ok, reason = approve_alias(row)
            (approved if ok else rejected).append({**row, "_reason": reason})
    return approved, rejected


def snapshot_path(root, period):
    return os.path.join(root, "snapshots", period, "universe.json")


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_snapshot(root, period, payload):
    """Write an immutable snapshot. Returns 'CREATED' or 'UNCHANGED'.

    Existing snapshot with different content raises SnapshotConflict; it is never overwritten.
    """
    path = snapshot_path(root, period)
    data = _canonical(payload)
    digest = hashlib.sha256(data).hexdigest()
    if os.path.exists(path):
        with open(path, "rb") as fh:
            existing = fh.read()
        if hashlib.sha256(existing).hexdigest() == digest:
            return "UNCHANGED"
        raise SnapshotConflict(f"{period}: existing snapshot differs; flag for review, do not overwrite")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "xb") as fh:  # 'x' fails if another writer created it meanwhile
        fh.write(data)
    return "CREATED"


def effective_dates(period_end, published=None, first_detected=None, sensitivity=SENSITIVITY_DAYS):
    """Keep period end, verified publication, first detection and estimated effective dates separate.

    - published: verified publication date or None. Never invented.
    - When published is None, sensitivity offsets are applied to period_end and flagged LOW confidence
      (publication happens after period end, so this can introduce look-ahead bias).
    """
    if published is not None:
        base, base_source, confidence = published, "verified_publication_date", "medium"
    else:
        base, base_source, confidence = period_end, "period_end_fallback", "low"
    scenarios = {f"+{d}d": (base + dt.timedelta(days=d)).isoformat() for d in sensitivity}
    return {
        "period_end_date": period_end.isoformat(),
        "published_date": published.isoformat() if published else None,
        "first_detected_date": first_detected.isoformat() if first_detected else None,
        "effective_from_estimated": scenarios["+0d"],
        "effective_base_source": base_source,
        "effective_confidence": confidence,
        "is_estimated": True,
        "sensitivity_dates": scenarios,
        "look_ahead_risk": published is None,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    report = validate_manifest(manifest)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if not report["errors"] else 2


if __name__ == "__main__":
    sys.exit(main())
