from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from identity import (
    get_source_id,
    make_identity_key,
)


AREA_ALIASES = {
    "柏の葉": "柏の葉キャンパス",
    "柏の葉キャンパス": "柏の葉キャンパス",
    "おおたかの森": "流山おおたかの森",
    "流山おおたかの森": "流山おおたかの森",
}


def now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_area(
    area: Any,
):
    if not area:
        return None

    value = str(area).strip()

    return AREA_ALIASES.get(
        value,
        value,
    )


def to_number(
    value: Any,
):
    if value is None or value == "":
        return None

    if isinstance(
        value,
        (int, float),
    ):
        return value

    text = str(value)

    for token in (
        ",",
        "㎡",
        "m²",
        "m2",
    ):
        text = text.replace(
            token,
            "",
        )

    text = text.strip()

    try:
        return float(text)
    except ValueError:
        return None


def to_integer(
    value: Any,
):
    number = to_number(value)

    if number is None:
        return None

    return int(number)


def normalize_property(
    data: Dict[str, Any],
) -> Dict[str, Any]:

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            "property data must be dict"
        )

    source = str(
        data.get(
            "source",
            "unknown",
        )
    ).strip().lower() or "unknown"

    source_id = get_source_id(
        data
    )

    if not source_id:
        raise ValueError(
            "sourceId / listingId / propertyId "
            "を取得できません"
        )

    identity = make_identity_key(
        data
    )

    collected_at = (
        data.get("collectedAt")
        or now_iso()
    )

    normalized = dict(data)

    normalized.update(
        {
            # ------------------------------------------------
            # Canonical identity
            # ------------------------------------------------
            "source": source,
            "sourceId": str(source_id),
            "sourcePropertyId": str(
                source_id
            ),
            "propertyId": identity[
                "propertyId"
            ],
            "identityKey": identity[
                "identityKey"
            ],
            "identityType": identity[
                "identityType"
            ],
            "identityCompleteness": identity[
                "identityCompleteness"
            ],

            # ------------------------------------------------
            # URL
            # ------------------------------------------------
            "sourceUrl": (
                data.get("sourceUrl")
                or data.get("url")
                or ""
            ),

            # ------------------------------------------------
            # Search / area
            # ------------------------------------------------
            "area": normalize_area(
                data.get("area")
            ),

            "searchArea": normalize_area(
                data.get("searchArea")
            ),

            # ------------------------------------------------
            # Basic property
            # ------------------------------------------------
            "name": (
                data.get("name")
                or ""
            ),

            "propertyType": (
                data.get("propertyType")
                or ""
            ),

            "price": to_number(
                data.get("price")
            ),

            "land": to_number(
                data.get("land")
            ),

            "building": to_number(
                data.get("building")
            ),

            "walk": to_number(
                data.get("walk")
            ),

            "year": to_integer(
                data.get("year")
            ),

            "layout": (
                data.get("layout")
                or ""
            ),

            # ------------------------------------------------
            # Area / quality
            # ------------------------------------------------
            "school": (
                data.get("school")
                or ""
            ),

            "flat": data.get(
                "flat"
            ),

            "retainingWall": data.get(
                "retainingWall"
            ),

            # ------------------------------------------------
            # Provenance
            # ------------------------------------------------
            "searchTarget": (
                data.get("searchTarget")
                or data.get("searchArea")
                or ""
            ),

            "searchPageNumber": (
                data.get(
                    "searchPageNumber"
                )
            ),

            "searchPosition": (
                data.get(
                    "searchPosition"
                )
            ),

            "searchPageUrl": (
                data.get(
                    "searchPageUrl"
                )
                or ""
            ),

            "discoveredAt": (
                data.get(
                    "discoveredAt"
                )
                or collected_at
            ),

            "lastSeenAt": collected_at,

            "collectedAt": collected_at,
        }
    )

    return normalized