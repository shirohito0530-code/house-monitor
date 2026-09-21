import json
from pathlib import Path

from adapters.suumo import SuumoAdapter
from adapters.homes import HomesAdapter
from normalize import normalize_property
from storage import load_json, save_json
from analytics import merge_properties


ROOT = Path(__file__).resolve().parent.parent
SEARCH_FILE = ROOT / "config" / "search.json"
SOURCES_FILE = ROOT / "config" / "sources.json"
HOUSES_FILE = ROOT / "data" / "houses.json"


def load_config(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def create_adapters(sources_config):
    adapters = []

    for source in sources_config["sources"]:
        if not source.get("enabled", False):
            continue

        name = source["name"]

        if name == "suumo":
            adapters.append(SuumoAdapter(source))

        elif name == "homes":
            adapters.append(HomesAdapter(source))

    return adapters


def main():
    search_config = load_config(SEARCH_FILE)
    sources_config = load_config(SOURCES_FILE)

    adapters = create_adapters(sources_config)

    collected = []

    for adapter in adapters:
        try:
            results = adapter.search(search_config)
            collected.extend(results)

        except Exception as error:
            print(f"取得エラー: {error}")

    normalized = []

    for property_data in collected:
        try:
            item = normalize_property(property_data)
            normalized.append(item)

        except Exception as error:
            print(f"正規化エラー: {error}")

    existing_data = load_json(
        HOUSES_FILE,
        default={"updatedAt": None, "properties": []}
    )

    updated_data = merge_properties(
        existing_data,
        normalized
    )

    save_json(HOUSES_FILE, updated_data)

    print(f"取得件数: {len(collected)}")
    print(f"正規化件数: {len(normalized)}")


if __name__ == "__main__":
    main()
