import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from storage import load_json, save_json
from adapters.suumo_search import SuumoSearchAdapter
from adapters.suumo_detail import SuumoDetailAdapter


# ============================================================
# 基本設定
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DETAIL_FETCH_LIMIT = 5

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ============================================================
# 共通ユーティリティ
# ============================================================

def now_iso():
    """
    UTCのISO 8601形式時刻を返す。
    """

    return datetime.now(
        timezone.utc
    ).isoformat()


def safe_int(value):
    """
    値を安全に整数へ変換する。
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    if isinstance(value, str):
        text = value.strip()
        text = text.replace(",", "")

        match = re.search(
            r"-?[0-9]+",
            text
        )

        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None

    return None


def parse_price(value):
    """
    価格を円単位の整数へ変換する。

    対応例:
      1,780万円 -> 17800000
      1780万円 -> 17800000
      1億2,000万円 -> 120000000
      17800000円 -> 17800000
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        price = int(value)

        if price > 0:
            return price

        return None

    text = str(value).strip()

    if not text:
        return None

    text = text.replace(",", "")
    text = text.replace(" ", "")
    text = text.replace("　", "")

    # 億円 + 万円
    oku_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)億"
        r"(?:([0-9]+(?:\.[0-9]+)?)万)?",
        text
    )

    if oku_match:
        oku = float(
            oku_match.group(1)
        )

        man = float(
            oku_match.group(2) or 0
        )

        price = int(
            oku * 100_000_000
            + man * 10_000
        )

        if price > 0:
            return price

    # 万円
    man_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)万(?:円)?",
        text
    )

    if man_match:
        price = int(
            float(man_match.group(1))
            * 10_000
        )

        if price > 0:
            return price

    # 円
    yen_match = re.search(
        r"([0-9][0-9,]*)円",
        text
    )

    if yen_match:
        price = int(
            yen_match.group(1).replace(",", "")
        )

        if price > 0:
            return price

    # 単位がない数値は、円単位として扱わない
    return None


def normalize_price_history(
    history,
    current_price,
    recorded_at
):
    """
    価格履歴を正規化する。

    旧形式の文字列価格にも対応する。
    """

    if not isinstance(
        history,
        list
    ):
        history = []

    normalized = []

    for item in history:

        if not isinstance(
            item,
            dict
        ):
            continue

        price = parse_price(
            item.get("price")
        )

        if price is None:
            continue

        recorded_time = item.get(
            "recordedAt"
        )

        if not recorded_time:
            recorded_time = recorded_at

        normalized.append({
            "price": price,
            "recordedAt": recorded_time
        })

    # 同一価格・同一記録時刻の重複を削除
    deduplicated = []

    seen = set()

    for item in normalized:

        key = (
            item["price"],
            item["recordedAt"]
        )

        if key in seen:
            continue

        seen.add(key)
        deduplicated.append(item)

    normalized = deduplicated

    current_price = parse_price(
        current_price
    )

    if current_price is None:
        return normalized

    # 最新履歴と同一価格なら追加しない
    latest_price = None

    if normalized:
        latest_price = normalized[-1].get(
            "price"
        )

    if latest_price != current_price:
        normalized.append({
            "price": current_price,
            "recordedAt": recorded_at
        })

    return normalized


# ============================================================
# 設定・アダプター
# ============================================================

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
        default={
            "sources": []
        }
    )


def create_adapters():
    """
    有効な検索データソースに対応する
    アダプターを作成する。
    """

    sources = load_sources()

    adapters = []

    for source in sources.get(
        "sources",
        []
    ):

        if not isinstance(
            source,
            dict
        ):
            continue

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

        if not isinstance(
            source,
            dict
        ):
            continue

        if source.get("name") == "suumo_search":

            return SuumoDetailAdapter(
                config=source,
                root_path=ROOT
            )

    return SuumoDetailAdapter(
        config={},
        root_path=ROOT
    )


# ============================================================
# 物件ID・URL処理
# ============================================================

def normalize_url(url):
    """
    URLを物件ID作成用に正規化する。
    """

    if not url:
        return ""

    return (
        str(url)
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
        normalized_url.encode("utf-8")
    ).hexdigest()[:16]

    return f"suumo-{digest}"


# ============================================================
# 物件データ初期化
# ============================================================

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
        "priceText": None,
        "priceHistory": [],

        "detailQuality": None,
        "detailQualityScore": None,
        "missingFields": [],
        "validationWarnings": [],
        "extractionQuality": {},

        "detail": {}
    }


