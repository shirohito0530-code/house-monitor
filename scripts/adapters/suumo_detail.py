from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
import re
import time

import requests
from bs4 import BeautifulSoup


class SuumoDetailAdapter:

    def __init__(self, config=None, root_path=None):
        self.config = config or {}
        self.root_path = Path(root_path) if root_path else Path.cwd()

        self.timeout = int(
            self.config.get("detailTimeoutSeconds", 20)
        )

        self.interval = int(
            self.config.get("detailIntervalSeconds", 10)
        )

        self.user_agent = (
            "Mozilla/5.0 "
            "(compatible; HouseMonitor/1.0)"
        )

    def is_valid_listing_url(self, url):
        """
        SUUMO中古戸建て個別物件URLか確認する。
        """

        if not url:
            return False

        try:
            parsed = urlparse(url)

        except ValueError:
            return False

        if parsed.scheme != "https":
            return False

        hostname = (
            parsed.hostname or ""
        ).lower()

        if hostname != "suumo.jp":
            return False

        path = parsed.path.lower()

        pattern = (
            r"^/chukoikkodate/"
            r".*/nc_[0-9]+/?$"
        )

        return bool(
            re.match(pattern, path)
        )

    def fetch_html(self, url):
        """
        個別物件ページのHTMLを取得する。
        """

        if not self.is_valid_listing_url(url):
            raise ValueError(
                f"無効なSUUMO物件URLです: {url}"
            )

        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "ja,en;q=0.8"
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=self.timeout
        )

        response.raise_for_status()

        return response.text

    def normalize_text(self, value):
        """
        空白や改行を整理する。
        """

        if value is None:
            return None

        text = str(value)

        text = re.sub(
            r"\s+",
            " ",
            text
        )

        text = text.strip()

        return text or None

    def extract_all_text(self, soup):
        """
        ページ内のテキストを1つにまとめる。
        """

        return self.normalize_text(
            soup.get_text(
                " ",
                strip=True
            )
        ) or ""

    def find_value_by_label(self, soup, labels):
        """
        テーブルや定義リストなどから、
        ラベルに対応する値を抽出する。

        HTML構造の違いに対応するため、
        複数の方法を試す。
        """

        if isinstance(labels, str):
            labels = [labels]

        # tableのth/tdを確認
        for row in soup.select("tr"):

            cells = row.find_all(
                ["th", "td"]
            )

            if len(cells) < 2:
                continue

            label = self.normalize_text(
                cells[0].get_text(
                    " ",
                    strip=True
                )
            )

            if not label:
                continue

            for target in labels:

                if target in label:

                    value = self.normalize_text(
                        cells[1].get_text(
                            " ",
                            strip=True
                        )
                    )

                    if value:
                        return value

        # dt/ddを確認
        for dt in soup.find_all("dt"):

            label = self.normalize_text(
                dt.get_text(
                    " ",
                    strip=True
                )
            )

            if not label:
                continue

            for target in labels:

                if target in label:

                    dd = dt.find_next_sibling("dd")

                    if dd:
                        value = self.normalize_text(
                            dd.get_text(
                                " ",
                                strip=True
                            )
                        )

                        if value:
                            return value

        # ページ内テキストから簡易抽出
        page_text = self.extract_all_text(
            soup
        )

        for target in labels:

            pattern = (
                re.escape(target)
                + r"\s*[:：]?\s*"
                r"([^\s]+)"
            )

            match = re.search(
                pattern,
                page_text
            )

            if match:
                return self.normalize_text(
                    match.group(1)
                )

        return None

    def extract_price(self, soup):
        """
        価格を抽出する。
        """

        value = self.find_value_by_label(
            soup,
            [
                "販売価格",
                "価格",
                "販売額"
            ]
        )

        if value:
            return value

        page_text = self.extract_all_text(
            soup
        )

        match = re.search(
            r"([0-9,]+)\s*万円",
            page_text
        )

        if match:
            return (
                match.group(1)
                + "万円"
            )

        return None

    def extract_detail(self, html):
        """
        HTMLから物件詳細を抽出する。

        取得できない値はNoneとする。
        """

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        detail = {
            "price": self.extract_price(
                soup
            ),
            "address": self.find_value_by_label(
                soup,
                [
                    "所在地",
                    "住所"
                ]
            ),
            "landArea": self.find_value_by_label(
                soup,
                [
                    "土地面積",
                    "土地"
                ]
            ),
            "buildingArea": self.find_value_by_label(
                soup,
                [
                    "建物面積",
                    "建物"
                ]
            ),
            "layout": self.find_value_by_label(
                soup,
                [
                    "間取り",
                    "間取"
                ]
            ),
            "buildYear": self.find_value_by_label(
                soup,
                [
                    "築年月",
                    "築年",
                    "建築年月"
                ]
            ),
            "station": self.find_value_by_label(
                soup,
                [
                    "沿線・駅",
                    "最寄り駅",
                    "駅"
                ]
            ),
            "walkMinutes": self.find_value_by_label(
                soup,
                [
                    "徒歩",
                    "駅徒歩"
                ]
            ),
            "builder": self.find_value_by_label(
                soup,
                [
                    "建築会社",
                    "施工会社",
                    "ハウスメーカー"
                ]
            )
        }

        return detail

    def fetch_detail(self, url):
        """
        URLから詳細情報を取得する。
        """

        fetched_at = datetime.now(
            timezone.utc
        ).isoformat()

        try:

            html = self.fetch_html(
                url
            )

            detail = self.extract_detail(
                html
            )

            return {
                "success": True,
                "fetchedAt": fetched_at,
                "detail": detail,
                "error": None
            }

        except requests.HTTPError as error:

            return {
                "success": False,
                "fetchedAt": fetched_at,
                "detail": {},
                "error": f"HTTPError: {error}"
            }

        except requests.RequestException as error:

            return {
                "success": False,
                "fetchedAt": fetched_at,
                "detail": {},
                "error": f"RequestException: {error}"
            }

        except Exception as error:

            return {
                "success": False,
                "fetchedAt": fetched_at,
                "detail": {},
                "error": f"UnexpectedError: {error}"
            }

    def wait(self):
        """
        取得間隔を空ける。
        """

        if self.interval > 0:
            time.sleep(
                self.interval
            )
