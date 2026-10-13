#!/usr/bin/env python3
"""Validate generated house-monitor JSON without confusing warnings with schema errors."""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REQUIRED = ("discovered_listings.json", "houses.json", "listing_observations.json", "summary.json")
ALLOWED_STATUSES = {"active", "possibly_ended", "observed_ended", "confirmed_ended", "unknown"}


def load_json(name: str):
    path = DATA / name
    if not path.exists():
        raise SystemExit(f"ERROR: required output missing: {path.relative_to(ROOT)}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: invalid JSON in {name}: {exc}") from exc


def get_rows(obj, name):
    if isinstance(obj, list):
        rows = obj
    elif isinstance(obj, dict):
        rows = obj.get("properties")
    else:
        rows = None
    if not isinstance(rows, list):
        raise SystemExit(f"ERROR: {name} must contain a properties list")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SystemExit(f"ERROR: {name}.properties[{i}] is not an object")
    return rows


def check_unique_ids(rows, name):
    ids = []
    missing = 0
    for row in rows:
        pid = row.get("propertyId") or row.get("id")
        if pid is None or not str(pid).strip():
            missing += 1
        else:
            ids.append(str(pid))
    duplicates = [pid for pid, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise SystemExit(f"ERROR: duplicate IDs in {name}: {duplicates[:10]}")
    if missing:
        raise SystemExit(f"ERROR: {missing} records without propertyId in {name}")
    return len(ids)


def main():
    for name in REQUIRED:
        if not (DATA / name).exists():
            raise SystemExit(f"ERROR: required output missing: data/{name}")

    discovered_obj = load_json("discovered_listings.json")
    houses_obj = load_json("houses.json")
    observations_obj = load_json("listing_observations.json")
    summary = load_json("summary.json")

    if not isinstance(discovered_obj, dict) or not discovered_obj.get("updatedAt"):
        raise SystemExit("ERROR: discovered_listings.json must be an object with updatedAt")
    if not isinstance(houses_obj, dict) or not houses_obj.get("updatedAt"):
        raise SystemExit("ERROR: houses.json must be an object with updatedAt")
    if not isinstance(summary, dict) or not summary.get("updatedAt"):
        raise SystemExit("ERROR: summary.json must be an object with updatedAt")

    discovered = get_rows(discovered_obj, "discovered_listings.json")
    houses = get_rows(houses_obj, "houses.json")
    discovered_count = check_unique_ids(discovered, "discovered_listings.json")
    houses_count = check_unique_ids(houses, "houses.json")

    for i, row in enumerate(discovered):
        status = row.get("status", "unknown")
        if status not in ALLOWED_STATUSES:
            raise SystemExit(f"ERROR: invalid status at discovered_listings.json[{i}]: {status!r}")
        if not (row.get("sourceUrl") or row.get("url")):
            print(f"WARNING: discovered record has no source URL: {row.get('propertyId')}")

    if isinstance(observations_obj, list):
        observations = observations_obj
    elif isinstance(observations_obj, dict):
        observations = observations_obj.get("observations", [])
    else:
        observations = None
    if not isinstance(observations, list):
        raise SystemExit("ERROR: listing_observations.json must contain an observations list")

    expected_discovered = summary.get("totalDiscovered")
    expected_houses = summary.get("totalMarketHouses")
    if expected_discovered is not None and expected_discovered != discovered_count:
        raise SystemExit(f"ERROR: summary totalDiscovered={expected_discovered}, actual={discovered_count}")
    if expected_houses is not None and expected_houses != houses_count:
        raise SystemExit(f"ERROR: summary totalMarketHouses={expected_houses}, actual={houses_count}")

    statuses = Counter(row.get("status", "unknown") for row in discovered)
    print("========================================")
    print("Output validation: OK")
    print("========================================")
    print(f"Discovered: {discovered_count}")
    print(f"Houses / market candidates: {houses_count}")
    print(f"Observation history: {len(observations)}")
    print(f"Listing status counts: {dict(statuses)}")
    print(f"Search incomplete targets: {summary.get('searchTargetsFailedOrIncomplete', 'n/a')}")
    print(f"Detail attempts / failures: {summary.get('detailAttemptsInRun', 'n/a')} / {summary.get('detailFailuresInRun', 'n/a')}")
    print(f"Criteria matched / pending / excluded: {summary.get('marketCriteriaMatchedCount', 'n/a')} / {summary.get('marketCriteriaPendingCount', 'n/a')} / {summary.get('marketCriteriaExcludedCount', 'n/a')}")
    print(f"Criteria pending by field: {summary.get('criteriaPendingByField', {})}")
    print(f"Criteria excluded by field: {summary.get('criteriaExcludedByField', {})}")
    field_coverage = summary.get("fieldCoverage", {})
    if isinstance(field_coverage, dict):
        for db_name in ("houses", "discovered"):
            db_coverage = field_coverage.get(db_name, {})
            fields = db_coverage.get("fields", {}) if isinstance(db_coverage, dict) else {}
            if fields:
                print(f"Field coverage ({db_name}, total={db_coverage.get('total', 0)}):")
                for field_name, metric in fields.items():
                    if isinstance(metric, dict):
                        print(f"  {field_name}: {metric.get('count', 0)} ({metric.get('ratio', 0):.1%})")
    if not houses:
        print("WARNING: houses.json is empty; inspect the criteria and area diagnostics above.")
    if summary.get("detailFetchPartial"):
        print("WARNING: detail fetching was partial; unprocessed candidates remain for later runs.")
    if summary.get("searchTargetsFailedOrIncomplete", 0):
        print("WARNING: one or more search targets were incomplete; missing listings were not treated as ended.")


if __name__ == "__main__":
    main()
