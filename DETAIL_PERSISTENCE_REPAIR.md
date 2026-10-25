# Detail persistence repair (2026-10-25)

## Root cause

`houses.json` retained normalized property details, but `build_discovery_record()` projected each listing through `DISCOVERY_FIELDS`, which omitted core detail fields such as `landAreaM2`, `buildingAreaM2`, `layout`, `constructionYear`, `constructionMonth`, and `propertyType`. The resulting `discovered_listings.json` lost these values despite detail parsing succeeding.

## Changes

- Expanded the discovery schema to version `3.1` and added normalized detail fields and extraction audit metadata.
- Promoted values from nested detail payloads to canonical top-level fields before projecting a discovery record.
- Added exact-ID backfill from `houses.json` into matching discovery records. It uses `propertyId`, `sourceId`, `listingId`, or a unique SUUMO listing ID extracted from the URL. Ambiguous matches are skipped; no fuzzy address match is used for this repair.
- Backfill fills only missing values and does not overwrite existing non-empty values.
- The repaired discovery database is saved after `houses.json` is rebuilt, then reports per-field backfill counts.
- Detail HTTP fetching retries transient timeout/network/429/5xx errors with bounded backoff; 401/403/404 are not retried and are reported as non-transient. Raw HTML is not printed to logs.
- Added request-attempt accounting and a workflow validation step that fails if known detail facts disappear from the discovery database.
- Refreshed the bundled discovery data by backfilling exact-ID matches from the bundled `houses.json` without deleting records. The current bundled sample validates at 604 matched house/discovery IDs with zero missing values for each matched field that was available in `houses.json`.

## Files changed

- `scripts/main.py`
- `scripts/adapters/suumo_detail.py`
- `scripts/validate_detail_persistence.py` (new)
- `.github/workflows/update.yml`
- `tests/test_detail_field_extraction.py`
- `data/discovered_listings.json` (exact-ID field backfill only)
- `data/summary.json` (field coverage and persistence metadata refreshed)

## Local validation

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
python scripts/validate_detail_persistence.py
```

## Operational note

The backfill only copies values from a matching `propertyId` / `sourceId` / `listingId` / unique SUUMO listing URL. It deliberately does not infer property facts from a shared town-level address. The validation runs after `main.py` and database compaction in GitHub Actions.


## Follow-up repair v2 (2026-10-26)

The supplied package still failed validation for `constructionAgeYears`: 145 of 452 known values (32.1%) were absent from matching discovery records. Inspection showed these records stored the value as the legacy top-level `builtAgeYears`, frequently `0.0`; the discovery projection/backfill did not include that alias. Numeric zero is a valid value and must not be treated as empty.

### Follow-up changes

- Normalize both top-level and nested `detail` / `lastSuccessfulDetail` payloads.
- Normalize `builtAgeYears` and `constructionAgeYears` in both directions while preserving existing valid values.
- Keep `builtMonth` / `constructionMonth` and `builtYearMonth` / `constructionYearMonth` as separate semantic aliases; do not substitute a year-month value for a month-only field.
- Persist legacy aliases in the compact discovery projection and include them in the exact-ID backfill.
- Normalize aliases before building `houses.json` records.
- Add regression tests for zero-year age, nested aliases, precedence, exact-ID backfill, and ambiguous IDs.
- Print sample IDs and both alias values when persistence validation fails.
- Run unit tests in GitHub Actions before the live scraping workflow.
- Backfill the bundled discovery sample only from uniquely matched property IDs / listing IDs / URLs; no fuzzy matching or duplicate merging.

### Validation after follow-up repair

- `python -m compileall -q scripts tests`: passed.
- `python -m unittest discover -s tests -p 'test_*.py' -v`: 36 tests passed.
- `python scripts/validate_detail_persistence.py`: passed; 452/452 known construction-age values persisted.
- `python scripts/validate_db.py`: passed.

`buildingFootprintAreaM2` remains 0/604 in the bundled sample. This is intentionally not filled from `buildingAreaM2`: total floor area and building footprint area are different measurements. Confirm actual source-page availability and extraction evidence before changing the parser.
