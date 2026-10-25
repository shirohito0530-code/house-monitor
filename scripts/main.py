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
    from storage import append_changed_observations
except ImportError:
    from scripts.storage import append_changed_observations

try:
    from analytics import summarize_listing_lifecycle
except ImportError:
    from scripts.analytics import summarize_listing_lifecycle

try:
    from normalize import normalize_listing_status
except ImportError:
    from scripts.normalize import normalize_listing_status

try:
    from identity import (
        infer_source,
        get_source_id,
        make_property_id,
        make_identity_key,
        make_strong_physical_identity_key,
    )
except ImportError:
    from scripts.identity import (
        infer_source,
        get_source_id,
        make_property_id,
        make_identity_key,
        make_strong_physical_identity_key,
    )


# ============================================================
# Constants
# ============================================================

MAIN_PARSER_VERSION = "2026-10-25-v45-duplicate-detail-enrichment"


# ============================================================
# Discovery DB retention / schema
# ============================================================

DISCOVERY_SCHEMA_VERSION = "3.0"
DISCOVERY_RETENTION_DAYS = 180
OBSERVATION_RETENTION_DAYS = 365
OBSERVATION_MAX_RECORDS = 20000
HEALTHY_MISSES_TO_OBSERVED_ENDED = 2

DISCOVERY_FIELDS = (
    # --------------------------------------------------------
    # Identity
    # --------------------------------------------------------
    "propertyId",
    "source",
    "sourceId",
    "sourceUrl",
    "physicalIdentityKey",
    "physicalIdentityType",
    "physicalIdentityCompleteness",

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
    # Phase 4 derived area decisions must survive main.py's discovery projection so
    # quarantine timestamps remain stable across daily runs.
    "areaDecision",
    "areaDecisionReason",
    "areaQuarantinedAt",
    "areaPolicyVersion",
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
    "priceHistory",
    "priceChange",
    "priceChangePercent",
    "priceConfidence",
    "priceStatus",

    # --------------------------------------------------------
    # Detail
    # --------------------------------------------------------
    "detailFetched",
    "detailQuality",
    "detailQualityScore",
    "detailQualityReasons",
    "coreComplete",
    "status",
    "firstSeenAt",
    "lastSeenAt",
    "endedAt",
    "endedObservedAt",
    "isListingEnded",
    "listingEndReason",
    "listingEndEvidence",
    "listingStatusUpdatedAt",
    "missedSearchRuns",
    "relistedAt",
    "lastDetailFetchAt",
    "lastDetailAttemptAt",
    "detailFetchStatus",
    "detailFetchErrorType",
    "detailFetchAttempts",
    "detailFetchSuccess",
    "detailFetchLastError",
    "detailFetchHttpStatus",
    "detailFetchErrorAt",
    "duplicateCandidate",
    "duplicateCandidateOf",
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
MAX_DUPLICATE_ENRICHMENT_ATTEMPTS = 5
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
    "possibly_ended",
    "observed_ended",
    "confirmed_ended",
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
# Identity & Physical Identity Deduplication
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

    raw_status = property_data.get("status")
    normalized_status = normalize_listing_status(raw_status)
    if normalized_status != "unknown":
        property_data["status"] = normalized_status
    elif raw_status not in (None, "", "unknown"):
        property_data["status"] = "active"
    if (
        property_data.get("status") == "confirmed_ended"
        and property_data.get("isListingEnded") is not True
    ):
        # 古いstatus表記だけでは「明示確認」と断定しない。
        property_data["status"] = "observed_ended"

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

            # canonicalIdentityKeyは掲載IDをまたぐ実物件候補キー。
            # 住所・土地・建物等の強い根拠がある場合のみ物理キーとして採用する。
            if identity.get("canonicalIdentityKey") and float(
                identity.get("canonicalIdentityCompleteness") or 0
            ) >= 0.6:
                property_data["physicalIdentityKey"] = identity.get("canonicalIdentityKey")
                property_data["physicalIdentityType"] = identity.get("canonicalIdentityType")
                property_data["physicalIdentityCompleteness"] = identity.get("canonicalIdentityCompleteness")

            if identity.get(
                "physicalIdentityKey"
            ):
                property_data[
                    "physicalIdentityKey"
                ] = identity.get(
                    "physicalIdentityKey"
                )

            if identity.get(
                "physicalIdentityType"
            ):
                property_data[
                    "physicalIdentityType"
                ] = identity.get(
                    "physicalIdentityType"
                )

            if identity.get(
                "physicalIdentityCompleteness"
            ) is not None:
                property_data[
                    "physicalIdentityCompleteness"
                ] = identity.get(
                    "physicalIdentityCompleteness"
                )

    except Exception:
        pass

    apply_detail_quality(property_data)
    return property_id


def deduplicate_by_physical_identity(
    properties: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    groups: Dict[str, List[Dict[str, Any]]] = {}
    standalone: List[Dict[str, Any]] = []

    for item in properties:
        physical_key = item.get(
            "physicalIdentityKey"
        )
        if physical_key:
            groups.setdefault(
                physical_key,
                [],
            ).append(item)
        else:
            standalone.append(item)

    result = []
    for physical_key, group in groups.items():
        # より情報量の多い掲載を代表物件にする
        representative = max(
            group,
            key=lambda x: (
                x.get(
                    "detailQualityScore",
                    0,
                ),
                x.get(
                    "identityCompleteness",
                    0,
                ),
                x.get(
                    "lastSeenAt",
                    "",
                ),
            ),
        )
        representative = deepcopy(
            representative
        )
        # 複数掲載情報を保持
        representative[
            "duplicateListings"
        ] = [
            {
                "propertyId": item.get(
                    "propertyId"
                ),
                "source": item.get(
                    "source"
                ),
                "sourceId": item.get(
                    "sourceId"
                ),
                "sourceUrl": item.get(
                    "sourceUrl"
                ),
                "priceYen": item.get(
                    "priceYen"
                ),
            }
            for item in group
        ]
        representative[
            "duplicateListingCount"
        ] = len(group)
        representative[
            "physicalIdentityKey"
        ] = physical_key
        result.append(
            representative
        )

    result.extend(
        standalone
    )
    return result


# ============================================================
# Detail Quality
# ============================================================

DETAIL_QUALITY_COMPLETE = "complete"
DETAIL_QUALITY_PARTIAL = "partial"
DETAIL_QUALITY_UNKNOWN = "unknown"


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def calculate_detail_quality(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    詳細ページの主要7項目を基準に品質を判定する。

    対象:
      - 土地面積
      - 建物面積
      - 築年
      - 対象駅
      - 駅徒歩
      - 価格
      - 物件種別
    """
    if not isinstance(property_data, dict):
        return {
            "detailQuality": DETAIL_QUALITY_UNKNOWN,
            "detailQualityScore": 0,
            "detailQualityReasons": [
                "property data is not a dictionary"
            ],
        }

    if property_data.get("detailFetched") is False:
        return {
            "detailQuality": DETAIL_QUALITY_UNKNOWN,
            "detailQualityScore": 0,
            "detailQualityReasons": [
                "detail page has not been fetched"
            ],
        }

    detail = property_data.get("detail")
    if not isinstance(detail, dict):
        detail = {}

    def value(*keys: str) -> Any:
        for key in keys:
            if _has_value(property_data.get(key)):
                return property_data.get(key)
            if _has_value(detail.get(key)):
                return detail.get(key)
        return None

    fields = {
        "landArea": value("landAreaM2", "land"),
        "buildingArea": value("buildingAreaM2", "building"),
        "builtYear": value(
            "constructionYear",
            "constructionYearMonth",
            "constructionMonth",
            "constructionAgeYears",
            "builtAgeYears",
        ),
        "station": value("targetStation", "station"),
        "walkMinutes": value(
            "targetStationWalkMinutes",
            "stationWalkMinutes",
            "walkMinutes",
        ),
        "price": value("priceYen", "currentPrice", "price"),
        "propertyType": value("propertyType", "searchPropertyType"),
    }

    missing_fields = [
        key for key, item in fields.items()
        if not _has_value(item)
    ]
    available = len(fields) - len(missing_fields)
    score = round(available / len(fields) * 100)

    if available == len(fields):
        return {
            "detailQuality": DETAIL_QUALITY_COMPLETE,
            "detailQualityScore": 100,
            "detailQualityReasons": [],
        }

    if available > 0:
        return {
            "detailQuality": DETAIL_QUALITY_PARTIAL,
            "detailQualityScore": score,
            "detailQualityReasons": [
                f"missing: {field}"
                for field in missing_fields
            ],
        }

    return {
        "detailQuality": DETAIL_QUALITY_UNKNOWN,
        "detailQualityScore": 0,
        "detailQualityReasons": [
            "no usable detail fields"
        ],
    }


def apply_detail_quality(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:
    quality = calculate_detail_quality(
        property_data
    )
    property_data["detailQuality"] = (
        quality["detailQuality"]
    )
    property_data["detailQualityScore"] = (
        quality["detailQualityScore"]
    )
    property_data["detailQualityReasons"] = (
        quality["detailQualityReasons"]
    )
    return property_data


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
            if field == "priceHistory" and isinstance(value, list):
                # 初回価格を残しつつ直近履歴だけを保持し、1レコードの肥大化を防ぐ。
                record[field] = value if len(value) <= 30 else [value[0]] + value[-29:]
            else:
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


def _merge_property_records(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    """同一propertyIdの情報を欠損補完しつつ、最新値を優先して統合する。"""
    merged = deepcopy(existing)
    incoming_detail_score = int(incoming.get("detailQualityScore") or 0)
    existing_detail_score = int(merged.get("detailQualityScore") or 0)
    incoming_seen = str(incoming.get("lastSeenAt") or incoming.get("collectedAt") or "")
    existing_seen = str(merged.get("lastSeenAt") or merged.get("collectedAt") or "")
    incoming_is_newer = incoming_seen >= existing_seen
    incoming_detail_is_better = incoming_detail_score > existing_detail_score

    always_latest = {
        "lastSeenAt", "status", "priceYen", "priceMan", "currentPrice",
        "currentPriceMan", "searchPriceYen", "searchPriceMan",
        "listingTitle", "searchTitle", "searchArea", "sourceUrl",
        "lastDetailFetchAt", "detailFetchStatus", "detailFetchSuccess",
        "listingStatusUpdatedAt", "missedSearchRuns", "endedObservedAt",
        "endedAt", "relistedAt", "isListingEnded", "listingEndReason",
        "listingEndEvidence", "detailFetchLastError", "detailFetchHttpStatus",
        "detailFetchErrorAt", "lastDetailAttemptAt",
    }
    detail_fields = {
        "address", "detectedArea", "areaDetected", "targetStation",
        "targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes",
        "landArea", "landAreaM2", "buildingArea", "buildingAreaM2",
        "yearBuilt", "builtYear", "propertyType", "layout", "detail",
    }

    for key, value in incoming.items():
        if value is None or value == "":
            continue
        if key in {"firstSeenAt", "discoveredAt"}:
            if not merged.get(key):
                merged[key] = value
        elif key in always_latest:
            if key in {"status", "missedSearchRuns", "isListingEnded", "endedObservedAt", "endedAt", "relistedAt", "listingEndReason", "listingEndEvidence"}:
                incoming_status_at = str(
                    incoming.get("listingStatusUpdatedAt")
                    or incoming.get("lastSeenAt")
                    or ""
                )
                existing_status_at = str(
                    merged.get("listingStatusUpdatedAt")
                    or merged.get("lastSeenAt")
                    or ""
                )
                if incoming_status_at >= existing_status_at:
                    merged[key] = value
            elif incoming_is_newer:
                merged[key] = value
        elif key in detail_fields:
            if merged.get(key) in (None, "", [], {}) or incoming_detail_is_better:
                merged[key] = deepcopy(value)
        elif merged.get(key) in (None, "", [], {}):
            merged[key] = deepcopy(value)

    # 掲載IDは維持。明示的な再掲載ならactiveへ戻す。
    if merged.get("seenThisRun"):
        merged["status"] = "active"
    return merged


def compact_discovery_db(
    properties: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """保持期間を適用し、同じcanonical propertyIdは1件へ統合する。"""
    now = datetime.now(timezone.utc)
    compacted: Dict[str, Dict[str, Any]] = {}

    for prop in properties:
        if not isinstance(prop, dict) or not should_keep_discovery(prop, now):
            continue

        record = build_discovery_record(prop)
        property_id = record.get("propertyId")
        if not property_id:
            continue

        if property_id in compacted:
            compacted[property_id] = _merge_property_records(
                compacted[property_id], record
            )
        else:
            compacted[property_id] = record

    return sorted(
        compacted.values(),
        key=lambda item: (
            str(item.get("status") or ""),
            str(item.get("lastSeenAt") or ""),
            str(item.get("propertyId") or ""),
        ),
        reverse=True,
    )



def mark_duplicate_candidates(
    properties: List[Dict[str, Any]],
) -> int:
    """強い実物件キーが一致する別掲載IDを重複候補として記録する。自動統合はしない。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        key = make_strong_physical_identity_key(prop)
        if not key:
            prop["duplicateCandidate"] = False
            prop["duplicateCandidateOf"] = None
            continue
        groups.setdefault(key, []).append(prop)

    marked = 0
    for group in groups.values():
        ids = {str(item.get("propertyId") or "") for item in group}
        if len(ids) <= 1:
            for item in group:
                item["duplicateCandidate"] = False
                item["duplicateCandidateOf"] = None
            continue

        representative = max(
            group,
            key=lambda item: (
                int(item.get("detailQualityScore") or 0),
                float(item.get("physicalIdentityCompleteness") or 0),
                str(item.get("lastSeenAt") or ""),
            ),
        )
        representative_id = str(representative.get("propertyId") or "")
        for item in group:
            item["duplicateCandidate"] = str(item.get("propertyId") or "") != representative_id
            item["duplicateCandidateOf"] = representative_id if item["duplicateCandidate"] else None
            if item["duplicateCandidate"]:
                marked += 1
    return marked


# ============================================================
# Market History DB Selection
# ============================================================

CRITERIA_FIELDS = (
    "builtAgeMatched",
    "propertyTypeMatched",
    "priceMatched",
    "walkMatched",
    "landAreaMatched",
    "buildingAreaMatched",
)


def get_market_criteria_review(property_data: Dict[str, Any]) -> Dict[str, Any]:
    """未知項目を不合格と誤認せず、適合・判定待ち・除外を分ける。"""
    excluded = [field for field in CRITERIA_FIELDS if property_data.get(field) is False]
    pending = [field for field in CRITERIA_FIELDS if property_data.get(field) is None]
    if excluded:
        status = "excluded"
    elif pending:
        status = "pending"
    else:
        status = "matched"
    return {
        "marketReviewStatus": status,
        "criteriaExcludedFields": excluded,
        "criteriaPendingFields": pending,
    }


def should_store_house(
    property_data: Dict[str, Any],
) -> bool:
    """明示的に条件外と判定された物件だけを除外。情報不足は判定待ちで保持。"""
    return not any(property_data.get(field) is False for field in CRITERIA_FIELDS)


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

    area_classification = property_data.get("areaClassification")

    # 住所・徒歩が未取得で分類保留の候補は保持し、UI上で判定待ちと表示する。
    # 明示的に対象外と分類された物件は市場DBに入れない。
    if area_classification not in HOUSE_DB_AREA_CLASSIFICATIONS | {"detailPending"}:
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


def _detail_missing_identity_fields(property_data: Dict[str, Any]) -> List[str]:
    """Fields needed for physical-duplicate decisions; success alone is not completeness."""
    detail = get_detail(property_data)
    aliases = {
        "address": ("address", "fullAddress", "propertyAddress"),
        "landAreaM2": ("landAreaM2", "land", "landArea"),
        "buildingAreaM2": ("buildingAreaM2", "building", "buildingArea"),
        "constructionYear": ("constructionYear", "builtYear", "year"),
        "layout": ("layout", "layoutRaw", "floorPlan"),
        "propertyType": ("propertyType", "type", "listingType"),
    }
    missing = []
    for target, keys in aliases.items():
        value = next((property_data.get(k) for k in keys if property_data.get(k) not in (None, "", "-")), None)
        if value is None:
            value = next((detail.get(k) for k in keys if detail.get(k) not in (None, "", "-")), None)
        if value is None:
            missing.append(target)
    return missing


def _duplicate_enrichment_ids(properties: List[Dict[str, Any]]) -> Set[str]:
    """Prioritize incomplete listings in small, plausible address blocks for detail fetch.

    Town-only matches are not considered proof of duplication. This is only a fetch-priority
    heuristic; final auto-merge requires stronger evidence in market_pipeline.py.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        address = str(prop.get("address") or prop.get("fullAddress") or prop.get("propertyAddress") or "")
        if prop.get("areaDecision") == "rejected" or not any(city in address for city in ("柏市", "流山市")):
            continue
        address = address.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        address = re.sub(r"[\s　、,，。．.]", "", address).replace("丁目", "-").replace("番地", "-").replace("番", "-").replace("号", "")
        address = re.sub(r"[-ー―‐−]+", "-", address)
        if not address or len(address) < 8:
            continue
        # Strip house numbers only to create a coarse locality block. Never use this
        # block to merge records; only to decide which incomplete pages to fetch first.
        block = re.sub(r"[0-9-]", "", address)
        if len(block) >= 6:
            buckets.setdefault(block, []).append(prop)
    urgent: Set[str] = set()
    for group in buckets.values():
        if not (2 <= len(group) <= 12):
            continue  # avoid fetching an entire subdivision/town on a noisy block
        if not any(re.search(r"\d", str(x.get("address") or x.get("fullAddress") or x.get("propertyAddress") or "")) for x in group):
            continue
        for prop in group:
            missing = _detail_missing_identity_fields(prop)
            attempts = int(prop.get("detailFetchAttempts") or 0)
            if missing and attempts < MAX_DUPLICATE_ENRICHMENT_ATTEMPTS and (prop.get("sourceUrl") or prop.get("url")):
                urgent.add(str(prop.get("propertyId") or ""))
    urgent.discard("")
    return urgent


def detail_fetch_priority(
    property_data: Dict[str, Any],
    force_duplicate_enrichment: bool = False,
) -> int:

    attempts = int(
        property_data.get(
            "detailFetchAttempts",
            0,
        )
        or 0
    )

    if force_duplicate_enrichment and _detail_missing_identity_fields(property_data):
        if attempts < MAX_DUPLICATE_ENRICHMENT_ATTEMPTS:
            return 0

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

    if identity.get("physicalIdentityKey"):
        result["physicalIdentityKey"] = identity.get("physicalIdentityKey")
    if identity.get("physicalIdentityType"):
        result["physicalIdentityType"] = identity.get("physicalIdentityType")
    if identity.get("physicalIdentityCompleteness") is not None:
        result["physicalIdentityCompleteness"] = identity.get("physicalIdentityCompleteness")

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


def promote_detail_fields(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:
    """詳細結果をトップレベルの正規フィールドへ反映する。"""
    detail = property_data.get("detail")
    if not isinstance(detail, dict):
        return property_data

    field_aliases = {
        "address": ("address",),
        "landAreaM2": ("landAreaM2", "land"),
        "buildingAreaM2": ("buildingAreaM2", "building"),
        "buildingFootprintAreaM2": ("buildingFootprintAreaM2",),
        "layout": ("layout", "layoutRaw", "floorPlan", "floor_plan"),
        "layoutRaw": ("layoutRaw", "layout", "floorPlan", "floor_plan"),
        "constructionYear": ("constructionYear",),
        "constructionMonth": ("constructionMonth",),
        "constructionYearMonth": ("constructionYearMonth",),
        "constructionAgeYears": ("constructionAgeYears",),
        "targetStation": ("targetStation", "station"),
        "targetStationWalkMinutes": (
            "targetStationWalkMinutes",
            "stationWalkMinutes",
            "walkMinutes",
        ),
        "targetStationWalkAvailable": (
            "targetStationWalkAvailable",
        ),
        "stationWalkMinutes": (
            "stationWalkMinutes",
            "targetStationWalkMinutes",
            "walkMinutes",
        ),
        "walkMinutes": (
            "walkMinutes",
            "targetStationWalkMinutes",
            "stationWalkMinutes",
        ),
        "propertyType": ("propertyType",),
    }

    for target, aliases in field_aliases.items():
        if _has_value(property_data.get(target)):
            continue
        for source in aliases:
            if _has_value(detail.get(source)):
                property_data[target] = detail.get(source)
                break

    # 数値項目は既存の正規化関数を通す。
    for key in ("landAreaM2", "buildingAreaM2", "buildingFootprintAreaM2",
                "constructionAgeYears", "targetStationWalkMinutes",
                "stationWalkMinutes", "walkMinutes"):
        if property_data.get(key) is not None:
            numeric = to_number(property_data.get(key))
            if numeric is not None:
                property_data[key] = numeric

    if property_data.get("landAreaM2") is not None:
        property_data["land"] = property_data["landAreaM2"]
    if property_data.get("buildingAreaM2") is not None:
        property_data["building"] = property_data["buildingAreaM2"]

    return property_data


def enrich_with_detail(
    property_data: Dict[str, Any],
    detail_res: Dict[str, Any],
) -> Dict[str, Any]:

    attempt_at = now_iso()

    property_data[
        "lastDetailAttemptAt"
    ] = attempt_at

    # detailFetchAttempts is incremented exactly once in the fetch loop.
    # Do not increment it here: this function handles enrichment, not a new request.

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
            "detailFetched"
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
            "detailFetched"
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
        "detailFetched"
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
        "coreComplete"
    ] = all(
        value is True
        for value in criteria_flags
    )

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
    """検索の健全性を確認したうえで、掲載状態を保守的に更新する。"""
    if not property_data.get("firstSeenAt"):
        property_data["firstSeenAt"] = property_data.get("discoveredAt") or now_timestamp

    previous_status = property_data.get("status") or "active"

    if property_data.get("seenThisRun", False):
        was_ended = previous_status in {
            "possibly_ended", "observed_ended", "confirmed_ended"
        } or int(property_data.get("missedSearchRuns") or 0) > 0

        property_data["lastSeenAt"] = now_timestamp
        property_data["status"] = "active"
        property_data["missedSearchRuns"] = 0
        # endedObservedAtは過去の終了イベントとして保持し、再掲載で消さない。
        property_data["isListingEnded"] = False
        property_data["listingEndReason"] = None
        property_data["listingEndEvidence"] = None
        if was_ended:
            property_data["relistedAt"] = now_timestamp
            property_data["listingStatusUpdatedAt"] = now_timestamp
        elif not property_data.get("listingStatusUpdatedAt"):
            property_data["listingStatusUpdatedAt"] = now_timestamp

    elif search_healthy:
        misses = int(property_data.get("missedSearchRuns") or 0) + 1
        property_data["missedSearchRuns"] = misses
        if misses >= HEALTHY_MISSES_TO_OBSERVED_ENDED:
            if previous_status not in {"observed_ended", "confirmed_ended"}:
                property_data["endedObservedAt"] = now_timestamp
                property_data["listingStatusUpdatedAt"] = now_timestamp
            property_data["status"] = (
                "confirmed_ended"
                if property_data.get("isListingEnded") is True
                else "observed_ended"
            )
            property_data["listingEndReason"] = (
                property_data.get("listingEndReason")
                or "missing_from_healthy_searches"
            )
        else:
            property_data["status"] = "possibly_ended"
            property_data["listingStatusUpdatedAt"] = now_timestamp
    else:
        # 一部でも検索が不完全なら、未検出を掲載終了の根拠にしない。
        print(
            f"[INFO] 検索不完全のため掲載状態を維持: "
            f"{property_data.get('propertyId')}"
        )

    property_data["daysListed"] = calculate_days_between(
        property_data.get("firstSeenAt"),
        property_data.get("endedObservedAt")
        if property_data.get("status") in {"possibly_ended", "observed_ended", "confirmed_ended"}
        else None,
    )
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

        "detailFetched":
            property_data.get(
                "detailFetched",
                has_successful_detail(property_data)
            ),

        "detailQuality":
            property_data.get(
                "detailQuality"
            )
            or detail.get(
                "detailQuality"
            ),

        "detailQualityScore":
            property_data.get(
                "detailQualityScore"
            ),

        "detailQualityReasons":
            property_data.get(
                "detailQualityReasons"
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

        "physicalIdentityKey":
            property_data.get(
                "physicalIdentityKey"
            ),

        "physicalIdentityType":
            property_data.get(
                "physicalIdentityType"
            ),

        "physicalIdentityCompleteness":
            property_data.get(
                "physicalIdentityCompleteness"
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

        "land":
            to_number(
                land_area
            ),

        "buildingAreaM2":
            to_number(
                building_area
            ),

        "building":
            to_number(
                building_area
            ),

        "buildingFootprintAreaM2":
            to_number(
                property_data.get("buildingFootprintAreaM2")
                if property_data.get("buildingFootprintAreaM2") is not None
                else detail.get("buildingFootprintAreaM2")
            ),

        "layout":
            layout,

        "layoutRaw":
            layout or detail.get("layoutRaw"),

        "constructionMonth":
            property_data.get("constructionMonth")
            or detail.get("constructionMonth"),

        "constructionAgeYears":
            property_data.get("constructionAgeYears")
            if property_data.get("constructionAgeYears") is not None
            else detail.get("constructionAgeYears"),

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

        "endedAt":
            property_data.get(
                "endedAt"
            ),

        "isListingEnded":
            property_data.get(
                "isListingEnded"
            ),

        "listingEndReason":
            property_data.get(
                "listingEndReason"
            ),

        "listingStatusUpdatedAt":
            property_data.get(
                "listingStatusUpdatedAt"
            ),

        "missedSearchRuns":
            property_data.get(
                "missedSearchRuns"
            ),

        "relistedAt":
            property_data.get(
                "relistedAt"
            ),

        "duplicateCandidate":
            property_data.get(
                "duplicateCandidate"
            ),

        "duplicateCandidateOf":
            property_data.get(
                "duplicateCandidateOf"
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

        "detailFetched":
            property_data.get(
                "detailFetched",
                has_successful_detail(property_data)
            ),

        "detailQuality":
            property_data.get(
                "detailQuality"
            ),

        "detailQualityScore":
            property_data.get(
                "detailQualityScore"
            ),

        "detailQualityReasons":
            property_data.get(
                "detailQualityReasons"
            ),

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

    criteria_review = get_market_criteria_review(property_data)
    record.update(criteria_review)

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

    detail_success = has_successful_detail(
        property_data
    )
    if (
        property_data.get("detailFetched") is True
        or detail_success
    ):
        record["detailFetched"] = True
    elif property_data.get("detailFetched") is False:
        record["detailFetched"] = False
    else:
        record["detailFetched"] = False

    record["detailFetchStatus"] = (
        property_data.get("detailFetchStatus")
        or detail.get("detailFetchStatus")
    )
    record["lastDetailFetchAt"] = (
        property_data.get("lastDetailFetchAt")
        or detail.get("lastDetailFetchAt")
    )
    record["detailFetchAttempts"] = (
        property_data.get("detailFetchAttempts")
        or detail.get("detailFetchAttempts")
    )
    record["detailFetchSuccess"] = (
        property_data.get("detailFetchSuccess")
        if property_data.get("detailFetchSuccess") is not None
        else (
            detail.get("detailFetchSuccess")
            if detail.get("detailFetchSuccess") is not None
            else detail_success
        )
    )

    apply_detail_quality(record)

    record["detailQuality"] = (
        record.get("detailQuality")
        or DETAIL_QUALITY_UNKNOWN
    )
    record["detailQualityScore"] = (
        record.get("detailQualityScore")
        if record.get("detailQualityScore") is not None
        else 0
    )
    record["detailQualityReasons"] = (
        record.get("detailQualityReasons")
        if isinstance(
            record.get("detailQualityReasons"),
            list
        )
        else []
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
    lifecycle_config = search_config.get("listingLifecycle", {})
    global DISCOVERY_RETENTION_DAYS, OBSERVATION_RETENTION_DAYS
    global OBSERVATION_MAX_RECORDS, HEALTHY_MISSES_TO_OBSERVED_ENDED
    DISCOVERY_RETENTION_DAYS = int(
        lifecycle_config.get("discoveryRetentionDays", DISCOVERY_RETENTION_DAYS)
    )
    OBSERVATION_RETENTION_DAYS = int(
        lifecycle_config.get("observationRetentionDays", OBSERVATION_RETENTION_DAYS)
    )
    OBSERVATION_MAX_RECORDS = int(
        lifecycle_config.get("observationMaxRecords", OBSERVATION_MAX_RECORDS)
    )
    HEALTHY_MISSES_TO_OBSERVED_ENDED = int(
        lifecycle_config.get("healthyMissesToObservedEnded", HEALTHY_MISSES_TO_OBSERVED_ENDED)
    )

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

        prop["seenThisRun"] = False
        if pid in db:
            db[pid] = _merge_property_records(db[pid], prop)
            db[pid]["seenThisRun"] = False
        else:
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
            search_failed_count += 1
            print(f"[WARN] 検索URLが空のため失敗扱い: {name}")
            continue

        print(
            f"--- Crawling target: {name} ({url}) ---"
        )

        try:

            previous_target_result_count = len(
                getattr(search_adapter, "search_target_results", [])
            )
            results = fetch_search_target(
                search_adapter,
                url,
                target,
                search_config,
            )
            target_results = getattr(search_adapter, "search_target_results", [])
            latest_target_result = (
                target_results[-1]
                if len(target_results) > previous_target_result_count
                and target_results
                else {}
            )
            target_healthy = (
                getattr(search_adapter, "last_search_healthy", True) is True
                and latest_target_result.get("success", False) is True
                and int(latest_target_result.get("pagesFetched") or 0) > 0
                and len(results) > 0
            )
            if target_healthy:
                search_success_count += 1
            else:
                search_failed_count += 1
                print(
                    f"[WARN] 検索結果が不完全なため掲載終了判定に使用しません: "
                    f"{name}; 件数={len(results)}; "
                    f"error={latest_target_result.get('error')}"
                )

            print(
                f"  取得件数: {len(results)}件 / "
                f"検索健全性={'OK' if target_healthy else 'NG'}"
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

                    # status更新は後段のupdate_property_lifecycleに任せる。
                    # 先にactiveへ上書きすると、終了物件の再掲載を検知できない。

                    if (
                        normalized.get(
                            "searchPriceYen"
                        )
                        is not None
                    ):

                        existing[
                            "searchPriceYen"
                        ] = normalized.get("searchPriceYen")

                        existing[
                            "searchPriceMan"
                        ] = normalized.get("searchPriceMan")

                    for k, v in normalized.items():
                        if v is None:
                            continue
                        # 検索結果由来の可変情報は毎回更新。詳細ページ由来の確度が高い値は後段で維持。
                        if k in {
                            "lastSeenAt", "status", "searchPriceYen", "searchPriceMan",
                            "priceYen", "priceMan", "currentPrice", "currentPriceMan",
                            "searchTitle", "listingTitle", "searchArea", "sourceUrl",
                            "searchPropertyType", "targetStation", "targetStationWalkMinutes",
                        } or existing.get(k) in (None, "", [], {}):
                            existing[k] = v

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

                    db[pid] = normalized

        except Exception as exc:

            search_failed_count += 1

            print(
                f"[ERROR] ターゲットクロール失敗: {name}: {exc}"
            )

    search_healthy = search_failed_count == 0

    # ========================================================
    # 4. Criteria & Lifecycle Evaluation
    # ========================================================

    for pid, prop in list(db.items()):
        canonicalize_property_identity(prop)
        evaluate_property_criteria(prop, search_config)
        update_property_lifecycle(prop, run_start_iso, search_healthy=search_healthy)

    # ========================================================
    # 5. Detail Fetching
    # ========================================================

    detail_adapter = SuumoDetailAdapter(config=search_config)
    duplicate_enrichment_ids = _duplicate_enrichment_ids(list(db.values()))
    detail_candidates = [
        prop for prop in db.values()
        if is_detail_target_property(prop, search_config, search_urls)
        and detail_fetch_priority(
            prop,
            str(prop.get("propertyId") or "") in duplicate_enrichment_ids,
        ) < 999
    ]
    detail_candidates.sort(key=lambda prop: (
        0 if str(prop.get("propertyId") or "") in duplicate_enrichment_ids else 1,
        detail_fetch_priority(prop, str(prop.get("propertyId") or "") in duplicate_enrichment_ids),
        str(prop.get("propertyId") or ""),
    ))

    fetch_limit = search_config.get("detailFetchLimit", DEFAULT_DETAIL_FETCH_LIMIT)
    fetched_count = 0
    attempted_count = 0
    detail_failed_count = 0
    consecutive_errors = 0

    print(
        f"--- Detail Fetching (対象候補: {len(detail_candidates)}件, "
        f"重複詳細優先: {len(duplicate_enrichment_ids)}件, 上限: {fetch_limit}件) ---"
    )

    for prop in detail_candidates:
        if attempted_count >= fetch_limit:
            break
        if consecutive_errors >= MAX_CONSECUTIVE_DETAIL_CONNECTIVITY_ERRORS:
            print("[WARN] 連続通信エラーのため詳細フェッチを中断します。")
            break

        url = prop.get("sourceUrl") or prop.get("url")
        if not url:
            continue

        try:
            attempted_count += 1
            prop["lastDetailAttemptAt"] = now_iso()
            prop["detailFetchAttempts"] = int(prop.get("detailFetchAttempts") or 0) + 1
            detail_res = detail_adapter.fetch_detail(url)
            prop["detailFetchHttpStatus"] = detail_res.get("httpStatus")
            if detail_res.get("success") is True:
                prop["lastDetailFetchAt"] = now_iso()
                prop["detailFetchLastError"] = None
                prop["detailFetchErrorAt"] = None
                detail_payload = detail_res.get("detail") or {}
                # 明示的な終了フラグが返された場合だけ確定終了とする。
                if detail_payload.get("listingEnded") is True or detail_payload.get("isListingEnded") is True:
                    prop["isListingEnded"] = True
                    prop["status"] = "confirmed_ended"
                    prop["listingEndReason"] = "explicit_detail_page_evidence"
                    prop["listingEndEvidence"] = (
                        detail_payload.get("listingEndEvidence")
                        or detail_payload.get("statusText")
                        or "detail_adapter_explicit_flag"
                    )
                    prop["endedAt"] = now_iso()
                    prop["listingStatusUpdatedAt"] = prop["endedAt"]
            else:
                detail_failed_count += 1
                prop["detailFetchLastError"] = str(detail_res.get("error") or "detail_fetch_failed")[:500]
                prop["detailFetchErrorAt"] = now_iso()
            enrich_with_detail(prop, detail_res)
            promote_detail_fields(prop)
            canonicalize_property_identity(prop)
            evaluate_property_criteria(prop, search_config)
            apply_detail_quality(prop)

            if prop.get("detailFetchStatus") == "success":
                fetched_count += 1
                consecutive_errors = 0
            else:
                if detail_res.get("success") is True:
                    detail_failed_count += 1
                err_type = prop.get("detailFetchErrorType")
                if err_type in RETRYABLE_DETAIL_ERROR_TYPES:
                    consecutive_errors += 1

        except Exception as exc:
            detail_failed_count += 1
            prop["detailFetchLastError"] = str(exc)[:500]
            prop["detailFetchErrorAt"] = now_iso()
            prop["detailFetchStatus"] = "error"
            print(f"[ERROR] 詳細フェッチ失敗: {url}: {exc}")
            consecutive_errors += 1

    # ========================================================
    # 6. Save Databases
    # ========================================================

    duplicate_candidate_count = mark_duplicate_candidates(list(db.values()))
    print(
        f"[INFO] 別掲載IDの重複候補: {duplicate_candidate_count}件 "
        "(自動統合せず候補として保持)"
    )

    # 6a. Discovered DB (全掲載・全IDを保持)
    discovered_records = compact_discovery_db(list(db.values()))
    save_json(
        DISCOVERED_PATH,
        {
            "version": DISCOVERY_SCHEMA_VERSION,
            "updatedAt": now_iso(),
            "count": len(discovered_records),
            "properties": discovered_records,
        },
    )

    # 6b. Listing Observations (差分のみ追記。毎回全件を上書きしない)
    previous_observations_raw = load_json(
        OBSERVATIONS_PATH,
        default={"observations": []},
    )
    if isinstance(previous_observations_raw, dict):
        observations = previous_observations_raw.get("observations", [])
    elif isinstance(previous_observations_raw, list):
        observations = previous_observations_raw
    else:
        observations = []
    if not isinstance(observations, list):
        observations = []

    current_observations = [
        build_listing_observation(prop)
        for prop in db.values()
    ]
    observations, appended_observations = append_changed_observations(
        observations,
        current_observations,
        compare_fields=(
            "status", "currentPrice", "currentPriceMan",
            "priceConfidence", "priceStatus", "priceSource", "searchTargets",
        ),
    )

    cutoff = datetime.now(timezone.utc).timestamp() - OBSERVATION_RETENTION_DAYS * 86400
    retained_observations = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        observed_dt = parse_iso_datetime(observation.get("observedAt"))
        if observed_dt is None or observed_dt.timestamp() >= cutoff:
            retained_observations.append(observation)
    if len(retained_observations) > OBSERVATION_MAX_RECORDS:
        retained_observations = retained_observations[-OBSERVATION_MAX_RECORDS:]

    save_json(
        OBSERVATIONS_PATH,
        {
            "version": "2.0",
            "updatedAt": now_iso(),
            "count": len(retained_observations),
            "appendedInRun": appended_observations,
            "observations": retained_observations,
        },
    )
    print(
        f"[INFO] 観測履歴: 今回追加={appended_observations}件 / "
        f"保存総数={len(retained_observations)}件"
    )

    # 6c. Market Houses DB (同一物件を1件にまとめて表示)
    market_house_records = [
        build_market_house_record(prop)
        for prop in db.values()
        if is_market_history_property(prop)
    ]

    # UI表示用 houses.json 生成直前に物理キーでの重複排除を適用
    deduplicated_houses = deduplicate_by_physical_identity(market_house_records)

    save_json(
        HOUSES_PATH,
        {
            "version": HOUSE_DB_SCHEMA_VERSION,
            "updatedAt": now_iso(),
            "count": len(deduplicated_houses),
            "properties": deduplicated_houses,
        },
    )

    # 6d. Summary
    status_counts = Counter(prop.get("status", "unknown") for prop in db.values())
    lifecycle_analytics = summarize_listing_lifecycle(list(db.values()))
    criteria_reviews = [get_market_criteria_review(prop) for prop in db.values()]
    criteria_review_counts = Counter(item["marketReviewStatus"] for item in criteria_reviews)
    market_area_pending_count = sum(
        1 for prop in db.values() if prop.get("areaClassification") == "detailPending"
    )
    criteria_excluded_by_field = Counter(
        field for item in criteria_reviews for field in item["criteriaExcludedFields"]
    )
    criteria_pending_by_field = Counter(
        field for item in criteria_reviews for field in item["criteriaPendingFields"]
    )

    def _has_nonempty_field(record: Dict[str, Any], field: str) -> bool:
        value = record.get(field)
        if field == "layout":
            return isinstance(value, str) and bool(value.strip()) and value.strip() != "-"
        if isinstance(value, str):
            return bool(value.strip())
        return value is not None

    coverage_fields = {
        "layout": "layout",
        "landAreaM2": "landAreaM2",
        "buildingAreaM2": "buildingAreaM2",
        "buildingFootprintAreaM2": "buildingFootprintAreaM2",
        "address": "address",
        "targetStationWalkMinutes": "targetStationWalkMinutes",
        "constructionMonth": "constructionMonth",
    }

    def _coverage(records: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = len(records)
        fields = {}
        for label, key in coverage_fields.items():
            count = sum(1 for record in records if _has_nonempty_field(record, key))
            fields[label] = {
                "count": count,
                "ratio": round(count / total, 4) if total else 0.0,
            }
        return {"total": total, "fields": fields}

    field_coverage = {
        "houses": _coverage(deduplicated_houses),
        "discovered": _coverage(discovered_records),
    }

    summary_data = {
        "updatedAt": now_iso(),
        "totalDiscovered": len(discovered_records),
        "totalMarketHouses": len(deduplicated_houses),
        "statusCounts": dict(status_counts),
        "detailFetchedInRun": fetched_count,
        "detailCandidates": len(detail_candidates),
        "duplicateEnrichmentPriorityCount": len(duplicate_enrichment_ids),
        "duplicateEnrichmentAttemptLimit": MAX_DUPLICATE_ENRICHMENT_ATTEMPTS,
        "detailFetchLimit": fetch_limit,
        "detailAttemptsInRun": attempted_count,
        "detailFailuresInRun": detail_failed_count,
        "detailFetchPartial": attempted_count < len(detail_candidates),
        "searchTargetsHealthy": search_success_count,
        "searchTargetsFailedOrIncomplete": search_failed_count,
        "possiblyEndedCount": status_counts.get("possibly_ended", 0),
        "observedEndedCount": status_counts.get("observed_ended", 0),
        "confirmedEndedCount": status_counts.get("confirmed_ended", 0),
        "activeCount": status_counts.get("active", 0),
        "duplicateCandidateCount": duplicate_candidate_count,
        "marketCriteriaMatchedCount": criteria_review_counts.get("matched", 0),
        "marketCriteriaPendingCount": criteria_review_counts.get("pending", 0),
        "marketCriteriaExcludedCount": criteria_review_counts.get("excluded", 0),
        "marketAreaPendingCount": market_area_pending_count,
        "criteriaExcludedByField": dict(criteria_excluded_by_field),
        "criteriaPendingByField": dict(criteria_pending_by_field),
        "marketDbEmptyWarning": len(deduplicated_houses) == 0,
        "fieldCoverage": field_coverage,
        "parserVersion": MAIN_PARSER_VERSION,
        **lifecycle_analytics,
    }
    save_json(SUMMARY_PATH, summary_data)

    if not deduplicated_houses:
        print(
            "[WARN] houses.json が0件です。"
            "criteriaPendingByField / criteriaExcludedByField / marketAreaPendingCount を確認してください。"
        )

    print("[FIELD-COVERAGE] houses.json")
    for field_name, metric in field_coverage["houses"]["fields"].items():
        print(
            f"  {field_name}: {metric['count']}/{field_coverage['houses']['total']} "
            f"({metric['ratio']:.1%})"
        )
    print("[FIELD-COVERAGE] discovered_listings.json")
    for field_name, metric in field_coverage["discovered"]["fields"].items():
        print(
            f"  {field_name}: {metric['count']}/{field_coverage['discovered']['total']} "
            f"({metric['ratio']:.1%})"
        )

    print("============================================================")
    run_result = (
        "completed with warnings"
        if search_failed_count > 0 or attempted_count < len(detail_candidates) or detail_failed_count > 0
        else "completed successfully"
    )
    print(f"=== Pipeline {run_result} at {now_iso()} ===")
    print(
        f"    Discovered: {len(discovered_records)}, Houses: {len(deduplicated_houses)}, "
        f"Search healthy: {search_success_count}, search incomplete: {search_failed_count}, "
        f"detail attempts: {attempted_count}, detail failures: {detail_failed_count}, "
        f"observations added: {appended_observations}"
    )
    print("============================================================")


if __name__ == "__main__":
    run_pipeline()
