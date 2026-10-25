#!/usr/bin/env python3
"""Validate that detail facts available in houses.json survive in discovered_listings.json."""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIELDS = {
    "landAreaM2": ("landAreaM2", "landArea", "land"),
    "buildingAreaM2": ("buildingAreaM2", "buildingArea", "building"),
    "layout": ("layout", "layoutRaw", "floorPlan"),
    "constructionMonth": ("constructionMonth", "constructionYearMonth"),
    "constructionYear": ("constructionYear", "yearBuilt", "builtYear"),
    "propertyType": ("propertyType", "listingType", "type"),
}


def load_properties(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"required file missing: {path.relative_to(ROOT)}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("properties", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError(f"invalid properties list: {path.relative_to(ROOT)}")
    return [x for x in rows if isinstance(x, dict)]


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in {"", "-", "－", "不明", "未定"}
    return True


def first_value(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for key in aliases:
        if has_value(row.get(key)):
            return row[key]
    return None


def url_id(row: dict[str, Any]) -> str | None:
    value = row.get("sourceUrl") or row.get("url")
    match = re.search(r"/(nc_\d+)(?:/|$)", str(value or ""), re.I)
    return match.group(1).lower() if match else None


def keys(row: dict[str, Any]) -> list[tuple[str, str]]:
    result = []
    for field in ("propertyId", "sourceId", "listingId"):
        if row.get(field):
            result.append((field, str(row[field]).strip().lower()))
    if url_id(row):
        result.append(("urlListingId", url_id(row) or ""))
    return result


def main() -> int:
    houses = load_properties(ROOT / "data/houses.json")
    discovered = load_properties(ROOT / "data/discovered_listings.json")
    if houses and not discovered:
        raise RuntimeError("houses.json has records but discovered_listings.json is empty")

    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in discovered:
        for key in set(keys(row)):
            index.setdefault(key, []).append(row)

    houses_coverage = {field: 0 for field in FIELDS}
    discovered_coverage = {field: 0 for field in FIELDS}
    matched_records = 0
    missing_by_field = {field: 0 for field in FIELDS}
    comparable_by_field = {field: 0 for field in FIELDS}

    for row in houses:
        for field, aliases in FIELDS.items():
            if has_value(first_value(row, aliases)):
                houses_coverage[field] += 1
        matches = []
        for key in keys(row):
            found = index.get(key, [])
            if len(found) == 1:
                matches = found
                break
        if not matches:
            continue
        matched_records += 1
        target = matches[0]
        for field, aliases in FIELDS.items():
            house_value = first_value(row, aliases)
            discovery_value = first_value(target, aliases)
            if has_value(discovery_value):
                discovered_coverage[field] += 1
            if has_value(house_value):
                comparable_by_field[field] += 1
                if not has_value(discovery_value):
                    missing_by_field[field] += 1

    print(f"[DETAIL-PERSISTENCE-CHECK] houses={len(houses)} discovered={len(discovered)} matched={matched_records}")
    failed = False
    for field in FIELDS:
        print(
            f"  {field}: houses={houses_coverage[field]}/{len(houses)}; "
            f"discovered-covered-in-matched={discovered_coverage[field]}; "
            f"missing-among-comparable={missing_by_field[field]}/{comparable_by_field[field]}"
        )
        if houses_coverage[field] > 0 and discovered and not any(
            has_value(first_value(row, FIELDS[field])) for row in discovered
        ):
            print(f"ERROR: {field} exists in houses.json but is completely absent from discovered_listings.json", file=sys.stderr)
            failed = True
        total = comparable_by_field[field]
        missing = missing_by_field[field]
        # For a sizeable matched set, fail if more than 10% of known detail facts disappear.
        if total >= 10 and missing / total > 0.10:
            print(f"ERROR: {field} persistence loss exceeds 10% ({missing}/{total})", file=sys.stderr)
            failed = True

    if failed:
        return 1
    print("[DETAIL-PERSISTENCE-CHECK] passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: detail persistence validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
