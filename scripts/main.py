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

DETAIL_PARSER_VERSION = "2026-09-21-v12"

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
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def safe_int(value):
    if value is None or isinstance(value, bool):
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
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value > 0 else None

    if isinstance(value, float):
        price = int(value)
        return price if price > 0 else None

    text = (
        str(value)
        .strip()
        .replace(",", "")
        .replace(" ", "")
        .replace("　", "")
    )

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

        price = int(
            round(
                oku * 100_000_000
                + man * 10_000
            )
        )

        return price if price > 0 else None

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*万(?:円)?",
        text
    )

    if match:
        price = int(
            round(
                float(match.group(1)) * 10_000
            )
        )

        return price if price > 0 else None

    match = re.search(
        r"(\d[\d\s]*)\s*円",
        text
    )

    if match:
        try:
            price = int(
                match.group(1).replace(" ", "")
            )

            return price if price > 0 else None

        except ValueError:
            return None

    return None


def parse_float(value):
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None

    text = (
        str(value)
        .replace(",", "")
        .replace("　", " ")
        .replace("m 2", "m2")
        .replace("m²", "m2")
        .replace("㎡", "m2")
    )

    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)",
        text
    )

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

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def is_promotional_text(value):
    text = clean_text(value)

    if not text:
        return True

    return any(
        word in text
        for word in PROMOTIONAL_WORDS
    )


def is_valid_year_month(value):
    if not isinstance(value, str):
        return False

    return bool(
        re.fullmatch(
            r"\d{4}-(0[1-9]|1[0-2])",
            value.strip()
        )
    )


def is_suspicious_station(value):
    if value is None:
        return True

    text = clean_text(value)

    if not text:
        return True

    suspicious_values = {
        "徒",
        "歩",
        "徒歩",
        "駅",
        "ヒント",
        "なし",
        "null",
        "none",
    }

    if (
        text.lower() in suspicious_values
        or len(text) > 15
    ):
        return True

    promotional_words = [
        "見学",
        "お迎え",
        "提案",
        "案内",
        "ローン",
        "頭金",
        "月々",
        "物件",
    ]

    return any(
        word in text
        for word in promotional_words
    )


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

    return any(
        word in text
        for word in suspicious_words
    )


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
# 築年数判定
# ============================================================

def get_build_year(detail):
    """
    詳細データから建築年を取得する。

    優先順位:
    1. constructionMonth
    2. buildYear
    """

    if not isinstance(detail, dict):
        return None

    construction_month = clean_text(
        detail.get("constructionMonth")
    )

    if construction_month:
        match = re.search(
            r"((?:19|20)\d{2})",
            construction_month
        )

        if match:
            return safe_int(
                match.group(1)
            )

    build_year = clean_text(
        detail.get("buildYear")
    )

    if build_year:
        match = re.search(
            r"((?:19|20)\d{2})",
            build_year
        )

        if match:
            return safe_int(
                match.group(1)
            )

    return None


def get_min_built_year(search_config):
    """
    築年数条件から最低建築年を算出する。

    優先順位:
    1. minBuiltYear
    2. maxBuiltAgeYears

    例:
        maxBuiltAgeYears = 20
        現在年 = 2026

        → 2006年以降
    """

    if not isinstance(search_config, dict):
        return None

    explicit_year = safe_int(
        search_config.get("minBuiltYear")
    )

    if explicit_year is not None:
        return explicit_year

    max_age = safe_int(
        search_config.get("maxBuiltAgeYears")
    )

    if max_age is None or max_age < 0:
        return None

    current_year = datetime.now(
        timezone.utc
    ).year

    return current_year - max_age


def evaluate_built_year(
    property_data,
    search_config
):
    """
    築年数条件への適合状況を返す。

    戻り値:
        True  = 条件内
        False = 条件外
        None  = 判定不能
    """

    min_built_year = get_min_built_year(
        search_config
    )

    if min_built_year is None:
        return True

    detail = property_data.get(
        "detail"
    )

    build_year = get_build_year(
        detail
    )

    if build_year is None:
        return None

    return build_year >= min_built_year


def apply_search_criteria(
    properties,
    search_config
):
    """
    検索条件を詳細情報取得後に適用する。

    築年数条件については、詳細ページから
    建築年を取得した上で判定する。
    """

    if not isinstance(properties, dict):
        return properties

    min_built_year = get_min_built_year(
        search_config
    )

    if min_built_year is None:
        return properties

    excluded_count = 0
    unknown_count = 0

    for property_data in properties.values():

        if not isinstance(
            property_data,
            dict
        ):
            continue

        result = evaluate_built_year(
            property_data,
            search_config
        )

        if result is True:

            property_data[
                "searchCriteriaMatched"
            ] = True

            property_data[
                "searchCriteria"
            ] = {
                "minBuiltYear": min_built_year
            }

        elif result is False:

            property_data[
                "searchCriteriaMatched"
            ] = False

            property_data[
                "searchCriteriaMismatchReason"
            ] = (
                f"築年数条件外: "
                f"{get_build_year(property_data.get('detail'))}"
                f"年 < {min_built_year}年"
            )

            excluded_count += 1

        else:

            property_data[
                "searchCriteriaMatched"
            ] = None

            property_data[
                "searchCriteriaMismatchReason"
            ] = (
                "築年月を取得できないため"
                "築年数条件を判定できません"
            )

            unknown_count += 1

    logger.info(
        "築年数フィルタ: 基準=%s年以降 / "
        "条件外=%s件 / 判定不能=%s件",
        min_built_year,
        excluded_count,
        unknown_count,
    )

    return properties


