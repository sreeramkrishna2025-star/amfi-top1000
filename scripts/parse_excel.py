"""Parse an official AMFI Average Market Capitalisation Excel file.

Outputs:
  - Top 1000 records (rank 1..1000) with BLOCKING validation checks.
  - Full-file report-only checks across ALL numbered rows (duplicates, invalid ranks).
Read-only with respect to inputs; writes only to the --out path you give.
The workbook is always closed, on success and on error paths.

Usage:
  python3 scripts/parse_excel.py file.xlsx --out result.json
Exit codes: 0 = no blocking errors, 2 = blocking errors or unreadable file.
"""
import argparse
import json
import re
import sys

import openpyxl

ISIN_RE = re.compile(r"^INE[A-Z0-9]{9}$")
TOP_N = 1000
HEADER_SCAN_ROWS = 30

# Logical column -> documented header synonyms (matched against normalised header text).
HEADER_SYNONYMS = {
    "rank": [r"^sr\.?\s*no\.?$", r"^sr\.?\s*no", r"^serial\s*no", r"^rank$"],
    "company_name": [r"^company\s*name", r"^name\s*of\s*(the\s*)?company$", r"^company$"],
    "isin": [r"^isin(\s*code)?$"],
    "bse_symbol": [r"^bse\s*(symbol|code|ticker)"],
    "nse_symbol": [r"^nse\s*(symbol|code|ticker)"],
    "avg_mcap_cr": [r"^average\s*of\s*all\s*exchanges", r"^avg.*all\s*exchanges"],
    "sebi_category": [r"^categori[sz]ation", r"^sebi\s*categor"],
}
REQUIRED = ["rank", "company_name", "isin", "nse_symbol", "bse_symbol", "avg_mcap_cr", "sebi_category"]


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def match_header_row(cells):
    """Map logical columns to column indexes in one header row. First synonym match wins."""
    norm = [_norm(c) for c in cells]
    cmap, matched = {}, {}
    for key, patterns in HEADER_SYNONYMS.items():
        for j, c in enumerate(norm):
            if not c or j in cmap.values():
                continue
            if any(re.search(p, c) for p in patterns):
                cmap[key] = j
                matched[key] = cells[j]
                break
    return cmap, matched


def find_header(wb):
    """Scan every sheet's first rows; pick the (sheet, row) matching the most required columns."""
    best = None
    for ws in wb.worksheets:
        for i, row in enumerate(ws.iter_rows(max_row=HEADER_SCAN_ROWS, values_only=True)):
            cmap, matched = match_header_row(list(row))
            score = sum(1 for k in REQUIRED if k in cmap)
            if score and (best is None or score > best["score"]):
                best = {"score": score, "sheet": ws.title, "row": i, "cmap": cmap, "matched": matched}
    return best


def parse_rank(v):
    """Return (rank_int_or_None, status). status: 'ok' | 'blank' | 'text' | 'invalid'.

    'invalid' = numeric-looking but not a genuine integer (e.g. 1.5, '1.5', 'A1').
    'text'    = plain text (footnotes, titles) -> ignored, not counted as data.
    Fractional values are NEVER truncated.
    """
    if v is None:
        return None, "blank"
    if isinstance(v, bool):
        return None, "invalid"
    if isinstance(v, int):
        return v, "ok"
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None, "invalid"
        return (int(v), "ok") if v.is_integer() else (None, "invalid")
    s = str(v).strip()
    if not s:
        return None, "blank"
    if re.fullmatch(r"\d+", s):
        return int(s), "ok"
    if re.fullmatch(r"\d+\.0+", s):
        return None, "invalid"  # text "1.0": ambiguous, keep out of data rather than guess
    if re.search(r"\d", s):
        return None, "invalid"
    return None, "text"


def parse_money(v):
    """Return (float_or_None, status). status: 'ok' | 'blank' | 'invalid'. Never raises."""
    if v is None:
        return None, "blank"
    if isinstance(v, bool):
        return None, "invalid"
    if isinstance(v, (int, float)):
        f = float(v)
        return (f, "ok") if f == f and abs(f) != float("inf") else (None, "invalid")
    s = str(v).strip().replace(",", "")
    if not s or s == "-":
        return None, "blank"
    try:
        f = float(s)
    except ValueError:
        return None, "invalid"
    return (f, "ok") if f == f and abs(f) != float("inf") else (None, "invalid")


