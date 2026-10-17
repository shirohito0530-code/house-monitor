"""Field-level detail enrichment; never invent missing values."""
from __future__ import annotations
import re
from typing import Any

FIELD_ALIASES = {
    "address": ("address",),
    "landAreaM2": ("landAreaM2", "land", "landArea"),
    "buildingAreaM2": ("buildingAreaM2", "building", "buildingArea"),
    "buildingFootprintAreaM2": ("buildingFootprintAreaM2", "buildingFootprintArea"),
    "layout": ("layout", "layoutRaw", "floorPlan", "floor_plan"),
    "constructionMonth": ("constructionMonth", "constructionYearMonth"),
    "constructionYear": ("constructionYear",),
    "targetStationWalkMinutes": ("targetStationWalkMinutes", "stationWalkMinutes", "walkMinutes"),
    "propertyType": ("propertyType",),
}
NUMERIC_FIELDS = {"landAreaM2", "buildingAreaM2", "buildingFootprintAreaM2", "constructionYear", "targetStationWalkMinutes"}

def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip() not in {"-", "－", "不明", "未定"}
    return True

def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("，", "").replace("㎡", "").replace("m²", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    try:
        return float(match.group(0)) if match else None
    except ValueError:
        return None

def _audit_entry(detail: dict[str, Any], field: str) -> dict[str, Any]:
    audit = detail.get("extractionAudit")
    if not isinstance(audit, dict):
        return {}
    aliases = {"landAreaM2": ("landAreaM2", "landArea"), "buildingAreaM2": ("buildingAreaM2", "buildingArea")}
    for key in aliases.get(field, (field,)):
        if isinstance(audit.get(key), dict):
            return audit[key]
    return {}

def enrich_detail_record(record: dict[str, Any]) -> dict[str, Any]:
    detail = record.get("detail")
    if not isinstance(detail, dict):
        detail = record.get("lastSuccessfulDetail")
    if not isinstance(detail, dict):
        return record
    field_audit = {}
    for target, aliases in FIELD_ALIASES.items():
        selected, source = None, None
        for key in aliases:
            if _nonempty(record.get(target)):
                selected, source = record[target], "record"
                break
            if _nonempty(detail.get(key)):
                selected, source = detail[key], f"detail.{key}"
                break
        if source and target in NUMERIC_FIELDS:
            selected = _number(selected)
            if selected is None:
                source = None
        status = "found" if source and selected is not None else "missing"
        if status == "found":
            record[target] = selected
            if target == "landAreaM2":
                record["land"] = selected
            if target == "buildingAreaM2":
                record["building"] = selected
        evidence = _audit_entry(detail, target)
        field_audit[target] = {
            "status": status, "source": source,
            "confidence": evidence.get("selected_confidence", evidence.get("confidence")),
            "candidateCount": evidence.get("candidate_count"),
        }
    record["fieldExtractionAudit"] = field_audit
    record["fieldExtractionUpdatedAt"] = detail.get("fetchedAt")
    return record

def summarize_field_coverage(records: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("address", "landAreaM2", "buildingAreaM2", "layout", "constructionMonth", "targetStationWalkMinutes")
    total = len(records)
    result = {"total": total, "fields": {}}
    for field in fields:
        count = sum(1 for item in records if _nonempty(item.get(field)))
        result["fields"][field] = {"count": count, "missing": total - count, "ratio": round(count / total, 4) if total else 0.0}
    return result
