from abc import ABC, abstractmethod


class PropertyAdapter(ABC):

    def __init__(self, config=None):
        self.config = config or {}

    @abstractmethod
    def search(self, search_config):
        """
        共通フォーマットの物件リストを返す。
        """
        raise NotImplementedError
