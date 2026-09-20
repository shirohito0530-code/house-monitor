import json
from datetime import datetime, timezone
from pathlib import Path


DATA_FILE = Path("data/houses.json")


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
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def add_or_update_property(data, property_data):
    properties = data.setdefault("properties", [])

    property_id = property_data["id"]

    existing = next(
        (p for p in properties if p.get("id") == property_id),
        None
    )

    today = datetime.now(timezone.utc).date().isoformat()

    if existing is None:
        property_data["history"] = [
            {
                "date": today,
                "price": property_data["price"]
            }
        ]

        property_data["firstSeen"] = today
        property_data["lastSeen"] = today

        properties.append(property_data)

        print(f"NEW: {property_data['name']}")

    else:
        old_price = existing.get("price")

        existing.update(property_data)
        existing["lastSeen"] = today

        if old_price != property_data["price"]:
            history = existing.setdefault("history", [])

            history.append({
                "date": today,
                "price": property_data["price"]
            })

            print(
                f"PRICE CHANGE: "
                f"{existing['name']} "
                f"{old_price} -> {property_data['price']}"
            )


def main():

    data = load_data()

    # ---------------------------------------
    # 現在はテストデータ
    # 後で正式な取得元に置き換える
    # ---------------------------------------

    sample_properties = [

        {
            "id": "demo-kashiwa-001",
            "area": "柏の葉",
            "name": "自動取得テスト物件",
            "price": 8490,
            "land": 150,
            "building": 105,
            "walk": 10,
            "year": 2023,
            "layout": "4LDK",
            "url": "",
            "school": "柏の葉小学校区",
            "flat": True,
            "retainingWall": False
        }

    ]

    for property_data in sample_properties:
        add_or_update_property(data, property_data)

    data["updatedAt"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_data(data)

    print(
        f"Updated {len(data['properties'])} properties."
    )


if __name__ == "__main__":
    main()
