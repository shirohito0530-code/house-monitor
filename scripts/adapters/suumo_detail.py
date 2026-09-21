from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; HouseMonitor/1.0; "
        "+https://github.com/)"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


# ------------------------------------------------------------
# 基本ユーティリティ
# ------------------------------------------------------------

def clean_text(value: Any) -> Optional[str]:
    """HTMLテキストを正規化する。"""

    if value is None:
        return None

    text = str(value)

    text = text.replace("\xa0", " ")
    text = text.replace("\u3000", " ")

    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    if not text:
        return None

    return text


def remove_noise_text(value: Optional[str]) -> Optional[str]:
    """SUUMO特有の補助文言などを除去する。"""

    if not value:
        return None

    text = clean_text(value)

    if not text:
        return None

    noise_patterns = [
        r"\[\s*[■□◆◇]\s*周辺環境\s*\]",
        r"\[\s*[■□◆◇]\s*支払シミュレーション\s*\]",
        r"\[\s*[■□◆◇]\s*お問い合わせ\s*\]",
        r"\[\s*[■□◆◇]\s*資料請求\s*\]",
        r"支払シミュレーション",
        r"お問い合わせはこちら",
        r"お気軽にお問い合わせください",
    ]

    for pattern in noise_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text).strip()

    return text or None


def is_promotional_text(value: Optional[str]) -> bool:
    """広告・営業文らしい文字列を判定する。"""

    if not value:
        return True

    text = clean_text(value)
