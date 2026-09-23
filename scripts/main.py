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
    try:
        from scripts.adapters.suumo_search import SuumoSearchAdapter
        from scripts.adapters.suumo_detail import SuumoDetailAdapter
    except ImportError:
        from suumo_search import SuumoSearchAdapter
        from suumo_detail import SuumoDetailAdapter

try:
    from identity import (
        get_source_id,
        make_property_id,
        make_identity_key,
    )
except ImportError:
    from scripts.identity import (
        get_source_id,
        make_property_id,
        make_identity_key,
    )


# ============================================================
# Constants
# ============================================================

MAIN_PARSER_VERSION = "2026-09-24-v25.1-market-db"

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

SEARCH_CONFIG_PATH = CONFIG_DIR / "search.json"
SEARCH_URLS_PATH = CONFIG_DIR / "search_urls.json"

DISCOVERED_PATH = DATA_DIR / "discovered_listings.json"
OBSERVATIONS_PATH = DATA_DIR / "listing_observations.json"
HOUSES_PATH = DATA_DIR / "houses.json"
SUMMARY_PATH = DATA_DIR / "summary.json"

DEFAULT_DETAIL_FETCH_LIMIT = 400

MAX_DETAIL_FETCH_ATTEMPTS = 3

MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS = 3

DETAIL_REFRESH_DAYS_ACTIVE = 2
DETAIL_REFRESH_DAYS_ENDED = 14

RETRYABLE_DETAIL_ERROR_TYPES = {
    "forbidden",
    "rate_limited",
    "timeout",
    "network_error",
    "server_error",
}


# ============================================================
# Fallback target area rules
# ============================================================

