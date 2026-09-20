import os
import json
import gzip
import requests
from datetime import datetime


API_URL = "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001"

API_KEY = os.environ.get("REINFOLIB_API_KEY")

if not API_KEY:
    raise RuntimeError("REINFOLIB_API_KEY is not configured")


def get_transactions(year, quarter, city):
    headers = {
        "Ocp-Apim-Subscription-Key": API_KEY
    }

    params = {
        "year": year,
        "quarter": quarter,
        "city": city,
        "priceClassification": "02",
        "language": "ja"
    }

    response = requests.get(
        API_URL,
        headers=headers,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    try:
        data = response.json()
    except Exception:
        data = json.loads(gzip.decompress(response.content))

    return data


def main():
    # 柏市
    kashiwa = get_transactions(
        year=datetime.now().year - 1,
        quarter=4,
        city="12217"
    )

    # 流山市
    nagareyama = get_transactions(
        year=datetime.now().year - 1,
        quarter=4,
        city="12220"
    )

    result = {
        "updatedAt": datetime.now().isoformat(),
        "kashiwa": kashiwa,
        "nagareyama": nagareyama
    }

    os.makedirs("data", exist_ok=True)

    with open(
        "data/transactions.json",
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            result,
            f,
            ensure_ascii=False,
            indent=2
        )


if __name__ == "__main__":
    main()