# ============================================================
# 既存物件の読み込み
# ============================================================

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

        property_data.setdefault(
            "price",
            None
        )

        property_data.setdefault(
            "priceText",
            None
        )

        property_data.setdefault(
            "detailQuality",
            None
        )

        property_data.setdefault(
            "detailQualityScore",
            None
        )

        property_data.setdefault(
            "missingFields",
            []
        )

        property_data.setdefault(
            "validationWarnings",
            []
        )

        property_data.setdefault(
            "extractionQuality",
            {}
        )

        if not isinstance(
            property_data.get("detail"),
            dict
        ):
            property_data["detail"] = {}

        if not isinstance(
            property_data.get("priceHistory"),
            list
        ):
            property_data["priceHistory"] = []

        result[property_id] = property_data

    return result


# ============================================================
# 物件データ統合
# ============================================================

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

    merged.setdefault(
        "price",
        None
    )

    merged.setdefault(
        "priceText",
        None
    )

    merged.setdefault(
        "detailQuality",
        None
    )

    merged.setdefault(
        "detailQualityScore",
        None
    )

    merged.setdefault(
        "missingFields",
        []
    )

    merged.setdefault(
        "validationWarnings",
        []
    )

    merged.setdefault(
        "extractionQuality",
        {}
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

        logger.warning(
            "物件データの形式が不正です"
        )

        return merged_properties

    for current in property_items:

        if not isinstance(
            current,
            dict
        ):

            logger.warning(
                "不正な物件データをスキップ: %s",
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


# ============================================================
# 価格履歴
# ============================================================

def add_price_history(
    property_data,
    new_price,
    fetched_at
):
    """
    価格履歴を正規化して保存する。
    """

    new_price = parse_price(
        new_price
    )

    history = normalize_price_history(
        property_data.get("priceHistory"),
        None,
        fetched_at
    )

    if new_price is None:
        property_data["priceHistory"] = history
        return

    previous_price = parse_price(
        property_data.get("price")
    )

    property_data["priceHistory"] = (
        normalize_price_history(
            history,
            new_price,
            fetched_at
        )
    )

    property_data["price"] = new_price

    if previous_price != new_price:
        logger.info(
            "価格変更を記録: %s -> %s",
            previous_price,
            new_price
        )


# ============================================================
# 詳細情報取得
# ============================================================

def apply_detail_to_property(
    property_data,
    detail,
    fetched_at
):
    """
    詳細取得結果を物件データへ反映する。
    """

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

    property_data["detail"] = (
        existing_detail
    )

    # 価格を数値化
    new_price = parse_price(
        detail.get("price")
    )

    price_text = detail.get(
        "priceText"
    )

    if price_text:
        property_data["priceText"] = (
            price_text
        )

    add_price_history(
        property_data,
        new_price,
        fetched_at
    )

    # 詳細項目を物件本体にも保存
    copy_fields = [
        "address",
        "landAreaM2",
        "landAreaText",
        "buildingAreaM2",
        "buildingAreaText",
        "layout",
        "builtYear",
        "builtMonth",
        "builtYearText",
        "station",
        "walkMinutes",
        "transportRaw",
        "builder",
        "detailQuality",
        "detailQualityScore",
        "missingFields",
        "validationWarnings",
        "extractionQuality"
    ]

    for field in copy_fields:

        if field not in detail:
            continue

        property_data[field] = (
            detail.get(field)
        )


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

    for property_id, property_data in (
        properties.items()
    ):

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

        logger.info(
            "詳細情報取得開始: %s %s",
            property_id,
            url
        )

        fetched_count += 1

        fetched_at = now_iso()

        try:

            result = detail_adapter.fetch_detail(
                url
            )

        except Exception as error:

            logger.exception(
                "詳細情報取得中に例外発生: %s",
                property_id
            )

            property_data[
                "detailFetched"
            ] = False

            property_data[
                "detailFetchedAt"
            ] = fetched_at

            property_data[
                "detailFetchError"
            ] = str(error)

            error_count += 1

            try:
                detail_adapter.wait()
            except Exception:
                pass

            continue

        if not isinstance(
            result,
            dict
        ):

            result = {
                "success": False,
                "error": (
                    "詳細取得結果が辞書形式ではありません"
                )
            }

        result_fetched_at = result.get(
            "fetchedAt"
        )

        if not result_fetched_at:
            result_fetched_at = fetched_at

        property_data[
            "detailFetchedAt"
        ] = result_fetched_at

        if result.get("success"):

            detail = result.get(
                "detail",
                {}
            )

            apply_detail_to_property(
                property_data,
                detail,
                result_fetched_at
            )

            property_data[
                "detailFetched"
            ] = True

            property_data[
                "detailFetchError"
            ] = None

            success_count += 1

            logger.info(
                "詳細情報取得成功: %s quality=%s score=%s",
                property_id,
                property_data.get(
                    "detailQuality"
                ),
                property_data.get(
                    "detailQualityScore"
                )
            )

        else:

            error_message = result.get(
                "error",
                "Unknown error"
            )

            property_data[
                "detailFetchError"
            ] = str(error_message)

            error_count += 1

            logger.warning(
                "詳細情報取得失敗: %s %s",
                property_id,
                error_message
            )

        try:
            detail_adapter.wait()
        except Exception as error:
            logger.warning(
                "待機処理に失敗しました: %s",
                error
            )

    logger.info(
        "詳細取得結果: 処理=%s件 / 成功=%s件 / 失敗=%s件",
        fetched_count,
        success_count,
        error_count
    )

    return properties


# ============================================================
# 出力データ作成
# ============================================================

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

    detail_fetched_count = sum(
        1
        for item in property_list
        if item.get("detailFetched")
    )

    detail_error_count = sum(
        1
        for item in property_list
        if item.get("detailFetchError")
    )

    quality_counts = {
        "good": 0,
        "partial": 0,
        "poor": 0,
        "unknown": 0
    }

    for item in property_list:

        quality = item.get(
            "detailQuality"
        )

        if quality in quality_counts:
            quality_counts[quality] += 1
        else:
            quality_counts["unknown"] += 1

    return {
        "updatedAt": collected_at,

        "summary": {
            "discoveredCount": len(
                property_list
            ),

            "detailFetchedCount": (
                detail_fetched_count
            ),

            "detailErrorCount": (
                detail_error_count
            ),

            "detailQualityCounts": (
                quality_counts
            )
        },

        "properties": property_list
    }


# ============================================================
# メイン処理
# ============================================================

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

    collected_at = now_iso()

    current_properties = []

    # --------------------------------------------------------
    # 1. SUUMO検索
    # --------------------------------------------------------

    for adapter in create_adapters():

        try:

            results = adapter.search(
                search_config
            )

        except Exception as error:

            logger.exception(
                "検索処理に失敗しました: %s",
                error
            )

            continue

        if not isinstance(
            results,
            list
        ):

            logger.warning(
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

    # --------------------------------------------------------
    # 2. 今回の検出結果をID単位で重複排除
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 3. 既存物件を読み込み
    # --------------------------------------------------------

    existing_properties = (
        load_existing_properties()
    )

    # --------------------------------------------------------
    # 4. 既存データと今回の結果を統合
    # --------------------------------------------------------

    merged_properties = merge_properties(
        existing_properties,
        current_unique,
        collected_at
    )

    # --------------------------------------------------------
    # 5. 詳細情報取得上限
    # --------------------------------------------------------

    configured_limit = search_config.get(
        "detailFetchLimit",
        DEFAULT_DETAIL_FETCH_LIMIT
    )

    try:

        max_detail_count = int(
            configured_limit
        )

    except (
        TypeError,
        ValueError
    ):

        max_detail_count = (
            DEFAULT_DETAIL_FETCH_LIMIT
        )

    if max_detail_count < 0:
        max_detail_count = 0

    # --------------------------------------------------------
    # 6. 詳細情報取得
    # --------------------------------------------------------

    detail_adapter = create_detail_adapter()

    merged_properties = fetch_details(
        merged_properties,
        detail_adapter,
        max_detail_count
    )

    # --------------------------------------------------------
    # 7. 保存用JSON作成
    # --------------------------------------------------------

    output = build_output(
        merged_properties,
        collected_at
    )

    # --------------------------------------------------------
    # 8. JSON保存
    # --------------------------------------------------------

    save_json(
        ROOT
        / "data"
        / "discovered_listings.json",
        output
    )

    # --------------------------------------------------------
    # 9. 実行結果表示
    # --------------------------------------------------------

    logger.info(
        "今回の検出物件数: %s",
        len(current_unique)
    )

    logger.info(
        "保存済み物件総数: %s",
        len(merged_properties)
    )

    logger.info(
        "詳細取得上限: %s",
        max_detail_count
    )

    logger.info(
        "詳細取得済み累積件数: %s",
        output["summary"]["detailFetchedCount"]
    )

    logger.info(
        "詳細取得エラー件数: %s",
        output["summary"]["detailErrorCount"]
    )


if __name__ == "__main__":
    main()
