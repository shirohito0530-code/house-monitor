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

MAIN_PARSER_VERSION = "2026-10-04-v40-broad-search-narrow-storage"


# ============================================================
# Discovery DB retention / schema
# ============================================================

DISCOVERY_SCHEMA_VERSION = "2.0"
DISCOVERY_RETENTION_DAYS = 180

DISCOVERY_FIELDS = (
    # --------------------------------------------------------
    # Identity
    # --------------------------------------------------------
    "propertyId",
    "source",
    "sourceId",
    "sourceUrl",

    # --------------------------------------------------------
    # Listing / Search
    # --------------------------------------------------------
    "name",
    "listingTitle",
    "searchTitle",
    "searchArea",
    "searchPropertyType",

    # --------------------------------------------------------
    # Address / Area
    # --------------------------------------------------------
    "address",
    "detectedArea",
    "areaDetected",
    "areaClassification",
    "areaStatus",
    "area",
    "areaCandidate",
    "areaMatched",
    "areaValidationReason",
    "urlCityCode",
    "areaCityMatched",

    # --------------------------------------------------------
    # Station
    # --------------------------------------------------------
    "targetStation",
    "targetStationWalkMinutes",
    "stationWalkMinutes",
    "targetStationWalkAvailable",
    "targetStationWalkSource",
    "walkMinutes",
    "walkMatched",

    # --------------------------------------------------------
    # Criteria
    # --------------------------------------------------------
    "propertyTypeMatched",
    "builtAgeMatched",
    "priceMatched",
    "landAreaMatched",
    "buildingAreaMatched",
    "searchCriteriaMatched",

    # --------------------------------------------------------
    # Price
    # --------------------------------------------------------
    "priceYen",
    "priceMan",
    "currentPrice",
    "currentPriceMan",
    "searchPriceYen",
    "searchPriceMan",
    "priceConfidence",
    "priceStatus",

    # --------------------------------------------------------
    # Detail
    # --------------------------------------------------------
    "detailQuality",
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


def validate_sale_price(
    price: Any,
) -> Tuple[Optional[int], Optional[str]]:

    if price is None:
        return None, "missing"

    try:
        value = int(price)
    except (TypeError, ValueError):
        return None, "invalid"

    if value <= 0:
        return None, "invalid"

    if value < 5_000_000:
        return value, "very_low"

    if value > 500_000_000:
        return value, "very_high"

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

    if not existing.get("firstPrice"):
        existing["firstPrice"] = current_price

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
    existing["priceYen"] = current_price
    existing["price"] = current_price

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

            if (
                previous is not None
                and price is not None
                and price < previous
            ):
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

    allowed_city_codes = (
        get_allowed_city_codes_for_area(
            search_area,
            search_config,
            search_urls,
        )
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
        ] = False

        property_data[
            "areaPrefilterReason"
        ] = f"city_mismatch_kept_for_station_evaluation_{city_code}"

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

def should_store_house(
    property_data: Dict[str, Any],
) -> bool:

    required_flags = (
        "builtAgeMatched",
        "propertyTypeMatched",
        "priceMatched",
        "walkMatched",
        "landAreaMatched",
        "buildingAreaMatched",
    )

    for field in required_flags:

        if property_data.get(field) is not True:
            return False

    return True


def is_market_history_property(
    property_data: Dict[str, Any],
) -> bool:

    if not isinstance(
        property_data,
        dict,
    ):
        return False

    status = property_data.get(
        "status"
    )

    if status not in HOUSE_DB_STATUSES:
        return False

    if not should_store_house(
        property_data
    ):
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

        if not isinstance(
            target,
            dict,
        ):
            continue

        target_area = normalize_search_area(
            target.get("area")
        )

        if target_area != normalized_area:
            continue

        configured = target.get(
            "allowedCityCodes"
        )

        if isinstance(
            configured,
            list,
        ):

            for code in configured:

                if code:
                    codes.add(
                        str(code)
                    )

    if codes:
        return codes

    area_rules = get_area_rules(
        search_config
    )

    rule = area_rules.get(
        normalized_area
    )

    if isinstance(
        rule,
        dict,
    ):

        configured = rule.get(
            "cityCodes"
        )

        if isinstance(
            configured,
            list,
        ):

            for code in configured:

                if code:
                    codes.add(
                        str(code)
                    )

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

    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
    )

    if not url:
        return False

    return True


def get_detail(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

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
        return last_detail

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

    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
        or property_data.get("searchUrl")
    )

    if url:

        path = urlsplit(
            str(url)
        ).path.lower()

        if "/chukoikkodate/" in path:
            return "中古戸建", "url", 1.00

        if "/ikkodate/" in path:
            return "新築戸建", "url", 1.00

    for key in [
        "searchPropertyType",
        "searchDetectedPropertyType",
    ]:

        val = normalize_property_type(
            property_data.get(key)
        )

        if val:
            return val, key, 0.95

    detail = get_detail(
        property_data
    )

    if isinstance(
        detail,
        dict,
    ):

        for key in [
            "propertyType",
            "propertyTypeText",
            "type",
        ]:

            val = normalize_property_type(
                detail.get(key)
            )

            if val:
                return val, "detail", 0.80

    val = normalize_property_type(
        property_data.get(
            "propertyType"
        )
    )

    if val:
        return val, "property_data", 0.70

    return None, "unknown", 0.0


