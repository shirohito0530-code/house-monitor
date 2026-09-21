from datetime import datetime, timezone


AREA_ALIASES = {
    "柏の葉": "柏の葉キャンパス",
    "柏の葉キャンパス": "柏の葉キャンパス",
    "おおたかの森": "流山おおたかの森",
    "流山おおたかの森": "流山おおたかの森"
}


def normalize_area(area):
    if not area:
        return None

    return AREA_ALIASES.get(area, area)


def to_number(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return value

    value = str(value)
    value = value.replace(",", "")
    value = value.replace("㎡", "")
    value = value.strip()

    try:
        return float(value)
    except ValueError:
        return None


def normalize_property(data):
    now = datetime.now(timezone.utc).isoformat()

    return {
        "source": data.get("source"),
        "sourceId": data.get("sourceId"),
        "sourceUrl": data.get("sourceUrl"),

        "area": normalize_area(data.get("area")),
        "name": data.get("name"),
        "propertyType": data.get("propertyType"),

        "price": to_number(data.get("price")),
        "land": to_number(data.get("land")),
        "building": to_number(data.get("building")),
        "walk": to_number(data.get("walk")),
        "year": data.get("year"),
        "layout": data.get("layout"),

        "school": data.get("school"),
        "flat": data.get("flat"),
        "retainingWall": data.get("retainingWall"),

        "collectedAt": now
    }
