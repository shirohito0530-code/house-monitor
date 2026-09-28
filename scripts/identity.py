from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse


SUUMO_LISTING_ID_PATTERN = re.compile(
    r"/(nc_\d+)(?:/|$)",
    re.IGNORECASE,
)


def normalize_value(value: Any) -> str:
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        "",
        str(value),
    )


def extract_suumo_listing_id(
    url: Any,
) -> Optional[str]:
    if not url:
        return None

    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return None

    match = SUUMO_LISTING_ID_PATTERN.search(
        parsed.path or ""
    )

    if not match:
        return None

    return match.group(1).lower()


def infer_source(property_data: Dict[str, Any]) -> str:
    """
    物件データからsourceを推定する。
    優先順位:
    1. 明示されたsource
    2. sourceUrl / urlからSUUMO IDを検出
    3. 既存propertyIdが suumo: で始まる
    4. unknown
    """
    source = str(property_data.get("source", "")).strip().lower()
    if source and source != "unknown":
        return source
    url = (
        property_data.get("sourceUrl")
        or property_data.get("url")
        or ""
    )
    if extract_suumo_listing_id(url):
        return "suumo"
    property_id = str(
        property_data.get("propertyId") or ""
    ).strip().lower()
    if property_id.startswith("suumo:"):
        return "suumo"
    return "unknown"


def get_source_id(property_data: Dict[str, Any]) -> Optional[str]:
    """
    物件のsource固有IDを取得する。
    SUUMOはURL上のnc_IDを最優先する。
    """
    source = infer_source(property_data)
    if source == "suumo":
        source_id = extract_suumo_listing_id(
            property_data.get("sourceUrl")
            or property_data.get("url")
        )
        if source_id:
            return source_id
    for key in (
        "sourceId",
        "sourcePropertyId",
        "listingId",
        "propertyId",
        "id",
    ):
        value = property_data.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue
        # 既存の完全なSUUMO propertyIdからID部分を取得
        if source == "suumo" and value.lower().startswith("suumo:"):
            value = value.split(":", 1)[1].strip()
        if value:
            return value
    return None


def make_property_id(property_data: Dict[str, Any]) -> Optional[str]:
    """
    source + sourceIdによる正規propertyIdを生成する。
    """
    source = infer_source(property_data)
    source_id = get_source_id(property_data)
    if not source_id:
        return None
    if source == "unknown":
        return None
    return f"{source}:{source_id}"


def get_property_id(property_data: Dict[str, Any]) -> Optional[str]:
    """
    物件のpropertyIdを取得する（make_property_idを利用）。
    """
    return make_property_id(property_data)


def make_identity_key(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:
    source = infer_source(property_data)
    source_id = get_source_id(property_data)

    if source_id and source != "unknown":
        property_id = f"{source}:{source_id}"

        return {
            "propertyId": property_id,
            "identityKey": property_id,
            "identityType": "source_id",
            "identityCompleteness": 1.0,
        }

    # --------------------------------------------------------
    # Source IDがないサイト向けfallback
    # --------------------------------------------------------

    fields = [
        "address",
        "landArea",
        "buildingArea",
        "builtYear",
        "floorPlan",
        "station",
        "walkMinutes",
    ]

    values = [
        normalize_value(
            property_data.get(field)
        )
        for field in fields
    ]

    filled_count = sum(
        bool(value)
        for value in values
    )

    raw = (
        source
        + "|"
        + "|".join(values)
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    identity_key = (
        f"candidate:{source}:"
        f"{digest[:20]}"
    )

    return {
        "propertyId": identity_key,
        "identityKey": identity_key,
        "identityType": "fallback_hash",
        "identityCompleteness": (
            filled_count / len(fields)
        ),
    }
