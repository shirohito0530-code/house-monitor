import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# Parser version
# ============================================================

DETAIL_PARSER_VERSION = "2026-09-22-v16-url-preserve"


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
    "リノベ",
    "即案内",
    "頭金",
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


# ============================================================
# Basic utilities
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> Optional[str]:

    if value is None:
        return None

    text = str(value)

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    return text or None


def is_promotional_text(
    value: Any,
) -> bool:

    text = clean_text(value)

    if not text:
        return True

    return any(
        word in text
        for word in PROMOTIONAL_WORDS
    )


def clean_suumo_value(
    value: Any,
) -> Optional[str]:

    text = clean_text(value)

    if not text:
        return None

    # UIノイズ
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


# ============================================================
# URL
# ============================================================

def _prepare_suumo_url(
    url: Any,
) -> Optional[str]:

    """
    SUUMO URLをHTTP取得可能な形に最低限だけ補正する。

    重要:
      この関数ではURLを「美しく正規化」しない。

    許可する補正:
      - //www.suumo.jp/... -> https://www.suumo.jp/...
      - /chukoikkodate/... -> https://www.suumo.jp/...
      - host/schemeがない場合の最低限の補正

    保持するもの:
      - http / https
      - www有無
      - path
      - query
      - 末尾スラッシュ

    削除するもの:
      - fragmentのみ
    """

    if not url:
        return None

    text = str(url).strip()

    if not text:
        return None

    # --------------------------------------------------------
    # Protocol-relative URL
    #
    # 例:
    #   //www.suumo.jp/chukoikkodate/.../nc_123/
    #
    # HTTPアクセスのためschemeだけ補う。
    # --------------------------------------------------------

    if text.startswith("//"):

        text = "https:" + text

    # --------------------------------------------------------
    # Relative URL
    #
    # 例:
    #   /chukoikkodate/chiba/sc_xxx/nc_123/
    #
    # SUUMO上の相対URLだけ最低限補正する。
    # --------------------------------------------------------

    elif text.startswith("/"):

        text = "https://www.suumo.jp" + text

    # --------------------------------------------------------
    # schemeなしのSUUMO URL
    #
    # 例:
    #   www.suumo.jp/chukoikkodate/...
    # --------------------------------------------------------

    elif text.startswith("www.suumo.jp/"):

        text = "https://" + text

    elif text.startswith("suumo.jp/"):

        text = "https://" + text

    return text


def _parse_and_validate_suumo_url(
    url: Any,
) -> Optional[Any]:

    """
    SUUMO URLを解析し、安全性と個別物件URLであることを確認する。

    この関数ではpathを書き換えない。
    """

    prepared = _prepare_suumo_url(
        url
    )

    if not prepared:
        return None

    try:

        parsed = urlparse(
            prepared
        )

    except Exception:

        return None

    scheme = (
        parsed.scheme or ""
    ).lower()

    hostname = (
        parsed.hostname or ""
    ).lower()

    # --------------------------------------------------------
    # Host
    # --------------------------------------------------------

    if hostname not in SUUMO_HOSTS:
        return None

    # --------------------------------------------------------
    # Scheme
    # --------------------------------------------------------

    if scheme not in {
        "http",
        "https",
    }:
        return None

    # --------------------------------------------------------
    # Path
    #
    # IMPORTANT:
    # ここでは /{2,} -> / のような加工をしない。
    # --------------------------------------------------------

    path = (
        parsed.path or ""
    )

    if not path:
        return None

    path_lower = path.lower()

    # --------------------------------------------------------
    # SUUMO戸建て個別ページ
    # --------------------------------------------------------

    if not any(
        path_lower.startswith(prefix)
        for prefix in SUUMO_LISTING_PREFIXES
    ):
        return None

    # --------------------------------------------------------
    # 個別物件ID
    #
    # 旧:
    #   /nc_\d+/?$
    #
    # 新:
    #   /nc_\d+(?:/|$)
    #
    # queryが付いていてもpathだけで判定できる。
    # --------------------------------------------------------

    if not re.search(
        r"/nc_\d+(?:/|$)",
        path,
        re.IGNORECASE,
    ):
        return None

    return parsed