# ============================================================
# 価格履歴
# ============================================================

def normalize_price_history(
    history,
    current_price,
    recorded_at,
    source="detail"
):
    if not isinstance(history, list):
        history = []

    normalized = []

    for item in history:

        if not isinstance(item, dict):
            continue

        price = parse_price(
            item.get("price")
        )

        if price is None:
            continue

        recorded_time = (
            item.get("recordedAt")
            or recorded_at
        )

        item_source = (
            item.get("source")
            or source
        )

        normalized.append({
            "price": price,
            "recordedAt": recorded_time,
            "source": item_source,
        })

    deduplicated = []
    seen = set()

    for item in normalized:

        key = (
            item["price"],
            item["recordedAt"],
            item["source"],
        )

        if key in seen:
            continue

        seen.add(key)
        deduplicated.append(item)

    deduplicated.sort(
        key=lambda item:
        safe_timestamp(
            item.get("recordedAt")
        )
    )

    current_price = parse_price(
        current_price
    )

    if current_price is None:
        return deduplicated

    latest_price = (
        deduplicated[-1].get("price")
        if deduplicated
        else None
    )

    if latest_price != current_price:

        deduplicated.append({
            "price": current_price,
            "recordedAt": recorded_at,
            "source": source,
        })

        deduplicated.sort(
            key=lambda item:
            safe_timestamp(
                item.get("recordedAt")
            )
        )

    return deduplicated


# ============================================================
# 設定・アダプター
# ============================================================

def load_config():
    return load_json(
        ROOT / "config" / "search.json",
        default={}
    )


def load_sources():
    return load_json(
        ROOT / "config" / "sources.json",
        default={"sources": []}
    )


def get_suumo_source_config():
    sources = load_sources()

    source_list = sources.get(
        "sources",
        []
    )

    if not isinstance(
        source_list,
        list
    ):
        return {}

    for source in source_list:

        if (
            isinstance(source, dict)
            and source.get("name")
            == "suumo_search"
        ):
            return source

    return {}


def create_adapters():
    sources = load_sources()

    adapters = []

    source_list = sources.get(
        "sources",
        []
    )

    if not isinstance(
        source_list,
        list
    ):
        return adapters

    for source in source_list:

        if (
            isinstance(source, dict)
            and source.get("enabled")
            and source.get("name")
            == "suumo_search"
        ):
            adapters.append(
                SuumoSearchAdapter(
                    config=source,
                    root_path=ROOT
                )
            )

    return adapters


def create_detail_adapter():
    source_config = (
        get_suumo_source_config()
    )

    return SuumoDetailAdapter(
        config=source_config,
        root_path=ROOT
    )


def get_detail_fetch_limit(
    search_config
):
    source_config = (
        get_suumo_source_config()
    )

    configured_limit = (
        source_config.get(
            "detailFetchLimit"
        )
    )

    if configured_limit is None:
        configured_limit = (
            search_config.get(
                "detailFetchLimit",
                DEFAULT_DETAIL_FETCH_LIMIT
            )
        )

    try:
        limit = int(
            configured_limit
        )

    except (
        TypeError,
        ValueError
    ):
        limit = DEFAULT_DETAIL_FETCH_LIMIT

    return max(0, limit)


# ============================================================
# 物件ID・URL処理
# ============================================================

def normalize_url(url):
    if not url:
        return ""

    return (
        str(url)
        .split("?")[0]
        .split("#")[0]
        .rstrip("/")
    )


def create_property_id(url):
    normalized_url = normalize_url(
        url
    )

    if not normalized_url:
        return None

    digest = hashlib.sha256(
        normalized_url.encode("utf-8")
    ).hexdigest()[:16]

    return f"suumo-{digest}"


# ============================================================
# 物件データ初期化
# ============================================================

def normalize_property(
    item,
    collected_at
):
    if not isinstance(item, dict):
        return None

    url = item.get(
        "sourceUrl",
        ""
    )

    property_id = create_property_id(
        url
    )

    if not property_id:
        return None

    raw_price = (
        item.get("price")
        or item.get("priceText")
        or item.get("priceValue")
    )

    parsed_search_price = parse_price(
        raw_price
    )

    return {
        "id": property_id,
        "source": item.get(
            "source",
            "suumo"
        ),
        "sourceUrl": url,
        "searchArea": item.get(
            "searchArea"
        ),
        "searchPropertyType": item.get(
            "searchPropertyType"
        ),
        "status": "discovered",

        "detailFetched": False,
        "detailFetchedAt": None,
        "lastSuccessfulDetailFetchedAt": None,
        "detailDataStale": False,
        "detailNeedsRefresh": True,

        "searchDetailPriceMismatch": False,

        "detailFetchError": None,
        "detailFetchBlocked": False,
        "detailFetchBlockReason": None,

        "fetchAttemptCount": 0,
        "lastFetchAttemptAt": None,
        "lastFetchParserVersion": None,

        "priceChanged": False,

        "firstSeenAt": collected_at,
        "lastSeenAt": collected_at,
        "collectedAt": collected_at,

        "lastSearchPrice": parsed_search_price,
        "lastSearchPriceText": (
            clean_text(raw_price)
            if raw_price
            else None
        ),

        "detailPrice": None,
        "detailPriceText": None,
        "lastSuccessfulDetailPrice": None,

        "price": parsed_search_price,
        "priceText": (
            clean_text(raw_price)
            if raw_price
            else None
        ),

        "priceHistory": (
            [{
                "price": parsed_search_price,
                "recordedAt": collected_at,
                "source": "search",
            }]
            if parsed_search_price
            else []
        ),

        "detailParserVersion": None,

        "detailQuality": "unknown",
        "detailQualityScore": None,

        "missingFields": [],
        "validationWarnings": [],
        "extractionQuality": {},

        "detail": {},
        "lastSuccessfulDetail": {},

        # 検索条件判定
        "searchCriteriaMatched": None,
        "searchCriteriaMismatchReason": None,
        "searchCriteria": {},
    }


