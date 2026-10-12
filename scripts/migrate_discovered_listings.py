#!/usr/bin/env python3
"""One-time, conservative migration for existing house-monitor JSON data.

- Deduplicates discovered records by propertyId only.
- Preserves separate listing IDs even when they may represent the same physical house.
- Initializes lifecycle fields without inferring confirmed-ended status from old data.
- Does not delete data; creates timestamped backups before writing.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def backup(path: Path):
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = path.with_name(path.name + f".{stamp}.bak")
        shutil.copy2(path, target)
        print(f"Backup: {target}")


def merge(existing: dict, incoming: dict) -> dict:
    out = dict(existing)
    latest_fields = {
        "status", "lastSeenAt", "priceYen", "priceMan", "currentPrice",
        "currentPriceMan", "searchPriceYen", "searchPriceMan", "sourceUrl",
    }
    for key, value in incoming.items():
        if value is None or value == "":
            continue
        if key in latest_fields:
            if str(incoming.get("lastSeenAt") or "") >= str(out.get("lastSeenAt") or ""):
                out[key] = value
        elif out.get(key) in (None, "", [], {}):
            out[key] = value
    return out


def main():
    path = DATA / "discovered_listings.json"
    if not path.exists():
        print("No discovered_listings.json found; nothing to migrate.")
        return

    raw = load(path, {})
    rows = raw.get("properties", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise SystemExit("ERROR: properties must be a list")

    records = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("propertyId") or row.get("id")
        if not pid:
            print("WARNING: skipped record without propertyId")
            continue
        pid = str(pid)
        row = dict(row)
        row["propertyId"] = pid
        row.setdefault("missedSearchRuns", 0)
        row.setdefault("isListingEnded", False)
        row.setdefault("duplicateCandidate", False)
        row.setdefault("duplicateCandidateOf", None)
        old_status = row.get("status")
        if old_status == "ended":
            row["status"] = "observed_ended"
        elif old_status not in {
            "active", "possibly_ended", "observed_ended", "confirmed_ended"
        }:
            row["status"] = "active"
        if row.get("status") == "confirmed_ended" and row.get("isListingEnded") is not True:
            # Legacy labels alone are not evidence of explicit end status.
            row["status"] = "observed_ended"
        records[pid] = merge(records[pid], row) if pid in records else row

    backup(path)
    result = {
        "version": "3.0",
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "properties": list(records.values()),
        "migration": {
            "deduplicatedBy": "propertyId",
            "physicalDuplicatesAutoMerged": False,
        },
    }
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Migrated {len(rows)} records to {len(records)} unique listing IDs.")


if __name__ == "__main__":
    main()
