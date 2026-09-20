import json
from datetime import datetime, timezone
from pathlib import Path


# ==========================================
# ファイル設定
# ==========================================

DATA_FILE = Path("data/houses.json")
CONFIG_FILE = Path("config/search.json")


# ==========================================
# エリア名の正規化
# ==========================================

AREA_ALIASES = {
    "柏の葉": "柏の葉キャンパス",
    "柏の葉キャンパス": "柏の葉キャンパス",
    "おおたかの森": "流山おおたかの森",
    "流山おおたかの森": "流山おおたかの森",
}


# ==========================================
# 現在日付
# ==========================================

def now_date():
    return datetime.now(timezone.utc).date().isoformat()


# ==========================================
# 設定読み込み
# ==========================================

def load_config():

    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            "config/search.json が見つかりません"
        )

    with open(
        CONFIG_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


# ==========================================
# 物件データ読み込み
# ==========================================

def load_data():

    if not DATA_FILE.exists():

        return {
            "updatedAt": None,
            "properties": []
        }

    with open(
        DATA_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


# ==========================================
# 物件データ保存
# ==========================================

def save_data(data):

    DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        DATA_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

        f.write("\n")


# ==========================================
# エリア名を正規化
# ==========================================

def normalize_area(area):

    if not area:
        return None

    return AREA_ALIASES.get(
        area,
        area
    )


# ==========================================
# 物件追加・更新
# ==========================================

def add_or_update_property(
    data,
    new_property
):

    properties = data.setdefault(
        "properties",
        []
    )

    property_id = new_property["id"]

    existing = next(
        (
            p
            for p in properties
            if p.get("id") == property_id
        ),
        None
    )

    today = now_date()

    # --------------------------------------
    # エリア名を正規化
    # --------------------------------------

    original_area = new_property.get(
        "area"
    )

    new_property["normalizedArea"] = (
        normalize_area(original_area)
    )

    # ======================================
    # 新規物件
    # ======================================

    if existing is None:

        new_property["firstSeen"] = today
        new_property["lastSeen"] = today
        new_property["status"] = "active"

        new_property["history"] = [
            {
                "date": today,
                "price": new_property["price"]
            }
        ]

        new_property["priceChange"] = 0
        new_property["priceChangePercent"] = 0
        new_property["lastPriceChange"] = 0

        properties.append(
            new_property
        )

        print(
            f"NEW PROPERTY: "
            f"{new_property['name']}"
        )

        return

    # ======================================
    # 既存物件
    # ======================================

    old_price = existing.get(
        "price"
    )

    new_price = new_property.get(
        "price"
    )

    # 直前価格との差額
    if (
        old_price is not None
        and new_price is not None
    ):

        last_price_change = (
            new_price - old_price
        )

    else:

        last_price_change = 0

    # 既存データを更新
    existing.update(
        new_property
    )

    existing["normalizedArea"] = (
        normalize_area(
            existing.get("area")
        )
    )

    existing["lastSeen"] = today
    existing["status"] = "active"

    existing["lastPriceChange"] = (
        last_price_change
    )

    # ======================================
    # 価格変更
    # ======================================

    if (
        old_price is not None
        and new_price is not None
        and old_price != new_price
    ):

        history = existing.setdefault(
            "history",
            []
        )

        history.append(
            {
                "date": today,
                "price": new_price
            }
        )

        print(
            f"PRICE CHANGE: "
            f"{existing['name']} "
            f"{old_price} -> {new_price} "
            f"({last_price_change:+}万円)"
        )

    else:

        print(
            f"NO PRICE CHANGE: "
            f"{existing['name']}"
        )


# ==========================================
# 指標計算
# ==========================================

def calculate_metrics(
    property_data,
    config
):

    history = property_data.get(
        "history",
        []
    )

    # ======================================
    # エリア正規化
    # ======================================

    property_data["normalizedArea"] = (
        normalize_area(
            property_data.get("area")
        )
    )

    # ======================================
    # 価格履歴
    # ======================================

    if history:

        first_price = history[0].get(
            "price"
        )

        current_price = property_data.get(
            "price"
        )

        if (
            first_price is not None
            and current_price is not None
        ):

            # 初回掲載価格からの変化
            property_data["priceChange"] = (
                current_price - first_price
            )

            # 初回掲載価格からの変化率
            if first_price != 0:

                property_data[
                    "priceChangePercent"
                ] = round(
                    (
                        current_price
                        / first_price
                        - 1
                    ) * 100,
                    2
                )

    # ======================================
    # 掲載日数
    # ======================================

    if property_data.get(
        "firstSeen"
    ):

        first = datetime.fromisoformat(
            property_data["firstSeen"]
        ).date()

        today = datetime.now(
            timezone.utc
        ).date()

        property_data["daysListed"] = (
            today - first
        ).days

    # ======================================
    # 土地㎡単価
    # ======================================

    land = property_data.get(
        "land"
    )

    price = property_data.get(
        "price"
    )

    if land and price:

        property_data["pricePerLand"] = round(
            price * 10000 / land
        )

    # ======================================
    # 基本条件判定
    # ======================================

    conditions = {
        "area": False,
        "propertyType": False,
        "price": False,
        "walk": False,
        "land": False,
        "building": False
    }

    # --------------------------------------
    # エリア
    # --------------------------------------

    target_areas = [
        area["name"]
        for area in config.get(
            "areas",
            []
        )
    ]

    normalized_area = property_data.get(
        "normalizedArea"
    )

    if normalized_area in target_areas:

        conditions["area"] = True

    # --------------------------------------
    # 物件種別
    # --------------------------------------

    if (
        property_data.get(
            "propertyType"
        )
        in config.get(
            "propertyTypes",
            []
        )
    ):

        conditions["propertyType"] = True

    # --------------------------------------
    # 価格
    # --------------------------------------

    if property_data.get(
        "price"
    ) is not None:

        if (
            property_data["price"]
            <= config["maxPrice"]
        ):

            conditions["price"] = True

    # --------------------------------------
    # 駅徒歩
    # --------------------------------------

    if property_data.get(
        "walk"
    ) is not None:

        if (
            property_data["walk"]
            <= config["maxWalkMinutes"]
        ):

            conditions["walk"] = True

    # --------------------------------------
    # 土地面積
    # --------------------------------------

    if property_data.get(
        "land"
    ) is not None:

        if (
            property_data["land"]
            >= config["minLandArea"]
        ):

            conditions["land"] = True

    # --------------------------------------
    # 建物面積
    # --------------------------------------

    if property_data.get(
        "building"
    ) is not None:

        if (
            property_data["building"]
            >= config["minBuildingArea"]
        ):

            conditions["building"] = True

    # ======================================
    # 条件保存
    # ======================================

    property_data["conditions"] = conditions

    property_data["isWithinCriteria"] = all(
        conditions.values()
    )

    # ======================================
    # 自動フラグ
    # ======================================

    flags = []

    # --------------------------------------
    # 新着
    # --------------------------------------

    if property_data.get(
        "daysListed",
        9999
    ) <= 7:

        flags.append(
            "新着"
        )

    # --------------------------------------
    # 初回価格から値下げ
    # --------------------------------------

    if property_data.get(
        "priceChange",
        0
    ) < 0:

        flags.append(
            "値下げ"
        )

    # --------------------------------------
    # 500万円以上値下げ
    # --------------------------------------

    if property_data.get(
        "priceChange",
        0
    ) <= -500:

        flags.append(
            "大幅値下げ"
        )

    # --------------------------------------
    # 長期掲載
    # --------------------------------------

    if property_data.get(
        "daysListed",
        0
    ) >= 90:

        flags.append(
            "長期掲載"
        )

    # --------------------------------------
    # 土地150㎡以上
    # --------------------------------------

    if property_data.get(
        "land",
        0
    ) >= 150:

        flags.append(
            "土地150㎡以上"
        )

    # --------------------------------------
    # 駅徒歩10分以内
    # --------------------------------------

    if property_data.get(
        "walk",
        999
    ) <= 10:

        flags.append(
            "駅徒歩10分以内"
        )

    # --------------------------------------
    # 柏の葉小学校区
    # --------------------------------------

    if (
        "柏の葉小学校"
        in str(
            property_data.get(
                "school",
                ""
            )
        )
    ):

        flags.append(
            "柏の葉小学校区"
        )

    # --------------------------------------
    # 平坦地
    # --------------------------------------

    if property_data.get(
        "flat"
    ) is True:

        flags.append(
            "平坦地"
        )

    # --------------------------------------
    # 擁壁なし
    # --------------------------------------

    if property_data.get(
        "retainingWall"
    ) is False:

        flags.append(
            "擁壁なし"
        )

    property_data["flags"] = flags


# ==========================================
# メイン処理
# ==========================================

def main():

    data = load_data()
    config = load_config()

    print(
        "================================"
    )

    print(
        "HOUSE SEARCH CONFIG"
    )

    print(
        "================================"
    )

    print(
        "Areas:",
        [
            area["name"]
            for area in config["areas"]
        ]
    )

    print(
        "Property types:",
        config["propertyTypes"]
    )

    print(
        "Max price:",
        config["maxPrice"],
        "万円"
    )

    print(
        "Max walk:",
        config["maxWalkMinutes"],
        "minutes"
    )

    print(
        "Min land:",
        config["minLandArea"],
        "㎡"
    )

    print(
        "Min building:",
        config["minBuildingArea"],
        "㎡"
    )

    print(
        "================================"
    )

    # ==========================================
    # 現在はテストデータ
    # ==========================================

    properties = [

        {
            "id": "demo-kashiwa-001",
            "area": "柏の葉",
            "name": "柏の葉テスト物件",
            "propertyType": "中古戸建",
            "price": 8290,
            "land": 150,
            "building": 105,
            "walk": 10,
            "year": 2023,
            "layout": "4LDK",
            "url": "",
            "school": "柏の葉小学校区",
            "flat": True,
            "retainingWall": False
        },

        {
            "id": "demo-otaka-001",
            "area": "おおたかの森",
            "name": "おおたかの森テスト物件",
            "propertyType": "中古戸建",
            "price": 7980,
            "land": 135,
            "building": 101,
            "walk": 12,
            "year": 2022,
            "layout": "4LDK",
            "url": "",
            "school": "おおたかの森",
            "flat": True,
            "retainingWall": False
        },

        {
            "id": "demo-kashiwa-002",
            "area": "柏の葉",
            "name": "柏の葉テスト物件2",
            "propertyType": "中古戸建",
            "price": 8980,
            "land": 165,
            "building": 110,
            "walk": 8,
            "year": 2024,
            "layout": "4LDK",
            "url": "",
            "school": "柏の葉小学校区",
            "flat": True,
            "retainingWall": False
        }

    ]

    # ==========================================
    # 物件追加・更新
    # ==========================================

    for property_data in properties:

        add_or_update_property(
            data,
            property_data
        )

    # ==========================================
    # 指標計算
    # ==========================================

    for property_data in data[
        "properties"
    ]:

        calculate_metrics(
            property_data,
            config
        )

    # ==========================================
    # 更新日時
    # ==========================================

    data["updatedAt"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    # ==========================================
    # 保存
    # ==========================================

    save_data(data)

    # ==========================================
    # ログ
    # ==========================================

    print(
        "--------------------------------"
    )

    print(
        f"Total properties: "
        f"{len(data['properties'])}"
    )

    print(
        f"Updated at: "
        f"{data['updatedAt']}"
    )

    print(
        "================================"
    )

    # 条件適合物件数
    within_criteria = sum(
        1
        for p in data["properties"]
        if p.get("isWithinCriteria")
    )

    print(
        f"Within criteria: "
        f"{within_criteria}"
    )


# ==========================================
# 実行
# ==========================================

if __name__ == "__main__":
    main()
