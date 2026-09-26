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

MAIN_PARSER_VERSION = "2026-09-25-v29-market-history"

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
# Market History DB
# ============================================================
HOUSE_DB_SCHEMA_VERSION = "2.0"
# houses.json に保存するMarket DB上のライフサイクル状態
#
# active:
#   現在SUUMO検索で確認されている物件
#
# observed_ended:
#   過去には確認されたが、現在の正常な検索では
#   確認できなくなった物件
HOUSE_DB_STATUSES = {
    "active",
    "observed_ended",
}
# houses.json に保存する対象エリア
#
# searchCriteriaMatched はここでは使用しない。
# 条件外物件も市場履歴として保持する。
HOUSE_DB_AREA_CLASSIFICATIONS = {
    "primaryTarget",
    "subTarget",
}


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
            dt_end_obj = datetime.fromisoformat(
                str(end_iso).replace("Z", "+00:00")
            )
        else:
            dt_end_obj = datetime.now(timezone.utc)

        delta = dt_end_obj - dt_start

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


def get_numeric_search_criteria(
    config: Dict[str, Any],
) -> Dict[str, Optional[float]]:

    return {
        "maxPriceMan": to_number(
            config.get("maxPriceMan")
        ),
        "maxWalkMinutes": to_number(
            config.get("maxWalkMinutes")
        ),
        "minLandArea": to_number(
            config.get("minLandArea")
        ),
        "minBuildingArea": to_number(
            config.get("minBuildingArea")
        ),
    }


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

        property_data[
            "areaPrefilterExcluded"
        ] = False

        property_data[
            "areaPrefilterReason"
        ] = "city_code_rule_unavailable"

        return property_data

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
# Market History DB Selection
# ============================================================
def is_market_history_property(
    property_data: Dict[str, Any],
) -> bool:
    """
    houses.json に保存するMarket History DB対象か判定する。
    保存対象:
      - active
      - observed_ended
    かつ:
      - primaryTarget
      - subTarget
    重要:
      searchCriteriaMatched は判定しない。
    したがって、
      ・価格上限超過
      ・土地面積不足
      ・徒歩分数超過
      ・築年数超過
      ・その他検索条件外
    の物件でも、対象エリアに存在した物件なら
    市場履歴としてhouses.jsonに保持する。
    """
    if not isinstance(property_data, dict):
        return False
    status = property_data.get("status")
    if status not in HOUSE_DB_STATUSES:
        return False
    area_classification = property_data.get(
        "areaClassification"
    )
    if (
        area_classification
        not in HOUSE_DB_AREA_CLASSIFICATIONS
    ):
        return False
    return True


# ============================================================
# Detail Helpers & Refresh Logic
# ============================================================

def is_detail_target_property(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
    search_urls: List[Dict[str, Any]],
) -> bool:
    """
    詳細取得対象エリアの物件か判定する。
    明確に対象外と判定できる市区町村コードは除外する。
    city_code が取得できない場合は、従来互換性のため
    True として扱う。
    """
    if not isinstance(property_data, dict):
        return False
    if property_data.get(
        "areaPrefilterExcluded",
        False,
    ):
        return False
    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
    )
    city_code = extract_city_from_url(url)
    # 市区町村コードが取得できない場合は、
    # ここでは安全側に「対象候補」とする
    if not city_code:
        return True
    allowed_city_codes = set()
    # search_urls の設定を優先
    for target in search_urls:
        codes = target.get(
            "allowedCityCodes"
        )
        if isinstance(codes, list):
            allowed_city_codes.update(
                str(code)
                for code in codes
                if code
            )
    # search_urls に無ければ search.json / fallback を利用
    if not allowed_city_codes:
        area_rules = get_area_rules(
            search_config
        )
        for rule in area_rules.values():
            if not isinstance(rule, dict):
                continue
            codes = rule.get(
                "cityCodes"
            )
            if isinstance(codes, list):
                allowed_city_codes.update(
                    str(code)
                    for code in codes
                    if code
                )
    # ルールが取得できない場合は従来互換
    if not allowed_city_codes:
        return True
    return city_code in allowed_city_codes


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

    if (
        seen
        and not has_success
        and attempts == 0
    ):
        return 0

    if (
        status == "active"
        and not has_success
    ):
        if attempts < MAX_DETAIL_FETCH_ATTEMPTS:
            return 1

    if (
        has_success
        and is_detail_stale(
            property_data
        )
    ):
        return 2

    if (
        error_type
        in RETRYABLE_DETAIL_ERROR_TYPES
        and attempts < MAX_DETAIL_FETCH_ATTEMPTS
    ):
        return 3

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
    property_data: Dict[str, Any],
) -> Optional[float]:

    detail = get_detail(
        property_data
    )

    value = detail.get("price")

    if value is None:
        value = property_data.get("price")

    if value is None:
        value = property_data.get(
            "currentPrice"
        )

    return to_number(value)


