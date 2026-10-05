from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


# ============================================================
# SUUMO
# ============================================================

SUUMO_LISTING_ID_PATTERN = re.compile(
    r"/(nc_\d+)(?:/|$)",
    re.IGNORECASE,
)


# ============================================================
# Generic normalization
# ============================================================

def normalize_value(value: Any) -> str:
    """
    基本的な正規化。

    - None -> ""
    - 全角/半角スペース等を除去
    - 前後空白除去
    """
    if value is None:
        return ""

    text = str(value)

    text = (
        text.replace("　", "")
        .replace(" ", "")
        .replace("\t", "")
        .replace("\r", "")
        .replace("\n", "")
    )

    return text.strip()


def normalize_text(value: Any) -> str:
    """
    物件同一性判定用の文字列正規化。

    数字・住所・駅名・間取り等について、
    表記揺れを可能な範囲で吸収する。
    """
    if value is None:
        return ""

    text = str(value)

    # 全角数字 → 半角数字
    text = text.translate(
        str.maketrans(
            "０１２３４５６７８９",
            "0123456789",
        )
    )

    # 全角英字 → 半角英字
    text = text.translate(
        str.maketrans(
            "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
            "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz",
        )
    )

    # ハイフン類を統一
    text = (
        text.replace("－", "-")
        .replace("―", "-")
        .replace("‐", "-")
        .replace("−", "-")
        .replace("ー", "-")
    )

    # 住所表記の代表的な揺れ
    text = (
        text.replace("丁目", "-")
        .replace("番地", "-")
        .replace("番", "-")
        .replace("号", "")
    )

    # 区切り記号を除去
    text = re.sub(
        r"[\s　,，、。．\.\(\)（）【】\[\]「」『』]",
        "",
        text,
    )

    # 連続ハイフンを1つに
    text = re.sub(
        r"-+",
        "-",
        text,
    )

    return text.lower().strip("-")


# ============================================================
# Numeric normalization
# ============================================================

def normalize_numeric(
    value: Any,
    decimals: int = 2,
) -> str:
    """
    数値を同一性判定用に正規化する。

    例:
        100
        "100m²"
        "100.0"
        "１００㎡"

    → "100"
    """
    if value is None:
        return ""

    if isinstance(value, bool):
        return ""

    text = str(value)

    text = (
        text.replace(",", "")
        .replace("，", "")
        .replace("㎡", "")
        .replace("m²", "")
        .replace("m2", "")
        .replace("ｍ²", "")
        .replace("ｍ2", "")
        .replace("坪", "")
        .replace("　", "")
        .replace(" ", "")
    )

    text = text.translate(
        str.maketrans(
            "０１２３４５６７８９．",
            "0123456789.",
        )
    )

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        text,
    )

    if not match:
        return ""

    try:
        number = float(match.group(0))
    except ValueError:
        return ""

    if decimals <= 0:
        return str(int(round(number)))

    formatted = f"{number:.{decimals}f}"

    formatted = formatted.rstrip("0").rstrip(".")

    return formatted


# ============================================================
# Property field extraction
# ============================================================

def first_value(
    property_data: Dict[str, Any],
    keys: List[str],
) -> Any:
    """
    複数候補フィールドから最初の有効値を取得。
    """
    for key in keys:
        value = property_data.get(key)

        if value is None:
            continue

        if isinstance(value, str):
            if not value.strip():
                continue

        return value

    return None


def get_address(
    property_data: Dict[str, Any],
) -> str:
    return normalize_text(
        first_value(
            property_data,
            [
                "address",
                "fullAddress",
                "propertyAddress",
                "location",
            ],
        )
    )


def get_land_area(
    property_data: Dict[str, Any],
) -> str:
    value = first_value(
        property_data,
        [
            "landArea",
            "landAreaM2",
            "land",
            "landSize",
        ],
    )

    return normalize_numeric(value, decimals=2)


def get_building_area(
    property_data: Dict[str, Any],
) -> str:
    value = first_value(
        property_data,
        [
            "buildingArea",
            "buildingAreaM2",
            "building",
            "buildingSize",
        ],
    )

    return normalize_numeric(value, decimals=2)


def get_built_year(
    property_data: Dict[str, Any],
) -> str:
    value = first_value(
        property_data,
        [
            "builtYear",
            "constructionYear",
            "yearBuilt",
        ],
    )

    normalized = normalize_numeric(
        value,
        decimals=0,
    )

    if not normalized:
        return ""

    # 年そのものが入っているケース
    if 1800 <= int(float(normalized)) <= 2200:
        return normalized

    return normalized


