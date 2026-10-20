
"""
Fetch MLIT XIT001 transaction data for Kashiwa and Nagareyama.

Policies:
- Preserve existing transaction records on every retrieval failure.
- Treat HTTP 404 as "no data for this period" and try older quarters.
- Fetch each city independently.
- Keep the existing data/transactions.json schema compatible with
  scripts/market_pipeline.py.
- Do not print the API key or request headers.
- Record retrieval outcomes without making an API failure stop the
  downstream market-analysis pipeline.
"""

import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


API_URL = (
    "https://www.reinfolib.mlit.go.jp/"
    "ex-api/external/XIT001"
)
API_KEY = os.environ.get("REINFOLIB_API_KEY", "").strip()
OUTPUT_FILE = Path("data/transactions.json")

PRICE_CLASSIFICATION = "02"
LANGUAGE = "ja"

# How many recent quarters to search, newest first.
MAX_QUARTERS_TO_SEARCH = 8

# Retry transient errors, but do not retry 404s.
MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 60
RETRY_WAIT_SECONDS = 2

CITIES = {
    "kashiwa": {
        "name": "柏市",
        "code": "12217",
        "area": "柏の葉キャンパス",
    },
    "nagareyama": {
        "name": "流山市",
        "code": "12220",
        "area": "流山おおたかの森",
    },
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def quarter_key(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


def get_recent_quarters(
    count: int = MAX_QUARTERS_TO_SEARCH,
) -> list[tuple[int, int]]:
    """
    Start from the most recently completed calendar quarter.
    For example, an October run starts at Q3 of the same year.
    """
    now = datetime.now(timezone.utc)
    current_quarter = ((now.month - 1) // 3) + 1

    year = now.year
    quarter = current_quarter - 1

    if quarter == 0:
        year -= 1
        quarter = 4

    result: list[tuple[int, int]] = []

    for _ in range(count):
        result.append((year, quarter))
        quarter -= 1

        if quarter == 0:
            year -= 1
            quarter = 4

    return result


def load_existing() -> dict[str, Any]:
    """
    Load the existing database without replacing it with an empty
    object if the file is malformed.
    """
    if not OUTPUT_FILE.exists():
        return {
            "schemaVersion": "1.0",
            "updatedAt": None,
            "periods": [],
        }

    try:
        with OUTPUT_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"既存の {OUTPUT_FILE} を読み込めません。"
            "データ保護のため上書きを中止します。"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            f"{OUTPUT_FILE} のトップレベルはJSON objectである必要があります。"
        )

    if not isinstance(data.get("periods", []), list):
        raise RuntimeError(
            f"{OUTPUT_FILE}: periods が配列ではありません。"
            "データ保護のため上書きを中止します。"
        )

    data.setdefault("periods", [])
    return data


def find_period(
    database: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    for period in database.get("periods", []):
        if isinstance(period, dict) and period.get("period") == key:
            return period
    return None


def city_has_data(
    database: dict[str, Any],
    city_key: str,
    year: int,
    quarter: int,
) -> bool:
    """
    A city is considered already covered if a valid city object exists
    for that period. An empty data list is valid and is not repeatedly
    fetched every day.
    """
    period = find_period(database, quarter_key(year, quarter))
    if not period:
        return False

    cities = period.get("cities", {})
    if not isinstance(cities, dict):
        return False

    city_data = cities.get(city_key)
    if not isinstance(city_data, dict):
        return False

    return isinstance(city_data.get("data"), list)


def get_or_create_period(
    database: dict[str, Any],
    year: int,
    quarter: int,
) -> dict[str, Any]:
    key = quarter_key(year, quarter)
    period = find_period(database, key)

    if period is None:
        period = {
            "period": key,
            "year": year,
            "quarter": quarter,
            "cities": {},
        }
        database.setdefault("periods", []).append(period)

    period.setdefault("cities", {})
    return period


def request_json(
    city_code: str,
    year: int,
    quarter: int,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """
    Returns:
      ("success", payload, None)
      ("no_data", None, message)  -- HTTP 404
      ("error", None, message)    -- other errors or invalid payload
    """
    if not API_KEY:
        return (
            "error",
            None,
            "REINFOLIB_API_KEY is not configured",
        )

    headers = {
        "Ocp-Apim-Subscription-Key": API_KEY,
        "Accept": "application/json",
    }
    params = {
        "year": year,
        "quarter": quarter,
        "city": city_code,
        "priceClassification": PRICE_CLASSIFICATION,
        "language": LANGUAGE,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                API_URL,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            message = f"Network error: {type(exc).__name__}"

            if attempt < MAX_RETRIES:
                print(
                    f"  Temporary request failure "
                    f"({attempt}/{MAX_RETRIES}); retrying."
                )
                time.sleep(RETRY_WAIT_SECONDS * attempt)
                continue

            return "error", None, message

        if response.status_code == 404:
            return (
                "no_data",
                None,
                "HTTP 404: no data returned for this city/period",
            )

        if response.status_code in (401, 403):
            return (
                "error",
                None,
                f"HTTP {response.status_code}: authentication/permission error",
            )

        if response.status_code == 429 or response.status_code >= 500:
            message = f"HTTP {response.status_code}"

            if attempt < MAX_RETRIES:
                print(
                    f"  Temporary API response {message} "
                    f"({attempt}/{MAX_RETRIES}); retrying."
                )
                time.sleep(RETRY_WAIT_SECONDS * attempt)
                continue

            return "error", None, message

        if not response.ok:
            return (
                "error",
                None,
                f"HTTP {response.status_code}: unexpected API response",
            )

        try:
            try:
                payload = response.json()
            except ValueError:
                # Some responses may be gzip-compressed JSON.
                payload = json.loads(gzip.decompress(response.content))

        except (ValueError, OSError, EOFError):
            return (
                "error",
                None,
                "Response was not valid JSON or gzip-compressed JSON",
            )

        if not isinstance(payload, dict):
            return (
                "error",
                None,
                "API response root was not a JSON object",
            )

        # A missing or malformed data field is not treated as zero records.
        if "data" not in payload or not isinstance(payload["data"], list):
            return (
                "error",
                None,
                "API response did not contain a valid data array",
            )

        if not all(isinstance(row, dict) for row in payload["data"]):
            return (
                "error",
                None,
                "API data array contained a non-object record",
            )

        return "success", payload, None

    return "error", None, "Request failed after retries"


def filter_detached_houses(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Retain land-and-building transactions as detached-house candidates.
    XIT001 transaction records may include properties other than detached
    houses, so this filter is intentionally conservative.
    """
    result = []

    for record in payload["data"]:
        trade_type = str(record.get("Type", "")).strip()

        if trade_type == "宅地(土地と建物)":
            result.append(record)

    return result


def save_data(database: dict[str, Any]) -> None:
    """
    Write atomically so an interrupted write does not truncate the
    previous transactions.json.
    """
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = OUTPUT_FILE.with_suffix(".json.tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(
            database,
            f,
            ensure_ascii=False,
            indent=2,
        )
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    temp_file.replace(OUTPUT_FILE)


def main() -> int:
    print("=" * 60)
    print("REAL ESTATE TRANSACTION DATA")
    print("=" * 60)

    # If the secret is missing, preserve the current file and return
    # success so the downstream market pipeline can still run.
    if not API_KEY:
        print(
            "::warning::REINFOLIB_API_KEY is not configured. "
            "Existing transaction data will be preserved."
        )
        return 0

    try:
        database = load_existing()
    except RuntimeError as exc:
        print(f"::error::{exc}")
        # Never overwrite a malformed or unreadable existing database.
        # Return non-zero here because continuing would conceal a data
        # integrity problem. The workflow should make this step
        # non-blocking only for API retrieval errors, not file corruption.
        return 1

    quarters = get_recent_quarters()
    retrieval_status: dict[str, Any] = {
        "checkedAt": utc_now_iso(),
        "source": "国土交通省 不動産情報ライブラリ XIT001",
        "priceClassification": PRICE_CLASSIFICATION,
        "maxQuartersSearched": MAX_QUARTERS_TO_SEARCH,
        "cities": {},
    }

    print(
        "Search periods: "
        + ", ".join(quarter_key(y, q) for y, q in quarters)
    )

    for city_key, city in CITIES.items():
        city_status: dict[str, Any] = {
            "name": city["name"],
            "cityCode": city["code"],
            "status": "not_attempted",
            "selectedPeriod": None,
            "recordsSaved": 0,
            "periodsChecked": [],
            "errors": [],
        }

        print(f"\nFetching: {city['name']} ({city['code']})")

        saved_existing = False

        for year, quarter in quarters:
            key = quarter_key(year, quarter)

            if city_has_data(database, city_key, year, quarter):
                print(f"  {key}: already stored; skipping")
                city_status["periodsChecked"].append({
                    "period": key,
                    "status": "already_stored",
                })
                saved_existing = True
                break

            print(f"  Requesting {key}...")

            status, payload, error = request_json(
                city["code"],
                year,
                quarter,
            )

            if status == "no_data":
                print(f"  {key}: no data (404); trying an older quarter")
                city_status["periodsChecked"].append({
                    "period": key,
                    "status": "no_data",
                    "message": error,
                })
                continue

            if status == "error":
                print(f"  {key}: retrieval error: {error}")
                city_status["periodsChecked"].append({
                    "period": key,
                    "status": "error",
                    "message": error,
                })
                city_status["errors"].append({
                    "period": key,
                    "message": error,
                })

                # Authentication, rate-limit, network, server, or response
                # errors are not evidence that older data is unavailable.
                # Stop this city's search but continue with the other city.
                city_status["status"] = "error"
                break

            assert payload is not None
            houses = filter_detached_houses(payload)

            period = get_or_create_period(database, year, quarter)
            period["cities"][city_key] = {
                "name": city["name"],
                "code": city["code"],
                "area": city["area"],
                "count": len(houses),
                "data": houses,
                "retrievedAt": utc_now_iso(),
                "source": "XIT001",
                "priceClassification": PRICE_CLASSIFICATION,
            }

            city_status["selectedPeriod"] = key
            city_status["recordsSaved"] = len(houses)
            city_status["status"] = (
                "success" if houses else "success_zero_records"
            )
            city_status["periodsChecked"].append({
                "period": key,
                "status": city_status["status"],
                "rawRecordCount": len(payload["data"]),
                "detachedHouseRecordCount": len(houses),
            })

            print(
                f"  {key}: raw records={len(payload['data'])}, "
                f"detached-house candidates={len(houses)}"
            )

            saved_existing = True
            break

        if city_status["status"] == "not_attempted":
            if saved_existing:
                city_status["status"] = "already_stored"
            elif city_status["periodsChecked"]:
                city_status["status"] = "no_data_in_search_window"
            else:
                city_status["status"] = "no_data_in_search_window"

        retrieval_status["cities"][city_key] = city_status

    # Sort periods chronologically and preserve all historical entries.
    database["periods"] = sorted(
        database.get("periods", []),
        key=lambda item: (
            int(item.get("year", 0)),
            int(item.get("quarter", 0)),
        ) if isinstance(item, dict) else (0, 0),
    )

    database["schemaVersion"] = database.get("schemaVersion", "1.0")
    database["updatedAt"] = utc_now_iso()
    database["retrievalStatus"] = retrieval_status

    try:
        save_data(database)
    except OSError as exc:
        print(
            "::error::Could not save transactions.json; "
            f"existing file should remain intact: {type(exc).__name__}"
        )
        return 1

    print("\n" + "-" * 60)
    print(f"Saved: {OUTPUT_FILE}")
    print(f"Periods stored: {len(database['periods'])}")
    print("Retrieval results:")

    for city_key, status in retrieval_status["cities"].items():
        print(
            f"  {status['name']}: {status['status']}; "
            f"period={status['selectedPeriod']}; "
            f"records={status['recordsSaved']}"
        )

    print("=" * 60)

    # API retrieval errors are recorded, and the existing dataset is
    # preserved. Return success so market_pipeline.py can still run.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
