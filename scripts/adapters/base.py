from abc import ABC, abstractmethod


class PropertyAdapter(ABC):

    @abstractmethod
    def search(self, search_config):
        """
        物件情報を取得し、共通フォーマットの
        リストを返す。
        """
        raise NotImplementedError
