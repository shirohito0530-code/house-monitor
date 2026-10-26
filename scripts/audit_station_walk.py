
#!/usr/bin/env python3
"""Read-only audit of station-walk extraction and persistence.

Run from repository root:
    python scripts/audit_station_walk.py

The script does not modify database files.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

WALK_FIELDS = (
    "targetStationWalkMinutes",
    "stationWalkMinutes",
    "walkMinutes",
)

RAW_FIELDS = (
    "transportRaw",
    "stationAccessRaw",
    "accessRaw",
    "access",
    "transport",
)

WALK_SOURCE_FIELDS = (
    "targetStationWalkSource",
    "stationWalkSource",
    "walkSource",
)

AVAILABILITY_FIELDS = (
    "targetStationWalkAvailable",
    "stationWalkAvailable",
    "walkAvailable",
)

DETAIL_CONTAINERS = (
    "detail",
    "lastSuccessfulDetail",
    "detailContent",
    "detailData",
    "detailResult",
)

EMPTY_VALUES = {
    "", "-", "－", "不明", "未定", "なし", "N/A", "n/a",
}

SUUMO_ID_RE = re.compile(
    r"/(nc_\d+)(?:/|$|[?#])",
    re.IGNORECASE,
)


def load_rows(filename: str) -> list[dict[str, Any]]:
    path = DATA / filename
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("properties", [])
    else:
        raise ValueError(f"{filename}: unsupported JSON structure")

    if not isinstance(rows, list):
        raise ValueError(f"{filename}: properties must be a list")

    return [row for row in rows if isinstance(row, dict)]


def usable(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in EMPTY_VALUES
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


def parse_walk(value: Any) -> float | None:
    """Parse a plausible walking time; reject booleans and invalid values."""
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = unicodedata.normalize("NFKC", str(value))
        text = text.replace(",", "")
        match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)", text)
        if not match:
            return None
        number = float(match.group(1))

    if not 1 <= number <= 120:
        return None

    return number


def normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip().lower()


def suumo_listing_id(value: Any) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", str(value))
    match = SUUMO_ID_RE.search(normalized)
    return match.group(1).lower() if match else None


def identity_keys(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Return stable identity keys; URL listing ID has priority."""
    result: list[tuple[str, str]] = []

    # A SUUMO URL ID is usually stable across the two JSON databases.
    for field in ("sourceUrl", "detailUrl", "url", "canonicalUrl"):
        listing_id = suumo_listing_id(row.get(field))
        if listing_id:
            result.append(("suumo_listing_id", listing_id))

    source = normalized_text(row.get("source") or "suumo")

    for field in ("sourceId", "listingId"):
        value = normalized_text(row.get(field))
        if value:
            result.append((f"{source}:{field}", value))

    property_id = normalized_text(row.get("propertyId") or row.get("id"))
    if property_id:
        # Keep the raw property ID as a secondary key, not the sole key.
        result.append(("property_id", property_id))

    # Preserve ordering and remove duplicate keys.
    return list(dict.fromkeys(result))


def primary_id(row: dict[str, Any]) -> str:
    keys = identity_keys(row)
    if keys:
        return f"{keys[0][0]}={keys[0][1]}"
    return "(unknown)"


