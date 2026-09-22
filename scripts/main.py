from __future__ import annotations

import json
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit


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

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

SEARCH_CONFIG_PATH = CONFIG_DIR / "search.json"
SEARCH_URLS_PATH = CONFIG_DIR / "search_urls.json"

DISCOVERED_PATH = DATA_DIR / "discovered_listings.json"
HOUSES_PATH = DATA_DIR / "houses.json"
SUMMARY_PATH = DATA_DIR / "summary.json"

DEFAULT_DETAIL_FETCH_LIMIT = 5

# 1回のGitHub Actions実行で同一物件について
# 最大3回までfetch_detailを呼ぶ。
MAX_DETAIL_FETCH_ATTEMPTS = 3

# 再試行対象
RETRYABLE_DETAIL_ERROR_TYPES = {
    "forbidden",
    "rate_limited",
    "timeout",
    "network_error",
    "server_error",
}

# v18:
# - detail URL canonicalization
# - successful detail判定をlastSuccessfulDetail基準へ変更
# - 404を無駄に3回リトライしない
# - detailFetchAttempts / detailFetchRunAttemptsを分離
# - 過去成功データを失敗時に保持
# - 新規・失敗物件を優先
MAIN_PARSER_VERSION = "2026-09-22-v18"


# ============================================================
# Fallback target area rules
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
# SUUMO URL normalization
# ============================================================

SUUMO_HOSTS = {
    "suumo.jp",
    "www.suumo.jp",
}

SUUMO_LISTING_PATH_PATTERN = re.compile(
    r"^/(?:chukoikkodate|ikkodate|mansion|chukomansion)"
    r"/.+/nc_\d+/?$",
    re.IGNORECASE,
)


def repair_malformed_suumo_scheme(
    text: str,
) -> str:
    """
    壊れたSUUMO URLを修復する。

    https:/www.suumo.jp/...
        ↓
    https://www.suumo.jp/...
    """

    text = re.sub(
        r"^https:/+",
        "https://",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"^http:/+",
        "http://",
        text,
        flags=re.IGNORECASE,
    )

    return text


def normalize_suumo_listing_url(
    url: Any,
) -> Optional[str]:
    """
    SUUMO個別物件URLを正規化する。

    正規形:

    https://www.suumo.jp/chukoikkodate/chiba/sc_kashiwa/nc_12345678/

    重要:
    URL全体に対して /{2,} を適用しない。
    """

    if url is None:
        return None

    text = str(url).strip()

    if not text:
        return None

    # --------------------------------------------------------
    # 危険なスキームを拒否
    # --------------------------------------------------------

    if text.lower().startswith(
        (
            "javascript:",
            "mailto:",
            "tel:",
            "#",
        )
    ):
        return None

    # --------------------------------------------------------
    # protocol-relative URL
    # --------------------------------------------------------

    if text.startswith("//"):
        text = "https:" + text

    # --------------------------------------------------------
    # 壊れたscheme修復
    # --------------------------------------------------------

    text = repair_malformed_suumo_scheme(
        text
    )

    # --------------------------------------------------------
    # 相対URL
    # --------------------------------------------------------

    if text.startswith("/"):
        text = urljoin(
            "https://www.suumo.jp/",
            text,
        )

    # --------------------------------------------------------
    # URL解析
    # --------------------------------------------------------

    try:
        parsed = urlsplit(
            text
        )

    except ValueError:
        return None

    scheme = (
        parsed.scheme.lower()
    )

    hostname = (
        parsed.hostname or ""
    ).lower()

    # --------------------------------------------------------
    # HTTP → HTTPS
    # --------------------------------------------------------

    if scheme == "http":
        scheme = "https"

    # --------------------------------------------------------
    # SUUMO以外を拒否
    # --------------------------------------------------------

    if hostname not in SUUMO_HOSTS:
        return None

    # --------------------------------------------------------
    # path
    # --------------------------------------------------------

    path = parsed.path or "/"

    # path部分だけ連続slashを整理
    path = re.sub(
        r"/{2,}",
        "/",
        path,
    )

    # --------------------------------------------------------
    # nc_ ID確認
    # --------------------------------------------------------

    if not re.search(
        r"/nc_\d+/?$",
        path,
        re.IGNORECASE,
    ):
        return None

    # --------------------------------------------------------
    # wwwへ統一
    # --------------------------------------------------------

    hostname = "www.suumo.jp"

    # --------------------------------------------------------
    # 末尾slashを必ず付ける
    # --------------------------------------------------------

    path = (
        path.rstrip("/")
        + "/"
    )

    # --------------------------------------------------------
    # query / fragmentを削除
    # --------------------------------------------------------

    return urlunsplit(
        (
            "https",
            hostname,
            path,
            "",
            "",
        )
    )