# ============================================================
# 詳細データの品質判定 & 正規化
# ============================================================

def validate_detail_data(detail):

    warnings = []

    if not isinstance(detail, dict):
        return [
            "detailが辞書形式ではありません"
        ]

    address = detail.get(
        "address"
    )

    if not address:
        warnings.append(
            "住所が取得できていません"
        )

    elif is_suspicious_address(
        address
    ):
        warnings.append(
            "住所が仲介会社住所または不正値の可能性があります"
        )

    construction_month = detail.get(
        "constructionMonth"
    )

    build_year = detail.get(
        "buildYear"
    )

    if construction_month:

        if not is_valid_year_month(
            str(construction_month)
        ):
            warnings.append(
                "constructionMonthがYYYY-MM形式ではありません"
            )

    elif build_year:

        build_year_text = clean_text(
            build_year
        )

        if not re.search(
            r"\d{4}年\d{1,2}月",
            build_year_text
        ):
            warnings.append(
                "築年月の形式を確認できません"
            )

    else:
        warnings.append(
            "築年月が取得できていません"
        )

    station = detail.get(
        "station"
    )

    if (
        station is not None
        and is_suspicious_station(station)
    ):
        warnings.append(
            "駅名が不正値の可能性があります"
        )

    price = parse_price(
        detail.get("price")
        or detail.get("priceText")
    )

    if price is None:
        warnings.append(
            "価格が取得できていません"
        )

    land_area = parse_float(
        detail.get("landAreaM2")
        or detail.get("landArea")
    )

    if land_area is None:
        warnings.append(
            "土地面積が取得できていません"
        )

    building_area = parse_float(
        detail.get("buildingAreaM2")
        or detail.get("buildingArea")
    )

    if building_area is None:
        warnings.append(
            "建物面積が取得できていません"
        )

    unique_warnings = []

    for warning in warnings:

        if warning not in unique_warnings:
            unique_warnings.append(
                warning
            )

    return unique_warnings


def determine_detail_quality(
    detail,
    detail_fetched=True
):
    if (
        not detail_fetched
        or not isinstance(detail, dict)
    ):
        return (
            "unknown",
            None,
            [],
            []
        )

    critical_fields = {
        "price": (
            detail.get("price")
            or detail.get("priceText")
        ),
        "address": detail.get(
            "address"
        ),
        "constructionMonth": (
            detail.get(
                "constructionMonth"
            )
            or detail.get(
                "buildYear"
            )
        ),
    }

    important_fields = {
        "landAreaM2": (
            detail.get(
                "landAreaM2"
            )
            or detail.get(
                "landArea"
            )
        ),
        "buildingAreaM2": (
            detail.get(
                "buildingAreaM2"
            )
            or detail.get(
                "buildingArea"
            )
        ),
        "layout": detail.get(
            "layout"
        ),
        "station": detail.get(
            "station"
        ),
    }

    missing_critical = [
        f
        for f, v
        in critical_fields.items()
        if v is None or v == ""
    ]

    missing_important = [
        f
        for f, v
        in important_fields.items()
        if v is None or v == ""
    ]

    missing_fields = (
        missing_critical
        + missing_important
    )

    warnings = validate_detail_data(
        detail
    )

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
        any(
            word in warning
            for word in critical_warning_words
        )
        for warning in warnings
    )

    if (
        missing_critical
        or has_critical_warning
    ):
        quality = "poor"

    elif (
        missing_important
        or warnings
    ):
        quality = "partial"

    else:
        quality = "good"

    score = {
        "good": 100,
        "partial": 70,
        "poor": 30,
    }.get(
        quality,
        0
    )

    return (
        quality,
        score,
        warnings,
        missing_fields
    )


