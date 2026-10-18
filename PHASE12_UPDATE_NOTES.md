# Phase 1/2 update notes

This package applies the Phase 1/2 overlay to the supplied house-monitor repository.

## Changes

- Integrated `scripts/detail_enrichment.py` into the main processing pipeline so detail-field enrichment and extraction coverage auditing run as part of the normal update.
- Added `fieldExtractionAudit` to the generated summary output.
- Added UI filters for listing lifecycle status and missing core fields (land area, building area, layout, and construction month).
- Added database maintenance and validation steps to `.github/workflows/update.yml` before committing generated data.
- Included DB maintenance, validation helpers, and Phase 1/2 tests in the source tree.
- Kept the supplied JSON data files; no database reset was performed.

## Validation performed locally

- `python -m compileall -q scripts` — passed.
- `python -m unittest discover -s tests -p 'test_*.py'` — 14 tests passed.
- `python scripts/db_maintenance.py` — passed on the supplied data.
- `python scripts/validate_db.py` — passed on the supplied data.
- Inline JavaScript in `index.html` checked with `node --check` — passed.

The workflow file copy `update.yml.txt` is synchronized with the active `update.yml` to avoid leaving a stale reference copy.
