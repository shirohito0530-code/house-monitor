import json
from pathlib import Path


def load_json(path, default=None):
    """
    JSONファイルを読み込む。

    ファイルが存在しない場合や
    JSONが不正な場合はdefaultを返す。
    """

    path = Path(path)

    if not path.exists():
        return default

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

    except (
        json.JSONDecodeError,
        OSError
    ) as error:

        print(
            f"JSON読み込みエラー: "
            f"{path}: {error}"
        )

        return default


def save_json(path, data):
    """
    JSONを一時ファイルに保存し、
    保存成功後に本ファイルへ置き換える。
    """

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary_path = path.with_suffix(
        ".tmp"
    )

    try:

        with open(
            temporary_path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

            file.write("\n")

        temporary_path.replace(
            path
        )

        print(
            f"JSON保存完了: {path}"
        )

    except OSError as error:

        print(
            f"JSON保存エラー: "
            f"{path}: {error}"
        )

        if temporary_path.exists():
            temporary_path.unlink()

        raise

def load_observations(
    path,
):
    data = load_json(
        path,
        default=[],
    )

    if isinstance(
        data,
        dict,
    ):
        data = data.get(
            "observations",
            [],
        )

    if not isinstance(
        data,
        list,
    ):
        return []

    return data


def save_observations(
    path,
    observations,
):
    save_json(
        path,
        {
            "updatedAt": (
                __import__(
                    "datetime"
                ).datetime.now(
                    __import__(
                        "datetime"
                    ).timezone.utc
                ).isoformat()
            ),
            "observations": observations,
        },
    )