def normalize_suumo_url(
    url: Any,
) -> Optional[str]:

    """
    SUUMO物件URLをHTTP取得用URLとして最低限だけ正規化する。

    ========================================================
    重要な設計方針
    ========================================================

    この関数はURLを別のURLに作り替えるためのものではない。

    以下を維持する:

      - http / https
      - suumo.jp / www.suumo.jp
      - path
      - path中のスラッシュ
      - query
      - 末尾スラッシュ

    以下のみ変更:

      - 相対URL → 絶対URL
      - protocol-relative URL → 絶対URL
      - fragment削除

    これにより、検索結果から取得した個別物件URLを
    可能な限りそのまま詳細取得に利用する。
    """

    parsed = _parse_and_validate_suumo_url(
        url
    )

    if parsed is None:
        return None

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def preserve_suumo_listing_url(
    url: Any,
) -> Optional[str]:

    """
    ユーザーに表示・保存するためのSUUMO物件URL。

    URLを可能な限り元の状態で保持する。

    ========================================================
    保持
    ========================================================

      - http / https
      - www有無
      - path
      - path中のスラッシュ
      - query
      - 末尾スラッシュ

    ========================================================
    変更
    ========================================================

      - 相対URLは絶対URLへ
      - protocol-relative URLはhttpsを補完
      - fragmentのみ削除
      - scheme / hostは比較用に小文字化

    ========================================================
    変更しない
    ========================================================

      - http → https の強制変換
      - suumo.jp → www.suumo.jp の強制変換
      - pathのスラッシュ整理
      - query削除
      - 末尾スラッシュ追加
    """

    parsed = _parse_and_validate_suumo_url(
        url
    )

    if parsed is None:
        return None

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def is_valid_suumo_url(
    url: str,
) -> bool:

    """
    SUUMO個別物件URLとして有効か確認する。
    """

    parsed = _parse_and_validate_suumo_url(
        url
    )

    return parsed is not None


# ============================================================
# Price
# ============================================================

def parse_price(
    value: Any,
) -> Optional[int]:

    """
    対応例:

      1.78億円
      1億7800万円
      1億7800万
      1780万円
      1780万
      17800000円
    """

    text = clean_text(value)

    if not text:
        return None

    text = (
        text
        .replace(",", "")
        .replace(" ", "")
        .replace("　", "")
    )

    # --------------------------------------------------------
    # 億
    # --------------------------------------------------------

    match = re.search(
        r"(?:(\d+(?:\.\d+)?)\s*億)"
        r"(?:\s*(\d+(?:\.\d+)?)\s*万)?"
        r"(?:円)?",
        text,
    )

    if match:

        oku = float(
            match.group(1)
        )

        man = float(
            match.group(2) or 0
        )

        price = int(
            round(
                oku * 100_000_000
                + man * 10_000
            )
        )

        return (
            price
            if price > 0
            else None
        )

    # --------------------------------------------------------
    # 万
    # --------------------------------------------------------

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

        return (
            price
            if price > 0
            else None
        )

    # --------------------------------------------------------
    # 円
    # --------------------------------------------------------

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

            return (
                price
                if price > 0
                else None
            )

        except ValueError:

            return None

    return None


# ============================================================
# Area
# ============================================================

def parse_area_m2(
    value: Any,
) -> Optional[float]:

    text = clean_text(value)

    if not text:
        return None

    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:m\s*[²2]?|㎡)",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    try:

        number = float(
            match.group(1)
        )

        return (
            number
            if number > 0
            else None
        )

    except ValueError:

        return None


# ============================================================
# Construction date
# ============================================================

def parse_year_month(
    value: Any,
) -> Tuple[
    Optional[str],
    Optional[str],
]:

    text = clean_text(value)

    if not text:
        return None, None

    patterns = [
        r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月",
        r"((?:19|20)\d{2})\s*[/-]\s*(\d{1,2})",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
        )

        if not match:
            continue

        year = int(
            match.group(1)
        )

        month = int(
            match.group(2)
        )

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

        match = re.search(
            pattern,
            text,
        )

        if not match:
            continue

        era_year = int(
            match.group(1)
        )

        month = int(
            match.group(2)
        )

        year = (
            base_year
            + era_year
        )

        if 1 <= month <= 12:

            return (
                f"{year:04d}-{month:02d}",
                "month",
            )

    era_year_patterns = [
        (
            r"令和\s*(\d{1,2})\s*年",
            2018,
        ),
        (
            r"平成\s*(\d{1,2})\s*年",
            1988,
        ),
        (
            r"昭和\s*(\d{1,2})\s*年",
            1925,
        ),
    ]

    for pattern, base_year in era_year_patterns:

        match = re.search(
            pattern,
            text,
        )

        if not match:
            continue

        year = (
            base_year
            + int(match.group(1))
        )

        return (
            f"{year:04d}-01",
            "year",
        )

    return None, None