def normalize_detail(detail):

    if not isinstance(detail, dict):
        return {}

    normalized = detail.copy()

    # --------------------------------------------------------
    # 土地面積
    # --------------------------------------------------------

    land_area_raw = (
        normalized.get("landAreaM2")
        or normalized.get("landArea")
    )

    normalized_land_area = parse_float(
        land_area_raw
    )

    if normalized_land_area is not None:
        normalized[
            "landAreaM2"
        ] = normalized_land_area

    land_text = clean_text(
        normalized.get(
            "landAreaText"
        )
        or normalized.get(
            "landArea"
        )
    )

    if (
        land_text
        and not is_promotional_text(
            land_text
        )
        and land_text not in INVALID_VALUES
    ):
        normalized[
            "landAreaText"
        ] = land_text

    elif normalized_land_area is not None:
        normalized[
            "landAreaText"
        ] = (
            f"{normalized_land_area}m²"
        )

    # --------------------------------------------------------
    # 建物面積
    # --------------------------------------------------------

    building_area_raw = (
        normalized.get(
            "buildingAreaM2"
        )
        or normalized.get(
            "buildingArea"
        )
    )

    normalized_building_area = parse_float(
        building_area_raw
    )

    if normalized_building_area is not None:
        normalized[
            "buildingAreaM2"
        ] = normalized_building_area

    bld_text = clean_text(
        normalized.get(
            "buildingAreaText"
        )
        or normalized.get(
            "buildingArea"
        )
    )

    if (
        bld_text
        and not is_promotional_text(
            bld_text
        )
        and bld_text not in INVALID_VALUES
    ):
        normalized[
            "buildingAreaText"
        ] = bld_text

    elif normalized_building_area is not None:
        normalized[
            "buildingAreaText"
        ] = (
            f"{normalized_building_area}m²"
        )

    normalized.pop(
        "landArea",
        None
    )

    normalized.pop(
        "buildingArea",
        None
    )

    # --------------------------------------------------------
    # 価格
    # --------------------------------------------------------

    raw_price = (
        normalized.get("price")
        or normalized.get("priceText")
    )

    normalized_price = parse_price(
        raw_price
    )

    if normalized_price is not None:
        normalized[
            "price"
        ] = normalized_price

    # --------------------------------------------------------
    # 築年月
    # --------------------------------------------------------

    construction_month = normalized.get(
        "constructionMonth"
    )

    if construction_month:

        construction_month_text = clean_text(
            construction_month
        )

        if not is_valid_year_month(
            construction_month_text
        ):

            year_month_match = re.search(
                r"((?:19|20)\d{2})年(\d{1,2})月",
                construction_month_text
            )

            if year_month_match:

                year = (
                    year_month_match.group(1)
                )

                month = int(
                    year_month_match.group(2)
                )

                normalized[
                    "constructionMonth"
                ] = (
                    f"{year}-{month:02d}"
                )

    if not normalized.get(
        "constructionMonth"
    ):

        build_year = normalized.get(
            "buildYear"
        )

        if build_year:

            build_year_text = clean_text(
                build_year
            )

            year_month_match = re.search(
                r"((?:19|20)\d{2})年(\d{1,2})月",
                build_year_text
            )

            if year_month_match:

                year = (
                    year_month_match.group(1)
                )

                month = int(
                    year_month_match.group(2)
                )

                normalized[
                    "constructionMonth"
                ] = (
                    f"{year}-{month:02d}"
                )

                normalized[
                    "constructionText"
                ] = build_year_text

    # --------------------------------------------------------
    # 駅情報
    # --------------------------------------------------------

    if not normalized.get(
        "station"
    ):

        station = normalized.get(
            "stationText"
        )

        if station:
            normalized[
                "station"
            ] = clean_text(
                station
            )

    if (
        normalized.get("station")
        and is_suspicious_station(
            normalized.get("station")
        )
    ):
        normalized[
            "station"
        ] = None

    if not normalized.get(
        "stationWalkMinutes"
    ):

        walking_minutes = normalized.get(
            "walkingMinutes"
        )

        if walking_minutes is not None:
            normalized[
                "stationWalkMinutes"
            ] = safe_int(
                walking_minutes
            )

    if normalized.get(
        "walkMinutes"
    ) is None:

        station_walk_minutes = normalized.get(
            "stationWalkMinutes"
        )

        if station_walk_minutes is not None:
            normalized[
                "walkMinutes"
            ] = safe_int(
                station_walk_minutes
            )

    if normalized.get(
        "walkMinutes"
    ) is not None:

        normalized[
            "walkMinutes"
        ] = safe_int(
            normalized.get(
                "walkMinutes"
            )
        )

    # --------------------------------------------------------
    # 住所
    # --------------------------------------------------------

    if normalized.get(
        "address"
    ):

        address = str(
            normalized["address"]
        )

        address = re.sub(
            r"\s*[\[［].*?[\]］]",
            "",
            address
        )

        address = re.sub(
            r"\s*[\[［].*$",
            "",
            address
        )

        address = re.sub(
            r"\s*(地図を見る|周辺環境|詳細を見る|お気に入り).*$",
            "",
            address
        )

        normalized[
            "address"
        ] = clean_text(
            address
        )

    # --------------------------------------------------------
    # 所在地ペア
    # --------------------------------------------------------

    label_value_pairs = normalized.get(
        "labelValuePairs"
    )

    if isinstance(
        label_value_pairs,
        dict
    ):

        pairs = label_value_pairs.copy()

        if pairs.get("所在地"):

            location = clean_text(
                pairs["所在地"]
            )

            location = re.sub(
                r"\s*(地図を見る|周辺環境|詳細を見る|お気に入り).*$",
                "",
                location
            )

            pairs["所在地"] = location

        normalized[
            "labelValuePairs"
        ] = pairs

    normalized[
        "missingFields"
    ] = normalize_string_list(
        normalized.get(
            "missingFields"
        )
    )

    normalized[
        "validationWarnings"
    ] = normalize_warning_list(
        normalized.get(
            "validationWarnings"
        )
    )

    normalized[
        "detailParserVersion"
    ] = DETAIL_PARSER_VERSION

    return normalized


# ============================================================
# 状態判定 & フラグ更新
# ============================================================

def has_usable_detail(
    property_data: dict
) -> bool:

    if not isinstance(
        property_data,
        dict
    ):
        return False

    return bool(
        property_data.get(
            "detailFetched"
        )
        or property_data.get(
            "lastSuccessfulDetailFetchedAt"
        )
    )


