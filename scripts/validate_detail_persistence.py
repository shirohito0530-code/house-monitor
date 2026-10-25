#!/usr/bin/env python3
"""Validate that detail facts in houses.json persist in discovered_listings.json.

Read-only validator. It does not modify either database.
Run from repository root or with: python scripts/validate_detail_persistence.py
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Canonical field -> aliases accepted in older database records.
FIELDS: dict[str, tuple[str, ...]] = {
    "address": ("address", "fullAddress", "propertyAddress"),
    "landAreaM2": ("landAreaM2", "landArea", "land"),
    "buildingAreaM2": ("buildingAreaM2", "buildingArea", "building"),
    # Footprint area is distinct from total floor area; never substitute one for the other.
    "buildingFootprintAreaM2": ("buildingFootprintAreaM2", "buildingFootprintArea"),
    "layout": ("layout", "layoutRaw", "floorPlan", "floor_plan"),
    "constructionYear": ("constructionYear", "yearBuilt", "builtYear"),
    "constructionMonth": ("constructionMonth", "builtMonth"),
    "constructionYearMonth": ("constructionYearMonth", "builtYearMonth"),
    "constructionAgeYears": ("constructionAgeYears", "builtAgeYears"),
    "propertyType": ("propertyType", "listingType", "type"),
    "targetStationWalkMinutes": (
        "targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes"
    ),
}

EMPTY_STRINGS = {"", "-", "－", "不明", "未定", "なし", "N/A", "n/a"}


def load_properties(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"required file missing: {path.relative_to(ROOT)}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("properties", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError(f"invalid properties list: {path.relative_to(ROOT)}")
    return [row for row in rows if isinstance(row, dict)]


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in EMPTY_STRINGS
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def first_value(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for key in aliases:
        value = row.get(key)
        if has_value(value):
            return value
    return None


def listing_id_from_url(row: dict[str, Any]) -> str | None:
    value = row.get("sourceUrl") or row.get("detailUrl") or row.get("url")
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    match = re.search(r"/(nc_\d+)(?:/|$|[?#])", normalized, re.I)
    return match.group(1).lower() if match else None


def keys(row: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for field in ("propertyId", "sourceId", "listingId"):
        value = row.get(field)
        if value is not None and str(value).strip():
            result.append((field, str(value).strip().lower()))
    url_id = listing_id_from_url(row)
    if url_id:
        result.append(("urlListingId", url_id))
    return list(dict.fromkeys(result))


def build_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        for key in keys(row):
            index.setdefault(key, []).append(row)
    return index


def find_unique_match(
    row: dict[str, Any],
    index: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    # A key mapping to multiple rows is ambiguous and is not used to claim a match.
    for key in keys(row):
        found = index.get(key, [])
        if len(found) == 1:
            return found[0]
    return None


def main() -> int:
    houses = load_properties(ROOT / "data" / "houses.json")
    discovered = load_properties(ROOT / "data" / "discovered_listings.json")

    if houses and not discovered:
        raise RuntimeError("houses.json has records but discovered_listings.json is empty")

    discovered_index = build_index(discovered)
    houses_index = build_index(houses)

    matched = 0
    unmatched_houses = 0
    ambiguous_houses = 0
    house_coverage = {field: 0 for field in FIELDS}
    discovered_coverage = {field: 0 for field in FIELDS}
    missing_among_comparable = {field: 0 for field in FIELDS}
    comparable = {field: 0 for field in FIELDS}
    ambiguous_match_keys = 0

    for row in houses:
        for field, aliases in FIELDS.items():
            if has_value(first_value(row, aliases)):
                house_coverage[field] += 1

        match = find_unique_match(row, discovered_index)
        if match is None:
            row_keys = keys(row)
            is_ambiguous = any(len(discovered_index.get(key, [])) > 1 for key in row_keys)
            if is_ambiguous:
                ambiguous_houses += 1
                ambiguous_match_keys += 1
            else:
                unmatched_houses += 1
            continue

        matched += 1
        for field, aliases in FIELDS.items():
            house_value = first_value(row, aliases)
            discovery_value = first_value(match, aliases)
            if has_value(discovery_value):
                discovered_coverage[field] += 1
            if has_value(house_value):
                comparable[field] += 1
                if not has_value(discovery_value):
                    missing_among_comparable[field] += 1

    print(
        "[DETAIL-PERSISTENCE-CHECK] "
        f"houses={len(houses)} discovered={len(discovered)} "
        f"matched={matched} unmatched={unmatched_houses} ambiguous={ambiguous_houses}"
    )
    print("Coverage below is field availability, not proof that SUUMO publishes the field.")
    failed = False

    for field in FIELDS:
        total_houses = len(houses)
        coverage_count = house_coverage[field]
        ratio = coverage_count / total_houses if total_houses else 0.0
        total_comparable = comparable[field]
        missing = missing_among_comparable[field]
        loss_ratio = missing / total_comparable if total_comparable else 0.0

        print(
            f"  {field}: houses={coverage_count}/{total_houses} ({ratio:.1%}); "
            f"discovered-present-in-matches={discovered_coverage[field]}; "
            f"missing-among-known={missing}/{total_comparable} ({loss_ratio:.1%})"
        )

        # Fail if known values in houses disappear from discovered rows.
        if total_comparable >= 10 and loss_ratio > 0.10:
            print(
                f"ERROR: {field} persistence loss exceeds 10% "
                f"({missing}/{total_comparable})",
                file=sys.stderr,
            )
            failed = True

    # Detect complete field disappearance only when the source houses DB has values.
    for field, aliases in FIELDS.items():
        source_has_any = any(has_value(first_value(row, aliases)) for row in houses)
        target_has_any = any(has_value(first_value(row, aliases)) for row in discovered)
        if source_has_any and not target_has_any:
            print(
                f"ERROR: {field} exists in houses.json but is absent from "
                "discovered_listings.json",
                file=sys.stderr,
            )
            failed = True

    # Duplicate keys are reported but do not fail the persistence check:
    # repeated/ambiguous keys need a separate identity audit, not destructive merging.
    def duplicate_key_count(index: dict[tuple[str, str], list[dict[str, Any]]]) -> int:
        return sum(1 for rows in index.values() if len(rows) > 1)

    print(
        f"[IDENTITY-KEY-AUDIT] duplicated_keys_houses={duplicate_key_count(houses_index)} "
        f"duplicated_keys_discovered={duplicate_key_count(discovered_index)} "
        f"ambiguous_match_keys={ambiguous_match_keys}"
    )

    if failed:
        return 1
    print("[DETAIL-PERSISTENCE-CHECK] passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"ERROR: detail persistence validation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
