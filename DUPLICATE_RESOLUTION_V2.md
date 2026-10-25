# Duplicate resolution v2 — 2026-10-25

## What changed

- `scripts/main.py` now prioritizes detail-page fetches for incomplete records that share a plausible target-city address block. This is a fetch-priority hint only; it does not merge records.
- Missing physical-identity fields are measured from actual values, not `detailFetchStatus=success` alone.
- Duplicate-focused retries can run up to five total detail attempts for a listing (the ordinary fetch policy remains unchanged for other listings).
- `scripts/market_pipeline.py` only generates duplicate pairs from sufficiently detailed addresses in the target municipalities and avoids broad town-level candidate floods.
- Pairs missing core identity fields go to `detailEnrichmentQueue` in `data/duplicate_review_candidates.json` rather than immediately becoming manual-review tasks.
- Automatic logical merge requires detailed-address equality, matching land/building areas, compatible construction year, and no type/layout conflict.
- Exact detailed-address pairs with contradictory extracted areas remain reviewable because extraction may be inconsistent; partial-address pairs with strong physical conflicts are suppressed.
- Existing `same` / `different` user decisions take precedence over the new automatic rules.
- Listing IDs, URLs, and observations remain intact. Grouping is logical; no source listing record is deleted by duplicate resolution.
- The GitHub Actions workflow prints review-candidate and enrichment-queue counts after the market pipeline.

## Why this reduces manual candidates

Town/chome-only matches are not emitted as review candidates. Missing details are routed to the enrichment queue, and candidates are only shown when the addresses are sufficiently specific and the core physical fields are available. Similarity score alone is not enough to create a review item.

## Validation

- Python compile check passed.
- `python -m unittest discover -s tests -v`: 29 tests passed.
- `python scripts/market_pipeline.py` ran against the included data copy; generated JSON files parsed successfully.
- In the included data snapshot, the new pipeline produced 0 manual-review candidates and 2 detail-enrichment queue records. This is a snapshot-specific local result, not a promise that future runs will always have zero candidates.

## Operational notes

- The next GitHub Actions run should be checked for `重複詳細優先` in the main pipeline logs and `Missing-detail enrichment queue` in the duplicate resolution report.
- If the same queue item remains incomplete after five attempts, inspect the detail parser and the source page manually rather than retrying indefinitely.
- No real GitHub Actions run or live SUUMO request was performed during local validation.