def reset_detail_fetch_state_if_parser_updated(
    property_data: dict
) -> None:

    if not isinstance(
        property_data,
        dict
    ):
        return

    last_version = property_data.get(
        "lastFetchParserVersion"
    )

    if (
        last_version
        != DETAIL_PARSER_VERSION
    ):

        property_data[
            "fetchAttemptCount"
        ] = 0

        property_data[
            "detailFetchBlocked"
        ] = False

        property_data[
            "detailFetchBlockReason"
        ] = None

        property_data[
            "lastFetchParserVersion"
        ] = DETAIL_PARSER_VERSION


def should_fetch_detail(
    property_data: dict
) -> bool:

    if not isinstance(
        property_data,
        dict
    ):
        return False

    if property_data.get(
        "detailFetchBlocked"
    ):
        return False

    return bool(
        property_data.get(
            "detailNeedsRefresh"
        )
    )


def update_price_mismatch_and_refresh_flags(
    property_data: dict
) -> None:

    if not isinstance(
        property_data,
        dict
    ):
        return

    search_price = property_data.get(
        "lastSearchPrice"
    )

    detail_price = property_data.get(
        "detailPrice"
    )

    if (
        search_price is not None
        and detail_price is not None
    ):
        property_data[
            "searchDetailPriceMismatch"
        ] = (
            search_price
            != detail_price
        )

    else:
        property_data[
            "searchDetailPriceMismatch"
        ] = False

    parser_version = property_data.get(
        "detailParserVersion"
    )

    is_parser_outdated = (
        parser_version
        != DETAIL_PARSER_VERSION
    )

    never_fetched = not bool(
        property_data.get(
            "lastSuccessfulDetailFetchedAt"
        )
    )

    price_changed = bool(
        property_data.get(
            "priceChanged"
        )
    )

    property_data[
        "detailNeedsRefresh"
    ] = bool(
        price_changed
        or is_parser_outdated
        or never_fetched
    )


# ============================================================
# 既存物件の読み込み
# ============================================================

def load_existing_properties():

    path = (
        ROOT
        / "data"
        / "discovered_listings.json"
    )

    data = load_json(
        path,
        default={}
    )

    if not isinstance(
        data,
        dict
    ):
        return {}

    properties = data.get(
        "properties",
        []
    )

    if not isinstance(
        properties,
        list
    ):
        return {}

    result = {}

    for property_data in properties:

        if not isinstance(
            property_data,
            dict
        ):
            continue

        property_id = (
            property_data.get("id")
            or create_property_id(
                property_data.get(
                    "sourceUrl",
                    ""
                )
            )
        )

        if not property_id:
            continue

        property_data["id"] = property_id

        property_data.setdefault(
            "detailFetched",
            False
        )

        property_data.setdefault(
            "detailFetchedAt",
            None
        )

        property_data.setdefault(
            "lastSuccessfulDetailFetchedAt",
            None
        )

        property_data.setdefault(
            "detailDataStale",
            False
        )

        property_data.setdefault(
            "searchDetailPriceMismatch",
            False
        )

        property_data.setdefault(
            "detailFetchError",
            None
        )

        property_data.setdefault(
            "detailFetchBlocked",
            False
        )

        property_data.setdefault(
            "detailFetchBlockReason",
            None
        )

        property_data.setdefault(
            "fetchAttemptCount",
            0
        )

        property_data.setdefault(
            "lastFetchAttemptAt",
            None
        )

        property_data.setdefault(
            "lastFetchParserVersion",
            None
        )

        property_data.setdefault(
            "priceChanged",
            False
        )

        property_data.setdefault(
            "lastSearchPrice",
            parse_price(
                property_data.get(
                    "price"
                )
            )
        )

        property_data.setdefault(
            "lastSearchPriceText",
            clean_text(
                property_data.get(
                    "priceText"
                )
            )
        )

        property_data.setdefault(
            "detailPrice",
            None
        )

        property_data.setdefault(
            "detailPriceText",
            None
        )

        property_data.setdefault(
            "lastSuccessfulDetailPrice",
            None
        )

        property_data.setdefault(
            "price",
            parse_price(
                property_data.get(
                    "price"
                )
            )
        )

        property_data.setdefault(
            "priceText",
            clean_text(
                property_data.get(
                    "priceText"
                )
            )
        )

        property_data.setdefault(
            "priceHistory",
            []
        )

        property_data.setdefault(
            "detailParserVersion",
            None
        )

        property_data.setdefault(
            "detailQuality",
            "unknown"
        )

        property_data.setdefault(
            "detailQualityScore",
            None
        )

        property_data.setdefault(
            "missingFields",
            []
        )

        property_data.setdefault(
            "validationWarnings",
            []
        )

        property_data.setdefault(
            "extractionQuality",
            {}
        )

        property_data.setdefault(
            "searchCriteriaMatched",
            None
        )

        property_data.setdefault(
            "searchCriteriaMismatchReason",
            None
        )

        property_data.setdefault(
            "searchCriteria",
            {}
        )

        if not isinstance(
            property_data.get(
                "detail"
            ),
            dict
        ):
            property_data[
                "detail"
            ] = {}

        if not isinstance(
            property_data.get(
                "lastSuccessfulDetail"
            ),
            dict
        ):
            property_data[
                "lastSuccessfulDetail"
            ] = property_data[
                "detail"
            ]

        if not isinstance(
            property_data.get(
                "priceHistory"
            ),
            list
        ):
            property_data[
                "priceHistory"
            ] = []

        update_price_mismatch_and_refresh_flags(
            property_data
        )

        result[
            property_id
        ] = property_data

    return result


