import json
from datetime import datetime, timezone
from pathlib import Path


DATA_FILE = Path("data/houses.json")


def now_date():
    return datetime.now(timezone.utc).date().isoformat()


def load_data():
    if not DATA_FILE.exists():
        return {
            "updatedAt": None,
            "properties": []
        }

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )
        f.write("\n")


def add_or_update_property(data, new_property):

    properties = data.setdefault("properties", [])

    property_id = new_property["id"]

    existing = next(
        (
            p for p in properties
            if p.get("id") == property_id
        ),
        None
    )

    today = now_date()

    # -------------------------
    # 新規物件
    # -------------------------

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

        properties.append(new_property)

        print(
            f"NEW PROPERTY: "
            f"{new_property['name']}"
        )

        return

    # -------------------------
    # 既存物件
    # -------------------------

    old_price = existing.get("price")
    new_price = new_property.get("price")

    existing.update(new_property)

    existing["lastSeen"] = today
    existing["status"] = "active"

    # -------------------------
    # 価格変更
    # -------------------------

    if old_price != new_price:

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

        difference = new_price - old_price

        print(
            f"PRICE CHANGE: "
            f"{existing['name']} "
            f"{old_price} -> {new_price} "
            f"({difference:+}万円)"
        )

    else:

        print(
            f"NO PRICE CHANGE: "
            f"{existing['name']}"
        )


def calculate_metrics(property_data):

    history = property_data.get(
        "history",
        []
    )

    if not history:
        return

    first_price = history[0]["price"]
    current_price = property_data["price"]

    property_data["priceChange"] = (
        current_price - first_price
    )

    property_data["priceChangePercent"] = round(
        (
            current_price
            / first_price
            - 1
        ) * 100,
        2
    )

    if property_data.get("firstSeen"):

        first = datetime.fromisoformat(
            property_data["firstSeen"]
        ).date()

        today = datetime.now(
            timezone.utc
        ).date()

        property_data["daysListed"] = (
            today - first
        ).days


def main():

    data = load_data()

    # ==========================================
    # 現在はテストデータ
    # ==========================================

    properties = [

        {
            "id": "demo-kashiwa-001",
            "area": "柏の葉",
            "name": "柏の葉テスト物件",
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
    # 保存
    # ==========================================

    for property_data in properties:

        add_or_update_property(
            data,
            property_data
        )

    # ==========================================
    # 指標計算
    # ==========================================

    for property_data in data["properties"]:

        calculate_metrics(
            property_data
        )

    data["updatedAt"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_data(data)

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


if __name__ == "__main__":
    main()