def normalize_url(
    url: Any,
) -> Optional[str]:

    return normalize_suumo_listing_url(
        url
    )


def is_valid_suumo_listing_url(
    url: Any,
) -> bool:

    normalized = (
        normalize_suumo_listing_url(
            url
        )
    )

    if not normalized:
        return False

    try:
        parsed = urlsplit(
            normalized
        )

    except ValueError:
        return False

    if parsed.hostname != "www.suumo.jp":
        return False

    return bool(
        SUUMO_LISTING_PATH_PATTERN.match(
            parsed.path
        )
    )


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
            "[WARN] search.json がobjectではありません"
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

        if cities:

            city_matched = any(
                str(city) in normalized
                for city in cities
                if city
            )

            if not city_matched:
                continue

        if patterns:

            address_matched = any(
                str(pattern) in normalized
                for pattern in patterns
                if pattern
            )

            if not address_matched:
                continue

        return str(area)

    return None


def normalize_search_area(
    value: Any,
) -> Optional[str]:

    text = clean_text(
        value
    )

    if not text:
        return None

    aliases = {
        "柏の葉": "柏の葉キャンパス",
        "柏の葉キャンパス": "柏の葉キャンパス",
        "流山おおたかの森": "流山おおたかの森",
        "おおたかの森": "流山おおたかの森",
    }

    return aliases.get(
        text,
        text,
    )


