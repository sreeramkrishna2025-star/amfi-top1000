# amfi-top1000

PUBLIC repository for the AMFI Top 1000 (full market-cap rank) automation.

## Purpose
- Check the official AMFI categorisation page weekly, detect new half-yearly lists, download the Excel (PDF fallback), extract ranks 1-1000, validate, and publish a versioned snapshot.
- Preserve every historical snapshot. Publish a new current version only after all validation gates pass. Keep the last valid version when anything fails.

## Source
- AMFI categorisation page: https://www.amfiindia.com/otherdata/categorisation-of-stocks
- Links are discovered by parsing the page. No URL guessing.

## Contents (this repo holds PUBLIC data only)
- `current/` latest validated Top 1000 (JSON, CSV, meta, status)
- `snapshots/<period>/` immutable historical snapshots
- `symbol_aliases.csv` manually approved old-to-new symbol mapping keyed by ISIN
- `scripts/` processing code
- `.github/workflows/` automation (standard GitHub-hosted runners only)

## Not allowed in this repo
- Tokens, API keys, Firebase secrets, credentials, or any private app data.

## Status
- Phase 1 (scaffold): in progress. Automation not yet implemented.
- See the approved architecture in the Trading Alerts project doc `claude/AMFI-Top1000-architecture.md`.
