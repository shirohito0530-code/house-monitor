from pathlib import Path
from urllib.parse import urljoin, urlparse
import json
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
        path = (
            self.root_path
            / "config"
            / "search_urls.json"
        )

        if not path.exists():
            return []

        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return data.get(
            "suumo_search_urls",
            []
        )

    def is_valid_url(self, url):
        parsed = urlparse(url)

        if parsed.scheme != "https":
            return False

        hostname = parsed.hostname or ""

        return (
            hostname == "suumo.jp"
            or hostname.endswith(".suumo.jp")
        )

    def fetch_search_page(self, url):
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
        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        results = set()

        for link in soup.select("a[href]"):
            href = link.get("href")

            if not href:
                continue

            absolute_url = urljoin(
                base_url,
                href
            )

            if not self.is_valid_url(
                absolute_url
            ):
                continue

            # 詳細URLの判定ルールは
            # 実際のHTML確認後に調整する
            if (
                "chukoikkodate" in absolute_url
                or "ikkodate" in absolute_url
            ):
                results.add(absolute_url)

        return sorted(results)

    def search(self, search_config):
        search_targets = self.load_search_urls()
        properties = []

        for target in search_targets:
            url = target.get("url")

            if not url or not self.is_valid_url(url):
                continue

            try:
                html = self.fetch_search_page(url)

                listing_urls = (
                    self.extract_listing_urls(
                        html,
                        url
                    )
                )

                for listing_url in listing_urls:
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

                print(
                    f"SUUMO: {url} "
                    f"→ {len(listing_urls)} URLs"
                )

                time.sleep(self.interval)

            except requests.HTTPError as error:
                print(
                    f"HTTP ERROR: {url} / {error}"
                )

            except requests.RequestException as error:
                print(
                    f"REQUEST ERROR: {url} / {error}"
                )

        return properties
