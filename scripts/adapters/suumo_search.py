from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import json
import re
import time
from datetime import datetime

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

        # -------------------------------------------------
        # 築年数条件
        #
        # 例:
        # "maxBuildingAgeYears": 20
        #
        # 2026年なら2006年以降を対象
        # -------------------------------------------------
        max_building_age = self.config.get(
            "maxBuildingAgeYears"
        )

        if max_building_age is None:
            self.min_built_year = None
        else:
            try:
                self.min_built_year = (
                    datetime.now().year
                    - int(max_building_age)
                )
            except (
                TypeError,
                ValueError
            ):
                self.min_built_year = None

        if self.min_built_year is not None:
            print(
                "築年数フィルター: "
                f"{self.min_built_year}年以降"
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

    # =====================================================
    # 築年月判定
    # =====================================================

    def extract_built_year(self, text):
        """
        検索結果カードのテキストから
        築年月の年を取得する。

        対応例:
        - 2008年
        - 2008年3月
        - 2008年12月築
        - 築15年

        年が取得できない場合はNone。
        """

        if not text:
            return None

        text = re.sub(
            r"\s+",
            " ",
            str(text)
        ).strip()

        # -------------------------------------------------
        # 「2008年」形式
        # -------------------------------------------------
        match = re.search(
            r"(19\d{2}|20\d{2})年",
            text
        )

        if match:
            try:
                return int(
                    match.group(1)
                )
            except ValueError:
                pass

        # -------------------------------------------------
        # 「築15年」形式
        #
        # 検索結果に築年数だけ表示される場合に対応。
        # 現在年から建築年を逆算する。
        # -------------------------------------------------
        match = re.search(
            r"築\s*(\d+)\s*年",
            text
        )

        if match:
            try:
                age = int(
                    match.group(1)
                )

                return (
                    datetime.now().year
                    - age
                )

            except ValueError:
                pass

        return None

    def is_within_building_age(
        self,
        built_year,
        text=""
    ):
        """
        築年数条件を満たすか判定する。

        - 条件なし → True
        - 築年が取得できない → True
          （検索段階では除外しない）
        - 築年が古い → False
        """

        if self.min_built_year is None:
            return True

        if built_year is None:
            return True

        return (
            built_year >= self.min_built_year
        )

    def extract_listing_candidates(
        self,
        html,
        base_url
    ):
        """
        検索結果HTMLから個別物件候補を抽出する。

        戻り値:
        [
            {
                "url": "...",
                "builtYear": 2010,
                "cardText": "..."
            }
        ]

        築年月が検索結果から取得できない場合は
        builtYear=Noneとして残す。
        """

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        results = []
        seen_urls = set()

        for link in soup.select(
            "a[href]"
        ):
            href = link.get("href")

            if not href:
                continue

            normalized_url = (
                self.normalize_url(
                    href,
                    base_url
                )
            )

            if not normalized_url:
                continue

            if not self.is_individual_listing_url(
                normalized_url
            ):
                continue

            if normalized_url in seen_urls:
                continue

            seen_urls.add(
                normalized_url
            )

            # -------------------------------------------------
            # 物件カード全体を取得
            #
            # SUUMOのHTML構造変更に強くするため、
            # リンク自身だけでなく親要素側のテキストも確認する。
            # -------------------------------------------------
            card = (
                link.find_parent(
                    class_=re.compile(
                        r"(cassette|property|result|item|house)",
                        re.I
                    )
                )
            )

            if card is None:
                card = link.parent

            if card is None:
                card_text = link.get_text(
                    " ",
                    strip=True
                )
            else:
                card_text = card.get_text(
                    " ",
                    strip=True
                )

            built_year = (
                self.extract_built_year(
                    card_text
                )
            )

            results.append({
                "url": normalized_url,
                "builtYear": built_year,
                "cardText": card_text
            })

        return results

    def extract_listing_urls(
        self,
        html,
        base_url
    ):
        """
        検索結果HTMLから
        中古戸建ての個別物件URLだけを抽出する。

        築年数条件が設定されている場合は、
        検索結果カードから築年を取得できた物件について
        古い物件を除外する。

        築年が取得できない物件は、
        誤って優良物件を除外しないため残す。
        """

        candidates = (
            self.extract_listing_candidates(
                html,
                base_url
            )
        )

        results = []

        filtered_out = 0
        unknown_year = 0

        for candidate in candidates:

            built_year = candidate.get(
                "builtYear"
            )

            if built_year is None:
                unknown_year += 1

                results.append(
                    candidate["url"]
                )

                continue

            if not self.is_within_building_age(
                built_year,
                candidate.get(
                    "cardText",
                    ""
                )
            ):
                filtered_out += 1

                print(
                    "築年数条件で除外: "
                    f"{candidate['url']} "
                    f"(築年={built_year})"
                )

                continue

            results.append(
                candidate["url"]
            )

        print(
            "築年数フィルター: "
            f"候補 {len(candidates)}件 / "
            f"採用 {len(results)}件 / "
            f"除外 {filtered_out}件 / "
            f"築年不明 {unknown_year}件"
        )

        return sorted(
            set(results)
        )

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
