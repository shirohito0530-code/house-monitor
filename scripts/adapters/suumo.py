from .base import PropertyAdapter


class SuumoAdapter(PropertyAdapter):

    def __init__(self, config):
        self.config = config

    def search(self, search_config):
        properties = []

        # ここにSUUMO用の許可された取得処理を実装
        # 取得後は共通フォーマットへ変換する

        return properties
