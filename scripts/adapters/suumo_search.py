from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from adapters.base import PropertyAdapter


class SuumoSearchAdapter(PropertyAdapter):
    """
    SUUMO search result adapter.

    Responsibilities
    ----------------
    - Load enabled search targets from config/search_urls.json
    - Crawl SUUMO search result pages
    - Follow pagination
    - Extract individual detached-house listing URLs
    - Normalize listing URLs
    - Extract SUUMO listing ID (nc_xxxxx)
    - Detect property type from URL
    - Extract lightweight search-result metadata
    - Preserve search provenance
    - Report search health

    Important design rule
    ---------------------
    This adapter is a DISCOVERY layer.

    It must NOT perform final property screening such as:
      - actual address / area qualification
      - school district
      - exact building age
      - land area
      - building area
      - station walking time
      - price
      - flat land
      - retaining wall

    Those decisions belong to main.py after detail acquisition.

    Identity
    --------
    SUUMO listing identity is based on:
        suumo:nc_xxxxx

    Cross-target duplicate handling
    -------------------------------
    IMPORTANT:

    A listing appearing in multiple search targets must NOT be removed
    at this adapter level.

    Example:

        target A -> nc_123
        target B -> nc_123

    Both occurrences are returned.

    main.py then performs identity-based merging and records:

        searchTargets
        searchOccurrences
        searchPageNumbers

    Therefore:

        same target + same listing
            -> deduplicate here

        different target + same listing
            -> preserve here
            -> merge in main.py
    """

    # ============================================================
    # Constants
    # ============================================================

    SUUMO_HOST = "suumo.jp"

    SUUMO_HOSTS = {
        "suumo.jp",
        "www.suumo.jp",
    }

    USED_HOUSE_PATH = "/chukoikkodate/"
    NEW_HOUSE_PATH = "/ikkodate/"

    LISTING_ID_PATTERN = re.compile(
        r"/nc_\d+(?:/|$)",
        re.IGNORECASE,
    )

    LISTING_ID_EXTRACT_PATTERN = re.compile(
        r"/(nc_\d+)(?:/|$)",
        re.IGNORECASE,
    )

    # ============================================================
    # Initialization
    # ============================================================

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        root_path: Optional[str] = None,
    ):
        super().__init__(config or {})

        if root_path:
            self.root_path = Path(root_path)
        else:
            self.root_path = (
                Path(__file__).resolve().parents[2]
            )

        self.timeout = self._safe_int(
            self.config.get("timeout", 20),
            default=20,
            minimum=1,
        )

        self.max_pages = self._safe_int(
            self.config.get("maxPagesPerRun", 3),
            default=3,
            minimum=1,
        )

        self.interval = self._safe_float(
            self.config.get("intervalSeconds", 5),
            default=5.0,
            minimum=0.0,
        )

        self.last_search_healthy = True

        self.search_target_results: List[
            Dict[str, Any]
        ] = []

        self.session = requests.Session()

        self.session.headers.update(
            {
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
        )

    # ============================================================
    # Generic conversion helpers
    # ============================================================

    @staticmethod
    def _safe_int(
        value: Any,
        default: int,
        minimum: int = 0,
    ) -> int:
        try:
            return max(
                minimum,
                int(value),
            )
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(
        value: Any,
        default: float,
        minimum: float = 0.0,
    ) -> float:
        try:
            return max(
                minimum,
                float(value),
            )
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(
            timezone.utc
        ).isoformat()

    # ============================================================
    # Search URL loading
    # ============================================================

    def load_search_urls(
        self,
    ) -> List[Dict[str, Any]]:
        """
        Load enabled search targets.

        Supported formats:

        {
          "suumo_search_urls": [
            {...}
          ]
        }

        or:

        {
          "targets": [
            {...}
          ]
        }

        or directly:

        [
          {...}
        ]
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
            raw_targets = data

        elif isinstance(data, dict):
            raw_targets = data.get(
                "suumo_search_urls",
                [],
            )

            if not raw_targets:
                raw_targets = data.get(
                    "targets",
                    [],
                )

        else:
            print(
                "[WARN] 検索URL設定の形式が不正です"
            )
            return []

        if not isinstance(
            raw_targets,
            list,
        ):
            print(
                "[WARN] 検索URL設定は配列で指定してください"
            )
            return []

        enabled_targets: List[
            Dict[str, Any]
        ] = []

        seen_target_keys = set()

        for index, target in enumerate(
            raw_targets,
            start=1,
        ):
            if not isinstance(
                target,
                dict,
            ):
                print(
                    "[WARN] 検索ターゲット"
                    f"{index}をスキップ: dictではありません"
                )
                continue

            if target.get(
                "enabled",
                True,
            ) is False:
                continue

            url = target.get("url")

            if not url:
                print(
                    "[WARN] 検索ターゲット"
                    f"{index}をスキップ: URLなし"
                )
                continue

            normalized_url = (
                self.normalize_search_url(url)
            )

            if not normalized_url:
                print(
                    "[WARN] 無効なSUUMO検索URLをスキップ: "
                    f"{url}"
                )
                continue

            normalized_target = dict(target)

            normalized_target["url"] = (
                normalized_url
            )

            name = (
                normalized_target.get("name")
                or normalized_target.get("target")
                or (
                    f"{normalized_target.get('area', '')}_"
                    f"{normalized_target.get('propertyType', '')}"
                )
            )

            normalized_target["name"] = name

            target_key = (
                str(name),
                normalized_url,
            )

            if target_key in seen_target_keys:
                print(
                    "[WARN] 重複検索ターゲットをスキップ: "
                    f"{name}"
                )
                continue

            seen_target_keys.add(
                target_key
            )

            enabled_targets.append(
                normalized_target
            )

        return enabled_targets

    # ============================================================
    # URL validation
    # ============================================================

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

        return (
            hostname in self.SUUMO_HOSTS
            or hostname.endswith(
                "." + self.SUUMO_HOST
            )
        )

    # ============================================================
    # Absolute URL conversion
    # ============================================================

    def _make_absolute_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:

        if not url:
            return None

        original_url = str(
            url
        ).strip()

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

        if re.match(
            r"^(?:www\.)?suumo\.jp/",
            original_url,
            re.IGNORECASE,
        ):
            original_url = (
                "https://"
                + original_url
            )

        if original_url.startswith("//"):
            absolute_url = (
                "https:"
                + original_url
            )

        elif original_url.startswith("/"):
            absolute_url = urljoin(
                base_url or "https://suumo.jp/",
                original_url,
            )

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

    # ============================================================
    # Listing ID
    # ============================================================

    def extract_listing_id(
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

        match = (
            self.LISTING_ID_EXTRACT_PATTERN.search(
                parsed.path or ""
            )
        )

        if not match:
            return None

        return match.group(1).lower()

    # ============================================================
    # Listing URL normalization
    # ============================================================

    def normalize_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        """
        Canonicalize individual SUUMO detached-house listing URLs.

        Query and fragment are removed.

        Canonical output:

            https://suumo.jp/.../nc_xxxxx/
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

        listing_id = (
            self.extract_listing_id(
                absolute_url
            )
        )

        if not listing_id:
            return None

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

    # ============================================================
    # Search URL normalization
    # ============================================================

    def normalize_search_url(
        self,
        url: Any,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        """
        Canonicalize a SUUMO search URL.

        Query parameters are retained because they may contain
        search filters and pagination parameters.
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

    # ============================================================
    # Listing URL validation
    # ============================================================

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

    # ============================================================
    # Property type
    # ============================================================

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

    # ============================================================
    # HTTP
    # ============================================================

    def fetch_search_page(
        self,
        url: str,
    ) -> str:

        response = self.session.get(
            url,
            timeout=self.timeout,
            allow_redirects=True,
        )

        response.raise_for_status()

        return response.text

    # ============================================================
    # Built year extraction
    # ============================================================

    def extract_built_year(
        self,
        text: Any,
    ) -> Optional[int]:
        """
        Lightweight extraction only.

        This value is NOT authoritative.

        Exact construction date is obtained from the detail page.
        """

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
                year = int(
                    match.group(1)
                )

                current_year = (
                    datetime.now().year
                )

                if (
                    1800
                    <= year
                    <= current_year + 5
                ):
                    return year

            except ValueError:
                pass

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
                        datetime.now().year
                        - age
                    )

            except ValueError:
                pass

        return None

    # ============================================================
    # Card text
    # ============================================================

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

    # ============================================================
    # Listing candidate extraction
    # ============================================================

    def extract_listing_candidates(
        self,
        html: str,
        base_url: str,
    ) -> List[Dict[str, Any]]:
        """
        Extract detached-house listing candidates from one page.

        Deduplication is performed only within this page.

        Cross-page / cross-target handling is performed by
        crawl_search_target() and main.py respectively.
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

        seen_listing_ids = set()

        for link in soup.select(
            "a[href]"
        ):
            href = link.get("href")

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

            listing_id = (
                self.extract_listing_id(
                    normalized_url
                )
            )

            if not listing_id:
                continue

            if listing_id in seen_listing_ids:
                continue

            seen_listing_ids.add(
                listing_id
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
                    "source": "suumo",
                    "sourceId": listing_id,
                    "listingId": listing_id,
                    "sourceUrl": normalized_url,
                    "url": normalized_url,
                    "detailUrl": normalized_url,
                    "builtYear": built_year,
                    "cardText": card_text,
                    "propertyType": property_type,
                }
            )

        return results

    # ============================================================
    # Property type filter
    # ============================================================

    def is_property_type_allowed(
        self,
        property_type: Optional[str],
        target_property_type: Optional[str] = None,
    ) -> bool:
        """
        Conservative property-type filter.

        URL-derived property type is authoritative for the
        detached-house search paths.

        Unknown type is retained.
        """

        if not target_property_type:
            return True

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

        return True

    # ============================================================
    # Pagination
    # ============================================================

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

        # --------------------------------------------------------
        # 1. rel="next"
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # 2. Text / aria-label / title
        # --------------------------------------------------------

        for link in soup.select(
            "a[href]"
        ):
            href = link.get("href")

            if not href:
                continue

            text = link.get_text(
                " ",
                strip=True,
            )

            aria_label = (
                link.get(
                    "aria-label",
                    "",
                )
                or ""
            )

            title = (
                link.get(
                    "title",
                    "",
                )
                or ""
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

    # ============================================================
    # Crawl one target
    # ============================================================

    def crawl_search_target(
        self,
        url: Optional[str] = None,
        target: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Crawl one search target.

        IMPORTANT INTERFACE:

        main.py calls:

            crawl_search_target(
                url,
                target=target,
                config=config,
            )

        Therefore this method intentionally returns a LIST,
        not the old target_result dictionary.

        Per-target diagnostics are stored in:
            self.search_target_results

        Deduplication:
            - same page: deduplicated
            - same target across pages: deduplicated
            - different targets: NOT deduplicated
        """

        target = (
            target
            if isinstance(target, dict)
            else {}
        )

        config = (
            config
            if isinstance(config, dict)
            else self.config
        )

        if not url:
            url = target.get("url")

        if not url:
            self.last_search_healthy = False

            result = {
                "target": (
                    target.get("name")
                    or target.get("target")
                    or "unknown"
                ),
                "targetArea": target.get(
                    "area"
                ),
                "targetPropertyType": target.get(
                    "propertyType"
                ),
                "searchUrl": None,
                "success": False,
                "error": "Missing search URL",
                "pagesFetched": 0,
                "candidateCount": 0,
                "acceptedCount": 0,
                "duplicateCount": 0,
                "propertyTypeRejectedCount": 0,
                "unknownBuiltYearCount": 0,
                "pageStats": [],
                "candidates": [],
            }

            self.search_target_results.append(
                result
            )

            return []

        normalized_start_url = (
            self.normalize_search_url(
                url
            )
        )

        target_name = (
            target.get("name")
            or target.get("target")
            or url
            or "unknown"
        )

        target_area = target.get(
            "area"
        )

        target_property_type = (
            target.get("propertyType")
        )

        target_result: Dict[
            str,
            Any,
        ] = {
            "target": target_name,
            "targetArea": target_area,
            "targetPropertyType": target_property_type,
            "searchUrl": normalized_start_url,
            "success": True,
            "error": None,
            "pagesFetched": 0,
            "candidateCount": 0,
            "acceptedCount": 0,
            "duplicateCount": 0,
            "propertyTypeRejectedCount": 0,
            "unknownBuiltYearCount": 0,
            "pageStats": [],
            "candidates": [],
        }

        # --------------------------------------------------------
        # Reset health for this target.
        #
        # main.py checks this value after every target.
        # --------------------------------------------------------

        self.last_search_healthy = True

        if not normalized_start_url:
            target_result["success"] = False
            target_result["error"] = (
                "Invalid search URL"
            )

            self.last_search_healthy = False

            self.search_target_results.append(
                target_result
            )

            return []

        # --------------------------------------------------------
        # Target-local configuration
        # --------------------------------------------------------

        max_pages = self._safe_int(
            config.get(
                "maxPagesPerRun",
                self.max_pages,
            ),
            default=self.max_pages,
            minimum=1,
        )

        interval = self._safe_float(
            config.get(
                "intervalSeconds",
                self.interval,
            ),
            default=self.interval,
            minimum=0.0,
        )

        current_url = normalized_start_url

        all_candidates: List[
            Dict[str, Any]
        ] = []

        seen_page_urls = set()

        # IMPORTANT:
        # This set is LOCAL TO ONE SEARCH TARGET.
        #
        # It must never be shared across different targets,
        # otherwise main.py cannot record cross-target occurrences.
        seen_listing_ids = set()

        for page_number in range(
            1,
            max_pages + 1,
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
                f"{page_number}/{max_pages} "
                f"{current_url}"
            )

            try:
                html = (
                    self.fetch_search_page(
                        current_url
                    )
                )

            except requests.HTTPError as error:
                target_result["success"] = False
                target_result["error"] = (
                    f"HTTP error: {error}"
                )

                self.last_search_healthy = False

                print(
                    "[ERROR] SUUMO検索HTTPエラー: "
                    f"{current_url} / {error}"
                )

                break

            except requests.RequestException as error:
                target_result["success"] = False
                target_result["error"] = (
                    f"Request error: {error}"
                )

                self.last_search_healthy = False

                print(
                    "[ERROR] SUUMO検索通信エラー: "
                    f"{current_url} / {error}"
                )

                break

            except Exception as error:
                target_result["success"] = False
                target_result["error"] = (
                    f"Unexpected error: {error}"
                )

                self.last_search_healthy = False

                print(
                    "[ERROR] SUUMO検索取得失敗: "
                    f"{current_url} / {error}"
                )

                break

            target_result["pagesFetched"] += 1

            candidates = (
                self.extract_listing_candidates(
                    html,
                    current_url,
                )
            )

            page_accepted = 0
            page_duplicate = 0
            page_rejected_type = 0
            page_unknown_year = 0

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

                listing_url = (
                    self.normalize_url(
                        listing_url,
                        current_url,
                    )
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

                # ------------------------------------------------
                # Target-local duplicate
                # ------------------------------------------------

                if listing_id in seen_listing_ids:
                    page_duplicate += 1
                    continue

                property_type = (
                    candidate.get(
                        "propertyType"
                    )
                )

                # ------------------------------------------------
                # Conservative property-type filter
                # ------------------------------------------------

                if not self.is_property_type_allowed(
                    property_type,
                    target_property_type,
                ):
                    page_rejected_type += 1
                    continue

                built_year = candidate.get(
                    "builtYear"
                )

                if built_year is None:
                    page_unknown_year += 1

                # ------------------------------------------------
                # IMPORTANT:
                #
                # No age/price/area/walk filtering here.
                # ------------------------------------------------

                seen_listing_ids.add(
                    listing_id
                )

                discovered_at = (
                    self._now_iso()
                )

                candidate = dict(
                    candidate
                )

                candidate["source"] = (
                    "suumo"
                )

                candidate["sourceId"] = (
                    listing_id
                )

                candidate["listingId"] = (
                    listing_id
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

                # ------------------------------------------------
                # Search provenance
                # ------------------------------------------------

                candidate["searchArea"] = (
                    target_area
                )

                candidate[
                    "searchPropertyType"
                ] = target_property_type

                candidate["searchUrl"] = (
                    normalized_start_url
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
                    target_name
                )

                candidate[
                    "searchTargetArea"
                ] = target_area

                candidate[
                    "searchTargetPropertyType"
                ] = target_property_type

                candidate[
                    "discoveredAt"
                ] = discovered_at

                # ------------------------------------------------
                # Initial detail state
                # ------------------------------------------------

                candidate[
                    "detailFetched"
                ] = False

                candidate[
                    "detailFetchSuccess"
                ] = False

                candidate[
                    "detailFetchStatus"
                ] = "pending"

                candidate[
                    "detailFetchError"
                ] = None

                candidate[
                    "detailFetchErrorType"
                ] = None

                all_candidates.append(
                    candidate
                )

                page_accepted += 1

            page_stat = {
                "pageNumber": page_number,
                "pageUrl": current_url,
                "candidateCount": len(
                    candidates
                ),
                "acceptedCount": page_accepted,
                "duplicateCount": page_duplicate,
                "propertyTypeRejectedCount": (
                    page_rejected_type
                ),
                "unknownBuiltYearCount": (
                    page_unknown_year
                ),
            }

            target_result[
                "pageStats"
            ].append(
                page_stat
            )

            print(
                "[SEARCH] 検索ページ結果: "
                f"候補={len(candidates)}件 / "
                f"採用={page_accepted}件 / "
                f"築年不明={page_unknown_year}件 / "
                f"種別除外={page_rejected_type}件 / "
                f"重複={page_duplicate}件"
            )

            if page_number >= max_pages:
                break

            next_url = (
                self.extract_next_page_url(
                    html,
                    current_url,
                )
            )

            if not next_url:
                break

            if (
                next_url == current_url
                or next_url in seen_page_urls
            ):
                print(
                    "[WARN] 次ページURLが既取得ページと重複。停止"
                )
                break

            current_url = next_url

            if interval > 0:
                time.sleep(
                    interval
                )

        target_result[
            "candidates"
        ] = all_candidates

        target_result[
            "candidateCount"
        ] = len(all_candidates)

        target_result[
            "acceptedCount"
        ] = sum(
            int(
                stat.get(
                    "acceptedCount",
                    0,
                )
            )
            for stat in target_result[
                "pageStats"
            ]
        )

        target_result[
            "duplicateCount"
        ] = sum(
            int(
                stat.get(
                    "duplicateCount",
                    0,
                )
            )
            for stat in target_result[
                "pageStats"
            ]
        )

        target_result[
            "propertyTypeRejectedCount"
        ] = sum(
            int(
                stat.get(
                    "propertyTypeRejectedCount",
                    0,
                )
            )
            for stat in target_result[
                "pageStats"
            ]
        )

        target_result[
            "unknownBuiltYearCount"
        ] = sum(
            int(
                stat.get(
                    "unknownBuiltYearCount",
                    0,
                )
            )
            for stat in target_result[
                "pageStats"
            ]
        )

        if (
            target_result["success"]
            and target_result["pagesFetched"] > 0
            and not all_candidates
        ):
            print(
                "[WARN] SUUMO検索ページは取得できましたが、"
                "物件候補が0件でした: "
                f"{target_name}"
            )

        self.search_target_results.append(
            target_result
        )

        return all_candidates

    # ============================================================
    # Preferred main.py interface
    # ============================================================

    def fetch_search_results(
        self,
        url: Optional[str] = None,
        target: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Preferred search adapter interface.

        main.py fetch_search_target() calls this method first.

        This intentionally returns a list of candidates.
        """

        return self.crawl_search_target(
            url=url,
            target=target,
            config=config,
        )

    # ============================================================
    # Legacy / direct adapter interface
    # ============================================================

    def search(
        self,
        search_config: Optional[
            Dict[str, Any]
        ] = None,
    ) -> List[
        Dict[str, Any]
    ]:
        """
        Crawl all enabled search targets.

        This method is retained for direct adapter use.

        IMPORTANT:
        Unlike the previous implementation, it does NOT perform
        global cross-target deduplication.

        Therefore:

            target A -> nc_123
            target B -> nc_123

        produces two candidate records.

        main.py is responsible for identity merging.
        """

        config = (
            search_config
            if isinstance(
                search_config,
                dict,
            )
            else self.config
        )

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

        for target_index, target in enumerate(
            search_targets
        ):

            if not isinstance(
                target,
                dict,
            ):
                self.last_search_healthy = False
                continue

            url = target.get(
                "url"
            )

            if not url:
                self.last_search_healthy = False
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

                self.last_search_healthy = False
                continue

            print(
                "----------------------------------------"
            )

            print(
                "[SEARCH] SUUMO検索開始: "
                f"{target.get('name', normalized_search_url)}"
            )

            candidates = (
                self.crawl_search_target(
                    url=normalized_search_url,
                    target={
                        **target,
                        "url": normalized_search_url,
                    },
                    config=config,
                )
            )

            # crawl_search_target() resets health for each target,
            # so explicitly preserve global health here.
            target_result = (
                self.search_target_results[-1]
                if self.search_target_results
                else {}
            )

            if not target_result.get(
                "success",
                False,
            ):
                self.last_search_healthy = False

            for candidate in candidates:

                if not isinstance(
                    candidate,
                    dict,
                ):
                    continue

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

                # ------------------------------------------------
                # DO NOT deduplicate across targets.
                #
                # Each occurrence is intentionally preserved.
                # ------------------------------------------------

                property_data = dict(
                    candidate
                )

                property_data[
                    "source"
                ] = "suumo"

                property_data[
                    "sourceId"
                ] = listing_id

                property_data[
                    "listingId"
                ] = listing_id

                property_data[
                    "sourceUrl"
                ] = listing_url

                property_data[
                    "url"
                ] = listing_url

                property_data[
                    "detailUrl"
                ] = listing_url

                property_data[
                    "detailFetched"
                ] = False

                property_data[
                    "detailFetchSuccess"
                ] = False

                property_data[
                    "detailFetchStatus"
                ] = "pending"

                property_data[
                    "detailFetchError"
                ] = None

                property_data[
                    "detailFetchErrorType"
                ] = None

                properties.append(
                    property_data
                )

            print(
                "[SEARCH] SUUMO検索完了: "
                f"候補={len(candidates)}件"
            )

            if (
                self.interval > 0
                and target_index
                < len(search_targets) - 1
            ):
                time.sleep(
                    self.interval
                )

        print(
            "========================================"
        )

        print(
            "[SEARCH] 検索全体ヘルス状態: "
            + (
                "正常 (OK)"
                if self.last_search_healthy
                else "異常発生 (WARNING/FAILED)"
            )
        )

        print(
            f"[SEARCH] 検索ターゲット数: "
            f"{len(search_targets)}"
        )

        print(
            f"[SEARCH] ターゲット別候補レコード数: "
            f"{len(properties)}件"
        )

        print(
            "========================================"
        )

        return properties