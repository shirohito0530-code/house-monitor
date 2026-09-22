from __future__ import annotations

import json
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# Imports
# ============================================================

try:
    from adapters.suumo_search import SuumoSearchAdapter
    from adapters.suumo_detail import SuumoDetailAdapter
except ImportError:
    from suumo_search import SuumoSearchAdapter
    from suumo_detail import SuumoDetailAdapter


# ============================================================
# Constants
# ============================================================

# IMPORTANT
# ------------------------------------------------------------
# main.py is located at:
#
#   house-monitor/scripts/main.py
#
# Therefore:
#
#   Path(__file__).resolve().parent
#
# points to:
#
#   house-monitor/scripts
#
# We need the repository root:
#
#   house-monitor
#
# ------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

SEARCH_CONFIG_PATH = CONFIG_DIR / "search.json"
SEARCH_URLS_PATH = CONFIG_DIR / "search_urls.json"

DISCOVERED_PATH = DATA_DIR / "discovered_listings.json"
HOUSES_PATH = DATA_DIR / "houses.json"
SUMMARY_PATH = DATA_DIR / "summary.json"

DEFAULT_DETAIL_FETCH_LIMIT = 5
MAX_DETAIL_FETCH_ATTEMPTS = 3

MAIN_PARSER_VERSION = "2026-09-22-v14"


# ============================================================
# Fallback target area rules
# ============================================================
#
# IMPORTANT
# ------------------------------------------------------------
# searchArea:
#   「どのSUUMO検索条件から見つかったか」
#
# areaDetected:
#   「詳細ページの実住所から判定したエリア」
#
# この2つは絶対に混同しない。
#
# 原則として search.json の areaRules を使用する。
# 以下は search.json に areaRules がない場合の
# 後方互換用。
#
# 注意:
# これは「柏の葉キャンパス周辺」の候補判定であり、
# 「柏の葉小学校区」の公式学区判定ではない。
# ============================================================

FALLBACK_AREA_RULES = {
    "柏の葉キャンパス": {
        "cities": [
            "柏市",
        ],
        "addressPatterns": [
            "柏の葉",
            "若柴",
            "正連寺",
            "中十余二",
            "十余二",
        ],
    },
    "流山おおたかの森": {
        "cities": [
            "流山市",
        ],
        "addressPatterns": [
            "おおたかの森北",
            "おおたかの森西",
            "おおたかの森東",
            "おおたかの森南",
        ],
    },
}


# ============================================================
# Utility
# ============================================================

def now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def load_json(
    path: Path,
    default: Any,
) -> Any:

    if not path.exists():
        return deepcopy(default)

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)

    except Exception as exc:

        print(
            f"[WARN] JSON読み込み失敗: "
            f"{path}: {exc}"
        )

        return deepcopy(default)


def save_json(
    path: Path,
    data: Any,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temp_path.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp_path.replace(path)


def clean_text(
    value: Any,
) -> Optional[str]:

    if value is None:
        return None

    text = re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()

    return text or None


def normalize_compact_text(
    value: Any,
) -> Optional[str]:

    if value is None:
        return None

    text = str(value)

    text = (
        text
        .replace("　", "")
        .replace(" ", "")
        .replace("\t", "")
        .replace("\r", "")
        .replace("\n", "")
    )

    return text or None


def to_number(
    value: Any,
) -> Optional[float]:

    if value is None:
        return None

    if isinstance(
        value,
        (int, float),
    ):
        return float(value)

    text = str(value)

    text = (
        text
        .replace(",", "")
        .replace("，", "")
        .replace(" ", "")
        .replace("　", "")
    )

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        text,
    )

    if not match:
        return None

    try:
        return float(
            match.group(0)
        )

    except ValueError:
        return None


def to_bool(
    value: Any,
) -> Optional[bool]:

    if isinstance(
        value,
        bool,
    ):
        return value

    if value is None:
        return None

    text = (
        str(value)
        .strip()
        .lower()
    )

    if text in {
        "true",
        "1",
        "yes",
        "y",
        "はい",
        "有",
        "あり",
    }:
        return True

    if text in {
        "false",
        "0",
        "no",
        "n",
        "いいえ",
        "無",
        "なし",
    }:
        return False

    return None


# ============================================================
# Config
# ============================================================

def load_search_config() -> Dict[str, Any]:

    config = load_json(
        SEARCH_CONFIG_PATH,
        {},
    )

    if not isinstance(
        config,
        dict,
    ):
        print(
            "[WARN] search.json が "
            "objectではありません"
        )
        return {}

    return config


def load_search_urls() -> List[Dict[str, Any]]:

    data = load_json(
        SEARCH_URLS_PATH,
        [],
    )

    if isinstance(
        data,
        list,
    ):
        return data

    if isinstance(
        data,
        dict,
    ):

        targets = data.get(
            "targets"
        )

        if isinstance(
            targets,
            list,
        ):
            return targets

    return []


# ============================================================
# Search configuration helpers
# ============================================================

def normalize_property_type(
    value: Any,
) -> Optional[str]:

    text = clean_text(
        value
    )

    if not text:
        return None

    if "中古" in text:
        return "中古戸建"

    if "新築" in text:
        return "新築戸建"

    return text


def get_search_max_age(
    config: Dict[str, Any],
) -> Optional[float]:

    value = config.get(
        "maxBuiltAgeYears"
    )

    if value is not None:
        return to_number(
            value
        )

    # 旧設定との互換
    value = config.get(
        "maxBuildingAgeYears"
    )

    if value is not None:
        return to_number(
            value
        )

    return None