def detail_objects(row: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    for name in DETAIL_CONTAINERS:
        value = row.get(name)
        if isinstance(value, dict):
            result.append((name, value))
    return result


def walk_values(
    row: dict[str, Any],
) -> list[tuple[str, Any, float]]:
    """Find all valid walk values at top level and in recognized detail objects."""
    objects = [("record", row), *detail_objects(row)]
    result: list[tuple[str, Any, float]] = []

    for object_name, obj in objects:
        for field in WALK_FIELDS:
            raw = obj.get(field)
            parsed = parse_walk(raw)
            if parsed is not None:
                result.append((f"{object_name}.{field}", raw, parsed))

    return result


def first_usable(
    row: dict[str, Any],
    fields: tuple[str, ...],
) -> tuple[str | None, Any]:
    objects = [("record", row), *detail_objects(row)]

    for object_name, obj in objects:
        for field in fields:
            value = obj.get(field)
            if usable(value):
                return f"{object_name}.{field}", value

    return None, None


def inspect_row(row: dict[str, Any]) -> dict[str, Any]:
    walks = walk_values(row)
    walk_path, raw_walk, walk = walks[0] if walks else (None, None, None)

    raw_path, raw_transport = first_usable(row, RAW_FIELDS)
    source_path, walk_source = first_usable(row, WALK_SOURCE_FIELDS)

    availability_values = []
    for object_name, obj in [
        ("record", row),
        *detail_objects(row),
    ]:
        for field in AVAILABILITY_FIELDS:
            if field in obj:
                availability_values.append({
                    "path": f"{object_name}.{field}",
                    "value": obj.get(field),
                })

    availability_true = any(
        item["value"] is True for item in availability_values
    )
    availability_false = (
        bool(availability_values)
        and all(item["value"] is False for item in availability_values)
    )

    station_path, station = first_usable(
        row, ("targetStation", "station")
    )

    if walks:
        if availability_false:
            category = "walk_value_but_unavailable_flag"
        elif not availability_values:
            category = "walk_value_without_availability_flag"
        else:
            category = "walk_value_found"
    elif usable(raw_transport):
        category = "raw_transport_without_walk"
    elif availability_true:
        category = "available_flag_without_walk"
    else:
        category = "no_walk_evidence"

    return {
        "id": primary_id(row),
        "identityKeys": identity_keys(row),
        "walkMinutes": walk,
        "walkPath": walk_path,
        "rawWalkValue": raw_walk,
        "allWalkValues": [
            {"path": path, "raw": raw, "minutes": minutes}
            for path, raw, minutes in walks
        ],
        "rawTransportPath": raw_path,
        "transportRaw": raw_transport,
        "walkSourcePath": source_path,
        "targetStationWalkSource": walk_source,
        "stationPath": station_path,
        "targetStation": station,
        "availabilityValues": availability_values,
        "category": category,
        "propertyId": row.get("propertyId"),
        "sourceId": row.get("sourceId"),
        "listingId": row.get("listingId"),
        "sourceUrl": row.get("sourceUrl") or row.get("detailUrl") or row.get("url"),
        "detailFetched": row.get("detailFetched"),
        "detailFetchStatus": row.get("detailFetchStatus"),
        "detailDataVersion": row.get("detailDataVersion"),
    }


def build_identity_index(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[int]]:
    index: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        for key in identity_keys(row):
            index[key].append(i)
    return index


def find_matches(
    row: dict[str, Any],
    index: dict[tuple[str, str], list[int]],
) -> tuple[list[int], str]:
    """Only accept a unique matching identity key; report ambiguous matches."""
    for key in identity_keys(row):
        matches = index.get(key, [])
        if len(matches) == 1:
            return matches, "unique"
        if len(matches) > 1:
            return matches, "ambiguous"
    return [], "unmatched"


def compare_files(
    discovered: list[dict[str, Any]],
    houses: list[dict[str, Any]],
) -> dict[str, Any]:
    discovered_index = build_identity_index(discovered)
    houses_index = build_identity_index(houses)

    matched_pairs = []
    unmatched_houses = []
    ambiguous_houses = []

    for house_i, house in enumerate(houses):
        matches, status = find_matches(house, discovered_index)
        if status == "unique":
            matched_pairs.append((house_i, matches[0]))
        elif status == "ambiguous":
            ambiguous_houses.append(primary_id(house))
        else:
            unmatched_houses.append(primary_id(house))

    discovered_has_walk_houses_missing = []
    houses_has_walk_discovered_missing = []
    matched_but_walk_conflict = []

    for house_i, discovered_i in matched_pairs:
        house_row = houses[house_i]
        discovered_row = discovered[discovered_i]
        house_audit = inspect_row(house_row)
        discovered_audit = inspect_row(discovered_row)

        hw = house_audit["walkMinutes"]
        dw = discovered_audit["walkMinutes"]

        if dw is not None and hw is None:
            discovered_has_walk_houses_missing.append({
                "id": primary_id(house_row),
                "discoveredWalk": dw,
                "discoveredPath": discovered_audit["walkPath"],
                "houseDetailFetched": house_row.get("detailFetched"),
                "houseStatus": house_row.get("status"),
            })
        elif hw is not None and dw is None:
            houses_has_walk_discovered_missing.append({
                "id": primary_id(house_row),
                "housesWalk": hw,
                "housesPath": house_audit["walkPath"],
            })
        elif hw is not None and dw is not None and hw != dw:
            matched_but_walk_conflict.append({
                "id": primary_id(house_row),
                "housesWalk": hw,
                "discoveredWalk": dw,
            })

    return {
        "matchedUniquePairCount": len(matched_pairs),
        "unmatchedHouseCount": len(unmatched_houses),
        "ambiguousHouseCount": len(ambiguous_houses),
        "unmatchedHouseExamples": unmatched_houses[:20],
        "ambiguousHouseExamples": ambiguous_houses[:20],
        "discoveredHasWalkButHousesMissing": discovered_has_walk_houses_missing,
        "housesHasWalkButDiscoveredMissing": houses_has_walk_discovered_missing,
        "walkValueConflicts": matched_but_walk_conflict,
        "discoveredIdentityKeyCount": len(discovered_index),
        "housesIdentityKeyCount": len(houses_index),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    audits = [inspect_row(row) for row in rows]
    counts = Counter(item["category"] for item in audits)
    total = len(rows)
    valid = sum(item["walkMinutes"] is not None for item in audits)

    examples: dict[str, list[dict[str, Any]]] = {}
    for category in (
        "walk_value_but_unavailable_flag",
        "walk_value_without_availability_flag",
        "raw_transport_without_walk",
        "available_flag_without_walk",
        "no_walk_evidence",
    ):
        examples[category] = [
            item for item in audits if item["category"] == category
        ][:15]

    return {
        "total": total,
        "validWalkCount": valid,
        "validWalkPct": round(100 * valid / total, 2) if total else 0.0,
        "categoryCounts": dict(counts),
        "examples": examples,
    }


def main() -> int:
    discovered = load_rows("discovered_listings.json")
    houses = load_rows("houses.json")

    discovered_report = summarize(discovered)
    houses_report = summarize(houses)
    comparison = compare_files(discovered, houses)

    report = {
        "readOnly": True,
        "discovered": discovered_report,
        "houses": houses_report,
        "crossFileComparison": comparison,
        "interpretation": [
            "This audit is diagnostic and does not modify any database.",
            "A missing walk value does not prove that the source page lacks access information.",
            "Ambiguous identity matches are not treated as successful matches.",
            "Building footprint area is not interchangeable with total floor area.",
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
        print(f"  categories: {metrics['categoryCounts']}")

    print("\n=== Cross-file comparison ===")
    print(json.dumps(comparison, ensure_ascii=False, indent=2))

    print("\n=== Representative examples ===")
    print(json.dumps({
        "discovered": discovered_report["examples"],
        "houses": houses_report["examples"],
    }, ensure_ascii=False, indent=2))

    print("\n=== Full JSON report ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
