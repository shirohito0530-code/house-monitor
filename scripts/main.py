from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
import hashlib

from storage import load_json, save_json
from adapters.suumo_search import SuumoSearchAdapter


ROOT = Path(__file__).resolve().parents[1]


def load_config():
    return load_json(
        ROOT / "config" / "search.json",
        default={}
    )


def load_sources():
    return load_json(
        ROOT / "config" / "sources.json",
        default={"sources": []}
    )


def create_adapters():
    sources = load_sources()
    adapters = []

    for source in sources.get("sources", []):
        if not source.get("enabled"):
            continue

        if source.get("name") == "suumo_search":
            adapters.append(
                SuumoSearchAdapter(
                    config=source,
                    root_path=ROOT
                )
            )

    return adapters


def create_property_id(url):
    """
    URLを基に暫定的な物件IDを作成する。
    詳細情報取得後に、物件情報ベースのIDへ改善する。
    """
    normalized_url = url.split("?")[0].rstrip("/")
    digest = hashlib.sha256(
        normalized_url.encode("utf-8")
    ).hexdigest()[:16]

    return f"suumo-{digest}"


def normalize_property(item, collected_at):
    url = item.get("sourceUrl", "")

    return {
        "id": create_property_id(url),
        "source": item.get("source", "suumo"),
        "sourceUrl": url,
        "searchArea": item.get("searchArea"),
        "searchPropertyType": item.get(
            "searchPropertyType"
        ),
        "status": "discovered",
        "detailFetched": False,
        "collectedAt": collected_at
    }


def deduplicate_properties(properties):
    result = {}

    for property_data in properties:
        property_id = property_data.get("id")

        if not property_id:
            continue

        result[property_id] = property_data

    return list(result.values())


def main():
    search_config = load_config()
    collected_at = datetime.now(
        timezone.utc
    ).isoformat()

    discovered = []

    for adapter in create_adapters():
        results = adapter.search(search_config)

        for item in results:
            discovered.append(
                normalize_property(
                    item,
                    collected_at
                )
            )

    discovered = deduplicate_properties(
        discovered
    )

    output = {
        "updatedAt": collected_at,
        "summary": {
            "discoveredCount": len(discovered)
        },
        "properties": discovered
    }

    save_json(
        ROOT / "data" / "discovered_listings.json",
        output
    )

    print(
        "Discovered listings:",
        len(discovered)
    )


if __name__ == "__main__":
    main()
