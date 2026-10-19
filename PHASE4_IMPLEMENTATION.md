# House Monitor: 4-stage integrated implementation

## Implemented scope

1. **Area decision and quarantine**
   - `areaDecision`: `accepted`, `review`, `rejected`.
   - Explicit municipalities outside Kashiwa/Nagareyama are quarantined and excluded from the normal shortlist and market statistics.
   - A target municipality alone is not enough for acceptance: the matching target station and walk time <= 20 minutes must be available. Missing or inconsistent facts remain `review`.
   - Quarantined records are retained for 90 days before eligible records are pruned from the listing indexes. User-reviewed records are protected.
2. **Duplicate identity**
   - Source listing IDs and URLs remain unchanged.
   - Automatic logical grouping requires a detailed normalized address containing a number, matching land/building area within tolerance, and no conflicting construction year.
   - Lower-confidence candidates are written to `data/duplicate_review_candidates.json`.
   - UI decisions can be exported as `data/duplicate_review_decisions.json`; after committing that file, the next workflow run applies `same`/`different` decisions. The static GitHub Pages UI cannot write repository files directly.
3. **3-month and 12-month market metrics**
   - Area-specific listing counts, current asking-price median, first/last observed price medians, repeated-observation price change, price reductions, and history coverage.
   - Low coverage is explicitly flagged; 3m/12m views do not pretend that a short observation history is a full historical series.
4. **Explainable bargain indicators**
   - Rule-based score with displayed reasons, comparables count, and confidence.
   - Transaction comparison is only used where at least 3 comparable MLIT records are available; otherwise the score uses available price reduction, listing duration, and similar active listings and confidence is lowered.
   - These are screening indicators, not appraisals or guaranteed transaction prices.

## Official school-district policy

`config/school_districts.json` links to the official Kashiwa City school catchment page and official Kashiwanoha district map. No address is automatically marked confirmed from a station, neighborhood, or candidate flag. Only an exact normalized address deliberately added to `verifiedAddresses` after checking the official source can become confirmed. The list is empty by default.

Official references:
- https://www.city.kashiwa.lg.jp/gakkokyoiku/kyouiku/tekiseka/shogakkotsugaku.html
- https://www.city.kashiwa.lg.jp/documents/413/kashiwanoha1122.pdf

## MLIT API

- Workflow checks `REINFOLIB_API_KEY` in GitHub Actions Secrets.
- If configured, it attempts to refresh `data/transactions.json` using the existing `scripts/get_transactions.py` implementation.
- A transient API error does not stop the main listing pipeline; existing transaction data is retained and the workflow logs a warning.
- Never commit the API key into code or JSON.

## Workflow order

1. Install and compile Python code.
2. Run `scripts/main.py` to collect listings.
3. Refresh MLIT transaction data if the secret is configured.
4. Run `scripts/db_maintenance.py` to compact source DBs.
5. Run `scripts/market_pipeline.py` to apply area decisions, duplicate groups, school status, and market indicators.
6. Run DB/schema validation and commit generated JSON files.

## Local checks

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -p 'test_*.py'
python scripts/db_maintenance.py
python scripts/market_pipeline.py
python scripts/validate_db.py
```

The first run after deployment will classify the existing records. It does not clear the source JSON databases. The initial summary and duplicate-review candidate file in this package are generated from the included data snapshot.