def parse_construction_date(
    construction_month: Optional[str],
) -> Tuple[
    Optional[int],
    Optional[int],
]:

    if not construction_month:
        return None, None

    match = re.fullmatch(
        r"(\d{4})-(\d{2})",
        construction_month,
    )

    if not match:
        return None, None

    year = int(
        match.group(1)
    )

    month = int(
        match.group(2)
    )

    if not (
        1900
        <= year
        <= datetime.now().year + 2
    ):
        return None, None

    if not (
        1
        <= month
        <= 12
    ):
        return None, None

    return (
        year,
        month,
    )


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

    year = int(
        match.group(1)
    )

    month = int(
        match.group(2)
    )

    if reference_date is None:

        reference_date = datetime.now(
            timezone.utc
        )

    ref_year = (
        reference_date.year
    )

    ref_month = (
        reference_date.month
    )

    months = (
        (ref_year - year) * 12
        + (ref_month - month)
    )

    if months < 0:
        return None

    return round(
        months / 12,
        2,
    )


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
        ".cassette_shop",
        "#js-shopInfo",
        "#js-inquiryForm",
        ".ar-shop",
        ".ar-company",
        "[class*='shop']",
        "[class*='company']",
    ]

    for selector in selectors_to_remove:

        for element in soup_copy.select(
            selector
        ):
            element.decompose()

    return soup_copy


# ============================================================
# Label / value extraction
# ============================================================

def collect_label_value_pairs(
    soup: BeautifulSoup,
) -> Dict[str, str]:

    pairs: Dict[str, str] = {}

    for tr in soup.find_all("tr"):

        is_shop_or_company = False

        for parent in tr.parents:

            if parent.name not in {
                "div",
                "section",
                "table",
            }:
                continue

            p_class = " ".join(
                parent.get(
                    "class",
                    [],
                )
            )

            p_id = parent.get(
                "id",
                "",
            )

            combined = (
                f"{p_class} {p_id}"
            ).lower()

            if any(
                keyword in combined
                for keyword in [
                    "shop",
                    "company",
                    "tenpo",
                    "store",
                ]
            ):

                is_shop_or_company = True
                break

        if is_shop_or_company:
            continue

        cells = tr.find_all(
            ["th", "td"]
        )

        if len(cells) < 2:
            continue

        texts = []

        for cell in cells:

            text = clean_text(
                cell.get_text(
                    " ",
                    strip=True,
                )
            )

            if text:
                texts.append(text)

        if len(texts) < 2:
            continue

        label = clean_text(
            texts[0]
        )

        value = clean_suumo_value(
            " ".join(texts[1:])
        )

        if label and value:

            if label not in pairs:

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

        for element in soup.select(
            selector
        ):

            text = clean_text(
                element.get_text(
                    " ",
                    strip=True,
                )
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
) -> Optional[
    Tuple[str, str]
]:

    for keyword in keywords:

        for label, value in pairs.items():

            if keyword not in label:
                continue

            cleaned = clean_suumo_value(
                value
            )

            if cleaned:
                return (
                    cleaned,
                    keyword,
                )

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
            h1.get_text(
                " ",
                strip=True,
            )
        )

    if soup.title:

        return clean_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    return None


# ============================================================
# Price
# ============================================================

def extract_price_from_blocks(
    blocks: List[str],
) -> Tuple[
    Optional[int],
    Optional[str],
]:

    priority_keywords = [
        "販売価格",
        "販売価格（税込）",
        "価格",
    ]

    for keyword in priority_keywords:

        for block in blocks:

            if keyword not in block:
                continue

            if any(
                exclusion in block
                for exclusion
                in LOAN_EXCLUSION_WORDS
            ):
                continue

            price = parse_price(
                block
            )

            if price is not None:

                return (
                    price,
                    block,
                )

    return None, None


# ============================================================
# Area
# ============================================================

def extract_area_from_page(
    page_text: str,
    label: str,
) -> Tuple[
    Optional[float],
    Optional[str],
]:

    pattern = (
        rf"{re.escape(label)}"
        r"\s*"
        r"([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:m\s*[²2]?|㎡)"
    )

    match = re.search(
        pattern,
        page_text,
        re.IGNORECASE,
    )

    if not match:
        return None, None

    try:

        value = float(
            match.group(1)
        )

    except ValueError:

        return None, None

    start = max(
        0,
        match.start(),
    )

    end = min(
        len(page_text),
        match.end() + 40,
    )

    return (
        value,
        clean_text(
            page_text[start:end]
        ),
    )


