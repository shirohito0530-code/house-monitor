#!/usr/bin/env python3
"""Area quarantine, conservative duplicate grouping, and explainable market indicators.

The pipeline is idempotent and non-destructive by default. Out-of-area records are
quarantined first; only old quarantined records without user decisions are eligible
for pruning from the discovery/master indexes. Observation history is retained under
its own retention policy.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CONFIG = ROOT / "config"
HOUSES_PATH = DATA / "houses.json"
DISCOVERED_PATH = DATA / "discovered_listings.json"
OBSERVATIONS_PATH = DATA / "listing_observations.json"
TRANSACTIONS_PATH = DATA / "transactions.json"
SUMMARY_PATH = DATA / "summary.json"
USER_SELECTIONS_PATH = DATA / "user_selections.json"
SCHOOL_CONFIG_PATH = CONFIG / "school_districts.json"
QUARANTINE_RETENTION_DAYS = 90
VERSION = "2026-10-19-v1"

TARGET_CITY = {
    "柏市": "柏の葉キャンパス",
    "流山市": "流山おおたかの森",
}
CITY_CODES = {
    "sc_kashiwa": ("柏市", "柏の葉キャンパス"),
    "sc_nagareyama": ("流山市", "流山おおたかの森"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def atomic_save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp.replace(path)


def records_from(data: Any, key: str = "properties") -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [x for x in data[key] if isinstance(x, dict)]
    return []


def text_value(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = item.get(key)
        if val is not None and str(val).strip() and str(val).strip() != "-":
            return str(val).strip()
    detail = item.get("detail")
    if isinstance(detail, dict):
        for key in keys:
            val = detail.get(key)
            if val is not None and str(val).strip() and str(val).strip() != "-":
                return str(val).strip()
    return ""


def number_value(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        val = item.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
        if isinstance(val, str):
            m = re.search(r"\d+(?:\.\d+)?", val.replace(",", "").translate(str.maketrans("０１２３４５６７８９．", "0123456789.")))
            if m:
                try:
                    n = float(m.group(0))
                    if n > 0:
                        return n
                except ValueError:
                    pass
    detail = item.get("detail")
    if isinstance(detail, dict):
        return number_value(detail, *keys)
    return None


def normalize_address(address: str) -> str:
    text = str(address or "").strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    text = text.replace("　", "").replace(" ", "")
    text = text.replace("ヶ", "ケ").replace("之", "の")
    text = text.replace("丁目", "-").replace("番地", "-").replace("番", "-").replace("号", "")
    text = re.sub(r"[-ー―‐−]+", "-", text)
    return text


def municipality(address: str) -> str | None:
    for city in TARGET_CITY:
        if city in address:
            return city
    # Any explicit municipality/city/town outside the two target cities is non-target.
    m = re.search(r"(?:北海道|東京都|大阪府|京都府|.{2,3}県)?([^都道府県市区町村]{1,8}市)", address)
    if m:
        return m.group(1)
    if any(x in address for x in ("埼玉県", "茨城県", "東京都", "神奈川県")):
        return "__outside_prefecture__"
    return None


def url_city_code(item: dict[str, Any]) -> str:
    url = text_value(item, "sourceUrl", "url")
    m = re.search(r"/sc_([a-z0-9_]+)/", url, re.I)
    return "sc_" + m.group(1).lower() if m else ""


def target_station(item: dict[str, Any]) -> str:
    return text_value(item, "targetStation", "station", "nearestStation", "target_station")


def walk_minutes(item: dict[str, Any]) -> float | None:
    return number_value(item, "targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes", "walk")


def classify_area(item: dict[str, Any]) -> tuple[str, str, str | None]:
    """Return accepted/review/rejected, reason, normalized target area."""
    address = text_value(item, "address", "fullAddress", "propertyAddress", "location")
    city = municipality(address)
    code = url_city_code(item)
    station = target_station(item)
    walk = walk_minutes(item)
    code_city = CITY_CODES.get(code)

    if city and city not in TARGET_CITY:
        return "rejected", f"municipality_outside_target:{city}", None
    if city == "__outside_prefecture__":
        return "rejected", "prefecture_outside_target", None
    if code and code not in CITY_CODES and city is None:
        # URL municipality is explicit but not one of the allowed municipalities.
        return "rejected", f"url_city_code_outside_target:{code}", None
    if city in TARGET_CITY:
        area = TARGET_CITY[city]
        if code and (code not in CITY_CODES or (code_city and code_city[0] != city)):
            return "review", "address_url_city_mismatch", area
        if station:
            expected = "柏の葉キャンパス" if city == "柏市" else "流山おおたかの森"
            if station != expected:
                # A different station may still be in the wider municipality, but it is not
                # accepted as a target-station listing without further verification.
                return "review", "target_station_mismatch", area
            if walk is not None and walk <= 20:
                return "accepted", "target_city_station_walk_within_20m", area
            if walk is not None and walk > 20:
                return "rejected", "target_station_walk_over_20m", area
            return "review", "target_station_walk_unknown", area
        # Recognized target-town names are not enough to infer a walk time or exact target.
        return "review", "target_city_station_unverified", area
    if code_city:
        return "review", "city_from_url_only_address_missing", code_city[1]
    if not address:
        return "review", "address_missing", None
    return "review", "municipality_unresolved", None


def school_status(item: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    address = normalize_address(text_value(item, "address", "fullAddress", "propertyAddress"))
    verified = config.get("verifiedAddresses", []) if isinstance(config, dict) else []
    verified_norm = {normalize_address(x) for x in verified if isinstance(x, str)}
    if address and address in verified_norm:
        return "confirmed", "config/school_districts.json:verifiedAddresses"
    return "unconfirmed", "official_source_required_exact_address"


def stable_hash(value: str, prefix: str = "") -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def pid(item: dict[str, Any]) -> str:
    return str(item.get("propertyId") or item.get("id") or "").strip()


def price(item: dict[str, Any]) -> float | None:
    value = number_value(item, "currentPrice", "priceYen", "price")
    if value is not None:
        return value
    man = number_value(item, "priceMan", "currentPriceMan")
    return man * 10000 if man is not None else None


def physical_signature(item: dict[str, Any]) -> tuple[str, float | None, float | None, int | None]:
    address = normalize_address(text_value(item, "address", "fullAddress", "propertyAddress"))
    land = number_value(item, "landAreaM2", "land", "landArea", "landSize")
    building = number_value(item, "buildingAreaM2", "building", "buildingArea", "buildingSize")
    year = number_value(item, "constructionYear", "builtYear", "year")
    return address, land, building, int(year) if year else None


def high_confidence_same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    aa, al, ab, ay = physical_signature(a)
    ba, bl, bb, by = physical_signature(b)
    if not aa or aa != ba or len(aa) < 8:
        return False
    # Town-level addresses such as "千葉県柏市若柴" do not uniquely identify a house.
    if not re.search(r"\d", aa):
        return False
    if al is None or bl is None or ab is None or bb is None:
        return False
    if abs(al - bl) > max(2.0, al * 0.02) or abs(ab - bb) > max(2.0, ab * 0.02):
        return False
    if ay is not None and by is not None and ay != by:
        return False
    return True


def duplicate_similarity(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, list[str]]:
    aa, al, ab, ay = physical_signature(a)
    ba, bl, bb, by = physical_signature(b)
    score, reasons = 0.0, []
    if aa and ba and aa == ba:
        score += 0.45; reasons.append("normalized_address_match")
    elif aa and ba and (aa in ba or ba in aa) and min(len(aa), len(ba)) >= 8:
        score += 0.25; reasons.append("address_partial_match")
    if al and bl:
        ratio = abs(al - bl) / max(al, bl)
        if ratio <= 0.02: score += 0.20; reasons.append("land_area_match")
        elif ratio <= 0.08: score += 0.10; reasons.append("land_area_close")
    if ab and bb:
        ratio = abs(ab - bb) / max(ab, bb)
        if ratio <= 0.02: score += 0.20; reasons.append("building_area_match")
        elif ratio <= 0.08: score += 0.10; reasons.append("building_area_close")
    if ay and by and ay == by:
        score += 0.10; reasons.append("construction_year_match")
    if a.get("layout") and b.get("layout") and str(a.get("layout")) == str(b.get("layout")):
        score += 0.05; reasons.append("layout_match")
    return min(1.0, score), reasons


def build_duplicate_groups(houses: list[dict[str, Any]], discovered: list[dict[str, Any]], decisions: dict[str, str] | None = None) -> dict[str, Any]:
    # Merge decisions are logical only: listing IDs and source URLs are never rewritten.
    decisions = decisions or {}
    all_records: dict[str, dict[str, Any]] = {}
    for item in houses + discovered:
        key = pid(item)
        if key:
            all_records.setdefault(key, item)
    items = list(all_records.values())
    parent = {pid(x): pid(x) for x in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # deterministic canonical ID: keep the one with better detail completeness,
            # then oldest first-seen, then lexical ID.
            def rank(k: str) -> tuple[int, str, str]:
                item = all_records[k]
                complete = int(bool(item.get("detailFetched"))) + int(bool(item.get("address"))) + int(bool(item.get("landAreaM2") or item.get("land"))) + int(bool(item.get("buildingAreaM2") or item.get("building")))
                first = str(item.get("firstSeenAt") or "9999")
                return (-complete, first, k)
            keep, drop = sorted([ra, rb], key=rank)
            parent[drop] = keep

    # Block candidates by normalized address / town key rather than O(n^2) comparisons
    # across every listing. Town-level addresses are candidate-only and never auto-merged.
    exact_address_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    address_block_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        address, _, _, _ = physical_signature(item)
        if not address:
            continue
        exact_address_buckets[address].append(item)
        # Removing digits and punctuation yields a coarse neighborhood block.
        block = re.sub(r"[0-9\-]", "", address)
        if len(block) >= 8:
            address_block_buckets[block].append(item)

    auto_pairs: set[tuple[str, str]] = set()
    candidate_pairs: set[tuple[str, str]] = set()
    for bucket in exact_address_buckets.values():
        if len(bucket) > 1:
            for i, a in enumerate(bucket):
                for b in bucket[i + 1:]:
                    auto_pairs.add(tuple(sorted((pid(a), pid(b)))))
                    candidate_pairs.add(tuple(sorted((pid(a), pid(b)))))
    for bucket in address_block_buckets.values():
        if len(bucket) > 1:
            # Large town blocks can produce noisy candidate floods; use exact address buckets
            # for town-only records and coarse blocks only where at least one full address exists.
            for i, a in enumerate(bucket):
                addr_a = physical_signature(a)[0]
                for b in bucket[i + 1:]:
                    addr_b = physical_signature(b)[0]
                    if re.search(r"\d", addr_a) or re.search(r"\d", addr_b):
                        candidate_pairs.add(tuple(sorted((pid(a), pid(b)))))

    auto_merge_operations = 0
    for aid, bid in sorted(auto_pairs):
        if aid in parent and bid in parent and find(aid) != find(bid) and high_confidence_same(all_records[aid], all_records[bid]):
            union(aid, bid)
            auto_merge_operations += 1

    review_candidates = []
    manually_merged_pairs: list[tuple[str, str]] = []
    for aid, bid in sorted(candidate_pairs):
        if not aid or not bid or aid not in all_records or bid not in all_records or find(aid) == find(bid):
            continue
        a, b = all_records[aid], all_records[bid]
        score, reasons = duplicate_similarity(a, b)
        has_address_evidence = any(r in reasons for r in ("normalized_address_match", "address_partial_match"))
        if score >= 0.55 and has_address_evidence:
            candidate_id = stable_hash("|".join(sorted([aid, bid])), "dup:")
            decision = decisions.get(candidate_id, "")
            if decision == "same":
                manually_merged_pairs.append((aid, bid))
            elif decision != "different":
                review_candidates.append({
                    "candidateId": candidate_id,
                    "propertyIdA": aid,
                    "propertyIdB": bid,
                    "similarityScore": round(score, 2),
                    "reasons": reasons,
                    "decisionStatus": "review_required",
                })
    manual_merge_operations = 0
    for aid, bid in manually_merged_pairs:
        if aid in parent and bid in parent and find(aid) != find(bid):
            union(aid, bid)
            manual_merge_operations += 1

    # Rebuild final groups after manually approved merges.
    groups = defaultdict(list)
    for item in items:
        groups[find(pid(item))].append(pid(item))

    # Annotate all source records with canonical group ID; retain each listing record.
    canonical_by_id: dict[str, str] = {}
    for root, members in groups.items():
        canonical = root
        for member in members:
            canonical_by_id[member] = canonical
    for item in houses + discovered:
        key = pid(item)
        if not key:
            continue
        canonical = canonical_by_id.get(key, key)
        item["canonicalPropertyId"] = canonical
        item["propertyGroupId"] = stable_hash(canonical, "property-group:")
        if canonical == key:
            item["duplicateStatus"] = "unique" if len(groups.get(canonical, [canonical])) == 1 else "canonical"
            item["duplicateCandidateOf"] = None
        else:
            item["duplicateStatus"] = "merged"
            item["duplicateCandidateOf"] = canonical
        item["duplicateReviewCandidateCount"] = sum(1 for x in review_candidates if key in (x["propertyIdA"], x["propertyIdB"]))
    return {
        "groups": groups,
        "canonicalById": canonical_by_id,
        "reviewCandidates": review_candidates,
        "autoMergedListingCount": auto_merge_operations,
        "manualMergedListingCount": manual_merge_operations,
        "mergedListingCount": sum(max(0, len(v) - 1) for v in groups.values()),
    }


def transaction_rows(data: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return result
    for period in data.get("periods", []) if isinstance(data.get("periods"), list) else []:
        if not isinstance(period, dict):
            continue
        cities = period.get("cities", {})
        if not isinstance(cities, dict):
            continue
        for city_key, city_data in cities.items():
            if not isinstance(city_data, dict):
                continue
            city_name = city_data.get("name") or ("柏市" if city_key == "kashiwa" else "流山市" if city_key == "nagareyama" else "")
            for row in city_data.get("data", []) if isinstance(city_data.get("data"), list) else []:
                if isinstance(row, dict):
                    copy = dict(row)
                    copy["_cityName"] = city_name
                    copy["_period"] = period.get("period")
                    result.append(copy)
    # Also support a normalized flat transactions array in future schema versions.
    for row in data.get("transactions", []) if isinstance(data.get("transactions"), list) else []:
        if isinstance(row, dict): result.append(row)
    return result


def tx_num(row: dict[str, Any], *keys: str) -> float | None:
    return number_value(row, *keys)


def tx_price(row: dict[str, Any]) -> float | None:
    return tx_num(row, "TradePrice", "Price", "TransactionPrice", "price", "取引価格", "取引価格（総額）", "取引価格(総額)")


def tx_land(row: dict[str, Any]) -> float | None:
    return tx_num(row, "Area", "LandArea", "landArea", "土地面積", "面積")


def tx_building(row: dict[str, Any]) -> float | None:
    return tx_num(row, "BuildingArea", "buildingArea", "建物面積", "建物面積（㎡）")


def tx_year(row: dict[str, Any]) -> int | None:
    val = text_value(row, "BuildingYear", "BuiltYear", "constructionYear", "建築年")
    if not val:
        return None
    m = re.search(r"(?:19|20)\d{2}", val)
    return int(m.group(0)) if m else None


def transaction_area(row: dict[str, Any]) -> str | None:
    city = str(row.get("_cityName") or row.get("city") or row.get("CityName") or row.get("市区町村名") or "")
    if "柏市" in city: return "柏の葉キャンパス"
    if "流山市" in city: return "流山おおたかの森"
    return None


def median_or_none(values: list[float]) -> float | None:
    values = [x for x in values if x is not None and x > 0]
    return round(statistics.median(values), 2) if values else None


def build_transaction_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for area in TARGET_CITY.values():
        subset = [r for r in rows if transaction_area(r) == area and tx_price(r)]
        prices = [tx_price(r) for r in subset if tx_price(r)]
        land_prices = []
        for row in subset:
            p, land = tx_price(row), tx_land(row)
            if p and land and land > 0:
                land_prices.append(p / land)
        out[area] = {
            "transactionCount": len(subset),
            "medianTransactionPriceYen": median_or_none(prices),
            "medianLandPricePerM2Yen": median_or_none(land_prices),
            "dataConfidence": "medium" if len(subset) >= 10 else "low" if len(subset) < 3 else "limited",
            "source": "国土交通省 不動産情報ライブラリ XIT001",
        }
    return out


def find_comparables(item: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    address = text_value(item, "address", "fullAddress")
    city = municipality(address)
    area = TARGET_CITY.get(city or "")
    land = number_value(item, "landAreaM2", "land")
    building = number_value(item, "buildingAreaM2", "building")
    year = number_value(item, "constructionYear", "builtYear")
    if not area:
        return []
    candidates = []
    for row in rows:
        if transaction_area(row) != area or not tx_price(row):
            continue
        rland, rbuilding, ryear = tx_land(row), tx_building(row), tx_year(row)
        if land and rland and not (land * 0.65 <= rland <= land * 1.35):
            continue
        if building and rbuilding and not (building * 0.65 <= rbuilding <= building * 1.35):
            continue
        if year and ryear and abs(year - ryear) > 20:
            continue
        candidates.append(row)
    return candidates


def price_history(item: dict[str, Any]) -> list[tuple[datetime, float]]:
    result: list[tuple[datetime, float]] = []
    for entry in item.get("priceHistory", []) if isinstance(item.get("priceHistory"), list) else []:
        if not isinstance(entry, dict): continue
        dt = parse_dt(entry.get("date") or entry.get("observedAt") or entry.get("timestamp"))
        val = number_value(entry, "priceYen", "currentPrice", "price", "value")
        if dt and val: result.append((dt, val))
    return result


def score_property(item: dict[str, Any], comparables: list[dict[str, Any]], similar_listings: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    current = price(item)
    components: list[tuple[float, float, str]] = []
    reasons: list[str] = []
    if current and len(comparables) >= 3:
        comp_prices = [tx_price(r) for r in comparables if tx_price(r)]
        med = statistics.median(comp_prices) if comp_prices else None
        if med and med > 0:
            delta = (med - current) / med
            score = max(0.0, min(100.0, 50.0 + delta * 250.0))
            components.append((score, 0.40, "transaction_comparison"))
            reasons.append(f"類似取引中央値との差：{delta * 100:+.1f}%（比較{len(comp_prices)}件）")
    reduction = number_value(item, "priceReductionRate")
    if reduction is not None and 0 <= reduction <= 1:
        # main.py stores the reduction ratio (e.g. 0.032 = 3.2%), not percent.
        reduction *= 100
    if reduction is None:
        first = number_value(item, "firstPrice", "firstPriceYen")
        if first and current and first >= current:
            reduction = (first - current) / first * 100
    if reduction is not None:
        components.append((max(0.0, min(100.0, 50.0 + reduction * 5.0)), 0.25, "price_reduction"))
        if reduction > 0.1: reasons.append(f"初回価格から{reduction:.1f}%値下げ")
        else: reasons.append("初回価格からの値下げは未確認")
    days = number_value(item, "daysListed")
    if days is None:
        first_seen = parse_dt(item.get("firstSeenAt") or item.get("firstSeen"))
        days = max(0, (now - first_seen).days) if first_seen else None
    if days is not None:
        day_score = 35 if days < 30 else 50 if days < 90 else 65 if days < 180 else 80
        components.append((float(day_score), 0.15, "listing_duration"))
        reasons.append(f"掲載観測期間：約{int(days)}日")
    if current and len(similar_listings) >= 3:
        comp = [price(x) for x in similar_listings if price(x)]
        if len(comp) >= 3:
            med = statistics.median(comp)
            delta = (med - current) / med if med else 0
            components.append((max(0.0, min(100.0, 50.0 + delta * 250.0)), 0.20, "similar_listing_price"))
            reasons.append(f"類似掲載中央値との比較：{delta * 100:+.1f}%（比較{len(comp)}件）")
    if not components:
        return {"score": None, "label": "データ不足", "confidence": "low", "components": {}, "reasons": ["価格・比較対象・履歴が不足しているため算出できません"]}
    total_weight = sum(weight for _, weight, _ in components)
    score = round(sum(value * weight for value, weight, _ in components) / total_weight)
    evidence = len(comparables) >= 3 and len(similar_listings) >= 3
    confidence = "high" if evidence and len(components) >= 3 else "medium" if len(components) >= 2 else "low"
    label = "割安候補" if score >= 65 else "相場並み" if score >= 40 else "割高候補"
    return {
        "score": score, "label": label, "confidence": confidence,
        "components": {name: round(value) for value, _, name in components},
        "reasons": reasons,
        "transactionComparableCount": len(comparables),
        "similarListingCount": sum(1 for x in similar_listings if price(x)),
    }


def market_window_metrics(area: str, houses: list[dict[str, Any]], observations: list[dict[str, Any]], transactions: dict[str, Any], now: datetime, days: int) -> dict[str, Any]:
    cutoff = now - timedelta(days=days)
    area_houses = [x for x in houses if x.get("areaDecision") == "accepted" and x.get("area") == area and x.get("canonicalPropertyId", pid(x)) == pid(x)]
    area_obs = [x for x in observations if x.get("areaDecision") == "accepted" and x.get("area") == area and (dt := parse_dt(x.get("observedAt") or x.get("timestamp"))) and dt >= cutoff and dt <= now]
    per_id: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for obs in area_obs:
        p = number_value(obs, "currentPrice", "priceYen", "price")
        dt = parse_dt(obs.get("observedAt") or obs.get("timestamp"))
        if p and dt: per_id[str(obs.get("propertyId") or obs.get("id"))].append((dt, p))
    price_deltas = []
    period_start_prices = []
    period_end_prices = []
    for hist in per_id.values():
        hist.sort()
        if hist:
            period_start_prices.append(hist[0][1])
            period_end_prices.append(hist[-1][1])
        if len(hist) >= 2 and hist[0][1] > 0:
            price_deltas.append((hist[-1][1] - hist[0][1]) / hist[0][1] * 100)
    current_prices = [price(x) for x in area_houses if price(x)]
    reductions = []
    for x in area_houses:
        reduction = number_value(x, "priceReductionRate")
        if reduction is not None and reduction > 0:
            reductions.append(reduction * 100 if reduction <= 1 else reduction)
    tx = transactions.get(area, {})
    observed_dates = [parse_dt(x.get("observedAt") or x.get("timestamp")) for x in area_obs]
    observed_dates = [x for x in observed_dates if x]
    span_days = max(0, (max(observed_dates) - min(observed_dates)).days) if observed_dates else 0
    coverage_pct = round(min(100.0, span_days / max(1, days) * 100), 1)
    sufficient = len(area_houses) >= 5 and span_days >= days * 0.25 and len(price_deltas) >= 5
    return {
        "periodDays": days,
        "area": area,
        "activeCanonicalListingCount": len(area_houses),
        "observationCount": len(area_obs),
        "propertiesWithRepeatedPriceObservation": len(price_deltas),
        "observationSpanDays": span_days,
        "periodCoveragePct": coverage_pct,
        "medianAskingPriceYen": median_or_none(current_prices),
        "medianPriceAtWindowStartYen": median_or_none(period_start_prices),
        "medianPriceAtWindowEndYen": median_or_none(period_end_prices),
        "medianObservedPriceChangePct": round(statistics.median(price_deltas), 2) if price_deltas else None,
        "priceReductionPropertyCount": len(reductions),
        "medianRecordedReductionPct": round(statistics.median(reductions), 2) if reductions else None,
        "transactionCountAvailable": tx.get("transactionCount", 0),
        "medianTransactionPriceYen": tx.get("medianTransactionPriceYen"),
        "medianLandPricePerM2Yen": tx.get("medianLandPricePerM2Yen"),
        "confidence": "medium" if sufficient else "low",
        "limitations": ([] if sufficient else [f"期間内の観測カバー率は{coverage_pct:.1f}%（観測期間{span_days}日）。十分な長期履歴が蓄積するまでは参考値です"])
            + ([] if len(price_deltas) >= 5 else ["期間内の複数回観測が少なく、価格トレンドは参考値です"]),
    }


def apply_pipeline() -> dict[str, Any]:
    now = utc_now()
    houses_data = load_json(HOUSES_PATH, {})
    discovered_data = load_json(DISCOVERED_PATH, {})
    observations_data = load_json(OBSERVATIONS_PATH, {})
    summary = load_json(SUMMARY_PATH, {})
    tx_data = load_json(TRANSACTIONS_PATH, {})
    school_config = load_json(SCHOOL_CONFIG_PATH, {})
    houses = records_from(houses_data)
    discovered = records_from(discovered_data)
    observations = records_from(observations_data, "observations")

    area_decisions_by_id: dict[str, str] = {}
    previous_quarantine_by_id: dict[str, str] = {}
    for prior in houses + discovered:
        prior_id = pid(prior)
        prior_quarantine = prior.get("areaQuarantinedAt")
        if prior_id and prior.get("areaDecision") == "rejected" and prior_quarantine:
            current = previous_quarantine_by_id.get(prior_id)
            if not current or (parse_dt(prior_quarantine) and parse_dt(current) and parse_dt(prior_quarantine) < parse_dt(current)):
                previous_quarantine_by_id[prior_id] = str(prior_quarantine)
    for item in houses + discovered:
        decision, reason, area = classify_area(item)
        if decision == "rejected":
            item["areaQuarantinedAt"] = item.get("areaQuarantinedAt") or previous_quarantine_by_id.get(pid(item)) or iso(now)
            item["areaStatus"] = item.get("areaStatus") or "pending"
        else:
            item.pop("areaQuarantinedAt", None)
        item["areaDecision"] = decision
        item["areaDecisionReason"] = reason
        if area:
            item["area"] = area
        item["areaPolicyVersion"] = VERSION
        school, school_reason = school_status(item, school_config)
        item["schoolDistrictStatus"] = school
        item["schoolDistrictSource"] = school_reason
        item["schoolDistrictVerifiedAt"] = item.get("schoolDistrictVerifiedAt") if school == "confirmed" else None
        area_decisions_by_id.setdefault(pid(item) or f"unkeyed:{len(area_decisions_by_id)}", decision)
    area_counts = Counter(area_decisions_by_id.values())

    # Logical duplicate groups; source IDs and URLs remain intact for traceability.
    decision_data = load_json(DATA / "duplicate_review_decisions.json", {"decisions": {}})
    raw_decisions = decision_data.get("decisions", {}) if isinstance(decision_data, dict) else {}
    decision_map = {str(k): str(v) for k, v in raw_decisions.items() if v in ("same", "different")} if isinstance(raw_decisions, dict) else {}
    duplicate_info = build_duplicate_groups(houses, discovered, decision_map)
    canonical_by_id = duplicate_info["canonicalById"]
    property_by_id: dict[str, dict[str, Any]] = {}
    for item in houses + discovered:
        if pid(item):
            property_by_id.setdefault(pid(item), item)
    for obs in observations:
        key = pid(obs)
        if key in canonical_by_id:
            obs["canonicalPropertyId"] = canonical_by_id[key]
        linked = property_by_id.get(key)
        if linked:
            obs["areaDecision"] = linked.get("areaDecision", "review")
            if linked.get("area"):
                obs["area"] = linked["area"]

    tx_rows = transaction_rows(tx_data)
    transaction_metrics = build_transaction_metrics(tx_rows)
    accepted_canonical = [x for x in houses if x.get("areaDecision") == "accepted" and x.get("canonicalPropertyId", pid(x)) == pid(x)]
    for item in houses:
        key = pid(item)
        canonical = item.get("canonicalPropertyId", key)
        item["isCanonicalProperty"] = canonical == key
        item["marketAnalysisEligible"] = item.get("areaDecision") == "accepted" and canonical == key
        if not item["marketAnalysisEligible"]:
            item["marketIndicators"] = {"score": None, "label": "分析対象外", "confidence": "low", "components": {}, "reasons": ["対象エリア外・要確認・重複統合先のため通常評価から除外"]}
            continue
        comparables = find_comparables(item, tx_rows)
        addr = text_value(item, "address")
        city = municipality(addr)
        similar = [x for x in accepted_canonical if x is not item and municipality(text_value(x, "address")) == city and price(x) and price(item) and abs(price(x) - price(item)) <= max(price(item) * 0.35, 1_000_000) and (not number_value(item, "landAreaM2", "land") or not number_value(x, "landAreaM2", "land") or abs(number_value(x, "landAreaM2", "land") - number_value(item, "landAreaM2", "land")) <= max(25, number_value(item, "landAreaM2", "land") * 0.35))]
        item["marketIndicators"] = score_property(item, comparables, similar, now)
        item["marketIndicators"]["area"] = item.get("area")
        item["marketIndicators"]["asOf"] = iso(now)

    area_metrics: dict[str, Any] = {}
    for area in TARGET_CITY.values():
        area_metrics[area] = {
            "3m": market_window_metrics(area, houses, observations, transaction_metrics, now, 90),
            "12m": market_window_metrics(area, houses, observations, transaction_metrics, now, 365),
        }

    # Candidate review pairs are stored separately from canonical records to avoid
    # inflating the main listing database. Stable candidate IDs make this idempotent.
    review_data = {
        "schemaVersion": "1.0",
        "updatedAt": iso(now),
        "algorithmVersion": VERSION,
        "count": len(duplicate_info["reviewCandidates"]),
        "decisionsApplied": len(decision_map),
        "candidates": duplicate_info["reviewCandidates"],
    }
    atomic_save(DATA / "duplicate_review_candidates.json", review_data)

    # Quarantine retention: only old rejected discovery records are pruned here.
    # Master records are kept unless they are rejected, old, and have no user decision.
    user_selections = load_json(USER_SELECTIONS_PATH, {})
    selections = user_selections.get("properties", {}) if isinstance(user_selections, dict) else {}
    cutoff = now - timedelta(days=QUARANTINE_RETENTION_DAYS)
    pruned_discovered = 0
    kept_discovered = []
    for item in discovered:
        qdt = parse_dt(item.get("areaQuarantinedAt"))
        key = pid(item)
        protected = key in selections or bool(item.get("userReviewedAt"))
        if item.get("areaDecision") == "rejected" and qdt and qdt < cutoff and not protected:
            pruned_discovered += 1
            continue
        kept_discovered.append(item)
    discovered = kept_discovered
    pruned_houses = 0
    kept_houses = []
    for item in houses:
        qdt = parse_dt(item.get("areaQuarantinedAt"))
        key = pid(item)
        protected = key in selections or bool(item.get("userReviewedAt"))
        if item.get("areaDecision") == "rejected" and qdt and qdt < cutoff and not protected:
            pruned_houses += 1
            continue
        kept_houses.append(item)
    houses = kept_houses

    # Summary fields consumed by the dashboard. Keep existing fields for backward compatibility.
    summary["updatedAt"] = iso(now)
    summary["marketPipelineVersion"] = VERSION
    summary["areaDecisionCounts"] = dict(area_counts)
    summary["quarantineRetentionDays"] = QUARANTINE_RETENTION_DAYS
    summary["quarantinePrunedThisRun"] = {"discovered": pruned_discovered, "houses": pruned_houses}
    summary["duplicateManagement"] = {
        "highConfidenceGroupsMergedLogically": duplicate_info["autoMergedListingCount"],
        "manualMergeOperations": duplicate_info["manualMergedListingCount"],
        "manualDecisionsLoaded": len(decision_map),
        "reviewCandidateCount": len(duplicate_info["reviewCandidates"]),
        "autoMergePolicy": "high_confidence_only; preserve_listing_ids_and_urls",
    }
    summary["marketAnalysis"] = {
        "updatedAt": iso(now),
        "source": "SUUMO observations + MLIT XIT001 transactions when available",
        "periods": ["3m", "12m"],
        "areas": area_metrics,
        "transactionMetrics": transaction_metrics,
        "limitations": ["観測履歴が短い場合、3か月・12か月指標も実質的には取得済み履歴の範囲に限定されます", "取引価格は売出価格ではなく過去の取引情報です。個別物件の参考値として扱い、成約価格の保証には使いません"],
    }
    summary["bargainIndicator"] = {
        "method": "explainable_rule_based_v1",
        "labels": {"65-100": "割安候補", "40-64": "相場並み", "0-39": "割高候補", "null": "データ不足"},
        "note": "比較対象が少ない場合は信頼度を低くし、根拠を併記する",
    }
    summary["schoolDistrictPolicy"] = {
        "school": "柏の葉小学校区",
        "status": "unconfirmed unless exact address exists in config/school_districts.json verifiedAddresses",
        "officialSource": school_config.get("officialSource") if isinstance(school_config, dict) else None,
    }

    for data, rows in ((houses_data, houses), (discovered_data, discovered)):
        data["updatedAt"] = iso(now)
        data["count"] = len(rows)
        data["properties"] = rows
        data["marketPipelineVersion"] = VERSION
    if isinstance(observations_data, dict):
        observations_data["updatedAt"] = iso(now)
        observations_data["count"] = len(observations)
        observations_data["observations"] = observations
    else:
        observations_data = {"version": "2.0", "updatedAt": iso(now), "count": len(observations), "observations": observations}

    atomic_save(HOUSES_PATH, houses_data)
    atomic_save(DISCOVERED_PATH, discovered_data)
    atomic_save(OBSERVATIONS_PATH, observations_data)
    atomic_save(SUMMARY_PATH, summary)
    return {
        "areaCounts": dict(area_counts),
        "houses": len(houses),
        "discovered": len(discovered),
        "duplicateReviewCandidates": len(duplicate_info["reviewCandidates"]),
        "logicalDuplicateListings": duplicate_info["mergedListingCount"],
        "autoMergedListingCount": duplicate_info["autoMergedListingCount"],
        "manualMergedListingCount": duplicate_info["manualMergedListingCount"],
        "pruned": {"houses": pruned_houses, "discovered": pruned_discovered},
        "transactionMetrics": transaction_metrics,
    }


def main() -> int:
    report = apply_pipeline()
    print("=== Market/Area/Duplicate pipeline ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[MARKET-PIPELINE-ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
