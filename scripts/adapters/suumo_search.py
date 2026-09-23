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
        - duplicate elimination
        - search metadata attachment
        - search health tracking

    Design principle:
        Search layer should maximize discovery and avoid applying detailed
        property criteria. Detailed criteria such as building age, land size,
        effective LDK, retaining wall, builder quality, etc. are evaluated
        later by main.py / detail parser.

    Important:
        - Do NOT exclude listings based on building age here.
        - Do NOT exclude listings because search-card information is missing.
        - URL normalization is performed before duplicate elimination.
    """

    # =====================================================
    # Constants
    # =====================================================

    SUUMO_HOST = "suumo.jp"

    SUUMO_HOSTS = {
        "suumo.jp",
        "www.suumo.jp",
    }

    USED_HOUSE_PATH = "/chukoikkodate/"
    NEW_HOUSE_PATH = "/ikkodate/"

    LISTING_ID_PATTERN = re.compile(
        r"/nc_[0-9]+(?:/|$)",
        re.IGNORECASE,
    )

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
            self.root_path = Path(__file__).resolve().parents[2]

        # -------------------------------------------------
        # HTTP settings
        # -------------------------------------------------

        try:
            self.timeout = max(
                1,
                int(self.config.get("timeout", 20)),
            )
        except (TypeError, ValueError):
            self.timeout = 20

        try:
            self.max_pages = max(
                1,
                int(self.config.get("maxPagesPerRun", 3)),
            )
        except (TypeError, ValueError):
            self.max_pages = 3

        try:
            self.interval = max(
                0.0,
                float(self.config.get("intervalSeconds", 5)),
            )
        except (TypeError, ValueError):
            self.interval = 5.0

        self.last_search_healthy = True
        self.search_target_results: List[Dict[str, Any]] = []

    # =====================================================
    # Search URL loading
    # =====================================================

    def load_search_urls(
        self,
    ) -> List[Dict[str, Any]]:
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
                path.read_text(encoding="utf-8")
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
            print("[WARN] 検索URL設定の形式が不正です")
            return []

        if not isinstance(search_urls, list):
            print(
                "[WARN] suumo_search_urlsは配列で指定してください"
            )
            return []

        enabled_urls: List[Dict[str, Any]] = []
        seen_urls = set()

        for target in search_urls:
            if not isinstance(target, dict):
                continue

            if target.get("enabled", True) is False:
                continue

            url = target.get("url")

            if not url:
                continue

            normalized_url = self.normalize_search_url(url)

            if not normalized_url:
                print(
                    "[WARN] 無効なSUUMO検索URLをスキップ: "
                    f"{url}"
                )
                continue

            # 同一検索URLの二重登録を防止
            if normalized_url in seen_urls:
                continue

            seen_urls.add(normalized_url)

            normalized_target = dict(target)
            normalized_target["url"] = normalized_url

            enabled_urls.append(normalized_target)

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
            parsed = urlparse(str(url).strip())
        except ValueError:
            return False

        if parsed.scheme.lower() not in {
            "http",
            "https",
        }:
            return False

        hostname = (parsed.hostname or "").lower()

        return hostname in self.SUUMO_HOSTS

    # =====================================================
    # Absolute URL conversion
    # =====================================================

    def _make_absolute_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        if not url:
            return None

        original_url = str(url).strip()

        if not original_url:
            return None

        lower_url = original_url.lower()

        if (
            lower_url.startswith("#")
            or lower_url.startswith("javascript:")
            or lower_url.startswith("mailto:")
            or lower_url.startswith("tel:")
            or lower_url.startswith("data:")
        ):
            return None

        # malformed scheme recovery
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

        # www.suumo.jp/... without scheme
        if re.match(
            r"^(?:www\.)?suumo\.jp/",
            original_url,
            re.IGNORECASE,
        ):
            original_url = (
                "https://" + original_url
            )

        if original_url.startswith("//"):
            absolute_url = (
                "https:" + original_url
            )

        elif base_url:
            absolute_url = urljoin(
                base_url,
                original_url,
            )

        else:
            absolute_url = original_url

        if not self.is_valid_url(absolute_url):
            return None

        return absolute_url

    # =====================================================
    # Listing ID
    # =====================================================

    def extract_listing_id(
        self,
        url: Any,
    ) -> Optional[str]:
        if not url:
            return None

        try:
            parsed = urlparse(str(url))
        except ValueError:
            return None

        path = parsed.path or ""

        match = self.LISTING_ID_EXTRACT_PATTERN.search(path)

        if not match:
            return None

        return match.group(1).lower()

    # =====================================================
    # Path helpers
    # =====================================================

    def _is_house_path(
        self,
        path: str,
    ) -> bool:
        path_lower = (path or "").lower()

        return (
            path_lower.startswith(self.USED_HOUSE_PATH)
            or path_lower.startswith(self.NEW_HOUSE_PATH)
        )

    # =====================================================
    # Individual listing URL normalization
    # =====================================================

    def normalize_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        absolute_url = self._make_absolute_url(
            url,
            base_url,
        )

        if not absolute_url:
            return None

        try:
            parsed = urlparse(absolute_url)
        except ValueError:
            return None

        hostname = (parsed.hostname or "").lower()

        if hostname not in self.SUUMO_HOSTS:
            return None

        path = parsed.path or ""

        if not self._is_house_path(path):
            return None

        listing_id = self.extract_listing_id(
            absolute_url
        )

        if not listing_id:
            return None

        if not path.endswith("/"):
            path += "/"

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
        absolute_url = self._make_absolute_url(
            url,
            base_url,
        )

        if not absolute_url:
            return None

        try:
            parsed = urlparse(absolute_url)
        except ValueError:
            return None

        hostname = (parsed.hostname or "").lower()

        if hostname not in self.SUUMO_HOSTS:
            return None

        path = parsed.path or "/"

        # 個別物件URLを検索URLとして誤登録しない
        if self.extract_listing_id(absolute_url):
            return None

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
            parsed = urlparse(str(url))
        except ValueError:
            return False

        if parsed.scheme.lower() not in {
            "http",
            "https",
        }:
            return False

        hostname = (parsed.hostname or "").lower()

        if hostname not in self.SUUMO_HOSTS:
            return False

        path = parsed.path or ""

        if not self._is_house_path(path):
            return False

        return bool(
            self.LISTING_ID_PATTERN.search(path)
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
            parsed = urlparse(str(url))
        except ValueError:
            return None

        path = (parsed.path or "").lower()

        if path.startswith(self.USED_HOUSE_PATH):
            return "中古戸建"

        if path.startswith(self.NEW_HOUSE_PATH):
            return "新築戸建"

        return None

    # =====================================================
    # HTTP
    # =====================================================

    def fetch_search_page(
        self,
        url: str,
    ) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; HouseMonitor/1.0)"
            ),
            "Accept-Language": "ja,en;q=0.8",
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
        if not text:
            return None

        normalized = re.sub(
            r"\s+",
            " ",
            str(text),
        ).strip()

        match = re.search(
            r"(19\d{2}|20\d{2})年",
            normalized,
        )

        if match:
            try:
                year = int(match.group(1))
                current_year = datetime.now(
                    timezone.utc
                ).year

                if 1800 <= year <= current_year + 5:
                    return year

            except ValueError:
                pass

        match = re.search(
            r"築\s*(\d+)\s*年",
            normalized,
        )

        if match:
            try:
                age = int(match.group(1))

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
    # Card text
    # =====================================================

    def get_card_text(
        self,
        link,
    ) -> str:
        if link is None:
            return ""

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

        parent = link.parent

        for _ in range(5):
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
        if not html:
            return []

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: List[Dict[str, Any]] = []
        seen_listing_ids = set()

        for link in soup.select("a[href]"):
            href = link.get("href")

            if not href:
                continue

            normalized_url = self.normalize_url(
                href,
                base_url,
            )

            if not normalized_url:
                continue

            if not self.is_individual_listing_url(
                normalized_url
            ):
                continue

            listing_id = self.extract_listing_id(
                normalized_url
            )

            if not listing_id:
                continue

            # listingIdベースで重複排除
            if listing_id in seen_listing_ids:
                continue

            seen_listing_ids.add(listing_id)

            property_type = (
                self.get_property_type_from_url(
                    normalized_url
                )
            )

            card_text = self.get_card_text(link)

            built_year = self.extract_built_year(
                card_text
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

        # URLから種別判定できなかったものは捨てない
        if property_type is None:
            return True

        target = str(
            target_property_type
        ).strip()

        if target in {
            "中古戸建",
            "中古戸建て",
        }:
            return property_type == "中古戸建"

        if target in {
            "新築戸建",
            "新築戸建て",
        }:
            return property_type == "新築戸建"

        return True

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
        # 1. rel=next
        # -------------------------------------------------

        next_link = soup.select_one(
            'a[rel~="next"]'
        )

        if next_link:
            href = next_link.get("href")

            normalized = self.normalize_search_url(
                href,
                current_url,
            )

            if normalized:
                return normalized

        # -------------------------------------------------
        # 2. Common next-page patterns
        # -------------------------------------------------

        for link in soup.select("a[href]"):
            href = link.get("href")

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

            if not any(
                marker in combined
                for marker in (
                    "次へ",
                    "次のページ",
                    "次ページ",
                    "Next",
                    "next",
                    "›",
                    "»",
                )
            ):
                continue

            normalized = self.normalize_search_url(
                href,
                current_url,
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
    ) -> Dict[str, Any]:

        target_result = {
            "target": (
                target.get("name")
                or target.get("url")
            )
            if isinstance(target, dict)
            else None,
            "success": True,
            "error": None,
            "pagesFetched": 0,
            "candidates": [],
        }

        if not isinstance(target, dict):
            target_result["success"] = False
            target_result["error"] = (
                "Invalid target format"
            )
            return target_result

        start_url = target.get("url")
        target_property_type = target.get(
            "propertyType"
        )
        target_area = target.get("area")

        if not start_url:
            target_result["success"] = False
            target_result["error"] = (
                "Missing start URL"
            )
            return target_result

        current_url = self.normalize_search_url(
            start_url
        )

        if not current_url:
            target_result["success"] = False
            target_result["error"] = (
                "Invalid start URL"
            )
            return target_result

        all_candidates: List[
            Dict[str, Any]
        ] = []

        seen_page_urls = set()
        seen_listing_ids = set()

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

            try:
                html = self.fetch_search_page(
                    current_url
                )

            except Exception as error:
                print(
                    "[ERROR] SUUMO 検索取得失敗: "
                    f"{current_url} / {error}"
                )

                target_result["success"] = False
                target_result["error"] = str(
                    error
                )

                self.last_search_healthy = False
                break

            target_result[
                "pagesFetched"
            ] += 1

            candidates = (
                self.extract_listing_candidates(
                    html,
                    current_url,
                )
            )

            accepted = 0
            rejected_type = 0
            duplicate_count = 0
            unknown_year_count = 0

            for position_idx, candidate in enumerate(
                candidates,
                start=1,
            ):
                listing_url = (
                    candidate.get("sourceUrl")
                    or candidate.get("url")
                )

                if not listing_url:
                    continue

                listing_url = self.normalize_url(
                    listing_url,
                    current_url,
                )

                if not listing_url:
                    continue

                listing_id = (
                    candidate.get("listingId")
                    or self.extract_listing_id(
                        listing_url
                    )
                )

                if not listing_id:
                    continue

                if listing_id in seen_listing_ids:
                    duplicate_count += 1
                    continue

                property_type = candidate.get(
                    "propertyType"
                )

                if not self.is_property_type_allowed(
                    property_type,
                    target_property_type,
                ):
                    rejected_type += 1
                    continue

                built_year = candidate.get(
                    "builtYear"
                )

                if built_year is None:
                    unknown_year_count += 1

                seen_listing_ids.add(
                    listing_id
                )

                candidate["sourceUrl"] = (
                    listing_url
                )
                candidate["url"] = listing_url
                candidate["detailUrl"] = (
                    listing_url
                )
                candidate["listingId"] = (
                    listing_id
                )

                candidate["searchArea"] = (
                    target_area
                )

                candidate[
                    "searchPropertyType"
                ] = target_property_type

                candidate["searchUrl"] = (
                    current_url
                )

                candidate[
                    "searchPageUrl"
                ] = current_url

                candidate[
                    "searchPageNumber"
                ] = page_number

                candidate[
                    "searchPosition"
                ] = position_idx

                candidate["searchTarget"] = (
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
                ] = datetime.now(
                    timezone.utc
                ).isoformat()

                all_candidates.append(
                    candidate
                )

                accepted += 1

            print(
                "[SEARCH] 検索ページ結果: "
                f"候補={len(candidates)}件 / "
                f"採用={accepted}件 / "
                f"築年不明={unknown_year_count}件 / "
                f"種別除外={rejected_type}件 / "
                f"重複={duplicate_count}件"
            )

            if page_number >= self.max_pages:
                break

            next_url = (
                self.extract_next_page_url(
                    html,
                    current_url,
                )
            )

            if (
                not next_url
                or next_url == current_url
                or next_url in seen_page_urls
            ):
                break

            current_url = next_url

            if self.interval > 0:
                time.sleep(
                    self.interval
                )

        target_result[
            "candidates"
        ] = all_candidates

        # ページは正常取得できたが物件が0件だった場合
        # 「通信エラー」とは別のwarningとして扱う
        if (
            target_result["success"]
            and target_result["pagesFetched"] > 0
            and not all_candidates
        ):
            print(
                "[WARN] SUUMO検索ページは取得できましたが、"
                "物件候補が0件でした: "
                f"{current_url}"
            )

        return target_result

    # =====================================================
    # Main search
    # =====================================================

    def search(
        self,
        search_config: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Crawl all enabled SUUMO search URLs.

        search_config is accepted for interface compatibility.
        Actual targets are loaded from config/search_urls.json.
        """

        search_targets = (
            self.load_search_urls()
        )

        if not search_targets:
            print(
                "[WARN] SUUMO検索URLが設定されていません"
            )

            self.last_search_healthy = False
            self.search_target_results = []

            return []

        self.last_search_healthy = True
        self.search_target_results = []

        properties: List[
            Dict[str, Any]
        ] = []

        seen_listing_ids = set()

        total_candidates = 0
        total_added = 0
        total_duplicate = 0

        for target_index, target in enumerate(
            search_targets
        ):
            if not isinstance(
                target,
                dict,
            ):
                self.last_search_healthy = False
                continue

            url = target.get("url")

            if not url:
                self.last_search_healthy = False
                continue

            normalized_search_url = (
                self.normalize_search_url(url)
            )

            if not normalized_search_url:
                print(
                    "[WARN] 無効な検索URLをスキップ: "
                    f"{url}"
                )
                self.last_search_healthy = False
                continue

            print(
                "----------------------------------------"
            )

            print(
                "[SEARCH] SUUMO検索開始: "
                f"{normalized_search_url}"
            )

            target_res = (
                self.crawl_search_target(
                    {
                        **target,
                        "url": normalized_search_url,
                    }
                )
            )

            self.search_target_results.append(
                target_res
            )

            if not target_res.get(
                "success",
                False,
            ):
                self.last_search_healthy = False

            candidates = target_res.get(
                "candidates",
                [],
            )

            total_candidates += len(
                candidates
            )

            new_count = 0
            duplicate_count = 0

            for candidate in candidates:
                listing_url = (
                    candidate.get(
                        "sourceUrl"
                    )
                    or candidate.get("url")
                )

                if not listing_url:
                    continue

                listing_url = self.normalize_url(
                    listing_url,
                    normalized_search_url,
                )

                if not listing_url:
                    continue

                listing_id = (
                    candidate.get(
                        "listingId"
                    )
                    or self.extract_listing_id(
                        listing_url
                    )
                )

                if not listing_id:
                    continue

                if listing_id in seen_listing_ids:
                    duplicate_count += 1
                    total_duplicate += 1
                    continue

                seen_listing_ids.add(
                    listing_id
                )

                properties.append(
                    {
                        "source": "suumo",
                        "listingId": listing_id,
                        "sourceUrl": listing_url,
                        "url": listing_url,
                        "detailUrl": listing_url,

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

                        "searchDetectedPropertyType": candidate.get(
                            "propertyType"
                        ),

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

            # ターゲット間の待機
            # 最終ターゲット後は不要
            if (
                self.interval > 0
                and target_index
                < len(search_targets) - 1
            ):
                time.sleep(
                    self.interval
                )

        # =================================================
        # Final normalization
        # =================================================

        unique_properties: List[
            Dict[str, Any]
        ] = []

        final_seen = set()

        for property_data in properties:
            source_url = (
                property_data.get(
                    "sourceUrl"
                )
                or property_data.get("url")
            )

            if not source_url:
                continue

            normalized_url = self.normalize_url(
                source_url
            )

            if not normalized_url:
                continue

            listing_id = (
                property_data.get(
                    "listingId"
                )
                or self.extract_listing_id(
                    normalized_url
                )
            )

            if not listing_id:
                continue

            if listing_id in final_seen:
                continue

            final_seen.add(
                listing_id
            )

            property_data[
                "listingId"
            ] = listing_id

            property_data[
                "sourceUrl"
            ] = normalized_url

            property_data[
                "url"
            ] = normalized_url

            property_data[
                "detailUrl"
            ] = normalized_url

            unique_properties.append(
                property_data
            )

        print(
            "========================================"
        )

        print(
            "[SEARCH] 検索全体ヘルス状態: "
            f"{'正常 (OK)' if self.last_search_healthy else '異常発生 (WARNING/FAILED)'}"
        )

        print(
            f"[SEARCH] 検索候補総数: "
            f"{total_candidates}件"
        )

        print(
            f"[SEARCH] 新規追加総数: "
            f"{total_added}件"
        )

        print(
            f"[SEARCH] 重複総数: "
            f"{total_duplicate}件"
        )

        print(
            f"[SEARCH] 個別取得物件総数: "
            f"{len(unique_properties)}件"
        )

        print(
            "========================================"
        )

        return unique_properties