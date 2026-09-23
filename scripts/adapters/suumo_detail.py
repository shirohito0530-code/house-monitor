import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# Parser version
# ============================================================

DETAIL_PARSER_VERSION = "2026-09-23-v18-detail-deep-audit"


# ============================================================
# Constants
# ============================================================

SUUMO_HOSTS = {
    "suumo.jp",
    "www.suumo.jp",
}

SUUMO_LISTING_PREFIXES = (
    "/chukoikkodate/",
    "/ikkodate/",
)

SUUMO_LISTING_PATH_PATTERN = re.compile(
    r"^/(?:chukoikkodate|ikkodate)/.+/nc_\d+(?:/)?$",
    re.IGNORECASE,
)

INVALID_VALUES = {
    "",
    "-",
    "ー",
    "－",
    "―",
    "なし",
    "ヒント",
    "詳細を見る",
    "地図を見る",
    "周辺環境",
    "支払シミュレーション",
    "お問い合わせ",
    "資料請求",
    "確認",
}

PROMOTIONAL_WORDS = [
    "ヒント",
    "詳細を見る",
    "地図を見る",
    "周辺環境",
    "支払シミュレーション",
    "お問い合わせ",
    "資料請求",
    "お待ち合わせ",
    "ご来社",
    "お気軽に",
    "クリック",
    "ご見学",
    "ご提案",
    "お申し付け",
    "即案内",
]

PREFECTURES_PATTERN = (
    r"(?:北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|"
    r"茨城県|栃木県|群馬県|埼玉県|千葉県|東京都|神奈川県|"
    r"新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|"
    r"静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|"
    r"奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|"
    r"徳島県|香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|"
    r"熊本県|大分県|宮崎県|鹿児島県|沖縄県)"
)

COMPANY_WORDS = [
    "不動産",
    "株式会社",
    "有限会社",
    "合同会社",
    "支店",
    "営業所",
    "店舗",
    "センター",
    "担当者",
    "取扱",
    "免許番号",
    "宅建",
    "ハウス",
    "ホーム",
    "リアルティ",
    "住まい",
    "ショールーム",
    "コーポレーション",
]

LOAN_EXCLUSION_WORDS = [
    "月々",
    "支払",
    "頭金",
    "ローン",
    "返済",
    "目安",
    "シミュレーション",
    "ボーナス",
    "金利",
]

PROPERTY_TYPE_KEYWORDS = {
    "新築戸建": [
        "新築",
        "新築一戸建て",
        "新築一戸建",
        "新築戸建",
    ],
    "中古戸建": [
        "中古",
        "中古一戸建て",
        "中古一戸建",
        "中古戸建",
    ],
}

FIELD_LABELS = {
    "price": [
        "販売価格",
        "価格",
        "販売価格（税込）",
        "販売価格(税込)",
        "総額",
    ],
    "address": [
        "物件所在地",
        "所在地",
        "住所",
    ],
    "landAreaM2": [
        "土地面積",
        "敷地面積",
    ],
    "buildingAreaM2": [
        "延床面積",
        "延べ床面積",
        "延べ面積",
        "建物面積",
    ],
    "buildingFootprintAreaM2": [
        "建築面積",
    ],
    "layout": [
        "間取り",
    ],
    "constructionMonth": [
        "完成時期",
        "築年月",
        "建築年月",
        "完成年月",
        "築年",
    ],
    "propertyType": [
        "物件種別",
        "物件タイプ",
        "建物種別",
    ],
}


# ============================================================
# Basic utilities
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def normalize_label(value: Any) -> str:
    text = clean_text(value) or ""

    text = (
        text.replace("：", "")
        .replace(":", "")
        .replace("　", "")
        .replace(" ", "")
        .replace("\n", "")
    )

    return text.lower()


def is_promotional_text(value: Any) -> bool:
    text = clean_text(value)

    if not text:
        return True

    return any(word in text for word in PROMOTIONAL_WORDS)


def clean_suumo_value(value: Any) -> Optional[str]:
    text = clean_text(value)

    if not text:
        return None

    text = re.sub(
        r"\s*(地図を見る|周辺環境|詳細を見る|お気に入り).*$",
        "",
        text,
    )

    text = re.sub(
        r"\s*[\[［].*?[\]］]",
        "",
        text,
    )

    text = re.sub(
        r"\s*【[^】]+】",
        "",
        text,
    )

    text = text.strip()

    if text in INVALID_VALUES:
        return None

    if is_promotional_text(text):
        return None

    return text


def create_candidate(
    field: str,
    value: Any,
    raw: str,
    source: str,
    confidence: float,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    return {
        "field": field,
        "value": value,
        "raw": raw,
        "source": source,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "metadata": metadata or {},
    }


# ============================================================
# URL handling
# ============================================================

def _prepare_suumo_url(url: Any) -> Optional[str]:

    if not url:
        return None

    text = str(url).strip()

    if not text:
        return None

    if text.startswith("//"):
        text = "https:" + text

    elif text.startswith("/"):
        text = "https://www.suumo.jp" + text

    elif text.startswith("www.suumo.jp/"):
        text = "https://" + text

    elif text.startswith("suumo.jp/"):
        text = "https://" + text

    return text


def _parse_and_validate_suumo_url(url: Any) -> Optional[Any]:

    prepared = _prepare_suumo_url(url)

    if not prepared:
        return None

    try:
        parsed = urlparse(prepared)
    except Exception:
        return None

    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()

    if hostname not in SUUMO_HOSTS:
        return None

    if scheme not in {"http", "https"}:
        return None

    path = parsed.path or ""

    if not path:
        return None

    if not SUUMO_LISTING_PATH_PATTERN.match(path):
        return None

    return parsed


def normalize_suumo_url(url: Any) -> Optional[str]:

    parsed = _parse_and_validate_suumo_url(url)

    if parsed is None:
        return None

    path = parsed.path or ""
    if not path.endswith("/"):
        path += "/"

    return urlunparse(
        (
            "https",
            "suumo.jp",
            path,
            "",
            "",
            "",
        )
    )


def preserve_suumo_listing_url(url: Any) -> Optional[str]:

    parsed = _parse_and_validate_suumo_url(url)

    if parsed is None:
        return None

    path = parsed.path or ""
    if not path.endswith("/"):
        path += "/"

    return urlunparse(
        (
            "https",
            "suumo.jp",
            path,
            "",
            "",
            "",
        )
    )


def is_valid_suumo_url(url: str) -> bool:
    return _parse_and_validate_suumo_url(url) is not None


# ============================================================
# Value parsers
# ============================================================

def parse_price(value: Any) -> Optional[int]:

    text = clean_text(value)

    if not text:
        return None

    text = (
        text.replace(",", "")
        .replace(" ", "")
        .replace("　", "")
    )

    # 例: 1億2,800万円
    match = re.search(
        r"(?:(\d+(?:\.\d+)?)\s*億)"
        r"(?:\s*(\d+(?:\.\d+)?)\s*万)?"
        r"(?:円)?",
        text,
    )

    if match:

        oku = float(match.group(1))
        man = float(match.group(2) or 0)

        price = int(
            round(
                oku * 100_000_000
                + man * 10_000
            )
        )

        return price if price > 0 else None

    # 例: 8,500万円
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*万(?:円)?",
        text,
    )

    if match:

        price = int(
            round(
                float(match.group(1))
                * 10_000
            )
        )

        return price if price > 0 else None

    # 例: 85,000,000円
    match = re.search(
        r"(\d[\d\s]*)\s*円",
        text,
    )

    if match:

        try:

            price = int(
                match.group(1)
                .replace(" ", "")
            )

            return price if price > 0 else None

        except ValueError:
            return None

    return None


