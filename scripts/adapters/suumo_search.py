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

    SUUMO_HOSTS = {
        "suumo.jp",
        "www.suumo.jp",
    }

    # 中古戸建
    USED_HOUSE_PATH = "/chukoikkodate/"

    # 新築戸建
    NEW_HOUSE_PATH = "/ikkodate/"

    # SUUMO個別物件ID
    LISTING_ID_PATTERN = re.compile(
        r"/nc_[0-9]+(?:/|$)",
        re.IGNORECASE
    )

    # SUUMO個別物件IDそのもの
    LISTING_ID_EXTRACT_PATTERN = re.compile(
        r"/(nc_[0-9]+)(?:/|$)",
        re.IGNORECASE
    )

    # =====================================================
    # 初期化
    # =====================================================

    def __init__(
        self,
        config,
        root_path
    ):
        super().__init__(config)

        self.root_path = Path(root_path)

        self.timeout = int(
            self.config.get(
                "timeout",
                20
            )
        )

        self.max_pages = max(
            1,
            int(
                self.config.get(
                    "maxPagesPerRun",
                    3
                )
            )
        )

        self.interval = max(
            0,
            float(
                self.config.get(
                    "intervalSeconds",
                    5
                )
            )
        )

        # -------------------------------------------------
        # 築年数条件
        #
        # ここでは検索結果カード上の築年を利用した
        # 「一次フィルター」のみ実施する。
        #
        # 正確な築年月はsuumo_detail.pyで取得し、
        # main.py側で最終判定する。
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
                        datetime.now().year - age
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
        config/search_urls.jsonから検索URLを読み込む。

        対応形式:

        {
          "suumo_search_urls": [
            {
              "area": "柏の葉キャンパス",
              "propertyType": "中古戸建",
              "url": "https://suumo.jp/..."
            }
          ]
        }

        または:

        [
          {
            "area": "柏の葉キャンパス",
            "propertyType": "中古戸建",
            "url": "https://suumo.jp/..."
          }
        ]

        enabled=false は無効化する。
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

        if isinstance(data, list):
            search_urls = data

        elif isinstance(data, dict):
            search_urls = data.get(
                "suumo_search_urls",
                []
            )

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

            url = target.get(
                "url"
            )

            if not url:
                continue

            normalized_url = (
                self.normalize_search_url(
                    url
                )
            )

            if not normalized_url:
                print(
                    "無効なSUUMO検索URLをスキップ: "
                    f"{url}"
                )
                continue

            normalized_target = dict(
                target
            )

            normalized_target["url"] = (
                normalized_url
            )

            enabled_urls.append(
                normalized_target
            )

        return enabled_urls

    # =====================================================
    # URL判定
    # =====================================================

    def is_valid_url(
        self,
        url
    ):
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
            hostname in self.SUUMO_HOSTS
            or hostname.endswith(
                "." + self.SUUMO_HOST
            )
        )

    # =====================================================
    # URL絶対化
    # =====================================================

    def _make_absolute_url(
        self,
        url,
        base_url=None
    ):
        """
        URLを絶対URLへ変換する。

        対応:
        - 相対URL
        - //suumo.jp/...
        - suumo.jp/...
        - https://...
        """

        if not url:
            return None

        original_url = str(
            url
        ).strip()

        if not original_url:
            return None

        if original_url.startswith("#"):
            return None

        if original_url.lower().startswith(
            (
                "javascript:",
                "mailto:",
                "tel:",
                "data:"
            )
        ):
            return None

        # -------------------------------------------------
        # suumo.jp/...
        # -------------------------------------------------

        if re.match(
            r"^(?:www\.)?suumo\.jp/",
            original_url,
            re.IGNORECASE
        ):
            original_url = (
                "https://"
                + original_url
            )

        # -------------------------------------------------
        # //suumo.jp/...
        # -------------------------------------------------

        if original_url.startswith("//"):

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

        if not self.is_valid_url(
            absolute_url
        ):
            return None

        return absolute_url

    # =====================================================
    # 個別物件ID取得
    # =====================================================

    def extract_listing_id(
        self,
        url
    ):
        """
        SUUMO個別物件URLからnc_xxxxxxxxを取得する。

        例:
        /chukoikkodate/chiba/sc_xxx/nc_12345678/

        -> nc_12345678
        """

        if not url:
            return None

        try:
            parsed = urlparse(
                str(url)
            )

        except ValueError:
            return None

        path = parsed.path or ""

        match = (
            self.LISTING_ID_EXTRACT_PATTERN.search(
                path
            )
        )

        if not match:
            return None

        return match.group(1).lower()

    # =====================================================
    # 個別物件URL正規化
    # =====================================================

    def normalize_url(
        self,
        url,
        base_url=None
    ):
        """
        個別物件URLをcanonical URLへ変換する。

        重要:
        main.py / suumo_detail.pyと物件識別を
        揃えるため、個別物件URLについては

        - httpsへ統一
        - wwwを除去
        - fragment削除
        - query削除
        - 個別物件URL末尾に/を付与

        とする。

        検索URLについてはqueryを保持するため、
        normalize_search_url()を使用する。
        """

        absolute_url = (
            self._make_absolute_url(
                url,
                base_url
            )
        )

        if not absolute_url:
            return None

        try:
            parsed = urlparse(
                absolute_url
            )

        except ValueError:
            return None

        hostname = (
            parsed.hostname or ""
        ).lower()

        if hostname not in self.SUUMO_HOSTS:
            return None

        path = parsed.path or ""

        # -------------------------------------------------
        # 個別物件ID確認
        # -------------------------------------------------

        listing_id = (
            self.extract_listing_id(
                absolute_url
            )
        )

        if not listing_id:
            return None

        # -------------------------------------------------
        # 物件ページとして認めるパス
        # -------------------------------------------------

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
            return None

        # -------------------------------------------------
        # 末尾スラッシュ統一
        # -------------------------------------------------

        if not path.endswith("/"):
            path += "/"

        # -------------------------------------------------
        # 個別物件URLはcanonical hostへ統一
        #
        # queryはトラッキングパラメータ等を
        # 物件識別に使わないため削除する。
        # -------------------------------------------------

        return urlunparse(
            (
                "https",
                self.SUUMO_HOST,
                path,
                "",
                "",
                ""
            )
        )

    # =====================================================
    # 検索URL正規化
    # =====================================================

    def normalize_search_url(
        self,
        url,
        base_url=None
    ):
        """
        検索ページURLを正規化する。

        検索条件・ページングでqueryを使用するため、
        queryは保持する。

        個別物件URLとは異なり、
        検索URLではquery削除を行わない。
        """

        absolute_url = (
            self._make_absolute_url(
                url,
                base_url
            )
        )

        if not absolute_url:
            return None

        try:
            parsed = urlparse(
                absolute_url
            )

        except ValueError:
            return None

        hostname = (
            parsed.hostname or ""
        ).lower()

        if hostname not in self.SUUMO_HOSTS:
            return None

        return urlunparse(
            (
                "https",
                self.SUUMO_HOST,
                parsed.path or "",
                "",
                parsed.query or "",
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
        if not url:
            return False

        try:
            parsed = urlparse(
                str(url)
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

        if hostname not in self.SUUMO_HOSTS:
            return False

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

        return bool(
            self.LISTING_ID_PATTERN.search(
                path
            )
        )

    # =====================================================
    # 物件種別判定
    # =====================================================

    def get_property_type_from_url(
        self,
        url
    ):
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
        SUUMO検索ページを取得する。
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
    # 築年抽出
    # =====================================================

    def extract_built_year(
        self,
        text
    ):
        if not text:
            return None

        text = re.sub(
            r"\s+",
            " ",
            str(text)
        ).strip()

        # -------------------------------------------------
        # 2008年
        # 2008年3月
        # 2008年12月築
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
        # 築15年
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

                if age >= 0:
                    return (
                        datetime.now().year
                        - age
                    )

            except ValueError:
                pass

        return None

    # =====================================================
    # 築年数判定
    # =====================================================

    def is_within_building_age(
        self,
        built_year,
        property_type=None
    ):
        """
        検索結果ページでの一次フィルター。

        築年不明は除外しない。

        最終的な築年判定は、
        detail取得後にmain.py側で行う。
        """

        if self.min_built_year is None:
            return True

        # -------------------------------------------------
        # 新築は常に許容
        # -------------------------------------------------

        if property_type == "新築戸建":
            return True

        # -------------------------------------------------
        # 築年不明は後段へ渡す
        # -------------------------------------------------

        if built_year is None:
            return True

        return (
            built_year
            >= self.min_built_year
        )

    # =====================================================
    # 物件カードテキスト取得
    # =====================================================

    def get_card_text(
        self,
        link
    ):
        if link is None:
            return ""

        # -------------------------------------------------
        # まず近い物件カードを探す
        # -------------------------------------------------

        card = (
            link.find_parent(
                class_=re.compile(
                    r"(cassette|property|result|item|house|estate)",
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
        # 親を数階層たどる
        # -------------------------------------------------

        parent = link.parent

        for _ in range(4):

            if parent is None:
                break

            text = parent.get_text(
                " ",
                strip=True
            )

            if text:
                return text

            parent = parent.parent

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
        検索結果HTMLから個別戸建てURLを抽出。

        この段階では、

        - 実所在地
        - 正確な築年月
        - 土地面積
        - 建物面積
        - 駅徒歩
        - 学区

        等の詳細条件は判定しない。
        """

        if not html:
            return []

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        results = []

        # -------------------------------------------------
        # canonical URLで重複排除
        # -------------------------------------------------

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

            listing_id = (
                self.extract_listing_id(
                    normalized_url
                )
            )

            results.append(
                {
                    "sourceUrl": normalized_url,
                    "url": normalized_url,
                    "listingId": listing_id,
                    "builtYear": built_year,
                    "cardText": card_text,
                    "propertyType": property_type
                }
            )

        return results

    # =====================================================
    # 物件種別フィルター
    # =====================================================

    def is_property_type_allowed(
        self,
        property_type,
        target_property_type=None
    ):
        if not target_property_type:
            return True

        if property_type is None:
            # URLから判定できないものは
            # detail側で確認できる可能性があるため残す。
            return True

        target = str(
            target_property_type
        ).strip()

        if target in (
            "中古戸建",
            "中古戸建て"
        ):
            return (
                property_type
                == "中古戸建"
            )

        if target in (
            "新築戸建",
            "新築戸建て"
        ):
            return (
                property_type
                == "新築戸建"
            )

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

            if not self.is_property_type_allowed(
                property_type,
                target_property_type
            ):
                property_type_filtered += 1
                continue

            if built_year is None:
                unknown_year += 1

                # 築年不明は後段へ渡す
                results.append(url)

                continue

            if not self.is_within_building_age(
                built_year,
                property_type
            ):
                filtered_out += 1
                continue

            results.append(url)

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
        if not html:
            return None

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
        # 次へ
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

            aria_label = link.get(
                "aria-label",
                ""
            )

            title = link.get(
                "title",
                ""
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
    # 1検索URL巡回
    # =====================================================

    def crawl_search_target(
        self,
        target
    ):
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

        # -------------------------------------------------
        # ページ重複防止
        # -------------------------------------------------

        seen_page_urls = set()

        # -------------------------------------------------
        # 物件重複防止
        # -------------------------------------------------

        seen_listing_urls = set()

        for page_number in range(
            1,
            self.max_pages + 1
        ):

            if not current_url:
                break

            if current_url in seen_page_urls:
                print(
                    "検索ページループを検出。停止: "
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

            # -------------------------------------------------
            # HTML取得
            # -------------------------------------------------

            try:
                html = self.fetch_search_page(
                    current_url
                )

            except requests.HTTPError as error:
                print(
                    "HTTP ERROR: "
                    f"{current_url} / {error}"
                )
                break

            except requests.RequestException as error:
                print(
                    "REQUEST ERROR: "
                    f"{current_url} / {error}"
                )
                break

            except Exception as error:
                print(
                    "UNEXPECTED ERROR: "
                    f"{current_url} / {error}"
                )
                break

            # -------------------------------------------------
            # 候補抽出
            # -------------------------------------------------

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
            unknown_year_count = 0

            # -------------------------------------------------
            # フィルター
            # -------------------------------------------------

            for candidate in candidates:

                listing_url = (
                    candidate.get(
                        "sourceUrl"
                    )
                    or
                    candidate.get(
                        "url"
                    )
                )

                if not listing_url:
                    continue

                # 念のためcanonical化
                listing_url = (
                    self.normalize_url(
                        listing_url,
                        current_url
                    )
                )

                if not listing_url:
                    continue

                if listing_url in seen_listing_urls:
                    duplicate_count += 1
                    continue

                property_type = (
                    candidate.get(
                        "propertyType"
                    )
                )

                # -------------------------------------------------
                # 種別
                # -------------------------------------------------

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
                # 築年
                # -------------------------------------------------

                built_year = (
                    candidate.get(
                        "builtYear"
                    )
                )

                if built_year is None:

                    unknown_year_count += 1

                elif not self.is_within_building_age(
                    built_year,
                    property_type
                ):

                    rejected_age += 1

                    print(
                        "築年数条件で除外: "
                        f"{listing_url} "
                        f"(築年={built_year}, "
                        f"基準={self.min_built_year})"
                    )

                    continue

                # -------------------------------------------------
                # 採用
                # -------------------------------------------------

                seen_listing_urls.add(
                    listing_url
                )

                candidate["sourceUrl"] = (
                    listing_url
                )

                candidate["url"] = (
                    listing_url
                )

                candidate["detailUrl"] = (
                    listing_url
                )

                # -------------------------------------------------
                # 検索条件メタデータ
                # -------------------------------------------------

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
                "検索ページ結果: "
                f"{len(candidates)}件 / "
                f"採用 {accepted}件 / "
                f"築年除外 {rejected_age}件 / "
                f"築年不明 {unknown_year_count}件 / "
                f"種別除外 {rejected_type}件 / "
                f"重複 {duplicate_count}件"
            )

            # -------------------------------------------------
            # 最終ページ
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
        search_config=None
    ):
        """
        SUUMO検索URLをすべて巡回する。

        search_configは互換性維持のため受け取る。

        正式な検索URLは
        config/search_urls.jsonを使用する。

        このAdapterで行うこと:

        - SUUMO検索URL取得
        - ページング
        - 個別物件URL抽出
        - 中古/新築判定
        - 検索結果上の築年一次フィルター
        - 重複排除
        - 検索条件メタデータ付与

        main.py / suumo_detail.pyで行うこと:

        - 詳細ページ取得
        - 実住所判定
        - 正確な築年月
        - 土地面積
        - 建物面積
        - 駅徒歩
        - 接道
        - 擁壁
        - 学区
        - 価格判定
        - その他詳細条件
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

        # -------------------------------------------------
        # 全検索条件共通の重複排除
        #
        # canonical URLをキーにする。
        # -------------------------------------------------

        seen_urls = set()

        total_candidates = 0
        total_added = 0
        total_duplicate = 0
        total_unknown_year = 0

        # =====================================================
        # 検索条件ごとに巡回
        # =====================================================

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

            # -------------------------------------------------
            # 検索実行
            # -------------------------------------------------

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

            # -------------------------------------------------
            # 候補を共通リストへ追加
            # -------------------------------------------------

            for candidate in candidates:

                listing_url = (
                    candidate.get(
                        "sourceUrl"
                    )
                    or
                    candidate.get(
                        "url"
                    )
                )

                if not listing_url:
                    continue

                # -------------------------------------------------
                # 最終canonical化
                # -------------------------------------------------

                listing_url = (
                    self.normalize_url(
                        listing_url,
                        normalized_search_url
                    )
                )

                if not listing_url:
                    continue

                if listing_url in seen_urls:
                    duplicate_count += 1
                    total_duplicate += 1
                    continue

                seen_urls.add(
                    listing_url
                )

                property_type = (
                    candidate.get(
                        "propertyType"
                    )
                )

                built_year = (
                    candidate.get(
                        "builtYear"
                    )
                )

                if built_year is None:
                    total_unknown_year += 1

                listing_id = (
                    candidate.get(
                        "listingId"
                    )
                )

                if not listing_id:
                    listing_id = (
                        self.extract_listing_id(
                            listing_url
                        )
                    )

                # =================================================
                # main.pyへ渡すデータ
                # =================================================

                properties.append(
                    {
                        "source": "suumo",

                        # -----------------------------------------
                        # 個別物件ID
                        # -----------------------------------------

                        "listingId": listing_id,

                        # -----------------------------------------
                        # 個別物件URL
                        #
                        # sourceUrl / url / detailUrlを
                        # 完全に同じcanonical URLへ統一。
                        # -----------------------------------------

                        "sourceUrl": listing_url,

                        "url": listing_url,

                        "detailUrl": listing_url,

                        # -----------------------------------------
                        # 検索条件上のエリア
                        #
                        # 実所在地ではない。
                        # -----------------------------------------

                        "searchArea": candidate.get(
                            "searchArea"
                        ),

                        # -----------------------------------------
                        # 検索条件上の物件種別
                        # -----------------------------------------

                        "searchPropertyType": candidate.get(
                            "searchPropertyType"
                        ),

                        # -----------------------------------------
                        # 検索結果カード上の築年
                        #
                        # 正確な築年月ではない。
                        # -----------------------------------------

                        "searchBuiltYear": candidate.get(
                            "builtYear"
                        ),

                        # -----------------------------------------
                        # 検索結果カード全文
                        # -----------------------------------------

                        "searchCardText": candidate.get(
                            "cardText"
                        ),

                        # -----------------------------------------
                        # URLから判定した物件種別
                        # -----------------------------------------

                        "searchDetectedPropertyType": (
                            property_type
                        ),

                        # -----------------------------------------
                        # 検索開始URL
                        # -----------------------------------------

                        "searchUrl": candidate.get(
                            "searchUrl"
                        ),

                        # -----------------------------------------
                        # 実際に取得した検索ページ
                        # -----------------------------------------

                        "searchPageUrl": candidate.get(
                            "searchPageUrl"
                        ),

                        # -----------------------------------------
                        # 検索ページ番号
                        # -----------------------------------------

                        "searchPageNumber": candidate.get(
                            "searchPageNumber"
                        ),

                        # -----------------------------------------
                        # detail取得状態
                        # -----------------------------------------

                        "detailFetched": False,

                        "detailFetchStatus": "pending"
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

            source_url = (
                property_data.get(
                    "sourceUrl"
                )
                or
                property_data.get(
                    "url"
                )
            )

            if not source_url:
                continue

            source_url = (
                self.normalize_url(
                    source_url
                )
            )

            if not source_url:
                continue

            if source_url in final_seen:
                continue

            final_seen.add(
                source_url
            )

            # -------------------------------------------------
            # URL関連フィールドを完全統一
            # -------------------------------------------------

            property_data["sourceUrl"] = (
                source_url
            )

            property_data["url"] = (
                source_url
            )

            property_data["detailUrl"] = (
                source_url
            )

            # -------------------------------------------------
            # listingIdを補完
            # -------------------------------------------------

            if not property_data.get(
                "listingId"
            ):
                property_data["listingId"] = (
                    self.extract_listing_id(
                        source_url
                    )
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
        # 最終ログ
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

        if total_unknown_year > 0:
            print(
                f"築年不明: "
                f"{total_unknown_year}件"
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
            "※ 築年不明物件は検索段階では除外せず、"
            "詳細ページ取得後に最終判定します。"
        )

        print(
            "※ 実所在地は検索条件エリアではなく、"
            "詳細ページのaddressを使用して判定します。"
        )

        print(
            "※ 個別物件URLはcanonical URLへ統一しています。"
        )

        print(
            "※ sourceUrl / url / detailUrlは"
            "同一物件について同一URLを保持します。"
        )

        print(
            "※ 検索URLのqueryはページング・検索条件維持のため保持します。"
        )

        print(
            "========================================"
        )

        return unique_properties