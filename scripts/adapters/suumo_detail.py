import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


# ============================================================
# Parser version
# ============================================================

DETAIL_PARSER_VERSION = "2026-09-21-v3"


# ============================================================
# Constants
# ============================================================

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


# ============================================================
# Basic utilities
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()

    return text or None


def clean_suumo_value(value: Any) -> Optional[str]:
    text = clean_text(value)

    if not text:
        return None

    text = re.sub(r"\s*\[[^\]]+\]", "", text)
    text = re.sub(r"\s*【[^】]+】", "", text)
    text = text.strip()

    if text in INVALID_VALUES:
        return None

    return text


def is_promotional_text(value: Any) -> bool:
    text = clean_text(value)

    if not text:
        return True

    words = [
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

    return any(word in text for word in words)


# ============================================================
# Price
# ============================================================

def parse_price(value: Any) -> Optional[int]:
    text = clean_text(value)

    if not text:
        return None

    text = text.replace(",", "").replace(" ", "")

    match = re.search(
        r"(?:(\d+(?:\.\d+)?)\s*億)?"
        r"\s*(?:(\d+(?:\.\d+)?)\s*万(?:円)?)",
        text,
    )

    if match:
        oku = float(match.group(1) or 0)
        man = float(match.group(2) or 0)

        price = int(
            round(
                oku * 100_000_000
                + man * 10_000
            )
        )

        return price if price > 0 else None

    match = re.search(r"(\d[\d\s]*)\s*円", text)

    if match:
        try:
            price = int(
                match.group(1).replace(" ", "")
            )
            return price if price > 0 else None
        except ValueError:
            return None

    return None


# ============================================================
# Area
# ============================================================

def parse_area_m2(value: Any) -> Optional[float]:
    text = clean_text(value)

    if not text:
        return None

    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:m\s*[²2]|㎡)",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    try:
        number = float(match.group(1))
        return number if number > 0 else None
    except ValueError:
        return None


# ============================================================
# Construction date
# ============================================================

def parse_year_month(value: Any) -> Optional[str]:
    text = clean_text(value)

    if not text:
        return None

    patterns = [
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月",
        r"(20\d{2})\s*[/-]\s*(\d{1,2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if not match:
            continue

        year = int(match.group(1))
        month = int(match.group(2))

        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"

    match = re.search(r"(20\d{2})\s*年", text)

    if match:
        return f"{int(match.group(1)):04d}-01"

    return None


# ============================================================
# URL validation
# ============================================================

def is_valid_suumo_url(url: str) -> bool:
    if not url:
        return False

    return bool(
        re.match(
            r"^https://(?:www\.)?suumo\.jp/"
            r"chukoikkodate/"
            r"[^/]+/"
            r"[^/]+/"
            r"nc_\d+/?$",
            url,
        )
    )


# ============================================================
# Label / value extraction
# ============================================================

def collect_label_value_pairs(
    soup: BeautifulSoup,
) -> Dict[str, str]:

    pairs: Dict[str, str] = {}

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])

        if len(cells) < 2:
            continue

        texts = []

        for cell in cells:
            text = clean_text(
                cell.get_text(" ", strip=True)
            )

            if text:
                texts.append(text)

        if len(texts) < 2:
            continue

        label = texts[0]
        value = " ".join(texts[1:])

        if label and value:
            pairs[label] = value

    return pairs


def extract_text_blocks(
    soup: BeautifulSoup,
) -> List[str]:

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
        if not any(keyword in label for keyword in keywords):
            continue

        cleaned = clean_suumo_value(value)

        if not cleaned:
            continue

        if is_promotional_text(cleaned):
            continue

        return cleaned

    return None


# ============================================================
# Title
# ============================================================

def extract_title(
    soup: BeautifulSoup,
) -> Optional[str]:

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


# ============================================================
# Price
# ============================================================

def extract_price_from_blocks(
    blocks: List[str],
) -> Tuple[Optional[int], Optional[str]]:

    for block in blocks:
        if not any(
            keyword in block
            for keyword in ["販売価格", "価格", "販売"]
        ):
            continue

        price = parse_price(block)

        if price is not None:
            return price, block

    return None, None


# ============================================================
# Area
# ============================================================

def extract_area_from_page(
    page_text: str,
    label: str,
) -> Tuple[Optional[float], Optional[str]]:

    pattern = (
        rf"{re.escape(label)}"
        r"\s*"
        r"([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:m\s*[²2]|㎡)"
    )

    match = re.search(
        pattern,
        page_text,
        re.IGNORECASE,
    )

    if not match:
        return None, None

    value = float(match.group(1))

    start = max(0, match.start())
    end = min(len(page_text), match.end() + 40)

    return value, clean_text(page_text[start:end])


