import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from storage import load_json, save_json
from adapters.suumo_search import SuumoSearchAdapter
from adapters.suumo_detail import SuumoDetailAdapter

# ============================================================
# 基本設定
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DETAIL_FETCH_LIMIT = 5
MAX_DETAIL_FETCH_ATTEMPTS = 3

DETAIL_PARSER_VERSION = "2026-09-21-v11"

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

INVALID_VALUES = {
    "",
    "-",
    "ー",
    "－",
    "―",
    "なし",
    "ヒント",
    "詳細を見る",
    "地図を見る",
    "周辺環境",
    "支払シミュレーション",
    "お問い合わせ",
    "資料請求",
    "確認",
}

PROMOTIONAL_WORDS = [
    "ヒント",
    "詳細を見る",
    "地図を見る",
    "周辺環境",
    "支払シミュレーション",
    "お問い合わせ",
    "資料請求",
    "確認",
    "おすすめ",
    "無料相談",
]

# ============================================================
# 共通ユーティリティ
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def safe_timestamp(value):
    if not value:
        return 0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0

def safe_int(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    if isinstance(value, str):
        text = value.strip().replace(",", "")
        match = re.search(r"-?[0-9]+", text)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None

    return None

def parse_price(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value > 0 else None

    if isinstance(value, float):
        price = int(value)
        return price if price > 0 else None

    text = str(value).strip().replace(",", "").replace(" ", "").replace("　", "")

    if not text:
        return None

    match = re.search(
        r"(?:(\d+(?:\.\d+)?)\s*億)"
        r"(?:\s*(\d+(?:\.\d+)?)\s*万)?"
        r"(?:円)?",
        text,
    )
    if match:
        oku = float(match.group(1))
        man = float(match.group(2) or 0)
        price = int(round(oku * 100_000_000 + man * 10_000))
        return price if price > 0 else None

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*万(?:円)?",
        text,
    )
    if match:
        price = int(round(float(match.group(1)) * 10_000))
        return price if price > 0 else None

    match = re.search(r"(\d[\d\s]*)\s*円", text)
    if match:
        try:
            price = int(match.group(1).replace(" ", ""))
            return price if price > 0 else None
        except ValueError:
            return None

    return None

def parse_float(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None

    text = str(value).replace(",", "").replace("　", " ").replace("m 2", "m2").replace("m²", "m2").replace("㎡", "m2")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)

    if not match:
        return None

    try:
        number = float(match.group(1))
        return number if number > 0 else None
    except ValueError:
        return None

def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()

def is_promotional_text(value):
    text = clean_text(value)
    if not text:
        return True
    return any(word in text for word in PROMOTIONAL_WORDS)

def is_valid_year_month(value):
    if not isinstance(value, str):
        return False

    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value.strip()))

def is_suspicious_station(value):
    if value is None:
        return True

    text = clean_text(value)

    if not text:
        return True

    suspicious_values = {"徒", "歩", "徒歩", "駅", "ヒント", "なし", "null", "none"}
    if text.lower() in suspicious_values:
        return True

    if len(text) > 15:
        return True

    promotional_words = ["見学", "お迎え", "提案", "案内", "ローン", "頭金", "月々", "物件"]
    if any(word in text for word in promotional_words):
        return True

    return False

def is_suspicious_address(value):
    if value is None:
        return True

    text = clean_text(value)

    if not text:
        return True

    suspicious_words = [
        "不動産",
        "株式会社",
        "有限会社",
        "支店",
        "営業所",
        "店舗",
        "センター",
        "アスライク",
        "免許番号",
    ]

    for word in suspicious_words:
        if word in text:
            return True

    if re.search(r"〒?\s*\d{3}-\d{4}", text):
        return True

    return False

def normalize_warning_list(value):
    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        text = clean_text(item)
        if text and text not in result:
            result.append(text)

    return result

def normalize_string_list(value):
    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        if item is None:
            continue
        text = clean_text(item)
        if text and text not in result:
            result.append(text)

    return result

# ============================================================
# 価格履歴
# ============================================================

