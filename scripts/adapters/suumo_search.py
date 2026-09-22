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
    #
    # 例:
    # /nc_12345678
    # /nc_12345678/
    # /nc_12345678?xxx=yyy
    #
    # queryはpathには含まれないため、
    # path末尾の / または文字列末尾を許容する。
    LISTING_ID_PATTERN = re.compile(
        r"/nc_[0-9]+(?:/|$)",
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

        self.interval = float(
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
        # 検索段階では築年の一次フィルターとして使用。
        # 最終判定はmain.py側で詳細ページの
        # constructionMonthを使用する。
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

        想定形式:

        {
          "suumo_search_urls": [
            {
              "area": "柏の葉キャンパス",
              "propertyType": "中古戸建",
              "url": "https://suumo.jp/..."
            }
          ]
        }

        areaは「検索条件上のエリア」であり、
        実際の所在地を保証するものではない。

        実際の所在地はmain.pyが詳細ページの
        addressを使って最終判定する。
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

        # -------------------------------------------------
        # 配列形式
        # -------------------------------------------------

        if isinstance(data, list):
            search_urls = data

        # -------------------------------------------------
        # 通常形式
        # -------------------------------------------------

        elif isinstance(data, dict):
            search_urls = data.get(
                "suumo_search_urls",
                []
            )

            # 念のため旧形式にも対応
            if not search_urls:
                search_urls = data.get(
                    "targets",
                    []
                )

        else:
            print(
                "検索URL設定の形式が不正です"
            )
            return []

        if not isinstance(
            search_urls,
            list
        ):
            print(
                "suumo_search_urlsは配列で指定してください"
            )
            return []

        # -------------------------------------------------
        # enabled=false は除外
        # -------------------------------------------------

        enabled_urls = []

        for target in search_urls:

            if not isinstance(
                target,
                dict
            ):
                continue

            if target.get(
                "enabled",
                True
            ) is False:
                continue

            enabled_urls.append(
                target
            )

        return enabled_urls

    # =====================================================
    # URL判定
    # =====================================================

    def is_valid_url(
        self,
        url
    ):
        """
        SUUMOのURLか判定する。

        重要:
        - http / https の両方を許容
        - www.suumo.jp も許容
        - サブドメインも許容
        - URLを書き換えない
        """

        if not url:
            return False

        try:
            parsed = urlparse(
                str(url).strip()
            )

        except ValueError:
            return False

        if parsed.scheme.lower() not in (
            "http",
            "https"
        ):
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

    # =====================================================
    # 個別物件URL正規化
    # =====================================================

    def normalize_url(
        self,
        url,
        base_url
    ):
        """
        個別物件URLを最小限だけ正規化する。

        重要:
        この関数ではURLを「きれいにする」ことより、
        SUUMOが返したURLを壊さないことを優先する。

        実施する処理:
        - 相対URL → 絶対URL
        - SUUMOドメイン確認
        - http / https の保持
        - pathの保持
        - queryの保持
        - fragmentのみ削除
        - 個別物件URLの末尾スラッシュを統一

        実施しない処理:
        - www.suumo.jpへの強制変更
        - query削除
        - pathの加工
        - 大文字小文字の過度な変換
        - URLの別形式への変換
        """

        if not url:
            return None

        original_url = str(
            url
        ).strip()

        if not original_url:
            return None

        if original_url.startswith(
            "#"
        ):
            return None

        if original_url.lower().startswith(
            (
                "javascript:",
                "mailto:",
                "tel:"
            )
        ):
            return None

        # -------------------------------------------------
        # プロトコル相対URL
        #
        # //www.suumo.jp/...
        # -------------------------------------------------

        if original_url.startswith(
            "//"
        ):
            absolute_url = (
                "https:"
                + original_url
            )

        else:
            absolute_url = urljoin(
                base_url,
                original_url
            )

        # -------------------------------------------------
        # URLとして妥当か確認
        # -------------------------------------------------

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

        path = parsed.path or ""

        # -------------------------------------------------
        # 個別物件IDが含まれる場合だけ
        # 末尾スラッシュを補正
        #
        # 例:
        # /nc_12345678
        # ↓
        # /nc_12345678/
        #
        # queryは保持する。
        # -------------------------------------------------

        if self.LISTING_ID_PATTERN.search(
            path
        ):
            if not path.endswith("/"):
                path += "/"

        # -------------------------------------------------
        # 最小限の再構成
        #
        # queryは絶対に捨てない。
        # fragmentのみ削除。
        # -------------------------------------------------

        normalized = urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                path,
                parsed.params,
                parsed.query,
                ""
            )
        )

        return normalized

    # =====================================================
    # 検索ページURL正規化
    # =====================================================

    def normalize_search_url(
        self,
        url,
        base_url=None
    ):
        """
        検索ページ用URLを最小限だけ正規化する。

        検索ページではページング等に
        query parameterを使用するため、
        queryは保持する。
        """

        if not url:
            return None

        original_url = str(
            url
        ).strip()

        if not original_url:
            return None

        if original_url.startswith(
            "#"
        ):
            return None

        if original_url.lower().startswith(
            (
                "javascript:",
                "mailto:",
                "tel:"
            )
        ):
            return None

        # -------------------------------------------------
        # プロトコル相対URL
        # -------------------------------------------------

        if original_url.startswith(
            "//"
        ):
            absolute_url = (
                "https:"
                + original_url
            )

        elif base_url:
            absolute_url = urljoin(
                base_url,
                original_url
            )

        else:
            absolute_url = original_url

        # -------------------------------------------------
        # URL検証
        # -------------------------------------------------

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

        # -------------------------------------------------
        # queryを保持
        # fragmentのみ削除
        # -------------------------------------------------

        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc,
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

        中古戸建
        /chukoikkodate/.../nc_xxxxxxxx/

        新築戸建
        /ikkodate/.../nc_xxxxxxxx/

        queryが付いていても許容する。
        """

        if not url:
            return False

        try:
            parsed = urlparse(
                str(url)
            )

        except ValueError:
            return False

        # -------------------------------------------------
        # scheme
        # -------------------------------------------------

        if parsed.scheme.lower() not in (
            "http",
            "https"
        ):
            return False

        # -------------------------------------------------
        # host
        # -------------------------------------------------

        hostname = (
            parsed.hostname or ""
        ).lower()

        if not (
            hostname == self.SUUMO_HOST
            or hostname.endswith(
                "." + self.SUUMO_HOST
            )
        ):
            return False

        # -------------------------------------------------
        # path
        # -------------------------------------------------

        path = parsed.path or ""

        path_lower = path.lower()

        is_house_path = (
            path_lower.startswith(
                self.USED_HOUSE_PATH
            )
            or
            path_lower.startswith(
                self.NEW_HOUSE_PATH
            )
        )

        if not is_house_path:
            return False

        # -------------------------------------------------
        # 個別物件ID
        #
        # queryはpathに含まれないため、
        # query付きURLも正常に判定できる。
        # -------------------------------------------------

        if not self.LISTING_ID_PATTERN.search(
            path
        ):
            return False

        return True

    # =====================================================
    # 物件種別判定
    # =====================================================

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
            parsed = urlparse(
                str(url)
            )

        except ValueError:
            return None

        path = (
            parsed.path or ""
        ).lower()

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
            ),
            "Cache-Control": "no-cache"
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=True
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

    # =====================================================
    # 築年数条件
    # =====================================================

    def is_within_building_age(
        self,
        built_year,
        property_type=None
    ):
        """
        検索段階の築年数条件を判定する。

        これは一次フィルター。

        最終判定はmain.py側で、
        詳細ページのconstructionMonthを使って行う。

        条件:
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

        # 築年不明は残す
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

        HTML構造変更に備えて
        複数候補を試す。
        """

        if link is None:
            return ""

        # -------------------------------------------------
        # 物件カードらしい親要素
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
        # リンク自身
        # -------------------------------------------------

        return link.get_text(
            " ",
            strip=True
        )

    # =====================================================
    # 物件候補抽出
    # =====================================================

    def extract_listing_candidates(
        self,
        html,
        base_url
    ):
        """
        検索結果HTMLから
        個別物件候補を抽出する。

        URLはここで破壊的なcanonicalizeを行わない。

        戻り値:

        [
            {
                "url": "...",
                "builtYear": 2010,
                "cardText": "...",
                "propertyType": "中古戸建"
            }
        ]
        """

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        results = []

        # 同一ページ内の重複排除
        seen_urls = set()

        for link in soup.select(
            "a[href]"
        ):

            href = link.get(
                "href"
            )

            if not href:
                continue

            # -------------------------------------------------
            # 最小限のURL変換
            #
            # ここではqueryを保持する。
            # -------------------------------------------------

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
                    # -------------------------------------------------
                    # 検索結果から取得した個別URL
                    # -------------------------------------------------
                    "url": normalized_url,

                    # -------------------------------------------------
                    # 築年
                    # -------------------------------------------------
                    "builtYear": built_year,

                    # -------------------------------------------------
                    # カード全文
                    # -------------------------------------------------
                    "cardText": card_text,

                    # -------------------------------------------------
                    # URLから判定した種別
                    # -------------------------------------------------
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
        検索URL側で指定された物件種別と
        URLから判定した種別が一致するか確認する。

        target_property_typeがない場合:
            True

        URLから種別が判定できない場合:
            True

        不一致:
            False
        """

        if not target_property_type:
            return True

        if property_type is None:
            return True

        normalized_target = (
            str(target_property_type)
            .strip()
        )

        if normalized_target in (
            "中古戸建",
            "中古戸建て"
        ):
            return (
                property_type
                == "中古戸建"
            )

        if normalized_target in (
            "新築戸建",
            "新築戸建て"
        ):
            return (
                property_type
                == "新築戸建"
            )

        # 不明な設定値は誤除外しない
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

        このメソッドは互換性維持用。

        実際のcrawlでは
        extract_listing_candidates()
        を直接使用する。
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

        優先順位:

        1. rel="next"
        2. aria-label / title / 表示文字
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
        # 「次へ」等
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

        searchAreaは
        「検索条件として指定されたエリア」。

        実際の所在地はmain.py側で
        SUUMO詳細ページのaddressを使って判定する。
        """

        if not isinstance(
            target,
            dict
        ):
            return []

        start_url = target.get(
            "url"
        )

        target_property_type = target.get(
            "propertyType"
        )

        target_area = target.get(
            "area"
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

        # ページをまたいだ重複排除
        seen_listing_urls = set()

        # ページURLのループ防止
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

            accepted = 0
            rejected_age = 0
            rejected_type = 0
            duplicate_count = 0

            for candidate in candidates:

                listing_url = candidate.get(
                    "url"
                )

                if not listing_url:
                    continue

                # -------------------------------------------------
                # 同一物件
                # -------------------------------------------------

                if listing_url in seen_listing_urls:
                    duplicate_count += 1
                    continue

                # -------------------------------------------------
                # 物件種別
                # -------------------------------------------------

                property_type = candidate.get(
                    "propertyType"
                )

                if not self.is_property_type_allowed(
                    property_type,
                    target_property_type
                ):
                    rejected_type += 1

                    print(
                        "物件種別条件で除外: "
                        f"{listing_url} "
                        f"(種別={property_type}, "
                        f"指定={target_property_type})"
                    )

                    continue

                # -------------------------------------------------
                # 築年数
                # -------------------------------------------------

                built_year = candidate.get(
                    "builtYear"
                )

                if not self.is_within_building_age(
                    built_year,
                    property_type
                ):
                    rejected_age += 1

                    print(
                        "築年数条件で除外: "
                        f"{listing_url} "
                        f"(築年={built_year})"
                    )

                    continue

                # -------------------------------------------------
                # 採用
                # -------------------------------------------------

                seen_listing_urls.add(
                    listing_url
                )

                # 検索条件の情報を候補に保持
                candidate["searchArea"] = (
                    target_area
                )

                candidate["searchPropertyType"] = (
                    target_property_type
                )

                candidate["searchUrl"] = (
                    start_url
                )

                candidate["searchPageUrl"] = (
                    current_url
                )

                candidate["searchPageNumber"] = (
                    page_number
                )

                all_candidates.append(
                    candidate
                )

                accepted += 1

            print(
                f"検索ページ結果: "
                f"{len(candidates)}件 / "
                f"採用 {accepted}件 / "
                f"築年除外 {rejected_age}件 / "
                f"種別除外 {rejected_type}件 / "
                f"重複 {duplicate_count}件"
            )

            # -------------------------------------------------
            # 最大ページ数
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

            if next_url in seen_page_urls:
                break

            current_url = next_url

            if self.interval > 0:
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
        個別物件候補を返す。

        search_configはmain.pyから渡されるが、
        検索URL自体はconfig/search_urls.jsonを
        正式な検索対象として使用する。

        実所在地の判定はmain.py側で、
        詳細ページのaddressを使って行う。
        """

        search_targets = (
            self.load_search_urls()
        )

        if not search_targets:
            print(
                "SUUMO検索URLが設定されていません"
            )
            return []

        properties = []

        # 全検索条件をまたいだURL重複排除
        seen_urls = set()

        total_candidates = 0
        total_added = 0
        total_duplicate = 0

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

            # -------------------------------------------------
            # 検索URLの検証
            # -------------------------------------------------

            normalized_search_url = (
                self.normalize_search_url(
                    url
                )
            )

            if not normalized_search_url:
                print(
                    f"無効な検索URLをスキップ: {url}"
                )
                continue

            target_area = target.get(
                "area"
            )

            target_property_type = target.get(
                "propertyType"
            )

            print(
                "----------------------------------------"
            )

            print(
                "SUUMO検索開始: "
                f"{normalized_search_url}"
            )

            if target_area:
                print(
                    f"検索エリア: {target_area}"
                )

            if target_property_type:
                print(
                    "検索物件種別: "
                    f"{target_property_type}"
                )

            candidates = (
                self.crawl_search_target(
                    {
                        **target,
                        "url": normalized_search_url
                    }
                )
            )

            total_candidates += len(
                candidates
            )

            new_count = 0
            duplicate_count = 0

            for candidate in candidates:

                listing_url = candidate.get(
                    "url"
                )

                if not listing_url:
                    continue

                # -------------------------------------------------
                # 全検索条件をまたいだ重複排除
                # -------------------------------------------------

                if listing_url in seen_urls:
                    duplicate_count += 1
                    total_duplicate += 1
                    continue

                seen_urls.add(
                    listing_url
                )

                property_type = candidate.get(
                    "propertyType"
                )

                # -------------------------------------------------
                # main.pyへ渡す物件データ
                # -------------------------------------------------

                properties.append(
                    {
                        "source": "suumo",

                        # -------------------------------------------------
                        # 個別物件URL
                        #
                        # ここではsourceUrlを
                        # 正式な個別物件URLとして保持する。
                        # -------------------------------------------------
                        "sourceUrl": listing_url,

                        # -------------------------------------------------
                        # urlも同じ値を保持
                        #
                        # main.py側の既存データ構造との互換性用。
                        # -------------------------------------------------
                        "url": listing_url,

                        # -------------------------------------------------
                        # 検索条件として指定されたエリア
                        #
                        # 注意:
                        # これは実所在地ではない。
                        # -------------------------------------------------
                        "searchArea": candidate.get(
                            "searchArea"
                        ),

                        # 検索条件として指定された種別
                        "searchPropertyType": candidate.get(
                            "searchPropertyType"
                        ),

                        # 検索カードから取得した築年
                        "searchBuiltYear": candidate.get(
                            "builtYear"
                        ),

                        # 検索カード全文
                        "searchCardText": candidate.get(
                            "cardText"
                        ),

                        # URLから判定した種別
                        "searchDetectedPropertyType": (
                            property_type
                        ),

                        # 実際に使用した検索URL
                        "searchUrl": candidate.get(
                            "searchUrl"
                        ),

                        # ページ番号
                        "searchPageNumber": candidate.get(
                            "searchPageNumber"
                        ),

                        # ページURL
                        "searchPageUrl": candidate.get(
                            "searchPageUrl"
                        )
                    }
                )

                new_count += 1
                total_added += 1

            print(
                "SUUMO検索完了: "
                f"候補 {len(candidates)}件 / "
                f"新規 {new_count}件 / "
                f"重複 {duplicate_count}件"
            )

            if self.interval > 0:
                time.sleep(
                    self.interval
                )

        # =====================================================
        # 最終重複排除
        # =====================================================

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

        # =====================================================
        # 種別集計
        # =====================================================

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

        # =====================================================
        # 検索エリア集計
        #
        # ここでは「検索条件上のエリア」を集計。
        # 実所在地の集計はmain.py側で行う。
        # =====================================================

        area_counts = {}

        for property_data in unique_properties:

            area = property_data.get(
                "searchArea"
            )

            if not area:
                area = "未指定"

            area_counts[area] = (
                area_counts.get(
                    area,
                    0
                )
                + 1
            )

        # =====================================================
        # ログ
        # =====================================================

        print(
            "========================================"
        )

        print(
            "SUUMO検索完了"
        )

        print(
            f"検索候補総数: "
            f"{total_candidates}件"
        )

        print(
            f"新規追加: "
            f"{total_added}件"
        )

        print(
            f"検索条件間重複: "
            f"{total_duplicate}件"
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

        if area_counts:
            print(
                "検索条件エリア別:"
            )

            for area, count in sorted(
                area_counts.items()
            ):
                print(
                    f"  {area}: {count}件"
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