def extract_land_area(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Tuple[Optional[float], Optional[str]]:

    value = find_value_by_keywords(
        pairs,
        ["土地面積", "敷地面積"],
    )

    if value:
        parsed = parse_area_m2(value)

        if parsed is not None:
            return parsed, value

    parsed, text = extract_area_from_page(
        page_text,
        "土地面積",
    )

    if parsed is not None:
        return parsed, text

    for block in blocks:
        if "土地面積" not in block and "敷地面積" not in block:
            continue

        parsed = parse_area_m2(block)

        if parsed is not None:
            return parsed, block

    return None, None


def extract_building_area(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Tuple[Optional[float], Optional[str]]:

    value = find_value_by_keywords(
        pairs,
        ["建物面積", "延床面積", "建築面積"],
    )

    if value:
        parsed = parse_area_m2(value)

        if parsed is not None:
            return parsed, value

    parsed, text = extract_area_from_page(
        page_text,
        "建物面積",
    )

    if parsed is not None:
        return parsed, text

    for block in blocks:
        if "建物面積" not in block:
            continue

        after_label = block.split("建物面積", 1)[1]
        parsed = parse_area_m2(after_label)

        if parsed is not None:
            return parsed, block

    return None, None


# ============================================================
# Address
# ============================================================

def is_company_address(
    address: str,
) -> bool:

    if not address:
        return True

    company_words = [
        "ビル",
        "不動産",
        "ハウス",
        "ホーム",
        "株式会社",
        "有限会社",
        "支店",
        "営業所",
        "店舗",
        "センター",
        "アスライク",
    ]

    if any(word in address for word in company_words):
        return True

    if "〒" in address:
        return True

    return False


def normalize_address(
    value: Any,
) -> Optional[str]:

    address = clean_suumo_value(value)

    if not address:
        return None

    address = re.sub(
        r"\s*地図を見る.*$",
        "",
        address,
    )

    address = re.sub(
        r"\s*\[.*?$",
        "",
        address,
    )

    address = address.strip()

    if is_company_address(address):
        return None

    if not re.search(
        r"(東京都|千葉県|埼玉県|神奈川県|茨城県)",
        address,
    ):
        return None

    return address


def extract_address(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Optional[str]:

    # 1. 「所在地」の値を優先
    for label, value in pairs.items():
        if "所在地" not in label:
            continue

        address = normalize_address(value)

        if address:
            return address

    # 2. 物件概要の所在地表記を優先
    patterns = [
        r"(?:所在地|物件所在地)\s*"
        r"((?:千葉県|東京都|埼玉県|神奈川県|茨城県)[^。]{2,80})",

        r"((?:千葉県|東京都|埼玉県|神奈川県|茨城県)"
        r"[^。]{2,80})\s*地図を見る",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text)

        if not match:
            continue

        address = normalize_address(match.group(1))

        if address:
            return address

    # 3. ブロックから抽出
    for block in blocks:
        if "会社情報" in block:
            continue

        if "取り扱い店舗" in block:
            continue

        match = re.search(
            r"((?:千葉県|東京都|埼玉県|神奈川県|茨城県)[^。]{2,80})",
            block,
        )

        if not match:
            continue

        address = normalize_address(match.group(1))

        if address:
            return address

    return None


# ============================================================
# Layout
# ============================================================

def extract_layout(
    pairs: Dict[str, str],
    blocks: List[str],
) -> Optional[str]:

    layout = find_value_by_keywords(
        pairs,
        ["間取り"],
    )

    if layout:
        match = re.search(
            r"\d+\s*(?:LDK|DK|K|LK)",
            layout,
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(0))

    for block in blocks:
        match = re.search(
            r"\b\d+\s*(?:LDK|DK|LK|K)"
            r"(?:\s*\+\s*S)?",
            block,
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(0))

    return None


# ============================================================
# Construction
# ============================================================

def extract_construction(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Tuple[Optional[str], Optional[str]]:

    # 「完成時期（築年月）」などのラベルを最優先
    for label, value in pairs.items():
        if not any(
            keyword in label
            for keyword in [
                "完成時期",
                "築年月",
                "建築年月",
                "完成年月",
            ]
        ):
            continue

        parsed = parse_year_month(value)

        if parsed:
            return parsed, value

    # ページ本文の明示的な築年月を優先
    patterns = [
        r"(?:完成時期\s*\(築年月\)|完成時期|築年月|建築年月)"
        r"\s*[:：]?\s*"
        r"((?:19|20)\d{2}年\d{1,2}月)",

        r"(?:完成時期\s*\(築年月\)|完成時期|築年月|建築年月)"
        r"[^0-9]{0,20}"
        r"((?:19|20)\d{2})年\s*(\d{1,2})月",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text)

        if not match:
            continue

        if match.lastindex and match.lastindex >= 2:
            value = f"{match.group(1)}年{match.group(2)}月"
        else:
            value = match.group(1)

        parsed = parse_year_month(value)

        if parsed:
            return parsed, value

    # ブロック内の築年月
    for block in blocks:
        if not any(
            keyword in block
            for keyword in ["築年月", "完成時期", "建築年月"]
        ):
            continue

        parsed = parse_year_month(block)

        if parsed:
            return parsed, block

    return None, None


# ============================================================
# Station / transportation
# ============================================================

def extract_station_info(
    blocks: List[str],
    page_text: str,
) -> Dict[str, Any]:

    result = {
        "station": None,
        "stationWalkMinutes": None,
        "walkMinutes": None,
        "transportRaw": None,
        "stationAccessType": None,
        "busMinutes": None,
        "busStop": None,
        "busStopWalkMinutes": None,
    }

    # 物件概要に記載された交通情報を優先する。
    transport_patterns = [
        r"(つくばエクスプレス|ＪＲ常磐線|JR常磐線|"
        r"東武野田線|東武アーバンパークライン|"
        r"京成松戸線|新京成線)"
        r"\s*[「『]?([^」』\s]+)[」』]?"
        r"\s*(?:駅)?"
        r"\s*(?:徒歩|歩)\s*(\d+)\s*分",

        r"(つくばエクスプレス|ＪＲ常磐線|JR常磐線|"
        r"東武野田線|東武アーバンパークライン|"
        r"京成松戸線|新京成線)"
        r"\s*[「『]?([^」』\s]+)[」』]?"
        r"\s*(?:駅)?"
        r"\s*"
        r"(?:バス\s*(\d+)\s*分)?"
        r"[^。]{0,30}?"
        r"(?:徒歩|歩)\s*(\d+)\s*分",
    ]

    # 物件の交通欄に限定するため、長大な会社情報ブロックを除外
    sources = []

    for block in blocks:
        if any(
            keyword in block
            for keyword in [
                "会社情報",
                "取り扱い店舗",
                "店舗情報",
                "免許番号",
            ]
        ):
            continue

        if any(
            keyword in block
            for keyword in [
                "交通",
                "アクセス",
                "バス",
                "歩",
                "徒歩",
            ]
        ):
            sources.append(block)

    sources.append(page_text)

    for source in sources:
        if not source:
            continue

        for pattern in transport_patterns:
            match = re.search(pattern, source)

            if not match:
                continue

            groups = match.groups()

            if len(groups) == 3:
                line_name, station, walk = groups
                bus_minutes = None
                walk_minutes = int(walk)
            else:
                line_name, station, bus, walk = groups
                bus_minutes = int(bus) if bus else None
                walk_minutes = int(walk)

            if station in ["徒", "歩", "分", "バス"]:
                continue

            result["station"] = clean_text(station)
            result["stationWalkMinutes"] = (
                walk_minutes if bus_minutes is None else None
            )
            result["walkMinutes"] = (
                walk_minutes if bus_minutes is None else None
            )
            result["transportRaw"] = clean_text(match.group(0))
            result["stationAccessType"] = (
                "bus" if bus_minutes is not None else "walk"
            )
            result["busMinutes"] = bus_minutes

            return result

    # 「柏の葉キャンパス」バス8分柏ビレジ第二歩10分
    bus_pattern = re.search(
        r"[「『]([^」』]+)[」』]"
        r"\s*バス\s*(\d+)\s*分"
        r"\s*([^。]{1,30}?)"
        r"\s*(?:徒歩|歩)\s*(\d+)\s*分",
        page_text,
    )

    if bus_pattern:
        station = clean_text(bus_pattern.group(1))
        bus_minutes = int(bus_pattern.group(2))
        stop = clean_text(bus_pattern.group(3))
        stop_walk = int(bus_pattern.group(4))

        result.update({
            "station": station,
            "stationAccessType": "bus",
            "busMinutes": bus_minutes,
            "busStop": stop,
            "busStopWalkMinutes": stop_walk,
            "walkMinutes": None,
            "stationWalkMinutes": None,
            "transportRaw": clean_text(
                bus_pattern.group(0)
            ),
        })

        return result

    return result


# ============================================================
# Information dates
# ============================================================

def extract_information_dates(
    page_text: str,
) -> Dict[str, Optional[str]]:

    result = {
        "informationDate": None,
        "nextUpdateDate": None,
    }

    patterns = {
        "informationDate": r"情報提供日\s*[:：]?\s*"
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",

        "nextUpdateDate": r"次回更新予定日\s*[:：]?\s*"
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, page_text)

        if not match:
            continue

        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))

        try:
            date = datetime(year, month, day)
            result[field] = date.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return result


# ============================================================
# Quality evaluation
# ============================================================

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

    address = detail.get("address")

    if address and is_company_address(str(address)):
        warnings.append(
            "会社・店舗住所の可能性がある"
        )

    construction_month = detail.get(
        "constructionMonth"
    )

    if construction_month:
        fetched_at = detail.get("fetchedAt", "")

        if fetched_at.startswith(construction_month):
            warnings.append(
                "築年月が取得年月と一致しており誤抽出の可能性がある"
            )

    station = detail.get("station")

    if station in ["徒", "歩", "分", "バス"]:
        warnings.append(
            "駅名が不正なUI文字列である"
        )

    if not station:
        warnings.append(
            "駅情報を抽出できない"
        )

    if (
        detail.get("stationAccessType") == "walk"
        and detail.get("walkMinutes") is None
    ):
        warnings.append(
            "徒歩分数を抽出できない"
        )

    if (
        detail.get("informationDate") is None
        and "情報提供日" in " ".join(
            detail.get("textBlocks", [])
        )
    ):
        warnings.append(
            "情報提供日が本文に存在するが抽出できない"
        )

    if (
        detail.get("nextUpdateDate") is None
        and "次回更新予定日" in " ".join(
            detail.get("textBlocks", [])
        )
    ):
        warnings.append(
            "次回更新予定日が本文に存在するが抽出できない"
        )

    total_fields = len(required_fields)
    valid_fields = total_fields - len(missing_fields)

    score = round(
        valid_fields / total_fields * 100
    )

    # 重大な警告がある場合はgoodにしない
    serious_warning_words = [
        "会社・店舗住所",
        "築年月が取得年月",
        "駅名が不正",
        "駅情報を抽出できない",
        "徒歩分数を抽出できない",
    ]

    has_serious_warning = any(
        any(word in warning for word in serious_warning_words)
        for warning in warnings
    )

    if score >= 80 and not warnings and not has_serious_warning:
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


# ============================================================
# Detail fetch
# ============================================================

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
            "Mozilla/5.0 "
            "(iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
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

    page_text = (
        clean_text(
            soup.get_text(" ", strip=True)
        )
        or ""
    )

    fetched_at = now_iso()

    title = extract_title(soup)

    price, price_text = extract_price_from_blocks(blocks)

    if price is None:
        value = find_value_by_keywords(
            pairs,
            ["販売価格", "価格"],
        )

        if value:
            price = parse_price(value)
            price_text = value

    address = extract_address(
        pairs,
        blocks,
        page_text,
    )

    land_area, land_area_text = extract_land_area(
        pairs,
        blocks,
        page_text,
    )

    building_area, building_area_text = extract_building_area(
        pairs,
        blocks,
        page_text,
    )

    layout = extract_layout(
        pairs,
        blocks,
    )

    construction_month, construction_text = extract_construction(
        pairs,
        blocks,
        page_text,
    )

    station_info = extract_station_info(
        blocks,
        page_text,
    )

    information_dates = extract_information_dates(
        page_text,
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
        "buildingAreaText": clean_text(building_area_text),

        "layout": layout,

        "constructionMonth": construction_month,
        "constructionText": clean_text(construction_text),

        "station": station_info.get("station"),
        "stationWalkMinutes": station_info.get(
            "stationWalkMinutes"
        ),
        "walkMinutes": station_info.get("walkMinutes"),
        "transportRaw": station_info.get("transportRaw"),
        "stationAccessType": station_info.get(
            "stationAccessType"
        ),
        "busMinutes": station_info.get("busMinutes"),
        "busStop": station_info.get("busStop"),
        "busStopWalkMinutes": station_info.get(
            "busStopWalkMinutes"
        ),

        "informationDate": information_dates.get(
            "informationDate"
        ),
        "nextUpdateDate": information_dates.get(
            "nextUpdateDate"
        ),

        "labelValuePairs": pairs,
        "textBlocks": blocks,

        "fetchedAt": fetched_at,
        "sourceUrl": url,
    }

    detail.update(
        evaluate_detail_quality(detail)
    )

    return {
        "success": True,
        "fetchedAt": fetched_at,
        "detail": detail,
        "error": None,
    }


# ============================================================
# Adapter
# ============================================================

class SuumoDetailAdapter:

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        root_path: Optional[str] = None,
        **kwargs,
    ):
        self.config = config or {}
        self.root_path = root_path
        self.extra_kwargs = kwargs

        self.interval_seconds = float(
            self.config.get(
                "detailRequestIntervalSeconds",
                1.5,
            )
        )

    def wait(self):
        time.sleep(self.interval_seconds)

    def fetch_detail(
        self,
        url: str,
    ) -> Dict[str, Any]:
        return fetch_detail(url)