def _clean_symbol(v):
    s = str(v).strip() if v is not None else ""
    return None if s in ("", "-") else s


def parse(path):
    result = {"source_file": path, "sheet": None, "header_row": None, "columns": {},
              "matched_headers": {}, "records": [], "errors": [], "warnings": [],
              "checks": {}, "full_file": {}}
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # never crash with a traceback
        result["errors"].append(f"cannot read workbook: {type(e).__name__}: {e}")
        return result
    try:
        _parse_workbook(wb, result)
    except Exception as e:  # unexpected sheet content: report, never crash
        result["errors"].append(f"parse failed: {type(e).__name__}: {e}")
    finally:
        wb.close()
    return result


def _parse_workbook(wb, result):
    hdr = find_header(wb)
    if hdr is None or any(k not in hdr["cmap"] for k in REQUIRED):
        missing = [] if hdr is None else [k for k in REQUIRED if k not in hdr["cmap"]]
        result["errors"].append(f"header not found or required columns missing: {missing or 'all'}")
        if hdr:
            result["sheet"], result["header_row"] = hdr["sheet"], hdr["row"]
            result["columns"], result["matched_headers"] = hdr["cmap"], hdr["matched"]
        return

    ws = wb[hdr["sheet"]]
    cmap = hdr["cmap"]
    result.update({"sheet": hdr["sheet"], "header_row": hdr["row"], "columns": cmap,
                   "matched_headers": hdr["matched"]})

    all_ranks, all_isins = [], []
    invalid_rank_rows, invalid_cap_ranks, missing_cap_ranks = [], [], []
    numbered_rows = 0

    def cell(row, key):
        idx = cmap[key]
        return row[idx] if idx < len(row) else None

    for excel_row, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if excel_row <= hdr["row"] + 1:  # skip rows up to and including the header
            continue
        row = list(row)
        rank, status = parse_rank(cell(row, "rank"))
        if status == "invalid":
            invalid_rank_rows.append(excel_row)
            continue
        if status != "ok":
            continue
        numbered_rows += 1
        all_ranks.append(rank)
        isin = str(cell(row, "isin") or "").strip()
        all_isins.append(isin)
        if not (1 <= rank <= TOP_N):
            continue
        cap, cap_status = parse_money(cell(row, "avg_mcap_cr"))
        if cap_status == "invalid":
            invalid_cap_ranks.append(rank)
        elif cap_status == "blank":
            missing_cap_ranks.append(rank)
        result["records"].append({
            "rank": rank,
            "company_name": str(cell(row, "company_name") or "").strip(),
            "isin": isin,
            "nse_symbol": _clean_symbol(cell(row, "nse_symbol")),
            "bse_symbol": _clean_symbol(cell(row, "bse_symbol")),
            "avg_mcap_cr": cap,
            "sebi_category": str(cell(row, "sebi_category") or "").strip(),
            "excel_row": excel_row,
        })

    result["records"].sort(key=lambda x: (x["rank"], x["excel_row"]))
    result["full_file"] = full_file_checks(all_ranks, all_isins, numbered_rows, invalid_rank_rows)
    top = top1000_checks(result["records"], invalid_cap_ranks, missing_cap_ranks)
    result["checks"] = top["checks"]
    result["errors"].extend(top["errors"])
    result["warnings"].extend(top["warnings"])