def get_floor_plan(
    property_data: Dict[str, Any],
) -> str:
    value = first_value(
        property_data,
        [
            "floorPlan",
            "間取り",
            "layout",
        ],
    )

    return normalize_text(value)


def get_station(
    property_data: Dict[str, Any],
) -> str:
    value = first_value(
        property_data,
        [
            "station",
            "targetStation",
            "nearestStation",
        ],
    )

    return normalize_text(value)


def get_walk_minutes(
    property_data: Dict[str, Any],
) -> str:
    value = first_value(
        property_data,
        [
            "walkMinutes",
            "stationWalkMinutes",
            "targetStationWalkMinutes",
        ],
    )

    return normalize_numeric(
        value,
        decimals=0,
    )


def get_property_type(
    property_data: Dict[str, Any],
) -> str:
    value = first_value(
        property_data,
        [
            "propertyType",
            "propertyTypeName",
            "searchPropertyType",
        ],
    )

    return normalize_text(value)


# ============================================================
# SUUMO source identity
# ============================================================

def extract_suumo_listing_id(
    url: Any,
) -> Optional[str]:
    """
    URLからSUUMOのnc_IDを抽出する。
    """
    if not url:
        return None

    try:
        parsed = urlparse(
            str(url).strip()
        )
    except ValueError:
        return None

    match = SUUMO_LISTING_ID_PATTERN.search(
        parsed.path or ""
    )

    if not match:
        return None

    return match.group(1).lower()


# ============================================================
# Source detection
# ============================================================

def infer_source(
    property_data: Dict[str, Any],
) -> str:
    """
    物件データからsourceを推定する。

    優先順位:
        1. 明示されたsource
        2. sourceUrl / urlからSUUMO IDを検出
        3. propertyIdがsuumo:で始まる
        4. unknown
    """

    source = str(
        property_data.get(
            "source",
            "",
        )
    ).strip().lower()

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
        property_data.get(
            "propertyId",
            "",
        )
    ).strip().lower()

    if property_id.startswith("suumo:"):
        return "suumo"

    return "unknown"


# ============================================================
# Source ID
# ============================================================

def get_source_id(
    property_data: Dict[str, Any],
) -> Optional[str]:
    """
    物件のsource固有IDを取得する。

    SUUMOではURLのnc_IDを最優先する。

    重要:
        sourceIdは「掲載」を識別するIDであり、
        実物件そのものの恒久IDではない。
    """

    source = infer_source(
        property_data
    )

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

        # suumo:nc_xxxxx
        if (
            source == "suumo"
            and value.lower().startswith("suumo:")
        ):
            value = value.split(
                ":",
                1,
            )[1].strip()

        if value:
            return value

    return None


# ============================================================
# Listing identity
# ============================================================

def make_property_id(
    property_data: Dict[str, Any],
) -> Optional[str]:
    """
    掲載単位のIDを生成する。

    例:
        suumo:nc_12345678

    これは「掲載ID」であり、
    同一物件の別会社掲載・再掲載とは別IDになり得る。
    """

    source = infer_source(
        property_data
    )

    source_id = get_source_id(
        property_data
    )

    if not source_id:
        return None

    if source == "unknown":
        return None

    return f"{source}:{source_id}"


# ============================================================
# Canonical property identity
# ============================================================

def build_identity_components(
    property_data: Dict[str, Any],
) -> Dict[str, str]:
    """
    掲載IDに依存しない「実物件同一性」判定用コンポーネント。

    価格は意図的に含めない。

    理由:
        価格変更
        値下げ
        再掲載
        別会社掲載

    があっても同一物件として追跡したいため。
    """

    return {
        "address": get_address(
            property_data
        ),
        "landArea": get_land_area(
            property_data
        ),
        "buildingArea": get_building_area(
            property_data
        ),
        "builtYear": get_built_year(
            property_data
        ),
        "floorPlan": get_floor_plan(
            property_data
        ),
        "propertyType": get_property_type(
            property_data
        ),
        "station": get_station(
            property_data
        ),
        "walkMinutes": get_walk_minutes(
            property_data
        ),
    }


