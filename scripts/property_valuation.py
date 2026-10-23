#!/usr/bin/env python3
"""Conservative, explainable detached-house valuation and deal scoring.

This module deliberately separates:
- estimated land/building components (rough model, not an appraisal),
- market/amenity premiums (scored, not converted to unsupported yen),
- price attractiveness,
- confidence and data limitations.

No missing value is silently treated as zero. City-wide transactions are fallback
comparables only and receive lower confidence than same-district transactions.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Any

VERSION = "2026-10-23-v1"
TARGET_CITY_TO_AREA = {"柏市": "柏の葉キャンパス", "流山市": "流山おおたかの森"}

# Transparent indicative replacement-cost assumptions; validate against local evidence.
REPLACEMENT_COST_YEN_M2 = {
    "木造": 220_000,
    "軽量鉄骨造": 260_000,
    "鉄骨造": 300_000,
    "RC造": 350_000,
    "SRC造": 380_000,
}
BUILDING_LIFE_YEARS = {
    "木造": 30,
    "軽量鉄骨造": 35,
    "鉄骨造": 40,
    "RC造": 50,
    "SRC造": 50,
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).replace(",", "").translate(str.maketrans("０１２３４５６７８９．", "0123456789."))
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group())
        return number if number > 0 else None
    except ValueError:
        return None


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", "-", "不明"):
            return value
    detail = item.get("detail")
    if isinstance(detail, dict):
        for key in keys:
            value = detail.get(key)
            if detail.get(key) not in (None, "", "-", "不明"):
                return detail[key]
    return None


def _norm(text: Any) -> str:
    value = str(text or "").strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return re.sub(r"[\s　\-ー―‐−丁目番地号]", "", value)


def _city(item: dict[str, Any]) -> str | None:
    address = str(_first(item, "address", "fullAddress", "propertyAddress", "location") or "")
    for city in TARGET_CITY_TO_AREA:
        if city in address:
            return city
    city = str(item.get("_cityName") or item.get("city") or item.get("Municipality") or "")
    for name in TARGET_CITY_TO_AREA:
        if name in city:
            return name
    code = str(item.get("MunicipalityCode") or "")
    return {"12217": "柏市", "12220": "流山市"}.get(code)


def _price(row: dict[str, Any]) -> float | None:
    return _number(_first(row, "TradePrice", "Price", "TransactionPrice", "price", "取引価格", "取引価格（総額）", "取引価格(総額)"))


def _land(row: dict[str, Any]) -> float | None:
    return _number(_first(row, "Area", "LandArea", "landArea", "土地面積", "面積"))


def _building(row: dict[str, Any]) -> float | None:
    return _number(_first(row, "TotalFloorArea", "BuildingArea", "buildingArea", "建物面積", "建物面積（㎡）"))


def _year(row: dict[str, Any]) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", str(_first(row, "BuildingYear", "BuiltYear", "constructionYear", "建築年") or ""))
    return int(match.group()) if match else None


def _district(row: dict[str, Any]) -> str:
    return _norm(_first(row, "DistrictName", "districtName", "町名", "地区名") or "")


def _structure(row: dict[str, Any]) -> str:
    raw = str(_first(row, "Structure", "structure", "構造") or "")
    if "木" in raw:
        return "木造"
    if "軽量鉄骨" in raw:
        return "軽量鉄骨造"
    if "鉄骨鉄筋" in raw:
        return "SRC造"
    if "鉄筋" in raw or "RC" in raw.upper():
        return "RC造"
    if "鉄骨" in raw:
        return "鉄骨造"
    return "木造"  # explicit fallback assumption, reported as assumed


def _building_residual(row: dict[str, Any], now_year: int) -> tuple[float | None, bool]:
    area = _building(row)
    year = _year(row)
    if not area:
        return None, False
    structure_raw = str(_first(row, "Structure", "structure", "構造") or "")
    structure = _structure(row)
    replacement = REPLACEMENT_COST_YEN_M2[structure]
    life = BUILDING_LIFE_YEARS[structure]
    age = max(0, now_year - year) if year else None
    # Unknown year uses a conservative 50% residual and lowers confidence.
    residual_ratio = max(0.05, 1 - age / life) if age is not None else 0.50
    assumed_structure = not bool(structure_raw)
    return area * replacement * residual_ratio, not assumed_structure and age is not None


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v > 0]
    return float(statistics.median(clean)) if clean else None


def _percentile_range(values: list[float]) -> tuple[float | None, float | None]:
    clean = sorted(v for v in values if v > 0)
    if not clean:
        return None, None
    return clean[int((len(clean) - 1) * 0.20)], clean[int((len(clean) - 1) * 0.80)]


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def _price_score(gap_pct: float | None) -> float | None:
    if gap_pct is None:
        return None
    # +10% below estimate => 75; -10% above estimate => 25.
    return _clamp(50 + gap_pct * 2.5)


def _land_score(land: float | None, comps: list[dict[str, Any]]) -> float | None:
    if land is None:
        return None
    comp_areas = [_land(x) for x in comps]
    comp_areas = [x for x in comp_areas if x]
    if not comp_areas:
        return None
    med = statistics.median(comp_areas)
    # Avoid rewarding oversized lots indefinitely; 100-180m² is a practical target range.
    size_score = 75 if 100 <= land <= 180 else 60 if 80 <= land < 100 or 180 < land <= 220 else 40
    comparability = 10 if 0.75 <= land / med <= 1.35 else 0
    return _clamp(size_score + comparability)


def _building_score(item: dict[str, Any], now_year: int) -> float | None:
    area = _number(_first(item, "buildingAreaM2", "building", "buildingArea", "buildingSize"))
    year = _number(_first(item, "constructionYear", "builtYear", "year"))
    if area is None and year is None:
        return None
    score = 50.0
    if area is not None:
        score += 15 if 90 <= area <= 125 else 7 if 80 <= area < 90 or 125 < area <= 140 else -5
    if year is not None:
        age = max(0, now_year - int(year))
        score += 20 if age <= 10 else 12 if age <= 20 else 4 if age <= 30 else -8
    return _clamp(score)


def _premium_score(item: dict[str, Any]) -> tuple[float | None, list[str]]:
    scores: list[float] = []
    reasons: list[str] = []
    walk = _number(_first(item, "targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes", "walk"))
    if walk is not None:
        score = 90 if walk <= 5 else 80 if walk <= 10 else 65 if walk <= 15 else 50 if walk <= 20 else 25
        scores.append(score)
        reasons.append(f"駅徒歩{int(walk)}分を立地評価に反映")
    if item.get("schoolDistrictStatus") == "confirmed":
        scores.append(90)
        reasons.append("柏の葉小学校区は設定済み公式確認情報に基づき加点")
    elif item.get("area") == "柏の葉キャンパス" or item.get("schoolDistrictCandidate"):
        scores.append(50)
        reasons.append("柏の葉小学校区は未確認のため加点していない")
    title = " ".join(str(_first(item, k) or "") for k in ("listingTitle", "name", "description", "remarks"))
    positive_terms = [
        ("整形地", 5), ("南道路", 5), ("南向き", 4), ("角地", 4),
        ("駐車", 2), ("太陽光", 3), ("床暖房", 2), ("長期優良", 4),
        ("耐震", 2), ("断熱", 2), ("リフォーム", 2), ("リノベーション", 2),
    ]
    title_points = 0
    for term, points in positive_terms:
        if term in title:
            title_points += points
            reasons.append(f"掲載情報に「{term}」の記載あり（要確認）")
    if title.strip():
        scores.append(_clamp(50 + title_points))
    if not scores:
        return None, ["駅距離・学区・プレミアム情報が不足"]
    return round(statistics.mean(scores), 1), reasons


def _resale_score(item: dict[str, Any]) -> float | None:
    walk = _number(_first(item, "targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes", "walk"))
    land = _number(_first(item, "landAreaM2", "land", "landArea"))
    building = _number(_first(item, "buildingAreaM2", "building", "buildingArea"))
    values = []
    if walk is not None:
        values.append(85 if walk <= 10 else 70 if walk <= 15 else 50 if walk <= 20 else 30)
    if land is not None:
        values.append(75 if 100 <= land <= 180 else 55)
    if building is not None:
        values.append(70 if 80 <= building <= 140 else 50)
    return round(statistics.mean(values), 1) if values else None


def _listing_score(item: dict[str, Any]) -> tuple[float | None, list[str]]:
    current = _number(_first(item, "currentPrice", "priceYen", "price"))
    first = _number(_first(item, "firstPrice", "firstPriceYen"))
    reduction = _number(item.get("priceReductionRate"))
    if reduction is not None and reduction <= 1:
        reduction *= 100
    if reduction is None and current and first and first >= current:
        reduction = (first - current) / first * 100
    reasons = []
    values = []
    if reduction is not None:
        values.append(_clamp(50 + reduction * 4))
        if reduction > 0.1:
            reasons.append(f"初回価格から{reduction:.1f}%値下げ")
    days = _number(_first(item, "daysListed"))
    if days is not None:
        values.append(45 if days < 30 else 55 if days < 90 else 65 if days < 180 else 75)
    if not values:
        return None, ["価格改定・掲載期間の履歴が不足"]
    return round(statistics.mean(values), 1), reasons


def _risk_score(item: dict[str, Any]) -> tuple[float | None, list[str]]:
    # Missing hazard/legal data is not treated as evidence of safety.
    known_risks = []
    combined = " ".join(str(_first(item, k) or "") for k in ("listingTitle", "name", "remarks", "riskFlags", "hazardRisk"))
    for term in ("再建築不可", "擁壁", "崖", "土砂災害", "浸水", "セットバック", "接道義務"):
        if term in combined:
            known_risks.append(term)
    if known_risks:
        return 20.0, ["掲載情報にリスク確認語あり：" + "・".join(known_risks)]
    if item.get("riskAssessmentStatus") == "verified":
        return 75.0, ["リスク項目は確認済み情報に基づく"]
    return None, ["ハザード・接道・擁壁・法的制約は未確認（安全を意味しない）"]


def build_property_valuation(item: dict[str, Any], transaction_rows: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    now_year = (now or datetime.now().astimezone()).year
    city = _city(item)
    subject_address = str(_first(item, "address", "fullAddress", "propertyAddress") or "")
    subject_district = _norm(subject_address)
    land = _number(_first(item, "landAreaM2", "land", "landArea", "landSize"))
    building = _number(_first(item, "buildingAreaM2", "building", "buildingArea", "buildingSize"))
    year = _number(_first(item, "constructionYear", "builtYear", "year"))
    current_price = _number(_first(item, "currentPrice", "priceYen", "price"))

    rows = []
    for row in transaction_rows:
        if not isinstance(row, dict) or _city(row) != city:
            continue
        kind = str(_first(row, "Type", "type", "取引の種類") or "")
        if kind and "土地と建物" not in kind and "戸建" not in kind:
            continue
        txp, txl = _price(row), _land(row)
        if not txp or not txl:
            continue
        txb = _building(row)
        txyear = _year(row)
        if land and not land * 0.55 <= txl <= land * 1.8:
            continue
        if building and txb and not building * 0.55 <= txb <= building * 1.8:
            continue
        if year and txyear and abs(year - txyear) > 25:
            continue
        rows.append(row)

    # District-level comparables preferred; fallback is explicitly lower-confidence.
    district_rows = []
    if subject_district:
        for row in rows:
            district = _district(row)
            if district and (district in subject_district or subject_district.endswith(district) or district.endswith(subject_district)):
                district_rows.append(row)
    if len(district_rows) >= 3:
        comps, comparable_level = district_rows, "same_district"
    else:
        comps, comparable_level = rows, "city_wide_fallback"

    comparable_prices = [_price(r) for r in comps if _price(r)]
    comparable_total_median = _median(comparable_prices)
    estimated_building = None
    building_method = "unavailable"
    building_confidence = "low"
    if building:
        subject_struct = str(_first(item, "structure", "Structure", "buildingStructure") or "")
        structure = _structure({"Structure": subject_struct})
        life = BUILDING_LIFE_YEARS[structure]
        age = max(0, now_year - int(year)) if year else None
        ratio = max(0.05, 1 - age / life) if age is not None else 0.50
        estimated_building = round(building * REPLACEMENT_COST_YEN_M2[structure] * ratio)
        building_method = f"再調達単価×延床面積×残価率（{structure}、耐用年数{life}年の簡易仮定）"
        building_confidence = "medium" if year and subject_struct else "low"
    # Infer land component from comparable total price minus modelled building residual.
    inferred_land_unit_values = []
    for row in comps:
        txp, txl = _price(row), _land(row)
        if not txp or not txl:
            continue
        tx_building_value, _ = _building_residual(row, now_year)
        land_component = max(0.0, txp - (tx_building_value or 0.0))
        inferred_land_unit_values.append(land_component / txl)
    land_unit_median = _median(inferred_land_unit_values)
    estimated_land = round(land * land_unit_median) if land and land_unit_median else None

    # Only publish a combined land+building "base value" when there are at least
    # three same-district transactions. City-wide data can inform context but is not
    # precise enough to support an individual property's yen-level fair value.
    estimated_total = (
        estimated_land + estimated_building
        if estimated_land is not None
        and estimated_building is not None
        and comparable_level == "same_district"
        and len(comps) >= 3
        else None
    )
    gap_pct = ((estimated_total - current_price) / estimated_total * 100) if estimated_total and current_price else None
    lo, hi = _percentile_range(comparable_prices)
    reasons: list[str] = []
    limitations: list[str] = []
    if estimated_land is not None:
        reasons.append("土地価値は戸建て取引総額から建物残価モデルを差し引いた参考推計")
    else:
        limitations.append("比較可能な取引から土地価値を推定できない")
    if estimated_building is not None:
        reasons.append(building_method)
    else:
        limitations.append("建物面積が不足し建物価値を推定できない")
    if comparable_level == "city_wide_fallback":
        limitations.append("同一町丁目の取引が3件未満のため市内取引を代替比較に使用")
    if len(comps) < 3:
        limitations.append("比較対象が3件未満")
    if not year:
        limitations.append("築年が不明のため建物残価は仮定値")
    if not current_price:
        limitations.append("売出価格が不明")

    price_component = _price_score(gap_pct)
    land_component_score = _land_score(land, comps)
    building_component_score = _building_score(item, now_year)
    premium_score, premium_reasons = _premium_score(item)
    resale_score = _resale_score(item)
    listing_score, listing_reasons = _listing_score(item)
    risk_score, risk_reasons = _risk_score(item)

    component_specs = [
        ("price", "価格の割安度", 25, price_component),
        ("land", "土地価値・土地条件", 20, land_component_score),
        ("building", "建物の価値", 15, building_component_score),
        ("premium", "立地・プレミアム", 20, premium_score),
        ("resale", "将来の売却・保有価値", 10, resale_score),
        ("listing", "売出状況・交渉余地", 5, listing_score),
        ("risk", "リスク評価", 5, risk_score),
    ]
    available = [(key, label, weight, value) for key, label, weight, value in component_specs if value is not None]
    score = round(sum(value * weight for _, _, weight, value in available) / sum(weight for _, _, weight, _ in available)) if available else None
    available_weight = sum(weight for _, _, weight, _ in available)
    component_coverage = available_weight / 100
    if len(comps) >= 10 and component_coverage >= 0.8 and building_confidence == "medium" and comparable_level == "same_district":
        confidence = "high"
    elif len(comps) >= 3 and component_coverage >= 0.55 and comparable_level == "same_district":
        confidence = "medium"
    else:
        confidence = "low"

    reasons.extend(premium_reasons)
    reasons.extend(listing_reasons)
    reasons.extend(risk_reasons)
    if gap_pct is not None:
        reasons.append(f"推定土地・建物価値との価格乖離率：{gap_pct:+.1f}%（簡易推計）")
    if current_price and comparable_total_median:
        reasons.append(f"条件抽出した戸建て取引総額中央値：{comparable_total_median:,.0f}円（比較{len(comparable_prices)}件）")
    if land_unit_median:
        reasons.append(f"推定土地単価：約{land_unit_median:,.0f}円/㎡（建物残価を差し引く簡易モデル）")

    if score is None:
        label = "データ不足"
    elif estimated_total is None or gap_pct is None:
        label = "価格評価データ不足"
    elif confidence == "low":
        label = "信頼度低・要確認"
    elif gap_pct <= -10:
        label = "割高傾向"
    elif gap_pct >= 10 and score >= 75:
        label = "優先検討"
    elif gap_pct >= 5 and score >= 65:
        label = "条件次第で有望"
    elif gap_pct >= 0 and score >= 65:
        label = "条件次第で有望"
    elif score >= 45:
        label = "適正価格帯"
    else:
        label = "割高傾向"
    if risk_score is not None and risk_score <= 20:
        label = "要注意・保留"

    return {
        "modelVersion": VERSION,
        "asOfYear": now_year,
        "estimatedLandValueYen": estimated_land,
        "estimatedBuildingValueYen": estimated_building,
        "estimatedBaseValueYen": estimated_total,
        "estimatedTotalComparableTransactionMedianYen": comparable_total_median,
        "comparableTransactionPriceRange20to80Yen": {"low": lo, "high": hi},
        "estimatedLandUnitYenPerM2": round(land_unit_median) if land_unit_median else None,
        "currentAskingPriceYen": current_price,
        "valueGapYen": round(estimated_total - current_price) if estimated_total is not None and current_price else None,
        "valueGapPct": round(gap_pct, 2) if gap_pct is not None else None,
        "comparableCount": len(comps),
        "comparableLevel": comparable_level,
        "buildingValueMethod": building_method,
        "buildingValueConfidence": building_confidence,
        "score": score,
        "label": label,
        "confidence": confidence,
        "componentCoveragePct": round(component_coverage * 100, 1),
        "components": {
            key: {"label": name, "score": round(value, 1), "weight": weight}
            for key, name, weight, value in component_specs if value is not None
        },
        "reasons": reasons,
        "limitations": limitations,
        "disclaimer": "自動推定による参考値であり、不動産鑑定・査定ではありません。取引総額からの土地/建物分解は簡易モデルです。個別の接道・形状・地勢・ハザード・建物状態を保証しません。",
    }