# ============================================================
# 物件統合
# ============================================================

def merge_property(
    existing,
    current,
    collected_at
):

    if existing is None:
        return current

    merged = existing.copy()

    if current.get(
        "sourceUrl"
    ):
        merged[
            "sourceUrl"
        ] = current[
            "sourceUrl"
        ]

    if current.get(
        "source"
    ):
        merged[
            "source"
        ] = current[
            "source"
        ]

    if current.get(
        "searchArea"
    ):
        merged[
            "searchArea"
        ] = current[
            "searchArea"
        ]

    if current.get(
        "searchPropertyType"
    ):
        merged[
            "searchPropertyType"
        ] = current[
            "searchPropertyType"
        ]

    existing_search_price = (
        merged.get(
            "lastSearchPrice"
        )
    )

    current_search_price = (
        current.get(
            "lastSearchPrice"
        )
    )

    if current_search_price is not None:

        merged[
            "lastSearchPrice"
        ] = current_search_price

        merged[
            "lastSearchPriceText"
        ] = current.get(
            "lastSearchPriceText"
        )

        if (
            existing_search_price
            is not None
            and current_search_price
            != existing_search_price
        ):

            logger.info(
                "検索一覧での価格差分を検知 "
                "(ID: %s): %s -> %s",
                merged.get("id"),
                existing_search_price,
                current_search_price,
            )

            merged[
                "priceChanged"
            ] = True

            merged[
                "priceHistory"
            ] = normalize_price_history(
                merged.get(
                    "priceHistory"
                ),
                current_search_price,
                collected_at,
                source="search"
            )

    if not merged.get(
        "firstSeenAt"
    ):
        merged[
            "firstSeenAt"
        ] = current.get(
            "firstSeenAt",
            collected_at
        )

    merged[
        "lastSeenAt"
    ] = collected_at

    merged[
        "collectedAt"
    ] = collected_at

    update_price_mismatch_and_refresh_flags(
        merged
    )

    return merged


def merge_properties(
    existing_properties,
    current_properties,
    collected_at
):

    if not isinstance(
        existing_properties,
        dict
    ):
        existing_properties = {}

    merged_properties = (
        existing_properties.copy()
    )

    if isinstance(
        current_properties,
        dict
    ):
        property_items = (
            current_properties.values()
        )

    elif isinstance(
        current_properties,
        list
    ):
        property_items = current_properties

    else:
        logger.warning(
            "物件データの形式が不正です"
        )

        return merged_properties

    for current in property_items:

        if not isinstance(
            current,
            dict
        ):
            continue

        property_id = current.get(
            "id"
        )

        if not property_id:
            continue

        existing = (
            merged_properties.get(
                property_id
            )
        )

        merged_properties[
            property_id
        ] = merge_property(
            existing,
            current,
            collected_at
        )

    return merged_properties


# ============================================================
# 詳細取得結果の反映
# ============================================================

def apply_detail_to_property(
    property_data: dict,
    detail_result: dict,
    fetched_at: str
) -> None:

    if not isinstance(
        property_data,
        dict
    ):
        return

    if not isinstance(
        detail_result,
        dict
    ):
        detail_result = {
            "success": False,
            "error": (
                "詳細取得結果が辞書形式ではありません"
            )
        }

    detail_fetched = bool(
        detail_result.get(
            "success",
            False
        )
    )

    property_data[
        "detailFetched"
    ] = detail_fetched

    property_data[
        "detailFetchedAt"
    ] = fetched_at

    if detail_fetched:

        raw_detail = (
            detail_result.get(
                "detail",
                {}
            )
        )

        if not isinstance(
            raw_detail,
            dict
        ):
            raw_detail = {}

        normalized_detail = normalize_detail(
            raw_detail
        )

        quality, score, warnings, missing_fields = (
            determine_detail_quality(
                normalized_detail,
                detail_fetched=True
            )
        )

        property_data[
            "detail"
        ] = normalized_detail

        property_data[
            "lastSuccessfulDetail"
        ] = normalized_detail

        property_data[
            "lastSuccessfulDetailFetchedAt"
        ] = fetched_at

        property_data[
            "detailQuality"
        ] = quality

        property_data[
            "detailQualityScore"
        ] = score

        property_data[
            "validationWarnings"
        ] = warnings

        property_data[
            "missingFields"
        ] = missing_fields

        detail_price = parse_price(
            normalized_detail.get(
                "price"
            )
            or normalized_detail.get(
                "priceText"
            )
        )

        property_data[
            "detailPrice"
        ] = detail_price

        if detail_price is not None:

            property_data[
                "detailPriceText"
            ] = clean_text(
                normalized_detail.get(
                    "priceText"
                )
            )

            property_data[
                "lastSuccessfulDetailPrice"
            ] = detail_price

            property_data[
                "price"
            ] = detail_price

            property_data[
                "priceHistory"
            ] = normalize_price_history(
                property_data.get(
                    "priceHistory"
                ),
                detail_price,
                fetched_at,
                source="detail"
            )

        else:

            property_data[
                "detailPriceText"
            ] = None

            property_data[
                "price"
            ] = property_data.get(
                "lastSearchPrice"
            )

        property_data[
            "detailParserVersion"
        ] = DETAIL_PARSER_VERSION

        property_data[
            "priceChanged"
        ] = False

        property_data[
            "detailDataStale"
        ] = False

        property_data[
            "detailFetchError"
        ] = None

        property_data[
            "detailFetchBlocked"
        ] = False

        property_data[
            "detailFetchBlockReason"
        ] = None

    else:

        if property_data.get(
            "lastSuccessfulDetail"
        ):
            property_data[
                "detail"
            ] = property_data[
                "lastSuccessfulDetail"
            ]

        else:
            property_data[
                "detail"
            ] = {}

        property_data[
            "detailDataStale"
        ] = bool(
            property_data.get(
                "lastSuccessfulDetailFetchedAt"
            )
        )

        property_data[
            "detailQuality"
        ] = "unknown"

        property_data[
            "detailQualityScore"
        ] = None

        property_data[
            "missingFields"
        ] = []

        property_data[
            "validationWarnings"
        ] = [
            "最新の詳細情報の再取得に失敗しました"
        ]

        property_data[
            "detailPrice"
        ] = None

        property_data[
            "detailPriceText"
        ] = None

        property_data[
            "price"
        ] = property_data.get(
            "lastSearchPrice"
        )

        error_msg = (
            detail_result.get(
                "error"
            )
            or "Unknown error"
        )

        property_data[
            "detailFetchError"
        ] = str(
            error_msg
        )

    update_price_mismatch_and_refresh_flags(
        property_data
    )


