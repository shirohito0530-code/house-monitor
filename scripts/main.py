from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit


# ============================================================
# Imports
# ============================================================

from adapters.suumo_search import SuumoSearchAdapter
from adapters.suumo_detail import SuumoDetailAdapter
try:
    from identity import (
        infer_source,
        get_source_id,
        make_property_id,
        make_identity_key,
    )
except ImportError:
    from scripts.identity import (
        infer_source,
        get_source_id,
        make_property_id,
        make_identity_key,
    )


# ============================================================
# Constants
# ============================================================

MAIN_PARSER_VERSION = "2026-09-29-v35-price-extraction-and-history-fix"

# ============================================================
# Discovery DB retention / schema
# ============================================================
DISCOVERY_SCHEMA_VERSION = "2.0"
DISCOVERY_RETENTION_DAYS = 180
DISCOVERY_FIELDS = (
    "propertyId",
    "source",
    "sourceId",
    "sourceUrl",
    "name",
    "listingTitle",
    "searchTitle",
    "searchArea",
    "searchPropertyType",
    "areaClassification",
    "status",
    "firstSeenAt",
    "lastSeenAt",
    "endedAt",
    "lastDetailFetchAt",
    "detailFetchStatus",
    "detailFetchErrorType",
    "detailFetchAttempts",
    "detailFetchSuccess",
)

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
HOUSE_DB_STATUSES = {
    "active",
    "observed_ended",
}
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
# Utility & Validation
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


def validate_sale_price(price: Any) -> Tuple[Optional[int], Optional[str]]:
    """
    販売価格の最終バリデーション。
    """
    if price is None:
        return None, "missing"
    try:
        value = int(price)
    except (TypeError, ValueError):
        return None, "invalid"
    if value <= 0:
        return None, "invalid"
    # 戸建てとして明らかに異常
    if value < 5_000_000:
        return value, "very_low"
    if value > 500_000_000:
        return value, "very_high"
    # 通常想定
    if 10_000_000 <= value <= 200_000_000:
        return value, "normal"
    return value, "unusual"


def update_price_history(
    existing: Dict[str, Any],
    current_price: Optional[int],
    observed_at: str,
) -> Dict[str, Any]:
    if current_price is None:
        return existing
    history = existing.get("priceHistory") or []
    if not isinstance(history, list):
        history = []

    # 初回価格
    if not existing.get("firstPrice"):
        existing["firstPrice"] = current_price

    # 現在価格
    previous_price = existing.get("currentPrice")
    if previous_price != current_price:
        history.append(
            {
                "price": current_price,
                "observedAt": observed_at,
            }
        )
    existing["priceHistory"] = history
    existing["currentPrice"] = current_price
    first_price = existing.get("firstPrice")
    if first_price and current_price < first_price:
        existing["totalPriceReduction"] = (
            first_price - current_price
        )
        existing["priceReductionRate"] = round(
            (first_price - current_price) / first_price,
            6,
        )
    else:
        existing["totalPriceReduction"] = 0
        existing["priceReductionRate"] = 0
    reductions = 0
    previous = None
    for item in history:
        if isinstance(item, dict):
            price = item.get("price")
            if previous is not None and price is not None and price < previous:
                reductions += 1
            previous = price
    existing["priceReductionCount"] = reductions
    return existing


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


