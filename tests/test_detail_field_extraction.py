import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "adapters"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import suumo_detail
import main


class DetailFieldExtractionTests(unittest.TestCase):
    def test_enrich_does_not_increment_fetch_attempt_counter(self):
        row = {"propertyId": "suumo:nc_12345678", "detailFetchAttempts": 1}
        result = main.enrich_with_detail(
            row,
            {
                "success": True,
                "detail": {
                    "title": "柏市 新築戸建 4LDK",
                    "priceYen": 59_800_000,
                    "landAreaM2": 120.5,
                    "buildingAreaM2": 95.2,
                    "layout": "4LDK",
                    "propertyType": "新築戸建",
                },
            },
        )
        self.assertEqual(result["detailFetchAttempts"], 1)

    def test_house_record_preserves_area_layout_and_construction_fields(self):
        row = {
            "propertyId": "suumo:nc_12345678",
            "source": "suumo",
            "sourceId": "nc_12345678",
            "sourceUrl": "https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345678/",
            "status": "active",
            "priceYen": 59800000,
            "landAreaM2": 120.5,
            "buildingAreaM2": 95.2,
            "layout": "4LDK",
            "constructionMonth": "2026-10",
            "detail": {
                "priceYen": 59800000,
                "landAreaM2": 120.5,
                "buildingAreaM2": 95.2,
                "layout": "4LDK",
                "constructionMonth": "2026-10",
                "buildingFootprintAreaM2": 55.1,
            },
        }
        record = main.build_market_house_record(row)
        self.assertEqual(record["landAreaM2"], 120.5)
        self.assertEqual(record["buildingAreaM2"], 95.2)
        self.assertEqual(record["layout"], "4LDK")
        self.assertEqual(record["layoutRaw"], "4LDK")
        self.assertEqual(record["constructionMonth"], "2026-10")
        self.assertEqual(record["buildingFootprintAreaM2"], 55.1)

    def test_area_with_commas_and_units(self):
        self.assertEqual(suumo_detail.parse_area_m2("1,234.56 m²"), 1234.56)
        self.assertEqual(suumo_detail.parse_area_m2("９５．２㎡"), 95.2)

    def test_bare_number_only_when_label_context_is_known(self):
        self.assertEqual(suumo_detail.parse_area_m2("120.50", allow_bare_number=True), 120.5)
        self.assertIsNone(suumo_detail.parse_area_m2("120.50"))

    def test_layout_from_labelled_page_text(self):
        candidates = suumo_detail.collect_layout_candidates(
            [], [], page_text="物件概要 間取り 4LDK 土地面積 120.50m²",
            title="新築戸建て",
        )
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["value"], "4LDK")

    def test_layout_full_width_and_plus_s(self):
        self.assertEqual(suumo_detail.normalize_layout("４ＬＤＫ＋Ｓ"), "4LDK+S")
        self.assertEqual(suumo_detail.normalize_layout("3LDK+2S"), "3LDK+2S")

    def test_parse_detail_html_extracts_core_fields_end_to_end(self):
        html = """<!doctype html><html><head><title>柏市 新築戸建 4LDK</title></head><body>
        <h1>柏市 新築戸建 4LDK</h1><table>
        <tr><th>所在地</th><td>千葉県柏市柏の葉一丁目</td></tr>
        <tr><th>販売価格</th><td>5,980万円</td></tr>
        <tr><th>土地面積</th><td>120.50</td></tr>
        <tr><th>建物面積</th><td>95.20</td></tr>
        <tr><th>間取り</th><td>4LDK</td></tr>
        </table></body></html>"""
        result = suumo_detail.parse_detail_html(
            html=html,
            source_url="https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345678/",
            request_url="https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345678/",
            final_url="https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345678/",
            http_status=200,
            target_stations=["柏の葉キャンパス", "流山おおたかの森"],
        )
        self.assertEqual(result["landAreaM2"], 120.5)
        self.assertEqual(result["buildingAreaM2"], 95.2)
        self.assertEqual(result["layout"], "4LDK")
        self.assertEqual(result["priceYen"], 59_800_000)

    def test_land_and_building_bare_values_are_extracted_by_label(self):
        land = suumo_detail.collect_land_area_candidates(
            [{"label": "土地面積", "value": "120.50", "source": "table_th_td"}],
            [], "",
        )
        building, footprint = suumo_detail.collect_building_area_candidates(
            [{"label": "建物面積", "value": "95.20", "source": "table_th_td"}],
            [], "",
        )
        self.assertEqual(land[0]["value"], 120.5)
        self.assertEqual(building[0]["value"], 95.2)
        self.assertEqual(footprint, [])


if __name__ == "__main__":
    unittest.main()