# ============================================================
# 詳細情報取得
# ============================================================

def fetch_details(
    properties,
    detail_adapter,
    max_count
):

    fetched_count = 0
    success_count = 0
    error_count = 0

    if max_count <= 0:

        logger.info(
            "詳細取得上限が0のため、"
            "詳細取得をスキップします"
        )

        return properties

    candidates = []

    for property_id, property_data in properties.items():

        if not isinstance(
            property_data,
            dict
        ):
            continue

        reset_detail_fetch_state_if_parser_updated(
            property_data
        )

        if should_fetch_detail(
            property_data
        ):
            candidates.append(
                (
                    property_id,
                    property_data
                )
            )

    candidates.sort(
        key=lambda item: (
            0
            if item[1].get(
                "priceChanged"
            )
            else 1,

            item[1].get(
                "fetchAttemptCount",
                0
            ),

            -safe_timestamp(
                item[1].get(
                    "lastSeenAt"
                )
            ),
        )
    )

    logger.info(
        "詳細再取得対象: %s件",
        len(candidates)
    )

    for property_id, property_data in candidates:

        if fetched_count >= max_count:
            break

        url = property_data.get(
            "sourceUrl"
        )

        if not url:
            continue

        logger.info(
            "詳細情報取得開始: %s %s",
            property_id,
            url
        )

        fetched_count += 1

        fetched_at = now_iso()

        property_data[
            "fetchAttemptCount"
        ] = (
            property_data.get(
                "fetchAttemptCount",
                0
            )
            + 1
        )

        property_data[
            "lastFetchAttemptAt"
        ] = fetched_at

        try:

            result = (
                detail_adapter.fetch_detail(
                    url
                )
            )

        except Exception as error:

            logger.exception(
                "詳細情報取得中に例外発生: %s",
                property_id
            )

            result = {
                "success": False,
                "error": str(error),
            }

        if not isinstance(
            result,
            dict
        ):

            result = {
                "success": False,
                "error": (
                    "詳細取得結果が辞書形式ではありません"
                )
            }

        apply_detail_to_property(
            property_data,
            result,
            fetched_at
        )

        if result.get(
            "success"
        ):

            success_count += 1

            logger.info(
                "詳細情報取得成功: %s "
                "quality=%s score=%s",
                property_id,
                property_data.get(
                    "detailQuality"
                ),
                property_data.get(
                    "detailQualityScore"
                ),
            )

        else:

            error_count += 1

            logger.warning(
                "詳細情報取得失敗: %s %s",
                property_id,
                result.get(
                    "error"
                )
            )

            if (
                property_data.get(
                    "fetchAttemptCount",
                    0
                )
                >= MAX_DETAIL_FETCH_ATTEMPTS
            ):

                property_data[
                    "detailFetchBlocked"
                ] = True

                property_data[
                    "detailFetchBlockReason"
                ] = (
                    "max_attempts_reached"
                )

        try:

            detail_adapter.wait()

        except Exception as error:

            logger.warning(
                "待機処理に失敗しました: %s",
                error
            )

    logger.info(
        "詳細取得結果: 処理=%s件 / "
        "成功=%s件 / 失敗=%s件",
        fetched_count,
        success_count,
        error_count,
    )

    return properties


# ============================================================
# 出力データ作成
# ============================================================