def canonicalize_property_identity(
    property_data: Dict[str, Any],
) -> Optional[str]:

    if not isinstance(
        property_data,
        dict,
    ):
        return None

    property_id = get_property_id(
        property_data
    )

    if not property_id:
        return None

    property_data[
        "propertyId"
    ] = property_id

    property_data[
        "id"
    ] = property_id

    source = infer_source(property_data)
    if source and source != "unknown":
        property_data["source"] = source

    source_id = get_source_id(
        property_data
    )

    if source_id:
        property_data[
            "sourceId"
        ] = source_id

    try:
        identity = make_identity_key(
            property_data
        )

        if isinstance(
            identity,
            dict,
        ):
            if identity.get(
                "identityKey"
            ):
                property_data[
                    "identityKey"
                ] = identity.get(
                    "identityKey"
                )

            if identity.get(
                "identityType"
            ):
                property_data[
                    "identityType"
                ] = identity.get(
                    "identityType"
                )

            if identity.get(
                "identityCompleteness"
            ) is not None:
                property_data[
                    "identityCompleteness"
                ] = identity.get(
                    "identityCompleteness"
                )

    except Exception:
        pass

    return property_id


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

    allowed_city_codes = get_allowed_city_codes_for_area(
        search_area,
        search_config,
        search_urls,
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
# Lightweight Discovery DB
# ============================================================

def build_discovery_record(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

    record: Dict[str, Any] = {}

    for field in DISCOVERY_FIELDS:
        value = property_data.get(field)
        if value is not None:
            record[field] = value

    property_id = (
        property_data.get("propertyId")
        or get_property_id(property_data)
    )

    if property_id:
        record["propertyId"] = property_id

    source_url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
    )

    if source_url:
        record["sourceUrl"] = source_url

    return record


def parse_iso_datetime(
    value: Any,
) -> Optional[datetime]:

    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:
        return None


def should_keep_discovery(
    property_data: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:

    if now is None:
        now = datetime.now(timezone.utc)

    status = property_data.get(
        "status"
    )

    if status == "active":
        return True

    last_seen = parse_iso_datetime(
        property_data.get("lastSeenAt")
    )

    if last_seen is None:
        return True

    age_days = (
        now - last_seen
    ).days

    return (
        age_days <= DISCOVERY_RETENTION_DAYS
    )


def compact_discovery_db(
    properties: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    now = datetime.now(timezone.utc)

    compacted: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for prop in properties:

        if not isinstance(
            prop,
            dict,
        ):
            continue

        if not should_keep_discovery(
            prop,
            now,
        ):
            continue

        record = build_discovery_record(
            prop
        )

        property_id = record.get(
            "propertyId"
        )

        if not property_id:
            continue

        compacted[property_id] = record

    return list(
        compacted.values()
    )


# ============================================================
# Market History DB Selection
# ============================================================

def is_market_history_property(
    property_data: Dict[str, Any],
) -> bool:

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

def get_allowed_city_codes_for_area(
    search_area: Optional[str],
    search_config: Dict[str, Any],
    search_urls: List[Dict[str, Any]],
) -> set[str]:

    normalized_area = normalize_search_area(
        search_area
    )
    codes: set[str] = set()

    for target in search_urls:
        if not isinstance(target, dict):
            continue
        target_area = normalize_search_area(
            target.get("area")
        )
        if target_area != normalized_area:
            continue
        configured = target.get(
            "allowedCityCodes"
        )
        if isinstance(configured, list):
            for code in configured:
                if code:
                    codes.add(str(code))

    if codes:
        return codes

    area_rules = get_area_rules(
        search_config
    )
    rule = area_rules.get(
        normalized_area
    )
    if isinstance(rule, dict):
        configured = rule.get(
            "cityCodes"
        )
        if isinstance(configured, list):
            for code in configured:
                if code:
                    codes.add(str(code))

    return codes


def is_detail_target_property(
    property_data: Dict[str, Any],
    search_config: Dict[str, Any],
    search_urls: List[Dict[str, Any]],
) -> bool:

    if not isinstance(
        property_data,
        dict,
    ):
        return False

    if property_data.get(
        "areaPrefilterExcluded",
        False,
    ):
        return False

    search_area = normalize_search_area(
        property_data.get(
            "searchArea"
        )
    )

    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
    )

    city_code = extract_city_from_url(
        url
    )

    if not city_code:
        return True

    allowed_city_codes = (
        get_allowed_city_codes_for_area(
            search_area,
            search_config,
            search_urls,
        )
    )

    if not allowed_city_codes:
        print(
            "[WARN] 詳細取得対象のcity codeルールが"
            f"未設定: area={search_area}, "
            f"city_code={city_code}"
        )
        return True

    return (
        city_code
        in allowed_city_codes
    )


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

    if property_data.get(
        "detailFetchSuccess"
    ) is True:
        return True

    last_successful = (
        property_data.get(
            "lastSuccessfulDetail"
        )
    )

    if (
        isinstance(
            last_successful,
            dict,
        )
        and last_successful
    ):
        return True

    detail = property_data.get(
        "detail"
    )

    if (
        isinstance(
            detail,
            dict,
        )
        and detail.get(
            "success"
        ) is True
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

def resolve_property_type(
    property_data: Dict[str, Any],
) -> Tuple[Optional[str], str, float]:
    """
    propertyTypeの最終確定（優先順位に基づく）。
    1. sourceUrl / searchUrl などのURLパス (/chukoikkodate/, /ikkodate/) -> 信頼度 1.00
    2. searchPropertyType / searchDetectedPropertyType -> 信頼度 0.95
    3. 詳細ページ由来 propertyType -> 信頼度 0.80
    """
    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
        or property_data.get("searchUrl")
    )
    if url:
        path = urlsplit(str(url)).path.lower()
        if "/chukoikkodate/" in path:
            return "中古戸建", "url", 1.00
        if "/ikkodate/" in path:
            return "新築戸建", "url", 1.00

    for key in [
        "searchPropertyType",
        "searchDetectedPropertyType",
    ]:
        val = normalize_property_type(property_data.get(key))
        if val:
            return val, key, 0.95

    detail = get_detail(property_data)
    if isinstance(detail, dict):
        for key in ["propertyType", "propertyTypeText", "type"]:
            val = normalize_property_type(detail.get(key))
            if val:
                return val, "detail", 0.80

    val = normalize_property_type(property_data.get("propertyType"))
    if val:
        return val, "property_data", 0.70

    return None, "unknown", 0.0


def detect_property_type(
    property_data: Dict[str, Any],
) -> Optional[str]:

    p_type, _, _ = resolve_property_type(property_data)
    return p_type


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

    val = property_data.get("priceYen")
    if val is not None:
        return float(val)

    detail = get_detail(property_data)
    value = detail.get("priceYen") or detail.get("price")

    if value is None:
        value = property_data.get("price")

    if value is None:
        value = property_data.get("currentPrice")

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


def get_target_station_walk_minutes(
    property_data: Dict[str, Any],
) -> Optional[float]:

    detail = get_detail(
        property_data
    )

    available = detail.get(
        "targetStationWalkAvailable"
    )

    if available is True:
        value = detail.get(
            "targetStationWalkMinutes"
        )
        if value is not None:
            return to_number(
                value
            )

    value = property_data.get(
        "targetStationWalkMinutes"
    )

    if value is not None:
        available = property_data.get(
            "targetStationWalkAvailable"
        )
        if available is True:
            return to_number(
                value
            )

    return None


def get_walk_minutes(
    property_data: Dict[str, Any],
) -> Optional[float]:

    return get_target_station_walk_minutes(
        property_data
    )


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

    price_man = (
        int(price / 10_000)
        if price is not None
        else None
    )

    result: Dict[str, Any] = {
        "priceMan": price_man,
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

    max_price = criteria["maxPriceMan"]

    if max_price is None:
        result["priceMatched"] = True
        result["priceReason"] = (
            "price_filter_not_configured"
        )
    elif price_man is None:
        result["priceReason"] = (
            "price_unavailable"
        )
    elif price_man <= max_price:
        result["priceMatched"] = True
        result["priceReason"] = (
            "within_price_limit"
        )
    else:
        result["priceMatched"] = False
        result["priceReason"] = (
            "price_over_limit"
        )

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

    source_url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
    )
    url_city_code = extract_city_from_url(
        source_url
    )
    area_rules = get_area_rules(
        search_config
    )
    rule = area_rules.get(
        search_area,
        {}
    )
    allowed_city_codes = set(
        rule.get(
            "cityCodes",
            []
        )
        if isinstance(rule, dict)
        else []
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
        "areaCityCode": url_city_code,
        "areaCityMatched": (
            url_city_code in allowed_city_codes
            if url_city_code and allowed_city_codes
            else None
        ),
    }

    if not address:
        result[
            "areaValidationReason"
        ] = "address_unavailable"
        return result

    if detected_area is None:
        if (
            url_city_code
            and allowed_city_codes
            and url_city_code in allowed_city_codes
        ):
            result["areaMatched"] = None
            result[
                "areaValidationReason"
            ] = (
                "city_matched_strict_address_pattern_unmatched"
            )
            return result
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

    if "/chukoikkodate/" in result["sourceUrl"].lower():
        result["searchPropertyType"] = "中古戸建"
        result["propertyType"] = "中古戸建"
        result["propertyTypeSource"] = "search_url"
        result["propertyTypeConfidence"] = "high"
    elif "/ikkodate/" in result["sourceUrl"].lower():
        result["searchPropertyType"] = "新築戸建"
        result["propertyType"] = "新築戸建"
        result["propertyTypeSource"] = "search_url"
        result["propertyTypeConfidence"] = "high"

    result["source"] = infer_source(result)

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

    result["listingTitle"] = (
        clean_text(
            result.get("listingTitle")
        )
        or clean_text(
            result.get("searchTitle")
        )
        or None
    )
    result["searchTitle"] = (
        clean_text(
            result.get("searchTitle")
        )
        or result.get("listingTitle")
        or None
    )
    result["name"] = (
        clean_text(
            result.get("name")
        )
        or result.get("listingTitle")
        or ""
    )

    result["price"] = to_number(
        result.get("price")
    )

    if result["price"] is not None:
        result["priceMan"] = int(
            result["price"] / 10_000
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

def build_detail_quality_reasons(
    detail: Dict[str, Any],
    has_valid_price: bool = False,
    has_valid_property_type: bool = False,
) -> List[str]:

    reasons: List[str] = []
    
    missing_critical = detail.get("missingCriticalFields") or []
    missing_important = detail.get("missingImportantFields") or []
    weak_extraction = detail.get("weakExtractionFields") or []
    warnings = detail.get("validationWarnings") or []

    for field in missing_critical:
        if has_valid_price and field in ("price", "priceYen"):
            continue
        if has_valid_property_type and field == "propertyType":
            continue
        reasons.append(f"critical_missing:{field}")

    for field in missing_important:
        reasons.append(f"important_missing:{field}")

    for field in weak_extraction:
        if has_valid_property_type and field == "propertyType":
            continue
        reasons.append(f"weak_extraction:{field}")

    for warning in warnings:
        if has_valid_price and ("価格" in str(warning) or "price" in str(warning).lower()):
            continue
        reasons.append(f"validation_warning:{warning}")

    return reasons


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
            "detailFetchSuccess"
        ] = False

        property_data[
            "detailFetchErrorType"
        ] = detail_res.get(
            "errorType",
            "unknown_error",
        )

        return property_data

    detail_content = detail_res.get(
        "detail"
    )

    if not isinstance(
        detail_content,
        dict,
    ):
        property_data[
            "detailFetchStatus"
        ] = "error"

        property_data[
            "detailFetchSuccess"
        ] = False

        property_data[
            "detailFetchErrorType"
        ] = "parser_error"

        return property_data

    property_data[
        "detailFetchStatus"
    ] = "success"

    property_data[
        "detailFetchSuccess"
    ] = True

    property_data[
        "detailFetchErrorType"
    ] = None

    property_data[
        "lastDetailFetchAt"
    ] = attempt_at

    property_data[
        "lastSuccessfulDetailAt"
    ] = attempt_at

    detail_title = clean_text(
        detail_content.get("title")
    )
    if detail_title:
        property_data["name"] = detail_title
        property_data["listingTitle"] = detail_title

    property_data[
        "detail"
    ] = detail_content

    property_data[
        "lastSuccessfulDetail"
    ] = deepcopy(
        detail_content
    )

    # --------------------------------------------------------
    # 価格情報の連携 & バリデーション＆不整合排除
    # --------------------------------------------------------
    raw_price = detail_content.get("priceYen") or detail_content.get("price")
    property_data["priceRaw"] = detail_content.get("priceRaw")
    property_data["priceConfidence"] = detail_content.get("priceConfidence")
    property_data["priceWarning"] = detail_content.get("priceWarning")
    property_data["priceSource"] = detail_content.get("priceSource")

    validated_price, price_status = validate_sale_price(raw_price)
    property_data["priceYen"] = validated_price
    property_data["priceStatus"] = price_status

    if price_status in ("very_low", "very_high"):
        property_data["priceConfidence"] = "low"
        property_data["priceWarning"] = "販売価格が異常値の可能性"

    if validated_price is not None:
        property_data["price"] = validated_price
        property_data["priceMan"] = int(validated_price / 10_000)
        property_data["currentPrice"] = validated_price
        property_data["currentPriceMan"] = int(validated_price / 10_000)
        
        # 抽出成功時に価格欠損系メッセージ・警告をクリア
        if "missingCriticalFields" in detail_content:
            detail_content["missingCriticalFields"] = [
                f for f in detail_content.get("missingCriticalFields", [])
                if f not in ("price", "priceYen")
            ]
        if "validationWarnings" in detail_content:
            detail_content["validationWarnings"] = [
                w for w in detail_content.get("validationWarnings", [])
                if "価格" not in str(w) and "price" not in str(w).lower()
            ]

    # 物件種別の判定・確定
    resolved_pt, pt_source, pt_confidence = resolve_property_type(property_data)
    if resolved_pt:
        property_data["propertyType"] = resolved_pt
        property_data["propertyTypeSource"] = pt_source
        property_data["propertyTypeConfidence"] = pt_confidence
        # propertyTypeがURLや検索から確定している場合、弱抽出の警告を除外
        if "weakExtractionFields" in detail_content:
            detail_content["weakExtractionFields"] = [
                f for f in detail_content.get("weakExtractionFields", [])
                if f != "propertyType"
            ]

    # 価格監査ログ出力
    p_id = property_data.get("propertyId")
    p_yen = property_data.get("priceYen")
    p_raw = property_data.get("priceRaw") or ""
    p_conf = property_data.get("priceConfidence") or "missing"
    p_stat = property_data.get("priceStatus") or "unknown"
    print(
        f"[PRICE-AUDIT] propertyId={p_id} price={p_yen} raw=\"{p_raw}\" confidence={p_conf} status={p_stat}"
    )

    quality_fields = [
        "detailQuality",
        "detailQualityScore",
        "missingFields",
        "missingCriticalFields",
        "missingImportantFields",
        "validationWarnings",
        "weakExtractionFields",
        "poorReasonCategory",
        "targetStationWalkWarning",
    ]
    for field in quality_fields:
        if field in detail_content:
            property_data[field] = deepcopy(
                detail_content[field]
            )

    property_data[
        "detailQualityReasons"
    ] = build_detail_quality_reasons(
        detail_content,
        has_valid_price=(validated_price is not None),
        has_valid_property_type=(resolved_pt is not None),
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

    if (
        detail_content.get(
            "targetStationWalkAvailable"
        )
        is True
        and detail_content.get(
            "targetStationWalkMinutes"
        )
        is not None
    ):
        target_walk = to_number(
            detail_content.get(
                "targetStationWalkMinutes"
            )
        )
        property_data[
            "targetStationWalkMinutes"
        ] = target_walk
        property_data[
            "targetStationWalkAvailable"
        ] = True
        property_data[
            "targetStation"
        ] = detail_content.get(
            "targetStation"
        )
        property_data[
            "targetStationWalkSource"
        ] = detail_content.get(
            "targetStationWalkSource"
        )
        property_data[
            "walk"
        ] = target_walk
    else:
        property_data[
            "targetStationWalkMinutes"
        ] = None
        property_data[
            "targetStationWalkAvailable"
        ] = False
        property_data[
            "targetStation"
        ] = None
        property_data[
            "targetStationWalkSource"
        ] = None
        property_data[
            "walk"
        ] = None

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

    numeric_eval = evaluate_numeric_criteria(
        property_data,
        search_config,
    )

    property_data.update(
        numeric_eval
    )

    excluded_reason = (
        assign_area_excluded_reason(
            property_data
        )
    )

    property_data[
        "areaExcludedReason"
    ] = excluded_reason

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
    # 価格履歴更新 (共通 update_price_history 使用)
    # --------------------------------------------------------
    current_price = property_data.get("priceYen")
    if current_price is None:
        p_num = to_number(property_data.get("price"))
        if p_num is not None:
            current_price = int(p_num)

    observed_at = (
        property_data.get("lastSeenAt")
        or property_data.get("firstSeenAt")
        or now_iso()
    )

    update_price_history(property_data, current_price, observed_at)

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
# Observation & Market House Record
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
                    "priceYen"
                )
                if property_data.get(
                    "priceYen"
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

        "priceConfidence": property_data.get("priceConfidence"),
        "priceStatus": property_data.get("priceStatus"),

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
            property_data.get(
                "detailQuality"
            )
            or detail.get(
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


def build_market_house_record(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

    detail = get_detail(property_data)
    record = deepcopy(property_data)

    record["detailFetchStatus"] = property_data.get(
        "detailFetchStatus"
    )
    record["detailFetchSuccess"] = property_data.get(
        "detailFetchSuccess"
    )
    record["lastDetailAttemptAt"] = property_data.get(
        "lastDetailAttemptAt"
    )
    record["lastDetailFetchAt"] = property_data.get(
        "lastDetailFetchAt"
    )
    record["lastSuccessfulDetailAt"] = property_data.get(
        "lastSuccessfulDetailAt"
    )

    # 必須欠損項目のクリーンアップ（価格取得成功時の除外）
    missing_crit = list(
        property_data.get("missingCriticalFields")
        or detail.get("missingCriticalFields", [])
    )
    if property_data.get("priceYen") is not None:
        missing_crit = [f for f in missing_crit if f not in ("price", "priceYen")]

    missing_imp = list(
        property_data.get("missingImportantFields")
        or detail.get("missingImportantFields", [])
    )

    record["missingCriticalFields"] = missing_crit
    record["missingImportantFields"] = missing_imp

    # 警告処理および弱抽出の再構築
    warnings = list(
        property_data.get("validationWarnings")
        or detail.get("validationWarnings", [])
    )
    weak_fields = list(
        property_data.get("weakExtractionFields")
        or detail.get("weakExtractionFields", [])
    )

    # 価格取得成功時は「価格が抽出できない」系の警告を除外
    if property_data.get("priceYen") is not None:
        warnings = [w for w in warnings if "価格" not in str(w) and "price" not in str(w).lower()]

    price_warning = property_data.get("priceWarning")
    if price_warning and price_warning not in warnings:
        warnings.append(price_warning)

    price_confidence = property_data.get("priceConfidence")
    if price_confidence == "low" and "price" not in weak_fields:
        weak_fields.append("price")

    # propertyTypeが確定している場合はweakExtractionFieldsから除外
    p_type = property_data.get("propertyType")
    if p_type and "propertyType" in weak_fields:
        weak_fields.remove("propertyType")

    record["validationWarnings"] = warnings
    record["weakExtractionFields"] = weak_fields

    # 品質評価（detailQuality）の再計算・更新
    if missing_crit:
        record["detailQuality"] = "partial"
    elif property_data.get("detailQuality") or detail.get("detailQuality"):
        # クリティカル欠損がない場合は、更新された品質を採用
        record["detailQuality"] = property_data.get("detailQuality") or detail.get("detailQuality")

    record["detailQualityScore"] = (
        property_data.get("detailQualityScore")
        or detail.get("detailQualityScore")
    )
    record["detailQualityReasons"] = build_detail_quality_reasons(
        record,
        has_valid_price=(property_data.get("priceYen") is not None),
        has_valid_property_type=(p_type is not None),
    )

    record["targetStation"] = (
        property_data.get("targetStation")
        or detail.get("targetStation")
    )
    record["targetStationWalkMinutes"] = (
        property_data.get("targetStationWalkMinutes")
        if property_data.get("targetStationWalkMinutes") is not None
        else detail.get("targetStationWalkMinutes")
    )
    record["targetStationWalkAvailable"] = (
        property_data.get("targetStationWalkAvailable")
        if property_data.get("targetStationWalkAvailable") is not None
        else detail.get("targetStationWalkAvailable")
    )
    record["targetStationWalkSource"] = (
        property_data.get("targetStationWalkSource")
        or detail.get("targetStationWalkSource")
    )

    return record


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

    target = (
        candidate.get("searchTarget")
        or candidate.get("searchTargetArea")
    )
    search_area = (
        candidate.get("searchTargetArea")
        or candidate.get("searchArea")
    )
    property_type = (
        candidate.get("searchTargetPropertyType")
        or candidate.get("searchPropertyType")
    )

    existing["searchTarget"] = target
    existing["searchTargetArea"] = search_area
    existing["searchTargetPropertyType"] = property_type
    existing["searchPageNumber"] = candidate.get(
        "searchPageNumber"
    )
    existing["searchPosition"] = candidate.get(
        "searchPosition"
    )
    existing["searchUrl"] = candidate.get(
        "searchUrl"
    )
    existing["searchPageUrl"] = candidate.get(
        "searchPageUrl"
    )


# ============================================================
# Search Adapter Compatibility
# ============================================================

def fetch_search_target(
    search_adapter: Any,
    url: str,
    target: Dict[str, Any],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:

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

    houses_raw = load_json(
        HOUSES_PATH,
        default={
            "properties": []
        },
    )

    if isinstance(
        houses_raw,
        dict,
    ):
        house_properties = houses_raw.get(
            "properties",
            [],
        )
    elif isinstance(
        houses_raw,
        list,
    ):
        house_properties = houses_raw
    else:
        house_properties = []

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
        discovered_properties = (
            discovered_raw.get(
                "properties",
                [],
            )
        )
    elif isinstance(
        discovered_raw,
        list,
    ):
        discovered_properties = discovered_raw
    else:
        discovered_properties = []

    db: Dict[
        str,
        Dict[str, Any],
    ] = {}

    house_identity_repaired_count = 0
    house_duplicate_canonical_count = 0

    for index, prop in enumerate(
        house_properties
    ):
        if not isinstance(
            prop,
            dict,
        ):
            continue

        prop = deepcopy(
            prop
        )

        before_property_id = (
            prop.get(
                "propertyId"
            )
        )

        pid = canonicalize_property_identity(
            prop
        )

        if not pid:
            continue

        after_property_id = (
            prop.get(
                "propertyId"
            )
        )

        if (
            before_property_id
            != after_property_id
        ):
            house_identity_repaired_count += 1

        prop[
            "seenThisRun"
        ] = False

        if pid in db:
            house_duplicate_canonical_count += 1
            existing = db[pid]
            for key, value in prop.items():
                if (
                    existing.get(key) is None
                    and value is not None
                ):
                    existing[key] = value
        else:
            db[pid] = prop

    for index, prop in enumerate(
        discovered_properties
    ):
        if not isinstance(
            prop,
            dict,
        ):
            continue

        prop = deepcopy(
            prop
        )

        pid = canonicalize_property_identity(
            prop
        )

        if not pid:
            continue

        if pid not in db:
            prop["seenThisRun"] = False
            db[pid] = prop

    # --------------------------------------------------------
    # 3. Search Crawling
    # --------------------------------------------------------

    search_adapter = SuumoSearchAdapter(
        config=search_config
    )

    search_success_count = 0
    search_failed_count = 0

    search_total_candidates = 0
    search_total_new = 0
    search_total_existing = 0

    run_start_iso = now_iso()

    for target in search_urls:
        name = target.get("name", "Unknown")
        url = target.get("url")

        if not url:
            continue

        print(f"--- Crawling target: {name} ({url}) ---")

        try:
            results = fetch_search_target(
                search_adapter,
                url,
                target,
                search_config,
            )

            search_success_count += 1
            search_total_candidates += len(results)
            print(f"  取得件数: {len(results)}件")

            for item in results:
                normalized = normalize_search_result(item)
                normalized = apply_url_area_prefilter(
                    normalized,
                    search_config,
                    search_urls,
                )

                pid = normalized["propertyId"]

                if pid in db:
                    search_total_existing += 1
                    existing = db[pid]
                    existing["seenThisRun"] = True
                    existing["lastSeenAt"] = run_start_iso
                    existing["status"] = "active"

                    if not existing.get("searchArea"):
                        existing["searchArea"] = normalized.get("searchArea")

                    search_targets = append_unique(
                        existing.get("searchTargets", []),
                        name,
                    )
                    existing["searchTargets"] = search_targets

                    merge_search_occurrence(existing, normalized)

                else:
                    search_total_new += 1
                    normalized["seenThisRun"] = True
                    normalized["firstSeenAt"] = run_start_iso
                    normalized["lastSeenAt"] = run_start_iso
                    normalized["status"] = "active"
                    normalized["searchTargets"] = [name]
                    normalized["detailFetchStatus"] = "pending"
                    normalized["detailFetchSuccess"] = None

                    db[pid] = normalized

        except Exception as exc:
            search_failed_count += 1
            print(f"[ERROR] ターゲットクロール失敗 {name}: {exc}")

    search_healthy = (search_failed_count == 0)

    # --------------------------------------------------------
    # 4. Update Lifecycle & Criteria for all DB items
    # --------------------------------------------------------

    for pid, prop in db.items():
        update_property_lifecycle(
            prop,
            run_start_iso,
            search_healthy=search_healthy,
        )
        evaluate_property_criteria(
            prop,
            search_config,
        )

    # --------------------------------------------------------
    # 5. Detail Fetch Queue Audit & Processing
    # --------------------------------------------------------

    to_fetch: List[Dict[str, Any]] = []

    for pid, prop in db.items():
        if not is_detail_target_property(
            prop,
            search_config,
            search_urls,
        ):
            continue

        priority = detail_fetch_priority(prop)

        if priority < 999:
            to_fetch.append(prop)

    detail_fetch_limit = search_config.get(
        "detailFetchLimit",
        DEFAULT_DETAIL_FETCH_LIMIT,
    )

    to_fetch = to_fetch[:detail_fetch_limit]

    print(
        f"[INFO] 詳細取得キュー: {len(to_fetch)}件 "
        f"(Limit: {detail_fetch_limit})"
    )

    # --------------------------------------------------------
    # Execute Detail Fetch
    # --------------------------------------------------------

    detail_adapter = SuumoDetailAdapter(
        config=search_config
    )

    consecutive_errors = 0
    detail_success_count = 0
    detail_error_count = 0

    interval = search_config.get(
        "detailRequestIntervalSeconds",
        1.5,
    )

    for index, prop in enumerate(to_fetch):
        url = (
            prop.get("sourceUrl")
            or prop.get("url")
        )

        if not url:
            continue

        if index > 0 and interval > 0:
            time.sleep(interval)

        print(
            f"[{index + 1}/{len(to_fetch)}] 詳細取得試行: "
            f"{prop.get('propertyId')} ({url})"
        )

        try:
            detail_res = detail_adapter.fetch_detail(url)

            enrich_with_detail(prop, detail_res)

            evaluate_property_criteria(
                prop,
                search_config,
            )

            if prop.get("detailFetchStatus") == "error":
                detail_error_count += 1
                error_type = prop.get(
                    "detailFetchErrorType",
                    "unknown_error",
                )
                print(
                    f"  [FAIL] 詳細取得エラー: {error_type}"
                )
                if error_type in RETRYABLE_DETAIL_ERROR_TYPES:
                    consecutive_errors += 1
                    if (
                        consecutive_errors
                        >= MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS
                    ):
                        print(
                            f"[WARN] 接続系エラーが{consecutive_errors}回連続したため途中終了します。"
                        )
                        break
                else:
                    consecutive_errors = 0
            else:
                detail_success_count += 1
                consecutive_errors = 0
                print("  [SUCCESS] 詳細取得成功")

        except Exception as exc:
            detail_error_count += 1
            print(f"  [ERROR] 詳細取得例外発生: {exc}")
            prop["detailFetchStatus"] = "error"
            prop["detailFetchSuccess"] = False
            prop["detailFetchErrorType"] = "exception"
            consecutive_errors += 1
            if (
                consecutive_errors
                >= MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS
            ):
                print(f"[WARN] 詳細取得例外が{consecutive_errors}回連続したため途中終了します。")
                break

    # --------------------------------------------------------
    # 6. Save Data Construction & Consistency Checks
    # --------------------------------------------------------

    properties_final: List[Dict[str, Any]] = []
    seen_final_pids: Set[str] = set()

    for pid, prop in db.items():
        evaluate_property_criteria(
            prop,
            search_config,
        )

        if is_market_history_property(prop):
            record = build_market_house_record(prop)

            record_pid = record["propertyId"]
            if record_pid in seen_final_pids:
                raise RuntimeError(
                    f"Duplicate propertyId in properties_final: {record_pid}"
                )
            seen_final_pids.add(record_pid)

            properties_final.append(record)

    # 価格評価集計ログ [PRICE-AUDIT]
    price_high = sum(1 for p in properties_final if p.get("priceConfidence") == "high")
    price_medium = sum(1 for p in properties_final if p.get("priceConfidence") == "medium")
    price_low = sum(1 for p in properties_final if p.get("priceConfidence") == "low")
    price_missing = sum(1 for p in properties_final if not p.get("priceConfidence"))
    print(
        f"[PRICE-AUDIT] high={price_high} medium={price_medium} low={price_low} missing={price_missing}"
    )

    partial_items = [
        item
        for item in properties_final
        if item.get("detailQuality") == "partial"
    ]
    print(f"[QUALITY-AUDIT] partial={len(partial_items)}")

    # --------------------------------------------------------
    # 7. Discovered, Observations, and Summary
    # --------------------------------------------------------

    discovered_final = compact_discovery_db(list(db.values()))

    observations_raw = load_json(OBSERVATIONS_PATH, default=[])
    if not isinstance(observations_raw, list):
        observations_raw = []

    current_observations = [
        build_listing_observation(prop)
        for prop in db.values()
        if prop.get("status") == "active"
    ]

    observations_all = observations_raw + current_observations

    summary_data = {
        "updatedAt": now_iso(),
        "parserVersion": MAIN_PARSER_VERSION,
        "totalActiveProperties": len(
            [p for p in properties_final if p.get("status") == "active"]
        ),
        "totalMarketProperties": len(properties_final),
        "totalDiscoveredProperties": len(discovered_final),
        "searchCrawlsSuccessful": search_success_count,
        "searchCrawlsFailed": search_failed_count,
        "partialQualityCount": len(partial_items),
        "priceAudit": {
            "high": price_high,
            "medium": price_medium,
            "low": price_low,
            "missing": price_missing,
        },
    }

    # --------------------------------------------------------
    # 8. Save JSON
    # --------------------------------------------------------

    updated_at = now_iso()

    houses_output = {
        "updatedAt": updated_at,
        "schemaVersion": HOUSE_DB_SCHEMA_VERSION,
        "parserVersion": MAIN_PARSER_VERSION,
        "properties": properties_final,
        "summary": {
            "marketDbCount": len(properties_final),
            "marketHistoryCount": len(properties_final),
            "marketHistoryActiveCount": len([
                p for p in properties_final if p.get("status") == "active"
            ]),
            "marketHistoryEndedCount": len([
                p for p in properties_final if p.get("status") == "observed_ended"
            ]),
            "partialQualityCount": len(partial_items),
        },
    }

    discovered_output = {
        "updatedAt": updated_at,
        "schemaVersion": DISCOVERY_SCHEMA_VERSION,
        "parserVersion": MAIN_PARSER_VERSION,
        "properties": discovered_final,
        "summary": {
            "discoveredCount": len(discovered_final),
            "activeCount": len([
                p for p in discovered_final if p.get("status") == "active"
            ]),
            "endedCount": len([
                p for p in discovered_final if p.get("status") == "observed_ended"
            ]),
        },
    }

    save_json(HOUSES_PATH, houses_output)
    save_json(DISCOVERED_PATH, discovered_output)
    save_json(OBSERVATIONS_PATH, observations_all)
    save_json(SUMMARY_PATH, summary_data)

    print("============================================================")
    print("=== Scraping Pipeline Completed Successfully ===")
    print(f"=== houses.json: {len(properties_final)} items ===")
    print("============================================================")


if __name__ == "__main__":
    run_pipeline()