def evaluate_area(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    detail = get_detail(
        property_data
    )

    address = detail.get(
        "address"
    )

    search_area = normalize_search_area(
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

    if not address:

        result[
            "areaValidationReason"
        ] = "address_unavailable"

        return result

    if detected_area is None:

        result[
            "areaMatched"
        ] = False

        result[
            "areaValidationReason"
        ] = "address_outside_target_area"

        return result

    if not search_area:

        result[
            "areaMatched"
        ] = True

        result[
            "areaValidationReason"
        ] = "area_detected"

        return result

    if detected_area == search_area:

        result[
            "areaMatched"
        ] = True

        result[
            "areaValidationReason"
        ] = "address_area_matched"

        return result

    result[
        "areaMatched"
    ] = False

    result[
        "areaValidationReason"
    ] = "search_area_address_mismatch"

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
        property_data.get(
            "searchDetectedPropertyType"
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

    url = normalize_suumo_listing_url(
        property_data.get(
            "url"
        )
        or property_data.get(
            "sourceUrl"
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
            return str(value)

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

    month = (
        1
        if month_text is None
        else int(month_text)
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
        + current.month
        - month
    )

    if months < 0:
        return 0.0

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

    if max_age is None:

        result[
            "builtAgeMatched"
        ] = True

        result[
            "builtAgeReason"
        ] = "age_filter_not_configured"

        return result

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

            age = calculate_age_from_month(
                construction_month
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
            ] = "building_age_over_limit"

        return result

    property_type = normalize_property_type(
        detail.get(
            "propertyType"
        )
    )

    if property_type == "新築戸建":

        result[
            "builtAgeMatched"
        ] = True

        result[
            "builtAgeReason"
        ] = "new_house_without_construction_date"

        return result

    result[
        "builtAgeReason"
    ] = "construction_date_unavailable"

    return result


# ============================================================
# Detail getters
# ============================================================

def get_detail(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # 最優先：最後に成功した詳細
    # --------------------------------------------------------

    last_detail = (
        property_data.get(
            "lastSuccessfulDetail"
        )
    )

    if isinstance(
        last_detail,
        dict,
    ) and last_detail:

        return last_detail

    # --------------------------------------------------------
    # 後方互換：detail
    # --------------------------------------------------------

    detail = property_data.get(
        "detail"
    )

    if isinstance(
        detail,
        dict,
    ) and detail:

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

    for key in [
        "walkMinutes",
        "stationWalkMinutes",
    ]:

        value = detail.get(
            key
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

    area_result = evaluate_area(
        property_data,
        search_config,
    )

    if area_result[
        "areaMatched"
    ] is False:

        reasons.append(
            area_result[
                "areaValidationReason"
            ]
        )

    property_type_result = (
        evaluate_property_type(
            property_data,
            search_config,
        )
    )

    if property_type_result[
        "propertyTypeMatched"
    ] is False:

        reasons.append(
            property_type_result[
                "propertyTypeReason"
            ]
        )

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

    age_result = evaluate_built_age(
        detail,
        search_config,
    )

    if age_result[
        "builtAgeMatched"
    ] is False:

        reasons.append(
            age_result[
                "builtAgeReason"
            ]
        )

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

    exclude_retaining_wall = bool(
        search_config.get(
            "excludeRetainingWall",
            False,
        )
    )

    retaining_wall = detect_retaining_wall(
        detail
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

        result = evaluate_search_criteria(
            property_data,
            search_config,
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

def extract_nc_id(
    url: Any,
) -> Optional[str]:

    normalized = (
        normalize_suumo_listing_url(
            url
        )
    )

    if not normalized:
        return None

    match = re.search(
        r"/nc_(\d+)/?$",
        normalized,
        re.IGNORECASE,
    )

    if not match:
        return None

    return (
        "nc_"
        + match.group(1)
    )


def get_property_id(
    property_data: Dict[str, Any],
) -> Optional[str]:

    url = (
        property_data.get(
            "url"
        )
        or property_data.get(
            "sourceUrl"
        )
    )

    nc_id = extract_nc_id(
        url
    )

    if nc_id:
        return nc_id

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

    original_url = (
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

    url = normalize_suumo_listing_url(
        original_url
    )

    if not url:
        return None

    property_id = extract_nc_id(
        url
    )

    if not property_id:

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

    result = deepcopy(
        item
    )

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    result[
        "url"
    ] = url

    result[
        "sourceUrl"
    ] = url

    # --------------------------------------------------------
    # ID
    # --------------------------------------------------------

    if property_id:

        result[
            "id"
        ] = str(
            property_id
        )

    # --------------------------------------------------------
    # Property type
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Timestamps
    # --------------------------------------------------------

    current_time = now_iso()

    if not result.get(
        "discoveredAt"
    ):

        result[
            "discoveredAt"
        ] = current_time

    result[
        "lastSeenAt"
    ] = current_time

    result[
        "urlNormalizedAt"
    ] = current_time

    result[
        "urlParserVersion"
    ] = MAIN_PARSER_VERSION

    return result


# ============================================================
# Price history
# ============================================================

def update_price_history(
    property_data: Dict[str, Any],
) -> None:

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

    # --------------------------------------------------------
    # firstDiscoveredAt
    # --------------------------------------------------------

    first_discovered = (
        old.get(
            "firstDiscoveredAt"
        )
        or old.get(
            "discoveredAt"
        )
        or new.get(
            "discoveredAt"
        )
    )

    # --------------------------------------------------------
    # 一般フィールド
    # --------------------------------------------------------

    for key, value in new.items():

        if key in {
            "detail",
            "lastSuccessfulDetail",
        }:
            continue

        if value is not None:

            merged[
                key
            ] = value

    # --------------------------------------------------------
    # Detail
    #
    # 新しいdetailがある場合のみ更新。
    # 空のdetailで過去データを消さない。
    # --------------------------------------------------------

    new_detail = new.get(
        "detail"
    )

    if (
        isinstance(
            new_detail,
            dict,
        )
        and new_detail
    ):

        merged[
            "detail"
        ] = deepcopy(
            new_detail
        )

    # --------------------------------------------------------
    # Last successful detail
    # --------------------------------------------------------

    new_last_detail = (
        new.get(
            "lastSuccessfulDetail"
        )
    )

    if (
        isinstance(
            new_last_detail,
            dict,
        )
        and new_last_detail
    ):

        merged[
            "lastSuccessfulDetail"
        ] = deepcopy(
            new_last_detail
        )

    # --------------------------------------------------------
    # firstDiscoveredAt
    # --------------------------------------------------------

    if first_discovered:

        merged[
            "firstDiscoveredAt"
        ] = first_discovered

    # --------------------------------------------------------
    # discoveryCount
    #
    # new側に lastSeenAt がある = 今回の検索で発見された
    # --------------------------------------------------------

    old_count = to_number(
        old.get(
            "discoveryCount"
        )
    )

    if old_count is None:
        old_count = 0

    if new.get(
        "lastSeenAt"
    ):

        merged[
            "discoveryCount"
        ] = int(
            old_count + 1
        )

    # --------------------------------------------------------
    # Canonical URL
    # --------------------------------------------------------

    canonical_url = (
        normalize_suumo_listing_url(
            merged.get(
                "url"
            )
            or merged.get(
                "sourceUrl"
            )
        )
    )

    if canonical_url:

        merged[
            "url"
        ] = canonical_url

        merged[
            "sourceUrl"
        ] = canonical_url

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

        normalized = normalize_search_result(
            item
        )

        if not normalized:
            continue

        key = get_property_id(
            normalized
        )

        if not key:
            continue

        # 既存データはlastSeenAtを今回更新しない。
        # normalize_search_resultで更新されてしまうため、
        # 元データから復元する。
        if item.get(
            "lastSeenAt"
        ):

            normalized[
                "lastSeenAt"
            ] = item[
                "lastSeenAt"
            ]

        if key in by_id:

            by_id[
                key
            ] = merge_property(
                by_id[key],
                normalized,
            )

        else:

            normalized.setdefault(
                "firstDiscoveredAt",
                normalized.get(
                    "discoveredAt"
                ),
            )

            normalized.setdefault(
                "discoveryCount",
                1,
            )

            by_id[
                key
            ] = normalized

    # --------------------------------------------------------
    # New discovery
    # --------------------------------------------------------

    for item in discovered:

        normalized = normalize_search_result(
            item
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
                by_id[key],
                normalized,
            )

        else:

            normalized[
                "firstDiscoveredAt"
            ] = normalized.get(
                "discoveredAt"
            )

            normalized[
                "discoveryCount"
            ] = 1

            by_id[
                key
            ] = normalized

    return list(
        by_id.values()
    )


# ============================================================
# Detail fetch status
# ============================================================

def has_successful_detail(
    property_data: Dict[str, Any],
) -> bool:
    """
    本当に詳細取得成功済みかを判定。

    古いデータのdetailだけが残っていても、
    detailFetchSuccessがfalseなら成功扱いしない。
    """

    last_detail = (
        property_data.get(
            "lastSuccessfulDetail"
        )
    )

    if (
        isinstance(
            last_detail,
            dict,
        )
        and last_detail
    ):
        return True

    if (
        property_data.get(
            "detailFetchSuccess"
        )
        is True
        and isinstance(
            property_data.get(
                "detail"
            ),
            dict,
        )
        and property_data.get(
            "detail"
        )
    ):
        return True

    return False


def should_fetch_detail(
    property_data: Dict[str, Any],
) -> bool:

    return not has_successful_detail(
        property_data
    )


def detail_fetch_priority(
    property_data: Dict[str, Any],
) -> int:

    # 一度も成功していない物件
    if not has_successful_detail(
        property_data
    ):

        # 新規未取得
        if (
            property_data.get(
                "detailFetchSuccess"
            )
            is None
            and not property_data.get(
                "detailFetchError"
            )
        ):
            return 0

        # 過去失敗
        return 1

    return 2


def classify_detail_error(
    error: Any,
) -> str:

    if error is None:
        return "unknown"

    text = str(
        error
    ).lower()

    if (
        "invalid_suumo_url" in text
        or "invalid url" in text
        or "missing_url" in text
    ):
        return "invalid_url"

    if (
        "404" in text
        or "not found" in text
    ):
        return "not_found"

    if (
        "403" in text
        or "forbidden" in text
    ):
        return "forbidden"

    if (
        "429" in text
        or "too many requests" in text
    ):
        return "rate_limited"

    if (
        "408" in text
        or "timeout" in text
        or "timed out" in text
    ):
        return "timeout"

    if (
        "500" in text
        or "502" in text
        or "503" in text
        or "504" in text
        or "server error" in text
    ):
        return "server_error"

    if (
        "connection" in text
        or "network" in text
        or "connectionerror" in text
    ):
        return "network_error"

    if (
        "parse" in text
        or "json" in text
        or "selector" in text
        or "html" in text
    ):
        return "parse_error"

    return "fetch_error"


def should_retry_detail(
    error_type: str,
) -> bool:

    return (
        error_type
        in RETRYABLE_DETAIL_ERROR_TYPES
    )


# ============================================================
# Detail fetch
# ============================================================

def fetch_detail_for_property(
    property_data: Dict[str, Any],
    detail_adapter: SuumoDetailAdapter,
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    original_url = (
        property_data.get(
            "url"
        )
        or property_data.get(
            "sourceUrl"
        )
    )

    url = normalize_suumo_listing_url(
        original_url
    )

    if not url:

        property_data[
            "detailFetchSuccess"
        ] = False

        property_data[
            "detailFetchStatus"
        ] = "failed"

        property_data[
            "detailFetchError"
        ] = "missing_or_invalid_suumo_url"

        property_data[
            "detailFetchErrorType"
        ] = "invalid_url"

        property_data[
            "lastDetailFetchAt"
        ] = now_iso()

        return property_data

    # --------------------------------------------------------
    # Canonical URLを必ず保存
    # --------------------------------------------------------

    property_data[
        "url"
    ] = url

    property_data[
        "sourceUrl"
    ] = url

    property_data[
        "detailFetchedUrl"
    ] = url

    # --------------------------------------------------------
    # Validate
    # --------------------------------------------------------

    if not is_valid_suumo_listing_url(
        url
    ):

        property_data[
            "detailFetchSuccess"
        ] = False

        property_data[
            "detailFetchStatus"
        ] = "failed"

        property_data[
            "detailFetchError"
        ] = "invalid_suumo_url"

        property_data[
            "detailFetchErrorType"
        ] = "invalid_url"

        property_data[
            "lastDetailFetchAt"
        ] = now_iso()

        return property_data

    # --------------------------------------------------------
    # Run attempts
    # --------------------------------------------------------

    run_attempts = 0
    successful = False
    result: Dict[str, Any] = {}
    last_error: Optional[str] = None
    last_error_type = "unknown"

    for attempt in range(
        1,
        MAX_DETAIL_FETCH_ATTEMPTS + 1,
    ):

        run_attempts = attempt

        print(
            f"[DETAIL] "
            f"id={get_property_id(property_data)} "
            f"attempt={attempt}/"
            f"{MAX_DETAIL_FETCH_ATTEMPTS} "
            f"url={url}"
        )

        try:

            adapter_result = (
                detail_adapter.fetch_detail(
                    url
                )
            )

            if not isinstance(
                adapter_result,
                dict,
            ):

                result = {
                    "success": False,
                    "detail": None,
                    "error":
                        "invalid_adapter_result",
                }

            else:

                result = adapter_result

        except Exception as exc:

            result = {
                "success": False,
                "detail": None,
                "error": str(exc),
            }

        # ----------------------------------------------------
        # Success
        # ----------------------------------------------------

        if (
            result.get(
                "success"
            )
            and isinstance(
                result.get(
                    "detail"
                ),
                dict,
            )
            and result.get(
                "detail"
            )
        ):

            successful = True
            break

        # ----------------------------------------------------
        # Failure
        # ----------------------------------------------------

        last_error = str(
            result.get(
                "error"
            )
            or "unknown_detail_error"
        )

        last_error_type = classify_detail_error(
            last_error
        )

        print(
            f"[DETAIL] failed "
            f"type={last_error_type} "
            f"error={last_error[:300]}"
        )

        # 404 / invalid URL / parse error等は
        # 再試行しても改善しないので即終了
        if not should_retry_detail(
            last_error_type
        ):
            break

        if attempt < MAX_DETAIL_FETCH_ATTEMPTS:

            wait_seconds = (
                2 ** (attempt - 1)
            )

            print(
                f"[DETAIL] "
                f"retry in {wait_seconds}s"
            )

            time.sleep(
                wait_seconds
            )

    # --------------------------------------------------------
    # Attempt counters
    # --------------------------------------------------------

    previous_attempts = to_number(
        property_data.get(
            "detailFetchAttempts"
        )
    )

    if previous_attempts is None:
        previous_attempts = 0

    property_data[
        "detailFetchAttempts"
    ] = int(
        previous_attempts + run_attempts
    )

    property_data[
        "detailFetchRunAttempts"
    ] = run_attempts

    fetched_at = (
        result.get(
            "fetchedAt"
        )
        or now_iso()
    )

    property_data[
        "lastDetailFetchAt"
    ] = fetched_at

    # ========================================================
    # Success
    # ========================================================

    if successful:

        detail = deepcopy(
            result[
                "detail"
            ]
        )

        # ----------------------------------------------------
        # Detail URL normalization
        # ----------------------------------------------------

        detail_url = (
            detail.get(
                "url"
            )
        )

        if detail_url:

            normalized_detail_url = (
                normalize_suumo_listing_url(
                    detail_url
                )
            )

            if normalized_detail_url:

                detail[
                    "url"
                ] = normalized_detail_url

        detail[
            "sourceUrl"
        ] = url

        # ----------------------------------------------------
        # Parent
        # ----------------------------------------------------

        property_data[
            "detail"
        ] = deepcopy(
            detail
        )

        property_data[
            "lastSuccessfulDetail"
        ] = deepcopy(
            detail
        )

        property_data[
            "detailFetchSuccess"
        ] = True

        property_data[
            "detailFetchStatus"
        ] = "success"

        property_data[
            "detailFetchError"
        ] = None

        property_data[
            "detailFetchErrorType"
        ] = None

        property_data[
            "detailFetchedAt"
        ] = fetched_at

        property_data[
            "detailParserVersion"
        ] = detail.get(
            "parserVersion"
        )

        property_data[
            "detailFetchParserVersion"
        ] = MAIN_PARSER_VERSION

        return property_data

    # ========================================================
    # Failure
    # ========================================================

    error = (
        last_error
        or result.get(
            "error"
        )
        or "unknown_detail_error"
    )

    error_text = str(
        error
    )

    if len(error_text) > 1000:

        error_text = (
            error_text[:1000]
            + "..."
        )

    error_type = (
        last_error_type
        if last_error
        else classify_detail_error(
            error_text
        )
    )

    property_data[
        "detailFetchSuccess"
    ] = False

    property_data[
        "detailFetchStatus"
    ] = "failed"

    property_data[
        "detailFetchError"
    ] = error_text

    property_data[
        "detailFetchErrorType"
    ] = error_type

    property_data[
        "detailFetchParserVersion"
    ] = MAIN_PARSER_VERSION

    # --------------------------------------------------------
    # 過去に成功した詳細は絶対に消さない
    # --------------------------------------------------------

    last_successful_detail = (
        property_data.get(
            "lastSuccessfulDetail"
        )
    )

    if (
        isinstance(
            last_successful_detail,
            dict,
        )
        and last_successful_detail
    ):

        property_data[
            "detail"
        ] = deepcopy(
            last_successful_detail
        )

    return property_data


# ============================================================
# Fetch multiple details
# ============================================================

def fetch_details(
    properties: List[Dict[str, Any]],
    detail_adapter: SuumoDetailAdapter,
    limit: int,
) -> List[Dict[str, Any]]:

    if limit <= 0:

        print(
            "[DETAIL] limit=0 "
            "skip detail fetching"
        )

        return properties

    # --------------------------------------------------------
    # Fetch candidates
    # --------------------------------------------------------

    candidates = [
        item
        for item in properties
        if should_fetch_detail(item)
    ]

    # --------------------------------------------------------
    # Priority
    # --------------------------------------------------------

    candidates.sort(
        key=lambda item: (
            detail_fetch_priority(
                item
            ),
            -(
                datetime.fromisoformat(
                    (
                        item.get(
                            "lastSeenAt"
                        )
                        or item.get(
                            "discoveredAt"
                        )
                        or "1970-01-01T00:00:00+00:00"
                    ).replace(
                        "Z",
                        "+00:00",
                    )
                ).timestamp()
            )
            if (
                item.get(
                    "lastSeenAt"
                )
                or item.get(
                    "discoveredAt"
                )
            )
            else 0,
        ),
    )

    fetched = 0
    success_count = 0
    failure_count = 0

    for property_data in candidates:

        if fetched >= limit:
            break

        property_id = get_property_id(
            property_data
        )

        canonical_url = (
            normalize_suumo_listing_url(
                property_data.get(
                    "url"
                )
                or property_data.get(
                    "sourceUrl"
                )
            )
        )

        print(
            "[DETAIL]",
            f"id={property_id}",
            f"url={canonical_url}",
        )

        fetch_detail_for_property(
            property_data,
            detail_adapter,
        )

        fetched += 1

        if property_data.get(
            "detailFetchSuccess"
        ) is True:

            success_count += 1

        else:

            failure_count += 1

        # ----------------------------------------------------
        # Request interval
        # ----------------------------------------------------

        try:

            detail_adapter.wait()

        except Exception:

            pass

    remaining = max(
        0,
        len(candidates) - fetched,
    )

    print(
        f"[DETAIL] "
        f"fetched={fetched} "
        f"success={success_count} "
        f"failure={failure_count} "
        f"remaining={remaining}"
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

    return properties


# ============================================================
# Output filtering
# ============================================================

def is_displayable_property(
    property_data: Dict[str, Any],
) -> bool:

    # 詳細取得成功が前提
    if not has_successful_detail(
        property_data
    ):
        return False

    # 対象エリア
    if (
        property_data.get(
            "areaMatched"
        ) is not True
    ):
        return False

    # 条件一致
    if (
        property_data.get(
            "searchCriteriaMatched"
        ) is not True
    ):
        return False

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

    detail_success_count = 0
    detail_failure_count = 0
    detail_pending_count = 0

    detail_invalid_url_count = 0
    detail_not_found_count = 0
    detail_forbidden_count = 0
    detail_rate_limited_count = 0
    detail_timeout_count = 0
    detail_network_error_count = 0
    detail_server_error_count = 0
    detail_parse_error_count = 0
    detail_other_error_count = 0

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
        # Detail fetch
        # ----------------------------------------------------

        fetch_success = item.get(
            "detailFetchSuccess"
        )

        if fetch_success is True:

            detail_success_count += 1

        elif fetch_success is False:

            detail_failure_count += 1

            error_type = item.get(
                "detailFetchErrorType"
            )

            if error_type == "invalid_url":

                detail_invalid_url_count += 1

            elif error_type == "not_found":

                detail_not_found_count += 1

            elif error_type == "forbidden":

                detail_forbidden_count += 1

            elif error_type == "rate_limited":

                detail_rate_limited_count += 1

            elif error_type == "timeout":

                detail_timeout_count += 1

            elif error_type == "network_error":

                detail_network_error_count += 1

            elif error_type == "server_error":

                detail_server_error_count += 1

            elif error_type == "parse_error":

                detail_parse_error_count += 1

            else:

                detail_other_error_count += 1

        else:

            detail_pending_count += 1

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

        "detailSuccessCount":
            detail_success_count,

        "detailFailureCount":
            detail_failure_count,

        "detailPendingCount":
            detail_pending_count,

        "detailInvalidUrlCount":
            detail_invalid_url_count,

        "detailNotFoundCount":
            detail_not_found_count,

        "detailForbiddenCount":
            detail_forbidden_count,

        "detailRateLimitedCount":
            detail_rate_limited_count,

        "detailTimeoutCount":
            detail_timeout_count,

        "detailNetworkErrorCount":
            detail_network_error_count,

        "detailServerErrorCount":
            detail_server_error_count,

        "detailParseErrorCount":
            detail_parse_error_count,

        "detailOtherErrorCount":
            detail_other_error_count,
    }


# ============================================================
# Persistent history loader
# ============================================================

def load_discovered_history(
    path: Path,
) -> List[Dict[str, Any]]:

    if not path.exists():
        return []

    data = load_json(
        path,
        [],
    )

    # --------------------------------------------------------
    # New object format
    # --------------------------------------------------------

    if isinstance(
        data,
        dict,
    ):

        properties = data.get(
            "properties",
            [],
        )

        if isinstance(
            properties,
            list,
        ):
            return properties

        print(
            "[WARN] discovered_listings.json の "
            "properties が配列ではありません"
        )

        return []

    # --------------------------------------------------------
    # Legacy array
    # --------------------------------------------------------

    if isinstance(
        data,
        list,
    ):

        print(
            "[INFO] discovered_listings.json は旧配列形式です。"
            "今回の実行で新形式へ移行します。"
        )

        return data

    print(
        "[WARN] discovered_listings.json の形式が不正です。"
    )

    return []


# ============================================================
# Output envelope
# ============================================================

def build_output_document(
    properties: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> Dict[str, Any]:

    return {
        "updatedAt": now_iso(),
        "properties": properties,
        "summary": summary,
    }


# ============================================================
# Save
# ============================================================

def save_discovered(
    properties: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> None:

    document = build_output_document(
        properties,
        summary,
    )

    save_json(
        DISCOVERED_PATH,
        document,
    )

    print(
        f"[OUTPUT] discovered="
        f"{len(properties)}"
    )


def save_houses(
    properties: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> List[Dict[str, Any]]:

    houses = build_output(
        properties
    )

    document = build_output_document(
        houses,
        summary,
    )

    save_json(
        HOUSES_PATH,
        document,
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
        load_discovered_history(
            DISCOVERED_PATH
        )
    )

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

        print(
            "[WARN] search adapter result "
            "is not a list"
        )

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
        f"[SEARCH] "
        f"raw={len(discovered_now)} "
        f"normalized={len(normalized_now)}"
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
        f"[CONFIG] "
        f"detailFetchLimit={detail_limit}"
    )

    # --------------------------------------------------------
    # Detail fetch
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

        if has_successful_detail(
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
    # Houses
    # --------------------------------------------------------

    houses = build_output(
        properties
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = build_summary(
        properties,
        houses,
    )

    # --------------------------------------------------------
    # Save discovered
    # --------------------------------------------------------

    save_discovered(
        properties,
        summary,
    )

    # --------------------------------------------------------
    # Save houses
    # --------------------------------------------------------

    save_houses(
        properties,
        summary,
    )

    # --------------------------------------------------------
    # Standalone summary
    # --------------------------------------------------------

    save_json(
        SUMMARY_PATH,
        summary,
    )

    # --------------------------------------------------------
    # Console summary
    # --------------------------------------------------------

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