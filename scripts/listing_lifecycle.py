"""Conservative listing lifecycle helpers."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

VALID_STATUSES = {"active", "possibly_ended", "observed_ended", "confirmed_ended"}
STATUS_LABELS = {
    "active": "掲載中",
    "possibly_ended": "終了の可能性あり",
    "observed_ended": "掲載終了（検索未検出）",
    "confirmed_ended": "掲載終了（明示確認）",
}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize_status(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "").strip()
    if status in VALID_STATUSES:
        if status == "confirmed_ended" and record.get("isListingEnded") is not True:
            return "observed_ended"
        return status
    if record.get("isListingEnded") is True and record.get("listingEndEvidence"):
        return "confirmed_ended"
    if record.get("endedObservedAt") or record.get("endedAt"):
        return "observed_ended"
    return "active"

def transition_status(
    record: dict[str, Any], *, observed_now: str | None = None,
    found_in_healthy_search: bool = False, healthy_search_completed: bool = False,
    explicit_end_evidence: str | None = None, healthy_misses_to_end: int = 2,
) -> dict[str, Any]:
    """Search failures and empty/incomplete results never count as evidence."""
    stamp = observed_now or now_iso()
    old_status = normalize_status(record)
    if found_in_healthy_search:
        was_ended = old_status in {"possibly_ended", "observed_ended", "confirmed_ended"}
        record.update(status="active", missedSearchRuns=0, isListingEnded=False,
                      listingEndReason=None, listingEndEvidence=None, lastSeenAt=stamp,
                      listingStatusUpdatedAt=stamp)
        if was_ended:
            record["relistedAt"] = stamp
        return record
    if explicit_end_evidence:
        record.update(status="confirmed_ended", isListingEnded=True,
                      listingEndReason="explicit_detail_page_evidence",
                      listingEndEvidence=str(explicit_end_evidence)[:500],
                      endedAt=record.get("endedAt") or stamp, listingStatusUpdatedAt=stamp)
        return record
    if not healthy_search_completed:
        record["status"] = old_status
        return record
    misses = int(record.get("missedSearchRuns") or 0) + 1
    record["missedSearchRuns"] = misses
    if misses >= max(1, int(healthy_misses_to_end)):
        if old_status not in {"observed_ended", "confirmed_ended"}:
            record["endedObservedAt"] = stamp
            record["listingStatusUpdatedAt"] = stamp
        record["status"] = "observed_ended"
        record["listingEndReason"] = record.get("listingEndReason") or "missing_from_healthy_searches"
    else:
        record["status"] = "possibly_ended"
    return record

def status_label(record: dict[str, Any]) -> str:
    status = normalize_status(record)
    if status == "active" and record.get("relistedAt"):
        return "再掲載"
    return STATUS_LABELS.get(status, "状態不明")
