import json
import os
import re
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

# 対象条件の設定
TARGET_URLS = [
    # SUUMOやHomesなどの対象検索URL（柏の葉キャンパス・流山おおたかの森エリア）
    # ※まずは動作確認用のベース処理
]

JSON_PATH = "data/houses.json"

def load_data():
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"updatedAt": None, "properties": []}

def save_data(data):
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

def main():
    data = load_data()
    print(f"Current properties count: {len(data.get('properties', []))}")
    # 今後の検索・判別ロジックをここに組み込みます
    save_data(data)
    print("Update completed.")

if __name__ == "__main__":
    main()
