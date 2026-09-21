"""
SUUMO individual property detail adapter.

- Fetch individual SUUMO listing pages
- Extract basic property information
- Provide SuumoDetailAdapter class for main.py compatibility
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Constants
# ---------------------------------------------------------

REQUEST_TIMEOUT = 20

USER_AGENT = (
    "Mozilla/5.0 (compatible; HouseMonitor/1.0; "
    "+https://github.com/)"
)


# ---------------------------------------------------------
# Utility functions
# ---------------------------------------------------------

def now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    """Normalize whitespace and convert a value to string."""
    if value is None:
        return ""

    text = str(value)

    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def parse_price(text: str) -> Optional[int]:
    """
    Parse Japanese property price text.

    Examples:
    - 8,490万円 -> 84900000
    - 8490万円 -> 84900000
    - 8,490万 -> 84900000
    """
    if not text:
        return None

    normalized = clean_text(text)
    normalized = normalized.replace(",", "")

    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*万?円?", normalized)

    if not match:
        return None

    try:
        number = float(match.group(1))

        if "万" in normalized:
            return int(number * 10000)

        return int(number)

    except (ValueError, TypeError):
        return None


def parse_area_m2(text: str) -> Optional[float]:
    """
    Parse area in square meters.

    Examples:
    - 120.50m²
    - 120.50㎡
    - 120.50 m2
    """
    if not text:
        return None

    normalized = clean_text(text)
    normalized = normalized.replace(",", "")

    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:m²|㎡|m2|平方メートル)",
        normalized,
        re.IGNORECASE,
    )

    if not match:
        return None

    try:
        return float(match.group(1))

    except (ValueError, TypeError):
        return None


def parse_year_month(text: str) -> Optional[str]:
    """
    Extract construction year and month.

    Examples:
    - 2020年5月
    - 2020年
    """
    if not text:
        return None

    normalized = clean_text(text)

    match = re.search(
        r"((?:19|20)\d{2})年\s*(\d{1,2})?月?",
        normalized,
    )

    if not match:
        return None

    year = match.group(1)
    month = match.group(2)

    if month:
        return f"{year}-{int(month):02d}"

    return year


def parse_walk_minutes(text: str) -> Optional[int]:
    """
    Extract walking time in minutes.

    Examples:
    - 徒歩10分
    - 徒歩 10 分
    """
    if not text:
        return None

    normalized = clean_text(text)

    match = re.search(r"徒歩\s*(\d+)\s*分", normalized)

    if not match:
        return None

    try:
        return int(match.group(1))

    except (ValueError, TypeError):
        return None


def is_promotional_text(text: str) -> bool:
    """Identify common promotional or irrelevant text."""
    if not text:
        return False

    promotional_words = [
        "おすすめ",
        "オススメ",
        "キャンペーン",
        "お問い合わせ",
        "お気軽に",
        "未公開",
        "限定",
        "資料請求",
        "見学予約",
    ]

    return any(word in text for word in promotional_words)


def is_valid_suumo_url(url: str) -> bool:
    """Validate an individual SUUMO listing URL."""
    if not url:
        return False

    try:
        parsed = urlparse(url)

        if parsed.scheme != "https":
            return False

        if parsed.netloc.lower() not in {
            "suumo.jp",
            "www.suumo.jp",
        }:
            return False

        return bool(
            re.search(
                r"/chukoikkodate/.*/nc_\d+",
                parsed.path,
            )
        )

    except Exception:
        return False


# ---------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------

def collect_label_value_pairs(
    soup: BeautifulSoup,
) -> Dict[str, str]:
    """
    Collect possible label/value pairs from tables and lists.
    """
    result: Dict[str, str] = {}

    # Table rows
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"])

        if len(cells) < 2:
            continue

        label = clean_text(cells[0].get_text(" ", strip=True))
        value = clean_text(cells[1].get_text(" ", strip=True))

        if label and value and len(label) <= 100:
            result[label] = value

    # Definition lists
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")

        if not dd:
            continue

        label = clean_text(dt.get_text(" ", strip=True))
        value = clean_text(dd.get_text(" ", strip=True))

        if label and value and len(label) <= 100:
            result[label] = value

    return result


def extract_text_blocks(soup: BeautifulSoup) -> List[str]:
    """Extract meaningful text blocks from the page."""
    blocks: List[str] = []

    selectors = [
        "h1",
        "h2",
        "h3",
        "p",
        "li",
        "th",
        "td",
        ".property_data",
        ".detail_data",
        ".section",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            text = clean_text(element.get_text(" ", strip=True))

            if not text:
                continue

            if len(text) > 500:
                continue

            if text not in blocks:
                blocks.append(text)

    return blocks


def find_value_by_keywords(
    pairs: Dict[str, str],
    keywords: List[str],
) -> Optional[str]:
    """Find a value whose label contains one of the keywords."""
    for label, value in pairs.items():
        if any(keyword in label for keyword in keywords):
            return value

    return None


def extract_title(soup: BeautifulSoup) -> Optional[str]:
    """Extract page title."""
    for selector in ["h1", "title"]:
        element = soup.select_one(selector)

        if element:
            text = clean_text(element.get_text(" ", strip=True))

            if text:
                return text[:300]

    return None


# ---------------------------------------------------------
# Detail fetching
# ---------------------------------------------------------

def fetch_detail(url: str) -> Dict[str, Any]:
    """
    Fetch and parse an individual SUUMO property page.

    Returns a detail dictionary.
    """
    if not is_valid_suumo_url(url):
        raise ValueError(f"Invalid SUUMO listing URL: {url}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    logger.info("Fetching SUUMO detail: %s", url)

    response = requests.get(
        url,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    if not response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    pairs = collect_label_value_pairs(soup)
    text_blocks = extract_text_blocks(soup)

    page_text = clean_text(
        soup.get_text(" ", strip=True)
    )

    price_text = find_value_by_keywords(
        pairs,
        ["価格", "販売価格", "価格帯"],
    )

    land_area_text = find_value_by_keywords(
        pairs,
        ["土地面積", "土地面積（", "敷地面積"],
    )

    building_area_text = find_value_by_keywords(
        pairs,
        ["建物面積", "延床面積", "延べ床面積"],
    )

    construction_text = find_value_by_keywords(
        pairs,
        ["築年月", "建築年月", "完成年月"],
    )

    station_text = find_value_by_keywords(
        pairs,
        ["駅", "交通", "最寄り駅"],
    )

    layout_text = find_value_by_keywords(
        pairs,
        ["間取り", "間取"],
    )

    address_text = find_value_by_keywords(
        pairs,
        ["所在地", "住所"],
    )

    structure_text = find_value_by_keywords(
        pairs,
        ["構造", "工法"],
    )

    detail: Dict[str, Any] = {
        "url": url,
        "title": extract_title(soup),
        "price": parse_price(price_text or page_text),
        "priceText": price_text,
        "landAreaM2": parse_area_m2(land_area_text or ""),
        "landAreaText": land_area_text,
        "buildingAreaM2": parse_area_m2(building_area_text or ""),
        "buildingAreaText": building_area_text,
        "constructionMonth": parse_year_month(
            construction_text or ""
        ),
        "constructionText": construction_text,
        "stationText": station_text,
        "layout": layout_text,
        "address": address_text,
        "structure": structure_text,
        "walkingMinutes": parse_walk_minutes(
            station_text or ""
        ),
        "labelValuePairs": pairs,
        "textBlocks": text_blocks[:200],
        "rawTextLength": len(page_text),
    }

    return detail


# ---------------------------------------------------------
# Adapter class used by main.py
# ---------------------------------------------------------

class SuumoDetailAdapter:
    """
    Adapter interface used by scripts/main.py.

    Expected usage:

        adapter = SuumoDetailAdapter(...)
        result = adapter.fetch_detail(url)
        adapter.wait()
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        root_path: Optional[str] = None,
    ):
        self.config = config or {}
        self.root_path = root_path

        self.wait_seconds = float(
            self.config.get(
                "detailRequestIntervalSeconds",
                1.5,
            )
        )

    def fetch_detail(
        self,
        url: str,
    ) -> Dict[str, Any]:
        """
        Fetch one listing and return the standard result format.
        """
        fetched_at = now_iso()

        try:
            detail = fetch_detail(url)

            return {
                "success": True,
                "fetchedAt": fetched_at,
                "detail": detail,
                "error": None,
            }

        except Exception as error:
            logger.exception(
                "Failed to fetch SUUMO detail: %s",
                url,
            )

            return {
                "success": False,
                "fetchedAt": fetched_at,
                "detail": {},
                "error": str(error),
            }

    def wait(self) -> None:
        """Wait between requests to reduce request frequency."""
        if self.wait_seconds > 0:
            time.sleep(self.wait_seconds)