def detect_property_type(
    property_data: Dict[str, Any],
) -> Optional[str]:

    p_type, _, _ = resolve_property_type(
        property_data
    )

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

    detail = get_detail(
        property_data
    )

    detail_price = (
        detail.get("priceYen")
        or detail.get("currentPrice")
        or detail.get("price")
    )

    if detail_price is not None:

        num = to_number(
            detail_price
        )

        if num is not None:
            return num

    val = (
        property_data.get("priceYen")
        or property_data.get("currentPrice")
        or property_data.get("price")
    )

    if val is not None:
        return to_number(val)

    search_price = (
        property_data.get("searchPriceYen")
        or property_data.get("searchPrice")
    )

    if search_price is not None:
        return to_number(search_price)

    return None


def get_land_area(
    property_data: Dict[str, Any],
) -> Optional[float]:

    detail = get_detail(
        property_data
    )

    value = detail.get(
        "landAreaM2"
    )

    if value is None:
        value = property_data.get(
            "land"
        )

    return to_number(
        value
    )


def get_building_area(
    property_data: Dict[str, Any],
) -> Optional[float]:

    detail = get_detail(
        property_data
    )

    value = detail.get(
        "buildingAreaM2"
    )

    if value is None:
        value = property_data.get(
            "building"
        )

    return to_number(
        value
    )


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

    max_price = criteria[
        "maxPriceMan"
    ]

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

    max_walk = criteria[
        "maxWalkMinutes"
    ]

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

    min_land = criteria[
        "minLandArea"
    ]

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

    if not address:
        address = clean_text(
            property_data.get("address")
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
        if isinstance(
            rule,
            dict,
        )
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
            if (
                url_city_code
                and allowed_city_codes
            )
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

            result[
                "areaMatched"
            ] = None

            result[
                "areaValidationReason"
            ] = (
                "city_matched_strict_address_pattern_unmatched"
            )

            return result

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


def assign_area_excluded_reason(
    property_data: Dict[str, Any],
) -> Optional[str]:

    classification = property_data.get(
        "areaClassification"
    )

    if classification == "primaryTarget":
        return None

    if classification == "subTarget":
        return "subTarget"

    if classification == "outOfTarget":
        return "outOfTarget"

    if classification == "detailPending":
        return "detailPending"

    return None


# ============================================================
# School District Candidate
# ============================================================

SCHOOL_DISTRICT_SCHEMA_VERSION = "1.0"

KASHIWA_HANNOHA_SCHOOL_CANDIDATE_PATTERNS = [
    "柏の葉",
    "若柴",
    "正連寺",
    "中十余二",
]


def evaluate_school_district(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:

    search_area = normalize_search_area(
        property_data.get("searchArea")
    )

    detail = get_detail(
        property_data
    )

    address = (
        clean_text(
            detail.get("address")
        )
        or clean_text(
            property_data.get("address")
        )
        or ""
    )

    normalized_address = (
        normalize_address_for_area(address)
        or ""
    )

    result = {

        "schoolDistrictSchemaVersion":
            SCHOOL_DISTRICT_SCHEMA_VERSION,

        "schoolDistrictStatus":
            "unknown",

        "schoolDistrictCandidate":
            False,

        "schoolDistrictCandidateArea":
            None,

        "schoolDistrictCandidateReason":
            None,

        "schoolDistrictCandidateConfidence":
            "none",

        "schoolDistrictDecision":
            "unconfirmed",

        "schoolDistrictDecisionLabel":
            "未確認",

        "schoolDistrictVerification":
            "manual",

        "schoolDistrictPolicy":
            "ui_manual_candidate",

        "schoolDistrictAddress":
            address or None,

        "schoolDistrictNote":
            (
                "学区は番地等による正式確認が必要です。"
                "本項目は候補抽出のみで、自動的な学区合否判定には使用しません。"
            ),
    }

    if search_area != "柏の葉キャンパス":

        result.update(
            {
                "schoolDistrictStatus":
                    "not_target_area",

                "schoolDistrictCandidate":
                    False,

                "schoolDistrictCandidateArea":
                    None,

                "schoolDistrictCandidateReason":
                    "柏の葉キャンパス検索対象ではない",

                "schoolDistrictCandidateConfidence":
                    "none",

                "schoolDistrictDecision":
                    "not_applicable",

                "schoolDistrictDecisionLabel":
                    "対象外",
            }
        )

        return result

    if not normalized_address:

        result.update(
            {
                "schoolDistrictStatus":
                    "unknown",

                "schoolDistrictCandidate":
                    False,

                "schoolDistrictCandidateArea":
                    "柏の葉小学校区",

                "schoolDistrictCandidateReason":
                    "住所情報なし",

                "schoolDistrictCandidateConfidence":
                    "none",
            }
        )

        return result

    is_candidate = any(
        pattern in normalized_address
        for pattern
        in KASHIWA_HANNOHA_SCHOOL_CANDIDATE_PATTERNS
    )

    if is_candidate:

        result.update(
            {
                "schoolDistrictStatus":
                    "candidate",

                "schoolDistrictCandidate":
                    True,

                "schoolDistrictCandidateArea":
                    "柏の葉小学校区",

                "schoolDistrictCandidateReason":
                    "候補住所パターンに合致",

                "schoolDistrictCandidateConfidence":
                    "address_based",
            }
        )

    else:

        result.update(
            {
                "schoolDistrictStatus":
                    "unmatched",

                "schoolDistrictCandidate":
                    False,

                "schoolDistrictCandidateArea":
                    "柏の葉小学校区",

                "schoolDistrictCandidateReason":
                    "候補住所パターン不一致",

                "schoolDistrictCandidateConfidence":
                    "none",
            }
        )

    return result


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

    if "/chukoikkodate/" in result[
        "sourceUrl"
    ].lower():

        result["searchPropertyType"] = "中古戸建"
        result["propertyType"] = "中古戸建"
        result["propertyTypeSource"] = "search_url"
        result["propertyTypeConfidence"] = "high"

    elif "/ikkodate/" in result[
        "sourceUrl"
    ].lower():

        result["searchPropertyType"] = "新築戸建"
        result["propertyType"] = "新築戸建"
        result["propertyTypeSource"] = "search_url"
        result["propertyTypeConfidence"] = "high"

    result["source"] = infer_source(
        result
    )

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

    raw_price = (
        result.get("searchPriceYen")
        or result.get("searchPrice")
        or result.get("priceYen")
        or result.get("price")
    )

    search_price_num = to_number(
        raw_price
    )

    if search_price_num is not None:

        search_price_val = int(
            search_price_num
        )

        result[
            "searchPriceYen"
        ] = search_price_val

        result[
            "searchPriceMan"
        ] = int(
            search_price_val / 10_000
        )

    else:

        result[
            "searchPriceYen"
        ] = None

        result[
            "searchPriceMan"
        ] = None

    effective_price = (
        result["searchPriceYen"]
    )

    result[
        "priceYen"
    ] = effective_price

    result[
        "currentPrice"
    ] = effective_price

    result[
        "price"
    ] = effective_price

    if effective_price is not None:

        result[
            "priceMan"
        ] = int(
            effective_price / 10_000
        )

        result[
            "currentPriceMan"
        ] = int(
            effective_price / 10_000
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

    missing_critical = (
        detail.get("missingCriticalFields")
        or []
    )

    missing_important = (
        detail.get("missingImportantFields")
        or []
    )

    weak_extraction = (
        detail.get("weakExtractionFields")
        or []
    )

    warnings = (
        detail.get("validationWarnings")
        or []
    )

    walk_fields = {
        "targetStationWalkMinutes",
        "walkMinutes",
        "targetStation",
        "walk",
    }

    for field in missing_critical:

        if (
            has_valid_price
            and field
            in (
                "price",
                "priceYen",
                "currentPrice",
            )
        ):
            continue

        if field == "propertyType":
            continue

        if field in walk_fields:
            continue

        reasons.append(
            f"critical_missing:{field}"
        )

    for field in missing_important:

        if field in walk_fields:
            continue

        reasons.append(
            f"important_missing:{field}"
        )

    for field in weak_extraction:

        if field == "propertyType":
            continue

        if field in walk_fields:
            continue

        reasons.append(
            f"weak_extraction:{field}"
        )

    for warning in warnings:

        if (
            has_valid_price
            and (
                "価格" in str(warning)
                or "price"
                in str(warning).lower()
            )
        ):
            continue

        if (
            "徒歩" in str(warning)
            or "walk"
            in str(warning).lower()
            or "駅"
            in str(warning)
        ):
            continue

        reasons.append(
            f"validation_warning:{warning}"
        )

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

        property_data[
            "name"
        ] = detail_title

        property_data[
            "listingTitle"
        ] = detail_title

    property_data[
        "detail"
    ] = detail_content

    property_data[
        "lastSuccessfulDetail"
    ] = deepcopy(
        detail_content
    )

    raw_price = (
        detail_content.get("priceYen")
        or detail_content.get("currentPrice")
        or detail_content.get("price")
    )

    validated_price, price_status = (
        validate_sale_price(
            raw_price
        )
    )

    if validated_price is not None:

        price_source = (
            detail_content.get(
                "priceSource"
            )
            or "detail"
        )

        price_confidence = (
            detail_content.get(
                "priceConfidence"
            )
            or "high"
        )

        price_warning = (
            detail_content.get(
                "priceWarning"
            )
        )

        price_raw_str = (
            detail_content.get(
                "priceRaw"
            )
        )

    else:

        search_price = (
            property_data.get(
                "searchPriceYen"
            )
            or property_data.get(
                "searchPrice"
            )
            or property_data.get(
                "priceYen"
            )
            or property_data.get(
                "price"
            )
        )

        validated_price, price_status = (
            validate_sale_price(
                search_price
            )
        )

        if validated_price is not None:

            price_source = (
                "search_fallback"
            )

            price_confidence = (
                property_data.get(
                    "searchPriceConfidence"
                )
                or "medium"
            )

            price_warning = (
                property_data.get(
                    "searchPriceWarning"
                )
                or
                "詳細ページ価格未取得のため検索結果価格を採用"
            )

            price_raw_str = (
                property_data.get(
                    "searchPriceRaw"
                )
                or str(search_price)
            )

        else:

            price_source = "none"
            price_confidence = "missing"

            price_warning = (
                "価格情報取得不可"
            )

            price_raw_str = None

    property_data[
        "priceYen"
    ] = validated_price

    property_data[
        "currentPrice"
    ] = validated_price

    property_data[
        "price"
    ] = validated_price

    property_data[
        "priceStatus"
    ] = price_status

    property_data[
        "priceRaw"
    ] = price_raw_str

    property_data[
        "priceConfidence"
    ] = price_confidence

    property_data[
        "priceWarning"
    ] = price_warning

    property_data[
        "priceSource"
    ] = price_source

    if price_status in (
        "very_low",
        "very_high",
    ):

        property_data[
            "priceConfidence"
        ] = "low"

        property_data[
            "priceWarning"
        ] = "販売価格が異常値の可能性"

    if validated_price is not None:

        property_data[
            "priceMan"
        ] = int(
            validated_price / 10_000
        )

        property_data[
            "currentPriceMan"
        ] = int(
            validated_price / 10_000
        )

    resolved_pt, pt_source, pt_confidence = (
        resolve_property_type(
            property_data
        )
    )

    if resolved_pt:

        property_data[
            "propertyType"
        ] = resolved_pt

        property_data[
            "propertyTypeSource"
        ] = pt_source

        property_data[
            "propertyTypeConfidence"
        ] = pt_confidence

    p_id = property_data.get(
        "propertyId"
    )

    p_yen = property_data.get(
        "priceYen"
    )

    p_raw = (
        property_data.get(
            "priceRaw"
        )
        or ""
    )

    p_conf = (
        property_data.get(
            "priceConfidence"
        )
        or "missing"
    )

    p_stat = (
        property_data.get(
            "priceStatus"
        )
        or "unknown"
    )

    p_src = (
        property_data.get(
            "priceSource"
        )
        or "unknown"
    )

    print(
        f'[PRICE-AUDIT] propertyId={p_id} '
        f'price={p_yen} raw="{p_raw}" '
        f'confidence={p_conf} source={p_src} '
        f'status={p_stat}'
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

        property_data[
            "landAreaM2"
        ] = property_data[
            "land"
        ]

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

        property_data[
            "buildingAreaM2"
        ] = property_data[
            "building"
        ]

    if detail_content.get(
        "layout"
    ):

        property_data[
            "layout"
        ] = detail_content.get(
            "layout"
        )

    return property_data


def classify_area(
    property_data: Dict[str, Any],
) -> Optional[str]:

    area_matched = property_data.get(
        "areaMatched"
    )

    walk_matched = property_data.get(
        "walkMatched"
    )

    if area_matched is True:
        return "primaryTarget"

    if walk_matched is True:
        return "subTarget"

    if (
        area_matched is False
        and walk_matched is False
    ):
        return "outOfTarget"

    return "detailPending"


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

    # --------------------------------------------------------
    # 修正③: 駅徒歩フィールド名の不一致解消
    # --------------------------------------------------------
    walk_minutes = (
        property_data.get("targetStationWalkMinutes")
        if property_data.get("targetStationWalkMinutes") is not None
        else (
            property_data.get("walkMinutes")
            if property_data.get("walkMinutes") is not None
            else get_walk_minutes(property_data)
        )
    )

    if walk_minutes is not None:
        property_data["stationWalkMinutes"] = walk_minutes
        property_data["targetStationWalkMinutes"] = walk_minutes
        property_data["walkMinutes"] = walk_minutes
    else:
        property_data["stationWalkMinutes"] = None

    criteria_flags = [
        property_data.get(
            "builtAgeMatched"
        ),

        property_data.get(
            "propertyTypeMatched"
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

    is_criteria_matched = (
        not any(
            value is False
            for value in criteria_flags
        )
    )

    property_data[
        "searchCriteriaMatched"
    ] = is_criteria_matched

    property_data[
        "searchResultFilterExcluded"
    ] = not is_criteria_matched

    current_price = (
        property_data.get(
            "priceYen"
        )
    )

    if current_price is None:

        p_num = get_price(
            property_data
        )

        if p_num is not None:

            current_price = int(
                p_num
            )

            property_data[
                "priceYen"
            ] = current_price

            property_data[
                "currentPrice"
            ] = current_price

            property_data[
                "price"
            ] = current_price

            property_data[
                "priceMan"
            ] = int(
                current_price / 10_000
            )

            property_data[
                "currentPriceMan"
            ] = int(
                current_price / 10_000
            )

    observed_at = (
        property_data.get(
            "lastSeenAt"
        )
        or property_data.get(
            "firstSeenAt"
        )
        or now_iso()
    )

    update_price_history(
        property_data,
        current_price,
        observed_at,
    )

    # --------------------------------------------------------
    # 修正①＆②: areaClassification → UI互換フィールド生成
    # --------------------------------------------------------
    classification = classify_area(property_data)
    property_data["areaClassification"] = classification

    search_area = normalize_search_area(
        property_data.get("searchArea") or property_data.get("area")
    )

    if classification == "primaryTarget":
        property_data["areaStatus"] = "confirmed"
        property_data["area"] = search_area
        property_data["areaCandidate"] = None
    elif classification == "subTarget":
        property_data["areaStatus"] = "candidate"
        property_data["area"] = None
        property_data["areaCandidate"] = search_area
    else:
        property_data["areaStatus"] = "pending"
        property_data["area"] = None
        property_data["areaCandidate"] = None

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

        "priceConfidence":
            property_data.get(
                "priceConfidence"
            ),

        "priceStatus":
            property_data.get(
                "priceStatus"
            ),

        "priceSource":
            property_data.get(
                "priceSource"
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

        "areaStatus":
            property_data.get(
                "areaStatus"
            ),

        "area":
            property_data.get(
                "area"
            ),

        "areaCandidate":
            property_data.get(
                "areaCandidate"
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
                "walkMinutes"
            ),

        "stationWalkMinutes":
            property_data.get(
                "stationWalkMinutes"
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

    detail = get_detail(
        property_data
    )

    raw_price = (
        detail.get("priceYen")
        or detail.get("currentPrice")
        or detail.get("price")
        or property_data.get("priceYen")
        or property_data.get("currentPrice")
        or property_data.get("price")
        or property_data.get("searchPriceYen")
        or property_data.get("searchPrice")
    )

    price_val, validated_price_status = (
        validate_sale_price(
            raw_price
        )
    )

    address = (
        property_data.get("address")
        or detail.get("address")
    )

    land_area = (
        property_data.get("landAreaM2")
        or property_data.get("land")
        or detail.get("landAreaM2")
    )

    building_area = (
        property_data.get("buildingAreaM2")
        or property_data.get("building")
        or detail.get("buildingAreaM2")
    )

    layout = (
        property_data.get("layout")
        or detail.get("layout")
    )

    property_type = (
        property_data.get("propertyType")
        or normalize_property_type(
            detail.get("propertyType")
        )
    )

    target_station = (
        property_data.get("targetStation")
        or detail.get("targetStation")
    )

    walk_minutes = (
        property_data.get(
            "stationWalkMinutes"
        )
        if property_data.get(
            "stationWalkMinutes"
        ) is not None
        else (
            property_data.get("walkMinutes")
            if property_data.get("walkMinutes") is not None
            else get_walk_minutes(property_data)
        )
    )

    history = (
        property_data.get(
            "priceHistory"
        )
        or []
    )

    if not isinstance(
        history,
        list,
    ):
        history = []

    compact_history = []

    for item in history:

        if not isinstance(
            item,
            dict,
        ):
            continue

        price = item.get(
            "price"
        )

        observed_at = item.get(
            "observedAt"
        )

        if price is None:
            continue

        compact_history.append(
            {
                "price": price,
                "observedAt": observed_at,
            }
        )

    record: Dict[str, Any] = {

        "propertyId":
            property_data.get(
                "propertyId"
            ),

        "source":
            property_data.get(
                "source"
            ),

        "sourceId":
            property_data.get(
                "sourceId"
            ),

        "sourceUrl":
            property_data.get(
                "sourceUrl"
            )
            or property_data.get(
                "url"
            ),

        "name":
            property_data.get(
                "name"
            ),

        "listingTitle":
            property_data.get(
                "listingTitle"
            ),

        "priceYen":
            price_val,

        "currentPrice":
            price_val,

        "price":
            price_val,

        "priceMan":
            (
                int(price_val / 10_000)
                if price_val is not None
                else None
            ),

        "address":
            address,

        "landAreaM2":
            to_number(
                land_area
            ),

        "buildingAreaM2":
            to_number(
                building_area
            ),

        "layout":
            layout,

        "constructionYear":
            property_data.get(
                "constructionYear"
            ),

        "builtAgeYears":
            property_data.get(
                "builtAgeYears"
            ),

        "propertyType":
            property_type,

        "targetStation":
            target_station,

        "targetStationWalkMinutes":
            to_number(
                walk_minutes
            ),

        "stationWalkMinutes":
            to_number(
                walk_minutes
            ),

        "areaClassification":
            property_data.get(
                "areaClassification"
            ),

        "areaStatus":
            property_data.get(
                "areaStatus"
            ),

        "area":
            property_data.get(
                "area"
            ),

        "areaCandidate":
            property_data.get(
                "areaCandidate"
            ),

        "searchArea":
            property_data.get(
                "searchArea"
            ),

        "schoolDistrictCandidate":
            property_data.get(
                "schoolDistrictCandidate"
            ),

        "schoolDistrictCandidateArea":
            property_data.get(
                "schoolDistrictCandidateArea"
            ),

        "firstSeenAt":
            property_data.get(
                "firstSeenAt"
            ),

        "lastSeenAt":
            property_data.get(
                "lastSeenAt"
            ),

        "endedObservedAt":
            property_data.get(
                "endedObservedAt"
            ),

        "status":
            property_data.get(
                "status"
            ),

        "daysListed":
            property_data.get(
                "daysListed"
            ),

        "firstPrice":
            property_data.get(
                "firstPrice"
            ),

        "totalPriceReduction":
            property_data.get(
                "totalPriceReduction",
                0,
            ),

        "priceReductionRate":
            property_data.get(
                "priceReductionRate",
                0,
            ),

        "priceReductionCount":
            property_data.get(
                "priceReductionCount",
                0,
            ),

        "priceHistory":
            compact_history,

        "parserVersion":
            MAIN_PARSER_VERSION,
    }

    price_source = (
        detail.get(
            "priceSource"
        )
        or property_data.get(
            "priceSource"
        )
    )

    price_confidence = (
        detail.get(
            "priceConfidence"
        )
        or property_data.get(
            "priceConfidence"
        )
    )

    price_warning = (
        detail.get(
            "priceWarning"
        )
        if detail.get(
            "priceWarning"
        ) is not None
        else property_data.get(
            "priceWarning"
        )
    )

    price_status = (
        detail.get(
            "priceStatus"
        )
        or property_data.get(
            "priceStatus"
        )
        or validated_price_status
    )

    record[
        "priceSource"
    ] = price_source

    record[
        "priceConfidence"
    ] = price_confidence

    record[
        "priceWarning"
    ] = price_warning

    record[
        "priceStatus"
    ] = price_status

    station_access = (
        detail.get(
            "stationAccess"
        )
        or property_data.get(
            "stationAccess"
        )
    )

    if isinstance(
        station_access,
        dict,
    ):

        compact_station_access = {}

        for station, minutes in station_access.items():

            minute_value = to_number(
                minutes
            )

            if minute_value is None:
                continue

            compact_station_access[
                str(station)
            ] = minute_value

        if compact_station_access:

            record[
                "stationAccess"
            ] = compact_station_access

    record[
        "houseDbSchemaVersion"
    ] = HOUSE_DB_SCHEMA_VERSION

    record[
        "candidateCriteria"
    ] = {
        "builtAgeMatched":
            property_data.get(
                "builtAgeMatched"
            ),

        "propertyTypeMatched":
            property_data.get(
                "propertyTypeMatched"
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
    }

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
        candidate.get(
            "searchTarget"
        )
        or candidate.get(
            "searchTargetArea"
        )
    )

    search_area = (
        candidate.get(
            "searchTargetArea"
        )
        or candidate.get(
            "searchArea"
        )
    )

    property_type = (
        candidate.get(
            "searchTargetPropertyType"
        )
        or candidate.get(
            "searchPropertyType"
        )
    )

    existing[
        "searchTarget"
    ] = target

    existing[
        "searchTargetArea"
    ] = search_area

    existing[
        "searchTargetPropertyType"
    ] = property_type

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

    # ========================================================
    # 1. Config
    # ========================================================

    search_config = load_search_config()

    search_urls = load_search_urls()

    if not search_urls:

        print(
            "[ERROR] 検索対象URLがありません。"
            "config/search_urls.json を確認してください。"
        )

        sys.exit(1)

    # ========================================================
    # 2. Existing DB
    # ========================================================

    houses_raw = load_json(
        HOUSES_PATH,
        default={
            "properties": []
        },
    )

    house_properties = (
        houses_raw.get(
            "properties",
            []
        )
        if isinstance(
            houses_raw,
            dict,
        )
        else (
            houses_raw
            if isinstance(
                houses_raw,
                list,
            )
            else []
        )
    )

    discovered_raw = load_json(
        DISCOVERED_PATH,
        default={
            "properties": []
        },
    )

    discovered_properties = (
        discovered_raw.get(
            "properties",
            []
        )
        if isinstance(
            discovered_raw,
            dict,
        )
        else (
            discovered_raw
            if isinstance(
                discovered_raw,
                list,
            )
            else []
        )
    )

    db: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for prop in house_properties:

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

        prop[
            "seenThisRun"
        ] = False

        if pid in db:

            existing = db[pid]

            for key, value in prop.items():

                if (
                    existing.get(key)
                    is None
                    and value is not None
                ):
                    existing[key] = value

        else:

            db[pid] = prop

    for prop in discovered_properties:

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

            prop[
                "seenThisRun"
            ] = False

            db[pid] = prop

    # ========================================================
    # 3. Search Crawling
    # ========================================================

    search_adapter = SuumoSearchAdapter(
        config=search_config
    )

    search_success_count = 0
    search_failed_count = 0

    run_start_iso = now_iso()

    for target in search_urls:

        name = target.get(
            "name",
            "Unknown",
        )

        url = target.get(
            "url"
        )

        if not url:
            continue

        print(
            f"--- Crawling target: {name} ({url}) ---"
        )

        try:

            results = fetch_search_target(
                search_adapter,
                url,
                target,
                search_config,
            )

            search_success_count += 1

            print(
                f"  取得件数: {len(results)}件"
            )

            for item in results:

                normalized = normalize_search_result(
                    item
                )

                normalized = apply_url_area_prefilter(
                    normalized,
                    search_config,
                    search_urls,
                )

                pid = normalized[
                    "propertyId"
                ]

                if pid in db:

                    existing = db[
                        pid
                    ]

                    existing[
                        "seenThisRun"
                    ] = True

                    existing[
                        "lastSeenAt"
                    ] = run_start_iso

                    existing[
                        "status"
                    ] = "active"

                    if (
                        normalized.get(
                            "searchPriceYen"
                        )
                        is not None
                    ):

                        existing[
                            "searchPriceYen"
                        ] = normalized.get(
                            "searchPriceYen"
                        )

                        existing[
                            "searchPriceMan"
                        ] = normalized.get(
                            "searchPriceMan"
                        )

                        if (
                            normalized.get(
                                "searchPriceRaw"
                            )
                            is not None
                        ):
                            existing[
                                "searchPriceRaw"
                            ] = normalized.get(
                                "searchPriceRaw"
                            )

                        if (
                            normalized.get(
                                "searchPriceConfidence"
                            )
                            is not None
                        ):
                            existing[
                                "searchPriceConfidence"
                            ] = normalized.get(
                                "searchPriceConfidence"
                            )

                        if (
                            normalized.get(
                                "searchPriceWarning"
                            )
                            is not None
                        ):
                            existing[
                                "searchPriceWarning"
                            ] = normalized.get(
                                "searchPriceWarning"
                            )

                        if (
                            normalized.get(
                                "searchPriceCandidates"
                            )
                            is not None
                        ):
                            existing[
                                "searchPriceCandidates"
                            ] = normalized.get(
                                "searchPriceCandidates"
                            )

                        if (
                            normalized.get(
                                "searchPriceCandidateCount"
                            )
                            is not None
                        ):
                            existing[
                                "searchPriceCandidateCount"
                            ] = normalized.get(
                                "searchPriceCandidateCount"
                            )

                    if not existing.get(
                        "searchArea"
                    ):

                        existing[
                            "searchArea"
                        ] = normalized.get(
                            "searchArea"
                        )

                    search_targets = append_unique(
                        existing.get(
                            "searchTargets",
                            [],
                        ),
                        name,
                    )

                    existing[
                        "searchTargets"
                    ] = search_targets

                    merge_search_occurrence(
                        existing,
                        normalized,
                    )

                else:

                    normalized[
                        "seenThisRun"
                    ] = True

                    normalized[
                        "firstSeenAt"
                    ] = run_start_iso

                    normalized[
                        "lastSeenAt"
                    ] = run_start_iso

                    normalized[
                        "status"
                    ] = "active"

                    normalized[
                        "searchTargets"
                    ] = [
                        name
                    ]

                    normalized[
                        "detailFetchStatus"
                    ] = "pending"

                    normalized[
                        "detailFetchSuccess"
                    ] = None

                    db[
                        pid
                    ] = normalized

        except Exception as exc:

            search_failed_count += 1

            print(
                f"[ERROR] ターゲットクロール失敗 "
                f"{name}: {exc}"
            )

    search_healthy = (
        search_failed_count == 0
    )

    # ========================================================
    # 4. Lifecycle & Initial Criteria
    # ========================================================

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

    # ========================================================
    # 5. Detail Fetch Processing
    # ========================================================

    to_fetch: List[
        Dict[str, Any]
    ] = []

    for pid, prop in db.items():

        if not is_detail_target_property(
            prop,
            search_config,
            search_urls,
        ):
            continue

        priority = detail_fetch_priority(
            prop
        )

        if priority < 999:

            to_fetch.append(
                {
                    "priority": priority,
                    "property": prop,
                }
            )

    to_fetch.sort(
        key=lambda x: x[
            "priority"
        ]
    )

    limit = search_config.get(
        "detailFetchLimit",
        DEFAULT_DETAIL_FETCH_LIMIT,
    )

    try:
        limit = int(limit)
    except (
        TypeError,
        ValueError,
    ):
        limit = DEFAULT_DETAIL_FETCH_LIMIT

    target_items = [
        x["property"]
        for x in to_fetch[:limit]
    ]

    print(
        f"--- Detail Fetch Target Count: "
        f"{len(target_items)} / Queue: "
        f"{len(to_fetch)} ---"
    )

    detail_adapter = SuumoDetailAdapter(
        config=search_config
    )

    consecutive_connectivity_errors = 0

    for idx, prop in enumerate(
        target_items,
        start=1,
    ):

        url = (
            prop.get("sourceUrl")
            or prop.get("url")
        )

        if not url:
            continue

        print(
            f"[{idx}/{len(target_items)}] "
            f"Detail fetching: "
            f"{prop.get('propertyId')} "
            f"({url})"
        )

        try:

            detail_res = (
                detail_adapter.fetch_detail(
                    url
                )
            )

            enrich_with_detail(
                prop,
                detail_res,
            )

            evaluate_property_criteria(
                prop,
                search_config,
            )

            if detail_res.get(
                "success"
            ):

                consecutive_connectivity_errors = 0

            else:

                err_type = (
                    detail_res.get(
                        "errorType"
                    )
                )

                if err_type in (
                    "timeout",
                    "network_error",
                    "server_error",
                ):

                    consecutive_connectivity_errors += 1

                else:

                    consecutive_connectivity_errors = 0

        except Exception as exc:

            print(
                f"[ERROR] Detail fetch error "
                f"({url}): {exc}"
            )

            consecutive_connectivity_errors += 1

        if (
            consecutive_connectivity_errors
            >= MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS
        ):

            print(
                f"[WARN] 連続接続エラー("
                f"{consecutive_connectivity_errors}回)"
                "のため詳細フェッチを中断します。"
            )

            break

        time.sleep(
            1.0
        )

    # ========================================================
    # 6. Re-evaluate all properties after detail processing
    # ========================================================

    for pid, prop in db.items():

        evaluate_property_criteria(
            prop,
            search_config,
        )

    # ========================================================
    # 7. Save Data
    # ========================================================

    discovered_list = (
        compact_discovery_db(
            list(db.values())
        )
    )

    market_houses = []

    observations = []

    storage_stats = Counter()

    for pid, prop in db.items():

        obs = build_listing_observation(
            prop
        )

        observations.append(
            obs
        )

        if is_market_history_property(
            prop
        ):

            record = build_market_house_record(
                prop
            )

            market_houses.append(
                record
            )

            storage_stats[
                "stored"
            ] += 1

        else:

            storage_stats[
                "not_stored"
            ] += 1

            if not should_store_house(
                prop
            ):

                storage_stats[
                    "criteria_not_matched"
                ] += 1

            elif prop.get(
                "areaClassification"
            ) not in HOUSE_DB_AREA_CLASSIFICATIONS:

                storage_stats[
                    "area_classification_invalid"
                ] += 1

    market_houses.sort(
        key=lambda x:
            x.get(
                "lastSeenAt"
            )
            or "",
        reverse=True,
    )

    # ========================================================
    # 8. Save Discovery DB
    # ========================================================

    save_json(
        DISCOVERED_PATH,
        {
            "version":
                DISCOVERY_SCHEMA_VERSION,

            "updatedAt":
                run_start_iso,

            "properties":
                discovered_list,
        },
    )

    # ========================================================
    # 9. Save Market House DB
    # ========================================================

    save_json(
        HOUSES_PATH,
        {
            "version":
                HOUSE_DB_SCHEMA_VERSION,

            "updatedAt":
                run_start_iso,

            "properties":
                market_houses,
        },
    )

    # ========================================================
    # 10. Save Observation DB
    # ========================================================

    save_json(
        OBSERVATIONS_PATH,
        {
            "version":
                "1.0",

            "updatedAt":
                run_start_iso,

            "observations":
                observations,
        },
    )

    # ========================================================
    # 11. Summary
    # ========================================================

    active_count = sum(
        1
        for p in market_houses
        if p.get(
            "status"
        ) == "active"
    )

    ended_count = sum(
        1
        for p in market_houses
        if p.get(
            "status"
        ) == "observed_ended"
    )

    primary_count = sum(
        1
        for p in market_houses
        if p.get(
            "areaClassification"
        ) == "primaryTarget"
    )

    sub_count = sum(
        1
        for p in market_houses
        if p.get(
            "areaClassification"
        ) == "subTarget"
    )

    print(
        "============================================================"
    )

    print(
        "=== Storage Summary ==="
    )

    print(
        f"  Discovery DB: "
        f"{len(discovered_list)}"
    )

    print(
        f"  Market House DB: "
        f"{len(market_houses)}"
    )

    print(
        f"  Active: "
        f"{active_count}"
    )

    print(
        f"  Observed ended: "
        f"{ended_count}"
    )

    print(
        f"  primaryTarget: "
        f"{primary_count}"
    )

    print(
        f"  subTarget: "
        f"{sub_count}"
    )

    print(
        f"  Not stored: "
        f"{storage_stats['not_stored']}"
    )

    print(
        "============================================================"
    )

    print(
        f"=== Pipeline Completed. "
        f"Active Market Houses: "
        f"{active_count} ==="
    )


if __name__ == "__main__":
    run_pipeline()
