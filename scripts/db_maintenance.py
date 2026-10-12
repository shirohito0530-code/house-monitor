#!/usr/bin/env python3
"""Conservative post-run compaction and validation for house-monitor JSON DBs.

- Deduplicates discovered listings by canonical propertyId.
- Deduplicates observations by stable content fingerprint.
- Applies observation retention (365 days / 20,000 records).
- Does NOT auto-merge houses by fuzzy physical identity; main.py owns that decision.
- Keeps malformed-date observations rather than silently deleting them, but reports them.
- Uses atomic file replacement and refuses dangerous record-count drops.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DISCOVERED_PATH = ROOT / "data" / "discovered_listings.json"
HOUSES_PATH = ROOT / "data" / "houses.json"
OBSERVATIONS_PATH = ROOT / "data" / "listing_observations.json"
DISCOVERY_RETENTION_DAYS = 180
OBSERVATION_RETENTION_DAYS = 365
MAX_OBSERVATIONS = 20_000
MAX_DROP_RATIO = float(os.getenv("DB_MAINTENANCE_MAX_DROP_RATIO", "0.30"))


def parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def property_id(item: dict[str, Any]) -> str | None:
    value = item.get("propertyId") or item.get("id")
    return str(value).strip() if value is not None and str(value).strip() else None


def load_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("properties"), list):
        raise ValueError(f"{path.relative_to(ROOT)} must be an object with a properties list")
    return data


def atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def merge_listing(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Prefer the most recently seen record for volatile fields; preserve useful non-empty values."""
    old_seen = parse_time(old.get("lastSeenAt")) or datetime.min.replace(tzinfo=timezone.utc)
    new_seen = parse_time(new.get("lastSeenAt")) or datetime.min.replace(tzinfo=timezone.utc)
    newer, older = (new, old) if new_seen >= old_seen else (old, new)
    merged = dict(older)
    merged.update({k: v for k, v in newer.items() if v not in (None, "", [], {})})
    for key, value in older.items():
        if merged.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            merged[key] = value
    # A propertyId is an identity key, not a field to infer or rewrite.
    merged["propertyId"] = property_id(newer) or property_id(older)
    return merged


def compact_discovered(data: dict[str, Any], now: datetime) -> tuple[dict[str, Any], int, int]:
    original = data["properties"]
    by_id: dict[str, dict[str, Any]] = {}
    invalid_id = 0
    expired = 0
    cutoff = now - timedelta(days=DISCOVERY_RETENTION_DAYS)
    for raw in original:
        if not isinstance(raw, dict):
            invalid_id += 1
            continue
        pid = property_id(raw)
        if not pid:
            invalid_id += 1
            continue
        last_seen = parse_time(raw.get("lastSeenAt") or raw.get("firstSeenAt"))
        # Unknown timestamps are retained for manual review; don't delete silently.
        if last_seen is not None and last_seen < cutoff:
            expired += 1
            continue
        record = dict(raw)
        record["propertyId"] = pid
        by_id[pid] = merge_listing(by_id[pid], record) if pid in by_id else record
    result = dict(data)
    result["properties"] = sorted(
        by_id.values(),
        key=lambda item: (str(item.get("status") or ""), str(item.get("lastSeenAt") or ""), str(item.get("propertyId") or "")),
        reverse=True,
    )
    result["updatedAt"] = now.isoformat()
    result.setdefault("summary", {})
    if isinstance(result["summary"], dict):
        result["summary"]["maintenance"] = {
            "deduplicatedAt": now.isoformat(),
            "inputCount": len(original),
            "outputCount": len(result["properties"]),
            "duplicatesMerged": max(0, len(original) - invalid_id - expired - len(by_id)),
            "expiredRemoved": expired,
            "invalidIdSkipped": invalid_id,
        }
    return result, len(original), len(result["properties"])


