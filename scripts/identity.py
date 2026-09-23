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


def get_source_id(
    property_data: Dict[str, Any],
) -> Optional[str]:
    source = str(
        property_data.get(
            "source",
            "",
        )
    ).strip().lower()

    # SUUMOではURL上のnc_IDを最優先
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

        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def make_property_id(
    property_data: Dict[str, Any],
) -> Optional[str]:
    source = str(
        property_data.get(
            "source",
            "unknown",
        )
    ).strip().lower() or "unknown"

    source_id = get_source_id(
        property_data
    )

    if not source_id:
        return None

    return f"{source}:{source_id}"


def make_identity_key(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:
    source = str(
        property_data.get(
            "source",
            "unknown",
        )
    ).strip().lower() or "unknown"

    source_id = get_source_id(
        property_data
    )

    if source_id:
        property_id = (
            f"{source}:{source_id}"
        )

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