def extract_land_area(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Tuple[
    Optional[float],
    Optional[str],
]:

    result = find_value_by_keywords(
        pairs,
        [
            "土地面積",
            "敷地面積",
        ],
    )

    if result:

        value, _ = result

        parsed = parse_area_m2(
            value
        )

        if parsed is not None:

            return (
                parsed,
                value,
            )

    for label in [
        "土地面積",
        "敷地面積",
    ]:

        parsed, text = (
            extract_area_from_page(
                page_text,
                label,
            )
        )

        if parsed is not None:

            return (
                parsed,
                text,
            )

    for block in blocks:

        if (
            "土地面積" not in block
            and "敷地面積" not in block
        ):
            continue

        parsed = parse_area_m2(
            block
        )

        if parsed is not None:

            return (
                parsed,
                block,
            )

    return None, None


def extract_building_area(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Tuple[
    Optional[float],
    Optional[str],
    Optional[str],
]:

    area_keywords = [
        "延床面積",
        "建物面積",
        "建築面積",
    ]

    result = find_value_by_keywords(
        pairs,
        area_keywords,
    )

    if result:

        value, matched_keyword = result

        parsed = parse_area_m2(
            value
        )

        if parsed is not None:

            return (
                parsed,
                value,
                matched_keyword,
            )

    for keyword in area_keywords:

        parsed, text = (
            extract_area_from_page(
                page_text,
                keyword,
            )
        )

        if parsed is not None:

            return (
                parsed,
                text,
                keyword,
            )

    for block in blocks:

        if not any(
            keyword in block
            for keyword in area_keywords
        ):
            continue

        for keyword in area_keywords:

            if keyword not in block:
                continue

            after_label = block.split(
                keyword,
                1,
            )[1]

            parsed = parse_area_m2(
                after_label
            )

            if parsed is not None:

                return (
                    parsed,
                    block,
                    keyword,
                )

    return None, None, None


# ============================================================
# Address
# ============================================================

def is_company_address(
    address: str,
) -> bool:

    if not address:
        return True

    company_words = [
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
    ]

    return any(
        word in address
        for word in company_words
    )


def normalize_address(
    value: Any,
) -> Optional[str]:

    address = clean_suumo_value(
        value
    )

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

    if is_company_address(
        address
    ):
        return None

    if not re.search(
        PREFECTURES_PATTERN,
        address,
    ):
        return None

    return address


def extract_address(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Optional[str]:

    for label, value in pairs.items():

        if "物件所在地" not in label:
            continue

        address = normalize_address(
            value
        )

        if address:
            return address

    for label, value in pairs.items():

        if "所在地" not in label:
            continue

        if any(
            keyword in label
            for keyword in [
                "店舗",
                "会社",
                "取扱",
                "販売会社",
            ]
        ):
            continue

        address = normalize_address(
            value
        )

        if address:
            return address

    patterns = [

        rf"(?:物件所在地|所在地)"
        rf"\s*[:：]?\s*"
        rf"({PREFECTURES_PATTERN}"
        rf"[^。\[［\]\n]{{2,100}})",

        rf"({PREFECTURES_PATTERN}"
        rf"[^。\[［\]\n]{{2,100}})"
        rf"\s*地図を見る",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
        )

        if not match:
            continue

        address = normalize_address(
            match.group(1)
        )

        if address:
            return address

    for block in blocks:

        if any(
            keyword in block
            for keyword in [
                "会社情報",
                "取り扱い店舗",
                "店舗情報",
                "加盟",
                "免許番号",
                "販売会社",
                "取引態様",
            ]
        ):
            continue

        match = re.search(
            rf"({PREFECTURES_PATTERN}"
            rf"[^。\[［\]\n]{{2,100}})",
            block,
        )

        if not match:
            continue

        address = normalize_address(
            match.group(1)
        )

        if address:
            return address

    return None


# ============================================================
# Layout
# ============================================================

def extract_layout(
    pairs: Dict[str, str],
    blocks: List[str],
) -> Tuple[
    Optional[str],
    Optional[str],
]:

    result = find_value_by_keywords(
        pairs,
        ["間取り"],
    )

    pattern = (
        r"\d+\s*"
        r"(?:LDK|DK|LK|K)"
        r"(?:\s*[\+＋]\s*\d*S)?"
    )

    if result:

        layout_raw, _ = result

        match = re.search(
            pattern,
            layout_raw,
            re.IGNORECASE,
        )

        if match:

            return (
                clean_text(
                    match.group(0)
                ),
                clean_text(
                    layout_raw
                ),
            )

    for block in blocks:

        match = re.search(
            pattern,
            block,
            re.IGNORECASE,
        )

        if match:

            return (
                clean_text(
                    match.group(0)
                ),
                clean_text(
                    block
                ),
            )

    return None, None


# ============================================================
# Construction
# ============================================================

def extract_construction(
    pairs: Dict[str, str],
    blocks: List[str],
    page_text: str,
) -> Tuple[
    Optional[str],
    Optional[str],
    Optional[str],
]:

    construction_keywords = [
        "完成時期",
        "築年月",
        "建築年月",
        "完成年月",
        "築年",
    ]

    for label, value in pairs.items():

        if not any(
            keyword in label
            for keyword in construction_keywords
        ):
            continue

        parsed, precision = (
            parse_year_month(
                value
            )
        )

        if parsed:

            return (
                parsed,
                precision,
                value,
            )

    patterns = [

        r"(?:完成時期\s*\(築年月\)|"
        r"完成時期|築年月|建築年月|完成年月)"
        r"\s*[:：]?\s*"
        r"((?:19|20)\d{2}年\d{1,2}月)",

        r"(?:完成時期\s*\(築年月\)|"
        r"完成時期|築年月|建築年月|完成年月)"
        r"[^0-9]{0,30}"
        r"((?:19|20)\d{2})年\s*(\d{1,2})月",

        r"(?:築年月|建築年月)"
        r"[^0-9]{0,30}"
        r"((?:19|20)\d{2})"
        r"[/-]"
        r"(\d{1,2})",

        r"(?:完成時期|築年月|建築年月)"
        r"[^0-9]{0,30}"
        r"(令和\s*\d{1,2}\s*年"
        r"\s*\d{1,2}\s*月|"
        r"平成\s*\d{1,2}\s*年"
        r"\s*\d{1,2}\s*月)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
        )

        if not match:
            continue

        if (
            match.lastindex
            and match.lastindex >= 2
            and match.group(2)
            and match.group(1)
        ):

            value = (
                f"{match.group(1)}年"
                f"{match.group(2)}月"
            )

        else:

            value = match.group(1)

        parsed, precision = (
            parse_year_month(
                value
            )
        )

        if parsed:

            return (
                parsed,
                precision,
                value,
            )

    for block in blocks:

        if not any(
            keyword in block
            for keyword in [
                "築年月",
                "完成時期",
                "建築年月",
                "完成年月",
            ]
        ):
            continue

        parsed, precision = (
            parse_year_month(
                block
            )
        )

        if parsed:

            return (
                parsed,
                precision,
                block,
            )

    return None, None, None


# ============================================================
# Station / transportation
# ============================================================

def is_valid_station_name(
    station: Optional[str],
) -> bool:

    if not station:
        return False

    text = clean_text(
        station
    )

    if not text:
        return False

    if text in {
        "徒",
        "歩",
        "徒歩",
        "駅",
        "分",
        "バス",
        "ヒント",
        "なし",
        "null",
        "none",
    }:
        return False

    if len(text) > 20:
        return False

    if any(
        keyword in text
        for keyword in [
            "お迎え",
            "見学",
            "提案",
            "物件",
            "案内",
            "月々",
            "頭金",
            "ローン",
            "リノベ",
            "お気軽",
        ]
    ):
        return False

    return True


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

    clean_blocks = []

    for block in blocks:

        if any(
            keyword in block
            for keyword in [
                "会社情報",
                "取り扱い店舗",
                "店舗情報",
                "免許番号",
                "お迎え",
                "見学予約",
                "ご案内方法",
                "コース",
                "加盟",
                "販売会社",
            ]
        ):
            continue

        clean_blocks.append(
            block
        )

    search_sources = (
        clean_blocks
        + [page_text]
    )

    bus_patterns = [

        (
            r"(?:([^\s「『]+?"
            r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
            r"[「『]([^」』]{1,15})[」』]"
            r"\s*(?:駅)?\s*"
            r"バス\s*(\d+)\s*分"
            r"\s*(.{1,30}?)"
            r"\s*(?:徒歩|歩)\s*(\d+)\s*分"
        ),

        (
            r"(?:([^\s「『]+?"
            r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
            r"[「『]([^」』]{1,15})[」』]"
            r"\s*(?:駅)?\s*"
            r"バス\s*(\d+)\s*分"
        ),
    ]

    for source in search_sources:

        for pattern in bus_patterns:

            match = re.search(
                pattern,
                source,
            )

            if not match:
                continue

            groups = match.groups()

            if len(groups) == 5:

                (
                    line,
                    station,
                    bus_minutes,
                    stop,
                    walk_minutes,
                ) = groups

                if not is_valid_station_name(
                    station
                ):
                    continue

                bus_int = int(
                    bus_minutes
                )

                walk_int = int(
                    walk_minutes
                )

                if not (
                    1 <= bus_int <= 180
                    and
                    1 <= walk_int <= 120
                ):
                    continue

                result["station"] = clean_text(
                    station
                )

                result["stationAccessType"] = (
                    "bus"
                )

                result["busMinutes"] = (
                    bus_int
                )

                result["busStop"] = (
                    clean_text(stop)
                )

                result["busStopWalkMinutes"] = (
                    walk_int
                )

                result["transportRaw"] = (
                    clean_text(
                        match.group(0)
                    )
                )

                return result

            if len(groups) == 3:

                (
                    line,
                    station,
                    bus_minutes,
                ) = groups

                if not is_valid_station_name(
                    station
                ):
                    continue

                bus_int = int(
                    bus_minutes
                )

                if not (
                    1 <= bus_int <= 180
                ):
                    continue

                result["station"] = (
                    clean_text(
                        station
                    )
                )

                result["stationAccessType"] = (
                    "bus"
                )

                result["busMinutes"] = (
                    bus_int
                )

                result["transportRaw"] = (
                    clean_text(
                        match.group(0)
                    )
                )

                return result

    walk_patterns = [

        (
            r"(?:([^\s「『]+?"
            r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
            r"[「『]([^」』]{1,15})[」』]"
            r"\s*(?:駅)?\s*"
            r"(?:徒歩|歩)\s*(\d+)\s*分"
        ),

        (
            r"(?:([^\s「『]+?"
            r"(?:線|ライン|エクスプレス|モノレール))\s*)?"
            r"([^\s「『]{1,10}駅)"
            r"\s*(?:徒歩|歩)\s*(\d+)\s*分"
        ),
    ]

    for source in search_sources:

        for pattern in walk_patterns:

            match = re.search(
                pattern,
                source,
            )

            if not match:
                continue

            groups = match.groups()

            line = groups[0]
            station = groups[1]
            walk_minutes = groups[2]

            station_clean = (
                station
                .replace(
                    "駅",
                    "",
                )
                .strip()
            )

            if not is_valid_station_name(
                station_clean
            ):
                continue

            walk_int = int(
                walk_minutes
            )

            if not (
                1 <= walk_int <= 120
            ):
                continue

            result["station"] = (
                clean_text(
                    station_clean
                )
            )

            result["stationAccessType"] = (
                "walk"
            )

            result["stationWalkMinutes"] = (
                walk_int
            )

            result["walkMinutes"] = (
                walk_int
            )

            result["transportRaw"] = (
                clean_text(
                    match.group(0)
                )
            )

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

        "informationDate": (
            r"情報提供日\s*[:：]?\s*"
            r"(20\d{2})年\s*"
            r"(\d{1,2})月\s*"
            r"(\d{1,2})日"
        ),

        "nextUpdateDate": (
            r"次回更新予定日\s*[:：]?\s*"
            r"(20\d{2})年\s*"
            r"(\d{1,2})月\s*"
            r"(\d{1,2})日"
        ),
    }

    for field, pattern in patterns.items():

        match = re.search(
            pattern,
            page_text,
        )

        if not match:
            continue

        year = int(
            match.group(1)
        )

        month = int(
            match.group(2)
        )

        day = int(
            match.group(3)
        )

        try:

            date = datetime(
                year,
                month,
                day,
            )

            result[field] = (
                date.strftime(
                    "%Y-%m-%d"
                )
            )

        except ValueError:

            continue

    return result


# ============================================================
# Quality evaluation
# ============================================================

def evaluate_detail_quality(
    detail: Dict[str, Any],
) -> Dict[str, Any]:

    critical_fields = {
        "price": detail.get(
            "price"
        ),
        "address": detail.get(
            "address"
        ),
        "constructionMonth": detail.get(
            "constructionMonth"
        ),
    }

    important_fields = {
        "landAreaM2": detail.get(
            "landAreaM2"
        ),
        "buildingAreaM2": detail.get(
            "buildingAreaM2"
        ),
        "layout": detail.get(
            "layout"
        ),
        "station": detail.get(
            "station"
        ),
    }

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

    warnings: List[str] = []

    if (
        detail.get("priceText")
        and detail.get("price") is None
    ):

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

    address = detail.get(
        "address"
    )

    if address and is_company_address(
        str(address)
    ):

        warnings.append(
            "会社・店舗住所の可能性がある"
        )

    station = detail.get(
        "station"
    )

    if station in {
        "徒",
        "歩",
        "分",
        "バス",
        "駅",
    }:

        warnings.append(
            "駅名が不正なUI文字列である"
        )

    if not station:

        warnings.append(
            "駅情報を抽出できない"
        )

    if (
        detail.get(
            "stationAccessType"
        )
        == "walk"
        and detail.get(
            "walkMinutes"
        ) is None
    ):

        warnings.append(
            "徒歩分数を抽出できない"
        )

    if not detail.get(
        "constructionMonth"
    ):

        warnings.append(
            "築年月を抽出できない"
        )

    serious_warning_words = [
        "会社・店舗住所",
        "駅名が不正",
    ]

    has_serious_warning = any(
        any(
            word in warning
            for word in serious_warning_words
        )
        for warning in warnings
    )

    if (
        missing_critical
        or has_serious_warning
    ):

        quality = "poor"

    elif (
        missing_important
        or warnings
    ):

        quality = "partial"

    else:

        quality = "good"

    score = {
        "good": 100,
        "partial": 70,
        "poor": 30,
    }.get(
        quality,
        0,
    )

    return {
        "detailQuality": quality,
        "detailQualityScore": score,
        "missingFields": missing_fields,
        "validationWarnings": warnings,
    }


# ============================================================
# HTTP error classification
# ============================================================

def classify_http_error(
    exc: Exception,
) -> str:

    if isinstance(
        exc,
        requests.Timeout,
    ):

        return "timeout"

    if isinstance(
        exc,
        requests.ConnectionError,
    ):

        return "connection_error"

    if isinstance(
        exc,
        requests.HTTPError,
    ):

        response = getattr(
            exc,
            "response",
            None,
        )

        if response is not None:

            status = response.status_code

            if status == 404:
                return "http_404"

            if status in {
                401,
                403,
            }:

                return "http_forbidden"

            if 500 <= status <= 599:

                return "http_5xx"

            return (
                f"http_{status}"
            )

        return "http_error"

    return "request_error"


# ============================================================
# Detail fetch
# ============================================================

def fetch_detail(
    url: str,
    timeout: int = 20,
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # 元URL
    # --------------------------------------------------------

    original_url = str(
        url
    ).strip()

    if not original_url:

        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": "empty_suumo_url",
            "errorType": "invalid_suumo_url",
            "sourceUrl": url,
            "requestUrl": None,
        }

    # --------------------------------------------------------
    # 表示・保存用URL
    #
    # 可能な限り元URLの形を維持する。
    # --------------------------------------------------------

    display_url = (
        preserve_suumo_listing_url(
            original_url
        )
    )

    # --------------------------------------------------------
    # HTTP取得用URL
    #
    # ここでも不要なcanonicalizeはしない。
    # --------------------------------------------------------

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
            "sourceUrl": (
                display_url
                or original_url
            ),
            "requestUrl": None,
        }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": (
            "ja-JP,ja;q=0.9"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Cache-Control": "no-cache",
    }

    # ========================================================
    # HTTP Request
    # ========================================================

    try:

        response = requests.get(
            request_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )

        response.raise_for_status()

        # ----------------------------------------------------
        # Redirect先
        #
        # redirect先も可能な限りそのまま保持する。
        # ----------------------------------------------------

        final_url = (
            preserve_suumo_listing_url(
                response.url
            )
        )

        # ----------------------------------------------------
        # redirect先が個別物件URLでない場合
        #
        # 例えばSUUMO側がcanonical/別ページへ遷移させるケースが
        # あり得るため、まずSUUMOドメインかどうかを確認する。
        #
        # ここでは「redirectされたから即失敗」とはしない。
        # ----------------------------------------------------

        if final_url is None:

            try:

                redirected = urlparse(
                    response.url
                )

                redirected_hostname = (
                    redirected.hostname
                    or ""
                ).lower()

            except Exception:

                redirected_hostname = ""

            if (
                redirected_hostname
                not in SUUMO_HOSTS
            ):

                return {
                    "success": False,
                    "fetchedAt": now_iso(),
                    "detail": None,
                    "error": "redirected_outside_suumo",
                    "errorType": "redirected_outside_suumo",
                    "sourceUrl": (
                        display_url
                        or original_url
                    ),
                    "requestUrl": request_url,
                    "finalUrl": response.url,
                }

            # ------------------------------------------------
            # SUUMO内リダイレクトであれば、
            # 個別物件URLとして再利用できない場合でも
            # 実際に取得したURLを保持する。
            # ------------------------------------------------

            final_url = str(
                response.url
            ).strip()

    except requests.RequestException as exc:

        error_type = (
            classify_http_error(
                exc
            )
        )

        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": str(exc),
            "errorType": error_type,
            "sourceUrl": (
                display_url
                or original_url
            ),
            "requestUrl": request_url,
        }

    except Exception as exc:

        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": str(exc),
            "errorType": "unexpected_error",
            "sourceUrl": (
                display_url
                or original_url
            ),
            "requestUrl": request_url,
        }

    # ========================================================
    # Encoding
    # ========================================================

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

    # ========================================================
    # Empty response detection
    # ========================================================

    html = response.text or ""

    if len(html.strip()) < 500:

        return {
            "success": False,
            "fetchedAt": now_iso(),
            "detail": None,
            "error": "empty_or_too_short_html",
            "errorType": "empty_html",
            "sourceUrl": (
                display_url
                or original_url
            ),
            "requestUrl": request_url,
            "finalUrl": final_url,
        }

    # ========================================================
    # Parse
    # ========================================================

    raw_soup = BeautifulSoup(
        html,
        "html.parser",
    )

    clean_soup = (
        remove_unwanted_sections(
            raw_soup
        )
    )

    pairs = (
        collect_label_value_pairs(
            clean_soup
        )
    )

    blocks = (
        extract_text_blocks(
            clean_soup
        )
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

    fetched_at = now_iso()

    # ========================================================
    # Basic
    # ========================================================

    title = extract_title(
        clean_soup
    )

    # ========================================================
    # Price
    # ========================================================

    price, price_text = (
        extract_price_from_blocks(
            blocks
        )
    )

    if price is None:

        result = find_value_by_keywords(
            pairs,
            [
                "販売価格",
                "価格",
            ],
        )

        if result:

            value, _ = result

            price = parse_price(
                value
            )

            price_text = value

    # ========================================================
    # Address
    # ========================================================

    address = extract_address(
        pairs,
        blocks,
        page_text,
    )

    # ========================================================
    # Land area
    # ========================================================

    (
        land_area,
        land_area_text,
    ) = extract_land_area(
        pairs,
        blocks,
        page_text,
    )

    # ========================================================
    # Building area
    # ========================================================

    (
        building_area,
        building_area_text,
        building_area_type,
    ) = extract_building_area(
        pairs,
        blocks,
        page_text,
    )

    # ========================================================
    # Layout
    # ========================================================

    (
        layout,
        layout_raw,
    ) = extract_layout(
        pairs,
        blocks,
    )

    # ========================================================
    # Construction
    # ========================================================

    (
        construction_month,
        construction_precision,
        construction_text,
    ) = extract_construction(
        pairs,
        blocks,
        page_text,
    )

    # ========================================================
    # Construction derived values
    # ========================================================

    (
        construction_year,
        construction_month_number,
    ) = parse_construction_date(
        construction_month
    )

    construction_age_years = (
        calculate_construction_age_years(
            construction_month
        )
    )

    # ========================================================
    # Transportation
    # ========================================================

    station_info = (
        extract_station_info(
            blocks,
            page_text,
        )
    )

    # ========================================================
    # Information dates
    # ========================================================

    information_dates = (
        extract_information_dates(
            page_text
        )
    )

    # ========================================================
    # Detail object
    # ========================================================

    detail: Dict[str, Any] = {

        # ----------------------------------------------------
        # Parser
        # ----------------------------------------------------

        "detailParserVersion":
            DETAIL_PARSER_VERSION,

        # ----------------------------------------------------
        # Basic
        # ----------------------------------------------------

        "title":
            title,

        # ----------------------------------------------------
        # URL
        #
        # sourceUrl:
        #   保存・表示するためのURL
        #
        # requestUrl:
        #   HTTP取得に使用したURL
        #
        # finalUrl:
        #   実際のレスポンスURL
        # ----------------------------------------------------

        "sourceUrl":
            display_url
            or original_url,

        "requestUrl":
            request_url,

        "finalUrl":
            final_url,

        # ----------------------------------------------------
        # Price
        # ----------------------------------------------------

        "price":
            price,

        "priceText":
            clean_text(
                price_text
            ),

        # ----------------------------------------------------
        # Address
        # ----------------------------------------------------

        "address":
            address,

        # ----------------------------------------------------
        # Land
        # ----------------------------------------------------

        "landAreaM2":
            land_area,

        "landAreaText":
            clean_text(
                land_area_text
            ),

        # ----------------------------------------------------
        # Building
        # ----------------------------------------------------

        "buildingAreaM2":
            building_area,

        "buildingAreaText":
            clean_text(
                building_area_text
            ),

        "buildingAreaType":
            building_area_type,

        # ----------------------------------------------------
        # Layout
        # ----------------------------------------------------

        "layout":
            layout,

        "layoutRaw":
            layout_raw,

        # ----------------------------------------------------
        # Construction
        # ----------------------------------------------------

        "constructionMonth":
            construction_month,

        "constructionMonthPrecision":
            construction_precision,

        "constructionText":
            clean_text(
                construction_text
            ),

        "constructionYear":
            construction_year,

        "constructionMonthNumber":
            construction_month_number,

        "constructionAgeYears":
            construction_age_years,

        # ----------------------------------------------------
        # Transportation
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Information dates
        # ----------------------------------------------------

        "informationDate":
            information_dates.get(
                "informationDate"
            ),

        "nextUpdateDate":
            information_dates.get(
                "nextUpdateDate"
            ),

        # ----------------------------------------------------
        # Debug / audit
        # ----------------------------------------------------

        "labelValuePairs":
            pairs,

        "textBlocks":
            blocks,

        # ----------------------------------------------------
        # Fetch metadata
        # ----------------------------------------------------

        "fetchedAt":
            fetched_at,

        "httpStatus":
            response.status_code,

        "htmlLength":
            len(html),
    }

    # ========================================================
    # Quality
    # ========================================================

    detail.update(
        evaluate_detail_quality(
            detail
        )
    )

    return {
        "success": True,
        "fetchedAt": fetched_at,
        "detail": detail,
        "error": None,
        "errorType": None,

        # ----------------------------------------------------
        # IMPORTANT:
        # sourceUrlは加工済みcanonical URLではなく、
        # 表示・再利用用URLを返す。
        # ----------------------------------------------------

        "sourceUrl": (
            display_url
            or original_url
        ),

        "requestUrl":
            request_url,

        "finalUrl":
            final_url,
    }


# ============================================================
# Adapter
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
            config or {}
        )

        self.root_path = (
            root_path
        )

        self.extra_kwargs = (
            kwargs
        )

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

    def wait(self):

        if self.interval_seconds <= 0:
            return

        time.sleep(
            self.interval_seconds
        )

    def fetch_detail(
        self,
        url: str,
    ) -> Dict[str, Any]:

        return fetch_detail(
            url,
            timeout=self.timeout,
        )