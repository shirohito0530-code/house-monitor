from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from adapters.base import PropertyAdapter


class SuumoSearchAdapter(PropertyAdapter):
    """
    SUUMO search result adapter.

    Responsibility:
        - search URL loading
        - search page crawling / pagination
        - individual listing URL extraction
        - property type detection
        - search-result-level building-age prefilter
        - duplicate elimination
        - search metadata attachment

    Final criteria evaluation is performed by main.py after
    detail-page acquisition.
    """

    # =====================================================
    # Constants
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

    # SUUMO individual listing ID
    LISTING_ID_PATTERN = re.compile(
        r"/nc_[0-9]+(?:/|$)",
        re.IGNORECASE,
    )

    # SUUMO individual listing ID extraction
    LISTING_ID_EXTRACT_PATTERN = re.compile(
        r"/(nc_[0-9]+)(?:/|$)",
        re.IGNORECASE,
    )

    # =====================================================
    # Initialization
    # =====================================================

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        root_path: Optional[str] = None,
    ):
        super().__init__(config or {})

        if root_path:
            self.root_path = Path(root_path)
        else:
            # scripts/adapters/suumo_search.py
            # -> project root = ../../
            self.root_path = (
                Path(__file__).resolve().parents[2]
            )

        # -------------------------------------------------
        # HTTP settings
        # -------------------------------------------------

        try:
            self.timeout = max(
                1,
                int(
                    self.config.get(
                        "timeout",
                        20,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.timeout = 20

        try:
            self.max_pages = max(
                1,
                int(
                    self.config.get(
                        "maxPagesPerRun",
                        3,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.max_pages = 3

        try:
            self.interval = max(
                0.0,
                float(
                    self.config.get(
                        "intervalSeconds",
                        5,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.interval = 5.0

        # -------------------------------------------------
        # Building-age primary filter
        #
        # This is ONLY a search-result-level prefilter.
        #
        # Exact construction date is evaluated later by
        # main.py after detail-page acquisition.
        # -------------------------------------------------

        max_building_age = self.config.get(
            "maxBuiltAgeYears"
        )

        if max_building_age is None:
            max_building_age = self.config.get(
                "maxBuildingAgeYears"
            )

        self.max_building_age_years: Optional[int] = None
        self.min_built_year: Optional[int] = None

        if max_building_age is not None:
            try:
                age = int(
                    float(max_building_age)
                )

                if age >= 0:
                    self.max_building_age_years = age

                    self.min_built_year = (
                        datetime.now(timezone.utc).year
                        - age
                    )

            except (TypeError, ValueError):
                print(
                    "[WARN] 築年数条件が不正です: "
                    f"{max_building_age}"
                )

        if self.min_built_year is not None:
            print(
                "[CONFIG] SUUMO検索段階の築年数一次フィルター: "
                f"{self.min_built_year}年以降 "
                f"(築{self.max_building_age_years}年以内の目安)"
            )

    # =====================================================
    # Search URL loading
    # =====================================================

    def load_search_urls(
        self,
    ) -> List[Dict[str, Any]]:
        """
        Load config/search_urls.json.

        Supported formats:

        {
          "suumo_search_urls": [
            {
              "area": "柏の葉キャンパス",
              "propertyType": "中古戸建",
              "url": "https://suumo.jp/..."
            }
          ]
        }

        or:

        [
          {
            "area": "柏の葉キャンパス",
            "propertyType": "中古戸建",
            "url": "https://suumo.jp/..."
          }
        ]

        enabled=false is ignored.
        """

        path = (
            self.root_path
            / "config"
            / "search_urls.json"
        )

        if not path.exists():
            print(
                "[WARN] 検索URL設定ファイルがありません: "
                f"{path}"
            )
            return []

        try:
            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except (
            json.JSONDecodeError,
            OSError,
        ) as error:
            print(
                "[ERROR] 検索URL設定ファイルの読み込み失敗: "
                f"{error}"
            )
            return []

        if isinstance(data, list):
            search_urls = data

        elif isinstance(data, dict):
            search_urls = data.get(
                "suumo_search_urls",
                [],
            )

            if not search_urls:
                search_urls = data.get(
                    "targets",
                    [],
                )

        else:
            print(
                "[WARN] 検索URL設定の形式が不正です"
            )
            return []

        if not isinstance(
            search_urls,
            list,
        ):
            print(
                "[WARN] suumo_search_urlsは配列で指定してください"
            )
            return []

        enabled_urls: List[
            Dict[str, Any]
        ] = []

        for target in search_urls:
            if not isinstance(
                target,
                dict,
            ):
                continue

            if target.get(
                "enabled",
                True,
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
                    "[WARN] 無効なSUUMO検索URLをスキップ: "
                    f"{url}"
                )
                continue

            normalized_target = dict(
                target
            )

            normalized_target[
                "url"
            ] = normalized_url

            enabled_urls.append(
                normalized_target
            )

        return enabled_urls

    # =====================================================
    # URL validation
    # =====================================================

    def is_valid_url(
        self,
        url: Any,
    ) -> bool:
        if not url:
            return False

        try:
            parsed = urlparse(
                str(url).strip()
            )
        except ValueError:
            return False

        if parsed.scheme.lower() not in {
            "http",
            "https",
        }:
            return False

        hostname = (
            parsed.hostname or ""
        ).lower()

        return hostname in self.SUUMO_HOSTS

    # =====================================================
    # URL absolute conversion
    # =====================================================

    def _make_absolute_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        """
        Convert URL into an absolute SUUMO URL.

        Supported:
            /relative/path
            //suumo.jp/...
            suumo.jp/...
            www.suumo.jp/...
            https://...
        """

        if not url:
            return None

        original_url = str(
            url
        ).strip()

        if not original_url:
            return None

        lower_url = original_url.lower()

        if (
            lower_url.startswith(
                "#"
            )
            or lower_url.startswith(
                "javascript:"
            )
            or lower_url.startswith(
                "mailto:"
            )
            or lower_url.startswith(
                "tel:"
            )
            or lower_url.startswith(
                "data:"
            )
        ):
            return None

        # -------------------------------------------------
        # Repair malformed scheme
        # -------------------------------------------------

        original_url = re.sub(
            r"^https:/+",
            "https://",
            original_url,
            flags=re.IGNORECASE,
        )

        original_url = re.sub(
            r"^http:/+",
            "http://",
            original_url,
            flags=re.IGNORECASE,
        )

        # -------------------------------------------------
        # suumo.jp/...
        # -------------------------------------------------

        if re.match(
            r"^(?:www\.)?suumo\.jp/",
            original_url,
            re.IGNORECASE,
        ):
            original_url = (
                "https://"
                + original_url
            )

        # -------------------------------------------------
        # Protocol-relative URL
        # -------------------------------------------------

        if original_url.startswith(
            "//"
        ):
            absolute_url = (
                "https:"
                + original_url
            )

        # -------------------------------------------------
        # Relative URL
        # -------------------------------------------------

        elif base_url:
            absolute_url = urljoin(
                base_url,
                original_url,
            )

        else:
            absolute_url = original_url

        if not self.is_valid_url(
            absolute_url
        ):
            return None

        return absolute_url

    # =====================================================
    # Listing ID
    # =====================================================

    def extract_listing_id(
        self,
        url: Any,
    ) -> Optional[str]:
        """
        Extract nc_xxxxxxxx from an individual listing URL.
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
    # Individual listing URL normalization
    # =====================================================

    def normalize_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        """
        Normalize an individual listing URL.

        Canonical form:

        https://suumo.jp/<listing-path>/nc_xxxxxxxx/

        Rules:
            - https
            - canonical host suumo.jp
            - query removed
            - fragment removed
            - trailing slash added
        """

        absolute_url = (
            self._make_absolute_url(
                url,
                base_url,
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
        # Individual listing ID must exist
        # -------------------------------------------------

        listing_id = (
            self.extract_listing_id(
                absolute_url
            )
        )

        if not listing_id:
            return None

        # -------------------------------------------------
        # Only detached-house listing paths
        # -------------------------------------------------

        path_lower = path.lower()

        is_house_path = (
            path_lower.startswith(
                self.USED_HOUSE_PATH
            )
            or path_lower.startswith(
                self.NEW_HOUSE_PATH
            )
        )

        if not is_house_path:
            return None

        # -------------------------------------------------
        # Trailing slash
        # -------------------------------------------------

        if not path.endswith("/"):
            path += "/"

        # -------------------------------------------------
        # Canonical URL
        # -------------------------------------------------

        return urlunparse(
            (
                "https",
                self.SUUMO_HOST,
                path,
                "",
                "",
                "",
            )
        )

    # =====================================================
    # Search URL normalization
    # =====================================================

    def normalize_search_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        """
        Normalize search-page URL.

        Unlike listing URLs, query parameters MUST be preserved
        because SUUMO search conditions and pagination may use them.
        """

        absolute_url = (
            self._make_absolute_url(
                url,
                base_url,
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

        path = parsed.path or "/"

        return urlunparse(
            (
                "https",
                self.SUUMO_HOST,
                path,
                "",
                parsed.query or "",
                "",
            )
        )

    # =====================================================
    # Individual listing URL validation
    # =====================================================

    def is_individual_listing_url(
        self,
        url: Any,
    ) -> bool:
        if not url:
            return False

        try:
            parsed = urlparse(
                str(url)
            )
        except ValueError:
            return False

        if parsed.scheme.lower() not in {
            "http",
            "https",
        }:
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
            or path_lower.startswith(
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
    # Property type
    # =====================================================

    def get_property_type_from_url(
        self,
        url: Any,
    ) -> Optional[str]:
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
        url: str,
    ) -> str:
        """
        Fetch a SUUMO search result page.
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
            "Cache-Control": "no-cache",
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=True,
        )

        response.raise_for_status()

        return response.text

    # =====================================================
    # Built year extraction
    # =====================================================

    def extract_built_year(
        self,
        text: Any,
    ) -> Optional[int]:
        """
        Extract a construction year from search-card text.

        Examples:
            2008年
            2008年3月
            2008年12月築
            築15年

        Unknown values are returned as None.
        """

        if not text:
            return None

        normalized = re.sub(
            r"\s+",
            " ",
            str(text),
        ).strip()

        # -------------------------------------------------
        # Explicit year
        # -------------------------------------------------

        match = re.search(
            r"(19\d{2}|20\d{2})年",
            normalized,
        )

        if match:
            try:
                year = int(
                    match.group(1)
                )

                current_year = (
                    datetime.now(
                        timezone.utc
                    ).year
                )

                if (
                    1800
                    <= year
                    <= current_year + 5
                ):
                    return year

            except ValueError:
                pass

        # -------------------------------------------------
        # Relative age
        # -------------------------------------------------

        match = re.search(
            r"築\s*(\d+)\s*年",
            normalized,
        )

        if match:
            try:
                age = int(
                    match.group(1)
                )

                if age >= 0:
                    return (
                        datetime.now(
                            timezone.utc
                        ).year
                        - age
                    )

            except ValueError:
                pass

        return None

    # =====================================================
    # Building-age primary filter
    # =====================================================

    def is_within_building_age(
        self,
        built_year: Optional[int],
        property_type: Optional[str] = None,
    ) -> bool:
        """
        Search-result-level primary filter.

        Important:
            Unknown built year is NOT rejected here.
            Final evaluation is performed by main.py
            using detail-page construction information.
        """

        if self.min_built_year is None:
            return True

        # New construction is exempt.
        if property_type == "新築戸建":
            return True

        # Unknown construction year is retained.
        if built_year is None:
            return True

        return (
            built_year
            >= self.min_built_year
        )

    # =====================================================
    # Card text
    # =====================================================

    def get_card_text(
        self,
        link,
    ) -> str:
        """
        Obtain text from the closest likely property card.
        """

        if link is None:
            return ""

        # -------------------------------------------------
        # Prefer nearby property-card-like container
        # -------------------------------------------------

        card = link.find_parent(
            class_=re.compile(
                r"(cassette|property|result|item|house|estate)",
                re.IGNORECASE,
            )
        )

        if card is not None:
            text = card.get_text(
                " ",
                strip=True,
            )

            if text:
                return text

        # -------------------------------------------------
        # Fallback: walk up a few levels
        # -------------------------------------------------

        parent = link.parent

        for _ in range(4):
            if parent is None:
                break

            text = parent.get_text(
                " ",
                strip=True,
            )

            if text:
                return text

            parent = parent.parent

        return link.get_text(
            " ",
            strip=True,
        )

    # =====================================================
    # Listing candidate extraction
    # =====================================================

    def extract_listing_candidates(
        self,
        html: str,
        base_url: str,
    ) -> List[Dict[str, Any]]:
        """
        Extract individual detached-house listings from
        a SUUMO search-result page.

        Detailed criteria are NOT evaluated here.
        """

        if not html:
            return []

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: List[
            Dict[str, Any]
        ] = []

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
                    base_url,
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
                    "propertyType": property_type,
                }
            )

        return results

    # =====================================================
    # Property-type filter
    # =====================================================

    def is_property_type_allowed(
        self,
        property_type: Optional[str],
        target_property_type: Optional[str] = None,
    ) -> bool:
        if not target_property_type:
            return True

        # Unknown type is retained for downstream validation.
        if property_type is None:
            return True

        target = str(
            target_property_type
        ).strip()

        if target in {
            "中古戸建",
            "中古戸建て",
        }:
            return (
                property_type == "中古戸建"
            )

        if target in {
            "新築戸建",
            "新築戸建て",
        }:
            return (
                property_type == "新築戸建"
            )

        # Unknown configuration:
        # do not silently exclude.
        return True

    # =====================================================
    # Listing URL extraction
    # =====================================================

    def extract_listing_urls(
        self,
        html: str,
        base_url: str,
        target_property_type: Optional[str] = None,
    ) -> List[str]:
        """
        Compatibility helper.

        Returns only accepted individual listing URLs.
        """

        candidates = (
            self.extract_listing_candidates(
                html,
                base_url,
            )
        )

        results: List[str] = []

        filtered_out = 0
        unknown_year = 0
        property_type_filtered = 0

        for candidate in candidates:
            url = candidate.get(
                "url"
            )

            if not url:
                continue

            built_year = candidate.get(
                "builtYear"
            )

            property_type = candidate.get(
                "propertyType"
            )

            if not self.is_property_type_allowed(
                property_type,
                target_property_type,
            ):
                property_type_filtered += 1
                continue

            if built_year is None:
                unknown_year += 1
                results.append(
                    url
                )
                continue

            if not self.is_within_building_age(
                built_year,
                property_type,
            ):
                filtered_out += 1
                continue

            results.append(
                url
            )

        print(
            "[SEARCH FILTER] "
            f"候補={len(candidates)}件 / "
            f"採用={len(results)}件 / "
            f"築年除外={filtered_out}件 / "
            f"築年不明={unknown_year}件 / "
            f"種別除外={property_type_filtered}件"
        )

        return sorted(
            set(results)
        )

    # =====================================================
    # Next-page URL
    # =====================================================

    def extract_next_page_url(
        self,
        html: str,
        current_url: str,
    ) -> Optional[str]:
        if not html:
            return None

        soup = BeautifulSoup(
            html,
            "html.parser",
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
                    current_url,
                )
            )

            if normalized:
                return normalized

        # -------------------------------------------------
        # Japanese pagination labels
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
                strip=True,
            )

            aria_label = link.get(
                "aria-label",
                "",
            )

            title = link.get(
                "title",
                "",
            )

            combined = (
                f"{text} "
                f"{aria_label} "
                f"{title}"
            )

            if (
                "次へ" in combined
                or "次のページ" in combined
            ):
                normalized = (
                    self.normalize_search_url(
                        href,
                        current_url,
                    )
                )

                if normalized:
                    return normalized

        return None

    # =====================================================
    # Crawl one search target
    # =====================================================

    def crawl_search_target(
        self,
        target: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not isinstance(
            target,
            dict,
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

        all_candidates: List[
            Dict[str, Any]
        ] = []

        # -------------------------------------------------
        # Page duplicate prevention
        # -------------------------------------------------

        seen_page_urls = set()

        # -------------------------------------------------
        # Listing duplicate prevention
        # -------------------------------------------------

        seen_listing_urls = set()

        for page_number in range(
            1,
            self.max_pages + 1,
        ):
            if not current_url:
                break

            if current_url in seen_page_urls:
                print(
                    "[WARN] 検索ページループを検出。停止: "
                    f"{current_url}"
                )
                break

            seen_page_urls.add(
                current_url
            )

            print(
                "[SEARCH] SUUMO検索ページ取得: "
                f"{page_number}/{self.max_pages} "
                f"{current_url}"
            )

            # -------------------------------------------------
            # Fetch page
            # -------------------------------------------------

            try:
                html = self.fetch_search_page(
                    current_url
                )

            except requests.HTTPError as error:
                print(
                    "[ERROR] SUUMO HTTP ERROR: "
                    f"{current_url} / {error}"
                )
                break

            except requests.RequestException as error:
                print(
                    "[ERROR] SUUMO REQUEST ERROR: "
                    f"{current_url} / {error}"
                )
                break

            except Exception as error:
                print(
                    "[ERROR] SUUMO UNEXPECTED ERROR: "
                    f"{current_url} / {error}"
                )
                break

            # -------------------------------------------------
            # Extract candidates
            # -------------------------------------------------

            candidates = (
                self.extract_listing_candidates(
                    html,
                    current_url,
                )
            )

            accepted = 0
            rejected_age = 0
            rejected_type = 0
            duplicate_count = 0
            unknown_year_count = 0

            # -------------------------------------------------
            # Filter candidates
            # -------------------------------------------------

            for position_idx, candidate in enumerate(
                candidates,
                start=1,
            ):
                listing_url = (
                    candidate.get(
                        "sourceUrl"
                    )
                    or candidate.get(
                        "url"
                    )
                )

                if not listing_url:
                    continue

                # Re-canonicalize
                listing_url = (
                    self.normalize_url(
                        listing_url,
                        current_url,
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
                # Property type
                # -------------------------------------------------

                if not self.is_property_type_allowed(
                    property_type,
                    target_property_type,
                ):
                    rejected_type += 1

                    print(
                        "[FILTER] 物件種別条件で除外: "
                        f"{listing_url} "
                        f"(種別={property_type}, "
                        f"指定={target_property_type})"
                    )

                    continue

                # -------------------------------------------------
                # Building age
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
                    property_type,
                ):
                    rejected_age += 1

                    print(
                        "[FILTER] 築年数条件で除外: "
                        f"{listing_url} "
                        f"(築年={built_year}, "
                        f"基準={self.min_built_year})"
                    )

                    continue

                # -------------------------------------------------
                # Accept
                # -------------------------------------------------

                seen_listing_urls.add(
                    listing_url
                )

                candidate[
                    "sourceUrl"
                ] = listing_url

                candidate[
                    "url"
                ] = listing_url

                candidate[
                    "detailUrl"
                ] = listing_url

                # -------------------------------------------------
                # Search metadata
                # -------------------------------------------------

                candidate[
                    "searchArea"
                ] = target_area

                candidate[
                    "searchPropertyType"
                ] = target_property_type

                candidate[
                    "searchUrl"
                ] = current_url

                candidate[
                    "searchPageUrl"
                ] = current_url

                candidate[
                    "searchPageNumber"
                ] = page_number

                # IMPORTANT:
                # preserve actual position in original
                # candidate list.
                candidate[
                    "searchPosition"
                ] = position_idx

                candidate[
                    "searchTarget"
                ] = (
                    target.get("name")
                    or target.get("target")
                    or (
                        f"{target_area}_"
                        f"{target_property_type}"
                    )
                )

                candidate[
                    "searchTargetArea"
                ] = target_area

                candidate[
                    "searchTargetPropertyType"
                ] = target_property_type

                candidate[
                    "discoveredAt"
                ] = (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )

                all_candidates.append(
                    candidate
                )

                accepted += 1

            print(
                "[SEARCH] 検索ページ結果: "
                f"候補={len(candidates)}件 / "
                f"採用={accepted}件 / "
                f"築年除外={rejected_age}件 / "
                f"築年不明={unknown_year_count}件 / "
                f"種別除外={rejected_type}件 / "
                f"重複={duplicate_count}件"
            )

            # -------------------------------------------------
            # Last page
            # -------------------------------------------------

            if page_number >= self.max_pages:
                break

            # -------------------------------------------------
            # Next page
            # -------------------------------------------------

            next_url = (
                self.extract_next_page_url(
                    html,
                    current_url,
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
    # Main search
    # =====================================================

    def search(
        self,
        search_config: Optional[
            Dict[str, Any]
        ] = None,
    ) -> List[Dict[str, Any]]:
        """
        Crawl all enabled SUUMO search URLs.

        Returns:
            List[Dict[str, Any]]
        """

        search_targets = (
            self.load_search_urls()
        )

        if not search_targets:
            print(
                "[WARN] SUUMO検索URLが設定されていません"
            )
            return []

        properties: List[
            Dict[str, Any]
        ] = []

        # -------------------------------------------------
        # Global duplicate prevention
        # -------------------------------------------------

        seen_urls = set()

        total_candidates = 0
        total_added = 0
        total_duplicate = 0
        total_unknown_year = 0

        # =================================================
        # Search target loop
        # =================================================

        for target in search_targets:
            if not isinstance(
                target,
                dict,
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
                    "[WARN] 無効な検索URLをスキップ: "
                    f"{url}"
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
                "[SEARCH] SUUMO検索開始: "
                f"{normalized_search_url}"
            )

            if target_area:
                print(
                    f"[SEARCH] 検索エリア: "
                    f"{target_area}"
                )

            if target_property_type:
                print(
                    "[SEARCH] 検索物件種別: "
                    f"{target_property_type}"
                )

            # -------------------------------------------------
            # Crawl
            # -------------------------------------------------

            candidates = (
                self.crawl_search_target(
                    {
                        **target,
                        "url": normalized_search_url,
                    }
                )
            )

            total_candidates += len(
                candidates
            )

            new_count = 0
            duplicate_count = 0

            # -------------------------------------------------
            # Add candidates
            # -------------------------------------------------

            for candidate in candidates:
                listing_url = (
                    candidate.get(
                        "sourceUrl"
                    )
                    or candidate.get(
                        "url"
                    )
                )

                if not listing_url:
                    continue

                listing_url = (
                    self.normalize_url(
                        listing_url,
                        normalized_search_url,
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
                # main.py input
                # =================================================

                properties.append(
                    {
                        "source": "suumo",

                        # -----------------------------------------
                        # Individual listing ID
                        # -----------------------------------------

                        "listingId": listing_id,

                        # -----------------------------------------
                        # Canonical individual listing URL
                        # -----------------------------------------

                        "sourceUrl": listing_url,
                        "url": listing_url,
                        "detailUrl": listing_url,

                        # -----------------------------------------
                        # Search metadata
                        # -----------------------------------------

                        "searchArea": candidate.get(
                            "searchArea"
                        ),

                        "searchPropertyType": candidate.get(
                            "searchPropertyType"
                        ),

                        "searchBuiltYear": candidate.get(
                            "builtYear"
                        ),

                        "searchCardText": candidate.get(
                            "cardText"
                        ),

                        "searchDetectedPropertyType": property_type,

                        "searchUrl": candidate.get(
                            "searchUrl"
                        ),

                        "searchPageUrl": candidate.get(
                            "searchPageUrl"
                        ),

                        "searchPageNumber": candidate.get(
                            "searchPageNumber"
                        ),

                        "searchPosition": candidate.get(
                            "searchPosition"
                        ),

                        "searchTarget": candidate.get(
                            "searchTarget"
                        ),

                        "searchTargetArea": candidate.get(
                            "searchTargetArea"
                        ),

                        "searchTargetPropertyType": candidate.get(
                            "searchTargetPropertyType"
                        ),

                        "discoveredAt": candidate.get(
                            "discoveredAt"
                        ),

                        # -----------------------------------------
                        # Detail state
                        # -----------------------------------------

                        "detailFetched": False,
                        "detailFetchSuccess": False,
                        "detailFetchStatus": "pending",
                        "detailFetchError": None,
                        "detailFetchErrorType": None,
                    }
                )

                new_count += 1
                total_added += 1

            print(
                "[SEARCH] SUUMO検索完了: "
                f"候補={len(candidates)}件 / "
                f"新規={new_count}件 / "
                f"重複={duplicate_count}件"
            )

            if self.interval > 0:
                time.sleep(
                    self.interval
                )

        # =====================================================
        # Final canonicalization / duplicate elimination
        # =====================================================

        unique_properties: List[
            Dict[str, Any]
        ] = []

        final_seen = set()

        for property_data in properties:
            source_url = (
                property_data.get(
                    "sourceUrl"
                )
                or property_data.get(
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
            # Canonical URL fields
            # -------------------------------------------------

            property_data[
                "sourceUrl"
            ] = source_url

            property_data[
                "url"
            ] = source_url

            property_data[
                "detailUrl"
            ] = source_url

            # -------------------------------------------------
            # Listing ID
            # -------------------------------------------------

            if not property_data.get(
                "listingId"
            ):
                property_data[
                    "listingId"
                ] = self.extract_listing_id(
                    source_url
                )

            unique_properties.append(
                property_data
            )

        # =====================================================
        # Statistics
        # =====================================================

        used_count = sum(
            1
            for property_data
            in unique_properties
            if property_data.get(
                "searchDetectedPropertyType"
            )
            == "中古戸建"
        )

        new_count = sum(
            1
            for property_data
            in unique_properties
            if property_data.get(
                "searchDetectedPropertyType"
            )
            == "新築戸建"
        )

        unknown_type_count = (
            len(unique_properties)
            - used_count
            - new_count
        )

        area_counts: Dict[
            str,
            int,
        ] = {}

        for property_data in unique_properties:
            area = (
                property_data.get(
                    "searchArea"
                )
                or "未指定"
            )

            area_counts[
                str(area)
            ] = (
                area_counts.get(
                    str(area),
                    0,
                )
                + 1
            )

        # =====================================================
        # Final log
        # =====================================================

        print(
            "========================================"
        )

        print(
            "[SEARCH] SUUMO検索完了"
        )

        print(
            f"[SEARCH] 検索候補総数: "
            f"{total_candidates}件"
        )

        print(
            f"[SEARCH] 新規追加: "
            f"{total_added}件"
        )

        print(
            f"[SEARCH] 検索条件間重複: "
            f"{total_duplicate}件"
        )

        print(
            f"[SEARCH] 個別物件: "
            f"{len(unique_properties)}件"
        )

        print(
            f"[SEARCH] 中古戸建: "
            f"{used_count}件"
        )

        print(
            f"[SEARCH] 新築戸建: "
            f"{new_count}件"
        )

        if unknown_type_count > 0:
            print(
                f"[SEARCH] 種別不明: "
                f"{unknown_type_count}件"
            )

        if total_unknown_year > 0:
            print(
                f"[SEARCH] 築年不明: "
                f"{total_unknown_year}件"
            )

        if area_counts:
            print(
                "[SEARCH] 検索条件エリア別:"
            )

            for area, count in sorted(
                area_counts.items()
            ):
                print(
                    f"  {area}: {count}件"
                )

        if self.min_built_year is not None:
            print(
                "[SEARCH] 検索段階の築年数基準: "
                f"{self.min_built_year}年以降"
            )

        print(
            "[SEARCH] ※ 築年不明物件は検索段階では除外せず、"
            "詳細ページ取得後に最終判定します。"
        )

        print(
            "[SEARCH] ※ 実所在地は検索条件エリアではなく、"
            "詳細ページのaddressを使用して判定します。"
        )

        print(
            "[SEARCH] ※ 個別物件URLはcanonical URLへ統一しています。"
        )

        print(
            "[SEARCH] ※ sourceUrl / url / detailUrlは"
            "同一物件について同一URLを保持します。"
        )

        print(
            "[SEARCH] ※ 検索URLのqueryは"
            "ページング・検索条件維持のため保持します。"
        )

        print(
            "========================================"
        )

        return unique_properties