def parse_area_m2(value: Any) -> Optional[float]:

    text = clean_text(value)

    if not text:
        return None

    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:m\s*[²2]?|㎡)",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    try:

        val = float(match.group(1))

        return val if val > 0 else None

    except ValueError:
        return None


def parse_year_month(
    value: Any,
) -> Tuple[Optional[str], Optional[str]]:

    text = clean_text(value)

    if not text:
        return None, None

    patterns = [
        r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月",
        r"((?:19|20)\d{2})\s*[/-]\s*(\d{1,2})",
    ]

    for pattern in patterns:

        match = re.search(pattern, text)

        if match:

            year = int(match.group(1))
            month = int(match.group(2))

            if 1 <= month <= 12:

                return (
                    f"{year:04d}-{month:02d}",
                    "month",
                )

    match = re.search(
        r"((?:19|20)\d{2})\s*年",
        text,
    )

    if match:

        return (
            f"{int(match.group(1)):04d}-01",
            "year",
        )

    era_patterns = [
        (
            r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月",
            2018,
        ),
        (
            r"平成\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月",
            1988,
        ),
        (
            r"昭和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月",
            1925,
        ),
    ]

    for pattern, base_year in era_patterns:

        match = re.search(pattern, text)

        if match:

            year = base_year + int(match.group(1))
            month = int(match.group(2))

            if 1 <= month <= 12:

                return (
                    f"{year:04d}-{month:02d}",
                    "month",
                )

    return None, None


def parse_construction_date(
    construction_month: Optional[str],
) -> Tuple[Optional[int], Optional[int]]:

    if not construction_month:
        return None, None

    match = re.fullmatch(
        r"(\d{4})-(\d{2})",
        construction_month,
    )

    if not match:
        return None, None

    year = int(match.group(1))
    month = int(match.group(2))

    if not (
        1900 <= year <= datetime.now().year + 2
    ):
        return None, None

    if not 1 <= month <= 12:
        return None, None

    return year, month


def calculate_construction_age_years(
    construction_month: Optional[str],
    reference_date: Optional[datetime] = None,
) -> Optional[float]:

    if not construction_month:
        return None

    match = re.fullmatch(
        r"(\d{4})-(\d{2})",
        construction_month,
    )

    if not match:
        return None

    year = int(match.group(1))
    month = int(match.group(2))

    ref = (
        reference_date
        or datetime.now(timezone.utc)
    )

    months = (
        (ref.year - year) * 12
        + ref.month
        - month
    )

    if months < 0:
        return None

    return round(months / 12, 2)


# ============================================================
# DOM preprocessing
# ============================================================

def remove_unwanted_sections(
    soup: BeautifulSoup,
) -> BeautifulSoup:

    soup_copy = BeautifulSoup(
        str(soup),
        "html.parser",
    )

    selectors_to_remove = [
        "script",
        "style",
        "noscript",
        "iframe",
        "svg",
        ".cassette_shop",
        "#js-shopInfo",
        "#js-inquiryForm",
        ".ar-shop",
        ".ar-company",
        ".shop",
        ".company",
        ".inquiry",
        ".js-shopInfo",
    ]

    for selector in selectors_to_remove:

        for element in soup_copy.select(selector):

            try:
                element.decompose()
            except Exception:
                pass

    return soup_copy


# ============================================================
# Structured DOM extraction
# ============================================================

def _parent_is_excluded(element: Any) -> bool:

    for parent in element.parents:

        if not getattr(parent, "name", None):
            continue

        p_class = " ".join(
            parent.get("class", [])
        )

        p_id = parent.get("id", "")

        combined = (
            f"{p_class} {p_id}"
        ).lower()

        if any(
            k in combined
            for k in [
                "shop",
                "company",
                "tenpo",
                "store",
                "inquiry",
                "cassette_shop",
            ]
        ):
            return True

    return False


def collect_label_value_pairs(
    soup: BeautifulSoup,
) -> List[Dict[str, Any]]:

    pairs: List[Dict[str, Any]] = []

    # --------------------------------------------------------
    # table / tr / th-td
    # --------------------------------------------------------

    for tr in soup.find_all("tr"):

        if _parent_is_excluded(tr):
            continue

        cells = tr.find_all(
            ["th", "td"],
            recursive=False,
        )

        if len(cells) < 2:

            cells = tr.find_all(
                ["th", "td"]
            )

        cell_data = []

        for cell in cells:

            txt = clean_text(
                cell.get_text(
                    " ",
                    strip=True,
                )
            )

            if txt:

                cell_data.append(
                    {
                        "tag": cell.name.lower(),
                        "text": txt,
                    }
                )

        i = 0

        while i < len(cell_data) - 1:

            curr = cell_data[i]
            nxt = cell_data[i + 1]

            if (
                curr["tag"] == "th"
                and nxt["tag"] == "td"
            ):

                label = curr["text"]
                value = clean_suumo_value(
                    nxt["text"]
                )

                if label and value:

                    pairs.append(
                        {
                            "label": label,
                            "value": value,
                            "raw_label": label,
                            "raw_value": nxt["text"],
                            "source": "table_th_td",
                        }
                    )

                i += 2

            else:
                i += 1

    # --------------------------------------------------------
    # dl / dt-dd
    # --------------------------------------------------------

    for dl in soup.find_all("dl"):

        if _parent_is_excluded(dl):
            continue

        dts = dl.find_all(
            "dt",
            recursive=False,
        )

        for dt in dts:

            dd = dt.find_next_sibling("dd")

            if not dd:
                continue

            label = clean_text(
                dt.get_text(
                    " ",
                    strip=True,
                )
            )

            value = clean_suumo_value(
                dd.get_text(
                    " ",
                    strip=True,
                )
            )

            if label and value:

                pairs.append(
                    {
                        "label": label,
                        "value": value,
                        "raw_label": label,
                        "raw_value": value,
                        "source": "dl_dt_dd",
                    }
                )

    return pairs


def extract_json_ld(
    soup: BeautifulSoup,
) -> List[Dict[str, Any]]:

    results = []

    for script in soup.find_all(
        "script",
        attrs={
            "type": re.compile(
                r"application/ld\+json",
                re.I,
            )
        },
    ):

        text = script.string or script.get_text()

        if not text:
            continue

        try:

            data = json.loads(text)

            if isinstance(data, list):

                results.extend(
                    x for x in data
                    if isinstance(x, dict)
                )

            elif isinstance(data, dict):

                results.append(data)

        except Exception:
            continue

    return results


def extract_meta_content(
    soup: BeautifulSoup,
) -> Dict[str, str]:

    result = {}

    for meta in soup.find_all("meta"):

        name = (
            meta.get("name")
            or meta.get("property")
            or meta.get("itemprop")
        )

        content = meta.get("content")

        if name and content:

            result[
                str(name).lower()
            ] = clean_text(content) or ""

    return result


