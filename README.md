# amfi-top1000

Code-only repository for the AMFI Top 1000 (full market-cap rank) pipeline.

> **Permission hold.** AMFI data is NOT downloaded, stored, parsed for publication, or published in this repository. Public data publication stays on hold until written permission or legal clearance from AMFI is obtained (see "Permission restrictions"). This repo currently contains code, tests, a manifest of official links, and documentation only.

## Purpose
- Build a point-in-time history of the AMFI Top 1000 universe for half-yearly categorisations from 2017H2 to 2026H1 (18 periods).
- Validate each period strictly, keep immutable historical snapshots, and record every failure instead of hiding it.

## AMFI Top 1000 methodology
- Source: AMFI categorisation page, https://www.amfiindia.com/otherdata/categorisation-of-stocks . Links are discovered by parsing the page. URLs are never guessed.
- Universe: companies ranked 1 to 1000 by AMFI "Average of All Exchanges" full market capitalisation for each six-month period.
- This is a separate universe. It is NOT NSE Nifty 1000, Nifty Total Market, or Nifty 500, and it is never substituted with them.

## Historical period coverage
- Expected: 2017H2 through 2026H1 (18 periods). Listed in `sources/amfi_manifest.json`.
- 2017H1: NOT_FOUND on the official page during investigation. This does not prove the file never existed.
- 2026H2: not yet due (period ends 2026-12-31).
- Download status today: only the two Phase A probe files (2020H2 and 2024H1 Excel) are recorded as DOWNLOAD_VERIFIED, by GitHub Actions run 43848842044. No checksum is stored in the repo for them. All other periods are NOT_DOWNLOADED (permission hold).

## ISIN identity approach
- ISIN is the primary company identity. Symbols are attributes, not identity.
- Symbol changes are not approved automatically. Uncertain mappings go to `review/identity_candidates.csv` with status `PENDING_REVIEW`.
- `symbol_aliases.csv` holds only manually approved rows. A row needs `approved_by` and `source`, and old and new symbols must differ (enforced by `validate_history.approve_alias`).
- Missing NSE or BSE symbols never remove a company. They are warnings.

## Validation rules
Blocking, for the selected Top 1000 (ranks 1 to 1000):
- Exactly 1,000 ranked records.
- Ranks are integers, with no missing or duplicate ranks.
- ISINs match `INE` + 9 alphanumerics, and are unique.
- Company name present.
- Average market cap is a positive, finite number.
- Market cap is non-increasing as rank increases.
- Fractional, text, or boolean ranks are never truncated or counted.

Report-only:
- Records outside ranks 1 to 1000 (reported separately, do not block).
- Missing NSE or BSE symbols.
- Identity candidates (symbol changes, shared symbols).

A period that fails a blocking check is not published as a complete Top 1000 universe.

## Point-in-time dates
Kept as separate fields, never merged:
- `period_end_date`: 30 June or 31 December.
- `published_date`: verified only. Currently null for every period.
- `first_detected_date`: when this system first saw the period. Currently null.
- `effective_from_estimated`: estimate, always flagged `is_estimated`. When no verified publication date exists it falls back to the period end and is marked low confidence with look-ahead risk.
- Sensitivity: 0, 30, 60 and 90-day offsets are generated for every period (`validate_history.effective_dates`).

## Snapshot structure
- `snapshots/<period>/universe.json`, for example `snapshots/2026H1/universe.json`.
- Snapshots are immutable. Writing identical content is a no-op. Writing different content to an existing snapshot raises `SnapshotConflict`, and the file is left unchanged for review.
- No snapshot exists in the repository yet.

## Scripts
- `scripts/discover.py`: parses the AMFI page and reports official `.xlsx` and `.pdf` links. Read-only.
- `scripts/parse_excel.py`: parses one AMFI Excel file (header detection, rank and money parsing, Top 1000 checks).
- `scripts/fetch_amfi.py`: validated, size-limited, checksummed downloader. HTTPS and official hosts only. Validated redirects. Retries only transient 5xx and timeouts, never 403 or other 4xx. Rejects HTML bodies and invalid workbooks. Refuses to run without `--i-understand-permission-hold`.
- `scripts/validate_history.py`: manifest coverage and status rules, Top 1000 checks, identity candidates, alias approval, immutable snapshots, point-in-time dates.

## Tests
Run from the repo root:

    python3 -m unittest discover -s tests -p "test_*.py" -v

The suite uses synthetic fixtures and a fake transport only. It makes no network requests and does not use AMFI data. Requires `openpyxl` for the parser tests.

## Manifest
`sources/amfi_manifest.json` (`manifest_version: gate-b-1`) keeps separate statuses per period:
`discovery_status`, `url_status`, `download_status`, `parse_status`, `validation_status`, `publication_status`.
No checksum, file size, publication date, or download status is filled in unless it was actually observed. Values not yet observed are null.

## Permission restrictions
AMFI's [Terms of Use](https://www.amfiindia.com/terms-of-use) (as read on the investigation date; not a legal opinion) say:
- Access is for personal, non-commercial use, under a limited and revocable right.
- Site content is protected by copyright and other IP rights. Use beyond the permitted uses needs written agreement.
- Publicly displaying, transmitting or publishing the content is prohibited.
- Storing any significant portion electronically is prohibited.
- Creating derivative works based on the content is prohibited.
- Selling or transferring the content is prohibited.

What written permission would be needed, per use:
1. **Downloading and processing:** AMFI's written consent to automated downloading and parsing of the Excel files.
2. **Electronic storage:** consent to store the files and derived tables, including historical snapshots.
3. **Historical snapshots:** consent for the 18-period derived universe.
4. **App or watchlist use:** consent for use inside a private app. Confirm whether the app use is personal and non-commercial.
5. **Public redistribution or display:** consent before any public repository or website publishes AMFI data or derivatives.

Until these are clarified: do not publish AMFI files, parsed lists, snapshots, or derived datasets. Changing the repo to private does not by itself resolve the terms.

## Workflow limitations
- `.github/workflows/amfi-update.yml` is manual (`workflow_dispatch`) only. There is no schedule.
- The `test` job runs the synthetic suite and does not contact AMFI.
- The `fetch-validate` job is gated. It runs only if the input `permission_cleared` is `true` and the repository variable `AMFI_PERMISSION_CLEARED` is `true`. The input's only choice is `false`, so it cannot run as checked in. It contains a placeholder step, not real download logic.
- There is no publish job.
- Repository activity: GitHub disables scheduled workflows in public repos after 60 days without repository activity. There is no schedule now, so this does not apply. If a schedule is added later, it may be disabled after 60 days of inactivity. Artificial heartbeat commits are not used.
- The Phase A probe workflow (`amfi-access-probe.yml`) is unchanged.

## Manual recovery and re-enable
- If a scheduled workflow is ever disabled: Actions tab, select the workflow, then "Enable workflow". Re-run it manually and check the summary.
- To enable the gated job, all of these must be true: written permission recorded, the input and variable set as described above, and a reviewed change that adds the real download logic.
- If a checksum for a verified source changes, flag it for review. Never overwrite the snapshot.

## Status
- Gate B (code-only implementation, synthetic tests, manifest, review file, documentation): see the commit log.
- Historical download, parsing for publication, snapshots, and publication: on hold.
- Phase B architecture reference: `claude/AMFI-Top1000-architecture.md` in the Trading Alerts project.
