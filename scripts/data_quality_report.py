#!/usr/bin/env python3
"""Create a non-fatal, auditable data-quality report for house-monitor."""
from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORT_PATH = DATA / "data_quality_report.json"
SUMMARY_PATH = DATA / "summary.json"


def load(name: str, default: Any) -> Any:
    path = DATA / name
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def rows(obj: Any, key: str = "properties") -> list[dict[str, Any]]:
    if isinstance(obj, list):
        source = obj
    elif isinstance(obj, dict):
        source = obj.get(key, [])
    else:
        source = []
    return [x for x in source if isinstance(x, dict)] if isinstance(source, list) else []


def has_value(item: dict[str, Any], *keys: str) -> bool:
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    for key in keys:
        value = item.get(key)
        if value in (None, "", "-", "不明", 0, "0"):
            value = detail.get(key)
        if value not in (None, "", "-", "不明", 0, "0"):
            return True
    return False


def pct(n: int, d: int) -> float:
    return round(n / d * 100, 1) if d else 0.0


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    houses_data = load("houses.json", {})
    discovered_data = load("discovered_listings.json", {})
    observations_data = load("listing_observations.json", {})
    transactions_data = load("transactions.json", {})
    houses = rows(houses_data)
    discovered = rows(discovered_data)
    observations = rows(observations_data, "observations")
    canonical = [
        x for x in houses
        if x.get("areaDecision") == "accepted"
        and (x.get("canonicalPropertyId") or x.get("propertyId") or x.get("id"))
        == (x.get("propertyId") or x.get("id"))
    ]

    fields = {
        "price": ("currentPrice", "priceYen", "price", "priceMan"),
        "landArea": ("landAreaM2", "land", "landArea"),
        "buildingArea": ("buildingAreaM2", "building", "buildingArea"),
        "stationWalk": ("targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes"),
        "constructionYear": ("constructionYear", "builtYear", "constructionMonth"),
        "schoolDistrictConfirmed": ("schoolDistrictStatus",),
    }
    field_coverage = {}
    for label, keys in fields.items():
        if label == "schoolDistrictConfirmed":
            count = sum(1 for x in canonical if x.get("schoolDistrictStatus") == "confirmed")
        else:
            count = sum(1 for x in canonical if has_value(x, *keys))
        field_coverage[label] = {
            "count": count,
            "total": len(canonical),
            "coveragePct": pct(count, len(canonical)),
        }

    period_reports = []
    periods = transactions_data.get("periods", []) if isinstance(transactions_data, dict) else []
    for period in periods if isinstance(periods, list) else []:
        if not isinstance(period, dict):
            continue
        city_reports = {}
        cities = period.get("cities", {})
        for city_key, city in cities.items() if isinstance(cities, dict) else []:
            if not isinstance(city, dict):
                continue
            city_reports[city_key] = {
                "name": city.get("name"),
                "count": len(city.get("data", [])) if isinstance(city.get("data"), list) else 0,
                "retrievedAt": city.get("retrievedAt"),
                "status": "stored_data" if isinstance(city.get("data"), list) and city.get("data") else
                          "stored_zero_records" if isinstance(city.get("data"), list) else "malformed",
            }
        period_reports.append({
            "period": period.get("period"),
            "cities": city_reports,
        })

    retrieval_status = transactions_data.get("retrievalStatus", {}) if isinstance(transactions_data, dict) else {}
    retrieval_cities = retrieval_status.get("cities", {}) if isinstance(retrieval_status, dict) else {}
    retrieval_summary = {}
    for key, status in retrieval_cities.items() if isinstance(retrieval_cities, dict) else []:
        if isinstance(status, dict):
            retrieval_summary[key] = {
                "name": status.get("name"),
                "status": status.get("status", "unknown"),
                "selectedPeriod": status.get("selectedPeriod"),
                "recordsSaved": status.get("recordsSaved", 0),
                "errors": status.get("errors", []),
                "periodsChecked": status.get("periodsChecked", []),
            }

    observation_dates = []
    for obs in observations:
        raw = obs.get("observedAt") or obs.get("timestamp")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            observation_dates.append(dt.astimezone(timezone.utc))
        except ValueError:
            pass
    observation_span = (
        max(observation_dates) - min(observation_dates)
    ).days if observation_dates else 0

    status_counts = Counter(str(x.get("status") or "unknown") for x in discovered)
    report = {
        "schemaVersion": "1.0",
        "generatedAt": now,
        "sourceFiles": {
            "housesCount": len(houses),
            "discoveredCount": len(discovered),
            "observationCount": len(observations),
            "transactionPeriodCount": len(periods) if isinstance(periods, list) else 0,
        },
        "listingStatusCounts": dict(status_counts),
        "canonicalAcceptedHouseCount": len(canonical),
        "fieldCoverage": field_coverage,
        "observationHistory": {
            "validTimestampCount": len(observation_dates),
            "spanDays": observation_span,
            "oldestObservedAt": min(observation_dates).isoformat() if observation_dates else None,
            "newestObservedAt": max(observation_dates).isoformat() if observation_dates else None,
        },
        "transactionRetrieval": {
            "updatedAt": transactions_data.get("updatedAt") if isinstance(transactions_data, dict) else None,
            "source": transactions_data.get("source") if isinstance(transactions_data, dict) else None,
            "cities": retrieval_summary,
            "storedPeriods": period_reports,
            "interpretation": "取得失敗・未取得・データなし・保存済みデータありを区別。取引件数0を市場で取引がなかった証拠として扱わない。",
        },
        "warnings": [],
    }

    if not transactions_data:
        report["warnings"].append("transactions.json が存在しないか空です")
    if not retrieval_summary:
        report["warnings"].append("API取得ステータスが未記録です。保存済み取引データの有無と取得成功を区別できません")
    if observation_span < 30:
        report["warnings"].append(f"掲載観測履歴が{observation_span}日しかなく、3か月・12か月の価格トレンドは限定的です")
    if len(canonical) and field_coverage["stationWalk"]["coveragePct"] < 50:
        report["warnings"].append("駅徒歩分数の欠損が多く、立地評価の信頼度に影響します")
    if len(canonical) and field_coverage["landArea"]["coveragePct"] < 70:
        report["warnings"].append("土地面積の欠損が多く、土地価値推定の対象が限られます")
    if len(canonical) and field_coverage["buildingArea"]["coveragePct"] < 70:
        report["warnings"].append("建物面積の欠損が多く、建物価値推定の対象が限られます")

    tmp = REPORT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(REPORT_PATH)

    summary = load("summary.json", {})
    if isinstance(summary, dict):
        summary["dataQuality"] = {
            "reportPath": "data/data_quality_report.json",
            "generatedAt": now,
            "canonicalAcceptedHouseCount": len(canonical),
            "fieldCoverage": field_coverage,
            "observationSpanDays": observation_span,
            "transactionRetrieval": {
                key: {"status": value.get("status"), "recordsSaved": value.get("recordsSaved", 0)}
                for key, value in retrieval_summary.items()
            },
            "warnings": report["warnings"],
        }
        tmp_summary = SUMMARY_PATH.with_suffix(".json.tmp")
        tmp_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_summary.replace(SUMMARY_PATH)

    print(json.dumps({
        "report": str(REPORT_PATH.relative_to(ROOT)),
        "canonicalAcceptedHouseCount": len(canonical),
        "fieldCoverage": field_coverage,
        "transactionRetrieval": {k: v.get("status") for k, v in retrieval_summary.items()},
        "warnings": report["warnings"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
