#!/usr/bin/env python3
"""Read-only audit of listing volume, identity duplication, and detail coverage.

This script deliberately does not merge, delete, or rewrite any database.
Run: python scripts/audit_detail_and_duplicates.py
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "address": ("address", "fullAddress", "propertyAddress"),
    "landAreaM2": ("landAreaM2", "landArea", "land"),
    "buildingAreaM2": ("buildingAreaM2", "buildingArea", "building"),
    "buildingFootprintAreaM2": ("buildingFootprintAreaM2", "buildingFootprintArea"),
    "layout": ("layout", "layoutRaw", "floorPlan", "floor_plan"),
    "constructionYear": ("constructionYear", "yearBuilt", "builtYear"),
    "constructionYearMonth": ("constructionYearMonth", "constructionMonth"),
    "propertyType": ("propertyType", "listingType", "type"),
    "targetStationWalkMinutes": (
        "targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes"
    ),
}
EMPTY_STRINGS = {"", "-", "－", "不明", "未定", "なし", "N/A", "n/a"}


def load_rows(filename: str) -> list[dict[str, Any]]:
    path = DATA / filename
    if not path.exists():
        raise FileNotFoundError(f"required data file missing: {path.relative_to(ROOT)}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("properties", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{filename}: expected list or object with properties list")
    return [row for row in rows if isinstance(row, dict)]


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in EMPTY_STRINGS
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


def first_value(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for key in aliases:
        value = row.get(key)
        if has_value(value):
            return value
    return None


def listing_id(row: dict[str, Any]) -> str | None:
    for field in ("sourceId", "listingId"):
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    url = row.get("sourceUrl") or row.get("detailUrl") or row.get("url")
    url = unicodedata.normalize("NFKC", str(url or ""))
    match = re.search(r"/(nc_\d+)(?:/|$|[?#])", url, re.I)
    return match.group(1).lower() if match else None


def property_id(row: dict[str, Any]) -> str:
    value = row.get("propertyId") or row.get("id")
    return str(value).strip() if value is not None else ""


def identity_key(row: dict[str, Any]) -> str | None:
    value = row.get("physicalIdentityKey")
    if value and str(value).strip():
        return str(value).strip()
    # Only use a persisted identity key. Do not invent a fuzzy key from partial address data.
    return None


def duplicate_groups(
    rows: list[dict[str, Any]],
    key_fn,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = key_fn(row)
        if key:
            groups[str(key)].append(row)
    result = []
    for key, group in groups.items():
        distinct_ids = sorted({property_id(row) or "(missing propertyId)" for row in group})
        if len(group) > 1:
            result.append({
                "key": key,
                "recordCount": len(group),
                "distinctPropertyIds": len(distinct_ids),
                "propertyIds": distinct_ids[:20],
                "statuses": dict(Counter(str(row.get("status") or "unknown") for row in group)),
            })
    return sorted(result, key=lambda item: (-item["recordCount"], item["key"]))


def coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    total = len(rows)
    for field, aliases in DETAIL_FIELDS.items():
        count = sum(1 for row in rows if has_value(first_value(row, aliases)))
        result[field] = {
            "count": count,
            "total": total,
            "ratio": round(count / total, 4) if total else 0.0,
        }
    return result


def main() -> int:
    discovered = load_rows("discovered_listings.json")
    houses = load_rows("houses.json")

    statuses = Counter(str(row.get("status") or "unknown") for row in discovered)
    discovered_ids = Counter(property_id(row) for row in discovered if property_id(row))
    listing_ids = Counter(listing_id(row) for row in discovered if listing_id(row))

    exact_property_id_dupes = [
        {"propertyId": key, "count": count}
        for key, count in discovered_ids.items() if count > 1
    ]
    exact_listing_id_dupes = [
        {"listingId": key, "count": count}
        for key, count in listing_ids.items() if count > 1
    ]

    physical_groups = duplicate_groups(discovered, identity_key)
    insufficient_identity = sum(
        1 for row in discovered
        if not identity_key(row)
        and not has_value(first_value(row, ("address", "fullAddress", "propertyAddress")))
        and not has_value(first_value(row, ("landAreaM2", "landArea", "land")))
    )

    summary = {
        "discoveredRecordCount": len(discovered),
        "housesRecordCount": len(houses),
        "discoveredStatusCounts": dict(statuses),
        "uniquePropertyIdCount": len(discovered_ids),
        "exactDuplicatePropertyIdGroups": len(exact_property_id_dupes),
        "exactDuplicatePropertyIdExtraRecords": sum(
            item["count"] - 1 for item in exact_property_id_dupes
        ),
        "uniqueListingIdCount": len(listing_ids),
        "duplicateListingIdGroups": len(exact_listing_id_dupes),
        "duplicateListingIdExtraRecords": sum(
            item["count"] - 1 for item in exact_listing_id_dupes
        ),
        "physicalIdentityKeyGroupsWithMultipleRecords": len(physical_groups),
        "physicalIdentityKeyExtraRecords": sum(
            item["recordCount"] - 1 for item in physical_groups
        ),
        "recordsWithoutPhysicalKeyAddressAndLandArea": insufficient_identity,
        "detailCoverageDiscovered": coverage(discovered),
        "detailCoverageHouses": coverage(houses),
        "topDuplicatePropertyIds": sorted(
            exact_property_id_dupes, key=lambda item: -item["count"]
        )[:20],
        "topDuplicateListingIds": sorted(
            exact_listing_id_dupes, key=lambda item: -item["count"]
        )[:20],
        "topPhysicalIdentityGroups": physical_groups[:30],
        "notes": [
            "Read-only report: no files are modified.",
            "Repeated listing IDs are identity/data-quality signals, not permission to delete records.",
            "Different listing IDs with the same physicalIdentityKey are candidate groups, not automatic merge instructions.",
            "buildingFootprintAreaM2 is separate from buildingAreaM2; one must not be substituted for the other.",
            "A zero coverage ratio alone does not prove parser failure; inspect real page HTML and extraction audit first.",
        ],
    }

    print("=== House Monitor: detail and duplicate audit (READ ONLY) ===")
    print(f"discovered_listings: {len(discovered)}")
    print(f"houses: {len(houses)}")
    print(f"statuses: {dict(statuses)}")
    print(
        "duplicate propertyId groups / extra records: "
        f"{summary['exactDuplicatePropertyIdGroups']} / "
        f"{summary['exactDuplicatePropertyIdExtraRecords']}"
    )
    print(
        "duplicate listingId groups / extra records: "
        f"{summary['duplicateListingIdGroups']} / "
        f"{summary['duplicateListingIdExtraRecords']}"
    )
    print(
        "physicalIdentityKey groups / extra records: "
        f"{summary['physicalIdentityKeyGroupsWithMultipleRecords']} / "
        f"{summary['physicalIdentityKeyExtraRecords']}"
    )
    print(f"records with insufficient identity fields: {insufficient_identity}")
    print("\nDetail coverage (discovered_listings.json):")
    for field, metric in summary["detailCoverageDiscovered"].items():
        print(f"  {field}: {metric['count']}/{metric['total']} ({metric['ratio']:.1%})")
    print("\nDetail coverage (houses.json):")
    for field, metric in summary["detailCoverageHouses"].items():
        print(f"  {field}: {metric['count']}/{metric['total']} ({metric['ratio']:.1%})")

    print("\nTop duplicate listing IDs:")
    for item in summary["topDuplicateListingIds"][:10]:
        print(f"  {item['listingId']}: {item['count']} records")
    print("\nTop repeated physical identity keys:")
    for item in summary["topPhysicalIdentityGroups"][:10]:
        print(
            f"  {item['key']}: records={item['recordCount']}, "
            f"distinct propertyIds={item['distinctPropertyIds']}"
        )

    # Machine-readable output is stdout only. No DB or report file is written.
    print("\n=== JSON SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import sys
        print(f"[AUDIT-ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