def normalize_price_history(history, current_price, recorded_at):
    if not isinstance(history, list):
        history = []

    normalized = []
    for item in history:
        if not isinstance(item, dict):
            continue

        price = parse_price(item.get("price"))
        if price is None:
            continue

        recorded_time = item.get("recordedAt") or recorded_at
        normalized.append({"price": price, "recordedAt": recorded_time})

    deduplicated = []
    seen = set()

    for item in normalized:
        key = (item["price"], item["recordedAt"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)

    normalized = deduplicated
    current_price = parse_price(current_price)

    if current_price is None:
        return normalized

    latest_price = normalized[-1].get("price") if normalized else None

    if latest_price != current_price:
        normalized.append({"price": current_price, "recordedAt": recorded_at})

    return normalized

def add_price_history(property_data, new_price, fetched_at):
    new_price = parse_price(new_price)
    history = normalize_price_history(property_data.get("priceHistory"), None, fetched_at)

    if new_price is None:
        property_data["priceHistory"] = history
        return

    previous_price = parse_price(property_data.get("price"))
    property_data["priceHistory"] = normalize_price_history(history, new_price, fetched_at)
    property_data["price"] = new_price

    if previous_price is not None and previous_price != new_price:
        logger.info("価格変更を記録 (ID: %s): %s -> %s", property_data.get("id"), previous_price, new_price)

# ============================================================
# 設定・アダプター
# ============================================================

def load_config():
    return load_json(ROOT / "config" / "search.json", default={})

def load_sources():
    return load_json(ROOT / "config" / "sources.json", default={"sources": []})

def get_suumo_source_config():
    sources = load_sources()
    source_list = sources.get("sources", [])

    if not isinstance(source_list, list):
        return {}

    for source in source_list:
        if isinstance(source, dict) and source.get("name") == "suumo_search":
            return source

    return {}

def create_adapters():
    sources = load_sources()
    adapters = []
    source_list = sources.get("sources", [])

    if not isinstance(source_list, list):
        return adapters

    for source in source_list:
        if isinstance(source, dict) and source.get("enabled") and source.get("name") == "suumo_search":
            adapters.append(SuumoSearchAdapter(config=source, root_path=ROOT))

    return adapters

def create_detail_adapter():
    source_config = get_suumo_source_config()
    return SuumoDetailAdapter(config=source_config, root_path=ROOT)

def get_detail_fetch_limit(search_config):
    source_config = get_suumo_source_config()
    configured_limit = source_config.get("detailFetchLimit")

    if configured_limit is None:
        configured_limit = search_config.get("detailFetchLimit", DEFAULT_DETAIL_FETCH_LIMIT)

    try:
        limit = int(configured_limit)
    except (TypeError, ValueError):
        limit = DEFAULT_DETAIL_FETCH_LIMIT

    return max(0, limit)

# ============================================================
# 物件ID・URL処理
# ============================================================

def normalize_url(url):
    if not url:
        return ""
    return str(url).split("?")[0].split("#")[0].rstrip("/")

def create_property_id(url):
    normalized_url = normalize_url(url)
    if not normalized_url:
        return None

    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"suumo-{digest}"

# ============================================================
# 物件データ初期化 (検索結果価格のパース・保持を強化)
# ============================================================

def normalize_property(item, collected_at):
    if not isinstance(item, dict):
        return None

    url = item.get("sourceUrl", "")
    property_id = create_property_id(url)

    if not property_id:
        return None

    raw_price = item.get("price") or item.get("priceText") or item.get("priceValue")
    parsed_search_price = parse_price(raw_price)

    return {
        "id": property_id,
        "source": item.get("source", "suumo"),
        "sourceUrl": url,
        "searchArea": item.get("searchArea"),
        "searchPropertyType": item.get("searchPropertyType"),
        "status": "discovered",
        "detailFetched": False,
        "detailFetchedAt": None,
        "detailFetchError": None,
        "fetchAttemptCount": 0,
        "lastFetchAttemptAt": None,
        "lastFetchParserVersion": None,
        "priceChanged": False,
        "firstSeenAt": collected_at,
        "lastSeenAt": collected_at,
        "collectedAt": collected_at,
        "price": parsed_search_price,
        "priceText": clean_text(raw_price) if raw_price else None,
        "priceHistory": [{"price": parsed_search_price, "recordedAt": collected_at}] if parsed_search_price else [],
        "detailParserVersion": None,
        "detailQuality": "unknown",
        "detailQualityScore": None,
        "missingFields": [],
        "validationWarnings": [],
        "extractionQuality": {},
        "detail": {},
    }

# ============================================================
# 詳細データの異常判定 & 品質計算
# ============================================================

def validate_detail_data(detail):
    warnings = []

    if not isinstance(detail, dict):
        return ["detailが辞書形式ではありません"]

    address = detail.get("address")
    if not address:
        warnings.append("住所が取得できていません")
    elif is_suspicious_address(address):
        warnings.append("住所が仲介会社住所または不正値の可能性があります")

    construction_month = detail.get("constructionMonth")
    build_year = detail.get("buildYear")

    if construction_month:
        if not is_valid_year_month(str(construction_month)):
            warnings.append("constructionMonthがYYYY-MM形式ではありません")
    elif build_year:
        build_year_text = clean_text(build_year)
        if not re.search(r"\d{4}年\d{1,2}月", build_year_text):
            warnings.append("築年月の形式を確認できません")
    else:
        warnings.append("築年月が取得できていません")

    station = detail.get("station")
    if station is not None and is_suspicious_station(station):
        warnings.append("駅名が不正値の可能性があります")

    price = parse_price(detail.get("price") or detail.get("priceText"))
    if price is None:
        warnings.append("価格が取得できていません")

    land_area = parse_float(detail.get("landAreaM2") or detail.get("landArea"))
    if land_area is None:
        warnings.append("土地面積が取得できていません")

    building_area = parse_float(detail.get("buildingAreaM2") or detail.get("buildingArea"))
    if building_area is None:
        warnings.append("建物面積が取得できていません")

    unique_warnings = []
    for warning in warnings:
        if warning not in unique_warnings:
            unique_warnings.append(warning)

    return unique_warnings

def determine_detail_quality(detail):
    critical_fields = {
        "price": detail.get("price") or detail.get("priceText"),
        "address": detail.get("address"),
        "constructionMonth": detail.get("constructionMonth") or detail.get("buildYear"),
    }

    important_fields = {
        "landAreaM2": detail.get("landAreaM2") or detail.get("landArea"),
        "buildingAreaM2": detail.get("buildingAreaM2") or detail.get("buildingArea"),
        "layout": detail.get("layout"),
        "station": detail.get("station"),
    }

    missing_critical = [f for f, v in critical_fields.items() if v is None or v == ""]
    missing_important = [f for f, v in important_fields.items() if v is None or v == ""]
    missing_fields = missing_critical + missing_important

    warnings = validate_detail_data(detail)

    critical_warning_words = [
        "住所が仲介会社住所",
        "住所が取得できていません",
        "築年月の形式",
        "築年月が取得",
        "価格が取得",
        "detailが辞書",
        "駅名が不正",
    ]

    has_critical_warning = any(
        any(word in warning for word in critical_warning_words)
        for warning in warnings
    )

    if missing_critical or has_critical_warning:
        quality = "poor"
    elif missing_important or warnings:
        quality = "partial"
    else:
        quality = "good"

    score = {"good": 100, "partial": 70, "poor": 30}.get(quality, 0)

    return quality, score, warnings, missing_fields

# ============================================================
# 詳細情報の正規化
# ============================================================

def normalize_detail(detail):
    if not isinstance(detail, dict):
        return {}

    normalized = detail.copy()

    # 1. 土地面積
    land_area_raw = normalized.get("landAreaM2") or normalized.get("landArea")
    normalized_land_area = parse_float(land_area_raw)
    if normalized_land_area is not None:
        normalized["landAreaM2"] = normalized_land_area

    land_text = clean_text(normalized.get("landAreaText") or normalized.get("landArea"))
    if land_text and not is_promotional_text(land_text) and land_text not in INVALID_VALUES:
        normalized["landAreaText"] = land_text
    elif normalized_land_area is not None:
        normalized["landAreaText"] = f"{normalized_land_area}m²"

    # 2. 建物面積
    building_area_raw = normalized.get("buildingAreaM2") or normalized.get("buildingArea")
    normalized_building_area = parse_float(building_area_raw)
    if normalized_building_area is not None:
        normalized["buildingAreaM2"] = normalized_building_area

    bld_text = clean_text(normalized.get("buildingAreaText") or normalized.get("buildingArea"))
    if bld_text and not is_promotional_text(bld_text) and bld_text not in INVALID_VALUES:
        normalized["buildingAreaText"] = bld_text
    elif normalized_building_area is not None:
        normalized["buildingAreaText"] = f"{normalized_building_area}m²"

    # 3. 旧キー削除
    normalized.pop("landArea", None)
    normalized.pop("buildingArea", None)

    # 4. 価格
    raw_price = normalized.get("price") or normalized.get("priceText")
    normalized_price = parse_price(raw_price)
    if normalized_price is not None:
        normalized["price"] = normalized_price

    # 5. 築年月
    construction_month = normalized.get("constructionMonth")
    if construction_month:
        construction_month_text = clean_text(construction_month)
        if not is_valid_year_month(construction_month_text):
            year_month_match = re.search(r"((?:19|20)\d{2})年(\d{1,2})月", construction_month_text)
            if year_month_match:
                year = year_month_match.group(1)
                month = int(year_month_match.group(2))
                normalized["constructionMonth"] = f"{year}-{month:02d}"

    if not normalized.get("constructionMonth"):
        build_year = normalized.get("buildYear")
        if build_year:
            build_year_text = clean_text(build_year)
            year_month_match = re.search(r"((?:19|20)\d{2})年(\d{1,2})月", build_year_text)
            if year_month_match:
                year = year_month_match.group(1)
                month = int(year_month_match.group(2))
                normalized["constructionMonth"] = f"{year}-{month:02d}"
                normalized["constructionText"] = build_year_text

    # 6. 駅情報
    if not normalized.get("station"):
        station = normalized.get("stationText")
        if station:
            normalized["station"] = clean_text(station)

    if normalized.get("station") and is_suspicious_station(normalized.get("station")):
        normalized["station"] = None

    if not normalized.get("stationWalkMinutes"):
        walking_minutes = normalized.get("walkingMinutes")
        if walking_minutes is not None:
            normalized["stationWalkMinutes"] = safe_int(walking_minutes)

    if normalized.get("walkMinutes") is None:
        station_walk_minutes = normalized.get("stationWalkMinutes")
        if station_walk_minutes is not None:
            normalized["walkMinutes"] = safe_int(station_walk_minutes)

    if normalized.get("walkMinutes") is not None:
        normalized["walkMinutes"] = safe_int(normalized.get("walkMinutes"))

    # 7. 住所
    if normalized.get("address"):
        address = str(normalized["address"])
        address = re.sub(r"\s*[\[［].*?[\]］]", "", address)
        address = re.sub(r"\s*[\[［].*$", "", address)
        address = re.sub(r"\s*(地図を見る|周辺環境|詳細を見る|お気に入り).*$", "", address)
        normalized["address"] = clean_text(address)

    # 8. labelValuePairs 内の「所在地」クレンジング
    label_value_pairs = normalized.get("labelValuePairs")
    if isinstance(label_value_pairs, dict):
        pairs = label_value_pairs.copy()
        if pairs.get("所在地"):
            location = clean_text(pairs["所在地"])
            location = re.sub(r"\s*(地図を見る|周辺環境|詳細を見る|お気に入り).*$", "", location)
            pairs["所在地"] = location
        normalized["labelValuePairs"] = pairs

    # 9. リスト項目
    normalized["missingFields"] = normalize_string_list(normalized.get("missingFields"))
    normalized["validationWarnings"] = normalize_warning_list(normalized.get("validationWarnings"))
    normalized["detailParserVersion"] = DETAIL_PARSER_VERSION

    return normalized

# ============================================================
# 既存物件の読み込みと正規化
# ============================================================

def normalize_existing_detail(property_data):
    if not isinstance(property_data, dict):
        return property_data

    detail = property_data.get("detail")
    if not isinstance(detail, dict):
        detail = {}

    original_root_version = property_data.get("detailParserVersion")
    original_detail_version = detail.get("detailParserVersion")

    detail = normalize_detail(detail)

    quality, score, warnings, missing_fields = determine_detail_quality(detail)

    detail["detailQuality"] = quality
    detail["detailQualityScore"] = score
    detail["validationWarnings"] = warnings
    detail["missingFields"] = missing_fields

    property_data["detail"] = detail
    property_data["detailQuality"] = quality
    property_data["detailQualityScore"] = score
    property_data["validationWarnings"] = warnings
    property_data["missingFields"] = missing_fields

    detail_price = parse_price(detail.get("price") or detail.get("priceText"))
    root_price = parse_price(property_data.get("price"))
    if detail_price is not None:
        property_data["price"] = detail_price
    elif root_price is not None:
        property_data["price"] = root_price
    else:
        property_data["price"] = None

    if (
        original_root_version == DETAIL_PARSER_VERSION
        and original_detail_version == DETAIL_PARSER_VERSION
    ):
        property_data["detailParserVersion"] = DETAIL_PARSER_VERSION

    property_data["priceHistory"] = normalize_price_history(
        property_data.get("priceHistory"),
        property_data.get("price"),
        property_data.get("detailFetchedAt") or now_iso(),
    )

    return property_data

def load_existing_properties():
    path = ROOT / "data" / "discovered_listings.json"
    data = load_json(path, default={})

    if not isinstance(data, dict):
        return {}

    properties = data.get("properties", [])
    if not isinstance(properties, list):
        return {}

    result = {}

    for property_data in properties:
        if not isinstance(property_data, dict):
            continue

        property_id = property_data.get("id") or create_property_id(property_data.get("sourceUrl", ""))
        if not property_id:
            continue

        property_data["id"] = property_id
        property_data.setdefault("detailFetched", False)
        property_data.setdefault("detailFetchedAt", None)
        property_data.setdefault("detailFetchError", None)
        property_data.setdefault("fetchAttemptCount", 0)
        property_data.setdefault("lastFetchAttemptAt", None)
        property_data.setdefault("lastFetchParserVersion", None)
        property_data.setdefault("priceChanged", False)
        property_data.setdefault("price", None)
        property_data.setdefault("priceText", None)
        property_data.setdefault("priceHistory", [])
        property_data.setdefault("detailParserVersion", None)
        property_data.setdefault("detailQuality", "unknown")
        property_data.setdefault("detailQualityScore", None)
        property_data.setdefault("missingFields", [])
        property_data.setdefault("validationWarnings", [])
        property_data.setdefault("extractionQuality", {})

        if not isinstance(property_data.get("detail"), dict):
            property_data["detail"] = {}

        if not isinstance(property_data.get("priceHistory"), list):
            property_data["priceHistory"] = []

        property_data = normalize_existing_detail(property_data)
        result[property_id] = property_data

    return result

# ============================================================
# 物件データ統合 (検索価格差分チェックと priceChanged 設定)
# ============================================================

def merge_property(existing, current, collected_at):
    if existing is None:
        return current

    merged = existing.copy()

    if current.get("sourceUrl"):
        merged["sourceUrl"] = current["sourceUrl"]

    if current.get("source"):
        merged["source"] = current["source"]

    if current.get("searchArea"):
        merged["searchArea"] = current["searchArea"]

    if current.get("searchPropertyType"):
        merged["searchPropertyType"] = current["searchPropertyType"]

    # 検索一覧の価格変化検知
    existing_price = parse_price(merged.get("price"))
    current_search_price = parse_price(current.get("price"))

    if current_search_price is not None and existing_price is not None:
        if current_search_price != existing_price:
            logger.info(
                "検索一覧での価格差分を検知 (ID: %s): %s -> %s",
                merged.get("id"),
                existing_price,
                current_search_price,
            )
            merged["priceChanged"] = True
            add_price_history(merged, current_search_price, collected_at)

    if not merged.get("firstSeenAt"):
        merged["firstSeenAt"] = current.get("firstSeenAt", collected_at)

    merged["lastSeenAt"] = collected_at
    merged["collectedAt"] = collected_at

    if not merged.get("status"):
        merged["status"] = "discovered"

    merged.setdefault("detailFetched", False)
    merged.setdefault("detailFetchedAt", None)
    merged.setdefault("detailFetchError", None)
    merged.setdefault("fetchAttemptCount", 0)
    merged.setdefault("lastFetchAttemptAt", None)
    merged.setdefault("lastFetchParserVersion", None)
    merged.setdefault("priceChanged", False)
    merged.setdefault("price", None)
    merged.setdefault("priceText", None)
    merged.setdefault("priceHistory", [])
    merged.setdefault("detailParserVersion", None)
    merged.setdefault("detailQuality", "unknown")
    merged.setdefault("detailQualityScore", None)
    merged.setdefault("missingFields", [])
    merged.setdefault("validationWarnings", [])
    merged.setdefault("extractionQuality", {})

    if not isinstance(merged.get("priceHistory"), list):
        merged["priceHistory"] = []

    if not isinstance(merged.get("detail"), dict):
        merged["detail"] = {}

    return merged

def merge_properties(existing_properties, current_properties, collected_at):
    if not isinstance(existing_properties, dict):
        existing_properties = {}

    merged_properties = existing_properties.copy()

    if isinstance(current_properties, dict):
        property_items = current_properties.values()
    elif isinstance(current_properties, list):
        property_items = current_properties
    else:
        logger.warning("物件データの形式が不正です")
        return merged_properties

    for current in property_items:
        if not isinstance(current, dict):
            continue

        property_id = current.get("id")
        if not property_id:
            continue

        existing = merged_properties.get(property_id)
        merged_properties[property_id] = merge_property(existing, current, collected_at)

    return merged_properties

# ============================================================
# 詳細情報取得結果の反映 (priceChanged クリア追加)
# ============================================================

def apply_detail_to_property(property_data, detail, fetched_at):
    detail = normalize_detail(detail)

    existing_detail = property_data.get("detail")
    if not isinstance(existing_detail, dict):
        existing_detail = {}

    replace_fields = [
        "title",
        "address",
        "landAreaM2",
        "landAreaText",
        "buildingAreaM2",
        "buildingAreaText",
        "buildingAreaType",
        "layout",
        "layoutRaw",
        "constructionMonth",
        "constructionMonthPrecision",
        "constructionText",
        "station",
        "stationAccessType",
        "stationWalkMinutes",
        "busMinutes",
        "busStop",
        "busStopWalkMinutes",
        "walkMinutes",
        "transportRaw",
        "informationDate",
        "nextUpdateDate",
    ]

    for field in replace_fields:
        existing_detail.pop(field, None)

    existing_detail.update(detail)

    new_price = parse_price(detail.get("price")) or parse_price(detail.get("priceText"))
    if detail.get("priceText"):
        property_data["priceText"] = str(detail.get("priceText"))

    add_price_history(property_data, new_price, fetched_at)

    quality, score, warnings, missing_fields = determine_detail_quality(existing_detail)

    existing_detail["detailQuality"] = quality
    existing_detail["detailQualityScore"] = score
    existing_detail["validationWarnings"] = warnings
    existing_detail["missingFields"] = missing_fields

    property_data["detail"] = existing_detail

    copy_fields = [
        "title",
        "address",
        "landAreaM2",
        "landAreaText",
        "buildingAreaM2",
        "buildingAreaText",
        "buildingAreaType",
        "layout",
        "layoutRaw",
        "constructionMonth",
        "constructionMonthPrecision",
        "constructionText",
        "buildYear",
        "builtYear",
        "builtMonth",
        "builtYearText",
        "station",
        "stationText",
        "stationAccessType",
        "stationWalkMinutes",
        "busMinutes",
        "busStop",
        "busStopWalkMinutes",
        "walkMinutes",
        "walkingMinutes",
        "transportRaw",
        "builder",
        "structure",
        "informationDate",
        "nextUpdateDate",
        "detailParserVersion",
        "extractionQuality",
    ]

    for field in copy_fields:
        if field in existing_detail:
            property_data[field] = existing_detail.get(field)

    property_data["detailQuality"] = quality
    property_data["detailQualityScore"] = score
    property_data["validationWarnings"] = warnings
    property_data["missingFields"] = missing_fields
    property_data["detailParserVersion"] = DETAIL_PARSER_VERSION

    property_data["detailFetched"] = True
    # 詳細再取得が成功したためフラグをクリア
    property_data["priceChanged"] = False

    logger.info("詳細品質判定: quality=%s score=%s warnings=%s", quality, score, len(warnings))

# ============================================================
# 詳細取得対象判定 (価格変更フラグの優先)
# ============================================================

def should_fetch_detail(property_data):
    if not isinstance(property_data, dict):
        return False

    if not property_data.get("sourceUrl"):
        return False

    detail = property_data.get("detail")
    if not isinstance(detail, dict):
        detail = {}

    attempt_count = safe_int(property_data.get("fetchAttemptCount")) or 0
    last_fetch_parser_version = property_data.get("lastFetchParserVersion")

    # 1. 価格変動が検知されている場合は最優先で再取得
    if property_data.get("priceChanged"):
        return True

    # 2. 現行パーサーで上限回数 (3回) 試行済みの場合はスキップ
    if last_fetch_parser_version == DETAIL_PARSER_VERSION and attempt_count >= MAX_DETAIL_FETCH_ATTEMPTS:
        return False

    # 3. パーサーバージョンが古い場合は優先して再取得
    root_version = property_data.get("detailParserVersion")
    detail_version = detail.get("detailParserVersion")
    if root_version != DETAIL_PARSER_VERSION or detail_version != DETAIL_PARSER_VERSION:
        return True

    # 4. 未取得または品質不良の場合
    if not property_data.get("detailFetched"):
        return True

    quality = property_data.get("detailQuality")
    if quality in (None, "", "unknown", "poor"):
        return True

    warnings = normalize_warning_list(property_data.get("validationWarnings"))
    if warnings:
        return True

    return False

# ============================================================
# 詳細情報取得
# ============================================================

def fetch_details(properties, detail_adapter, max_count):
    fetched_count = 0
    success_count = 0
    error_count = 0

    if max_count <= 0:
        logger.info("詳細取得上限が0のため、詳細取得をスキップします")
        return properties

    candidates = []
    for property_id, property_data in properties.items():
        if not isinstance(property_data, dict):
            continue

        if should_fetch_detail(property_data):
            candidates.append((property_id, property_data))

    # ソート: 1. priceChanged 優先, 2. 試行回数少優先, 3. 直近確認日優先
    candidates.sort(
        key=lambda item: (
            0 if item[1].get("priceChanged") else 1,
            item[1].get("fetchAttemptCount", 0),
            -safe_timestamp(item[1].get("lastSeenAt")),
        )
    )

    logger.info("詳細再取得対象: %s件", len(candidates))

    for property_id, property_data in candidates:
        if fetched_count >= max_count:
            break

        url = property_data.get("sourceUrl")
        if not url:
            continue

        logger.info("詳細情報取得開始: %s %s", property_id, url)
        fetched_count += 1
        fetched_at = now_iso()

        last_fetch_parser_version = property_data.get("lastFetchParserVersion")
        if last_fetch_parser_version != DETAIL_PARSER_VERSION:
            property_data["fetchAttemptCount"] = 0
            property_data["lastFetchParserVersion"] = DETAIL_PARSER_VERSION

        property_data["fetchAttemptCount"] = property_data.get("fetchAttemptCount", 0) + 1
        property_data["lastFetchAttemptAt"] = fetched_at

        try:
            result = detail_adapter.fetch_detail(url)
        except Exception as error:
            logger.exception("詳細情報取得中に例外発生: %s", property_id)
            property_data["detailFetched"] = False
            property_data["detailFetchedAt"] = fetched_at
            property_data["detailFetchError"] = str(error)
            error_count += 1
            try:
                detail_adapter.wait()
            except Exception:
                pass
            continue

        if not isinstance(result, dict):
            result = {"success": False, "error": "詳細取得結果が辞書形式ではありません"}

        result_fetched_at = result.get("fetchedAt") or fetched_at
        property_data["detailFetchedAt"] = result_fetched_at

        if result.get("success"):
            fetched_detail = result.get("detail", {})
            if not isinstance(fetched_detail, dict):
                fetched_detail = {}

            apply_detail_to_property(property_data, fetched_detail, result_fetched_at)
            property_data["detailFetchError"] = None
            success_count += 1

            logger.info(
                "詳細情報取得成功: %s quality=%s score=%s",
                property_id,
                property_data.get("detailQuality"),
                property_data.get("detailQualityScore"),
            )
        else:
            error_message = result.get("error", "Unknown error")
            property_data["detailFetchError"] = str(error_message)
            property_data["detailFetched"] = False
            error_count += 1
            logger.warning("詳細情報取得失敗: %s %s", property_id, error_message)

        try:
            detail_adapter.wait()
        except Exception as error:
            logger.warning("待機処理に失敗しました: %s", error)

    logger.info(
        "詳細取得結果: 処理=%s件 / 成功=%s件 / 失敗=%s件",
        fetched_count,
        success_count,
        error_count,
    )

    return properties

# ============================================================
# 出力データ作成 (保存先のデータ構造抽出)
# ============================================================

def build_output(properties, collected_at, filter_fetched_only=False):
    property_list = list(properties.values())
    
    if filter_fetched_only:
        # houses.json 用: 詳細取得済みの物件を中心に抽出
        property_list = [item for item in property_list if item.get("detailFetched")]

    property_list.sort(
        key=lambda item: (item.get("lastSeenAt", ""), item.get("id", "")),
        reverse=True,
    )

    detail_fetched_count = sum(1 for item in property_list if item.get("detailFetched"))
    detail_error_count = sum(1 for item in property_list if item.get("detailFetchError"))

    quality_counts_all = {"good": 0, "partial": 0, "poor": 0, "unknown": 0}
    quality_counts_fetched = {"good": 0, "partial": 0, "poor": 0}

    for item in property_list:
        quality = item.get("detailQuality", "unknown")

        if quality in quality_counts_all:
            quality_counts_all[quality] += 1
        else:
            quality_counts_all["unknown"] += 1

        if item.get("detailFetched"):
            if quality in quality_counts_fetched:
                quality_counts_fetched[quality] += 1

    return {
        "updatedAt": collected_at,
        "summary": {
            "discoveredCount": len(property_list),
            "detailFetchedCount": detail_fetched_count,
            "detailErrorCount": detail_error_count,
            "detailQualityCounts": {
                "all": quality_counts_all,
                "fetched": quality_counts_fetched,
            },
        },
        "properties": property_list,
    }

# ============================================================
# メイン処理
# ============================================================

def main():
    search_config = load_config()

    if not isinstance(search_config, dict):
        logger.warning("search.jsonの形式が不正です。空の設定として処理します")
        search_config = {}

    collected_at = now_iso()
    current_properties = []

    # 1. SUUMO検索
    for adapter in create_adapters():
        try:
            results = adapter.search(search_config)
        except Exception as error:
            logger.exception("検索処理に失敗しました: %s", error)
            continue

        if not isinstance(results, list):
            logger.warning("検索結果がリスト形式ではありません")
            continue

        for item in results:
            normalized = normalize_property(item, collected_at)
            if normalized is not None:
                current_properties.append(normalized)

    # 2. 今回の検出結果をID単位で重複排除
    current_unique = {}
    for property_data in current_properties:
        if not isinstance(property_data, dict):
            continue

        property_id = property_data.get("id")
        if property_id:
            current_unique[property_id] = property_data

    # 3. 既存物件を読み込み
    existing_properties = load_existing_properties()

    # 4. 既存データと今回の結果を統合
    merged_properties = merge_properties(existing_properties, current_unique, collected_at)

    # 5. 詳細情報取得上限
    max_detail_count = get_detail_fetch_limit(search_config)

    # 6. 詳細情報取得
    if max_detail_count > 0:
        detail_adapter = create_detail_adapter()
        merged_properties = fetch_details(merged_properties, detail_adapter, max_detail_count)
    else:
        logger.info("詳細取得上限が0のため、アダプター作成をスキップします")

    # 7. discovered_listings.json (全検索発見データ) の保存
    discovered_output = build_output(merged_properties, collected_at, filter_fetched_only=False)
    save_json(ROOT / "data" / "discovered_listings.json", discovered_output)

    # 8. houses.json (詳細取得済み評価対象データ) の分離保存
    houses_output = build_output(merged_properties, collected_at, filter_fetched_only=True)
    save_json(ROOT / "data" / "houses.json", houses_output)

    # 9. 実行結果表示
    logger.info("今回の検出物件数: %s", len(current_unique))
    logger.info("全発見物件総数 (discovered_listings.json): %s", len(merged_properties))
    logger.info("詳細取得済み総数 (houses.json): %s", houses_output["summary"]["discoveredCount"])
    logger.info("詳細取得上限: %s", max_detail_count)
    logger.info("詳細取得済み累積件数: %s", discovered_output["summary"]["detailFetchedCount"])
    logger.info("詳細取得エラー件数: %s", discovered_output["summary"]["detailErrorCount"])

if __name__ == "__main__":
    main()
