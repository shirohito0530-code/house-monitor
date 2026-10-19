#!/usr/bin/env python3
"""One-time Phase 4 data migration helper.

Default mode is read-only and reports the expected area classification counts.
Pass --apply to write the same idempotent transformation used by the daily workflow.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import market_pipeline as pipeline  # noqa: E402


def dry_run() -> dict[str, object]:
    houses_data = pipeline.load_json(ROOT / "data" / "houses.json", {})
    discovered_data = pipeline.load_json(ROOT / "data" / "discovered_listings.json", {})
    houses = pipeline.records_from(houses_data)
    discovered = pipeline.records_from(discovered_data)
    decisions: dict[str, str] = {}
    for item in houses + discovered:
        decision, _, _ = pipeline.classify_area(item)
        decisions.setdefault(pipeline.pid(item) or f"unkeyed:{len(decisions)}", decision)
    return {
        "mode": "dry-run",
        "uniqueListingIds": len(decisions),
        "areaDecisionCounts": dict(Counter(decisions.values())),
        "quarantineRetentionDays": pipeline.QUARANTINE_RETENTION_DAYS,
        "wouldWrite": [
            "data/houses.json",
            "data/discovered_listings.json",
            "data/listing_observations.json",
            "data/summary.json",
            "data/duplicate_review_candidates.json",
        ],
        "note": "No files were changed. Use --apply to write the idempotent migration.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write Phase 4 migration output")
    parser.add_argument("--dry-run", action="store_true", help="explicitly select the read-only default mode")
    args = parser.parse_args()
    report = pipeline.apply_pipeline() if args.apply else dry_run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
