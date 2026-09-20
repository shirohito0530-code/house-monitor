import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests


# ==========================================
# 設定
# ==========================================

API_URL = (
    "https://www.reinfolib.mlit.go.jp/"
    "ex-api/external/XIT001"
)

API_KEY = os.environ.get(
    "REINFOLIB_API_KEY"
)

OUTPUT_FILE = Path(
    "data/transactions.json"
)


# ==========================================
# 対象地域
# ==========================================

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


# ==========================================
# APIキー確認
# ==========================================

def check_api_key():

    if not API_KEY:

        raise RuntimeError(
            "REINFOLIB_API_KEY が "
            "GitHub Actions Secrets に設定されていません"
        )


# ==========================================
# 対象四半期
# ==========================================

def get_target_quarter():

    now = datetime.now(
        timezone.utc
    )

    year = now.year
    quarter = (
        (now.month - 1) // 3
    ) + 1

    # 最新四半期が未公表の可能性があるため、
    # 1四半期前を取得対象にする
    quarter -= 1

    if quarter == 0:
        year -= 1
        quarter = 4

    return year, quarter


# ==========================================
# API取得
# ==========================================

def fetch_transactions(
    city_code,
    year,
    quarter
):

    headers = {
        "Ocp-Apim-Subscription-Key":
            API_KEY
    }

    params = {
        "year": year,
        "quarter": quarter,
        "city": city_code,
        "priceClassification": "02",
        "language": "ja",
    }

    response = requests.get(
        API_URL,
        headers=headers,
        params=params,
        timeout=60
    )

    response.raise_for_status()

    # APIはgzip圧縮されたJSONを返す場合がある
    try:

        return response.json()

    except Exception:

        return json.loads(
            gzip.decompress(
                response.content
            )
        )


# ==========================================
# 戸建住宅だけ抽出
# ==========================================

def filter_detached_houses(data):

    if not isinstance(data, dict):
        return []

    records = data.get(
        "data",
        []
    )

    result = []

    for record in records:

        trade_type = str(
            record.get(
                "Type",
                ""
            )
        )

        # 「宅地(土地と建物)」を
        # 戸建住宅候補として扱う
        if trade_type != "宅地(土地と建物)":
            continue

        result.append(record)

    return result


# ==========================================
# 既存データ読み込み
# ==========================================

def load_existing():

    if not OUTPUT_FILE.exists():

        return {
            "updatedAt": None,
            "periods": []
        }

    with open(
        OUTPUT_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


# ==========================================
# 保存
# ==========================================

def save_data(data):

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

        f.write("\n")


# ==========================================
# メイン
# ==========================================

def main():

    check_api_key()

    year, quarter = (
        get_target_quarter()
    )

    print(
        "================================"
    )

    print(
        "REAL ESTATE TRANSACTION DATA"
    )

    print(
        "================================"
    )

    print(
        f"Target period: "
        f"{year} Q{quarter}"
    )

    existing = load_existing()

    period_key = (
        f"{year}Q{quarter}"
    )

    # 同じ四半期を再取得した場合は
    # 重複登録しない
    already_exists = any(
        p.get("period") == period_key
        for p in existing.get(
            "periods",
            []
        )
    )

    if already_exists:

        print(
            f"Period {period_key} "
            f"already exists."
        )

    else:

        period_data = {
            "period": period_key,
            "year": year,
            "quarter": quarter,
            "cities": {}
        }

        for key, city in CITIES.items():

            print(
                f"Fetching: "
                f"{city['name']}"
            )

            raw_data = fetch_transactions(
                city["code"],
                year,
                quarter
            )

            houses = filter_detached_houses(
                raw_data
            )

            period_data[
                "cities"
            ][key] = {
                "name": city["name"],
                "code": city["code"],
                "area": city["area"],
                "count": len(houses),
                "data": houses
            }

            print(
                f"  Detached house records: "
                f"{len(houses)}"
            )

        existing.setdefault(
            "periods",
            []
        ).append(
            period_data
        )

    # ======================================
    # 更新日時
    # ======================================

    existing["updatedAt"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    save_data(existing)

    print(
        "--------------------------------"
    )

    print(
        f"Saved: "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Periods stored: "
        f"{len(existing['periods'])}"
    )

    print(
        "================================"
    )


if __name__ == "__main__":
    main()
