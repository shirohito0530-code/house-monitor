from pathlib import Path
from datetime import datetime, timezone
import hashlib

from storage import load_json, save_json
from adapters.suumo_search import SuumoSearchAdapter


ROOT = Path(__file__).resolve().parents[1]


def load_config():
    """
    config/search.jsonを読み込む
    """

    return load_json(
        ROOT / "config" / "search.json",
        default={}
    )


def load_sources():
    """
    config/sources.jsonを読み込む
    """

    return load_json(
        ROOT / "config" / "sources.json",
        default={"sources": []}
    )


def create_adapters():
    """
    有効なデータソースに対応する
    アダプターを作成する
    """

    sources = load_sources()
    adapters = []

    for source in sources.get(
        "sources",
        []
    ):

        if not source.get("enabled"):
            continue

        if source.get(
            "name"
        ) == "suumo_search":

            adapters.append(
                SuumoSearchAdapter(
                    config=source,
                    root_path=ROOT
                )
            )

    return adapters


def normalize_url(url):
    """
    URLを物件ID作成用に正規化する
    """

    if not url:
        return ""

    return (
        url
        .split("?")[0]
        .split("#")[0]
        .rstrip("/")
    )


def create_property_id(url):
    """
    URLを基に安定した物件IDを作成する。

    同一URLであれば、実行ごとに
    同じIDが生成される。
    """

    normalized_url = normalize_url(
        url
    )

    if not normalized_url:
        return None

    digest = hashlib.sha256(
        normalized_url.encode(
            "utf-8"
        )
    ).hexdigest()[:16]

    return f"suumo-{digest}"


def normalize_property(
    item,
    collected_at
):
    """
    新しく検出した物件の初期データを作成する。
    """

    url = item.get(
        "sourceUrl",
        ""
    )

    property_id = create_property_id(
        url
    )

    if not property_id:
        return None

    return {
        "id": property_id,
        "source": item.get(
            "source",
            "suumo"
        ),
        "sourceUrl": url,
        "searchArea": item.get(
            "searchArea"
        ),
        "searchPropertyType": item.get(
            "searchPropertyType"
        ),
        "status": "discovered",
        "detailFetched": False,
        "firstSeenAt": collected_at,
        "lastSeenAt": collected_at,
        "collectedAt": collected_at,
        "price": None,
        "priceHistory": [],
        "detail": {}
    }


def load_existing_properties():
    """
    既存の検出済み物件を読み込む。

    ファイルが存在しない場合は
    空の配列を返す。
    """

    path = (
        ROOT
        / "data"
        / "discovered_listings.json"
    )

    data = load_json(
        path,
        default={}
    )

    if not isinstance(
        data,
        dict
    ):
        return {}

    properties = data.get(
        "properties",
        []
    )

    if not isinstance(
        properties,
        list
    ):
        return {}

    result = {}

    for property_data in properties:

        if not isinstance(
            property_data,
            dict
        ):
            continue

        property_id = property_data.get(
            "id"
        )

        if not property_id:
            source_url = property_data.get(
                "sourceUrl",
                ""
            )

            property_id = create_property_id(
                source_url
            )

        if not property_id:
            continue

        property_data["id"] = property_id

        result[property_id] = property_data

    return result


def merge_property(
    existing,
    current,
    collected_at
):
    """
    既存物件と今回検出データを統合する。

    既存の詳細情報や価格履歴は保持し、
    検出日時だけ更新する。
    """

    if existing is None:
        return current

    merged = existing.copy()

    # 今回の検索で取得した基本情報を更新
    if current.get(
        "sourceUrl"
    ):
        merged["sourceUrl"] = current[
            "sourceUrl"
        ]

    if current.get(
        "source"
    ):
        merged["source"] = current[
            "source"
        ]

    if current.get(
        "searchArea"
    ):
        merged["searchArea"] = current[
            "searchArea"
        ]

    if current.get(
        "searchPropertyType"
    ):
        merged["searchPropertyType"] = current[
            "searchPropertyType"
        ]

    # 初回検出日を維持
    if not merged.get(
        "firstSeenAt"
    ):
        merged["firstSeenAt"] = (
            current.get(
                "firstSeenAt",
                collected_at
            )
        )

    # 最終確認日時を更新
    merged["lastSeenAt"] = collected_at

    # 既存形式との互換性維持
    merged["collectedAt"] = collected_at

    # 既存ステータスを維持
    if not merged.get(
        "status"
    ):
        merged["status"] = "discovered"

    # 詳細取得状態を維持
    if "detailFetched" not in merged:
        merged["detailFetched"] = False

    # 価格履歴を初期化
    if not isinstance(
        merged.get("priceHistory"),
        list
    ):
        merged["priceHistory"] = []

    # 詳細情報を初期化
    if not isinstance(
        merged.get("detail"),
        dict
    ):
        merged["detail"] = {}

    return merged


def merge_properties(
    existing_properties,
    current_properties,
    collected_at
):
    """
    既存物件と今回検出物件を統合する。
    """

    merged_properties = (
        existing_properties.copy()
    )

    for current in current_properties:

        property_id = current.get(
            "id"
        )

        if not property_id:
            continue

        existing = merged_properties.get(
            property_id
        )

        merged_properties[property_id] = (
            merge_property(
                existing,
                current,
                collected_at
            )
        )

    return merged_properties


def build_output(
    properties,
    collected_at
):
    """
    保存用JSONの構造を作成する。
    """

    property_list = list(
        properties.values()
    )

    property_list.sort(
        key=lambda item: (
            item.get(
                "lastSeenAt",
                ""
            ),
            item.get(
                "id",
                ""
            )
        ),
        reverse=True
    )

    return {
        "updatedAt": collected_at,
        "summary": {
            "discoveredCount": len(
                property_list
            )
        },
        "properties": property_list
    }


def main():
    """
    メイン処理。

    1. SUUMO検索
    2. 新規検出データ作成
    3. 既存データと統合
    4. JSON保存
    """

    search_config = load_config()

    collected_at = datetime.now(
        timezone.utc
    ).isoformat()

    current_properties = []

    for adapter in create_adapters():

        results = adapter.search(
            search_config
        )

        for item in results:

            normalized = normalize_property(
                item,
                collected_at
            )

            if normalized is None:
                continue

            current_properties.append(
                normalized
            )

    # 既存物件を読み込み
    existing_properties = (
        load_existing_properties()
    )

    # 今回の検出結果をID単位で重複排除
    current_unique = {}

    for property_data in current_properties:

        property_id = property_data.get(
            "id"
        )

        if not property_id:
            continue

        current_unique[property_id] = (
            property_data
        )

    # 既存データと今回の結果を統合
    merged_properties = merge_properties(
        existing_properties,
        current_unique,
        collected_at
    )

    output = build_output(
        merged_properties,
        collected_at
    )

    save_json(
        ROOT
        / "data"
        / "discovered_listings.json",
        output
    )

    print(
        "今回の新規・検出物件数:",
        len(current_unique)
    )

    print(
        "保存済み物件総数:",
        len(merged_properties)
    )


if __name__ == "__main__":
    main()
