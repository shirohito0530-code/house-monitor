from __future__ import annotations

import json
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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

DEFAULT_DETAIL_FETCH_LIMIT = 50

MAX_DETAIL_FETCH_ATTEMPTS = 3

MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS = 3

RETRYABLE_DETAIL_ERROR_TYPES = {
    "forbidden",
    "rate_limited",
    "timeout",
    "network_error",
    "server_error",
}


# ============================================================
# Parser version
# ============================================================

MAIN_PARSER_VERSION = "2026-09-23-v24.1-market-db"


# ============================================================
# Fallback target area rules
# ============================================================

FALLBACK_AREA_RULES = {
    "柏の葉キャンパス": {
        "cities": [
            "柏市",
        ],
        "cityCodes": [
            "sc_kashiwa",
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
        "cityCodes": [
            "sc_nagareyama",
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
    return datetime.now(timezone.utc).isoformat()


def calculate_days_between(
    start_iso: Optional[str],
    end_iso: Optional[str] = None,
) -> Optional[int]:
    if not start_iso:
        return None

    try:
        dt_start = datetime.fromisoformat(
            str(start_iso).replace("Z", "+00:00")
        )

        if end_iso:
            dt_end = datetime.fromisoformat(
                str(end_iso).replace("Z", "+00:00")
            )
        else:
            dt_end = datetime.now(timezone.utc)

        delta = dt_end - dt_start
        return max(0, delta.days)

    except Exception:
        return None


def load_json(
    path: Path,
    default: Any,
) -> Any:
    if not path.exists():
        return deepcopy(default)

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as exc:
        print(f"[WARN] JSON読み込み失敗: {path}: {exc}")
        return deepcopy(default)


def save_json(
    path: Path,
    data: Any,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp_path.replace(path)


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()

    return text or None


def normalize_compact_text(value: Any) -> Optional[str]:
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


def to_number(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = (
        str(value)
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
        return float(match.group(0))
    except ValueError:
        return None


def to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value

    if value is None:
        return None

    text = str(value).strip().lower()

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
# SUUMO URL handling
# ============================================================

SUUMO_HOSTS = {
    "suumo.jp",
    "www.suumo.jp",
}


SUUMO_LISTING_PATH_PATTERN = re.compile(
    r"^/(?:chukoikkodate|ikkodate|mansion|chukomansion)/.+/nc_\d+(?:/)?$",
    re.IGNORECASE,
)


def repair_malformed_suumo_scheme(text: str) -> str:
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
    if url is None:
        return None

    text = str(url).strip()

    if not text:
        return None

    if text.lower().startswith(
        (
            "javascript:",
            "mailto:",
            "tel:",
            "#",
        )
    ):
        return None

    if text.startswith("//"):
        text = "https:" + text

    text = repair_malformed_suumo_scheme(text)

    if text.startswith("/"):
        text = urljoin(
            "https://suumo.jp/",
            text,
        )

    try:
        parsed = urlsplit(text)
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    if scheme not in {"http", "https"}:
        return None

    if hostname not in SUUMO_HOSTS:
        return None

    path = parsed.path or "/"

    if not re.search(
        r"/nc_\d+(?:/|$)",
        path,
        re.IGNORECASE,
    ):
        return None

    return urlunsplit(
        (
            scheme,
            parsed.netloc,
            path,
            parsed.query,
            "",
        )
    )


def normalize_url(url: Any) -> Optional[str]:
    return normalize_suumo_listing_url(url)


def is_valid_suumo_listing_url(
    url: Any,
) -> bool:
    normalized = normalize_suumo_listing_url(url)

    if not normalized:
        return False

    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return False

    hostname = (parsed.hostname or "").lower()

    if hostname not in SUUMO_HOSTS:
        return False

    path = parsed.path or ""

    return bool(
        SUUMO_LISTING_PATH_PATTERN.match(path)
    )


def extract_city_from_url(
    url: Any,
) -> Optional[str]:
    normalized = normalize_suumo_listing_url(url)

    if not normalized:
        return None

    path = urlsplit(normalized).path.lower()

    match = re.search(
        r"/sc_([a-z0-9]+)/",
        path,
    )

    if match:
        return f"sc_{match.group(1)}"

    return None


# ============================================================
# Config
# ============================================================

def load_search_config() -> Dict[str, Any]:
    config = load_json(
        SEARCH_CONFIG_PATH,
        {},
    )

    if not isinstance(config, dict):
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

    if isinstance(data, list):
        return [
            item
            for item in data
            if (
                isinstance(item, dict)
                and item.get("enabled", True) is not False
            )
        ]

    if isinstance(data, dict):
        for key in [
            "suumo_search_urls",
            "targets",
        ]:
            targets = data.get(key)

            if isinstance(targets, list):
                return [
                    item
                    for item in targets
                    if (
                        isinstance(item, dict)
                        and item.get("enabled", True) is not False
                    )
                ]

    return []


# ============================================================
# Search configuration helpers
# ============================================================

def normalize_property_type(
    value: Any,
) -> Optional[str]:
    text = clean_text(value)

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
    for key in [
        "maxBuiltAgeYears",
        "maxBuildingAgeYears",
    ]:
        value = config.get(key)

        if value is not None:
            return to_number(value)

    return None


def get_area_rules(
    config: Dict[str, Any],
) -> Dict[str, Any]:
    rules = config.get("areaRules")

    if isinstance(rules, dict) and rules:
        return rules

    return deepcopy(FALLBACK_AREA_RULES)


def get_allowed_property_types(
    config: Dict[str, Any],
) -> List[str]:
    values = config.get(
        "propertyTypes",
        [],
    )

    if not isinstance(values, list):
        return []

    result = []

    for value in values:
        normalized = normalize_property_type(value)

        if normalized:
            result.append(normalized)

    return list(
        dict.fromkeys(result)
    )


# ============================================================
# Address / Area
# ============================================================

def normalize_address_for_area(
    address: Any,
) -> Optional[str]:
    if address is None:
        return None

    text = str(address)

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
    normalized = normalize_address_for_area(
        address
    )

    if not normalized:
        return None

    area_rules = get_area_rules(
        search_config
    )

    for area, rule in area_rules.items():
        if not isinstance(rule, dict):
            continue

        cities = rule.get(
            "cities",
            [],
        )

        patterns = (
            rule.get("addressPatterns")
            or rule.get("address_patterns")
            or []
        )

        if not isinstance(cities, list):
            cities = []

        if not isinstance(patterns, list):
            patterns = []

        if cities:
            if not any(
                str(city) in normalized
                for city in cities
                if city
            ):
                continue

        if patterns:
            if not any(
                str(pattern) in normalized
                for pattern in patterns
                if pattern
            ):
                continue

        return str(area)

    return None


def normalize_search_area(
    value: Any,
) -> Optional[str]:
    text = clean_text(value)

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


# ============================================================
# URL city prefilter
# ============================================================

def apply_url_area_prefilter(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
    search_urls: List[Dict[str, Any]],
) -> Dict[str, Any]:

    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
    )

    city_code = extract_city_from_url(url)

    search_area = normalize_search_area(
        property_data.get("searchArea")
    )

    property_data["urlCityCode"] = city_code

    if not city_code:
        property_data["areaPrefilterExcluded"] = False
        property_data[
            "areaPrefilterReason"
        ] = "city_code_unknown"

        return property_data

    allowed_city_codes = set()

    for target in search_urls:
        target_area = normalize_search_area(
            target.get("area")
        )

        if target_area != search_area:
            continue

        codes = target.get(
            "allowedCityCodes"
        )

        if isinstance(codes, list):
            allowed_city_codes.update(
                str(code)
                for code in codes
                if code
            )

    if not allowed_city_codes:
        area_rules = get_area_rules(
            search_config
        )

        if search_area in area_rules:
            rule = area_rules[
                search_area
            ]

            city_codes = rule.get(
                "cityCodes",
                [],
            )

            if city_codes:
                allowed_city_codes.update(
                    str(code)
                    for code in city_codes
                    if code
                )
            else:
                cities = rule.get(
                    "cities",
                    [],
                )

                if "柏市" in cities:
                    allowed_city_codes.add(
                        "sc_kashiwa"
                    )

                if "流山市" in cities:
                    allowed_city_codes.add(
                        "sc_nagareyama"
                    )

        else:
            allowed_city_codes = {
                "sc_kashiwa",
                "sc_nagareyama",
            }

    if city_code not in allowed_city_codes:
        property_data[
            "areaPrefilterExcluded"
        ] = True

        property_data[
            "areaPrefilterReason"
        ] = f"city_mismatch_{city_code}"

    else:
        property_data[
            "areaPrefilterExcluded"
        ] = False

        property_data[
            "areaPrefilterReason"
        ] = "accepted_target_city"

    return property_data


# ============================================================
# Area evaluation
# ============================================================

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
        property_data.get("searchArea")
    )

    detected_area = detect_area_from_address(
        address,
        search_config,
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
# School district candidate classification
# ============================================================

def evaluate_school_district(
    property_data: Dict[str, Any],
) -> str:
    """
    学区の正式判定は行わない。

    柏の葉小学校区については、
    SUUMO詳細ページで番地まで取得できないケースが多いため、

        柏の葉キャンパス検索対象
        +
        柏市対象

    であれば「校区候補」として管理する。

    これは正式な学区判定ではなく、
    後続の問い合わせ・番地確認対象を示すための状態。
    """

    search_area = normalize_search_area(
        property_data.get("searchArea")
    )

    city_code = property_data.get(
        "urlCityCode"
    )

    if (
        search_area == "柏の葉キャンパス"
        and city_code in {
            None,
            "sc_kashiwa",
        }
    ):
        return "area_candidate_unverified"

    if search_area == "流山おおたかの森":
        return "not_applicable"

    return "unknown"


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

    if isinstance(detail, dict):
        candidates.extend(
            [
                detail.get(
                    "propertyType"
                ),
                detail.get(
                    "propertyTypeText"
                ),
                detail.get(
                    "type"
                ),
            ]
        )

    for value in candidates:
        normalized = normalize_property_type(
            value
        )

        if normalized:
            return normalized

    url = normalize_suumo_listing_url(
        property_data.get("sourceUrl")
        or property_data.get("url")
    )

    if url:
        path = urlsplit(
            url
        ).path.lower()

        if "/chukoikkodate/" in path:
            return "中古戸建"

        if "/ikkodate/" in path:
            return "新築戸建"

    return None


def evaluate_property_type(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    allowed = get_allowed_property_types(
        search_config
    )

    actual = detect_property_type(
        property_data
    )

    if not allowed:
        return {
            "propertyType": actual,
            "propertyTypeMatched": True,
            "propertyTypeReason": (
                "property_type_filter_not_configured"
            ),
        }

    if actual is None:
        return {
            "propertyType": None,
            "propertyTypeMatched": None,
            "propertyTypeReason": (
                "property_type_unknown"
            ),
        }

    if actual in allowed:
        return {
            "propertyType": actual,
            "propertyTypeMatched": True,
            "propertyTypeReason": (
                "property_type_allowed"
            ),
        }

    return {
        "propertyType": actual,
        "propertyTypeMatched": False,
        "propertyTypeReason": (
            "property_type_not_allowed"
        ),
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
        value = detail.get(key)

        if value:
            return str(value)

    construction_text = detail.get(
        "constructionText"
    )

    if construction_text:
        text = str(
            construction_text
        )

        match = re.search(
            r"(19\d{2}|20\d{2})"
            r"\D{0,3}"
            r"(1[0-2]|0?[1-9])"
            r"\D{0,2}"
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

    if not 1 <= month <= 12:
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
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    detail = get_detail(
        property_data
    )

    p_type = (
        normalize_property_type(
            detail.get(
                "propertyType"
            )
        )
        or normalize_property_type(
            property_data.get(
                "propertyType"
            )
        )
        or normalize_property_type(
            property_data.get(
                "searchPropertyType"
            )
        )
    )

    if p_type == "新築戸建":
        return {
            "builtAgeMatched": True,
            "builtAgeYears": 0.0,
            "builtAgeReason": (
                "new_house_exempt_from_age_limit"
            ),
        }

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
            ] = (
                "building_age_over_limit"
            )

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

    last_detail = property_data.get(
        "lastSuccessfulDetail"
    )

    if (
        isinstance(last_detail, dict)
        and last_detail
    ):
        return last_detail

    detail = property_data.get(
        "detail"
    )

    if (
        isinstance(detail, dict)
        and detail
    ):
        return detail

    return {}


def get_price(
    detail: Dict[str, Any],
) -> Optional[float]:
    return to_number(
        detail.get("price")
    )


def get_land_area(
    detail: Dict[str, Any],
) -> Optional[float]:
    return to_number(
        detail.get("landAreaM2")
    )


def get_building_area(
    detail: Dict[str, Any],
) -> Optional[float]:
    return to_number(
        detail.get("buildingAreaM2")
    )


def get_walk_minutes(
    detail: Dict[str, Any],
) -> Optional[float]:

    for key in [
        "walkMinutes",
        "stationWalkMinutes",
    ]:
        value = detail.get(key)

        if value is not None:
            return to_number(value)

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
        value = detail.get(key)

        if value is None:
            continue

        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    texts.extend(
                        str(v)
                        for v in item.values()
                        if v is not None
                    )
                else:
                    texts.append(
                        str(item)
                    )

        elif isinstance(value, dict):
            texts.extend(
                str(v)
                for v in value.values()
                if v is not None
            )

        else:
            texts.append(
                str(value)
            )

    return (
        normalize_compact_text(
            " ".join(texts)
        )
        or ""
    )


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

    if any(
        word in text
        for word in [
            "擁壁なし",
            "擁壁無",
            "擁壁無し",
            "擁壁不要",
        ]
    ):
        return False

    if any(
        word in text
        for word in [
            "擁壁",
            "よう壁",
            "ヨウヘキ",
            "高低差",
            "崖",
            "土留め",
        ]
    ):
        return True

    return None


# ============================================================
# Search criteria evaluation
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

    if (
        property_type_result[
            "propertyTypeMatched"
        ]
        is False
    ):
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
            walk_minutes <= max_walk
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
            land_area >= min_land
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
            building_area >= min_building
        )

        if not building_matched:
            reasons.append(
                "building_area_under_limit"
            )

    age_result = evaluate_built_age(
        property_data,
        search_config,
    )

    if (
        age_result[
            "builtAgeMatched"
        ]
        is False
    ):
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
        area_result["areaMatched"],
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
        "areaMatched": area_result[
            "areaMatched"
        ],
        "areaDetected": area_result[
            "areaDetected"
        ],
        "areaAddress": area_result[
            "areaAddress"
        ],
        "areaValidationReason": area_result[
            "areaValidationReason"
        ],

        "propertyType": property_type_result[
            "propertyType"
        ],
        "propertyTypeMatched": property_type_result[
            "propertyTypeMatched"
        ],
        "propertyTypeReason": property_type_result[
            "propertyTypeReason"
        ],

        "priceMatched": price_matched,
        "walkMatched": walk_matched,
        "landAreaMatched": land_matched,
        "buildingAreaMatched": building_matched,

        "builtAgeMatched": age_result[
            "builtAgeMatched"
        ],
        "builtAgeYears": age_result[
            "builtAgeYears"
        ],
        "builtAgeReason": age_result[
            "builtAgeReason"
        ],

        "flatLandMatched": flat_land_matched,
        "retainingWallMatched": retaining_wall_matched,

        "searchCriteriaMatched": matched,
        "searchCriteriaReasons": reasons,
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

    normalized = normalize_suumo_listing_url(
        url
    )

    if not normalized:
        return None

    path = urlsplit(
        normalized
    ).path or ""

    match = re.search(
        r"/nc_(\d+)(?:/|$)",
        path,
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
        property_data.get("sourceUrl")
        or property_data.get("url")
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
            return str(value)

    return None


# ============================================================
# Current search result normalization
# ============================================================

def normalize_search_result(
    item: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    if not isinstance(item, dict):
        return None

    original_url = (
        item.get("sourceUrl")
        or item.get("url")
        or item.get("href")
    )

    source_url = normalize_suumo_listing_url(
        original_url
    )

    if not source_url:
        return None

    property_id = (
        extract_nc_id(source_url)
        or item.get("id")
        or item.get("propertyId")
        or item.get("listingId")
    )

    result = deepcopy(item)

    if original_url:
        result[
            "sourceUrlOriginal"
        ] = str(
            original_url
        ).strip()

    result[
        "sourceUrl"
    ] = source_url

    result[
        "url"
    ] = source_url

    if property_id:
        result[
            "id"
        ] = str(property_id)

    if not result.get(
        "propertyType"
    ):
        path = urlsplit(
            source_url
        ).path.lower()

        if "/chukoikkodate/" in path:
            result[
                "propertyType"
            ] = "中古戸建"

        elif "/ikkodate/" in path:
            result[
                "propertyType"
            ] = "新築戸建"

    current_time = now_iso()

    if not result.get(
        "discoveredAt"
    ):
        result[
            "discoveredAt"
        ] = current_time

    # ここは「今回の検索結果」にだけ適用する。
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
# Historical record normalization
# ============================================================

def normalize_historical_record(
    item: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    既存履歴を読み込む際の正規化。

    重要:
    normalize_search_result() と違い、
    lastSeenAt / discoveredAt を現在時刻で上書きしない。
    """

    if not isinstance(item, dict):
        return None

    result = deepcopy(item)

    url = (
        result.get("sourceUrl")
        or result.get("url")
    )

    normalized_url = normalize_suumo_listing_url(
        url
    )

    if normalized_url:
        result[
            "sourceUrl"
        ] = normalized_url

        result[
            "url"
        ] = normalized_url

    property_id = get_property_id(
        result
    )

    if not property_id:
        return None

    result[
        "id"
    ] = str(property_id)

    first_seen = (
        result.get("firstSeen")
        or result.get(
            "firstDiscoveredAt"
        )
        or result.get(
            "discoveredAt"
        )
    )

    if first_seen:
        result[
            "firstSeen"
        ] = first_seen

        result[
            "firstDiscoveredAt"
        ] = first_seen

    if not result.get(
        "discoveryCount"
    ):
        result[
            "discoveryCount"
        ] = 1

    result[
        "seenThisRun"
    ] = False

    return result


# ============================================================
# Search-level filtering
# ============================================================

def apply_search_result_filter(
    properties: List[Dict[str, Any]],
    search_config: Dict[str, Any],
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, int],
]:

    allowed_types = get_allowed_property_types(
        search_config
    )

    max_age = get_search_max_age(
        search_config
    )

    counters = {
        "input": len(properties),
        "accepted": 0,
        "propertyTypeExcluded": 0,
        "builtAgeExcluded": 0,
        "unknownPropertyType": 0,
        "unknownBuiltAge": 0,
    }

    result: List[Dict[str, Any]] = []

    for item in properties:
        property_data = deepcopy(
            item
        )

        property_type = detect_property_type(
            property_data
        )

        if property_type is None:
            counters[
                "unknownPropertyType"
            ] += 1

        elif (
            allowed_types
            and property_type
            not in allowed_types
        ):
            counters[
                "propertyTypeExcluded"
            ] += 1

            property_data[
                "searchResultFilterExcluded"
            ] = True

            property_data[
                "searchResultFilterReason"
            ] = (
                "property_type_not_allowed"
            )

            continue

        if property_type == "新築戸建":
            property_data[
                "searchResultFilterExcluded"
            ] = False

            property_data[
                "searchResultFilterReason"
            ] = "accepted_new_house"

            counters[
                "accepted"
            ] += 1

            result.append(
                property_data
            )

            continue

        if max_age is not None:
            built_age = to_number(
                property_data.get(
                    "builtAgeYears"
                )
            )

            if built_age is None:
                built_year = (
                    property_data.get(
                        "builtYear"
                    )
                    or property_data.get(
                        "constructionYear"
                    )
                )

                built_year_number = to_number(
                    built_year
                )

                if built_year_number is not None:
                    current_year = (
                        datetime.now(
                            timezone.utc
                        ).year
                    )

                    built_age = (
                        current_year
                        - int(
                            built_year_number
                        )
                    )

            if built_age is None:
                counters[
                    "unknownBuiltAge"
                ] += 1

            elif built_age > max_age:
                counters[
                    "builtAgeExcluded"
                ] += 1

                property_data[
                    "searchResultFilterExcluded"
                ] = True

                property_data[
                    "searchResultFilterReason"
                ] = (
                    "building_age_over_limit"
                )

                continue

        counters[
            "accepted"
        ] += 1

        property_data[
            "searchResultFilterExcluded"
        ] = False

        property_data[
            "searchResultFilterReason"
        ] = (
            "accepted_for_detail_verification"
        )

        result.append(
            property_data
        )

    return result, counters


# ============================================================
# Price history & Lifecycle DB
# ============================================================

def update_price_and_lifecycle(
    property_data: Dict[str, Any],
    seen_this_run: bool,
    search_healthy: bool,
    now_ts: str,
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # First Seen
    # --------------------------------------------------------

    first_seen = (
        property_data.get(
            "firstSeen"
        )
        or property_data.get(
            "firstDiscoveredAt"
        )
        or property_data.get(
            "discoveredAt"
        )
        or now_ts
    )

    property_data[
        "firstSeen"
    ] = first_seen

    property_data[
        "firstDiscoveredAt"
    ] = first_seen

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    was_status = property_data.get(
        "status"
    )

    if seen_this_run:
        property_data[
            "lastSeen"
        ] = now_ts

        property_data[
            "lastSeenAt"
        ] = now_ts

        property_data[
            "status"
        ] = "active"

        property_data[
            "seenThisRun"
        ] = True

        property_data[
            "observedEndedAt"
        ] = None

    else:
        property_data[
            "seenThisRun"
        ] = False

        if search_healthy:
            if was_status != "observedEnded":
                property_data[
                    "observedEndedAt"
                ] = now_ts

            property_data[
                "status"
            ] = "observedEnded"

    # --------------------------------------------------------
    # Listing days
    # --------------------------------------------------------

    listing_end = (
        property_data.get(
            "observedEndedAt"
        )
        if property_data.get(
            "status"
        ) == "observedEnded"
        else property_data.get(
            "lastSeen"
        )
    )

    if listing_end:
        property_data[
            "listingDays"
        ] = calculate_days_between(
            first_seen,
            listing_end,
        )
    else:
        property_data[
            "listingDays"
        ] = calculate_days_between(
            first_seen,
            now_ts,
        )

    # --------------------------------------------------------
    # School district candidate
    # --------------------------------------------------------

    property_data[
        "schoolDistrictStatus"
    ] = evaluate_school_district(
        property_data
    )

    if (
        property_data[
            "schoolDistrictStatus"
        ]
        == "area_candidate_unverified"
    ):
        property_data[
            "schoolDistrictVerificationRequired"
        ] = True

        property_data[
            "schoolDistrictVerificationNote"
        ] = (
            "柏の葉キャンパス検索対象を"
            "柏の葉小学校区候補として扱う。"
            "番地による正式な学区判定は未実施。"
            "購入検討時に売主・仲介会社等への"
            "問い合わせ確認が必要。"
        )

    else:
        property_data[
            "schoolDistrictVerificationRequired"
        ] = False

        property_data[
            "schoolDistrictVerificationNote"
        ] = None

    # --------------------------------------------------------
    # Area exclusion reason
    # --------------------------------------------------------

    property_data[
        "areaExcludedReason"
    ] = assign_area_excluded_reason(
        property_data
    )

    # --------------------------------------------------------
    # Price history
    # --------------------------------------------------------

    detail = get_detail(
        property_data
    )

    price_yen = get_price(
        detail
    )

    if price_yen is None or price_yen <= 0:
        return property_data

    price_man = round(
        price_yen / 10_000,
        2,
    )

    history = property_data.get(
        "priceHistory"
    )

    if not isinstance(
        history,
        list,
    ):
        history = []

    last_price_yen = None

    if history:
        last_entry = history[-1]

        if isinstance(
            last_entry,
            dict,
        ):
            last_price_yen = to_number(
                last_entry.get(
                    "price"
                )
            )

    # --------------------------------------------------------
    # Initial price
    # --------------------------------------------------------

    if (
        property_data.get(
            "initialPrice"
        )
        is None
    ):
        property_data[
            "initialPrice"
        ] = price_yen

        property_data[
            "initialPriceMan"
        ] = price_man

        property_data[
            "firstPrice"
        ] = price_yen

        property_data[
            "firstPriceMan"
        ] = price_man

    # --------------------------------------------------------
    # Price history append
    # --------------------------------------------------------

    if (
        last_price_yen is None
        or last_price_yen != price_yen
    ):
        history.append(
            {
                "date": datetime.now(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d"
                ),
                "recordedAt": now_ts,
                "price": price_yen,
                "priceMan": price_man,
            }
        )

    property_data[
        "priceHistory"
    ] = history

    property_data[
        "currentPrice"
    ] = price_yen

    property_data[
        "currentPriceMan"
    ] = price_man

    # --------------------------------------------------------
    # Reduction count
    # --------------------------------------------------------

    init_price = (
        to_number(
            property_data.get(
                "initialPrice"
            )
        )
        or price_yen
    )

    reductions = 0
    previous_price = None

    for entry in history:
        if not isinstance(
            entry,
            dict,
        ):
            continue

        current_history_price = to_number(
            entry.get(
                "price"
            )
        )

        if (
            current_history_price is not None
            and previous_price is not None
            and current_history_price
            < previous_price
        ):
            reductions += 1

        if current_history_price is not None:
            previous_price = (
                current_history_price
            )

    property_data[
        "priceReductionCount"
    ] = reductions

    total_reduction = max(
        0.0,
        init_price - price_yen,
    )

    property_data[
        "totalPriceReduction"
    ] = total_reduction

    property_data[
        "totalPriceReductionMan"
    ] = round(
        total_reduction / 10_000,
        2,
    )

    if init_price > 0:
        property_data[
            "priceReductionRate"
        ] = round(
            (
                total_reduction
                / init_price
            )
            * 100,
            2,
        )
    else:
        property_data[
            "priceReductionRate"
        ] = 0.0

    return property_data


# ============================================================
# Area exclusion reason
# ============================================================

def assign_area_excluded_reason(
    property_data: Dict[str, Any],
) -> Optional[str]:

    prefilter_excluded = property_data.get(
        "areaPrefilterExcluded",
        False,
    )

    prefilter_reason = property_data.get(
        "areaPrefilterReason"
    )

    if prefilter_excluded:
        if (
            prefilter_reason
            and "city_mismatch"
            in prefilter_reason
        ):
            return "cityMismatch"

        return "cityMismatch"

    area_matched = property_data.get(
        "areaMatched"
    )

    area_val_reason = property_data.get(
        "areaValidationReason"
    )

    if area_matched is False:
        if area_val_reason in {
            "address_outside_target_area",
            "search_area_address_mismatch",
        }:
            return "addressMismatch"

        return "addressMismatch"

    if area_matched is None:
        if (
            area_val_reason
            == "address_unavailable"
        ):
            return "detailPending"

        return "unknown"

    return None


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

    first_discovered = (
        old.get(
            "firstDiscoveredAt"
        )
        or old.get(
            "firstSeen"
        )
        or old.get(
            "discoveredAt"
        )
        or new.get(
            "discoveredAt"
        )
    )

    for key, value in new.items():

        if key in {
            "detail",
            "lastSuccessfulDetail",
        }:
            continue

        if value is not None:
            merged[key] = value

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

    new_last_detail = new.get(
        "lastSuccessfulDetail"
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

    if first_discovered:
        merged[
            "firstDiscoveredAt"
        ] = first_discovered

        merged[
            "firstSeen"
        ] = first_discovered

    # 今回の検索結果に登場した場合のみ
    # discoveryCount を増やす
    if new.get(
        "seenThisRun"
    ):
        old_count = (
            to_number(
                old.get(
                    "discoveryCount"
                )
            )
            or 0
        )

        merged[
            "discoveryCount"
        ] = int(
            old_count + 1
        )

    # sourceUrl は今回検索で取得したURLを優先
    new_source_url = new.get(
        "sourceUrl"
    )

    if new_source_url:
        validated = normalize_suumo_listing_url(
            new_source_url
        )

        if validated:
            merged[
                "sourceUrl"
            ] = validated

            merged[
                "url"
            ] = validated

    else:
        old_source_url = merged.get(
            "sourceUrl"
        )

        validated = normalize_suumo_listing_url(
            old_source_url
        )

        if validated:
            merged[
                "sourceUrl"
            ] = validated

            merged[
                "url"
            ] = validated

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
    # Existing history
    # --------------------------------------------------------

    for item in existing:

        normalized = normalize_historical_record(
            item
        )

        if not normalized:
            continue

        key = get_property_id(
            normalized
        )

        if not key:
            continue

        normalized[
            "seenThisRun"
        ] = False

        if key in by_id:
            by_id[key] = merge_property(
                by_id[key],
                normalized,
            )
        else:
            by_id[key] = normalized

    # --------------------------------------------------------
    # Current search results
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

        normalized[
            "seenThisRun"
        ] = True

        if key in by_id:

            by_id[key] = merge_property(
                by_id[key],
                normalized,
            )

            by_id[key][
                "seenThisRun"
            ] = True

        else:

            first_time = (
                normalized.get(
                    "discoveredAt"
                )
                or now_iso()
            )

            normalized[
                "firstDiscoveredAt"
            ] = first_time

            normalized[
                "firstSeen"
            ] = first_time

            normalized[
                "discoveryCount"
            ] = 1

            normalized[
                "status"
            ] = "active"

            by_id[key] = normalized

    return list(
        by_id.values()
    )


# ============================================================
# Detail fetch status
# ============================================================

def has_successful_detail(
    property_data: Dict[str, Any],
) -> bool:

    last_detail = property_data.get(
        "lastSuccessfulDetail"
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
    ):
        detail = property_data.get(
            "detail"
        )

        if (
            isinstance(
                detail,
                dict,
            )
            and detail
        ):
            return True

    return False


def should_fetch_detail(
    property_data: Dict[str, Any],
) -> bool:

    if property_data.get(
        "areaPrefilterExcluded",
        False,
    ):
        return False

    if property_data.get(
        "searchResultFilterExcluded",
        False,
    ):
        return False

    seen_this_run = property_data.get(
        "seenThisRun",
        False,
    )

    has_success = has_successful_detail(
        property_data
    )

    if (
        not seen_this_run
        and has_success
    ):
        return False

    if (
        seen_this_run
        and not has_success
    ):
        return True

    price_history = property_data.get(
        "priceHistory",
        [],
    )

    if (
        seen_this_run
        and isinstance(
            price_history,
            list,
        )
        and len(price_history) >= 2
    ):
        return True

    if (
        seen_this_run
        and property_data.get(
            "detailFetchErrorType"
        )
        in RETRYABLE_DETAIL_ERROR_TYPES
    ):
        return True

    return not has_success


def detail_fetch_priority(
    property_data: Dict[str, Any],
) -> int:

    if property_data.get(
        "areaPrefilterExcluded",
        False,
    ):
        return 999

    seen_this_run = property_data.get(
        "seenThisRun",
        False,
    )

    has_success = has_successful_detail(
        property_data
    )

    if (
        has_success
        and not seen_this_run
    ):
        return 900

    is_new = (
        property_data.get(
            "firstDiscoveredAt"
        ) is not None
        and (
            to_number(
                property_data.get(
                    "discoveryCount"
                )
            )
            == 1
        )
    )

    price_history = property_data.get(
        "priceHistory",
        [],
    )

    has_price_change = (
        isinstance(
            price_history,
            list,
        )
        and len(price_history) >= 2
    )

    has_error = (
        property_data.get(
            "detailFetchErrorType"
        )
        is not None
    )

    if (
        seen_this_run
        and not has_success
    ):
        return 0

    if is_new:
        return 1

    if has_price_change:
        return 2

    if has_error:
        return 3

    if not has_success:
        return 4

    return 5


# ============================================================
# Detail error
# ============================================================

def classify_detail_error(
    error: Any,
) -> str:

    if error is None:
        return "unknown"

    text = str(
        error
    ).lower()

    if any(
        k in text
        for k in [
            "invalid_suumo_url",
            "invalid url",
            "missing_url",
        ]
    ):
        return "invalid_url"

    if any(
        k in text
        for k in [
            "404",
            "not found",
        ]
    ):
        return "not_found"

    if any(
        k in text
        for k in [
            "403",
            "forbidden",
        ]
    ):
        return "forbidden"

    if any(
        k in text
        for k in [
            "429",
            "too many requests",
        ]
    ):
        return "rate_limited"

    if any(
        k in text
        for k in [
            "408",
            "timeout",
            "timed out",
        ]
    ):
        return "timeout"

    if any(
        k in text
        for k in [
            "500",
            "502",
            "503",
            "504",
            "server error",
        ]
    ):
        return "server_error"

    if any(
        k in text
        for k in [
            "connection",
            "network",
            "connectionerror",
        ]
    ):
        return "network_error"

    if any(
        k in text
        for k in [
            "parse",
            "json",
            "selector",
            "html",
        ]
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

    original_url = (
        property_data.get(
            "sourceUrl"
        )
        or property_data.get(
            "url"
        )
    )

    if (
        original_url
        and not property_data.get(
            "sourceUrlOriginal"
        )
    ):
        property_data[
            "sourceUrlOriginal"
        ] = str(
            original_url
        ).strip()

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
        ] = (
            "missing_or_invalid_suumo_url"
        )

        property_data[
            "detailFetchErrorType"
        ] = "invalid_url"

        property_data[
            "lastDetailFetchAt"
        ] = now_iso()

        return property_data

    property_data[
        "sourceUrl"
    ] = url

    property_data[
        "url"
    ] = url

    property_data[
        "detailFetchedUrl"
    ] = url

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

        print(
            "[DETAIL] invalid URL:",
            url,
        )

        return property_data

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
            "[DETAIL]"
            f" id={get_property_id(property_data)}"
            f" attempt={attempt}/{MAX_DETAIL_FETCH_ATTEMPTS}"
            f" url={url}"
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
                    "error": (
                        "invalid_adapter_result"
                    ),
                }
            else:
                result = adapter_result

        except Exception as exc:
            result = {
                "success": False,
                "detail": None,
                "error": str(exc),
            }

        if (
            result.get("success")
            and isinstance(
                result.get("detail"),
                dict,
            )
            and result.get("detail")
        ):
            successful = True
            break

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
            "[DETAIL] failed"
            f" type={last_error_type}"
            f" error={last_error[:300]}"
        )

        if not should_retry_detail(
            last_error_type
        ):
            break

        if (
            attempt
            < MAX_DETAIL_FETCH_ATTEMPTS
        ):
            wait_seconds = (
                2 ** (attempt - 1)
            )

            print(
                f"[DETAIL] retry in {wait_seconds}s"
            )

            time.sleep(
                wait_seconds
            )

    previous_attempts = (
        to_number(
            property_data.get(
                "detailFetchAttempts"
            )
        )
        or 0
    )

    property_data[
        "detailFetchAttempts"
    ] = int(
        previous_attempts
        + run_attempts
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

    if successful:

        detail = deepcopy(
            result[
                "detail"
            ]
        )

        detail_url = detail.get(
            "url"
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
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, int],
]:

    run_stats = {
        "fetchedThisRun": 0,
        "successThisRun": 0,
        "failureThisRun": 0,
    }

    if limit <= 0:
        print(
            "[DETAIL] limit=0 skip detail fetching"
        )

        return properties, run_stats

    candidates = [
        item
        for item in properties
        if should_fetch_detail(item)
    ]

    def sort_timestamp(
        item: Dict[str, Any],
    ) -> float:

        value = (
            item.get("lastSeenAt")
            or item.get("discoveredAt")
        )

        if not value:
            return 0

        try:
            return datetime.fromisoformat(
                str(value).replace(
                    "Z",
                    "+00:00",
                )
            ).timestamp()

        except Exception:
            return 0

    candidates.sort(
        key=lambda item: (
            detail_fetch_priority(
                item
            ),
            -sort_timestamp(item),
        )
    )

    fetched = 0
    success_count = 0
    failure_count = 0

    consecutive_connectivity_errors = 0

    for property_data in candidates:

        if fetched >= limit:
            break

        property_id = get_property_id(
            property_data
        )

        detail_url = (
            property_data.get(
                "sourceUrl"
            )
            or property_data.get(
                "url"
            )
        )

        canonical_url = normalize_suumo_listing_url(
            detail_url
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

        if (
            property_data.get(
                "detailFetchSuccess"
            )
            is True
        ):
            success_count += 1
            consecutive_connectivity_errors = 0

        else:
            failure_count += 1

            error_type = (
                property_data.get(
                    "detailFetchErrorType"
                )
            )

            if error_type in {
                "timeout",
                "network_error",
            }:

                consecutive_connectivity_errors += 1

                print(
                    "[DETAIL]"
                    " consecutive connectivity"
                    f" errors={consecutive_connectivity_errors}"
                    f"/{MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS}"
                )

                if (
                    consecutive_connectivity_errors
                    >= MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS
                ):

                    print(
                        "[DETAIL]"
                        " SUUMO connectivity failure"
                        " threshold reached."
                        " Stopping detail fetch for"
                        " this run."
                    )

                    break

            else:
                consecutive_connectivity_errors = 0

        try:
            detail_adapter.wait()
        except Exception:
            pass

    remaining = max(
        0,
        len(candidates)
        - fetched,
    )

    run_stats[
        "fetchedThisRun"
    ] = fetched

    run_stats[
        "successThisRun"
    ] = success_count

    run_stats[
        "failureThisRun"
    ] = failure_count

    print(
        "[DETAIL]"
        f" fetched={fetched}"
        f" success={success_count}"
        f" failure={failure_count}"
        f" remaining={remaining}"
    )

    return properties, run_stats


# ============================================================
# Output filtering
# ============================================================

def is_displayable_property(
    property_data: Dict[str, Any],
) -> bool:

    # --------------------------------------------------------
    # 掲載終了観測物件は現在候補から除外
    # --------------------------------------------------------

    if property_data.get(
        "status"
    ) != "active":
        return False

    # --------------------------------------------------------
    # 詳細取得成功が必要
    # --------------------------------------------------------

    if not has_successful_detail(
        property_data
    ):
        return False

    # --------------------------------------------------------
    # エリア
    #
    # 柏の葉については正式な番地学区判定をしない。
    # 「柏の葉キャンパス検索対象 + 柏市」を
    # 校区候補として扱う。
    # --------------------------------------------------------

    area_matched = property_data.get(
        "areaMatched"
    )

    school_status = property_data.get(
        "schoolDistrictStatus"
    )

    kashiwa_area_candidate = (
        school_status
        == "area_candidate_unverified"
        and property_data.get(
            "urlCityCode"
        )
        == "sc_kashiwa"
    )

    if (
        area_matched is not True
        and not kashiwa_area_candidate
    ):
        return False

    # --------------------------------------------------------
    # その他検索条件
    # --------------------------------------------------------

    if property_data.get(
        "searchCriteriaMatched"
    ) is not True:

        # 柏の葉校区候補の場合、
        # areaMatched=False だけを理由として
        # 除外しない。
        #
        # それ以外の条件については
        # searchCriteriaMatched を維持する。

        if not kashiwa_area_candidate:
            return False

        reasons = property_data.get(
            "searchCriteriaReasons",
            [],
        )

        non_area_reasons = [
            reason
            for reason in reasons
            if reason not in {
                "address_outside_target_area",
                "search_area_address_mismatch",
            }
        ]

        if non_area_reasons:
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

    return [
        item
        for item in properties
        if is_displayable_property(item)
    ]


# ============================================================
# Summary
# ============================================================

def build_summary(
    discovered: List[Dict[str, Any]],
    houses: List[Dict[str, Any]],
    search_filter_summary: Optional[
        Dict[str, Any]
    ] = None,
    run_stats: Optional[
        Dict[str, int]
    ] = None,
    run_category_counts: Optional[
        Dict[str, int]
    ] = None,
    search_healthy: bool = True,
) -> Dict[str, Any]:

    good_count = 0
    partial_count = 0
    poor_count = 0

    active_count = 0
    observed_ended_count = 0

    price_reduction_count = 0
    total_reduction_sum_man = 0.0
    max_reduction_man = 0.0

    area_prefilter_excluded_count = 0
    area_excluded_count = 0
    area_unknown_count = 0

    school_district_candidate_count = 0
    kashiwa_leaf_candidate_count = 0
    otakanomori_count = 0

    area_excluded_reasons = {
        "cityMismatch": 0,
        "addressMismatch": 0,
        "detailPending": 0,
        "unknown": 0,
    }

    detail_with_historical_success_count = 0
    detail_failure_count = 0
    detail_pending_count = 0

    for item in discovered:

        status = item.get(
            "status",
            "active",
        )

        if status == "active":
            active_count += 1

        elif status == "observedEnded":
            observed_ended_count += 1

        if item.get(
            "areaPrefilterExcluded",
            False,
        ):
            area_prefilter_excluded_count += 1

        reason = item.get(
            "areaExcludedReason"
        )

        if reason in area_excluded_reasons:
            area_excluded_reasons[
                reason
            ] += 1

        sd_status = item.get(
            "schoolDistrictStatus"
        )

        if (
            sd_status
            == "area_candidate_unverified"
        ):
            school_district_candidate_count += 1

        search_area = normalize_search_area(
            item.get(
                "searchArea"
            )
        )

        if search_area == "柏の葉キャンパス":
            kashiwa_leaf_candidate_count += 1

        elif search_area == "流山おおたかの森":
            otakanomori_count += 1

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

        if has_successful_detail(
            item
        ):
            detail_with_historical_success_count += 1

        fetch_success = item.get(
            "detailFetchSuccess"
        )

        if fetch_success is False:
            detail_failure_count += 1

        elif (
            fetch_success is not True
            and not has_successful_detail(
                item
            )
        ):
            detail_pending_count += 1

        if item.get(
            "areaMatched"
        ) is False:
            area_excluded_count += 1

        elif item.get(
            "areaMatched"
        ) is None:
            area_unknown_count += 1

        red_count = to_number(
            item.get(
                "priceReductionCount",
                0,
            )
        ) or 0

        if red_count > 0:

            price_reduction_count += 1

            total_reduction = (
                to_number(
                    item.get(
                        "totalPriceReductionMan"
                    )
                )
                or 0.0
            )

            total_reduction_sum_man += (
                total_reduction
            )

            if (
                total_reduction
                > max_reduction_man
            ):
                max_reduction_man = (
                    total_reduction
                )

    avg_reduction_amount_man = 0.0

    if price_reduction_count > 0:
        avg_reduction_amount_man = round(
            total_reduction_sum_man
            / price_reduction_count,
            2,
        )

    fetched_this_run = (
        run_stats.get(
            "fetchedThisRun",
            0,
        )
        if run_stats
        else 0
    )

    success_this_run = (
        run_stats.get(
            "successThisRun",
            0,
        )
        if run_stats
        else 0
    )

    failure_this_run = (
        run_stats.get(
            "failureThisRun",
            0,
        )
        if run_stats
        else 0
    )

    run_cats = (
        run_category_counts
        or {}
    )

    summary = {
        "generatedAt": now_iso(),

        "parserVersion": (
            MAIN_PARSER_VERSION
        ),

        "searchHealthy": (
            search_healthy
        ),

        "discoveredCount": len(
            discovered
        ),

        "houseCount": len(
            houses
        ),

        # Detail quality
        "quality": {
            "goodCount": good_count,
            "partialCount": partial_count,
            "poorCount": poor_count,
        },

        "priceReductionCount": (
            price_reduction_count
        ),

        "areaPrefilterExcludedCount": (
            area_prefilter_excluded_count
        ),

        "areaExcludedCount": (
            area_excluded_count
        ),

        "areaUnknownCount": (
            area_unknown_count
        ),

        "currentRun": {
            "newCount": run_cats.get(
                "new",
                0,
            ),
            "existingCount": run_cats.get(
                "existing",
                0,
            ),
            "updatedCount": run_cats.get(
                "updated",
                0,
            ),
            "priceChangedCount": run_cats.get(
                "priceChanged",
                0,
            ),
            "fetchedThisRun": (
                fetched_this_run
            ),
            "successThisRun": (
                success_this_run
            ),
            "failureThisRun": (
                failure_this_run
            ),
        },

        "inventory": {
            "totalDiscoveredCount": len(
                discovered
            ),
            "activeCount": active_count,
            "observedEndedCount": (
                observed_ended_count
            ),
            "houseCount": len(
                houses
            ),
            "detailPendingCount": (
                detail_pending_count
            ),
        },

        "price": {
            "priceReductionCount": (
                price_reduction_count
            ),
            "averageReductionAmountMan": (
                avg_reduction_amount_man
            ),
            "maxReductionAmountMan": (
                max_reduction_man
            ),
        },

        "area": {
            "kashiwaLeafCandidateCount": (
                kashiwa_leaf_candidate_count
            ),
            "otakanomoriCount": (
                otakanomori_count
            ),
            "schoolDistrictCandidateCount": (
                school_district_candidate_count
            ),
            "schoolDistrictPolicy": (
                "柏の葉キャンパス検索対象を"
                "柏の葉小学校区候補として扱う。"
                "番地による正式判定は行わない。"
            ),
            "areaExcludedReasonCounts": (
                area_excluded_reasons
            ),
        },

        "detailFetchedThisRun": (
            fetched_this_run
        ),

        "detailSuccessThisRun": (
            success_this_run
        ),

        "detailFailureThisRun": (
            failure_this_run
        ),

        "detailWithHistoricalSuccess": (
            detail_with_historical_success_count
        ),

        "detailFailureCount": (
            detail_failure_count
        ),

        "detailPendingCount": (
            detail_pending_count
        ),
    }

    if search_filter_summary:
        summary[
            "searchFilterSummary"
        ] = search_filter_summary

    return summary


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
            "[WARN]"
            " discovered_listings.json"
            " の properties が配列ではありません"
        )

        return []

    if isinstance(
        data,
        list,
    ):

        print(
            "[INFO]"
            " discovered_listings.json は旧配列形式です。"
            "今回の実行で新形式へ移行します。"
        )

        return data

    print(
        "[WARN]"
        " discovered_listings.json の形式が不正です。"
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
        f"[OUTPUT] discovered={len(properties)}"
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
        f"[OUTPUT] houses={len(houses)}"
    )

    return houses


def save_summary(
    summary: Dict[str, Any],
) -> None:

    save_json(
        SUMMARY_PATH,
        summary,
    )

    print(
        "[OUTPUT] summary saved"
    )


# ============================================================
# Main
# ============================================================

def main() -> int:

    print(
        "============================================"
    )

    print(
        "House Monitor - Market DB Architecture"
    )

    print(
        f"main parser: {MAIN_PARSER_VERSION}"
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

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    search_config = load_search_config()

    search_urls = load_search_urls()

    detail_fetch_limit = int(
        search_config.get(
            "detailFetchLimit",
            DEFAULT_DETAIL_FETCH_LIMIT,
        )
    )

    print(
        "[CONFIG]",
        json.dumps(
            search_config,
            ensure_ascii=False,
        ),
    )

    print(
        f"[CONFIG] search targets={len(search_urls)}"
    )

    print(
        f"[CONFIG] detailFetchLimit={detail_fetch_limit}"
    )

    if not search_urls:
        print(
            "[FATAL]"
            " No enabled SUUMO search targets were found."
        )

        return 1

    existing_discovered = (
        load_discovered_history(
            DISCOVERED_PATH
        )
    )

    print(
        f"[HISTORY] existing={len(existing_discovered)}"
    )

    search_adapter = SuumoSearchAdapter(
        config=search_config,
        root_path=str(ROOT),
    )

    # ========================================================
    # Search
    # ========================================================

    search_healthy = False

    discovered_now = []

    try:

        discovered_now = (
            search_adapter.search(
                search_config
            )
        )

        if discovered_now is None:
            discovered_now = []

        if (
            len(discovered_now) == 0
            and len(existing_discovered) > 0
        ):

            print(
                "[WARN]"
                " Current SUUMO search returned 0 results."
                " Preserving existing history."
            )

            search_healthy = False

        else:
            search_healthy = True

    except Exception as exc:

        print(
            "[ERROR]"
            " SUUMO search failed:",
            repr(exc),
        )

        search_healthy = False

    # ========================================================
    # Normalize current search results
    # ========================================================

    normalized_now = []

    for item in (
        discovered_now or []
    ):

        normalized = (
            normalize_search_result(
                item
            )
        )

        if normalized:
            normalized_now.append(
                normalized
            )

    existing_ids = {
        get_property_id(item)
        for item in existing_discovered
        if get_property_id(item)
    }

    new_candidate_count = sum(
        1
        for item in normalized_now
        if get_property_id(item)
        not in existing_ids
    )

    overlap_count = (
        len(normalized_now)
        - new_candidate_count
    )

    print(
        "[SEARCH]"
        f" 検索取得候補: {len(discovered_now)}"
        f" | 正規化済み: {len(normalized_now)}"
        f" | 既存履歴との重複: {overlap_count}"
        f" | 今回の新規候補: {new_candidate_count}"
    )

    if (
        len(discovered_now) > 0
        and len(normalized_now) == 0
    ):

        print(
            "[FATAL]"
            " Search returned candidates,"
            " but all were rejected during normalization."
        )

        return 1

    # ========================================================
    # Search filter
    # ========================================================

    filtered_now, search_filter_summary = (
        apply_search_result_filter(
            normalized_now,
            search_config,
        )
    )

    print(
        "[SEARCH FILTER]",
        json.dumps(
            search_filter_summary,
            ensure_ascii=False,
        ),
    )

    # ========================================================
    # Merge history
    # ========================================================

    properties = (
        merge_discovered_listings(
            existing_discovered,
            filtered_now,
        )
    )

    print(
        f"[MERGE] total history properties={len(properties)}"
    )

    # ========================================================
    # URL city prefilter
    # ========================================================

    prefilter_excluded_count = 0

    for item in properties:

        apply_url_area_prefilter(
            item,
            search_config,
            search_urls,
        )

        if item.get(
            "areaPrefilterExcluded",
            False,
        ):
            prefilter_excluded_count += 1

    print(
        "[PREFILTER]"
        f" URL市区町村プリフィルター除外件数:"
        f" {prefilter_excluded_count}件"
        f" / 全{len(properties)}件"
    )

    # ========================================================
    # Detail crawl
    # ========================================================

    run_stats = {
        "fetchedThisRun": 0,
        "successThisRun": 0,
        "failureThisRun": 0,
    }

    if search_healthy:

        detail_adapter = SuumoDetailAdapter(
            config=search_config,
            root_path=str(ROOT),
        )

        properties, run_stats = (
            fetch_details(
                properties,
                detail_adapter,
                limit=detail_fetch_limit,
            )
        )

    else:

        print(
            "[DETAIL]"
            " Skipped because current search was unhealthy."
        )

    # ========================================================
    # Evaluate criteria
    # ========================================================

    properties = apply_search_criteria(
        properties,
        search_config,
    )

    # ========================================================
    # Lifecycle / price DB / school candidate
    # ========================================================

    now_ts = now_iso()

    run_category_counts = {
        "new": 0,
        "existing": 0,
        "updated": 0,
        "priceChanged": 0,
    }

    for item in properties:

        p_id = get_property_id(
            item
        )

        is_new = (
            p_id not in existing_ids
            and item.get(
                "seenThisRun",
                False,
            )
        )

        hist_before = item.get(
            "priceHistory"
        )

        hist_before_len = (
            len(hist_before)
            if isinstance(
                hist_before,
                list,
            )
            else 0
        )

        old_price = (
            item.get(
                "currentPrice"
            )
        )

        old_status = item.get(
            "status"
        )

        update_price_and_lifecycle(
            item,
            seen_this_run=item.get(
                "seenThisRun",
                False,
            ),
            search_healthy=search_healthy,
            now_ts=now_ts,
        )

        hist_after = item.get(
            "priceHistory"
        )

        hist_after_len = (
            len(hist_after)
            if isinstance(
                hist_after,
                list,
            )
            else 0
        )

        new_price = item.get(
            "currentPrice"
        )

        if is_new:

            item[
                "runCategory"
            ] = "new"

            run_category_counts[
                "new"
            ] += 1

        elif (
            old_price is not None
            and new_price is not None
            and to_number(old_price)
            != to_number(new_price)
        ):

            item[
                "runCategory"
            ] = "priceChanged"

            run_category_counts[
                "priceChanged"
            ] += 1

        elif (
            old_status != item.get(
                "status"
            )
            or item.get(
                "detailFetchedAt"
            ) == item.get(
                "lastDetailFetchAt"
            )
        ):

            item[
                "runCategory"
            ] = "updated"

            run_category_counts[
                "updated"
            ] += 1

        else:

            item[
                "runCategory"
            ] = "existing"

            run_category_counts[
                "existing"
            ] += 1

    # ========================================================
    # Output
    # ========================================================

    houses = build_output(
        properties
    )

    summary = build_summary(
        properties,
        houses,
        search_filter_summary=(
            search_filter_summary
        ),
        run_stats=run_stats,
        run_category_counts=(
            run_category_counts
        ),
        search_healthy=search_healthy,
    )

    print(
        "[SUMMARY]",
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
    )

    save_discovered(
        properties,
        summary,
    )

    save_houses(
        properties,
        summary,
    )

    save_summary(
        summary
    )

    print(
        "============================================"
    )

    print(
        "Done."
    )

    print(
        "============================================"
    )

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )