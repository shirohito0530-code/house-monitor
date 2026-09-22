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

    # =====================================================
    # 定数
    # =====================================================

    SUUMO_HOST = "suumo.jp"

    # 中古戸建
    USED_HOUSE_PATH = "/chukoikkodate/"

    # 新築戸建
    NEW_HOUSE_PATH = "/ikkodate/"

    # 個別物件ID
    LISTING_ID_PATTERN = re.compile(
        r"/nc_[0-9]+/?$",
        re.IGNORECASE
    )

    # =====================================================
    # 初期化
    # =====================================================

    def __init__(self, config, root_path):
        super().__init__(config)

        self.root_path = Path(root_path)

        self.timeout = int(
            self.config.get(
                "timeout",
                20
            )
        )

        self.max_pages = int(
            self.config.get(
                "maxPagesPerRun",
                3
            )
        )

        self.interval = int(
            self.config.get(
                "intervalSeconds",
                5
            )
        )

        # -------------------------------------------------
        # 築年数条件
        #
        # 優先順位:
        # 1. maxBuiltAgeYears
        # 2. maxBuildingAgeYears
        #
        # 例:
        # "maxBuiltAgeYears": 20
        #
        # 2026年の場合:
        # 2006年以降を検索段階の目安とする。
        #
        # 最終的な判定は main.py 側で
        # 詳細ページの築年月を使って再判定する。
        # -------------------------------------------------

        max_building_age = self.config.get(
            "maxBuiltAgeYears"
        )

        if max_building_age is None:
            max_building_age = self.config.get(
                "maxBuildingAgeYears"
            )

        self.max_building_age_years = None
        self.min_built_year = None

        if max_building_age is not None:
            try:
                age = int(
                    max_building_age
                )

                if age >= 0:
                    self.max_building_age_years = age

                    self.min_built_year = (
                        datetime.now().year
                        - age
                    )

            except (
                TypeError,
                ValueError
            ):
                print(
                    "築年数条件が不正です: "
                    f"{max_building_age}"
                )

        if self.min_built_year is not None:
            print(
                "SUUMO検索段階の築年数フィルター: "
                f"{self.min_built_year}年以降 "
                f"(築{self.max_building_age_years}年以内の目安)"
            )

    # =====================================================
    # 検索URL読み込み
    # =====================================================

    def load_search_urls(self):
        """
        config/search_urls.jsonから
        SUUMO検索URLを読み込む。
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
                "検索URL設定ファイルのJSONが不正です: "
                f"{error}"
            )
            return []

        search_urls = data.get(
            "suumo_search_urls",
            []
        )

        if not isinstance(
            search_urls,
            list
        ):
            print(
                "suumo_search_urlsは配列で指定してください"
            )
            return []

        return search_urls

    # =====================================================
    # URL判定
    # =====================================================

    def is_valid_url(self, url):
        """
        SUUMOの正規URLか判定する。
        """

        if not url:
            return False

        try:
            parsed = urlparse(url)

        except ValueError:
            return False

        if parsed.scheme.lower() != "https":
            return False

        hostname = (
            parsed.hostname or ""
        ).lower()

        return (
            hostname == self.SUUMO_HOST
            or hostname.endswith(
                "." + self.SUUMO_HOST
            )
        )

    def normalize_url(
        self,
        url,
        base_url
    ):
        """
        URLを絶対URLに変換する。

        個別物件URLについては、

        - SUUMO正規ドメインのみ
        - クエリパラメータ削除
        - フラグメント削除
        - 個別物件URL末尾スラッシュ統一

        とする。
        """

        if not url:
            return None

        url = url.strip()

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

        try:
            parsed = urlparse(
                absolute_url
            )

        except ValueError:
            return None

        path = parsed.path

        # 個別物件URLの末尾スラッシュを統一
        if self.LISTING_ID_PATTERN.search(
            path
        ):
            if not path.endswith("/"):
                path += "/"

        normalized = urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                "",
                "",
                ""
            )
        )

        return normalized

    def normalize_search_url(
        self,
        url,
        base_url=None
    ):
        """
        検索ページ用URL。

        個別物件URLとは異なり、
        ページングに使われるクエリパラメータを
        消さない。
        """

        if not url:
            return None

        url = url.strip()

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

        if base_url:
            url = urljoin(
                base_url,
                url
            )

        if not self.is_valid_url(url):
            return None

        try:
            parsed = urlparse(url)

        except ValueError:
            return None

        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.params,
                parsed.query,
                ""
            )
        )

    # =====================================================
    # 個別物件URL判定
    # =====================================================

    def is_individual_listing_url(
        self,
        url
    ):
        """
        SUUMOの個別戸建て物件ページか判定する。

        対応:
        - 中古戸建
          /chukoikkodate/.../nc_xxxxxxxx/

        - 新築戸建
          /ikkodate/.../nc_xxxxxxxx/

        例:
        https://suumo.jp/chukoikkodate/chiba/sc_nagareyama/nc_21634932/

        https://suumo.jp/ikkodate/chiba/sc_nagareyama/nc_20848620/
        """

        if not url:
            return False

        try:
            parsed = urlparse(url)

        except ValueError:
            return False

        if parsed.scheme.lower() != "https":
            return False

        hostname = (
            parsed.hostname or ""
        ).lower()

        if hostname != self.SUUMO_HOST:
            return False

        path = parsed.path.lower()

        is_house_path = (
            path.startswith(
                self.USED_HOUSE_PATH
            )
            or
            path.startswith(
                self.NEW_HOUSE_PATH
            )
        )

        if not is_house_path:
            return False

        if not self.LISTING_ID_PATTERN.search(
            path
        ):
            return False

        return True

    def get_property_type_from_url(
        self,
        url
    ):
        """
        URLから物件種別を推定する。

        戻り値:
        - 中古戸建
        - 新築戸建
        - None
        """

        if not url:
            return None

        try:
            parsed = urlparse(url)

        except ValueError:
            return None

        path = parsed.path.lower()

        if path.startswith(
            self.USED_HOUSE_PATH
        ):
            return "中古戸建"

        if path.startswith(
            self.NEW_HOUSE_PATH
        ):
            return "新築戸建"

        return None

    # =====================================================
    # HTTP
    # =====================================================

    def fetch_search_page(
        self,
        url
    ):
        """
        SUUMO検索ページのHTMLを取得する。
        """

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; HouseMonitor/1.0)"
            ),
            "Accept-Language": (
                "ja,en;q=0.8"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            )
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

    def extract_built_year(
        self,
        text
    ):
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
                year = int(
                    match.group(1)
                )

                # 明らかに不正な年を除外
                current_year = (
                    datetime.now().year
                )

                if (
                    1800 <= year
                    <= current_year + 5
                ):
                    return year

            except ValueError:
                pass

        # -------------------------------------------------
        # 「築15年」形式
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

                if age < 0:
                    return None

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
        property_type=None
    ):
        """
        検索段階の築年数条件を判定する。

        重要:
        検索結果カードでは築年月の月まで
        正確に取得できない場合があるため、
        ここでは「検索候補を減らすための一次判定」とする。

        最終判定はmain.pyで
        個別物件詳細の築年月を使って実施する。

        - 条件なし → True
        - 新築戸建 → True
        - 築年不明 → True
        - 条件内 → True
        - 古すぎる → False
        """

        if self.min_built_year is None:
            return True

        # 新築は築年数条件で除外しない
        if property_type == "新築戸建":
            return True

        # 築年不明は安全側で残す
        if built_year is None:
            return True

        return (
            built_year
            >= self.min_built_year
        )

    # =====================================================
    # 物件カード抽出
    # =====================================================

    def get_card_text(
        self,
        link
    ):
        """
        SUUMOの物件カード全体から
        テキストを取得する。

        HTML構造変更に備えて、
        複数候補を試す。
        """

        if link is None:
            return ""

        # -------------------------------------------------
        # 優先的に物件カードらしい親要素を探す
        # -------------------------------------------------

        card = (
            link.find_parent(
                class_=re.compile(
                    r"(cassette|property|result|item|house)",
                    re.I
                )
            )
        )

        if card is not None:
            text = card.get_text(
                " ",
                strip=True
            )

            if text:
                return text

        # -------------------------------------------------
        # 親要素
        # -------------------------------------------------

        parent = link.parent

        if parent is not None:
            text = parent.get_text(
                " ",
                strip=True
            )

            if text:
                return text

        # -------------------------------------------------
        # 最後にリンク自身
        # -------------------------------------------------

        return link.get_text(
            " ",
            strip=True
        )

    def extract_listing_candidates(
        self,
        html,
        base_url
    ):
        """
        検索結果HTMLから
        個別物件候補を抽出する。

        戻り値:

        [
            {
                "url": "...",
                "builtYear": 2010,
                "cardText": "...",
                "propertyType": "中古戸建"
            }
        ]

        築年月が検索結果から
        取得できない場合はbuiltYear=None。
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

            href = link.get(
                "href"
            )

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

            property_type = (
                self.get_property_type_from_url(
                    normalized_url
                )
            )

            card_text = (
                self.get_card_text(
                    link
                )
            )

            built_year = (
                self.extract_built_year(
                    card_text
                )
            )

            results.append(
                {
                    "url": normalized_url,
                    "builtYear": built_year,
                    "cardText": card_text,
                    "propertyType": property_type
                }
            )

        return results

    # =====================================================
    # 物件種別判定
    # =====================================================

    def is_property_type_allowed(
        self,
        property_type,
        target_property_type=None
    ):
        """
        検索URL側で指定された
        propertyTypeとURL上の種別が一致するか判定。

        target_property_typeがない場合は許可。

        URLから種別が判定できない場合も、
        誤除外防止のため許可。
        """

        if not target_property_type:
            return True

        if property_type is None:
            return True

        normalized_target = (
            str(target_property_type)
            .strip()
        )

        # -------------------------------------------------
        # 中古戸建
        # -------------------------------------------------

        if normalized_target == "中古戸建":
            return property_type == "中古戸建"

        # -------------------------------------------------
        # 新築戸建
        # -------------------------------------------------

        if normalized_target == "新築戸建":
            return property_type == "新築戸建"

        # -------------------------------------------------
        # 不明な設定値は誤除外しない
        # -------------------------------------------------

        return True

    # =====================================================
    # 検索結果からURL抽出
    # =====================================================

    def extract_listing_urls(
        self,
        html,
        base_url,
        target_property_type=None
    ):
        """
        検索結果HTMLから
        個別戸建て物件URLだけを抽出する。

        築年数条件が設定されている場合:

        1. 検索結果カードで築年が分かる
           → 古い物件を除外

        2. 築年が分からない
           → 残す

        3. 新築戸建
           → 築年数条件では除外しない

        最終的な築年数判定は
        main.py側の詳細ページ判定で行う。
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
        property_type_filtered = 0

        for candidate in candidates:

            url = candidate.get(
                "url"
            )

            built_year = candidate.get(
                "builtYear"
            )

            property_type = candidate.get(
                "propertyType"
            )

            # -------------------------------------------------
            # 物件種別
            # -------------------------------------------------

            if not self.is_property_type_allowed(
                property_type,
                target_property_type
            ):
                property_type_filtered += 1

                print(
                    "物件種別条件で除外: "
                    f"{url} "
                    f"(種別={property_type}, "
                    f"指定={target_property_type})"
                )

                continue

            # -------------------------------------------------
            # 築年不明
            # -------------------------------------------------

            if built_year is None:
                unknown_year += 1

                results.append(
                    url
                )

                continue

            # -------------------------------------------------
            # 築年数条件
            # -------------------------------------------------

            if not self.is_within_building_age(
                built_year,
                property_type
            ):
                filtered_out += 1

                print(
                    "築年数条件で除外: "
                    f"{url} "
                    f"(築年={built_year})"
                )

                continue

            results.append(
                url
            )

        print(
            "SUUMO検索結果フィルター: "
            f"候補 {len(candidates)}件 / "
            f"採用 {len(results)}件 / "
            f"築年除外 {filtered_out}件 / "
            f"築年不明 {unknown_year}件 / "
            f"種別除外 {property_type_filtered}件"
        )

        return sorted(
            set(results)
        )

    # =====================================================
    # 次ページURL抽出
    # =====================================================

    def extract_next_page_url(
        self,
        html,
        current_url
    ):
        """
        SUUMO検索結果から
        次ページURLを取得する。

        HTML構造変更に備えて、

        - rel="next"
        - 「次へ」
        - 「次のページ」

        を順に確認する。

        見つからなければNone。
        """

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # -------------------------------------------------
        # rel="next"
        # -------------------------------------------------

        next_link = soup.select_one(
            'a[rel="next"]'
        )

        if next_link:
            href = next_link.get(
                "href"
            )

            normalized = (
                self.normalize_search_url(
                    href,
                    current_url
                )
            )

            if normalized:
                return normalized

        # -------------------------------------------------
        # aria-label等
        # -------------------------------------------------

        for link in soup.select(
            "a[href]"
        ):

            href = link.get(
                "href"
            )

            if not href:
                continue

            text = link.get_text(
                " ",
                strip=True
            )

            aria_label = (
                link.get(
                    "aria-label",
                    ""
                )
            )

            title = (
                link.get(
                    "title",
                    ""
                )
            )

            combined = (
                f"{text} "
                f"{aria_label} "
                f"{title}"
            )

            if (
                "次へ" in combined
                or
                "次のページ" in combined
            ):
                normalized = (
                    self.normalize_search_url(
                        href,
                        current_url
                    )
                )

                if normalized:
                    return normalized

        return None

    # =====================================================
    # 1検索URLの巡回
    # =====================================================

    def crawl_search_target(
        self,
        target
    ):
        """
        1つの検索条件URLについて
        最大max_pagesページを巡回する。

        戻り値:
        [
            {
                "url": "...",
                "builtYear": ...,
                "cardText": "...",
                "propertyType": "..."
            }
        ]
        """

        start_url = target.get(
            "url"
        )

        target_property_type = target.get(
            "propertyType"
        )

        if not start_url:
            return []

        current_url = (
            self.normalize_search_url(
                start_url
            )
        )

        if not current_url:
            return []

        all_candidates = []

        seen_page_urls = set()

        for page_number in range(
            1,
            self.max_pages + 1
        ):

            if not current_url:
                break

            if current_url in seen_page_urls:
                print(
                    "検索ページのループを検出。停止: "
                    f"{current_url}"
                )
                break

            seen_page_urls.add(
                current_url
            )

            print(
                "SUUMO検索ページ取得: "
                f"{page_number}/{self.max_pages} "
                f"{current_url}"
            )

            try:
                html = self.fetch_search_page(
                    current_url
                )

            except requests.HTTPError as error:
                print(
                    f"HTTP ERROR: "
                    f"{current_url} / {error}"
                )
                break

            except requests.RequestException as error:
                print(
                    f"REQUEST ERROR: "
                    f"{current_url} / {error}"
                )
                break

            except Exception as error:
                print(
                    f"UNEXPECTED ERROR: "
                    f"{current_url} / {error}"
                )
                break

            candidates = (
                self.extract_listing_candidates(
                    html,
                    current_url
                )
            )

            # -------------------------------------------------
            # ページ内フィルター
            # -------------------------------------------------

            accepted = 0
            rejected_age = 0
            rejected_type = 0

            for candidate in candidates:

                url = candidate.get(
                    "url"
                )

                property_type = candidate.get(
                    "propertyType"
                )

                built_year = candidate.get(
                    "builtYear"
                )

                # 物件種別
                if not self.is_property_type_allowed(
                    property_type,
                    target_property_type
                ):
                    rejected_type += 1
                    continue

                # 築年数
                if not self.is_within_building_age(
                    built_year,
                    property_type
                ):
                    rejected_age += 1

                    print(
                        "築年数条件で除外: "
                        f"{url} "
                        f"(築年={built_year})"
                    )

                    continue

                all_candidates.append(
                    candidate
                )

                accepted += 1

            print(
                f"検索ページ結果: "
                f"{len(candidates)}件 / "
                f"採用 {accepted}件 / "
                f"築年除外 {rejected_age}件 / "
                f"種別除外 {rejected_type}件"
            )

            # -------------------------------------------------
            # max_pagesに達したら終了
            # -------------------------------------------------

            if page_number >= self.max_pages:
                break

            # -------------------------------------------------
            # 次ページ
            # -------------------------------------------------

            next_url = (
                self.extract_next_page_url(
                    html,
                    current_url
                )
            )

            if not next_url:
                break

            if next_url == current_url:
                break

            current_url = next_url

            time.sleep(
                self.interval
            )

        return all_candidates

    # =====================================================
    # メイン検索
    # =====================================================

    def search(
        self,
        search_config
    ):
        """
        設定されたSUUMO検索URLを巡回し、
        個別物件を返す。

        同一物件が複数検索条件・複数ページに
        出現してもURL単位で重複排除する。
        """

        search_targets = (
            self.load_search_urls()
        )

        properties = []

        seen_urls = set()

        total_candidates = 0
        total_added = 0

        for target in search_targets:

            if not isinstance(
                target,
                dict
            ):
                continue

            url = target.get(
                "url"
            )

            if not url:
                continue

            if not self.is_valid_url(
                url
            ):
                print(
                    f"無効な検索URLをスキップ: {url}"
                )
                continue

            target_property_type = target.get(
                "propertyType"
            )

            target_area = target.get(
                "area"
            )

            print(
                "----------------------------------------"
            )

            print(
                "SUUMO検索開始: "
                f"{url}"
            )

            if target_area:
                print(
                    f"検索エリア: {target_area}"
                )

            if target_property_type:
                print(
                    f"検索物件種別: "
                    f"{target_property_type}"
                )

            candidates = (
                self.crawl_search_target(
                    target
                )
            )

            total_candidates += len(
                candidates
            )

            new_count = 0

            for candidate in candidates:

                listing_url = candidate.get(
                    "url"
                )

                if not listing_url:
                    continue

                if listing_url in seen_urls:
                    continue

                seen_urls.add(
                    listing_url
                )

                property_type = candidate.get(
                    "propertyType"
                )

                # -------------------------------------------------
                # 物件データ
                #
                # builtYear / cardText は
                # main.py側で再利用できるよう保持する。
                # -------------------------------------------------

                properties.append(
                    {
                        "source": "suumo",
                        "sourceUrl": listing_url,
                        "searchArea": target.get(
                            "area"
                        ),
                        "searchPropertyType": (
                            target.get(
                                "propertyType"
                            )
                        ),
                        "searchBuiltYear": (
                            candidate.get(
                                "builtYear"
                            )
                        ),
                        "searchCardText": (
                            candidate.get(
                                "cardText"
                            )
                        ),
                        "searchDetectedPropertyType": (
                            property_type
                        )
                    }
                )

                new_count += 1
                total_added += 1

            print(
                f"SUUMO検索完了: "
                f"候補 {len(candidates)}件 / "
                f"新規 {new_count}件"
            )

            time.sleep(
                self.interval
            )

        # =================================================
        # 最終重複排除
        # =================================================

        unique_properties = []
        final_seen = set()

        for property_data in properties:

            source_url = property_data.get(
                "sourceUrl"
            )

            if not source_url:
                continue

            if source_url in final_seen:
                continue

            final_seen.add(
                source_url
            )

            unique_properties.append(
                property_data
            )

        # =================================================
        # 集計
        # =================================================

        used_count = sum(
            1
            for property_data
            in unique_properties
            if property_data.get(
                "searchDetectedPropertyType"
            ) == "中古戸建"
        )

        new_count = sum(
            1
            for property_data
            in unique_properties
            if property_data.get(
                "searchDetectedPropertyType"
            ) == "新築戸建"
        )

        unknown_type_count = (
            len(unique_properties)
            - used_count
            - new_count
        )

        print(
            "========================================"
        )

        print(
            "SUUMO検索完了"
        )

        print(
            f"個別物件: "
            f"{len(unique_properties)}件"
        )

        print(
            f"中古戸建: "
            f"{used_count}件"
        )

        print(
            f"新築戸建: "
            f"{new_count}件"
        )

        if unknown_type_count > 0:
            print(
                f"種別不明: "
                f"{unknown_type_count}件"
            )

        if self.min_built_year is not None:
            print(
                "検索段階の築年数基準: "
                f"{self.min_built_year}年以降"
            )

        print(
            "========================================"
        )

        return unique_properties
