import hashlib
import re


def normalize_value(value):
    if value is None:
        return ""

    value = str(value)
    value = re.sub(r"\s+", "", value)

    return value


def make_identity_key(property_data):
    fields = [
        "address",
        "landArea",
        "buildingArea",
        "builtYear",
        "floorPlan",
        "station",
        "walkMinutes"
    ]

    values = [
        normalize_value(
            property_data.get(field)
        )
        for field in fields
    ]

    # 不足項目が多い場合は
    # 自動統合の信頼度を下げる
    filled_count = sum(
        bool(value)
        for value in values
    )

    raw = "|".join(values)

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    return {
        "identityKey": (
            f"candidate-{digest[:16]}"
        ),
        "identityCompleteness": (
            filled_count / len(fields)
        )
    }
