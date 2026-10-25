
#!/usr/bin/env python3
"""Read-only audit of station-walk extraction and persistence.

Usage:
    python scripts/audit_station_walk.py

This script does not modify any database or JSON file.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

WALK_FIELDS = (
    "targetStationWalkMinutes",
    "stationWalkMinutes",
    "walkMinutes",
)

DETAIL_CONTAINERS = (
    "detail",
    "lastSuccessfulDetail",
)

EMPTY_VALUES = {
    "",
    "-",
    "－",
    "不明",
    "未定",
    "なし",
    "N/A",
    "n/a",
}


def load_rows(filename: str, key: str = "properties") -> list[dict[str, Any]]:
    path = DATA / filename

    if not path.exists():
        raise FileNotFoundError(
            f"Required file not found: {path.relative_to(ROOT)}"
        )

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get(key, [])
    else:
        raise ValueError(f"{filename}: unsupported JSON structure")

    if not isinstance(rows, list):
        raise ValueError(f"{filename}: expected a list under '{key}'")

    return [row for row in rows if isinstance(row, dict)]


def usable(value: Any) -> bool:
    if value is None:
        return False

    if isinstance(value, str):
        return value.strip() not in EMPTY_VALUES

    return True


def parse_walk(value: Any) -> float | None:
    """Parse a numeric walking time without inventing missing values."""
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).replace(",", "").replace("，", "")
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(match.group(0))

    # Reject implausible values instead of silently accepting them.
    if not 1 <= number <= 120:
        return None

    return number


def nested_details(row: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result = []

    for container in DETAIL_CONTAINERS:
        value = row.get(container)
        if isinstance(value, dict):
            result.append((container, value))

    return result


def first_walk(
    row: dict[str, Any],
    containers: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[float | None, str | None, Any]:
    """Return first valid walk time, its source path, and original value."""
    for field in WALK_FIELDS:
        value = row.get(field)
        number = parse_walk(value)
        if number is not None:
            return number, field, value

    for container_name, detail in containers or nested_details(row):
        for field in WALK_FIELDS:
            value = detail.get(field)
            number = parse_walk(value)
            if number is not None:
                return number, f"{container_name}.{field}", value

    return None, None, None


def property_id(row: dict[str, Any]) -> str:
    return str(
        row.get("propertyId")
        or row.get("id")
        or row.get("sourceId")
        or row.get("listingId")
        or row.get("sourceUrl")
        or row.get("detailUrl")
        or "(unknown)"
    )


def inspect_row(row: dict[str, Any]) -> dict[str, Any]:
    details = nested_details(row)
    walk, walk_path, raw_walk = first_walk(row, details)

    top_walk = any(
        parse_walk(row.get(field)) is not None
        for field in WALK_FIELDS
    )

    nested_walk = any(
        parse_walk(detail.get(field)) is not None
        for _, detail in details
        for field in WALK_FIELDS
    )

    all_objects = [("record", row), *details]

    available_values = [
        (name, obj.get("targetStationWalkAvailable"))
        for name, obj in all_objects
        if "targetStationWalkAvailable" in obj
    ]

    available_true = any(value is True for _, value in available_values)
    available_false = any(value is False for _, value in available_values)

    station = None
    walk_source = None
    transport_raw = None

    for name, obj in all_objects:
        if not station:
            station = obj.get("targetStation") or obj.get("station")
        if not walk_source:
            walk_source = obj.get("targetStationWalkSource")
        if not transport_raw:
            transport_raw = obj.get("transportRaw")

    if top_walk:
        category = "top_level_walk"
    elif nested_walk:
        category = "nested_detail_only"
    elif usable(transport_raw):
        category = "raw_transport_without_walk"
    elif available_true:
        category = "available_flag_without_walk"
    else:
        category = "no_walk_evidence"

    if walk is not None and available_false and not available_true:
        category = "walk_value_but_unavailable_flag"

    return {
        "propertyId": property_id(row),
        "walkMinutes": walk,
        "walkPath": walk_path,
        "rawWalkValue": raw_walk,
        "topLevelWalk": top_walk,
        "nestedWalk": nested_walk,
        "availableValues": available_values,
        "targetStation": station,
        "targetStationWalkSource": walk_source,
        "transportRaw": transport_raw,
        "category": category,
    }


def coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    inspected = [inspect_row(row) for row in rows]
    categories = Counter(item["category"] for item in inspected)

    top_count = sum(item["topLevelWalk"] for item in inspected)
    nested_count = sum(item["nestedWalk"] for item in inspected)
    valid_count = sum(item["walkMinutes"] is not None for item in inspected)

    return {
        "total": total,
        "validWalkCount": valid_count,
        "validWalkPct": round(valid_count / total * 100, 1) if total else 0,
        "topLevelWalkCount": top_count,
        "topLevelWalkPct": round(top_count / total * 100, 1) if total else 0,
        "nestedWalkCount": nested_count,
        "nestedWalkPct": round(nested_count / total * 100, 1) if total else 0,
        "categories": dict(categories),
        "examples": {
            category: [
                item for item in inspected
                if item["category"] == category
            ][:10]
            for category in (
                "nested_detail_only",
                "walk_value_but_unavailable_flag",
                "raw_transport_without_walk",
                "available_flag_without_walk",
                "no_walk_evidence",
            )
        },
    }


def main() -> int:
    discovered = load_rows("discovered_listings.json")
    houses = load_rows("houses.json")

    discovered_report = coverage(discovered)
    houses_report = coverage(houses)

    discovered_by_id = {
        property_id(row): inspect_row(row)
        for row in discovered
    }

    houses_by_id = {
        property_id(row): inspect_row(row)
        for row in houses
    }

    # Compare IDs present in both files. These are diagnostic signals only:
    # do not automatically overwrite or repair records based on this report.
    shared_ids = set(discovered_by_id) & set(houses_by_id)
    comparison = {
        "sharedPropertyIdCount": len(shared_ids),
        "discoveredHasWalkButHousesMissing": [],
        "housesHasWalkButDiscoveredMissing": [],
    }

    for pid in sorted(shared_ids):
        discovered_item = discovered_by_id[pid]
        houses_item = houses_by_id[pid]

        if (
            discovered_item["walkMinutes"] is not None
            and houses_item["walkMinutes"] is None
        ):
            comparison["discoveredHasWalkButHousesMissing"].append({
                "propertyId": pid,
                "discoveredWalk": discovered_item["walkMinutes"],
                "discoveredPath": discovered_item["walkPath"],
            })

        if (
            houses_item["walkMinutes"] is not None
            and discovered_item["walkMinutes"] is None
        ):
            comparison["housesHasWalkButDiscoveredMissing"].append({
                "propertyId": pid,
                "housesWalk": houses_item["walkMinutes"],
                "housesPath": houses_item["walkPath"],
            })

    report = {
        "readOnly": True,
        "discovered": discovered_report,
        "houses": houses_report,
        "crossFileComparison": comparison,
        "interpretation": [
            "nested_detail_only: a value exists in detail but not at top level.",
            "walk_value_but_unavailable_flag: a walk value exists but all recorded availability flags are false.",
            "raw_transport_without_walk: raw access text exists, but no valid walk value was parsed.",
            "available_flag_without_walk: availability is true, but no valid walk value was found.",
            "no_walk_evidence: inspect the original listing page and search result data.",
            "These categories identify investigation targets; they do not prove the exact root cause.",
        ],
    }

    print("=== Station Walk Audit (READ ONLY) ===")
    for name, metrics in (
        ("discovered_listings.json", discovered_report),
        ("houses.json", houses_report),
    ):
        print(f"\n{name}")
        print(f"  total: {metrics['total']}")
        print(
            f"  valid walk: {metrics['validWalkCount']}/"
            f"{metrics['total']} ({metrics['validWalkPct']}%)"
        )
        print(
            f"  top-level walk: {metrics['topLevelWalkCount']}/"
            f"{metrics['total']} ({metrics['topLevelWalkPct']}%)"
        )
        print(
            f"  nested detail walk: {metrics['nestedWalkCount']}/"
            f"{metrics['total']} ({metrics['nestedWalkPct']}%)"
        )
        print(f"  categories: {metrics['categories']}")

    print("\nCross-file comparison")
    print(f"  shared IDs: {comparison['sharedPropertyIdCount']}")
    print(
        "  discovered has walk, houses missing: "
        f"{len(comparison['discoveredHasWalkButHousesMissing'])}"
    )
    print(
        "  houses has walk, discovered missing: "
        f"{len(comparison['housesHasWalkButDiscoveredMissing'])}"
    )

    print("\n=== JSON DETAILS ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
