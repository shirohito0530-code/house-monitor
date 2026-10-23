# Phase 13 — Total-value valuation and multi-factor deal scoring

## What changed

- Added `scripts/property_valuation.py` with a versioned, explainable multi-factor scoring model.
- Added `scripts/data_quality_report.py`; it records field coverage, observation-history span and per-city API retrieval status in `data/data_quality_report.json` and `summary.json`.
- `scripts/market_pipeline.py` now adds a `valuation` object to each eligible canonical house and keeps the dashboard-compatible `marketIndicators` fields.
- Generates `data/valuation_results.json` as a separate, reproducible output artifact.
- Updates `summary.json` with scoring weights and model version.
- Dashboard shows estimated land/building components, price gap when sufficiently supported, component scores, confidence and limitations.
- Market dashboard no longer labels `transaction price / land area` as a pure land unit price. It is explicitly described as a rough house-transaction ratio.
- Workflow validates and commits `data/valuation_results.json`.
- Added tests for valuation components, missing data and non-target municipalities.

## Initial weights (renormalized over available components)

| Component | Weight |
|---|---:|
| Asking-price attractiveness vs estimated base value | 25 |
| Land value / land conditions | 20 |
| Building value / condition proxies | 15 |
| Location and premium features | 20 |
| Resale / holding liquidity proxies | 10 |
| Listing duration and price reductions | 5 |
| Known risk information | 5 |

These are initial rule-based weights, not statistically calibrated market coefficients. Missing components are omitted and the remaining weights are renormalized; `componentCoveragePct` indicates how much of the intended weighting was supported by data.

## Valuation safety policy

- A combined land + building base value is published only when at least three same-district transactions are available after matching basic property characteristics.
- City-wide transactions can provide context and a low-confidence land-component proxy, but are not sufficient by themselves to publish an individual property's yen-level fair value.
- The 3-month/12-month transaction counts are scoped using the stored quarter's end date as a proxy. Quarter-granular API data cannot support exact trade-date windows; missing periods are not interpreted as zero actual market transactions.
- The land component is inferred from detached-house transaction totals after subtracting a simplified depreciated replacement-cost estimate for the building. This is an approximate decomposition, not a pure land price.
- The building estimate uses assumed replacement costs and useful lives by structure. Unknown structure/year reduces confidence.
- Amenity and title-derived premiums are scored; they are not converted into arbitrary yen additions.
- School-district premium is applied only when the exact address is in the manually verified list. A candidate or unverified address is not confirmed.
- Missing hazard, legal, retaining-wall, access-road or building-condition data is not treated as evidence of safety.
- A high score alone cannot produce `優先検討`; a supported combined value estimate, non-negative value gap and non-low confidence are also required.
- `data/transactions.json` is preserved by the fetcher; the valuation pipeline reads it and writes derived outputs separately.

## Before using a result for an actual purchase

Check each property's displayed comparables, district matching, transaction period, building year/structure, land area, road/access, terrain/retaining walls, hazards, repair needs and official school-zone evidence. The model is a screening and prioritization aid, not a real-estate appraisal or a substitute for a site visit and professional inspection.