FALLBACK_AREA_RULES = {
    "柏の葉キャンパス": {
        "cities": [
            "柏市",
            "流山市",
        ],
        "cityCodes": [
            "sc_kashiwa",
            "sc_nagareyama",
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
            "柏市",
        ],
        "cityCodes": [
            "sc_nagareyama",
            "sc_kashiwa",
        ],
        "addressPatterns": [
            "おおたかの森北",
            "おおたかの森西",
            "おおたかの森東",
            "おおたかの森南",
            "西初石",
            "市野谷",
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
        print(
            f"[WARN] JSON読み込み失敗: {path}: {exc}"
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

    if isinstance(value, dict):
        value = (
            value.get("name")
            or value.get("text")
            or value.get("label")
            or str(value)
        )

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
        text.replace("　", "")
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


def to_bool(
    value: Any,
) -> Optional[bool]:

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
# SUUMO URL Handling
# ============================================================

SUUMO_HOSTS = {
    "suumo.jp",
    "www.suumo.jp",
}

SUUMO_LISTING_PATH_PATTERN = re.compile(
    r"^/(?:chukoikkodate|ikkodate|mansion|chukomansion)/.+/nc_\d+(?:/)?$",
    re.IGNORECASE,
)


def repair_malformed_suumo_scheme(
    text: str,
) -> str:

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

    if scheme not in {
        "http",
        "https",
    }:
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
# Config Helpers
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

    if not isinstance(values, list):
        return []

    result = []

    for value in values:

        normalized = normalize_property_type(
            value
        )

        if normalized:
            result.append(normalized)

    return list(
        dict.fromkeys(result)
    )


# ============================================================
# Identity
# ============================================================

def get_property_id(
    property_data: Dict[str, Any],
) -> Optional[str]:

    return make_property_id(
        property_data
    )


# ============================================================
# Area & Address Logic
# ============================================================

def normalize_address_for_area(
    address: Any,
) -> Optional[str]:

    if address is None:
        return None

    text = clean_text(address)

    if not text:
        return None

    text = (
        text.replace("　", "")
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


def apply_url_area_prefilter(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
    search_urls: List[Dict[str, Any]],
) -> Dict[str, Any]:

    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
    )

    city_code = extract_city_from_url(
        url
    )

    search_area = normalize_search_area(
        property_data.get("searchArea")
    )

    property_data["urlCityCode"] = city_code

    if not city_code:

        property_data[
            "areaPrefilterExcluded"
        ] = False

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
                str(c)
                for c in codes
                if c
            )

    if not allowed_city_codes:

        area_rules = get_area_rules(
            search_config
        )

        if search_area:

            rule = area_rules.get(
                search_area
            )

            if isinstance(rule, dict):

                configured_codes = rule.get(
                    "cityCodes"
                )

                if isinstance(
                    configured_codes,
                    list,
                ):

                    allowed_city_codes.update(
                        str(c)
                        for c in configured_codes
                        if c
                    )

    if not allowed_city_codes:

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
# Detail Helpers
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


def has_successful_detail(
    property_data: Dict[str, Any],
) -> bool:

    last_successful = property_data.get(
        "lastSuccessfulDetail"
    )

    if (
        isinstance(last_successful, dict)
        and last_successful
    ):
        return True

    detail = property_data.get(
        "detail"
    )

    if (
        isinstance(detail, dict)
        and detail.get("success") is True
    ):
        return True

    return (
        property_data.get(
            "detailFetchStatus"
        )
        == "success"
    )


def is_detail_stale(
    property_data: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:

    if now is None:
        now = datetime.now(timezone.utc)

    last_fetch = property_data.get(
        "lastDetailFetchAt"
    )

    if not last_fetch:
        return True

    try:
        fetched_at = datetime.fromisoformat(
            str(last_fetch).replace(
                "Z",
                "+00:00",
            )
        )

        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(
                tzinfo=timezone.utc
            )

    except Exception:
        return True

    status = property_data.get(
        "status"
    )

    refresh_days = (
        DETAIL_REFRESH_DAYS_ENDED
        if status == "observed_ended"
        else DETAIL_REFRESH_DAYS_ACTIVE
    )

    elapsed = (
        now - fetched_at
    ).total_seconds()

    return (
        elapsed
        >= refresh_days * 86400
    )


def detail_fetch_priority(
    property_data: Dict[str, Any],
) -> int:

    if (
        property_data.get(
            "areaPrefilterExcluded",
            False,
        )
        or property_data.get(
            "searchResultFilterExcluded",
            False,
        )
    ):
        return 999

    attempts = int(
        property_data.get(
            "detailFetchAttempts",
            0,
        )
        or 0
    )

    error_type = property_data.get(
        "detailFetchErrorType"
    )

    status = property_data.get(
        "status"
    )

    has_success = has_successful_detail(
        property_data
    )

    seen = property_data.get(
        "seenThisRun",
        False,
    )

    # 新規物件は最優先
    if (
        seen
        and not has_success
        and attempts == 0
    ):
        return 0

    # 現在activeで詳細未取得
    if (
        status == "active"
        and not has_success
    ):
        if (
            attempts < MAX_DETAIL_FETCH_ATTEMPTS
            or error_type in RETRYABLE_DETAIL_ERROR_TYPES
        ):
            return 1

    # エリア判定に必要な詳細未取得
    if not has_success:

        if (
            property_data.get(
                "areaClassification"
            )
            in {
                None,
                "",
                "unknown",
                "subTarget",
            }
        ):
            return 2

    # stale詳細
    if (
        has_success
        and is_detail_stale(
            property_data
        )
    ):
        return 3

    # retryable error
    if (
        error_type
        in RETRYABLE_DETAIL_ERROR_TYPES
        and attempts < MAX_DETAIL_FETCH_ATTEMPTS
    ):
        return 4

    return 999


# ============================================================
# Property Type & Construction Age
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
        property_data.get(
            "sourceUrl"
        )
        or property_data.get(
            "url"
        )
    )

    if url:

        path = urlsplit(url).path.lower()

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
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    detail = get_detail(
        property_data
    )

    p_type = (
        normalize_property_type(
            detail.get("propertyType")
        )
        or normalize_property_type(
            property_data.get("propertyType")
        )
        or normalize_property_type(
            property_data.get("searchPropertyType")
        )
    )

    if p_type == "新築戸建":

        return {
            "builtAgeMatched": True,
            "builtAgeYears": 0.0,
            "builtAgeStatus": "confirmed",
            "builtAgeReason":
                "new_house_exempt_from_age_limit",
        }

    max_age = get_search_max_age(
        search_config
    )

    result = {
        "builtAgeMatched": None,
        "builtAgeYears": None,
        "builtAgeStatus": "unknown",
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
        get_construction_month(detail)
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

        result[
            "builtAgeStatus"
        ] = (
            "confirmed"
            if len(construction_month) >= 7
            else "estimated"
        )

        if age_number is None:

            result[
                "builtAgeReason"
            ] = "construction_date_unparseable"

            result[
                "builtAgeStatus"
            ] = "unknown"

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

    result[
        "builtAgeReason"
    ] = "construction_date_unavailable"

    result[
        "builtAgeStatus"
    ] = "unknown"

    return result


# ============================================================
# Scalar Detail Getters
# ============================================================

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
# Area Evaluation
# ============================================================

def evaluate_area(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    detail = get_detail(
        property_data
    )

    address = clean_text(
        detail.get("address")
    )

    search_area = normalize_search_area(
        property_data.get(
            "searchArea"
        )
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

        result["areaMatched"] = False

        result[
            "areaValidationReason"
        ] = "address_outside_target_area"

        return result

    if not search_area:

        result["areaMatched"] = True

        result[
            "areaValidationReason"
        ] = "area_detected"

        return result

    if detected_area == search_area:

        result["areaMatched"] = True

        result[
            "areaValidationReason"
        ] = "address_area_matched"

        return result

    result["areaMatched"] = False

    result[
        "areaValidationReason"
    ] = "search_area_address_mismatch"

    return result


def assign_area_excluded_reason(
    property_data: Dict[str, Any],
) -> Optional[str]:

    if property_data.get(
        "areaPrefilterExcluded",
        False,
    ):
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

        if area_val_reason == "address_unavailable":
            return "detailPending"

        return "unknown"

    return None


# ============================================================
# School District
# ============================================================

def evaluate_school_district(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

    search_area = normalize_search_area(
        property_data.get(
            "searchArea"
        )
    )

    detail = get_detail(
        property_data
    )

    address = (
        clean_text(
            detail.get("address")
        )
        or ""
    )

    normalized_address = (
        normalize_address_for_area(
            address
        )
        or ""
    )

    kashiwa_address_candidate = any(
        pattern in normalized_address
        for pattern in [
            "柏の葉",
            "若柴",
            "正連寺",
            "中十余二",
        ]
    )

    if (
        search_area
        == "柏の葉キャンパス"
        and kashiwa_address_candidate
    ):

        return {
            "schoolDistrictStatus":
                "candidate",
            "schoolDistrictConfidence":
                "address_based",
            "schoolDistrictPolicy":
                "station_area_candidate",
            "schoolDistrictVerification":
                "required_for_final_decision",
            "schoolDistrictNote":
                (
                    "柏の葉キャンパス対象住所から"
                    "柏の葉小学校区候補として扱う。"
                    "正式な学区は番地で確認が必要。"
                ),
        }

    return {
        "schoolDistrictStatus":
            "none",
        "schoolDistrictConfidence":
            "not_applicable",
        "schoolDistrictPolicy":
            "none",
        "schoolDistrictVerification":
            "not_required",
        "schoolDistrictNote":
            None,
    }


# ============================================================
# Search Result Normalization
# ============================================================

def normalize_search_result(
    raw_result: Dict[str, Any],
) -> Dict[str, Any]:

    result = dict(
        raw_result
    )

    url = normalize_suumo_listing_url(
        result.get("sourceUrl")
        or result.get("url")
    )

    result["sourceUrl"] = (
        url
        or result.get("sourceUrl")
        or result.get("url")
        or ""
    )

    result["source"] = str(
        result.get(
            "source",
            "suumo",
        )
    ).strip().lower()

    identity = make_identity_key(
        result
    )

    result.update(
        {
            "propertyId":
                identity["propertyId"],
            "identityKey":
                identity["identityKey"],
            "identityType":
                identity["identityType"],
            "identityCompleteness":
                identity["identityCompleteness"],
            "sourceId":
                get_source_id(result),
            "id":
                identity["propertyId"],
        }
    )

    result["name"] = (
        clean_text(
            result.get("name")
        )
        or ""
    )

    result["price"] = to_number(
        result.get("price")
    )

    if result["price"] is not None:
        result["priceMan"] = int(
            result["price"]
        )

    result["area"] = normalize_search_area(
        result.get("area")
        or result.get("searchArea")
    )

    result["searchArea"] = normalize_search_area(
        result.get("searchArea")
        or result.get("area")
    )

    return result


# ============================================================
# Detail Enrichment
# ============================================================

def enrich_with_detail(
    property_data: Dict[str, Any],
    detail_res: Dict[str, Any],
) -> Dict[str, Any]:

    now = now_iso()

    property_data[
        "lastDetailFetchAt"
    ] = now

    attempts = int(
        property_data.get(
            "detailFetchAttempts",
            0,
        )
        or 0
    ) + 1

    property_data[
        "detailFetchAttempts"
    ] = attempts

    if not detail_res.get(
        "success",
        False,
    ):

        property_data[
            "detailFetchStatus"
        ] = "error"

        property_data[
            "detailFetchErrorType"
        ] = detail_res.get(
            "errorType",
            "unknown_error",
        )

        return property_data

    property_data[
        "detailFetchStatus"
    ] = "success"

    property_data[
        "detailFetchErrorType"
    ] = None

    detail_content = detail_res.get(
        "detail",
        {}
    )

    if not isinstance(
        detail_content,
        dict,
    ):
        detail_content = {}

    property_data[
        "detail"
    ] = detail_content

    property_data[
        "lastSuccessfulDetail"
    ] = detail_content

    if detail_content.get(
        "address"
    ):
        property_data[
            "address"
        ] = detail_content.get(
            "address"
        )

    if detail_content.get(
        "price"
    ) is not None:

        price_num = to_number(
            detail_content.get(
                "price"
            )
        )

        if price_num is not None:

            property_data[
                "price"
            ] = price_num

            property_data[
                "priceMan"
            ] = int(price_num)

            property_data[
                "currentPrice"
            ] = price_num

            property_data[
                "currentPriceMan"
            ] = int(price_num)

    if detail_content.get(
        "landAreaM2"
    ) is not None:

        property_data[
            "land"
        ] = to_number(
            detail_content.get(
                "landAreaM2"
            )
        )

    if detail_content.get(
        "buildingAreaM2"
    ) is not None:

        property_data[
            "building"
        ] = to_number(
            detail_content.get(
                "buildingAreaM2"
            )
        )

    if detail_content.get(
        "walkMinutes"
    ) is not None:

        property_data[
            "walk"
        ] = to_number(
            detail_content.get(
                "walkMinutes"
            )
        )

    p_type = detect_property_type(
        property_data
    )

    if p_type:
        property_data[
            "propertyType"
        ] = p_type

    return property_data


# ============================================================
# Criteria Evaluation
# ============================================================

def evaluate_property_criteria(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    area_eval = evaluate_area(
        property_data,
        search_config,
    )

    property_data.update(
        area_eval
    )

    school_eval = evaluate_school_district(
        property_data
    )

    property_data.update(
        school_eval
    )

    type_eval = evaluate_property_type(
        property_data,
        search_config,
    )

    property_data.update(
        type_eval
    )

    age_eval = evaluate_built_age(
        property_data,
        search_config,
    )

    property_data.update(
        age_eval
    )

    excluded_reason = (
        assign_area_excluded_reason(
            property_data
        )
    )

    property_data[
        "areaExcludedReason"
    ] = excluded_reason

    # --------------------------------------------------------
    # Price History
    # --------------------------------------------------------

    current_price = to_number(
        property_data.get("price")
    )

    price_history = property_data.get(
        "priceHistory",
        [],
    )

    if not isinstance(
        price_history,
        list,
    ):
        price_history = []

    if current_price is not None:

        if not price_history:

            price_history.append(
                {
                    "price":
                        current_price,
                    "recordedAt":
                        property_data.get(
                            "firstSeenAt"
                        )
                        or now_iso(),
                }
            )

        else:

            last_price = to_number(
                price_history[-1].get(
                    "price"
                )
            )

            if (
                last_price is not None
                and abs(
                    last_price
                    - current_price
                ) > 0.01
            ):

                change_type = (
                    "reduction"
                    if current_price
                    < last_price
                    else "increase"
                )

                price_history.append(
                    {
                        "price":
                            current_price,
                        "previousPrice":
                            last_price,
                        "changeType":
                            change_type,
                        "recordedAt":
                            now_iso(),
                    }
                )

    property_data[
        "priceHistory"
    ] = price_history

    # --------------------------------------------------------
    # Price Change Summary
    # --------------------------------------------------------

    reductions = [
        item
        for item in price_history
        if item.get(
            "changeType"
        ) == "reduction"
    ]

    property_data[
        "priceReductionCount"
    ] = len(reductions)

    if reductions:

        first_price = to_number(
            price_history[0].get(
                "price"
            )
        )

        latest_price = current_price

        if (
            first_price is not None
            and latest_price is not None
            and first_price > 0
        ):

            property_data[
                "totalPriceReductionAmount"
            ] = (
                first_price
                - latest_price
            )

            property_data[
                "totalPriceReductionRate"
            ] = round(
                (
                    first_price
                    - latest_price
                )
                / first_price
                * 100,
                2,
            )

    else:

        property_data[
            "totalPriceReductionAmount"
        ] = 0

        property_data[
            "totalPriceReductionRate"
        ] = 0

    # --------------------------------------------------------
    # Area Classification
    # --------------------------------------------------------

    if excluded_reason:

        property_data[
            "areaClassification"
        ] = "outOfTarget"

    elif property_data.get(
        "areaMatched"
    ) is True:

        property_data[
            "areaClassification"
        ] = "primaryTarget"

    elif property_data.get(
        "areaMatched"
    ) is False:

        property_data[
            "areaClassification"
        ] = "outOfTarget"

    else:

        property_data[
            "areaClassification"
        ] = "subTarget"

    return property_data


# ============================================================
# Lifecycle
# ============================================================

def update_property_lifecycle(
    property_data: Dict[str, Any],
    now_timestamp: str,
) -> Dict[str, Any]:

    if not property_data.get(
        "firstSeenAt"
    ):

        property_data[
            "firstSeenAt"
        ] = (
            property_data.get(
                "discoveredAt"
            )
            or now_timestamp
        )

    if property_data.get(
        "seenThisRun",
        False,
    ):

        property_data[
            "lastSeenAt"
        ] = now_timestamp

        property_data[
            "status"
        ] = "active"

    else:

        property_data[
            "status"
        ] = "observed_ended"

        if not property_data.get(
            "endedObservedAt"
        ):

            property_data[
                "endedObservedAt"
            ] = now_timestamp

    days_listed = calculate_days_between(
        property_data.get(
            "firstSeenAt"
        ),
        property_data.get(
            "lastSeenAt"
        )
        if property_data.get(
            "status"
        ) == "observed_ended"
        else None,
    )

    property_data[
        "daysListed"
    ] = days_listed

    return property_data


# ============================================================
# Observation
# ============================================================

def build_listing_observation(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

    observed_at = now_iso()

    detail = get_detail(
        property_data
    )

    return {
        "observationId": (
            f"{property_data.get('propertyId')}"
            f"@{observed_at}"
        ),
        "propertyId": (
            property_data.get(
                "propertyId"
            )
            or get_property_id(
                property_data
            )
        ),
        "source":
            property_data.get(
                "source"
            ),
        "sourceId":
            property_data.get(
                "sourceId"
            ),
        "observedAt":
            observed_at,
        "status":
            property_data.get(
                "status"
            ),
        "currentPrice":
            to_number(
                property_data.get(
                    "price"
                )
                or property_data.get(
                    "currentPrice"
                )
            ),
        "currentPriceMan":
            property_data.get(
                "priceMan"
            )
            or property_data.get(
                "currentPriceMan"
            ),
        "searchTargets":
            property_data.get(
                "searchTargets",
                [],
            ),
        "searchOccurrences":
            property_data.get(
                "searchOccurrences",
                [],
            ),
        "searchPageNumbers":
            property_data.get(
                "searchPageNumbers",
                [],
            ),
        "areaClassification":
            property_data.get(
                "areaClassification"
            ),
        "schoolDistrictStatus":
            property_data.get(
                "schoolDistrictStatus"
            ),
        "detailFetchStatus":
            property_data.get(
                "detailFetchStatus"
            ),
        "detailQuality":
            detail.get(
                "detailQuality"
            ),
        "builtAgeYears":
            property_data.get(
                "builtAgeYears"
            ),
        "landAreaM2":
            property_data.get(
                "land"
            ),
        "buildingAreaM2":
            property_data.get(
                "building"
            ),
        "walkMinutes":
            property_data.get(
                "walk"
            ),
        "priceReductionCount":
            property_data.get(
                "priceReductionCount",
                0,
            ),
        "parserVersion":
            MAIN_PARSER_VERSION,
    }


# ============================================================
# Search Occurrence Tracking
# ============================================================

def append_unique(
    values: Any,
    value: Any,
) -> List[Any]:

    if not isinstance(
        values,
        list,
    ):
        values = []

    if value is None:
        return values

    if value not in values:
        values.append(value)

    return values


def merge_search_occurrence(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
) -> None:

    target = candidate.get(
        "searchTarget"
    )

    page_number = candidate.get(
        "searchPageNumber"
    )

    position = candidate.get(
        "searchPosition"
    )

    existing[
        "searchTargets"
    ] = append_unique(
        existing.get(
            "searchTargets"
        ),
        target,
    )

    existing[
        "searchPageNumbers"
    ] = append_unique(
        existing.get(
            "searchPageNumbers"
        ),
        page_number,
    )

    occurrence = {
        "searchTarget": target,
        "searchPageNumber":
            page_number,
        "searchPosition":
            position,
        "observedAt":
            candidate.get(
                "discoveredAt"
            )
            or now_iso(),
    }

    occurrences = existing.get(
        "searchOccurrences"
    )

    if not isinstance(
        occurrences,
        list,
    ):
        occurrences = []

    occurrence_key = (
        occurrence.get(
            "searchTarget"
        ),
        occurrence.get(
            "searchPageNumber"
        ),
        occurrence.get(
            "searchPosition"
        ),
    )

    already_exists = any(
        (
            item.get(
                "searchTarget"
            ),
            item.get(
                "searchPageNumber"
            ),
            item.get(
                "searchPosition"
            ),
        )
        == occurrence_key
        for item in occurrences
        if isinstance(
            item,
            dict,
        )
    )

    if not already_exists:
        occurrences.append(
            occurrence
        )

    existing[
        "searchOccurrences"
    ] = occurrences


# ============================================================
# Main Pipeline
# ============================================================

def run_pipeline() -> None:

    print(
        "============================================================"
    )
    print(
        f"=== Starting SUUMO Scraping Pipeline "
        f"{MAIN_PARSER_VERSION} ==="
    )
    print(
        f"=== {now_iso()} ==="
    )
    print(
        "============================================================"
    )

    # --------------------------------------------------------
    # 1. Config
    # --------------------------------------------------------

    search_config = load_search_config()

    search_urls = load_search_urls()

    if not search_urls:

        print(
            "[ERROR] 検索対象URLがありません。"
            "config/search_urls.json を確認してください。"
        )

        sys.exit(1)

    # --------------------------------------------------------
    # 2. Existing Market DB
    # --------------------------------------------------------

    discovered_raw = load_json(
        DISCOVERED_PATH,
        default={
            "properties": []
        },
    )

    if isinstance(
        discovered_raw,
        dict,
    ):

        properties_list = (
            discovered_raw.get(
                "properties",
                [],
            )
        )

    elif isinstance(
        discovered_raw,
        list,
    ):

        properties_list = (
            discovered_raw
        )

    else:

        properties_list = []

    db: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for prop in properties_list:

        if not isinstance(
            prop,
            dict,
        ):
            continue

        pid = get_property_id(
            prop
        )

        if not pid:
            continue

        prop[
            "seenThisRun"
        ] = False

        db[pid] = prop

    print(
        f"既存Market DB読み込み完了: "
        f"{len(db)}件"
    )

    # --------------------------------------------------------
    # 3. Search
    # --------------------------------------------------------

    search_adapter = (
        SuumoSearchAdapter(
            search_config
        )
    )

    all_candidates: List[
        Dict[str, Any]
    ] = []

    search_target_stats = []

    for target in search_urls:

        if target.get(
            "enabled",
            True,
        ) is False:
            continue

        target_area = target.get(
            "area"
        )

        target_property_type = target.get(
            "propertyType"
        )

        url = target.get(
            "url"
        )

        print(
            "------------------------------------------------------------"
        )

        print(
            "検索開始: "
            f"エリア={target_area}, "
            f"タイプ={target_property_type}"
        )

        candidates = (
            search_adapter.fetch_search_results(
                url,
                target=target,
                config=search_config,
            )
        )

        target_count = 0

        for pos, candidate in enumerate(
            candidates,
            start=1,
        ):

            candidate[
                "searchPosition"
            ] = pos

            candidate[
                "searchTarget"
            ] = (
                target.get(
                    "name"
                )
                or (
                    f"{target_area}_"
                    f"{target_property_type}"
                )
            )

            candidate[
                "searchTargetArea"
            ] = target_area

            candidate[
                "searchTargetPropertyType"
            ] = target_property_type

            candidate[
                "discoveredAt"
            ] = now_iso()

            normalized = (
                normalize_search_result(
                    candidate
                )
            )

            if not normalized.get(
                "propertyId"
            ):
                continue

            all_candidates.append(
                normalized
            )

            target_count += 1

        search_target_stats.append(
            {
                "name":
                    target.get(
                        "name"
                    ),
                "area":
                    target_area,
                "propertyType":
                    target_property_type,
                "count":
                    target_count,
            }
        )

        print(
            f"検索候補取得完了: "
            f"{target_count}件"
        )

    print(
        f"全検索候補取得完了: "
        f"{len(all_candidates)}件"
    )

    # --------------------------------------------------------
    # 4. Merge
    # --------------------------------------------------------

    now_stamp = now_iso()

    new_count = 0
    existing_count = 0

    for candidate in all_candidates:

        pid = get_property_id(
            candidate
        )

        if not pid:
            continue

        if pid in db:

            existing = db[pid]

            existing_count += 1

            # Search occurrence is preserved.
            merge_search_occurrence(
                existing,
                candidate,
            )

            # Update volatile search fields.
            if candidate.get(
                "name"
            ):
                existing[
                    "name"
                ] = candidate.get(
                    "name"
                )

            if candidate.get(
                "price"
            ) is not None:

                existing[
                    "price"
                ] = candidate.get(
                    "price"
                )

                existing[
                    "priceMan"
                ] = candidate.get(
                    "priceMan"
                )

            if candidate.get(
                "sourceUrl"
            ):
                existing[
                    "sourceUrl"
                ] = candidate.get(
                    "sourceUrl"
                )

            if candidate.get(
                "searchArea"
            ):
                existing[
                    "searchArea"
                ] = candidate.get(
                    "searchArea"
                )

            existing[
                "searchTarget"
            ] = candidate.get(
                "searchTarget"
            )

            existing[
                "searchPageNumber"
            ] = candidate.get(
                "searchPageNumber"
            )

            existing[
                "searchPosition"
            ] = candidate.get(
                "searchPosition"
            )

            existing[
                "seenThisRun"
            ] = True

            existing[
                "lastSeenAt"
            ] = now_stamp

        else:

            candidate[
                "seenThisRun"
            ] = True

            candidate[
                "firstSeenAt"
            ] = now_stamp

            candidate[
                "lastSeenAt"
            ] = now_stamp

            candidate[
                "status"
            ] = "active"

            candidate[
                "searchTargets"
            ] = []

            candidate[
                "searchPageNumbers"
            ] = []

            candidate[
                "searchOccurrences"
            ] = []

            merge_search_occurrence(
                candidate,
                candidate,
            )

            db[pid] = candidate

            new_count += 1

    print(
        f"MERGE完了: "
        f"新規={new_count}, "
        f"既存={existing_count}, "
        f"Market DB={len(db)}"
    )

    # --------------------------------------------------------
    # 5. URL area prefilter
    # --------------------------------------------------------

    for prop in db.values():

        apply_url_area_prefilter(
            prop,
            search_config,
            search_urls,
        )

    # --------------------------------------------------------
    # 6. Detail Queue
    # --------------------------------------------------------

    items = list(
        db.values()
    )

    items_by_priority = sorted(
        items,
        key=lambda item: (
            detail_fetch_priority(
                item
            ),
            item.get(
                "firstSeenAt",
                "",
            ),
        ),
    )

    fetch_limit = int(
        search_config.get(
            "detailFetchLimit",
            DEFAULT_DETAIL_FETCH_LIMIT,
        )
    )

    request_interval = float(
        search_config.get(
            "detailRequestIntervalSeconds",
            1.5,
        )
    )

    to_fetch = [
        item
        for item in items_by_priority
        if detail_fetch_priority(
            item
        ) < 999
    ][:fetch_limit]

    print(
        f"詳細情報取得キュー: "
        f"{len(to_fetch)}件 "
        f"(上限={fetch_limit})"
    )

    detail_adapter = (
        SuumoDetailAdapter(
            search_config
        )
    )

    consecutive_errors = 0

    detail_success_count = 0
    detail_failure_count = 0

    for i, item in enumerate(
        to_fetch,
        start=1,
    ):

        url = (
            item.get(
                "sourceUrl"
            )
            or item.get(
                "url"
            )
        )

        if not url:
            continue

        print(
            f"[{i}/{len(to_fetch)}] "
            f"詳細取得中: "
            f"{item.get('name', 'N/A')}"
        )

        detail_res = (
            detail_adapter.fetch_detail(
                url
            )
        )

        enrich_with_detail(
            item,
            detail_res,
        )

        if detail_res.get(
            "success"
        ):

            detail_success_count += 1
            consecutive_errors = 0

        else:

            detail_failure_count += 1
            consecutive_errors += 1

            print(
                "  -> 取得失敗: "
                f"{detail_res.get('errorType')} "
                f"(連続={consecutive_errors})"
            )

            if (
                consecutive_errors
                >= MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS
            ):

                print(
                    "[WARN] 連続エラー上限に達したため"
                    "詳細取得を中断します。"
                )

                break

        time.sleep(
            request_interval
        )

    print(
        f"詳細取得結果: "
        f"success={detail_success_count}, "
        f"failure={detail_failure_count}"
    )

    # --------------------------------------------------------
    # 7. Evaluation & Lifecycle
    # --------------------------------------------------------

    for item in db.values():

        evaluate_property_criteria(
            item,
            search_config,
        )

        update_property_lifecycle(
            item,
            now_stamp,
        )

    properties_final = list(
        db.values()
    )

    # --------------------------------------------------------
    # 8. Observations
    # --------------------------------------------------------

    existing_observations_raw = (
        load_json(
            OBSERVATIONS_PATH,
            default={
                "observations": []
            },
        )
    )

    if isinstance(
        existing_observations_raw,
        dict,
    ):

        observations = (
            existing_observations_raw.get(
                "observations",
                [],
            )
        )

    else:

        observations = []

    new_observations_count = 0

    for item in properties_final:

        if not item.get(
            "seenThisRun",
            False,
        ):
            continue

        observation = (
            build_listing_observation(
                item
            )
        )

        observations.append(
            observation
        )

        new_observations_count += 1

    save_json(
        OBSERVATIONS_PATH,
        {
            "schemaVersion":
                "1.0",
            "updatedAt":
                now_iso(),
            "observations":
                observations,
        },
    )

    print(
        f"観測履歴保存完了: "
        f"追加={new_observations_count}, "
        f"総計={len(observations)}"
    )

    # --------------------------------------------------------
    # 9. Save Market DB
    # --------------------------------------------------------

    save_json(
        DISCOVERED_PATH,
        {
            "schemaVersion":
                "1.0",
            "parserVersion":
                MAIN_PARSER_VERSION,
            "updatedAt":
                now_iso(),
            "properties":
                properties_final,
        },
    )

    # --------------------------------------------------------
    # 10. Houses Output
    # --------------------------------------------------------

    active_houses = [
        item
        for item in properties_final
        if (
            item.get(
                "status"
            ) == "active"
            and item.get(
                "areaClassification"
            )
            in {
                "primaryTarget",
                "subTarget",
            }
        )
    ]

    save_json(
        HOUSES_PATH,
        {
            "updatedAt":
                now_iso(),
            "count":
                len(active_houses),
            "houses":
                active_houses,
        },
    )

    # --------------------------------------------------------
    # 11. Summary
    # --------------------------------------------------------

    active_count = sum(
        1
        for p in properties_final
        if p.get(
            "status"
        ) == "active"
    )

    primary_count = sum(
        1
        for p in properties_final
        if (
            p.get(
                "status"
            ) == "active"
            and p.get(
                "areaClassification"
            ) == "primaryTarget"
        )
    )

    subtarget_count = sum(
        1
        for p in properties_final
        if (
            p.get(
                "status"
            ) == "active"
            and p.get(
                "areaClassification"
            ) == "subTarget"
        )
    )

    ended_count = sum(
        1
        for p in properties_final
        if p.get(
            "status"
        ) == "observed_ended"
    )

    detail_pending_count = sum(
        1
        for p in properties_final
        if not has_successful_detail(
            p
        )
        and p.get(
            "status"
        ) == "active"
    )

    price_reduction_count = sum(
        int(
            p.get(
                "priceReductionCount",
                0,
            )
            or 0
        )
        for p in properties_final
    )

    summary = {
        "updatedAt":
            now_iso(),
        "parserVersion":
            MAIN_PARSER_VERSION,
        "totalObserved":
            len(properties_final),
        "activeCount":
            active_count,
        "endedCount":
            ended_count,
        "primaryTargetCount":
            primary_count,
        "subTargetCount":
            subtarget_count,
        "detailPendingCount":
            detail_pending_count,
        "detailSuccessThisRun":
            detail_success_count,
        "detailFailureThisRun":
            detail_failure_count,
        "priceReductionCount":
            price_reduction_count,
        "observationsCount":
            len(observations),
        "searchCandidateCount":
            len(all_candidates),
        "newPropertyCount":
            new_count,
        "existingPropertyCount":
            existing_count,
        "searchTargetStats":
            search_target_stats,
    }

    save_json(
        SUMMARY_PATH,
        summary,
    )

    # --------------------------------------------------------
    # 12. Final Log
    # --------------------------------------------------------

    print(
        "============================================================"
    )

    print(
        "=== Pipeline Complete ==="
    )

    print(
        f"Market DB       : "
        f"{summary['totalObserved']}"
    )

    print(
        f"Active          : "
        f"{summary['activeCount']}"
    )

    print(
        f"Ended           : "
        f"{summary['endedCount']}"
    )

    print(
        f"Primary Target  : "
        f"{summary['primaryTargetCount']}"
    )

    print(
        f"Sub Target      : "
        f"{summary['subTargetCount']}"
    )

    print(
        f"Detail Pending  : "
        f"{summary['detailPendingCount']}"
    )

    print(
        f"Price Reduction : "
        f"{summary['priceReductionCount']}"
    )

    print(
        "============================================================"
    )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    run_pipeline()