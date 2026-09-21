import logging
from datetime import datetime, timezone

from adapters.suumo_detail import fetch_detail

from pathlib import Path
from datetime import datetime, timezone
import hashlib

from storage import load_json, save_json
from adapters.suumo_search import SuumoSearchAdapter
from adapters.suumo_detail import SuumoDetailAdapter


ROOT = Path(__file__).resolve().parents[1]


def load_config():
    """
    config/search.jsonを読み込む。
    """

    return load_json(
        ROOT / "config" / "search.json",
        default={}
    )


def load_sources():
    """
    config/sources.jsonを読み込む。
    """

    return load_json(
        ROOT / "config" / "sources.json",
        default={"sources": []}
    )


def create_adapters():
    """
    有効なデータソースに対応する
    アダプターを作成する。
    """

    sources = load_sources()

    adapters = []

    for source in sources.get(
        "sources",
        []
    ):

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


def create_detail_adapter():
    """
    詳細情報取得アダプターを作成する。
    """

    sources = load_sources()

    for source in sources.get(
        "sources",
        []
    ):

        if source.get("name") == "suumo_search":

            return SuumoDetailAdapter(
                config=source,
                root_path=ROOT
            )

    return SuumoDetailAdapter(
        config={},
        root_path=ROOT
    )


def normalize_url(url):
    """
    URLを物件ID作成用に正規化する。
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

    if not isinstance(
        item,
        dict
    ):
        return None

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
        "detailFetchedAt": None,
        "detailFetchError": None,
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

        # 既存データとの互換性維持
        property_data.setdefault(
            "detailFetched",
            False
        )

        property_data.setdefault(
            "detailFetchedAt",
            None
        )

        property_data.setdefault(
            "detailFetchError",
            None
        )

        if not isinstance(
            property_data.get(
                "detail"
            ),
            dict
        ):
            property_data["detail"] = {}

        if not isinstance(
            property_data.get(
                "priceHistory"
            ),
            list
        ):
            property_data["priceHistory"] = []

        result[property_id] = property_data

    return result


def merge_property(
    existing,
    current,
    collected_at
):
    """
    既存物件と今回検出データを統合する。
    """

    if existing is None:
        return current

    merged = existing.copy()

    if current.get("sourceUrl"):
        merged["sourceUrl"] = current[
            "sourceUrl"
        ]

    if current.get("source"):
        merged["source"] = current[
            "source"
        ]

    if current.get("searchArea"):
        merged["searchArea"] = current[
            "searchArea"
        ]

    if current.get("searchPropertyType"):
        merged["searchPropertyType"] = current[
            "searchPropertyType"
        ]

    if not merged.get("firstSeenAt"):
        merged["firstSeenAt"] = current.get(
            "firstSeenAt",
            collected_at
        )

    merged["lastSeenAt"] = collected_at
    merged["collectedAt"] = collected_at

    if not merged.get("status"):
        merged["status"] = "discovered"

    merged.setdefault(
        "detailFetched",
        False
    )

    merged.setdefault(
        "detailFetchedAt",
        None
    )

    merged.setdefault(
        "detailFetchError",
        None
    )

    if not isinstance(
        merged.get("priceHistory"),
        list
    ):
        merged["priceHistory"] = []

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

    if not isinstance(
        existing_properties,
        dict
    ):
        existing_properties = {}

    merged_properties = (
        existing_properties.copy()
    )

    if isinstance(
        current_properties,
        dict
    ):

        property_items = (
            current_properties.values()
        )

    elif isinstance(
        current_properties,
        list
    ):

        property_items = current_properties

    else:

        print(
            "物件データの形式が不正です"
        )

        return merged_properties

    for current in property_items:

        if not isinstance(
            current,
            dict
        ):

            print(
                "不正な物件データをスキップ:",
                current
            )

            continue

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


def add_price_history(
    property_data,
    new_price,
    fetched_at
):
    """
    価格が変わった場合のみ履歴に追加する。
    """

    if not new_price:
        return

    previous_price = property_data.get(
        "price"
    )

    history = property_data.get(
        "priceHistory"
    )

    if not isinstance(
        history,
        list
    ):
        history = []

    if previous_price == new_price:
        property_data["priceHistory"] = history
        return

    history.append({
        "price": new_price,
        "recordedAt": fetched_at
    })

    property_data["priceHistory"] = history
    property_data["price"] = new_price


def fetch_details(
    properties,
    detail_adapter,
    max_count
):
    """
    詳細未取得の物件から、
    最大max_count件だけ詳細情報を取得する。
    """

    fetched_count = 0
    success_count = 0
    error_count = 0

    for property_id, property_data in properties.items():

        if fetched_count >= max_count:
            break

        if not isinstance(
            property_data,
            dict
        ):
            continue

        # 詳細取得済みの物件はスキップ
        if property_data.get(
            "detailFetched"
        ):
            continue

        url = property_data.get(
            "sourceUrl"
        )

        if not url:
            continue

        print(
            "詳細情報取得開始:",
            property_id,
            url
        )

        result = detail_adapter.fetch_detail(
            url
        )

        fetched_count += 1

        fetched_at = result.get(
            "fetchedAt"
        )

        property_data[
            "detailFetchedAt"
        ] = fetched_at

        if result.get("success"):

            detail = result.get(
                "detail",
                {}
            )

            if not isinstance(
                detail,
                dict
            ):
                detail = {}

            existing_detail = property_data.get(
                "detail"
            )

            if not isinstance(
                existing_detail,
                dict
            ):
                existing_detail = {}

            existing_detail.update(
                detail
            )

            property_data[
                "detail"
            ] = existing_detail

            new_price = detail.get(
                "price"
            )

            add_price_history(
                property_data,
                new_price,
                fetched_at
            )

            property_data[
                "detailFetched"
            ] = True

            property_data[
                "detailFetchError"
            ] = None

            success_count += 1

            print(
                "詳細情報取得成功:",
                property_id
            )

        else:

            property_data[
                "detailFetchError"
            ] = result.get(
                "error",
                "Unknown error"
            )

            error_count += 1

            print(
                "詳細情報取得失敗:",
                property_id,
                property_data[
                    "detailFetchError"
                ]
            )

        detail_adapter.wait()

    print(
        "詳細情報取得結果:",
        f"処理 {fetched_count}件 / "
        f"成功 {success_count}件 / "
        f"失敗 {error_count}件"
    )

    return properties


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
            ),
            "detailFetchedCount": sum(
                1
                for item in property_list
                if item.get(
                    "detailFetched"
                )
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
    4. 詳細情報取得
    5. JSON保存
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

        if not isinstance(
            results,
            list
        ):

            print(
                "検索結果がリスト形式ではありません"
            )

            continue

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

    # 今回の検出結果をID単位で重複排除
    current_unique = {}

    for property_data in current_properties:

        if not isinstance(
            property_data,
            dict
        ):
            continue

        property_id = property_data.get(
            "id"
        )

        if not property_id:
            continue

        current_unique[property_id] = (
            property_data
        )

    # 既存物件を読み込み
    existing_properties = (
        load_existing_properties()
    )

    # 既存データと今回の結果を統合
    merged_properties = merge_properties(
        existing_properties,
        current_unique,
        collected_at
    )

    # 詳細取得件数
    detail_adapter = create_detail_adapter()

    max_detail_count = int(
        search_config.get(
            "detailFetchLimit",
            3
        )
    )

    if max_detail_count < 0:
        max_detail_count = 0

    # 詳細情報を取得
    merged_properties = fetch_details(
        merged_properties,
        detail_adapter,
        max_detail_count
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
        "今回の検出物件数:",
        len(current_unique)
    )

    print(
        "保存済み物件総数:",
        len(merged_properties)
    )

    print(
        "詳細取得上限:",
        max_detail_count
    )


if __name__ == "__main__":
    main()
