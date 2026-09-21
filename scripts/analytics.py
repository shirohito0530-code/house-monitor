from datetime import date, datetime


def today_string():
    return date.today().isoformat()


def calculate_days_listed(first_seen):
    if not first_seen:
        return 0

    try:
        first_date = datetime.strptime(
            first_seen,
            "%Y-%m-%d"
        ).date()

        return (date.today() - first_date).days

    except ValueError:
        return 0


def create_property_key(property_data):
    source = property_data.get("source", "unknown")
    source_id = property_data.get("sourceId")

    return f"{source}:{source_id}"


def matches_criteria(property_data, search_config):
    areas = search_config.get("areas", [])
    property_types = search_config.get("propertyTypes", [])

    if areas and property_data.get("area") not in areas:
        return False

    if (
        property_types
        and property_data.get("propertyType") not in property_types
    ):
        return False

    price = property_data.get("price")
    if price is not None:
        if price > search_config.get("maxPrice", float("inf")):
            return False

    walk = property_data.get("walk")
    if walk is not None:
        if walk > search_config.get("maxWalk", float("inf")):
            return False

    land = property_data.get("land")
    if land is not None:
        if land < search_config.get("minLand", 0):
            return False

    building = property_data.get("building")
    if building is not None:
        if building < search_config.get("minBuilding", 0):
            return False

    year = property_data.get("year")
    if year is not None:
        if year < search_config.get("minYear", 0):
            return False

    if search_config.get("onlyFlatLand"):
        if property_data.get("flat") is not True:
            return False

    if search_config.get("excludeRetainingWall"):
        if property_data.get("retainingWall") is not False:
            return False

    return True


def calculate_flags(property_data):
    flags = []

    if property_data.get("priceChange", 0) < 0:
        flags.append("値下げ")

    if property_data.get("priceChange", 0) <= -500:
        flags.append("大幅値下げ")

    if property_data.get("daysListed", 0) >= 90:
        flags.append("長期掲載")

    if property_data.get("land") is not None:
        if property_data["land"] >= 150:
            flags.append("土地150㎡以上")

    if property_data.get("walk") is not None:
        if property_data["walk"] <= 10:
            flags.append("駅徒歩10分以内")

    if property_data.get("school") == "柏の葉小学校区":
        flags.append("柏の葉小学校区")

    if property_data.get("flat") is True:
        flags.append("平坦地")

    if property_data.get("retainingWall") is False:
        flags.append("擁壁なし")

    return flags


def enrich_property(
    property_data,
    previous_property,
    search_config
):
    today = today_string()
    current_price = property_data.get("price")

    if previous_property:
        first_seen = previous_property.get("firstSeen", today)
        old_price = previous_property.get("price")

        if old_price is not None and current_price is not None:
            price_change = current_price - old_price
        else:
            price_change = 0

        history = previous_property.get("history", [])

        if current_price != old_price:
            history.append({
                "date": today,
                "price": current_price
            })

    else:
        first_seen = today
        old_price = None
        price_change = 0

        history = []

        if current_price is not None:
            history.append({
                "date": today,
                "price": current_price
            })

    property_data["firstSeen"] = first_seen
    property_data["lastSeen"] = today
    property_data["status"] = "active"

    property_data["priceChange"] = price_change

    if old_price and old_price != 0:
        property_data["priceChangePercent"] = (
            price_change / old_price * 100
        )
    else:
        property_data["priceChangePercent"] = 0

    property_data["history"] = history

    property_data["daysListed"] = calculate_days_listed(first_seen)

    land = property_data.get("land")
    price = property_data.get("price")

    if land and land > 0 and price is not None:
        property_data["pricePerLand"] = round(
            price * 10000 / land
        )
    else:
        property_data["pricePerLand"] = None

    property_data["isWithinCriteria"] = matches_criteria(
        property_data,
        search_config
    )

    property_data["flags"] = calculate_flags(property_data)

    if not previous_property:
        property_data["flags"].insert(0, "新着")

    return property_data


def merge_properties(
    existing_data,
    collected_properties,
    search_config
):
    existing_properties = existing_data.get("properties", [])

    existing_map = {
        create_property_key(item): item
        for item in existing_properties
    }

    updated_properties = []
    collected_keys = set()

    for property_data in collected_properties:
        key = create_property_key(property_data)
        collected_keys.add(key)

        previous = existing_map.get(key)

        enriched = enrich_property(
            property_data,
            previous,
            search_config
        )

        updated_properties.append(enriched)

    for key, previous in existing_map.items():
        if key not in collected_keys:
            previous["status"] = "missing"
            updated_properties.append(previous)

    return {
        "updatedAt": datetime.now().isoformat(),
        "properties": updated_properties
    }
