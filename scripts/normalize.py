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

    area = str(area).strip()
    return AREA_ALIASES.get(area, area)


def to_number(value):
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        return value

    text = str(value)
    text = text.replace(",", "")
    text = text.replace("㎡", "")
    text = text.replace("m²", "")
    text = text.strip()

    try:
        return float(text)

    except ValueError:
        return None


def to_integer(value):
    number = to_number(value)

    if number is None:
        return None

    return int(number)


def normalize_property(data):
    collected_at = datetime.now(timezone.utc).isoformat()

    source = data.get("source", "unknown")
    source_id = data.get("sourceId") or data.get("id")

    if not source_id:
        raise ValueError("sourceIdまたはidがありません")

    return {
        "source": source,
        "sourceId": str(source_id),
        "sourceUrl": data.get("sourceUrl") or data.get("url") or "",

        "area": normalize_area(data.get("area")),
        "name": data.get("name") or "",

        "propertyType": data.get("propertyType") or "",

        "price": to_number(data.get("price")),
        "land": to_number(data.get("land")),
        "building": to_number(data.get("building")),
        "walk": to_number(data.get("walk")),
        "year": to_integer(data.get("year")),
        "layout": data.get("layout") or "",

        "school": data.get("school") or "",
        "flat": data.get("flat"),
        "retainingWall": data.get("retainingWall"),

        "collectedAt": collected_at
    }