def get_land_area(
    property_data: Dict[str, Any],
) -> Optional[float]:

    detail = get_detail(
        property_data
    )

    value = detail.get("landAreaM2")

    if value is None:
        value = property_data.get("land")

    return to_number(value)


def get_building_area(
    property_data: Dict[str, Any],
) -> Optional[float]:

    detail = get_detail(
        property_data
    )

    value = detail.get("buildingAreaM2")

    if value is None:
        value = property_data.get("building")

    return to_number(value)


def get_walk_minutes(
    property_data: Dict[str, Any],
) -> Optional[float]:

    detail = get_detail(
        property_data
    )

    for key in [
        "walkMinutes",
        "stationWalkMinutes",
    ]:

        value = detail.get(key)

        if value is not None:
            return to_number(value)

    for key in [
        "walk",
        "stationWalkMinutes",
    ]:

        value = property_data.get(key)

        if value is not None:
            return to_number(value)

    return None


# ============================================================
# Numeric Criteria
# ============================================================

def evaluate_numeric_criteria(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
) -> Dict[str, Any]:

    criteria = get_numeric_search_criteria(
        search_config
    )

    price = get_price(
        property_data
    )

    walk = get_walk_minutes(
        property_data
    )

    land = get_land_area(
        property_data
    )

    building = get_building_area(
        property_data
    )

    result: Dict[str, Any] = {
        "priceMan": (
            int(price)
            if price is not None
            else None
        ),
        "walkMinutes": walk,
        "landAreaM2": land,
        "buildingAreaM2": building,

        "priceMatched": None,
        "walkMatched": None,
        "landAreaMatched": None,
        "buildingAreaMatched": None,

        "priceReason": None,
        "walkReason": None,
        "landAreaReason": None,
        "buildingAreaReason": None,
    }

    # --------------------------------------------------------
    # Price
    # --------------------------------------------------------

    max_price = criteria["maxPriceMan"]

    if max_price is None:

        result["priceMatched"] = True
        result["priceReason"] = (
            "price_filter_not_configured"
        )

    elif price is None:

        result["priceReason"] = (
            "price_unavailable"
        )

    elif price <= max_price:

        result["priceMatched"] = True
        result["priceReason"] = (
            "within_price_limit"
        )

    else:

        result["priceMatched"] = False
        result["priceReason"] = (
            "price_over_limit"
        )

    # --------------------------------------------------------
    # Walk
    # --------------------------------------------------------

    max_walk = criteria["maxWalkMinutes"]

    if max_walk is None:

        result["walkMatched"] = True
        result["walkReason"] = (
            "walk_filter_not_configured"
        )

    elif walk is None:

        result["walkReason"] = (
            "walk_minutes_unavailable"
        )

    elif walk <= max_walk:

        result["walkMatched"] = True
        result["walkReason"] = (
            "within_walk_limit"
        )

    else:

        result["walkMatched"] = False
        result["walkReason"] = (
            "walk_minutes_over_limit"
        )

    # --------------------------------------------------------
    # Land
    # --------------------------------------------------------

    min_land = criteria["minLandArea"]

    if min_land is None:

        result["landAreaMatched"] = True
        result["landAreaReason"] = (
            "land_area_filter_not_configured"
        )

    elif land is None:

        result["landAreaReason"] = (
            "land_area_unavailable"
        )

    elif land >= min_land:

        result["landAreaMatched"] = True
        result["landAreaReason"] = (
            "within_land_area_limit"
        )

    else:

        result["landAreaMatched"] = False
        result["landAreaReason"] = (
            "land_area_below_limit"
        )

    # --------------------------------------------------------
    # Building
    # --------------------------------------------------------

    min_building = criteria[
        "minBuildingArea"
    ]

    if min_building is None:

        result["buildingAreaMatched"] = True
        result["buildingAreaReason"] = (
            "building_area_filter_not_configured"
        )

    elif building is None:

        result["buildingAreaReason"] = (
            "building_area_unavailable"
        )

    elif building >= min_building:

        result["buildingAreaMatched"] = True
        result["buildingAreaReason"] = (
            "within_building_area_limit"
        )

    else:

        result["buildingAreaMatched"] = False
        result["buildingAreaReason"] = (
            "building_area_below_limit"
        )

    return result


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

    attempt_at = now_iso()

    property_data[
        "lastDetailAttemptAt"
    ] = attempt_at

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

    property_data[
        "lastDetailFetchAt"
    ] = attempt_at

    property_data[
        "lastSuccessfulDetailAt"
    ] = attempt_at

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
    ] = deepcopy(
        detail_content
    )

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

    # --------------------------------------------------------
    # Area
    # --------------------------------------------------------

    area_eval = evaluate_area(
        property_data,
        search_config,
    )

    property_data.update(
        area_eval
    )

    # --------------------------------------------------------
    # School district
    # --------------------------------------------------------

    school_eval = evaluate_school_district(
        property_data
    )

    property_data.update(
        school_eval
    )

    # --------------------------------------------------------
    # Property type
    # --------------------------------------------------------

    type_eval = evaluate_property_type(
        property_data,
        search_config,
    )

    property_data.update(
        type_eval
    )

    # --------------------------------------------------------
    # Building age
    # --------------------------------------------------------

    age_eval = evaluate_built_age(
        property_data,
        search_config,
    )

    property_data.update(
        age_eval
    )

    # --------------------------------------------------------
    # Numeric criteria
    # --------------------------------------------------------

    numeric_eval = evaluate_numeric_criteria(
        property_data,
        search_config,
    )

    property_data.update(
        numeric_eval
    )

    # --------------------------------------------------------
    # Area exclusion reason
    # --------------------------------------------------------

    excluded_reason = (
        assign_area_excluded_reason(
            property_data
        )
    )

    property_data[
        "areaExcludedReason"
    ] = excluded_reason

    # --------------------------------------------------------
    # Overall criteria
    #
    # None = unknown/pending
    # False = confirmed failure
    #
    # Market DBにはunknownも残す。
    # --------------------------------------------------------

    criteria_flags = [
        property_data.get(
            "builtAgeMatched"
        ),
        property_data.get(
            "propertyTypeMatched"
        ),
        property_data.get(
            "areaMatched"
        ),
        property_data.get(
            "priceMatched"
        ),
        property_data.get(
            "walkMatched"
        ),
        property_data.get(
            "landAreaMatched"
        ),
        property_data.get(
            "buildingAreaMatched"
        ),
    ]

    is_criteria_matched = not any(
        value is False
        for value in criteria_flags
    )

    property_data[
        "searchCriteriaMatched"
    ] = is_criteria_matched

    property_data[
        "searchResultFilterExcluded"
    ] = not is_criteria_matched

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

            last_entry = price_history[-1]

            last_price = (
                to_number(
                    last_entry.get(
                        "price"
                    )
                )
                if isinstance(
                    last_entry,
                    dict,
                )
                else None
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
        if (
            isinstance(item, dict)
            and item.get(
                "changeType"
            ) == "reduction"
        )
    ]

    property_data[
        "priceReductionCount"
    ] = len(reductions)

    first_price = (
        to_number(
            price_history[0].get(
                "price"
            )
        )
        if price_history
        and isinstance(
            price_history[0],
            dict,
        )
        else None
    )

    latest_price = current_price

    if (
        first_price is not None
        and latest_price is not None
        and first_price > 0
    ):

        reduction_amount = (
            first_price
            - latest_price
        )

        property_data[
            "totalPriceReductionAmount"
        ] = reduction_amount

        property_data[
            "totalPriceReductionRate"
        ] = round(
            reduction_amount
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
    search_healthy: bool = True,
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

        if search_healthy:

            property_data[
                "status"
            ] = "observed_ended"

            if not property_data.get(
                "endedObservedAt"
            ):

                property_data[
                    "endedObservedAt"
                ] = now_timestamp

        else:

            print(
                f"[INFO] 検索一部失敗のためステータス維持: "
                f"{property_data.get('propertyId')}"
            )

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
                if property_data.get(
                    "price"
                ) is not None
                else property_data.get(
                    "currentPrice"
                )
            ),

        "currentPriceMan":
            property_data.get(
                "priceMan"
            )
            if property_data.get(
                "priceMan"
            ) is not None
            else property_data.get(
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

        "propertyTypeMatched":
            property_data.get(
                "propertyTypeMatched"
            ),

        "builtAgeMatched":
            property_data.get(
                "builtAgeMatched"
            ),

        "priceMatched":
            property_data.get(
                "priceMatched"
            ),

        "walkMatched":
            property_data.get(
                "walkMatched"
            ),

        "landAreaMatched":
            property_data.get(
                "landAreaMatched"
            ),

        "buildingAreaMatched":
            property_data.get(
                "buildingAreaMatched"
            ),

        "searchCriteriaMatched":
            property_data.get(
                "searchCriteriaMatched"
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

    search_area = normalize_search_area(
        candidate.get(
            "searchTargetArea"
        )
        or candidate.get(
            "searchArea"
        )
    )

    property_type = normalize_property_type(
        candidate.get(
            "searchTargetPropertyType"
        )
        or candidate.get(
            "searchPropertyType"
        )
    )

    search_url = candidate.get(
        "searchUrl"
    )

    search_page_url = candidate.get(
        "searchPageUrl"
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
        "searchTargetArea": search_area,
        "searchTargetPropertyType": property_type,
        "searchUrl": search_url,
        "searchPageUrl": search_page_url,
        "searchPageNumber": page_number,
        "searchPosition": position,
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
            "searchTargetArea"
        ),
        occurrence.get(
            "searchTargetPropertyType"
        ),
        occurrence.get(
            "searchUrl"
        ),
        occurrence.get(
            "searchPageUrl"
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
                "searchTargetArea"
            ),
            item.get(
                "searchTargetPropertyType"
            ),
            item.get(
                "searchUrl"
            ),
            item.get(
                "searchPageUrl"
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
# Search Adapter Compatibility
# ============================================================

def fetch_search_target(
    search_adapter: Any,
    url: str,
    target: Dict[str, Any],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:

    """
    現行search adapterとのインターフェースを一本化する。

    優先:
      fetch_search_results()

    後方互換:
      crawl_search_target()

    search adapter側では、価格・面積・築年数等の
    最終条件による除外を行わない。
    """

    if hasattr(
        search_adapter,
        "fetch_search_results",
    ):

        result = (
            search_adapter.fetch_search_results(
                url,
                target=target,
                config=config,
            )
        )

    elif hasattr(
        search_adapter,
        "crawl_search_target",
    ):

        result = (
            search_adapter.crawl_search_target(
                url,
                target=target,
                config=config,
            )
        )

    else:

        raise AttributeError(
            "SuumoSearchAdapterに"
            "fetch_search_results() または "
            "crawl_search_target() がありません。"
        )

    if not isinstance(
        result,
        list,
    ):
        return []

    return [
        item
        for item in result
        if isinstance(
            item,
            dict,
        )
    ]


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
            search_config,
            ROOT,
        )
    )

    all_candidates: List[
        Dict[str, Any]
    ] = []

    search_target_stats = []
    search_healthy = True

    for target in search_urls:

        if target.get(
            "enabled",
            True,
        ) is False:
            continue

        target_area = normalize_search_area(
            target.get("area")
        )

        target_property_type = normalize_property_type(
            target.get("propertyType")
        )

        url = target.get(
            "url"
        )

        if not url:

            search_healthy = False

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
                        0,
                    "success":
                        False,
                    "reason":
                        "search_url_missing",
                }
            )

            print(
                "[WARN] 検索URLがありません。"
            )

            continue

        print(
            "------------------------------------------------------------"
        )

        print(
            "検索開始: "
            f"エリア={target_area}, "
            f"タイプ={target_property_type}"
        )

        target_success = True

        try:

            candidates = fetch_search_target(
                search_adapter,
                url,
                target,
                search_config,
            )

            if getattr(
                search_adapter,
                "last_search_healthy",
                True,
            ) is False:

                target_success = False

        except Exception as exc:

            print(
                f"[ERROR] 検索ターゲット失敗 "
                f"[{target_area} - "
                f"{target_property_type}]: {exc}"
            )

            candidates = []

            target_success = False

        if not target_success:
            search_healthy = False

        target_count = 0

        for pos, candidate in enumerate(
            candidates,
            start=1,
        ):

            if not isinstance(
                candidate,
                dict,
            ):
                continue

            candidate = dict(
                candidate
            )

            # ------------------------------------------------
            # Search provenance
            #
            # Search adapter側で既に付いている値を優先。
            # 無い場合のみmainで補完する。
            # ------------------------------------------------

            candidate[
                "searchPosition"
            ] = (
                candidate.get(
                    "searchPosition"
                )
                or pos
            )

            candidate[
                "searchTarget"
            ] = (
                candidate.get(
                    "searchTarget"
                )
                or target.get(
                    "name"
                )
                or (
                    f"{target_area}_"
                    f"{target_property_type}"
                )
            )

            candidate[
                "searchTargetArea"
            ] = normalize_search_area(
                candidate.get(
                    "searchTargetArea"
                )
                or target_area
            )

            candidate[
                "searchTargetPropertyType"
            ] = normalize_property_type(
                candidate.get(
                    "searchTargetPropertyType"
                )
                or target_property_type
            )

            candidate[
                "searchPropertyType"
            ] = normalize_property_type(
                candidate.get(
                    "searchPropertyType"
                )
                or target_property_type
            )

            candidate[
                "searchUrl"
            ] = (
                candidate.get(
                    "searchUrl"
                )
                or url
            )

            candidate[
                "searchPageUrl"
            ] = (
                candidate.get(
                    "searchPageUrl"
                )
                or url
            )

            candidate[
                "discoveredAt"
            ] = (
                candidate.get(
                    "discoveredAt"
                )
                or now_iso()
            )

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
                "url":
                    url,
                "count":
                    target_count,
                "success":
                    target_success,
            }
        )

        print(
            f"検索候補取得完了: "
            f"{target_count}件 "
            f"(Success={target_success})"
        )

    print(
        f"全検索候補取得完了: "
        f"{len(all_candidates)}件 "
        f"(Overall Search Healthy={search_healthy})"
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

            merge_search_occurrence(
                existing,
                candidate,
            )

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

            if candidate.get(
                "searchPropertyType"
            ):
                existing[
                    "searchPropertyType"
                ] = candidate.get(
                    "searchPropertyType"
                )

            # 最新検索位置は互換性のため保持
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
                "searchUrl"
            ] = candidate.get(
                "searchUrl"
            )

            existing[
                "searchPageUrl"
            ] = candidate.get(
                "searchPageUrl"
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
            ] = (
                candidate.get(
                    "discoveredAt"
                )
                or now_stamp
            )

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
        f"既存候補={existing_count}, "
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

    try:

        fetch_limit = int(
            search_config.get(
                "detailFetchLimit",
                DEFAULT_DETAIL_FETCH_LIMIT,
            )
        )

    except (TypeError, ValueError):

        fetch_limit = DEFAULT_DETAIL_FETCH_LIMIT

    try:

        request_interval = float(
            search_config.get(
                "detailRequestIntervalSeconds",
                1.5,
            )
        )

    except (TypeError, ValueError):

        request_interval = 1.5

    fetch_limit = max(
        0,
        fetch_limit,
    )

    request_interval = max(
        0.0,
        request_interval,
    )

    detail_queue_excluded_count = 0
    to_fetch_candidates = []
    for item in items_by_priority:
        if detail_fetch_priority(item) >= 999:
            continue
        if not is_detail_target_property(
            item,
            search_config,
            search_urls,
        ):
            detail_queue_excluded_count += 1
            continue
        to_fetch_candidates.append(item)
    to_fetch = to_fetch_candidates[:fetch_limit]

    print(
        f"詳細取得キュー: {len(to_fetch)}件 "
        f"(上限={fetch_limit}, "
        f"対象外除外={detail_queue_excluded_count})"
    )

    detail_adapter = (
        SuumoDetailAdapter(
            search_config,
            ROOT,
        )
    )

    consecutive_errors = 0

    detail_success_count = 0
    detail_failure_count = 0
    detail_interrupted = False

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

        display_name = (
            item.get("name")
            or item.get("propertyId")
            or get_property_id(item)
            or "N/A"
        )
        print(
            f"[{i}/{len(to_fetch)}] "
            f"詳細取得中: {display_name}"
        )

        try:

            detail_res = (
                detail_adapter.fetch_detail(
                    url
                )
            )

            if not isinstance(
                detail_res,
                dict,
            ):
                detail_res = {
                    "success": False,
                    "errorType":
                        "invalid_detail_response",
                }

        except Exception as exc:

            detail_res = {
                "success": False,
                "errorType":
                    "adapter_exception",
                "error":
                    str(exc),
            }

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
            error_type = detail_res.get(
                "errorType"
            )
            error_message = detail_res.get(
                "error"
            )
            print(
                "  -> 取得失敗: "
                f"{error_type}"
            )
            if error_message:
                print(
                    f"     error={error_message}"
                )
            print(
                "     propertyId="
                f"{item.get('propertyId') or get_property_id(item)}"
            )
            print(
                f"     sourceUrl={url}"
            )
            # 接続系エラーだけを
            # circuit breaker の対象とする
            if (
                error_type
                in RETRYABLE_DETAIL_ERROR_TYPES
            ):
                consecutive_errors += 1
            else:
                # Parserエラー等の場合は
                # 接続エラー連続数をリセット
                consecutive_errors = 0

            if (
                error_type
                in RETRYABLE_DETAIL_ERROR_TYPES
                and consecutive_errors
                >= MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS
            ):
                print(
                    "[WARN] 連続した接続系エラーが"
                    "上限に達したため"
                    "詳細取得を中断します。"
                )
                detail_interrupted = True
                break

        if request_interval > 0:
            time.sleep(
                request_interval
            )

    print(
        f"詳細取得結果: "
        f"success={detail_success_count}, "
        f"failure={detail_failure_count}, "
        f"interrupted={detail_interrupted}"
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
            search_healthy=search_healthy,
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

    if not isinstance(
        observations,
        list,
    ):
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
    #
    # houses.json は「現在の採用物件」ではなく、
    # 対象エリアのMarket History DBとして扱う。
    #
    # 保存対象:
    #   - active
    #   - observed_ended
    #
    # 対象エリア:
    #   - primaryTarget
    #   - subTarget
    #
    # 重要:
    #   searchCriteriaMatched == False
    #   でも保存する。
    #
    # これにより、現在の検索条件から外れた物件でも、
    # 過去の市場価格・値下げ・掲載期間等を
    # 後から参照できる。
    # --------------------------------------------------------
    market_houses = [
        item
        for item in properties_final
        if is_market_history_property(item)
    ]
    market_history_active_count = sum(
        1
        for item in market_houses
        if item.get("status") == "active"
    )
    market_history_ended_count = sum(
        1
        for item in market_houses
        if item.get("status") == "observed_ended"
    )
    market_history_criteria_excluded_count = sum(
        1
        for item in market_houses
        if item.get("searchCriteriaMatched") is False
    )
    market_history_criteria_pending_count = sum(
        1
        for item in market_houses
        if (
            item.get("searchCriteriaMatched") is True
            and any(
                item.get(key) is None
                for key in [
                    "areaMatched",
                    "propertyTypeMatched",
                    "builtAgeMatched",
                    "priceMatched",
                    "walkMatched",
                    "landAreaMatched",
                    "buildingAreaMatched",
                ]
            )
        )
    )
    save_json(
        HOUSES_PATH,
        {
            "schemaVersion":
                HOUSE_DB_SCHEMA_VERSION,
            "parserVersion":
                MAIN_PARSER_VERSION,
            "updatedAt":
                now_iso(),
            "count":
                len(market_houses),
            "activeCount":
                market_history_active_count,
            "endedCount":
                market_history_ended_count,
            "criteriaExcludedCount":
                market_history_criteria_excluded_count,
            "criteriaPendingCount":
                market_history_criteria_pending_count,
            "properties":
                market_houses,
        },
    )
    print(
        f"Market History DB保存完了: "
        f"total={len(market_houses)}, "
        f"active={market_history_active_count}, "
        f"ended={market_history_ended_count}, "
        f"criteriaExcluded="
        f"{market_history_criteria_excluded_count}, "
        f"criteriaPending="
        f"{market_history_criteria_pending_count}"
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
        if (
            not has_successful_detail(p)
            and p.get(
                "status"
            ) == "active"
        )
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

    criteria_excluded_count = sum(
        1
        for p in properties_final
        if p.get(
            "searchCriteriaMatched"
        ) is False
    )

    criteria_pending_count = sum(
        1
        for p in properties_final
        if (
            p.get(
                "searchCriteriaMatched"
            ) is True
            and any(
                p.get(key) is None
                for key in [
                    "areaMatched",
                    "propertyTypeMatched",
                    "builtAgeMatched",
                    "priceMatched",
                    "walkMatched",
                    "landAreaMatched",
                    "buildingAreaMatched",
                ]
            )
        )
    )

    summary = {
        "updatedAt":
            now_iso(),
        "parserVersion":
            MAIN_PARSER_VERSION,

        "searchHealthy":
            search_healthy,

        # ----------------------------------------------------
        # Market History DB
        # ----------------------------------------------------
        "marketHistoryCount":
            len(market_houses),
        "marketHistoryActiveCount":
            market_history_active_count,
        "marketHistoryEndedCount":
            market_history_ended_count,
        "marketHistoryCriteriaExcludedCount":
            market_history_criteria_excluded_count,
        "marketHistoryCriteriaPendingCount":
            market_history_criteria_pending_count,

        "detailInterrupted":
            detail_interrupted,

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

        "criteriaExcludedCount":
            criteria_excluded_count,

        "criteriaPendingCount":
            criteria_pending_count,

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

        "existingPropertyCandidateCount":
            existing_count,

        "marketDbCount":
            len(properties_final),

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
        f"Search Healthy  : "
        f"{summary['searchHealthy']}"
    )

    print(
        f"Market DB       : "
        f"{summary['marketDbCount']}"
    )

    print(
        f"Market History  : "
        f"{summary['marketHistoryCount']}"
    )

    print(
        f"History Active  : "
        f"{summary['marketHistoryActiveCount']}"
    )

    print(
        f"History Ended   : "
        f"{summary['marketHistoryEndedCount']}"
    )

    print(
        f"History Criteria Excluded : "
        f"{summary['marketHistoryCriteriaExcludedCount']}"
    )

    print(
        f"History Criteria Pending  : "
        f"{summary['marketHistoryCriteriaPendingCount']}"
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
        f"Criteria Excluded : "
        f"{summary['criteriaExcludedCount']}"
    )

    print(
        f"Criteria Pending : "
        f"{summary['criteriaPendingCount']}"
    )

    print(
        f"Price Reduction : "
        f"{summary['priceReductionCount']}"
    )

    print(
        f"Detail Interrupted : "
        f"{summary['detailInterrupted']}"
    )

    print(
        "============================================================"
    )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    run_pipeline()
