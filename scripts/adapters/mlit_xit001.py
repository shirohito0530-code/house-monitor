from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests


# ============================================================
# Adapter version
# ============================================================

XIT001_ADAPTER_VERSION = "2026-09-27-v1"


# ============================================================
# Constants
# ============================================================

DEFAULT_TIMEOUT = 30

PROPERTY_TYPE_DETACHED = "中古戸建"


# ============================================================
# Exceptions
# ============================================================

class MLITAdapterError(Exception):
    pass


# ============================================================
# Utility
# ============================================================

def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    return text


def _to_number(value: Any) -> Optional[float]:
    """
    数値文字列を数値へ変換。
    カンマ、円、m²等を除去。
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    text = text.replace(",", "")
    text = text.replace("円", "")
    text = text.replace("㎡", "")
    text = text.replace("m²", "")
    text = text.replace("m2", "")

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    number = _to_number(value)

    if number is None:
        return None

    return int(number)


def _normalize_price(value: Any) -> Optional[int]:
    """
    取引価格を円単位の整数へ。
    """
    number = _to_number(value)

    if number is None:
        return None

    return int(round(number))


def _normalize_area(value: Any) -> Optional[float]:
    number = _to_number(value)

    if number is None:
        return None

    return round(number, 2)


def _make_transaction_id(row: Dict[str, Any]) -> str:
    """
    XIT001に安定したIDが存在しない場合のfallback。

    取引年月・地域・価格・面積等を組み合わせて
    SHA-256の短縮値を生成する。
    """

    raw = "|".join(
        str(row.get(key) or "")
        for key in [
            "transactionDate",
            "prefecture",
            "city",
            "district",
            "price",
            "landArea",
            "buildingArea",
            "builtYear",
            "propertyType",
        ]
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:24]

    return f"mlit:{digest}"


# ============================================================
# XIT001 Adapter
# ============================================================

class XIT001Adapter:

    def __init__(
        self,
        api_url: str,
        api_key: str,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "house-monitor/"
                    f"{XIT001_ADAPTER_VERSION}"
                )
            }
        )

    # --------------------------------------------------------
    # API request
    # --------------------------------------------------------

    def _request(
        self,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:

        try:
            response = self.session.get(
                self.api_url,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MLITAdapterError(
                f"XIT001 request failed: {exc}"
            ) from exc

        if response.status_code != 200:
            raise MLITAdapterError(
                "XIT001 HTTP error: "
                f"{response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise MLITAdapterError(
                "XIT001 returned invalid JSON"
            ) from exc

        if not isinstance(data, dict):
            raise MLITAdapterError(
                "XIT001 response is not an object"
            )

        return data

    # --------------------------------------------------------
    # Raw records
    # --------------------------------------------------------

    def fetch(
        self,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:

        data = self._request(params)

        records = self._extract_records(data)

        return records

    # --------------------------------------------------------
    # Extract records
    # --------------------------------------------------------

    def _extract_records(
        self,
        data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:

        """
        XIT001の実レスポンスに合わせてここを確定させる。

        APIレスポンスのrecords/data/results等を
        ここで吸収する。
        """

        for key in (
            "data",
            "results",
            "records",
            "result",
        ):
            value = data.get(key)

            if isinstance(value, list):
                return [
                    item
                    for item in value
                    if isinstance(item, dict)
                ]

        return []

    # --------------------------------------------------------
    # Normalize
    # --------------------------------------------------------

    def normalize(
        self,
        raw: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:

        """
        XIT001 → house-monitor共通schema
        """

        # ----------------------------------------------
        # 実APIのフィールド名確定後にここを合わせる
        # ----------------------------------------------

        transaction_date = _clean_text(
            raw.get("transactionDate")
            or raw.get("transaction_date")
            or raw.get("transactionPeriod")
        )

        prefecture = _clean_text(
            raw.get("prefecture")
            or raw.get("prefectureName")
        )

        city = _clean_text(
            raw.get("city")
            or raw.get("cityName")
            or raw.get("municipality")
        )

        district = _clean_text(
            raw.get("district")
            or raw.get("districtName")
            or raw.get("town")
        )

        price = _normalize_price(
            raw.get("price")
            or raw.get("transactionPrice")
            or raw.get("tradePrice")
        )

        land_area = _normalize_area(
            raw.get("landArea")
            or raw.get("land_area")
        )

        building_area = _normalize_area(
            raw.get("buildingArea")
            or raw.get("building_area")
        )

        built_year = _to_int(
            raw.get("builtYear")
            or raw.get("buildingYear")
        )

        property_type = _clean_text(
            raw.get("propertyType")
            or raw.get("type")
        )

        structure = _clean_text(
            raw.get("structure")
        )

        # ----------------------------------------------
        # 最低限の品質チェック
        # ----------------------------------------------

        if not prefecture:
            return None

        if not city:
            return None

        if price is None:
            return None

        # ----------------------------------------------
        # normalized record
        # ----------------------------------------------

        record = {
            "transactionId": _make_transaction_id(
                {
                    "transactionDate": transaction_date,
                    "prefecture": prefecture,
                    "city": city,
                    "district": district,
                    "price": price,
                    "landArea": land_area,
                    "buildingArea": building_area,
                    "builtYear": built_year,
                    "propertyType": property_type,
                }
            ),

            "source": "mlit",

            "sourceType": "XIT001",

            "adapterVersion": XIT001_ADAPTER_VERSION,

            "transactionDate": transaction_date,

            "prefecture": prefecture,

            "city": city,

            "district": district,

            "propertyType": property_type,

            "price": price,

            "landArea": land_area,

            "buildingArea": building_area,

            "builtYear": built_year,

            "structure": structure,

            "fetchedAt": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        return record

    # --------------------------------------------------------
    # Fetch + normalize
    # --------------------------------------------------------

    def fetch_normalized(
        self,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:

        raw_records = self.fetch(params)

        normalized = []

        for raw in raw_records:

            try:
                record = self.normalize(raw)

                if record is not None:
                    normalized.append(record)

            except Exception as exc:

                print(
                    "[MLIT] normalize error:",
                    exc,
                )

        return normalized