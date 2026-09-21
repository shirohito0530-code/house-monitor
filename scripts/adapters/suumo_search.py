from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from adapters.base import PropertyAdapter


class SuumoSearchAdapter(PropertyAdapter):

    def __init__(self, config, root_path):
        super().__init__(config)

        self.root_path = Path(root_path)

        self.timeout = int(
            self.config.get("timeout", 20)
        )

        self.max_pages = int(
            self.config.get("maxPagesPerRun", 3)
        )

        self.interval = int(
            self.config.get("intervalSeconds", 5)
        )

    def load_search_urls(self):
        """
        config/search_urls.jsonから
        SUUMO検索URLを読み込む
        """

        path = (
            self.root_path
            / "config"
            / "search_urls.json"
        )

        if not path.exists():
            print(
                f"検索URL設定ファイルがありません: {path}"
            )
            return []

        try:
            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

        except json.JSONDecodeError as error:
            print(
                f"検索URL設定ファイルのJSONが不正です: {error}"
            )
            return []

        search_urls = data.get(
            "suumo_search_urls",
            []
        )

        if not isinstance(search_urls, list):
            print(
                "suumo_search_urlsは配列で指定してください"
            )
            return []

        return search_urls

    def is_valid_url(self, url):
        """
        SUUMOの正規URLか判定する
        """

        try:
            parsed = urlparse(url)

        except ValueError:
            return False

        if parsed.scheme != "https":
            return False

        hostname = (
            parsed.hostname or ""
        ).lower()

        return (
            hostname == "suumo.jp"
            or hostname.endswith(".suumo.jp")
        )

    def normalize_url(self, url, base_url):
        """
        URLを絶対URLに変換する。

        - SUUMOの正規ドメインのみ許可
        - クエリパラメータを削除
        - フラグメントを削除
        - 個別物件URLの末尾にスラッシュを付与
        """

        if not url:
            return None

        url = url.strip()

        # JavaScriptリンクやページ内リンクを除外
        if url.startswith("#"):
            return None

        if url.lower().startswith(
            (
                "javascript:",
                "mailto:",
                "tel:"
            )
        ):
            return None

        absolute_url = urljoin(
            base_url,
            url
        )

        if not self.is_valid_url(
            absolute_url
        ):
            return None

        parsed = urlparse(
            absolute_url
        )

        path = parsed.path

        # 個別物件URLの末尾スラッシュを統一
        if re.search(
            r"/nc_[0-9]+$",
            path.lower()
        ):
            path = path + "/"

        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            "",
            ""
        ))

        return normalized

    def is_individual_listing_url(self, url):
        """
        SUUMOの中古戸建て個別物件ページか判定する。

        対象例:
        https://suumo.jp/chukoikkodate/chiba/sc_nagareyama/nc_21634932/

        除外例:
        https://suumo.jp/chukoikkodate/chiba/sc_nagareyama/
        """

        if not url:
            return False

        try:
            parsed = urlparse(url)

        except ValueError:
            return False

        # HTTPSのみ許可
        if parsed.scheme != "https":
            return False

        # SUUMO本体ドメインに限定
        hostname = (
            parsed.hostname or ""
        ).lower()

        if hostname != "suumo.jp":
            return False

        path = parsed.path.lower()

        # 中古戸建てページに限定
        if not path.startswith(
            "/chukoikkodate/"
        ):
            return False

        # nc_数字の個別物件URLに限定
        pattern = (
            r"^/chukoikkodate/"
            r".*/nc_[0-9]+/?$"
        )

        if not re.match(
            pattern,
            path
        ):
            return False

        return True

    def fetch_search_page(self, url):
        """
        SUUMO検索ページのHTMLを取得する
        """

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; HouseMonitor/1.0)"
            ),
            "Accept-Language": "ja,en;q=0.8"
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=self.timeout
        )

        response.raise_for_status()

        return response.text

    def extract_listing_urls(self, html, base_url):
        """
        検索結果HTMLから
        中古戸建ての個別物件URLだけを抽出する。

        抽出対象:
        - SUUMOドメイン
        - /chukoikkodate/ 配下
        - /nc_数字/形式

        除外対象:
        - 検索結果ページ
        - エリア集約ページ
        - 条件検索ページ
        - 他の物件種別
        - 重複URL
        """

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        results = set()

        for link in soup.select("a[href]"):
            href = link.get("href")

            if not href:
                continue

            normalized_url = self.normalize_url(
                href,
                base_url
            )

            if not normalized_url:
                continue

            if not self.is_individual_listing_url(
                normalized_url
            ):
                continue

            results.add(
                normalized_url
            )

        return sorted(results)

    def search(self, search_config):
        """
        設定されたSUUMO検索URLを巡回し、
        個別物件URLを返す。

        同一物件が複数検索条件に該当する場合、
        URL単位で重複排除する。
        """

        search_targets = (
            self.load_search_urls()
        )

        properties = []

        seen_urls = set()

        for target in search_targets:

            if not isinstance(
                target,
                dict
            ):
                continue

            url = target.get("url")

            if not url:
                continue

            if not self.is_valid_url(url):
                print(
                    f"無効な検索URLをスキップ: {url}"
                )
                continue

            try:
                html = self.fetch_search_page(
                    url
                )

                listing_urls = (
                    self.extract_listing_urls(
                        html,
                        url
                    )
                )

                new_count = 0

                for listing_url in listing_urls:

                    if listing_url in seen_urls:
                        continue

                    seen_urls.add(
                        listing_url
                    )

                    properties.append({
                        "source": "suumo",
                        "sourceUrl": listing_url,
                        "searchArea": target.get(
                            "area"
                        ),
                        "searchPropertyType": target.get(
                            "propertyType"
                        )
                    })

                    new_count += 1

                print(
                    f"SUUMO: {url} "
                    f"→ 抽出 {len(listing_urls)}件 "
                    f"/ 新規 {new_count}件"
                )

                time.sleep(
                    self.interval
                )

            except requests.HTTPError as error:
                print(
                    f"HTTP ERROR: {url} / {error}"
                )

            except requests.RequestException as error:
                print(
                    f"REQUEST ERROR: {url} / {error}"
                )

            except Exception as error:
                print(
                    f"UNEXPECTED ERROR: {url} / {error}"
                )

        print(
            f"SUUMO検索完了: "
            f"個別物件 {len(properties)}件"
        )

        return properties
