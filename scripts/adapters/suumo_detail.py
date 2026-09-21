import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup


DETAIL_PARSER_VERSION = "2026-09-21-v2"

INVALID_VALUES = {
    "",
    "-",
    "ー",
    "－",
    "―",
    "なし",
    "ヒント",
    "詳細を見る",
    "周辺環境",
    "支払シミュレーション",
    "お問い合わせ",
    "資料請求",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value)
    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    if not text:
        return None

    return text


def clean_suumo_value(value: Any) -> Optional[str]:
    text = clean_text(value)

    if not text:
        return None

    # SUUMOのUIリンク等
    text = re.sub(r"\s*\[[^\]]+\]", "", text)
    text = re.sub(r"\s*【[^】]+】", "", text)

    text = text.strip()

    if text in INVALID_VALUES:
        return None

    return text


def parse_price(value: Any) -> Optional[int]:
    """
    価格文字列を円単位の整数へ変換。

    対応例:
      1455万円
      1億4550万円
      1億2,000万円
      8,480万円
      8480万円
      8480万
    """

    text = clean_text(value)

    if not text:
        return None

    # カンマ除去
    text = text.replace(",", "")

    # 億 + 万円
    match = re.search(
        r"(?:(\d+(?:\.\d+)?)\s*億)?\s*"
        r"(?:(\d+(?:\.\d+)?)\s*万(?:円)?)",
        text,
    )

    if match:
        oku = float(match.group(1)) if match.group(1) else 0
        man = float(match.group(2)) if match.group(2) else 0

        return int(round(oku * 100_000_000 + man * 10_000))

    # 円表記
    match = re.search(r"(\d[\d\s]*)\s*円", text)

    if match:
        number = match.group(1).replace(" ", "")
        try:
            return int(number)
        except ValueError:
            pass

    return None


def parse_area_m2(value: Any) -> Optional[float]:
    """
    m2 / m 2 / m² / ㎡ に対応。
    """

    text = clean_text(value)

    if not text:
        return None

    # m2 / m 2 / m² / ㎡
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:m\s*[²2]|㎡)",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_year_month(value: Any) -> Optional[str]:
    """
    築年月を YYYY-MM に統一。
    """

    text = clean_text(value)

    if not text:
        return None

    # 2026年9月
    match = re.search(
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月",
        text,
    )

    if match:
        year = int(match.group(1))
        month = int(match.group(2))

        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"

    # 2026/09
    match = re.search(
        r"(20\d{2})\s*[/-]\s*(\d{1,2})",
        text,
    )

    if match:
        year = int(match.group(1))
        month = int(match.group(2))

        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"

    # 2026年
    match = re.search(r"(20\d{2})\s*年", text)

    if match:
        return f"{int(match.group(1)):04d}-01"

    return None


def parse_walk_minutes(value: Any) -> Optional[int]:
    """
    徒歩分数を整数へ変換。
    """

    text = clean_text(value)

    if not text:
        return None

    patterns = [
        r"(?:徒歩|歩)\s*(\d+)\s*分",
        r"(\d+)\s*分",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue

    return None


def is_promotional_text(value: Any) -> bool:
    text = clean_text(value)

    if not text:
        return True

    promotional_words = [
        "ヒント",
        "詳細を見る",
        "周辺環境",
        "支払シミュレーション",
        "お問い合わせ",
        "資料請求",
        "お待ち合わせ",
        "ご来社",
        "お気軽に",
        "クリック",
    ]

    return any(word in text for word in promotional_words)


def is_valid_suumo_url(url: str) -> bool:
    if not url:
        return False

    return bool(
        re.match(
            r"^https://(?:www\.)?suumo\.jp/chukoikkodate/[^/]+/[^/]+/nc_\d+/?$",
            url,
        )
    )


def collect_label_value_pairs(soup: BeautifulSoup) -> Dict[str, str]:
    """
    SUUMOの物件概要テーブル等から
    ラベル→値を可能な範囲で収集。
    """

    pairs: Dict[str, str] = {}

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])

        if len(cells) < 2:
            continue

        texts = [
            clean_text(cell.get_text(" ", strip=True))
            for cell in cells
        ]

        texts = [x for x in texts if x]

        if len(texts) < 2:
            continue

        label = texts[0]
        value = " ".join(texts[1:])

        if label and value:
            pairs[label] = value

    return pairs