def get_area_rules(
    config: Dict[str, Any],
) -> Dict[str, Any]:

    rules = config.get(
        "areaRules"
    )

    if isinstance(
        rules,
        dict,
    ) and rules:

        return rules

    return deepcopy(
        FALLBACK_AREA_RULES
    )


def get_allowed_property_types(
    config: Dict[str, Any],
) -> List[str]:

    values = config.get(
        "propertyTypes",
        [],
    )

    if not isinstance(
        values,
        list,
    ):
        return []

    result = []

    for value in values:

        normalized = (
            normalize_property_type(
                value
            )
        )

        if normalized:
            result.append(
                normalized
            )

    return result


# ============================================================
# Address / Area
# ============================================================

def normalize_address_for_area(
    address: Any,
) -> Optional[str]:

    if address is None:
        return None

    text = str(
        address
    )

    text = (
        text
        .replace("　", "")
        .replace(" ", "")
        .replace("\t", "")
        .replace("\r", "")
        .replace("\n", "")
        .replace("〒", "")
    )

    return text or None


def detect_area_from_address(
    address: Any,
    search_config: Dict[str, Any],
) -> Optional[str]:
    """
    詳細ページの実住所から監視対象エリアを判定。

    検索URL・駅名・タイトルは使用しない。

    必ず detail.address を優先する。
    """

    normalized = (
        normalize_address_for_area(
            address
        )
    )

    if not normalized:
        return None

    area_rules = get_area_rules(
        search_config
    )

    for area, rule in area_rules.items():

        if not isinstance(
            rule,
            dict,
        ):
            continue

        cities = rule.get(
            "cities",
            [],
        )

        patterns = rule.get(
            "addressPatterns"
        )

        if patterns is None:
            patterns = rule.get(
                "address_patterns",
                [],
            )

        if not isinstance(
            cities,
            list,
        ):
            cities = []

        if not isinstance(
            patterns,
            list,
        ):
            patterns = []

        # ----------------------------------------------------
        # City
        # ----------------------------------------------------

        if cities:

            city_matched = any(
                str(city) in normalized
                for city in cities
                if city
            )

            if not city_matched:
                continue

        # ----------------------------------------------------
        # Address pattern
        # ----------------------------------------------------

        if patterns:

            address_matched = any(
                str(pattern) in normalized
                for pattern in patterns
                if pattern
            )

            if not address_matched:
                continue

        # ----------------------------------------------------
        # If city and pattern conditions passed
        # ----------------------------------------------------

        return str(
            area
        )

    return None