def extract_text_blocks(
    soup: BeautifulSoup,
) -> List[str]:

    blocks: List[str] = []

    selectors = [
        "h1",
        "h2",
        "h3",
        "h4",
        "p",
        "li",
        "td",
        "th",
        "dt",
        "dd",
        "div",
        "span",
    ]

    for selector in selectors:

        for element in soup.select(selector):

            if _parent_is_excluded(element):
                continue

            text = clean_text(
                element.get_text(
                    " ",
                    strip=True,
                )
            )

            if text and len(text) <= 1500:

                blocks.append(text)

    result = []
    seen = set()

    for block in blocks:

        if block not in seen:

            seen.add(block)
            result.append(block)

    return result


# ============================================================
# Candidate selection
# ============================================================

def _value_key(value: Any) -> str:

    if value is None:
        return ""

    if isinstance(value, dict):
        if "station" in value and value["station"] is not None:
            return str(value["station"]).strip()
        return str(value).strip()

    if isinstance(value, float):
        return f"{value:.6f}"

    return str(value).strip()


def select_best_candidate(
    candidates: List[Dict[str, Any]],
) -> Tuple[
    Optional[Any],
    Optional[Dict[str, Any]]
]:

    if not candidates:
        return None, None

    # --------------------------------------------------------
    # 同一値の出現数を信頼度に反映
    # --------------------------------------------------------

    value_counts = Counter(
        _value_key(c["value"])
        for c in candidates
        if c.get("value") is not None
    )

    scored = []

    for candidate in candidates:

        value = candidate.get("value")

        if value is None:
            continue

        count = value_counts[
            _value_key(value)
        ]

        score = float(
            candidate.get(
                "confidence",
                0,
            )
        )

        # 複数箇所一致
        if count >= 2:
            score += 0.08

        if count >= 3:
            score += 0.04

        # structured source bonus
        source = candidate.get(
            "source",
            "",
        )

        if source in {
            "table_th_td_exact",
            "table_th_td",
            "table_th_td_gross",
            "dl_dt_dd",
            "json_ld",
        }:
            score += 0.03

        candidate_copy = dict(candidate)
        candidate_copy["selectionScore"] = round(
            min(1.0, score),
            3,
        )

        scored.append(candidate_copy)

    if not scored:
        return None, None

    scored.sort(
        key=lambda x: (
            x["selectionScore"],
            value_counts[
                _value_key(x["value"])
            ],
        ),
        reverse=True,
    )

    best = scored[0]

    return (
        best["value"],
        best,
    )