def build_output(
    properties,
    collected_at,
    filter_fetched_only=False,
    filter_search_criteria=False
):

    property_list = list(
        properties.values()
    )

    if filter_fetched_only:

        property_list = [
            item
            for item in property_list
            if has_usable_detail(item)
        ]

    if filter_search_criteria:

        before_count = len(
            property_list
        )

        property_list = [
            item
            for item in property_list
            if item.get(
                "searchCriteriaMatched"
            ) is not False
        ]

        excluded_count = (
            before_count
            - len(property_list)
        )

        if excluded_count:

            logger.info(
                "検索条件外物件を出力から除外: %s件",
                excluded_count
            )

    property_list.sort(
        key=lambda item: (
            item.get(
                "lastSeenAt",
                ""
            ),
            item.get(
                "id",
                ""
            )
        ),
        reverse=True,
    )

    detail_fetched_count = sum(
        1
        for item in property_list
        if item.get(
            "detailFetched"
        )
    )

    detail_error_count = sum(
        1
        for item in property_list
        if item.get(
            "detailFetchError"
        )
    )

    quality_counts_all = {
        "good": 0,
        "partial": 0,
        "poor": 0,
        "unknown": 0,
    }

    quality_counts_fetched = {
        "good": 0,
        "partial": 0,
        "poor": 0,
    }

    for item in property_list:

        quality = item.get(
            "detailQuality",
            "unknown"
        )

        if quality in quality_counts_all:

            quality_counts_all[
                quality
            ] += 1

        else:

            quality_counts_all[
                "unknown"
            ] += 1

        if item.get(
            "detailFetched"
        ):

            if quality in quality_counts_fetched:

                quality_counts_fetched[
                    quality
                ] += 1

    return {
        "updatedAt": collected_at,

        "summary": {
            "discoveredCount": len(
                property_list
            ),

            "detailFetchedCount": (
                detail_fetched_count
            ),

            "detailErrorCount": (
                detail_error_count
            ),

            "detailQualityCounts": {
                "all": (
                    quality_counts_all
                ),
                "fetched": (
                    quality_counts_fetched
                ),
            },
        },

        "properties": property_list,
    }


# ============================================================
# メイン処理
# ============================================================

def main():

    search_config = load_config()

    if not isinstance(
        search_config,
        dict
    ):

        logger.warning(
            "search.jsonの形式が不正です。"
            "空の設定として処理します"
        )

        search_config = {}

    collected_at = now_iso()

    current_properties = []

    # --------------------------------------------------------
    # 1. SUUMO検索
    # --------------------------------------------------------

    for adapter in create_adapters():

        try:

            results = adapter.search(
                search_config
            )

        except Exception as error:

            logger.exception(
                "検索処理に失敗しました: %s",
                error
            )

            continue

        if not isinstance(
            results,
            list
        ):

            logger.warning(
                "検索結果がリスト形式ではありません"
            )

            continue

        for item in results:

            normalized = normalize_property(
                item,
                collected_at
            )

            if normalized is not None:
                current_properties.append(
                    normalized
                )

    # --------------------------------------------------------
    # 2. 今回の検出結果をID単位で重複排除
    # --------------------------------------------------------

    current_unique = {}

    for property_data in current_properties:

        if not isinstance(
            property_data,
            dict
        ):
            continue

        property_id = property_data.get(
            "id"
        )

        if property_id:
            current_unique[
                property_id
            ] = property_data

    # --------------------------------------------------------
    # 3. 既存物件読み込み
    # --------------------------------------------------------

    existing_properties = (
        load_existing_properties()
    )

    # --------------------------------------------------------
    # 4. 既存データと今回の結果を統合
    # --------------------------------------------------------

    merged_properties = merge_properties(
        existing_properties,
        current_unique,
        collected_at
    )

    # --------------------------------------------------------
    # 5. 詳細取得上限
    # --------------------------------------------------------

    max_detail_count = (
        get_detail_fetch_limit(
            search_config
        )
    )

    # --------------------------------------------------------
    # 6. 詳細情報取得
    # --------------------------------------------------------

    if max_detail_count > 0:

        detail_adapter = (
            create_detail_adapter()
        )

        merged_properties = fetch_details(
            merged_properties,
            detail_adapter,
            max_detail_count
        )

    else:

        logger.info(
            "詳細取得上限が0のため、"
            "アダプター作成をスキップします"
        )

    # --------------------------------------------------------
    # 7. 築年数などの検索条件を適用
    # --------------------------------------------------------

    merged_properties = apply_search_criteria(
        merged_properties,
        search_config
    )

    # --------------------------------------------------------
    # 8. discovered_listings.json
    #
    # 全発見履歴を保存。
    # 条件外物件も履歴として残す。
    # --------------------------------------------------------

    discovered_output = build_output(
        merged_properties,
        collected_at,
        filter_fetched_only=False,
        filter_search_criteria=False
    )

    save_json(
        ROOT
        / "data"
        / "discovered_listings.json",
        discovered_output
    )

    # --------------------------------------------------------
    # 9. houses.json
    #
    # 詳細取得済み ＋ 検索条件内を表示対象とする。
    # --------------------------------------------------------

    houses_output = build_output(
        merged_properties,
        collected_at,
        filter_fetched_only=True,
        filter_search_criteria=True
    )

    save_json(
        ROOT
        / "data"
        / "houses.json",
        houses_output
    )

    # --------------------------------------------------------
    # 10. 実行ログ
    # --------------------------------------------------------

    min_built_year = (
        get_min_built_year(
            search_config
        )
    )

    logger.info(
        "今回の検出物件数: %s",
        len(current_unique)
    )

    logger.info(
        "全発見物件総数 "
        "(discovered_listings.json): %s",
        len(merged_properties)
    )

    logger.info(
        "詳細取得済み総数 "
        "(houses.json): %s",
        houses_output[
            "summary"
        ][
            "discoveredCount"
        ]
    )

    logger.info(
        "詳細取得上限: %s",
        max_detail_count
    )

    logger.info(
        "詳細取得済み累積件数: %s",
        discovered_output[
            "summary"
        ][
            "detailFetchedCount"
        ]
    )

    logger.info(
        "詳細取得エラー件数: %s",
        discovered_output[
            "summary"
        ][
            "detailErrorCount"
        ]
    )

    logger.info(
        "築年数条件: %s年以降",
        min_built_year
        if min_built_year is not None
        else "指定なし"
    )


if __name__ == "__main__":
    main()