def extract_text_blocks(soup: BeautifulSoup) -> List[str]:
    blocks: List[str] = []

    selectors = [
        "h1",
        "h2",
        "h3",
        "p",
        "li",
        "td",
        "th",
        "div",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            text = clean_text(
                element.get_text(" ", strip=True)
            )

            if not text:
                continue

            if len(text) > 1000:
                continue

            blocks.append(text)

    # 重複除去
    result = []
    seen = set()

    for block in blocks:
        if block in seen:
            continue

        seen.add(block)
        result.append(block)

    return result


def find_value_by_keywords(
    pairs: Dict[str, str],
    keywords: List[str],
) -> Optional[str]:

    for label, value in pairs.items():
        if any(keyword in label for keyword in keywords):
            cleaned = clean_suumo_value(value)

            if cleaned and not is_promotional_text(cleaned):
                return cleaned

    return None


def extract_title(soup: BeautifulSoup) -> Optional[str]:
    h1 = soup.find("h1")

    if h1:
        return clean_text(
            h1.get_text(" ", strip=True)
        )

    if soup.title:
        return clean_text(
            soup.title.get_text(" ", strip=True)
        )

    return None


def extract_price_from_blocks(
    blocks: List[str],
) -> tuple[Optional[int], Optional[str]]:

    price_keywords = [
        "販売価格",
        "価格",
        "販売",
    ]

    # まず価格ラベルを含むブロックを優先
    for block in blocks:
        if not any(k in block for k in price_keywords):
            continue

        price = parse_price(block)

        if price:
            return price, block

    return None, None


def extract_area_from_page(
    page_text: str,
    label: str,
) -> tuple[Optional[float], Optional[str]]:

    pattern = (
        rf"{re.escape(label)}"
        r"\s*"
        r"([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:m\s*[²2]|㎡)"
        r"(?:\s*[（(][^）)]*[）)])?"
    )

    match = re.search(
        pattern,
        page_text,
        re.IGNORECASE,
    )

    if match:
        value = float(match.group(1))

        # マッチした前後を保存
        start = max(0, match.start())
        end = min(len(page_text), match.end() + 30)

        text = page_text[start:end]

        return value, clean_text(text)

    return None, None


def extract_station_info(
    blocks: List[str],
    page_text: str,
) -> Dict[str, Any]:

    # 例:
    # 京成松戸線「五香」歩22分
    # JR常磐線「柏」歩10分
    # 五香駅 徒歩21分
    # 駅から徒歩10分

    patterns = [
        r"(.{0,100}(?:線)?[「『]?([^「』」\s]{1,20})[」』]?\s*"
        r"(?:駅)?\s*(?:徒歩|歩)\s*(\d+)\s*分)",
        r"(.{0,80}([^\s「」]{1,20})駅\s*"
        r"(?:から)?\s*(?:徒歩|歩)\s*(\d+)\s*分)",
    ]

    candidates = blocks + [page_text]

    for source in candidates:
        if not source:
            continue

        for pattern in patterns:
            match = re.search(
                pattern,
                source,
            )

            if not match:
                continue

            raw = clean_text(match.group(1))
            station = clean_text(match.group(2))

            try:
                minutes = int(match.group(3))
            except ValueError:
                minutes = None

            if not raw or not station:
                continue

            if is_promotional_text(raw):
                continue

            return {
                "transportRaw": raw,
                "station": station,
                "walkMinutes": minutes,
            }

    # 別パターン:
    # 「五香」歩22分
    match = re.search(
        r"[「『]([^」』]+)[」』]\s*歩\s*(\d+)\s*分",
        page_text,
    )

    if match:
        station = clean_text(match.group(1))

        return {
            "transportRaw": match.group(0),
            "station": station,
            "walkMinutes": int(match.group(2)),
        }

    return {
        "transportRaw": None,
        "station": None,
        "walkMinutes": None,
    }


def extract_address(
    pairs: Dict[str, str],
    blocks: List[str],
) -> Optional[str]:

    address = find_value_by_keywords(
        pairs,
        [
            "所在地",
            "住所",
            "物件所在地",
        ],
    )

    if address:
        return clean_suumo_value(address)

    # 本文から都道府県＋市区町村を含むものを探す
    pattern = r"(千葉県[^。]{2,100})"

    for block in blocks:
        match = re.search(pattern, block)

        if match:
            candidate = clean_suumo_value(
                match.group(1)
            )

            if candidate and len(candidate) < 150:
                return candidate

    return None


def extract_layout(
    pairs: Dict[str, str],
    blocks: List[str],
) -> Optional[str]:

    layout = find_value_by_keywords(
        pairs,
        [
            "間取り",
        ],
    )

    if layout and re.search(
        r"\d+LDK|\d+DK|\d+K",
        layout,
        re.IGNORECASE,
    ):
        return layout

    for block in blocks:
        match = re.search(
            r"\b\d+\s*(?:LDK|DK|LK|K|L)\s*(?:\+\s*S)?",
            block,
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(0))

    return None


def extract_construction(
    pairs: Dict[str, str],
    blocks: List[str],
) -> tuple[Optional[str], Optional[str]]:

    value = find_value_by_keywords(
        pairs,
        [
            "完成時期",
            "築年月",
            "建築年月",
            "完成年月",
        ],
    )

    if value:
        parsed = parse_year_month(value)

        if parsed:
            return parsed, value

    for block in blocks:
        parsed = parse_year_month(block)

        if parsed and (
            "築" in block
            or "完成" in block
            or "建築" in block
        ):
            return parsed, block

    return None, None


def extract_land_area(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> tuple[Optional[float], Optional[str]]:

    value = find_value_by_keywords(
        pairs,
        [
            "土地面積",
            "敷地面積",
        ],
    )

    if value:
        parsed = parse_area_m2(value)

        if parsed:
            return parsed, value

    # 本文補完
    parsed, text = extract_area_from_page(
        page_text,
        "土地面積",
    )

    if parsed:
        return parsed, text

    # 「土地面積」のない文章
    for block in blocks:
        if "土地" not in block:
            continue

        parsed = parse_area_m2(block)

        if parsed:
            return parsed, block

    return None, None


def extract_building_area(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> tuple[Optional[float], Optional[str]]:

    value = find_value_by_keywords(
        pairs,
        [
            "建物面積",
            "延床面積",
            "建築面積",
        ],
    )

    if value:
        parsed = parse_area_m2(value)

        if parsed:
            return parsed, value

    # 本文補完
    parsed, text = extract_area_from_page(
        page_text,
        "建物面積",
    )

    if parsed:
        return parsed, text

    # 本文に「土地面積＋建物面積」がある場合
    for block in blocks:
        if "建物面積" not in block:
            continue

        parsed = parse_area_m2(
            block.split("建物面積", 1)[1]
        )

        if parsed:
            return parsed, block

    return None, None


def extract_information_dates(
    page_text: str,
) -> Dict[str, Optional[str]]:

    result = {
        "informationDate": None,
        "nextUpdateDate": None,
    }

    match = re.search(
        r"情報提供日\s*"
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
        page_text,
    )

    if match:
        result["informationDate"] = (
            f"{int(match.group(1)):04d}-"
            f"{int(match.group(2)):02d}-"
            f"{int(match.group(3)):02d}"
        )

    match = re.search(
        r"次回更新予定日\s*"
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
        page_text,
    )

    if match:
        result["nextUpdateDate"] = (
            f"{int(match.group(1)):04d}-"
            f"{int(match.group(2)):02d}-"
            f"{int(match.group(3)):02d}"
        )

    return result


def evaluate_detail_quality(
    detail: Dict[str, Any],
) -> Dict[str, Any]:

    required_fields = {
        "price": detail.get("price"),
        "address": detail.get("address"),
        "landAreaM2": detail.get("landAreaM2"),
        "buildingAreaM2": detail.get("buildingAreaM2"),
        "layout": detail.get("layout"),
        "constructionMonth": detail.get("constructionMonth"),
    }

    missing_fields = [
        field
        for field, value in required_fields.items()
        if value is None or value == ""
    ]

    warnings: List[str] = []

    if detail.get("priceText") and detail.get("price") is None:
        warnings.append(
            "価格テキストは存在するが数値化できない"
        )

    if (
        detail.get("landAreaText")
        and detail.get("landAreaM2") is None
    ):
        warnings.append(
            "土地面積テキストは存在するが数値化できない"
        )

    if (
        detail.get("buildingAreaText")
        and detail.get("buildingAreaM2") is None
    ):
        warnings.append(
            "建物面積テキストは存在するが数値化できない"
        )

    if is_promotional_text(
        detail.get("buildingAreaText")
    ):
        if detail.get("buildingAreaText"):
            warnings.append(
                "建物面積にUIテキストが混入している"
            )

    if not detail.get("station"):
        warnings.append("駅情報を抽出できない")

    if detail.get("walkMinutes") is None:
        warnings.append("徒歩分数を抽出できない")

    total_fields = len(required_fields)
    valid_fields = total_fields - len(missing_fields)

    score = round(
        valid_fields / total_fields * 100
    )

    if score >= 80 and not warnings:
        quality = "good"
    elif score >= 50:
        quality = "partial"
    else:
        quality = "poor"

    return {
        "detailQuality": quality,
        "detailQualityScore": score,
        "missingFields": missing_fields,
        "validationWarnings": warnings,
    }


def fetch_detail(url: str) -> Dict[str, Any]:

    if not is_valid_suumo_url(url):
        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": "invalid_suumo_url",
        }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=20,
        )

        response.raise_for_status()

    except Exception as exc:
        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": str(exc),
        }

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    pairs = collect_label_value_pairs(soup)
    blocks = extract_text_blocks(soup)

    page_text = clean_text(
        soup.get_text(" ", strip=True)
    ) or ""

    title = extract_title(soup)

    price, price_text = extract_price_from_blocks(
        blocks
    )

    # price label/valueからの補完
    if price is None:
        value = find_value_by_keywords(
            pairs,
            [
                "販売価格",
                "価格",
            ],
        )

        if value:
            price = parse_price(value)
            price_text = value

    address = extract_address(
        pairs,
        blocks,
    )

    land_area, land_area_text = extract_land_area(
        pairs,
        blocks,
        page_text,
    )

    building_area, building_area_text = (
        extract_building_area(
            pairs,
            blocks,
            page_text,
        )
    )

    layout = extract_layout(
        pairs,
        blocks,
    )

    construction_month, construction_text = (
        extract_construction(
            pairs,
            blocks,
        )
    )

    station_info = extract_station_info(
        blocks,
        page_text,
    )

    information_dates = extract_information_dates(
        page_text
    )

    detail: Dict[str, Any] = {
        "detailParserVersion": DETAIL_PARSER_VERSION,

        "title": title,

        "price": price,
        "priceText": clean_text(price_text),

        "address": address,

        "landAreaM2": land_area,
        "landAreaText": clean_text(land_area_text),

        "buildingAreaM2": building_area,
        "buildingAreaText": clean_text(
            building_area_text
        ),

        "layout": layout,

        "constructionMonth": construction_month,
        "constructionText": clean_text(
            construction_text
        ),

        "station": station_info.get("station"),
        "walkMinutes": station_info.get(
            "walkMinutes"
        ),
        "transportRaw": station_info.get(
            "transportRaw"
        ),

        "informationDate": information_dates.get(
            "informationDate"
        ),
        "nextUpdateDate": information_dates.get(
            "nextUpdateDate"
        ),

        "labelValuePairs": pairs,
        "textBlocks": blocks,

        "fetchedAt": now_iso(),
        "sourceUrl": url,
    }

    quality = evaluate_detail_quality(
        detail
    )

    detail.update(quality)

    return {
        "success": True,
        "fetchedAt": detail["fetchedAt"],
        "detail": detail,
        "error": None,
    }


class SuumoDetailAdapter:

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.config = config or {}

        self.interval_seconds = float(
            self.config.get(
                "detailRequestIntervalSeconds",
                1.5,
            )
        )

    def wait(self):
        time.sleep(
            self.interval_seconds
        )

    def fetch_detail(
        self,
        url: str,
    ) -> Dict[str, Any]:

        result = fetch_detail(url)

        return result
