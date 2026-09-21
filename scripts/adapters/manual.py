from pathlib import Path

from adapters.base import PropertyAdapter
from storage import load_json


class ManualAdapter(PropertyAdapter):

    def __init__(self, config, root_path):
        super().__init__(config)
        self.root_path = Path(root_path)

    def search(self, search_config):
        source_file = (
            self.root_path
            / "data"
            / "source_properties.json"
        )

        data = load_json(
            source_file,
            default={"properties": []}
        )

        return data.get("properties", [])
