import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from storage import load_json, save_json
from normalize import normalize_property
from analytics import merge_properties
from adapters.manual import ManualAdapter


SEARCH_FILE = ROOT / "config" / "search.json"
SOURCES_FILE = ROOT / "config" / "sources.json"
HOUSES_FILE = ROOT / "data" / "houses.json"


def create_adapters(sources_config):
    adapters = []

    for source in sources_config.get("sources", []):
        if not source.get("enabled", False):
            continue

        name = source.get("name")

        if name == "manual":
            adapters.append(
                ManualAdapter(source, ROOT)
            )

        else:
            print(
                f"未実装の取得元をスキップ: {name}"
            )

    return adapters


def main():
    search_config = load_json(
        SEARCH_FILE,
        default={}
    )

    sources_config = load_json(
        SOURCES_FILE,
        default={"sources": []}
    )

    adapters = create_adapters(sources_config)

    collected = []

    for adapter in adapters:
        try:
            results = adapter.search(search_config)
            collected.extend(results)

            print(
                f"{adapter.__class__.__name__}: "
                f"{len(results)}件取得"
            )

        except Exception as error:
            print(
                f"取得エラー: "
                f"{adapter.__class__.__name__}: {error}"
            )

    normalized = []

    for property_data in collected:
        try:
            normalized.append(
                normalize_property(property_data)
            )

        except Exception as error:
            print(f"正規化エラー: {error}")

    existing_data = load_json(
        HOUSES_FILE,
        default={
            "updatedAt": None,
            "properties": []
        }
    )

    updated_data = merge_properties(
        existing_data,
        normalized,
        search_config
    )

    save_json(HOUSES_FILE, updated_data)

    print(f"総取得件数: {len(collected)}")
    print(f"正規化件数: {len(normalized)}")
    print(
        f"保存件数: "
        f"{len(updated_data['properties'])}"
    )


if __name__ == "__main__":
    main()