def calculate_identity_completeness(
    components: Dict[str, str],
) -> float:
    """
    実物件同一性判定情報の充足率。

    addressを最重要情報として扱う。
    """

    important_fields = [
        "address",
        "landArea",
        "buildingArea",
        "builtYear",
        "floorPlan",
    ]

    filled_count = sum(
        bool(
            components.get(field)
        )
        for field in important_fields
    )

    return round(
        filled_count
        / len(important_fields),
        3,
    )


def build_canonical_identity(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    掲載IDに依存しないcanonical identityを生成する。

    優先順位:

    Tier 1:
        address + landArea + buildingArea

    Tier 2:
        address + landArea + builtYear + floorPlan

    Tier 3:
        address + buildingArea + builtYear + floorPlan

    Tier 4:
        address + landArea + buildingArea

    Tier 5:
        address単独は使用しない

    住所だけで統合すると同一住所の別区画・二世帯住宅等で
    誤統合する可能性があるため、単独住所ではcanonical keyを
    作らない。
    """

    components = build_identity_components(
        property_data
    )

    address = components["address"]
    land_area = components["landArea"]
    building_area = components["buildingArea"]
    built_year = components["builtYear"]
    floor_plan = components["floorPlan"]
    property_type = components["propertyType"]

    completeness = calculate_identity_completeness(
        components
    )

    # --------------------------------------------------------
    # Tier 1
    # 住所 + 土地 + 建物
    #
    # 別会社掲載・再出品に最も強い。
    # 価格は含めない。
    # --------------------------------------------------------

    if (
        address
        and land_area
        and building_area
    ):
        raw = "|".join(
            [
                "address_land_building",
                address,
                land_area,
                building_area,
                property_type,
            ]
        )

        digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        return {
            "canonicalIdentityKey": (
                f"property:"
                f"{digest[:24]}"
            ),
            "canonicalIdentityType": (
                "address_land_building"
            ),
            "canonicalIdentityCompleteness": max(
                completeness,
                0.6,
            ),
            "canonicalIdentityComponents": components,
        }

    # --------------------------------------------------------
    # Tier 2
    # 住所 + 土地 + 築年 + 間取り
    # --------------------------------------------------------

    if (
        address
        and land_area
        and built_year
        and floor_plan
    ):
        raw = "|".join(
            [
                "address_land_year_plan",
                address,
                land_area,
                built_year,
                floor_plan,
                property_type,
            ]
        )

        digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        return {
            "canonicalIdentityKey": (
                f"property:"
                f"{digest[:24]}"
            ),
            "canonicalIdentityType": (
                "address_land_year_plan"
            ),
            "canonicalIdentityCompleteness": max(
                completeness,
                0.6,
            ),
            "canonicalIdentityComponents": components,
        }

    # --------------------------------------------------------
    # Tier 3
    # 住所 + 建物 + 築年 + 間取り
    # --------------------------------------------------------

    if (
        address
        and building_area
        and built_year
        and floor_plan
    ):
        raw = "|".join(
            [
                "address_building_year_plan",
                address,
                building_area,
                built_year,
                floor_plan,
                property_type,
            ]
        )

        digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        return {
            "canonicalIdentityKey": (
                f"property:"
                f"{digest[:24]}"
            ),
            "canonicalIdentityType": (
                "address_building_year_plan"
            ),
            "canonicalIdentityCompleteness": max(
                completeness,
                0.6,
            ),
            "canonicalIdentityComponents": components,
        }

    # --------------------------------------------------------
    # Tier 4
    # 住所 + 土地 + 建物
    #
    # propertyTypeを含める。
    # --------------------------------------------------------

    if (
        address
        and (
            land_area
            or building_area
        )
    ):
        raw = "|".join(
            [
                "address_size",
                address,
                land_area,
                building_area,
                property_type,
            ]
        )

        digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        return {
            "canonicalIdentityKey": (
                f"property:"
                f"{digest[:24]}"
            ),
            "canonicalIdentityType": (
                "address_size"
            ),
            "canonicalIdentityCompleteness": max(
                completeness,
                0.4,
            ),
            "canonicalIdentityComponents": components,
        }

    # --------------------------------------------------------
    # Canonical identity cannot be safely created
    #
    # 住所だけの場合などは、誤統合防止のため
    # canonical identityを作らない。
    # --------------------------------------------------------

    return {
        "canonicalIdentityKey": None,
        "canonicalIdentityType": "insufficient",
        "canonicalIdentityCompleteness": completeness,
        "canonicalIdentityComponents": components,
    }


# ============================================================
# Identity aliases
# ============================================================

def get_identity_aliases(
    property_data: Dict[str, Any],
) -> List[str]:
    """
    物件を検索・統合する際に利用可能なID一覧。

    例:
        suumo:nc_12345678
        property:abcdef123456...

    を同一レコードに保持できるようにする。
    """

    aliases: List[str] = []

    property_id = make_property_id(
        property_data
    )

    if property_id:
        aliases.append(
            property_id
        )

    canonical = build_canonical_identity(
        property_data
    )

    canonical_key = canonical.get(
        "canonicalIdentityKey"
    )

    if canonical_key:
        aliases.append(
            canonical_key
        )

    existing_aliases = property_data.get(
        "identityAliases"
    )

    if isinstance(
        existing_aliases,
        list,
    ):
        for value in existing_aliases:
            if value is None:
                continue

            value = str(value).strip()

            if (
                value
                and value not in aliases
            ):
                aliases.append(value)

    return aliases


# ============================================================
# Public identity API
# ============================================================

def make_identity_key(
    property_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    物件のidentity情報を生成する。

    注意:
        propertyId:
            掲載単位ID

        canonicalIdentityKey:
            実物件単位ID

    を明確に分離する。
    """

    source = infer_source(
        property_data
    )

    source_id = get_source_id(
        property_data
    )

    property_id = None

    if (
        source_id
        and source != "unknown"
    ):
        property_id = (
            f"{source}:{source_id}"
        )

    canonical = build_canonical_identity(
        property_data
    )

    canonical_key = canonical.get(
        "canonicalIdentityKey"
    )

    canonical_type = canonical.get(
        "canonicalIdentityType"
    )

    canonical_completeness = canonical.get(
        "canonicalIdentityCompleteness",
        0.0,
    )

    aliases = get_identity_aliases(
        property_data
    )

    # --------------------------------------------------------
    # Source IDあり
    # --------------------------------------------------------

    if property_id:
        return {
            "propertyId": property_id,
            "identityKey": property_id,
            "identityType": "source_id",
            "identityCompleteness": 1.0,

            "canonicalIdentityKey": canonical_key,
            "canonicalIdentityType": canonical_type,
            "canonicalIdentityCompleteness": (
                canonical_completeness
            ),

            "identityAliases": aliases,
        }

    # --------------------------------------------------------
    # Source IDなし
    # --------------------------------------------------------

    if canonical_key:
        return {
            "propertyId": canonical_key,
            "identityKey": canonical_key,
            "identityType": canonical_type,
            "identityCompleteness": (
                canonical_completeness
            ),

            "canonicalIdentityKey": canonical_key,
            "canonicalIdentityType": canonical_type,
            "canonicalIdentityCompleteness": (
                canonical_completeness
            ),

            "identityAliases": aliases,
        }

    # --------------------------------------------------------
    # 最終fallback
    # --------------------------------------------------------

    components = build_identity_components(
        property_data
    )

    values = [
        components.get(
            field,
            "",
        )
        for field in (
            "address",
            "landArea",
            "buildingArea",
            "builtYear",
            "floorPlan",
            "propertyType",
            "station",
            "walkMinutes",
        )
    ]

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
        f"{digest[:24]}"
    )

    return {
        "propertyId": identity_key,
        "identityKey": identity_key,
        "identityType": "fallback_hash",
        "identityCompleteness": (
            calculate_identity_completeness(
                components
            )
        ),

        "canonicalIdentityKey": None,
        "canonicalIdentityType": "insufficient",
        "canonicalIdentityCompleteness": (
            calculate_identity_completeness(
                components
            )
        ),

        "identityAliases": [
            identity_key
        ],
    }


# ============================================================
# Convenience API
# ============================================================

def get_property_id(
    property_data: Dict[str, Any],
) -> Optional[str]:
    """
    掲載単位のpropertyIdを取得する。
    """
    return make_property_id(
        property_data
    )


def get_canonical_identity_key(
    property_data: Dict[str, Any],
) -> Optional[str]:
    """
    実物件単位のcanonicalIdentityKeyを取得する。
    """
    result = build_canonical_identity(
        property_data
    )

    value = result.get(
        "canonicalIdentityKey"
    )

    if value:
        return str(value)

    return None