def observation_fingerprint(item: dict[str, Any]) -> str:
    # Use identity + time + tracked values. Do not rely on generated IDs that may differ across runs.
    fields = (
        "propertyId", "observedAt", "timestamp", "status", "currentPrice", "currentPriceMan",
        "priceYen", "priceMan", "priceConfidence", "priceStatus", "priceSource", "sourceUrl",
    )
    payload = {key: item.get(key) for key in fields if key in item}
    # Legacy observation schemas may use `id` instead of `propertyId`.
    payload["_canonicalPropertyId"] = property_id(item)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compact_observations(path: Path, now: datetime) -> tuple[int, int, int]:
    if not path.exists():
        print(f"[INFO] observations file absent; skipping: {path.relative_to(ROOT)}")
        return 0, 0, 0
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if isinstance(data, dict):
        observations = data.get("observations", [])
    elif isinstance(data, list):
        observations = data
    else:
        raise ValueError("listing_observations.json must be a list or an object with observations")
    if not isinstance(observations, list):
        raise ValueError("observations must be a list")

    cutoff = now - timedelta(days=OBSERVATION_RETENTION_DAYS)
    seen: set[str] = set()
    compacted: list[dict[str, Any]] = []
    malformed_dates = 0
    expired = 0
    for item in observations:
        if not isinstance(item, dict) or not property_id(item):
            continue
        observed = parse_time(item.get("observedAt") or item.get("timestamp") or item.get("createdAt"))
        if observed is None:
            malformed_dates += 1
        elif observed < cutoff:
            expired += 1
            continue
        fingerprint = observation_fingerprint(item)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        compacted.append(item)

    # Keep newest first for cap selection, then restore chronological order.
    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        dt = parse_time(item.get("observedAt") or item.get("timestamp") or item.get("createdAt"))
        return (1 if dt else 0, dt.isoformat() if dt else str(item.get("observedAt") or ""))
    compacted.sort(key=sort_key, reverse=True)
    dropped_by_cap = max(0, len(compacted) - MAX_OBSERVATIONS)
    compacted = compacted[:MAX_OBSERVATIONS]
    compacted.sort(key=sort_key)

    if isinstance(data, dict):
        output = dict(data)
        output["updatedAt"] = now.isoformat()
        output["observations"] = compacted
        output["maintenance"] = {
            "deduplicatedAt": now.isoformat(),
            "inputCount": len(observations),
            "outputCount": len(compacted),
            "expiredRemoved": expired,
            "malformedDateRetained": malformed_dates,
            "droppedByCap": dropped_by_cap,
        }
    else:
        output = compacted
    atomic_write(path, output)
    return len(observations), len(compacted), malformed_dates


def check_unique_property_ids(data: dict[str, Any], label: str) -> None:
    seen: set[str] = set()
    for index, item in enumerate(data["properties"]):
        if not isinstance(item, dict):
            raise ValueError(f"{label}: properties[{index}] is not an object")
        pid = property_id(item)
        if not pid:
            raise ValueError(f"{label}: properties[{index}] has no propertyId/id")
        if pid in seen:
            raise ValueError(f"{label}: duplicate propertyId after compaction: {pid}")
        seen.add(pid)


def main() -> int:
    now = datetime.now(timezone.utc)
    if not DISCOVERED_PATH.exists() or not HOUSES_PATH.exists():
        raise FileNotFoundError("Required DB missing; refusing to create empty DB")

    discovered_before = load_object(DISCOVERED_PATH)
    houses = load_object(HOUSES_PATH)
    discovered_after, before_count, after_count = compact_discovered(discovered_before, now)

    # Fail closed if compaction would remove too much. Set DB_MAINTENANCE_MAX_DROP_RATIO only
    # for a deliberate one-time cleanup after inspecting the report.
    drop_ratio = (before_count - after_count) / max(1, before_count)
    if drop_ratio > MAX_DROP_RATIO:
        raise RuntimeError(
            f"discovered DB would shrink by {drop_ratio:.1%} ({before_count}->{after_count}); "
            "refusing to write. Inspect duplicates/retention and explicitly adjust threshold if intended."
        )

    check_unique_property_ids(discovered_after, "discovered_listings.json")
    check_unique_property_ids(houses, "houses.json")

    # Write discovered only after all required DB validation has passed.
    atomic_write(DISCOVERED_PATH, discovered_after)
    obs_before, obs_after, malformed = compact_observations(OBSERVATIONS_PATH, now)
    print("=== DB maintenance report ===")
    print(f"discovered_listings.json: {before_count} -> {after_count}; merged/expired/invalid details in summary")
    print(f"houses.json: {len(houses['properties'])} properties; physical identity merging left to main.py")
    print(f"listing_observations.json: {obs_before} -> {obs_after}; malformed timestamps retained={malformed}")
    print(f"filesize: discovered={DISCOVERED_PATH.stat().st_size:,} bytes; houses={HOUSES_PATH.stat().st_size:,} bytes")
    if OBSERVATIONS_PATH.exists():
        print(f"filesize: observations={OBSERVATIONS_PATH.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[DB-MAINTENANCE-ERROR] {exc}", file=sys.stderr)
        raise