def evaluate_area(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    実住所によるエリア最終判定。

    areaMatched:
      True  = 対象エリア
      False = 明確に対象外
      None  = 住所取得不能などで判定不能

    searchArea:
      検索元のエリア

    areaDetected:
      実住所から判定したエリア
    """

    detail = (
        property_data.get(
            "detail"
        )
        or property_data.get(
            "lastSuccessfulDetail"
        )
        or {}
    )

    if not isinstance(
        detail,
        dict,
    ):
        detail = {}

    address = detail.get(
        "address"
    )

    search_area = clean_text(
        property_data.get(
            "searchArea"
        )
    )

    detected_area = (
        detect_area_from_address(
            address,
            search_config,
        )
    )

    result = {
        "areaMatched": None,
        "areaDetected": detected_area,
        "areaAddress": address,
        "areaValidationReason": None,
    }

    # --------------------------------------------------------
    # 住所が取得できない
    # --------------------------------------------------------

    if not address:

        result[
            "areaValidationReason"
        ] = "address_unavailable"

        return result

    # --------------------------------------------------------
    # 実住所が監視対象外
    # --------------------------------------------------------

    if detected_area is None:

        result[
            "areaMatched"
        ] = False

        result[
            "areaValidationReason"
        ] = "address_outside_target_area"

        return result

    # --------------------------------------------------------
    # 検索元エリアが不明
    # --------------------------------------------------------

    if not search_area:

        result[
            "areaMatched"
        ] = True

        result[
            "areaValidationReason"
        ] = "area_detected"

        return result

    # --------------------------------------------------------
    # 検索元と実住所が一致
    # --------------------------------------------------------

    if detected_area == search_area:

        result[
            "areaMatched"
        ] = True

        result[
            "areaValidationReason"
        ] = "address_area_matched"

        return result

    # --------------------------------------------------------
    # 検索元と実住所が不一致
    #
    # 例:
    #
    # searchArea:
    #   柏の葉キャンパス
    #
    # actual address:
    #   流山市おおたかの森...
    #
    # → 除外
    # --------------------------------------------------------

    result[
        "areaMatched"
    ] = False

    result[
        "areaValidationReason"
    ] = (
        "search_area_address_mismatch"
    )

    return result


# ============================================================
# Property type
# ============================================================

def detect_property_type(
    property_data: Dict[str, Any],
) -> Optional[str]:

    candidates = [
        property_data.get(
            "propertyType"
        ),
        property_data.get(
            "searchPropertyType"
        ),
    ]

    detail = get_detail(
        property_data
    )

    if isinstance(
        detail,
        dict,
    ):

        candidates.extend([
            detail.get(
                "propertyType"
            ),
            detail.get(
                "propertyTypeText"
            ),
            detail.get(
                "type"
            ),
        ])

    for value in candidates:

        normalized = (
            normalize_property_type(
                value
            )
        )

        if normalized:
            return normalized

    # URLからの最終推定
    url = normalize_url(
        property_data.get(
            "url"
        )
    )

    if url:

        if "/chukoikkodate/" in url:
            return "中古戸建"

        if "/ikkodate/" in url:
            return "新築戸建"

    return None


def evaluate_property_type(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    allowed = (
        get_allowed_property_types(
            search_config
        )
    )

    actual = (
        detect_property_type(
            property_data
        )
    )

    if not allowed:

        return {
            "propertyType": actual,
            "propertyTypeMatched": True,
            "propertyTypeReason":
                "property_type_filter_not_configured",
        }

    if actual is None:

        return {
            "propertyType": None,
            "propertyTypeMatched": None,
            "propertyTypeReason":
                "property_type_unknown",
        }

    if actual in allowed:

        return {
            "propertyType": actual,
            "propertyTypeMatched": True,
            "propertyTypeReason":
                "property_type_allowed",
        }

    return {
        "propertyType": actual,
        "propertyTypeMatched": False,
        "propertyTypeReason":
            "property_type_not_allowed",
    }


# ============================================================
# Construction / Age
# ============================================================

def get_construction_month(
    detail: Dict[str, Any],
) -> Optional[str]:

    for key in [
        "constructionMonth",
        "constructionYearMonth",
    ]:

        value = detail.get(
            key
        )

        if value:
            return str(
                value
            )

    # constructionTextから補完
    construction_text = (
        detail.get(
            "constructionText"
        )
    )

    if construction_text:

        text = str(
            construction_text
        )

        match = re.search(
            r"(19\d{2}|20\d{2})\D{0,3}"
            r"(1[0-2]|0?[1-9])\D{0,2}"
            r"(?:月)?",
            text,
        )

        if match:

            return (
                f"{match.group(1)}-"
                f"{int(match.group(2)):02d}"
            )

        year_match = re.search(
            r"(19\d{2}|20\d{2})",
            text,
        )

        if year_match:
            return year_match.group(1)

    return None


def calculate_age_from_month(
    construction_month: str,
) -> Optional[float]:

    if not construction_month:
        return None

    match = re.fullmatch(
        r"(\d{4})(?:-(\d{1,2}))?",
        construction_month,
    )

    if not match:
        return None

    year = int(
        match.group(1)
    )

    month_text = match.group(2)

    if month_text is None:

        # 年だけなら年齢は年単位で概算
        month = 1

    else:

        month = int(
            month_text
        )

        if not (
            1 <= month <= 12
        ):
            return None

    current = datetime.now(
        timezone.utc
    )

    if not (
        1900
        <= year
        <= current.year + 2
    ):
        return None

    months = (
        (current.year - year) * 12
        + (
            current.month
            - month
        )
    )

    if months < 0:
        return None

    return round(
        months / 12,
        2,
    )


def evaluate_built_age(
    detail: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    max_age = get_search_max_age(
        search_config
    )

    result = {
        "builtAgeMatched": None,
        "builtAgeYears": None,
        "builtAgeReason": None,
    }

    # --------------------------------------------------------
    # 年齢フィルターなし
    # --------------------------------------------------------

    if max_age is None:

        result[
            "builtAgeMatched"
        ] = True

        result[
            "builtAgeReason"
        ] = "age_filter_not_configured"

        return result

    # --------------------------------------------------------
    # 新築戸建
    #
    # 新築の場合、築年月が取得できなくても
    # 検索結果上で新築と確認できている場合は
    # 年齢条件上は許容する。
    # --------------------------------------------------------

    property_type = normalize_property_type(
        detail.get(
            "propertyType"
        )
    )

    construction_month = (
        get_construction_month(
            detail
        )
    )

    if construction_month:

        age = detail.get(
            "constructionAgeYears"
        )

        if age is None:

            age = (
                calculate_age_from_month(
                    construction_month
                )
            )

        age_number = to_number(
            age
        )

        result[
            "builtAgeYears"
        ] = age_number

        if age_number is None:

            result[
                "builtAgeReason"
            ] = (
                "construction_date_unparseable"
            )

            return result

        if age_number <= max_age:

            result[
                "builtAgeMatched"
            ] = True

            result[
                "builtAgeReason"
            ] = "within_age_limit"

        else:

            result[
                "builtAgeMatched"
            ] = False

            result[
                "builtAgeReason"
            ] = (
                "building_age_over_limit"
            )

        return result

    # --------------------------------------------------------
    # 新築の場合
    # --------------------------------------------------------

    if property_type == "新築戸建":

        result[
            "builtAgeMatched"
        ] = True

        result[
            "builtAgeReason"
        ] = "new_house_without_construction_date"

        return result

    # --------------------------------------------------------
    # 中古等で築年月不明
    # --------------------------------------------------------

    result[
        "builtAgeMatched"
    ] = None

    result[
        "builtAgeReason"
    ] = (
        "construction_date_unavailable"
    )

    return result


# ============================================================
# Detail getters
# ============================================================

def get_detail(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

    detail = property_data.get(
        "detail"
    )

    if isinstance(
        detail,
        dict,
    ):
        return detail

    detail = property_data.get(
        "lastSuccessfulDetail"
    )

    if isinstance(
        detail,
        dict,
    ):
        return detail

    return {}


def get_price(
    detail: Dict[str, Any],
) -> Optional[float]:

    return to_number(
        detail.get(
            "price"
        )
    )


def get_land_area(
    detail: Dict[str, Any],
) -> Optional[float]:

    return to_number(
        detail.get(
            "landAreaM2"
        )
    )


def get_building_area(
    detail: Dict[str, Any],
) -> Optional[float]:

    return to_number(
        detail.get(
            "buildingAreaM2"
        )
    )


def get_walk_minutes(
    detail: Dict[str, Any],
) -> Optional[float]:

    value = detail.get(
        "walkMinutes"
    )

    if value is not None:

        return to_number(
            value
        )

    value = detail.get(
        "stationWalkMinutes"
    )

    if value is not None:

        return to_number(
            value
        )

    return None


# ============================================================
# Flat land / retaining wall
# ============================================================

def collect_detail_text(
    detail: Dict[str, Any],
) -> str:

    texts: List[str] = []

    keys = [
        "landCondition",
        "landConditionText",
        "landRemarks",
        "remarks",
        "description",
        "transportRaw",
        "textBlocks",
        "labelValuePairs",
    ]

    for key in keys:

        value = detail.get(
            key
        )

        if value is None:
            continue

        if isinstance(
            value,
            list,
        ):

            for item in value:

                if isinstance(
                    item,
                    dict,
                ):

                    texts.extend(
                        str(v)
                        for v in item.values()
                        if v is not None
                    )

                else:

                    texts.append(
                        str(item)
                    )

        elif isinstance(
            value,
            dict,
        ):

            texts.extend(
                str(v)
                for v in value.values()
                if v is not None
            )

        else:

            texts.append(
                str(value)
            )

    return normalize_compact_text(
        " ".join(texts)
    ) or ""


def detect_flat_land(
    detail: Dict[str, Any],
) -> Optional[bool]:

    for key in [
        "flatLand",
        "isFlatLand",
        "landFlat",
    ]:

        if key in detail:

            value = to_bool(
                detail.get(key)
            )

            if value is not None:
                return value

    text = collect_detail_text(
        detail
    )

    if not text:
        return None

    negative_words = [
        "傾斜地",
        "ひな壇",
        "高低差",
        "擁壁",
        "崖",
        "段差",
        "急傾斜",
    ]

    positive_words = [
        "平坦地",
        "平坦",
    ]

    if any(
        word in text
        for word in negative_words
    ):
        return False

    if any(
        word in text
        for word in positive_words
    ):
        return True

    return None


def detect_retaining_wall(
    detail: Dict[str, Any],
) -> Optional[bool]:

    for key in [
        "retainingWall",
        "hasRetainingWall",
        "isRetainingWall",
    ]:

        if key in detail:

            value = to_bool(
                detail.get(key)
            )

            if value is not None:
                return value

    text = collect_detail_text(
        detail
    )

    if not text:
        return None

    retaining_words = [
        "擁壁",
        "よう壁",
        "ヨウヘキ",
        "高低差",
        "崖",
        "土留め",
    ]

    if any(
        word in text
        for word in retaining_words
    ):
        return True

    # 明示的に擁壁なしと書かれている場合
    no_retaining_words = [
        "擁壁なし",
        "擁壁無",
        "擁壁無し",
        "擁壁不要",
    ]

    if any(
        word in text
        for word in no_retaining_words
    ):
        return False

    return None


# ============================================================
# Search criteria
# ============================================================

def evaluate_search_criteria(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    detail = get_detail(
        property_data
    )

    reasons: List[str] = []

    # ========================================================
    # Area
    # ========================================================

    area_result = evaluate_area(
        property_data,
        search_config,
    )

    if (
        area_result[
            "areaMatched"
        ] is False
    ):
        reasons.append(
            area_result[
                "areaValidationReason"
            ]
        )

    # ========================================================
    # Property type
    # ========================================================

    property_type_result = (
        evaluate_property_type(
            property_data,
            search_config,
        )
    )

    if (
        property_type_result[
            "propertyTypeMatched"
        ] is False
    ):
        reasons.append(
            property_type_result[
                "propertyTypeReason"
            ]
        )

    # ========================================================
    # Price
    # ========================================================

    max_price_man = to_number(
        search_config.get(
            "maxPriceMan"
        )
    )

    price = get_price(
        detail
    )

    if max_price_man is None:

        price_matched = True

    elif price is None:

        price_matched = None

    else:

        price_matched = (
            price
            <= max_price_man * 10_000
        )

        if not price_matched:

            reasons.append(
                "price_over_limit"
            )

    # ========================================================
    # Walk
    # ========================================================

    max_walk = to_number(
        search_config.get(
            "maxWalkMinutes"
        )
    )

    walk_minutes = get_walk_minutes(
        detail
    )

    if max_walk is None:

        walk_matched = True

    elif walk_minutes is None:

        walk_matched = None

    else:

        walk_matched = (
            walk_minutes
            <= max_walk
        )

        if not walk_matched:

            reasons.append(
                "walk_over_limit"
            )

    # ========================================================
    # Land
    # ========================================================

    min_land = to_number(
        search_config.get(
            "minLandArea"
        )
    )

    land_area = get_land_area(
        detail
    )

    if min_land is None:

        land_matched = True

    elif land_area is None:

        land_matched = None

    else:

        land_matched = (
            land_area
            >= min_land
        )

        if not land_matched:

            reasons.append(
                "land_area_under_limit"
            )

    # ========================================================
    # Building
    # ========================================================

    min_building = to_number(
        search_config.get(
            "minBuildingArea"
        )
    )

    building_area = get_building_area(
        detail
    )

    if min_building is None:

        building_matched = True

    elif building_area is None:

        building_matched = None

    else:

        building_matched = (
            building_area
            >= min_building
        )

        if not building_matched:

            reasons.append(
                "building_area_under_limit"
            )

    # ========================================================
    # Building age
    # ========================================================

    age_result = evaluate_built_age(
        detail,
        search_config,
    )

    if (
        age_result[
            "builtAgeMatched"
        ] is False
    ):

        reasons.append(
            age_result[
                "builtAgeReason"
            ]
        )

    # ========================================================
    # Flat land
    # ========================================================

    only_flat_land = bool(
        search_config.get(
            "onlyFlatLand",
            False,
        )
    )

    flat_land = detect_flat_land(
        detail
    )

    if not only_flat_land:

        flat_land_matched = True

    elif flat_land is None:

        flat_land_matched = None

    else:

        flat_land_matched = (
            flat_land is True
        )

        if not flat_land_matched:

            reasons.append(
                "not_flat_land"
            )

    # ========================================================
    # Retaining wall
    # ========================================================

    exclude_retaining_wall = bool(
        search_config.get(
            "excludeRetainingWall",
            False,
        )
    )

    retaining_wall = (
        detect_retaining_wall(
            detail
        )
    )

    if not exclude_retaining_wall:

        retaining_wall_matched = True

    elif retaining_wall is None:

        retaining_wall_matched = None

    else:

        retaining_wall_matched = (
            retaining_wall is False
        )

        if not retaining_wall_matched:

            reasons.append(
                "retaining_wall_detected"
            )

    # ========================================================
    # Final determination
    # ========================================================
    #
    # False:
    #   明確に条件違反
    #
    # None:
    #   判定不能
    #
    # True:
    #   条件適合
    #
    # IMPORTANT:
    #   houses.json は True のみ。
    # ========================================================

    all_results = [
        area_result[
            "areaMatched"
        ],
        property_type_result[
            "propertyTypeMatched"
        ],
        price_matched,
        walk_matched,
        land_matched,
        building_matched,
        age_result[
            "builtAgeMatched"
        ],
        flat_land_matched,
        retaining_wall_matched,
    ]

    if False in all_results:

        matched = False

    elif all(
        value is True
        for value in all_results
    ):

        matched = True

    else:

        matched = None

    return {

        # ----------------------------------------------------
        # Area
        # ----------------------------------------------------

        "areaMatched":
            area_result[
                "areaMatched"
            ],

        "areaDetected":
            area_result[
                "areaDetected"
            ],

        "areaAddress":
            area_result[
                "areaAddress"
            ],

        "areaValidationReason":
            area_result[
                "areaValidationReason"
            ],

        # ----------------------------------------------------
        # Property type
        # ----------------------------------------------------

        "propertyType":
            property_type_result[
                "propertyType"
            ],

        "propertyTypeMatched":
            property_type_result[
                "propertyTypeMatched"
            ],

        "propertyTypeReason":
            property_type_result[
                "propertyTypeReason"
            ],

        # ----------------------------------------------------
        # Individual criteria
        # ----------------------------------------------------

        "priceMatched":
            price_matched,

        "walkMatched":
            walk_matched,

        "landAreaMatched":
            land_matched,

        "buildingAreaMatched":
            building_matched,

        "builtAgeMatched":
            age_result[
                "builtAgeMatched"
            ],

        "builtAgeYears":
            age_result[
                "builtAgeYears"
            ],

        "builtAgeReason":
            age_result[
                "builtAgeReason"
            ],

        "flatLandMatched":
            flat_land_matched,

        "retainingWallMatched":
            retaining_wall_matched,

        # ----------------------------------------------------
        # Overall
        # ----------------------------------------------------

        "searchCriteriaMatched":
            matched,

        "searchCriteriaReasons":
            reasons,
    }


def apply_search_criteria(
    properties: List[Dict[str, Any]],
    search_config: Dict[str, Any],
) -> List[Dict[str, Any]]:

    evaluated_at = now_iso()

    for property_data in properties:

        result = (
            evaluate_search_criteria(
                property_data,
                search_config,
            )
        )

        property_data.update(
            result
        )

        property_data[
            "criteriaEvaluatedAt"
        ] = evaluated_at

        property_data[
            "criteriaParserVersion"
        ] = MAIN_PARSER_VERSION

    return properties


# ============================================================
# Property identity
# ============================================================

def normalize_url(
    url: Any,
) -> Optional[str]:

    if not url:
        return None

    text = str(
        url
    ).strip()

    text = text.split(
        "#",
        1,
    )[0]

    text = text.rstrip(
        "/"
    )

    return text or None


def get_property_id(
    property_data: Dict[str, Any],
) -> Optional[str]:

    for key in [
        "id",
        "propertyId",
        "listingId",
    ]:

        value = property_data.get(
            key
        )

        if value:
            return str(
                value
            )

    url = normalize_url(
        property_data.get(
            "url"
        )
        or property_data.get(
            "sourceUrl"
        )
    )

    if url:

        match = re.search(
            r"/nc_(\d+)$",
            url,
        )

        if match:

            return (
                "nc_"
                + match.group(1)
            )

        return url

    return None


# ============================================================
# Search result normalization
# ============================================================

def normalize_search_result(
    item: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    if not isinstance(
        item,
        dict,
    ):
        return None

    url = normalize_url(
        item.get(
            "url"
        )
        or item.get(
            "sourceUrl"
        )
        or item.get(
            "href"
        )
    )

    if not url:
        return None

    property_id = (
        item.get(
            "id"
        )
        or item.get(
            "propertyId"
        )
        or item.get(
            "listingId"
        )
    )

    if not property_id:

        match = re.search(
            r"/nc_(\d+)$",
            url,
        )

        if match:

            property_id = (
                "nc_"
                + match.group(1)
            )

    result = deepcopy(
        item
    )

    result[
        "url"
    ] = url

    if property_id:

        result[
            "id"
        ] = str(
            property_id
        )

    # URLからproperty typeを補完
    if not result.get(
        "propertyType"
    ):

        if "/chukoikkodate/" in url:

            result[
                "propertyType"
            ] = "中古戸建"

        elif "/ikkodate/" in url:

            result[
                "propertyType"
            ] = "新築戸建"

    if not result.get(
        "discoveredAt"
    ):

        result[
            "discoveredAt"
        ] = now_iso()

    return result


# ============================================================
# Price history
# ============================================================

def update_price_history(
    property_data: Dict[str, Any],
) -> None:
    """
    現在価格をpriceHistoryへ記録。

    既存のpriceHistoryがある場合は、
    同一価格を連続記録しない。
    """

    detail = get_detail(
        property_data
    )

    price = get_price(
        detail
    )

    if price is None:
        return

    history = property_data.get(
        "priceHistory"
    )

    if not isinstance(
        history,
        list,
    ):

        history = []

    last_price = None

    if history:

        last = history[-1]

        if isinstance(
            last,
            dict,
        ):

            last_price = to_number(
                last.get(
                    "price"
                )
            )

    if (
        last_price is not None
        and last_price == price
    ):
        return

    history.append({
        "price": price,
        "recordedAt": now_iso(),
    })

    # 履歴は過剰に肥大化させない
    property_data[
        "priceHistory"
    ] = history[-100:]


# ============================================================
# Merge discovery history
# ============================================================

def merge_property(
    old: Dict[str, Any],
    new: Dict[str, Any],
) -> Dict[str, Any]:

    merged = deepcopy(
        old
    )

    for key, value in new.items():

        if key == "detail":

            if value:

                merged[
                    "detail"
                ] = value

            continue

        if key == "lastSuccessfulDetail":

            if value:

                merged[
                    "lastSuccessfulDetail"
                ] = value

            continue

        # Noneで既存データを上書きしない
        if value is not None:

            merged[
                key
            ] = value

    return merged


def merge_discovered_listings(
    existing: List[Dict[str, Any]],
    discovered: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    by_id: Dict[
        str,
        Dict[str, Any],
    ] = {}

    # --------------------------------------------------------
    # Existing
    # --------------------------------------------------------

    for item in existing:

        normalized = (
            normalize_search_result(
                item
            )
        )

        if not normalized:
            continue

        key = get_property_id(
            normalized
        )

        if key:

            by_id[
                key
            ] = normalized

    # --------------------------------------------------------
    # New discovery
    # --------------------------------------------------------

    for item in discovered:

        normalized = (
            normalize_search_result(
                item
            )
        )

        if not normalized:
            continue

        key = get_property_id(
            normalized
        )

        if not key:
            continue

        if key in by_id:

            by_id[
                key
            ] = merge_property(
                by_id[
                    key
                ],
                normalized,
            )

        else:

            by_id[
                key
            ] = normalized

    return list(
        by_id.values()
    )


# ============================================================
# Detail fetch
# ============================================================

def should_fetch_detail(
    property_data: Dict[str, Any],
) -> bool:

    detail = property_data.get(
        "detail"
    )

    if isinstance(
        detail,
        dict,
    ):
        return False

    last_detail = (
        property_data.get(
            "lastSuccessfulDetail"
        )
    )

    if isinstance(
        last_detail,
        dict,
    ):
        return False

    return True


def fetch_detail_for_property(
    property_data: Dict[str, Any],
    detail_adapter: SuumoDetailAdapter,
) -> Dict[str, Any]:

    url = normalize_url(
        property_data.get(
            "url"
        )
    )

    if not url:
        return property_data

    result: Optional[
        Dict[str, Any]
    ] = None

    for attempt in range(
        1,
        MAX_DETAIL_FETCH_ATTEMPTS + 1,
    ):

        try:

            result = (
                detail_adapter.fetch_detail(
                    url
                )
            )

            if not isinstance(
                result,
                dict,
            ):

                result = {
                    "success": False,
                    "detail": None,
                    "error":
                        "invalid_adapter_result",
                }

        except Exception as exc:

            result = {
                "success": False,
                "detail": None,
                "error": str(
                    exc
                ),
            }

        if result.get(
            "success"
        ):
            break

        if attempt < (
            MAX_DETAIL_FETCH_ATTEMPTS
        ):

            time.sleep(
                2 ** (
                    attempt - 1
                )
            )

    if not result:

        return property_data

    property_data[
        "detailFetchAttempts"
    ] = (
        property_data.get(
            "detailFetchAttempts",
            0,
        )
        + 1
    )

    property_data[
        "lastDetailFetchAt"
    ] = (
        result.get(
            "fetchedAt"
        )
        or now_iso()
    )

    if (
        result.get(
            "success"
        )
        and result.get(
            "detail"
        )
    ):

        detail = result[
            "detail"
        ]

        property_data[
            "detail"
        ] = detail

        property_data[
            "lastSuccessfulDetail"
        ] = deepcopy(
            detail
        )

        property_data[
            "detailFetchSuccess"
        ] = True

        property_data[
            "detailFetchError"
        ] = None

    else:

        property_data[
            "detailFetchSuccess"
        ] = False

        property_data[
            "detailFetchError"
        ] = result.get(
            "error"
        )

        # ----------------------------------------------------
        # IMPORTANT
        # ----------------------------------------------------
        # 以前取得できていた詳細情報がある場合、
        # 取得失敗によってそれを消さない。
        # ----------------------------------------------------

        if (
            isinstance(
                property_data.get(
                    "lastSuccessfulDetail"
                ),
                dict,
            )
        ):

            property_data[
                "detail"
            ] = deepcopy(
                property_data[
                    "lastSuccessfulDetail"
                ]
            )

    return property_data


def fetch_details(
    properties: List[Dict[str, Any]],
    detail_adapter: SuumoDetailAdapter,
    limit: int,
) -> List[Dict[str, Any]]:

    fetched = 0

    for property_data in properties:

        if fetched >= limit:
            break

        if not should_fetch_detail(
            property_data
        ):
            continue

        print(
            "[DETAIL]",
            property_data.get(
                "url"
            )
        )

        fetch_detail_for_property(
            property_data,
            detail_adapter,
        )

        fetched += 1

        try:

            detail_adapter.wait()

        except Exception:

            # wait()がない実装でも停止させない
            pass

    print(
        f"[DETAIL] fetched={fetched}"
    )

    return properties


# ============================================================
# Existing detail refresh
# ============================================================

def refresh_existing_details(
    properties: List[Dict[str, Any]],
    detail_adapter: SuumoDetailAdapter,
    limit: int,
) -> List[Dict[str, Any]]:

    """
    既存物件の再取得。

    現在は新規物件を優先するため、
    自動再取得は行わない。

    将来的に価格監視を強化する場合は、
    ここにrefresh周期を実装する。
    """

    return properties


# ============================================================
# Output filtering
# ============================================================

def is_displayable_property(
    property_data: Dict[str, Any],
) -> bool:

    # --------------------------------------------------------
    # 実住所から対象エリア確認済みであることを必須化
    # --------------------------------------------------------

    if (
        property_data.get(
            "areaMatched"
        ) is not True
    ):
        return False

    # --------------------------------------------------------
    # 全検索条件を通過
    # --------------------------------------------------------

    if (
        property_data.get(
            "searchCriteriaMatched"
        ) is not True
    ):
        return False

    # --------------------------------------------------------
    # Detail
    # --------------------------------------------------------

    detail = get_detail(
        property_data
    )

    if not detail:
        return False

    return True


def build_output(
    properties: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    result = []

    for property_data in properties:

        if not is_displayable_property(
            property_data
        ):
            continue

        result.append(
            property_data
        )

    return result


# ============================================================
# Summary
# ============================================================

def build_summary(
    discovered: List[Dict[str, Any]],
    houses: List[Dict[str, Any]],
) -> Dict[str, Any]:

    good_count = 0
    partial_count = 0
    poor_count = 0

    price_reduction_count = 0

    area_excluded_count = 0
    area_unknown_count = 0

    criteria_excluded_count = 0
    criteria_unknown_count = 0

    property_type_excluded_count = 0

    for item in discovered:

        detail = get_detail(
            item
        )

        quality = detail.get(
            "detailQuality"
        )

        if quality == "good":

            good_count += 1

        elif quality == "partial":

            partial_count += 1

        elif quality == "poor":

            poor_count += 1

        # ----------------------------------------------------
        # Area
        # ----------------------------------------------------

        if (
            item.get(
                "areaMatched"
            ) is False
        ):

            area_excluded_count += 1

        elif (
            item.get(
                "areaMatched"
            ) is None
        ):

            area_unknown_count += 1

        # ----------------------------------------------------
        # Criteria
        # ----------------------------------------------------

        if (
            item.get(
                "searchCriteriaMatched"
            ) is False
        ):

            criteria_excluded_count += 1

        elif (
            item.get(
                "searchCriteriaMatched"
            ) is None
        ):

            criteria_unknown_count += 1

        # ----------------------------------------------------
        # Property type
        # ----------------------------------------------------

        if (
            item.get(
                "propertyTypeMatched"
            ) is False
        ):

            property_type_excluded_count += 1

        # ----------------------------------------------------
        # Price reduction
        # ----------------------------------------------------

        price_history = (
            item.get(
                "priceHistory"
            )
        )

        if (
            isinstance(
                price_history,
                list,
            )
            and len(
                price_history
            ) >= 2
        ):

            old_item = (
                price_history[-2]
            )

            new_item = (
                price_history[-1]
            )

            old_price = (
                to_number(
                    old_item.get(
                        "price"
                    )
                )
                if isinstance(
                    old_item,
                    dict,
                )
                else None
            )

            new_price = (
                to_number(
                    new_item.get(
                        "price"
                    )
                )
                if isinstance(
                    new_item,
                    dict,
                )
                else None
            )

            if (
                old_price is not None
                and new_price is not None
                and new_price < old_price
            ):

                price_reduction_count += 1

    return {

        "generatedAt":
            now_iso(),

        "parserVersion":
            MAIN_PARSER_VERSION,

        "discoveredCount":
            len(discovered),

        "houseCount":
            len(houses),

        "goodCount":
            good_count,

        "partialCount":
            partial_count,

        "poorCount":
            poor_count,

        "priceReductionCount":
            price_reduction_count,

        "areaExcludedCount":
            area_excluded_count,

        "areaUnknownCount":
            area_unknown_count,

        "criteriaExcludedCount":
            criteria_excluded_count,

        "criteriaUnknownCount":
            criteria_unknown_count,

        "propertyTypeExcludedCount":
            property_type_excluded_count,
    }


# ============================================================
# Save
# ============================================================

def save_discovered(
    properties: List[Dict[str, Any]],
) -> None:

    save_json(
        DISCOVERED_PATH,
        properties,
    )


def save_houses(
    properties: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    houses = build_output(
        properties
    )

    save_json(
        HOUSES_PATH,
        houses,
    )

    print(
        f"[OUTPUT] houses="
        f"{len(houses)}"
    )

    return houses


# ============================================================
# Main
# ============================================================

def main() -> int:

    print(
        "============================================"
    )

    print(
        "House Monitor"
    )

    print(
        f"main parser: "
        f"{MAIN_PARSER_VERSION}"
    )

    print(
        f"root: {ROOT}"
    )

    print(
        f"config: {CONFIG_DIR}"
    )

    print(
        f"data: {DATA_DIR}"
    )

    print(
        "============================================"
    )

    # --------------------------------------------------------
    # Directories
    # --------------------------------------------------------

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Config
    # --------------------------------------------------------

    search_config = (
        load_search_config()
    )

    search_urls = (
        load_search_urls()
    )

    print(
        "[CONFIG]",
        json.dumps(
            search_config,
            ensure_ascii=False,
        )
    )

    print(
        f"[CONFIG] search targets="
        f"{len(search_urls)}"
    )

    # --------------------------------------------------------
    # Existing history
    # --------------------------------------------------------

    existing_discovered = (
        load_json(
            DISCOVERED_PATH,
            [],
        )
    )

    if not isinstance(
        existing_discovered,
        list,
    ):

        existing_discovered = []

    print(
        f"[HISTORY] existing="
        f"{len(existing_discovered)}"
    )

    # --------------------------------------------------------
    # Search adapter
    # --------------------------------------------------------

    search_adapter = (
        SuumoSearchAdapter(
            config=search_config,
            root_path=str(ROOT),
        )
    )

    # --------------------------------------------------------
    # Search
    # --------------------------------------------------------

    try:

        discovered_now = (
            search_adapter.search()
        )

    except TypeError:

        # ----------------------------------------------------
        # Adapterによってはsearch_urlsを
        # 引数に取る実装があるため対応
        # ----------------------------------------------------

        discovered_now = (
            search_adapter.search(
                search_urls
            )
        )

    except Exception as exc:

        print(
            "[ERROR] SUUMO search failed:",
            repr(exc),
        )

        discovered_now = []

    if discovered_now is None:

        discovered_now = []

    if not isinstance(
        discovered_now,
        list,
    ):

        discovered_now = []

    normalized_now = []

    for item in discovered_now:

        normalized = (
            normalize_search_result(
                item
            )
        )

        if normalized:

            normalized_now.append(
                normalized
            )

    print(
        f"[SEARCH] discovered="
        f"{len(normalized_now)}"
    )

    # --------------------------------------------------------
    # Merge
    # --------------------------------------------------------

    properties = (
        merge_discovered_listings(
            existing_discovered,
            normalized_now,
        )
    )

    print(
        f"[MERGE] total="
        f"{len(properties)}"
    )

    # --------------------------------------------------------
    # Detail adapter
    # --------------------------------------------------------

    detail_adapter = (
        SuumoDetailAdapter(
            config=search_config,
            root_path=str(ROOT),
        )
    )

    # --------------------------------------------------------
    # Detail fetch limit
    # --------------------------------------------------------

    detail_limit = to_number(
        search_config.get(
            "detailFetchLimit"
        )
    )

    if detail_limit is None:

        detail_limit = (
            DEFAULT_DETAIL_FETCH_LIMIT
        )

    detail_limit = max(
        0,
        int(
            detail_limit
        ),
    )

    print(
        f"[CONFIG] detailFetchLimit="
        f"{detail_limit}"
    )

    # --------------------------------------------------------
    # Detail fetch
    # --------------------------------------------------------
    #
    # 新規物件は詳細ページを取得する。
    #
    # 検索結果だけでは実住所が分からないため、
    # エリア判定を詳細取得前には確定させない。
    # --------------------------------------------------------

    properties = fetch_details(
        properties,
        detail_adapter,
        detail_limit,
    )

    # --------------------------------------------------------
    # Price history
    # --------------------------------------------------------

    for property_data in properties:

        if get_detail(
            property_data
        ):

            update_price_history(
                property_data
            )

    # --------------------------------------------------------
    # Criteria
    # --------------------------------------------------------

    properties = apply_search_criteria(
        properties,
        search_config,
    )

    # --------------------------------------------------------
    # Save discovery history
    #
    # IMPORTANT:
    #
    # area外
    # 条件外
    # 住所不明
    # 詳細取得失敗
    #
    # も保存する。
    #
    # これにより、なぜDashboardから消えたか追跡できる。
    # --------------------------------------------------------

    save_discovered(
        properties
    )

    # --------------------------------------------------------
    # Save houses
    #
    # IMPORTANT:
    #
    # areaMatched == True
    #
    # AND
    #
    # searchCriteriaMatched == True
    #
    # の物件だけを表示する。
    # --------------------------------------------------------

    houses = save_houses(
        properties
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = build_summary(
        properties,
        houses,
    )

    save_json(
        SUMMARY_PATH,
        summary,
    )

    print(
        "============================================"
    )

    print(
        "[SUMMARY]"
    )

    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        )
    )

    print(
        "============================================"
    )

    return 0


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":

    try:

        sys.exit(
            main()
        )

    except KeyboardInterrupt:

        print(
            "\n[STOP] interrupted"
        )

        sys.exit(
            130
        )

    except Exception as exc:

        print(
            "[FATAL]",
            repr(exc),
        )

        raise