def full_file_checks(ranks, isins, numbered_rows, invalid_rank_rows):
    """REPORT-ONLY: checks across all numbered rows in the source file (not blocking)."""
    rank_counts, isin_counts = {}, {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    for i in isins:
        if i:
            isin_counts[i] = isin_counts.get(i, 0) + 1
    dup_ranks = sorted(k for k, n in rank_counts.items() if n > 1)
    dup_isins = sorted(k for k, n in isin_counts.items() if n > 1)
    return {
        "numbered_rows": numbered_rows,
        "min_rank": min(ranks) if ranks else None,
        "max_rank": max(ranks) if ranks else None,
        "duplicate_ranks": dup_ranks,
        "duplicate_isins": dup_isins,
        "invalid_rank_rows_excel": invalid_rank_rows,
        "note": "report-only; does not block publishing",
    }


def top1000_checks(recs, invalid_cap_ranks, missing_cap_ranks):
    """BLOCKING checks for ranks 1..1000."""
    errors, warnings = [], []
    ranks = [r["rank"] for r in recs]
    rank_counts = {}
    for k in ranks:
        rank_counts[k] = rank_counts.get(k, 0) + 1
    missing_ranks = [k for k in range(1, TOP_N + 1) if k not in rank_counts]
    dup_ranks = sorted(k for k, n in rank_counts.items() if n > 1)
    isins = [r["isin"] for r in recs]
    isin_counts = {}
    for i in isins:
        isin_counts[i] = isin_counts.get(i, 0) + 1
    dup_isins = sorted(i for i, n in isin_counts.items() if n > 1)
    bad_isin = [r["isin"] for r in recs if not ISIN_RE.match(r["isin"])]
    missing_nse = [r["rank"] for r in recs if r["nse_symbol"] is None]
    missing_bse = [r["rank"] for r in recs if r["bse_symbol"] is None]
    no_symbol_at_all = [r["rank"] for r in recs if r["nse_symbol"] is None and r["bse_symbol"] is None]
    missing_name = [r["rank"] for r in recs if not r["company_name"]]
    missing_cat = [r["rank"] for r in recs if not r["sebi_category"]]
    nonpositive_cap = [r["rank"] for r in recs if r["avg_mcap_cr"] is not None and r["avg_mcap_cr"] <= 0]
    caps = [(r["rank"], r["avg_mcap_cr"]) for r in recs if r["avg_mcap_cr"] is not None]
    non_desc = [(a[0], b[0]) for a, b in zip(caps, caps[1:]) if b[1] > a[1]]

    if len(recs) != TOP_N:
        errors.append(f"expected {TOP_N} records for ranks 1-1000, got {len(recs)}")
    if missing_ranks:
        errors.append(f"missing ranks: {missing_ranks[:20]}{'...' if len(missing_ranks) > 20 else ''}")
    if dup_ranks:
        errors.append(f"duplicate ranks within 1-1000: {dup_ranks}")
    if dup_isins:
        errors.append(f"duplicate ISINs within Top 1000: {dup_isins[:10]}")
    if bad_isin:
        errors.append(f"bad ISIN format: {bad_isin[:10]}")
    if missing_name:
        errors.append(f"missing company names at ranks: {missing_name[:10]}")
    if missing_cat:
        errors.append(f"missing SEBI category at ranks: {missing_cat[:10]}")
    if invalid_cap_ranks:
        errors.append(f"non-numeric average market cap at ranks: {invalid_cap_ranks[:10]}")
    if missing_cap_ranks:
        errors.append(f"blank average market cap at ranks: {missing_cap_ranks[:10]}")
    if nonpositive_cap:
        errors.append(f"zero or negative average market cap at ranks: {nonpositive_cap[:10]}")
    if non_desc:
        errors.append(f"average cap not descending in {len(non_desc)} places (first: {non_desc[:3]})")
    if no_symbol_at_all:
        warnings.append(f"no NSE and no BSE symbol at ranks: {no_symbol_at_all[:20]} (kept, reported)")
    return {
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "records_extracted": len(recs),
            "first_rank": ranks[0] if ranks else None,
            "last_rank": ranks[-1] if ranks else None,
            "missing_ranks": missing_ranks,
            "duplicate_ranks": dup_ranks,
            "duplicate_isins": dup_isins,
            "bad_isin": bad_isin,
            "nonpositive_cap_ranks": nonpositive_cap,
            "missing_nse_symbol_ranks": missing_nse,
            "missing_nse_symbol_count": len(missing_nse),
            "missing_bse_symbol_ranks": missing_bse,
            "missing_bse_symbol_count": len(missing_bse),
            "no_symbol_at_all_ranks": no_symbol_at_all,
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    res = parse(args.xlsx)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    c = res["checks"]
    print(json.dumps({
        "sheet": res["sheet"], "header_row": res["header_row"], "columns": res["columns"],
        "records_extracted": c.get("records_extracted"), "first_rank": c.get("first_rank"),
        "last_rank": c.get("last_rank"), "missing_nse_symbol_count": c.get("missing_nse_symbol_count"),
        "missing_bse_symbol_count": c.get("missing_bse_symbol_count"),
        "full_file": res["full_file"], "warnings": res["warnings"], "errors": res["errors"],
    }, indent=1, ensure_ascii=False))
    return 0 if not res["errors"] else 2


if __name__ == "__main__":
    sys.exit(main())