def build_audit_entry(
    candidates: List[Dict[str, Any]],
    selected_val: Any,
) -> Dict[str, Any]:

    if selected_val is not None:

        selected_candidates = [
            c for c in candidates
            if _value_key(c.get("value"))
            == _value_key(selected_val)
        ]

        selected_cand = (
            max(
                selected_candidates,
                key=lambda x: x.get(
                    "selectionScore",
                    x.get(
                        "confidence",
                        0,
                    ),
                ),
            )
            if selected_candidates
            else None
        )

        return {
            "status": "found",
            "selected_method": (
                selected_cand["source"]
                if selected_cand
                else "unknown"
            ),
            "selected_confidence": (
                selected_cand.get(
                    "selectionScore",
                    selected_cand.get(
                        "confidence",
                        0,
                    ),
                )
                if selected_cand
                else None
            ),
            "candidate_count": len(candidates),
            "candidate_sources": sorted(
                set(
                    c["source"]
                    for c in candidates
                )
            ),
            "candidates": candidates,
        }

    return {
        "status": "missing",
        "methods_tried": sorted(
            set(
                c["source"]
                for c in candidates
            )
        )
        if candidates
        else ["all_sources"],
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


# ============================================================
# Price
# ============================================================

def collect_price_candidates(
    pairs: List[Dict[str, Any]],
    blocks: List[str],
    page_text: str,
    json_ld: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:

    candidates = []

    # 1. Structured table / dl
    for pair in pairs:

        label = normalize_label(
            pair["label"]
        )

        if any(
            normalize_label(k) in label
            for k in FIELD_LABELS["price"]
        ):

            if any(
                ex in pair["value"]
                for ex in LOAN_EXCLUSION_WORDS
            ):
                continue

            price = parse_price(
                pair["value"]
            )

            if price:

                confidence = (
                    0.98
                    if "販売価格"
                    in pair["label"]
                    else 0.93
                )

                candidates.append(
                    create_candidate(
                        "price",
                        price,
                        pair["value"],
                        pair["source"],
                        confidence,
                    )
                )

    # 2. JSON-LD
    for obj in json_ld or []:

        for key in [
            "price",
            "lowPrice",
        ]:

            if key in obj:

                price = parse_price(
                    obj[key]
                )

                if price:

                    candidates.append(
                        create_candidate(
                            "price",
                            price,
                            str(obj[key]),
                            "json_ld",
                            0.94,
                        )
                    )

    # 3. Text blocks
    for block in blocks:

        if not any(
            k in block
            for k in [
                "販売価格",
                "価格",
            ]
        ):
            continue

        if any(
            ex in block
            for ex in LOAN_EXCLUSION_WORDS
        ):
            continue

        price = parse_price(block)

        if price:

            candidates.append(
                create_candidate(
                    "price",
                    price,
                    block,
                    "text_block",
                    0.78,
                )
            )

    # 4. Page regex
    patterns = [
        r"(?:販売価格)\s*[:：]?\s*([0-9\.,]+(?:\s*億|\s*万|\s*円)+)",
        r"(?:価格)\s*[:：]?\s*([0-9\.,]+(?:\s*億|\s*万|\s*円)+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
        )

        if match:

            raw = match.group(0)

            if any(
                ex in raw
                for ex in LOAN_EXCLUSION_WORDS
            ):
                continue

            price = parse_price(raw)

            if price:

                candidates.append(
                    create_candidate(
                        "price",
                        price,
                        raw,
                        "page_regex",
                        0.72,
                    )
                )

    return candidates


# ============================================================
# Address
# ============================================================

def is_company_address(
    address: str,
) -> bool:

    if not address:
        return True

    return any(
        word in address
        for word in COMPANY_WORDS
    )


def normalize_address(
    value: Any,
) -> Optional[str]:

    address = clean_suumo_value(value)

    if not address:
        return None

    address = re.sub(
        r"\s*(地図を見る|周辺環境|詳細を見る|お気に入り).*$",
        "",
        address,
    ).strip()

    address = re.sub(
        r"〒\s*\d{3}-?\d{4}\s*",
        "",
        address,
    ).strip()

    if is_company_address(address):
        return None

    if not re.search(
        PREFECTURES_PATTERN,
        address,
    ):
        return None

    return address


def collect_address_candidates(
    pairs: List[Dict[str, Any]],
    blocks: List[str],
    page_text: str,
    json_ld: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:

    candidates = []

    # Structured
    for pair in pairs:

        label = pair["label"]

        if "物件所在地" in label:

            addr = normalize_address(
                pair["value"]
            )

            if addr:

                candidates.append(
                    create_candidate(
                        "address",
                        addr,
                        pair["value"],
                        "table_th_td_exact",
                        0.99,
                    )
                )

        elif (
            "所在地" in label
            and not any(
                ex in label
                for ex in [
                    "店舗",
                    "会社",
                    "取扱",
                    "販売",
                ]
            )
        ):

            addr = normalize_address(
                pair["value"]
            )

            if addr:

                candidates.append(
                    create_candidate(
                        "address",
                        addr,
                        pair["value"],
                        pair["source"],
                        0.91,
                    )
                )

    # JSON-LD
    for obj in json_ld or []:

        address_obj = obj.get(
            "address"
        )

        if isinstance(
            address_obj,
            dict,
        ):

            parts = [
                address_obj.get(
                    "addressRegion"
                ),
                address_obj.get(
                    "addressLocality"
                ),
                address_obj.get(
                    "streetAddress"
                ),
            ]

            addr = clean_text(
                "".join(
                    x or ""
                    for x in parts
                )
            )

            addr = normalize_address(addr)

            if addr:

                candidates.append(
                    create_candidate(
                        "address",
                        addr,
                        str(address_obj),
                        "json_ld",
                        0.90,
                    )
                )

    # Page regex
    match = re.search(
        rf"(?:物件所在地|所在地|住所)"
        rf"\s*[:：]?\s*"
        rf"({PREFECTURES_PATTERN}"
        rf"[^。\[［\]\n]{{2,120}})",
        page_text,
    )

    if match:

        addr = normalize_address(
            match.group(1)
        )

        if addr:

            candidates.append(
                create_candidate(
                    "address",
                    addr,
                    match.group(0),
                    "page_regex",
                    0.80,
                )
            )

    # Blocks
    for block in blocks:

        if any(
            ex in block
            for ex in [
                "会社情報",
                "店舗情報",
                "加盟",
                "免許番号",
                "取扱店",
            ]
        ):
            continue

        match = re.search(
            rf"({PREFECTURES_PATTERN}"
            rf"[^。\[［\]\n]{{2,120}})",
            block,
        )

        if match:

            addr = normalize_address(
                match.group(1)
            )

            if addr:

                candidates.append(
                    create_candidate(
                        "address",
                        addr,
                        block,
                        "text_block",
                        0.68,
                    )
                )

    return candidates


# ============================================================
# Land area
# ============================================================

def collect_land_area_candidates(
    pairs: List[Dict[str, Any]],
    blocks: List[str],
    page_text: str,
) -> List[Dict[str, Any]]:

    candidates = []

    for pair in pairs:

        label = pair["label"]

        if any(
            k in label
            for k in FIELD_LABELS["landAreaM2"]
        ):

            val = parse_area_m2(
                pair["value"]
            )

            if val:

                confidence = (
                    0.97
                    if "土地面積"
                    in label
                    else 0.93
                )

                candidates.append(
                    create_candidate(
                        "landAreaM2",
                        val,
                        pair["value"],
                        pair["source"],
                        confidence,
                    )
                )

    match = re.search(
        r"(?:土地面積|敷地面積)"
        r"\s*[:：]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:m\s*[²2]?|㎡)",
        page_text,
        re.IGNORECASE,
    )

    if match:

        val = parse_area_m2(
            match.group(0)
        )

        if val:

            candidates.append(
                create_candidate(
                    "landAreaM2",
                    val,
                    match.group(0),
                    "page_regex",
                    0.82,
                )
            )

    for block in blocks:

        if (
            "土地面積" not in block
            and "敷地面積" not in block
        ):
            continue

        val = parse_area_m2(block)

        if val:

            candidates.append(
                create_candidate(
                    "landAreaM2",
                    val,
                    block,
                    "text_block",
                    0.72,
                )
            )

    return candidates


# ============================================================
# Building area
# ============================================================

def collect_building_area_candidates(
    pairs: List[Dict[str, Any]],
    blocks: List[str],
    page_text: str,
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:

    gross_candidates = []
    footprint_candidates = []

    for pair in pairs:

        label = pair["label"]
        val_str = pair["value"]

        # 建築面積 must be checked first
        if "建築面積" in label:

            val = parse_area_m2(
                val_str
            )

            if val:

                footprint_candidates.append(
                    create_candidate(
                        "buildingFootprintAreaM2",
                        val,
                        val_str,
                        pair["source"],
                        0.94,
                    )
                )

            continue

        if (
            "延床面積" in label
            or "延べ床面積" in label
            or "延べ面積" in label
        ):

            val = parse_area_m2(
                val_str
            )

            if val:

                gross_candidates.append(
                    create_candidate(
                        "buildingAreaM2",
                        val,
                        val_str,
                        "table_th_td_gross",
                        0.99,
                    )
                )

        elif "建物面積" in label:

            val = parse_area_m2(
                val_str
            )

            if val:

                gross_candidates.append(
                    create_candidate(
                        "buildingAreaM2",
                        val,
                        val_str,
                        "table_th_td_building",
                        0.94,
                    )
                )

    # Page regex
    gross_patterns = [
        r"延床面積\s*[:：]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:m\s*[²2]?|㎡)",

        r"延べ床面積\s*[:：]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:m\s*[²2]?|㎡)",

        r"建物面積\s*[:：]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:m\s*[²2]?|㎡)",
    ]

    for pattern in gross_patterns:

        match = re.search(
            pattern,
            page_text,
            re.IGNORECASE,
        )

        if match:

            val = parse_area_m2(
                match.group(0)
            )

            if val:

                gross_candidates.append(
                    create_candidate(
                        "buildingAreaM2",
                        val,
                        match.group(0),
                        "page_regex",
                        0.82,
                    )
                )

    match = re.search(
        r"建築面積\s*[:：]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:m\s*[²2]?|㎡)",
        page_text,
        re.IGNORECASE,
    )

    if match:

        val = parse_area_m2(
            match.group(0)
        )

        if val:

            footprint_candidates.append(
                create_candidate(
                    "buildingFootprintAreaM2",
                    val,
                    match.group(0),
                    "page_regex",
                    0.82,
                )
            )

    return (
        gross_candidates,
        footprint_candidates,
    )


# ============================================================
# Layout
# ============================================================

def normalize_layout(
    value: Any,
) -> Optional[str]:

    text = clean_text(value)

    if not text:
        return None

    pattern = (
        r"\d+\s*"
        r"(?:LDK|DK|LK|K)"
        r"(?:\s*[\+＋]\s*\d*S)?"
    )

    match = re.search(
        pattern,
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    return clean_text(
        match.group(0)
    )


def collect_layout_candidates(
    pairs: List[Dict[str, Any]],
    blocks: List[str],
) -> List[Dict[str, Any]]:

    candidates = []

    for pair in pairs:

        if "間取り" not in pair["label"]:
            continue

        layout = normalize_layout(
            pair["value"]
        )

        if layout:

            candidates.append(
                create_candidate(
                    "layout",
                    layout,
                    pair["value"],
                    pair["source"],
                    0.97,
                )
            )

    for block in blocks:

        if "間取り" not in block:
            continue

        layout = normalize_layout(
            block
        )

        if layout:

            candidates.append(
                create_candidate(
                    "layout",
                    layout,
                    block,
                    "text_block",
                    0.72,
                )
            )

    return candidates


# ============================================================
# Construction date
# ============================================================

def collect_construction_candidates(
    pairs: List[Dict[str, Any]],
    blocks: List[str],
    page_text: str,
) -> List[Dict[str, Any]]:

    candidates = []

    keywords = [
        "完成時期",
        "築年月",
        "建築年月",
        "完成年月",
        "築年",
    ]

    for pair in pairs:

        if not any(
            k in pair["label"]
            for k in keywords
        ):
            continue

        parsed, precision = parse_year_month(
            pair["value"]
        )

        if parsed:

            candidates.append(
                create_candidate(
                    "constructionMonth",
                    parsed,
                    pair["value"],
                    pair["source"],
                    0.97,
                    {
                        "precision": precision,
                    },
                )
            )

    patterns = [
        r"(?:完成時期\s*\(築年月\)|"
        r"完成時期|築年月|建築年月|完成年月)"
        r"\s*[:：]?\s*"
        r"((?:19|20)\d{2}"
        r"年\d{1,2}月)",

        r"(?:完成時期|築年月)"
        r"\s*[:：]?\s*"
        r"(令和\s*\d{1,2}年\s*\d{1,2}月)",

        r"(?:完成時期|築年月)"
        r"\s*[:：]?\s*"
        r"(平成\s*\d{1,2}年\s*\d{1,2}月)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
        )

        if not match:
            continue

        parsed, precision = parse_year_month(
            match.group(1)
        )

        if parsed:

            candidates.append(
                create_candidate(
                    "constructionMonth",
                    parsed,
                    match.group(0),
                    "page_regex",
                    0.84,
                    {
                        "precision": precision,
                    },
                )
            )

    return candidates


# ============================================================
# Property type
# ============================================================

def collect_property_type_candidates(
    pairs: List[Dict[str, Any]],
    blocks: List[str],
    title: Optional[str],
    page_text: str,
) -> List[Dict[str, Any]]:

    candidates = []

    def classify(text: str) -> Optional[str]:

        text = clean_text(text) or ""

        if any(
            keyword in text
            for keyword in PROPERTY_TYPE_KEYWORDS[
                "新築戸建"
            ]
        ):
            return "新築戸建"

        if any(
            keyword in text
            for keyword in PROPERTY_TYPE_KEYWORDS[
                "中古戸建"
            ]
        ):
            return "中古戸建"

        return None

    # Structured
    for pair in pairs:

        if any(
            k in pair["label"]
            for k in FIELD_LABELS["propertyType"]
        ):

            p_type = classify(
                pair["value"]
            )

            if p_type:

                candidates.append(
                    create_candidate(
                        "propertyType",
                        p_type,
                        pair["value"],
                        pair["source"],
                        0.98,
                    )
                )

    # Title
    if title:

        p_type = classify(title)

        if p_type:

            candidates.append(
                create_candidate(
                    "propertyType",
                    p_type,
                    title,
                    "title",
                    0.92,
                )
            )

    # Blocks
    for block in blocks:

        p_type = classify(block)

        if p_type:

            candidates.append(
                create_candidate(
                    "propertyType",
                    p_type,
                    block,
                    "text_block",
                    0.70,
                )
            )

    # Page text
    p_type = classify(page_text)

    if p_type:

        candidates.append(
            create_candidate(
                "propertyType",
                p_type,
                page_text[
                    :1000
                ],
                "page_text",
                0.60,
            )
        )

    return candidates


# ============================================================
# Station / transportation
# ============================================================

def extract_station_info(
    blocks: List[str],
    page_text: str,
) -> Tuple[
    Dict[str, Any],
    List[Dict[str, Any]],
]:

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

    candidates = []

    clean_blocks = [
        b
        for b in blocks
        if not any(
            k in b
            for k in [
                "会社情報",
                "店舗情報",
                "免許番号",
                "お迎え",
                "販売会社",
            ]
        )
    ]

    sources = [
        (
            "text_block",
            block,
        )
        for block in clean_blocks
    ]

    sources.append(
        (
            "page_text",
            page_text,
        )
    )

    # --------------------------------------------------------
    # Bus
    # --------------------------------------------------------

    bus_patterns = [
        r"(?:([^\s「『]+?"
        r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
        r"[「『]([^」』]{1,15})[」』]"
        r"\s*(?:駅)?\s*"
        r"バス\s*(\d+)\s*分"
        r"\s*(.{1,30}?)"
        r"\s*(?:徒歩|歩)\s*(\d+)\s*分",

        r"(?:([^\s「『]+?"
        r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
        r"[「『]([^」』]{1,15})[」』]"
        r"\s*(?:駅)?\s*"
        r"バス\s*(\d+)\s*分",
    ]

    for source, src in sources:

        for pattern in bus_patterns:

            match = re.search(
                pattern,
                src,
            )

            if not match:
                continue

            groups = match.groups()

            if len(groups) == 5:

                _, station, bus_min, stop, walk_min = groups

                if (
                    station
                    and len(station) <= 15
                ):

                    candidate = {
                        "station": clean_text(
                            station
                        ),
                        "stationAccessType": "bus",
                        "busMinutes": int(
                            bus_min
                        ),
                        "busStop": clean_text(
                            stop
                        ),
                        "busStopWalkMinutes": int(
                            walk_min
                        ),
                        "transportRaw": clean_text(
                            match.group(0)
                        ),
                    }

                    candidates.append(
                        create_candidate(
                            "station",
                            candidate,
                            match.group(0),
                            source,
                            0.92
                            if source == "text_block"
                            else 0.82,
                        )
                    )

            elif len(groups) == 4:

                _, station, bus_min, _ = (
                    groups
                )

                if station:

                    candidate = {
                        "station": clean_text(
                            station
                        ),
                        "stationAccessType": "bus",
                        "busMinutes": int(
                            bus_min
                        ),
                        "busStop": None,
                        "busStopWalkMinutes": None,
                        "transportRaw": clean_text(
                            match.group(0)
                        ),
                    }

                    candidates.append(
                        create_candidate(
                            "station",
                            candidate,
                            match.group(0),
                            source,
                            0.85,
                        )
                    )

    # --------------------------------------------------------
    # Walk
    # --------------------------------------------------------

    walk_patterns = [
        r"(?:([^\s「『]+?"
        r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
        r"[「『]([^」』]{1,15})[」』]"
        r"\s*(?:駅)?\s*"
        r"(?:徒歩|歩)\s*(\d+)\s*分",

        r"(?:([^\s「『]+?"
        r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
        r"([^\s「『]{1,10}駅)"
        r"\s*(?:徒歩|歩)\s*(\d+)\s*分",
    ]

    for source, src in sources:

        for pattern in walk_patterns:

            match = re.search(
                pattern,
                src,
            )

            if not match:
                continue

            groups = match.groups()

            station = groups[1]
            walk_min = int(
                groups[2]
            )

            station = station.replace(
                "駅",
                "",
            ).strip()

            if not (
                station
                and 1 <= walk_min <= 120
            ):
                continue

            candidate = {
                "station": clean_text(
                    station
                ),
                "stationAccessType": "walk",
                "stationWalkMinutes": walk_min,
                "walkMinutes": walk_min,
                "transportRaw": clean_text(
                    match.group(0)
                ),
                "busMinutes": None,
                "busStop": None,
                "busStopWalkMinutes": None,
            }

            candidates.append(
                create_candidate(
                    "station",
                    candidate,
                    match.group(0),
                    source,
                    0.94
                    if source == "text_block"
                    else 0.84,
                )
            )

    # --------------------------------------------------------
    # Select station candidate
    # --------------------------------------------------------

    if candidates:

        candidates.sort(
            key=lambda x: x[
                "confidence"
            ],
            reverse=True,
        )

        best = candidates[0]

        result.update(
            best["value"]
        )

    return result, candidates


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

    m1 = re.search(
        r"情報提供日\s*[:：]?\s*"
        r"(20\d{2})年\s*"
        r"(\d{1,2})月\s*"
        r"(\d{1,2})日",
        page_text,
    )

    if m1:

        result["informationDate"] = (
            f"{int(m1.group(1)):04d}-"
            f"{int(m1.group(2)):02d}-"
            f"{int(m1.group(3)):02d}"
        )

    m2 = re.search(
        r"次回更新予定日\s*[:：]?\s*"
        r"(20\d{2})年\s*"
        r"(\d{1,2})月\s*"
        r"(\d{1,2})日",
        page_text,
    )

    if m2:

        result["nextUpdateDate"] = (
            f"{int(m2.group(1)):04d}-"
            f"{int(m2.group(2)):02d}-"
            f"{int(m2.group(3)):02d}"
        )

    return result


# ============================================================
# Quality evaluation
# ============================================================

def evaluate_detail_quality(
    detail: Dict[str, Any],
    property_type: Optional[str] = None,
) -> Dict[str, Any]:

    p_type = (
        property_type
        or detail.get("propertyType")
    )

    # --------------------------------------------------------
    # Critical fields
    # --------------------------------------------------------

    critical_fields = {
        "price": detail.get("price"),
        "address": detail.get("address"),
        "landAreaM2": detail.get("landAreaM2"),
        "buildingAreaM2": detail.get("buildingAreaM2"),
        "layout": detail.get("layout"),
    }

    # 中古戸建では築年月を重要項目として追加
    if p_type != "新築戸建":

        critical_fields[
            "constructionMonth"
        ] = detail.get(
            "constructionMonth"
        )

    # --------------------------------------------------------
    # Important
    # --------------------------------------------------------

    important_fields = {
        "station": detail.get("station"),
    }

    # --------------------------------------------------------
    # Missing
    # --------------------------------------------------------

    missing_critical = [
        field
        for field, value
        in critical_fields.items()
        if value is None
        or value == ""
    ]

    missing_important = [
        field
        for field, value
        in important_fields.items()
        if value is None
        or value == ""
    ]

    missing_fields = (
        missing_critical
        + missing_important
    )

    warnings = []

    # --------------------------------------------------------
    # Address validation
    # --------------------------------------------------------

    if detail.get("address"):

        if is_company_address(
            str(detail["address"])
        ):

            warnings.append(
                "会社・店舗住所の可能性がある"
            )

        if not re.search(
            PREFECTURES_PATTERN,
            str(detail["address"]),
        ):

            warnings.append(
                "所在地が都道府県住所形式ではない"
            )

    # --------------------------------------------------------
    # Area sanity check
    # --------------------------------------------------------

    land = detail.get(
        "landAreaM2"
    )

    building = detail.get(
        "buildingAreaM2"
    )

    if land and building:

        if building > land * 3:

            warnings.append(
                "建物面積が土地面積に対して異常に大きい"
            )

    if land is not None:

        if land < 20:

            warnings.append(
                "土地面積が異常に小さい"
            )

    if building is not None:

        if building < 20:

            warnings.append(
                "建物面積が異常に小さい"
            )

    # --------------------------------------------------------
    # Price sanity
    # --------------------------------------------------------

    price = detail.get("price")

    if price is not None:

        if price < 1_000_000:

            warnings.append(
                "販売価格が異常に低い"
            )

    # --------------------------------------------------------
    # Station
    # --------------------------------------------------------

    if not detail.get("station"):

        warnings.append(
            "駅情報を抽出できない"
        )

    # --------------------------------------------------------
    # Extraction audit
    # --------------------------------------------------------

    audit = detail.get(
        "extractionAudit",
        {},
    )

    weak_fields = []

    for field, entry in audit.items():

        if entry.get("status") != "found":
            continue

        confidence = entry.get(
            "selected_confidence"
        )

        if (
            confidence is not None
            and confidence < 0.70
        ):

            weak_fields.append(field)

    if weak_fields:

        warnings.append(
            "低信頼度抽出項目: "
            + ",".join(weak_fields)
        )

    # --------------------------------------------------------
    # Quality
    # --------------------------------------------------------

    has_invalid = any(
        "会社・店舗住所"
        in warning
        for warning in warnings
    )

    # 重要項目が揃っている場合は、
    # 駅情報欠落だけでpoorにしない
    if missing_critical or has_invalid:

        quality = "poor"

        if missing_critical:
            poor_reason = "true_missing_or_extraction_failure"
        else:
            poor_reason = "invalid"

    elif (
        missing_important
        or warnings
    ):

        quality = "partial"
        poor_reason = None

    else:

        quality = "good"
        poor_reason = None

    score = {
        "good": 100,
        "partial": 75,
        "poor": 30,
    }.get(
        quality,
        0,
    )

    return {
        "detailQuality": quality,
        "detailQualityScore": score,
        "missingFields": missing_fields,
        "missingCriticalFields": missing_critical,
        "missingImportantFields": missing_important,
        "validationWarnings": warnings,
        "poorReasonCategory": poor_reason,
        "propertyTypeUsedForEvaluation": p_type,
        "weakExtractionFields": weak_fields,
    }


# ============================================================
# HTTP
# ============================================================

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Cache-Control": "no-cache",
}

DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Cache-Control": "no-cache",
}


def fetch_html(
    request_url: str,
    headers: Dict[str, str],
    timeout: int,
) -> Dict[str, Any]:

    try:

        response = requests.get(
            request_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )

        response.raise_for_status()

        if (
            not response.encoding
            or response.encoding.lower()
            in {
                "iso-8859-1",
                "latin-1",
            }
        ):

            response.encoding = (
                response.apparent_encoding
                or "utf-8"
            )

        html = response.text or ""

        return {
            "success": True,
            "html": html,
            "response": response,
            "finalUrl": response.url,
            "error": None,
        }

    except Exception as exc:

        return {
            "success": False,
            "html": "",
            "response": None,
            "finalUrl": None,
            "error": str(exc),
        }


# ============================================================
# Single HTML parser
# ============================================================

def parse_detail_html(
    html: str,
    source_url: str,
    request_url: str,
    final_url: str,
    http_status: int,
) -> Dict[str, Any]:

    raw_soup = BeautifulSoup(
        html,
        "html.parser",
    )

    clean_soup = remove_unwanted_sections(
        raw_soup
    )

    pairs = collect_label_value_pairs(
        clean_soup
    )

    blocks = extract_text_blocks(
        clean_soup
    )

    page_text = (
        clean_text(
            clean_soup.get_text(
                " ",
                strip=True,
            )
        )
        or ""
    )

    json_ld = extract_json_ld(
        raw_soup
    )

    meta = extract_meta_content(
        raw_soup
    )

    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------

    h1 = clean_soup.find("h1")

    title = (
        clean_text(
            h1.get_text(
                " ",
                strip=True,
            )
        )
        if h1
        else None
    )

    if not title and clean_soup.title:

        title = clean_text(
            clean_soup.title.get_text(
                " ",
                strip=True,
            )
        )

    # --------------------------------------------------------
    # Candidates
    # --------------------------------------------------------

    price_cands = collect_price_candidates(
        pairs,
        blocks,
        page_text,
        json_ld,
    )

    price, _ = select_best_candidate(
        price_cands
    )

    address_cands = collect_address_candidates(
        pairs,
        blocks,
        page_text,
        json_ld,
    )

    address, _ = select_best_candidate(
        address_cands
    )

    land_cands = collect_land_area_candidates(
        pairs,
        blocks,
        page_text,
    )

    land_area, _ = select_best_candidate(
        land_cands
    )

    (
        building_cands,
        footprint_cands,
    ) = collect_building_area_candidates(
        pairs,
        blocks,
        page_text,
    )

    building_area, _ = select_best_candidate(
        building_cands
    )

    building_footprint_area, _ = (
        select_best_candidate(
            footprint_cands
        )
    )

    layout_cands = collect_layout_candidates(
        pairs,
        blocks,
    )

    layout, _ = select_best_candidate(
        layout_cands
    )

    construction_cands = (
        collect_construction_candidates(
            pairs,
            blocks,
            page_text,
        )
    )

    (
        construction_month,
        construction_best,
    ) = select_best_candidate(
        construction_cands
    )

    construction_precision = (
        construction_best[
            "metadata"
        ].get("precision")
        if construction_best
        else None
    )

    property_type_cands = (
        collect_property_type_candidates(
            pairs,
            blocks,
            title,
            page_text,
        )
    )

    property_type, _ = (
        select_best_candidate(
            property_type_cands
        )
    )

    station_info, station_cands = (
        extract_station_info(
            blocks,
            page_text,
        )
    )

    information_dates = (
        extract_information_dates(
            page_text
        )
    )

    construction_year, construction_month_number = (
        parse_construction_date(
            construction_month
        )
    )

    construction_age_years = (
        calculate_construction_age_years(
            construction_month
        )
    )

    # --------------------------------------------------------
    # Extraction audit
    # --------------------------------------------------------

    extraction_audit = {
        "price": build_audit_entry(
            price_cands,
            price,
        ),

        "address": build_audit_entry(
            address_cands,
            address,
        ),

        "landAreaM2": build_audit_entry(
            land_cands,
            land_area,
        ),

        "buildingAreaM2": build_audit_entry(
            building_cands,
            building_area,
        ),

        "buildingFootprintAreaM2": build_audit_entry(
            footprint_cands,
            building_footprint_area,
        ),

        "layout": build_audit_entry(
            layout_cands,
            layout,
        ),

        "constructionMonth": build_audit_entry(
            construction_cands,
            construction_month,
        ),

        "propertyType": build_audit_entry(
            property_type_cands,
            property_type,
        ),

        "station": build_audit_entry(
            station_cands,
            station_info.get(
                "station"
            ),
        ),
    }

    # --------------------------------------------------------
    # Canonical listing URL
    #
    # main.py / suumo_search.py と物件識別を統一する。
    # 個別物件URLは
    # https://suumo.jp/.../nc_xxxxxxxx/
    # に統一し、query / fragment は除去する。
    # --------------------------------------------------------

    normalized_listing_url = (
        normalize_suumo_url(
            source_url
        )
        or normalize_suumo_url(
            request_url
        )
        or normalize_suumo_url(
            final_url
        )
    )

    # --------------------------------------------------------
    # Detail
    # --------------------------------------------------------

    detail: Dict[str, Any] = {

        # main.py が参照する正式なparser version
        "parserVersion":
            DETAIL_PARSER_VERSION,

        # 後方互換用
        "detailParserVersion":
            DETAIL_PARSER_VERSION,

        "title":
            title,

        "propertyType":
            property_type,

        # main.py が参照するcanonical URL
        "url":
            normalized_listing_url,

        "sourceUrl":
            normalized_listing_url
            or source_url,

        "requestUrl":
            request_url,

        "finalUrl":
            final_url,

        "price":
            price,

        "address":
            address,

        "landAreaM2":
            land_area,

        "buildingAreaM2":
            building_area,

        "buildingFootprintAreaM2":
            building_footprint_area,

        "layout":
            layout,

        "constructionMonth":
            construction_month,

        "constructionMonthPrecision":
            construction_precision,

        "constructionYear":
            construction_year,

        "constructionMonthNumber":
            construction_month_number,

        "constructionAgeYears":
            construction_age_years,

        "station":
            station_info.get(
                "station"
            ),

        "stationWalkMinutes":
            station_info.get(
                "stationWalkMinutes"
            ),

        "walkMinutes":
            station_info.get(
                "walkMinutes"
            ),

        "transportRaw":
            station_info.get(
                "transportRaw"
            ),

        "stationAccessType":
            station_info.get(
                "stationAccessType"
            ),

        "busMinutes":
            station_info.get(
                "busMinutes"
            ),

        "busStop":
            station_info.get(
                "busStop"
            ),

        "busStopWalkMinutes":
            station_info.get(
                "busStopWalkMinutes"
            ),

        "informationDate":
            information_dates.get(
                "informationDate"
            ),

        "nextUpdateDate":
            information_dates.get(
                "nextUpdateDate"
            ),

        "extractionAudit":
            extraction_audit,

        "parserDiagnostics": {
            "tablePairCount":
                len(pairs),

            "textBlockCount":
                len(blocks),

            "jsonLdCount":
                len(json_ld),

            "metaCount":
                len(meta),

            "pageTextLength":
                len(page_text),

            "htmlLength":
                len(html),
        },
    }

    detail.update(
        evaluate_detail_quality(
            detail,
            property_type,
        )
    )

    return detail


# ============================================================
# Candidate comparison between HTML versions
# ============================================================

def merge_detail_candidates(
    primary: Dict[str, Any],
    secondary: Dict[str, Any],
) -> Dict[str, Any]:

    merged = dict(primary)

    audit_primary = (
        primary.get(
            "extractionAudit",
            {},
        )
    )

    audit_secondary = (
        secondary.get(
            "extractionAudit",
            {},
        )
    )

    merged_audit = {}

    fields = set(
        audit_primary.keys()
    ) | set(
        audit_secondary.keys()
    )

    for field in fields:

        candidates = []

        for audit in [
            audit_primary.get(
                field,
                {},
            ),
            audit_secondary.get(
                field,
                {},
            ),
        ]:

            candidates.extend(
                audit.get(
                    "candidates",
                    [],
                )
            )

        if field == "station":
            primary_station = primary.get("station")
            secondary_station = secondary.get("station")

            selected_val, selected_cand = select_best_candidate(candidates)

            station_str = None
            selected_station_dict = None

            if isinstance(selected_val, dict):
                station_str = selected_val.get("station")
                selected_station_dict = selected_val
            elif isinstance(selected_val, str):
                station_str = selected_val

            if not station_str:
                station_str = primary_station or secondary_station

            merged["station"] = station_str

            station_related_keys = [
                "stationWalkMinutes",
                "walkMinutes",
                "transportRaw",
                "stationAccessType",
                "busMinutes",
                "busStop",
                "busStopWalkMinutes",
            ]

            if selected_station_dict:
                for station_key in station_related_keys:
                    if selected_station_dict.get(station_key) is not None:
                        merged[station_key] = selected_station_dict[station_key]

            for station_key in station_related_keys:
                if (
                    merged.get(station_key) is None
                    and secondary.get(station_key) is not None
                ):
                    merged[station_key] = secondary[station_key]

            merged_audit[field] = build_audit_entry(
                candidates,
                station_str,
            )
            continue

        selected_primary = (
            primary.get(field)
        )

        selected_secondary = (
            secondary.get(field)
        )

        # 候補を再評価
        selected, selected_candidate = (
            select_best_candidate(
                candidates
            )
        )

        if selected is None:

            selected = (
                selected_primary
                if selected_primary is not None
                else selected_secondary
            )

        if selected is not None:

            merged[field] = selected

        merged_audit[field] = (
            build_audit_entry(
                candidates,
                selected,
            )
        )

    merged[
        "extractionAudit"
    ] = merged_audit

    # primaryに存在しない情報はsecondaryから補完
    for key, value in secondary.items():

        if key in {
            "extractionAudit",
            "detailQuality",
            "detailQualityScore",
            "missingFields",
            "missingCriticalFields",
            "missingImportantFields",
            "validationWarnings",
            "poorReasonCategory",
            "weakExtractionFields",
        }:
            continue

        if (
            merged.get(key) is None
            and value is not None
        ):
            merged[key] = value

    merged.update(
        evaluate_detail_quality(
            merged,
            merged.get(
                "propertyType"
            ),
        )
    )

    return merged


# ============================================================
# Detail Fetcher Core
# ============================================================

def fetch_detail(
    url: str,
    timeout: int = 20,
    retry_desktop_on_partial: bool = True,
) -> Dict[str, Any]:

    original_url = str(url).strip()

    if not original_url:

        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": "empty_suumo_url",
            "errorType": "invalid_suumo_url",
            "sourceUrl": url,
        }

    display_url = (
        preserve_suumo_listing_url(
            original_url
        )
    )

    request_url = (
        normalize_suumo_url(
            original_url
        )
    )

    if not request_url:

        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": "invalid_suumo_url",
            "errorType": "invalid_suumo_url",
            "sourceUrl":
                display_url
                or original_url,
        }

    fetched_at = now_iso()

    # --------------------------------------------------------
    # First pass: mobile
    # --------------------------------------------------------

    first = fetch_html(
        request_url,
        MOBILE_HEADERS,
        timeout,
    )

    if not first["success"]:

        # HTTP失敗時はdesktopでも1回だけ試す
        second = fetch_html(
            request_url,
            DESKTOP_HEADERS,
            timeout,
        )

        if not second["success"]:

            return {
                "success": False,
                "fetchedAt": fetched_at,
                "detail": None,
                "error": (
                    first["error"]
                    or second["error"]
                ),
                "errorType": "http_error",
                "sourceUrl":
                    display_url
                    or original_url,
            }

        first = second

    html = first["html"]

    if len(
        html.strip()
    ) < 500:

        return {
            "success": False,
            "fetchedAt": fetched_at,
            "detail": None,
            "error": "empty_html",
            "errorType": "empty_html",
            "sourceUrl":
                display_url
                or original_url,
        }

    final_url = (
        preserve_suumo_listing_url(
            first["finalUrl"]
        )
        or first["finalUrl"]
        or request_url
    )

    response = first["response"]

    primary_detail = parse_detail_html(
        html=html,
        source_url=(
            display_url
            or original_url
        ),
        request_url=request_url,
        final_url=final_url,
        http_status=(
            response.status_code
            if response
            else 200
        ),
    )

    # --------------------------------------------------------
    # Deep audit retry
    #
    # poor / partialの場合はdesktop HTMLを取得し、
    # mobile DOMで見えなかった情報を補完する。
    # --------------------------------------------------------

    secondary_detail = None

    should_retry = (
        retry_desktop_on_partial
        and primary_detail.get(
            "detailQuality"
        ) in {
            "poor",
            "partial",
        }
    )

    if should_retry:

        second = fetch_html(
            request_url,
            DESKTOP_HEADERS,
            timeout,
        )

        if second["success"]:

            second_final_url = (
                preserve_suumo_listing_url(
                    second["finalUrl"]
                )
                or second["finalUrl"]
                or request_url
            )

            second_response = (
                second["response"]
            )

            secondary_detail = (
                parse_detail_html(
                    html=second["html"],
                    source_url=(
                        display_url
                        or original_url
                    ),
                    request_url=request_url,
                    final_url=second_final_url,
                    http_status=(
                        second_response.status_code
                        if second_response
                        else 200
                    ),
                )
            )

    # --------------------------------------------------------
    # Merge
    # --------------------------------------------------------

    if secondary_detail:

        detail = merge_detail_candidates(
            primary_detail,
            secondary_detail,
        )

        detail[
            "deepAudit"
        ] = {
            "performed": True,
            "primaryQuality":
                primary_detail.get(
                    "detailQuality"
                ),
            "secondaryQuality":
                secondary_detail.get(
                    "detailQuality"
                ),
        }

    else:

        detail = primary_detail

        detail[
            "deepAudit"
        ] = {
            "performed": False,
            "reason":
                "primary_detail_quality_good"
                if primary_detail.get(
                    "detailQuality"
                ) == "good"
                else "secondary_fetch_failed_or_disabled",
        }

    detail[
        "fetchedAt"
    ] = fetched_at

    detail[
        "httpStatus"
    ] = (
        response.status_code
        if response
        else None
    )

    detail[
        "htmlLength"
    ] = len(html)

    return {
        "success": True,
        "fetchedAt": fetched_at,
        "detail": detail,
        "error": None,
        "errorType": None,
        "sourceUrl":
            display_url
            or original_url,
        "requestUrl":
            request_url,
        "finalUrl":
            detail.get(
                "finalUrl"
            ),
    }


# ============================================================
# Adapter Class
# ============================================================

class SuumoDetailAdapter:

    def __init__(
        self,
        config: Optional[
            Dict[str, Any]
        ] = None,
        root_path: Optional[str] = None,
        **kwargs,
    ):

        self.config = (
            config
            or {}
        )

        self.root_path = root_path

        self.extra_kwargs = kwargs

        self.interval_seconds = float(
            self.config.get(
                "detailRequestIntervalSeconds",
                1.5,
            )
        )

        self.timeout = int(
            self.config.get(
                "detailTimeoutSeconds",
                20,
            )
        )

        self.retry_desktop_on_partial = bool(
            self.config.get(
                "detailRetryDesktopOnPartial",
                True,
            )
        )

    def wait(self):

        if self.interval_seconds > 0:

            time.sleep(
                self.interval_seconds
            )

    def fetch_detail(
        self,
        url: str,
    ) -> Dict[str, Any]:

        result = fetch_detail(
            url,
            timeout=self.timeout,
            retry_desktop_on_partial=(
                self.retry_desktop_on_partial
            ),
        )

        return result
