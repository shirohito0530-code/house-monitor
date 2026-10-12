import json
from pathlib import Path


def load_json(path, default=None):
    """
    JSONファイルを読み込む。

    ファイルが存在しない場合や
    JSONが不正な場合はdefaultを返す。
    """

    path = Path(path)

    if not path.exists():
        return default

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

    except (
        json.JSONDecodeError,
        OSError
    ) as error:

        print(
            f"JSON読み込みエラー: "
            f"{path}: {error}"
        )

        return default


def save_json(path, data):
    """
    JSONを一時ファイルに保存し、
    保存成功後に本ファイルへ置き換える。
    """

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary_path = path.with_suffix(
        ".tmp"
    )

    try:

        with open(
            temporary_path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

            file.write("\n")

        temporary_path.replace(
            path
        )

        print(
            f"JSON保存完了: {path}"
        )

    except OSError as error:

        print(
            f"JSON保存エラー: "
            f"{path}: {error}"
        )

        if temporary_path.exists():
            temporary_path.unlink()

        raise

def load_observations(
    path,
):
    data = load_json(
        path,
        default=[],
    )

    if isinstance(
        data,
        dict,
    ):
        data = data.get(
            "observations",
            [],
        )

    if not isinstance(
        data,
        list,
    ):
        return []

    return data


def save_observations(
    path,
    observations,
):
    save_json(
        path,
        {
            "updatedAt": (
                __import__(
                    "datetime"
                ).datetime.now(
                    __import__(
                        "datetime"
                    ).timezone.utc
                ).isoformat()
            ),
            "observations": observations,
        },
    )

def merge_records_by_property_id(records):
    """propertyId単位で重複レコードを統合する。非空値を保持し、後続値で可変項目を更新。"""
    result = {}
    latest_fields = {
        "status", "lastSeenAt", "priceYen", "priceMan", "currentPrice",
        "currentPriceMan", "searchPriceYen", "searchPriceMan", "sourceUrl",
        "missedSearchRuns", "endedObservedAt", "relistedAt",
        "listingStatusUpdatedAt",
    }
    for record in records or []:
        if not isinstance(record, dict):
            continue
        property_id = record.get("propertyId") or record.get("id")
        if not property_id:
            continue
        property_id = str(property_id)
        if property_id not in result:
            result[property_id] = dict(record)
            continue
        existing = result[property_id]
        for key, value in record.items():
            if value is None or value == "":
                continue
            if key in latest_fields:
                old_seen = str(existing.get("lastSeenAt") or "")
                new_seen = str(record.get("lastSeenAt") or "")
                if new_seen >= old_seen:
                    existing[key] = value
            elif existing.get(key) in (None, "", [], {}):
                existing[key] = value
    return list(result.values())


def append_changed_observations(existing_observations, current_observations, compare_fields=None):
    """同一propertyIdで比較対象フィールドが変化した場合だけ履歴を追記する。"""
    compare_fields = compare_fields or (
        "status", "currentPrice", "currentPriceMan",
        "priceConfidence", "priceStatus", "priceSource",
    )
    history = [
        item for item in (existing_observations or [])
        if isinstance(item, dict)
    ]
    latest = {}
    for item in history:
        if item.get("propertyId"):
            latest[str(item["propertyId"])] = item
    appended = 0
    for item in current_observations or []:
        if not isinstance(item, dict) or not item.get("propertyId"):
            continue
        key = str(item["propertyId"])
        previous = latest.get(key)
        if previous is None or any(previous.get(field) != item.get(field) for field in compare_fields):
            history.append(dict(item))
            latest[key] = item
            appended += 1
    return history, appended
