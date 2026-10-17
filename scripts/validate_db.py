#!/usr/bin/env python3
"""Fail-closed validation for house-monitor JSON databases."""
from __future__ import annotations
import json, sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REQUIRED = ("discovered_listings.json", "houses.json")
VALID_STATUSES = {"active", "possibly_ended", "observed_ended", "confirmed_ended"}
MAX_DB_BYTES = 80 * 1024 * 1024
MAX_OBSERVATION_BYTES = 100 * 1024 * 1024

def load(path: Path) -> Any:
    if not path.exists():
        raise ValueError(f"required file missing: {path.relative_to(ROOT)}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)

def validate_properties(data: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("properties"), list):
        raise ValueError(f"{label}: expected object with properties list")
    if not data.get("updatedAt"):
        raise ValueError(f"{label}: updatedAt missing")
    seen, records = set(), []
    for i, item in enumerate(data["properties"]):
        if not isinstance(item, dict):
            raise ValueError(f"{label}: properties[{i}] is not an object")
        pid = str(item.get("propertyId") or item.get("id") or "").strip()
        if not pid:
            raise ValueError(f"{label}: properties[{i}] has no propertyId/id")
        if pid in seen:
            raise ValueError(f"{label}: duplicate ID {pid!r}")
        seen.add(pid)
        if (item.get("source") or item.get("sourceId")) and not (item.get("sourceUrl") or item.get("url")):
            raise ValueError(f"{label}: {pid} has source identity but no URL")
        status = item.get("status")
        if status not in (None, "") and status not in VALID_STATUSES:
            raise ValueError(f"{label}: {pid} invalid status {status!r}")
        for field in ("landAreaM2", "buildingAreaM2", "buildingFootprintAreaM2"):
            value = item.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                raise ValueError(f"{label}: {pid} invalid {field}={value!r}")
        records.append(item)
    return records

def validate_observations() -> int:
    path = DATA / "listing_observations.json"
    if not path.exists():
        return 0
    if path.stat().st_size > MAX_OBSERVATION_BYTES:
        raise ValueError("listing_observations.json exceeds size limit")
    data = load(path)
    records = data.get("observations", []) if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError("observations must be a list")
    seen = set()
    for i, item in enumerate(records):
        if not isinstance(item, dict) or not (item.get("propertyId") or item.get("id")):
            raise ValueError(f"observations[{i}] lacks an object/property ID")
        fp = json.dumps({k: item.get(k) for k in ("propertyId", "id", "observedAt", "timestamp", "status", "currentPrice", "priceYen", "priceMan", "sourceUrl")}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if fp in seen:
            raise ValueError(f"duplicate observation fingerprint at index {i}")
        seen.add(fp)
    return len(records)

def main() -> int:
    for filename in REQUIRED:
        path = DATA / filename
        if path.stat().st_size > MAX_DB_BYTES:
            raise ValueError(f"{filename} exceeds {MAX_DB_BYTES} bytes")
        records = validate_properties(load(path), filename)
        statuses = {}
        for item in records:
            status = item.get("status") or "active"
            statuses[status] = statuses.get(status, 0) + 1
        print(f"OK {filename}: {len(records)} records; statuses={statuses}; bytes={path.stat().st_size:,}")
    print(f"OK listing_observations.json: {validate_observations()} observations")
    print("DB validation passed.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[DB-VALIDATION-ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
