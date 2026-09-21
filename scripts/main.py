from pathlib import Path
from datetime import datetime

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


def main():
    search_config = load_config()
    current_properties = []

    for adapter in create_adapters():
        results = adapter.search(
            search_config
        )

        current_properties.extend(results)

    output = {
        "updatedAt": datetime.now().isoformat(),
        "properties": current_properties
    }

    save_json(
        ROOT / "data" / "discovered_listings.json",
        output
    )

    print(
        "Discovered listings:",
        len(current_properties)
    )


if __name__ == "__main__":
    main()
