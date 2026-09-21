import json
from pathlib import Path


def load_json(path, default=None):
    path = Path(path)

    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    except (json.JSONDecodeError, OSError) as error:
        print(f"JSON読み込みエラー: {path}: {error}")
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(".tmp")

    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )
        file.write("\n")

    temporary_path.replace(path)
