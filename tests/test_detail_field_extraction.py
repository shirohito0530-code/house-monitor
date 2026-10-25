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


    def test_discovery_record_persists_detail_fields(self):
        row = {
            "propertyId": "suumo:nc_12345678",
            "sourceId": "nc_12345678",
            "sourceUrl": "https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345678/",
            "detail": {
                "address": "千葉県柏市柏の葉一丁目1",
                "landAreaM2": 120.5,
                "buildingAreaM2": 95.2,
                "layout": "4LDK",
                "constructionYear": 2022,
                "constructionMonth": "2022-06",
                "propertyType": "中古戸建",
            },
        }
        record = main.build_discovery_record(row)
        self.assertEqual(record["landAreaM2"], 120.5)
        self.assertEqual(record["buildingAreaM2"], 95.2)
        self.assertEqual(record["layout"], "4LDK")
        self.assertEqual(record["constructionYear"], 2022)
        self.assertEqual(record["constructionMonth"], "2022-06")
        self.assertEqual(record["propertyType"], "中古戸建")
        self.assertEqual(record["detailDataVersion"], "1.1")

    def test_backfill_discovery_detail_from_matching_house_id(self):
        discovery = [{
            "propertyId": "suumo:nc_12345678",
            "sourceId": "nc_12345678",
            "sourceUrl": "https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345678/",
        }]
        houses = [{
            "propertyId": "suumo:nc_12345678",
            "sourceId": "nc_12345678",
            "sourceUrl": "https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345678/",
            "landAreaM2": 120.5,
            "buildingAreaM2": 95.2,
            "layout": "4LDK",
            "constructionMonth": "2022-06",
            "propertyType": "中古戸建",
        }]
        rows, counts = main.backfill_discovery_records_from_houses(discovery, houses)
        self.assertEqual(rows[0]["landAreaM2"], 120.5)
        self.assertEqual(rows[0]["buildingAreaM2"], 95.2)
        self.assertEqual(rows[0]["layout"], "4LDK")
        self.assertEqual(rows[0]["propertyType"], "中古戸建")
        self.assertEqual(counts["landAreaM2"], 1)

    def test_legacy_built_age_zero_is_promoted_and_persisted(self):
        row = {
            "propertyId": "suumo:nc_12345001",
            "sourceId": "nc_12345001",
            "sourceUrl": "https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345001/",
            "builtAgeYears": 0.0,
        }

        promoted = main._promote_discovery_detail_fields(row)
        self.assertEqual(promoted["constructionAgeYears"], 0.0)
        self.assertEqual(promoted["builtAgeYears"], 0.0)

        discovery = main.build_discovery_record(row)
        self.assertEqual(discovery["constructionAgeYears"], 0.0)
        self.assertEqual(discovery["builtAgeYears"], 0.0)

        house = main.build_market_house_record(row)
        self.assertEqual(house["constructionAgeYears"], 0.0)
        self.assertEqual(house["builtAgeYears"], 0.0)

    def test_legacy_built_age_nested_detail_is_promoted(self):
        row = {
            "propertyId": "suumo:nc_12345002",
            "detail": {"builtAgeYears": 3},
        }
        promoted = main._promote_discovery_detail_fields(row)
        self.assertEqual(promoted["constructionAgeYears"], 3)
        self.assertEqual(promoted["builtAgeYears"], 3)

    def test_existing_canonical_age_is_not_overwritten_by_alias(self):
        row = {
            "constructionAgeYears": 4,
            "builtAgeYears": 5,
            "detail": {"builtAgeYears": 6},
        }
        promoted = main._promote_discovery_detail_fields(row)
        self.assertEqual(promoted["constructionAgeYears"], 4)
        self.assertEqual(promoted["builtAgeYears"], 5)

    def test_backfill_legacy_built_age_from_exact_matching_house(self):
        discovery = [{
            "propertyId": "suumo:nc_12345003",
            "sourceId": "nc_12345003",
            "sourceUrl": "https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345003/",
        }]
        houses = [{
            "propertyId": "suumo:nc_12345003",
            "sourceId": "nc_12345003",
            "sourceUrl": "https://suumo.jp/ikkodate/chiba/sc_kashiwa/nc_12345003/",
            "builtAgeYears": 0.0,
        }]
        rows, counts = main.backfill_discovery_records_from_houses(discovery, houses)
        self.assertEqual(rows[0]["constructionAgeYears"], 0.0)
        self.assertEqual(rows[0]["builtAgeYears"], 0.0)
        self.assertGreaterEqual(counts["builtAgeYears"], 1)

    def test_backfill_does_not_overwrite_existing_age_or_merge_ambiguous_ids(self):
        discovery = [{
            "propertyId": "suumo:nc_12345004",
            "sourceId": "nc_12345004",
            "constructionAgeYears": 2,
        }]
        houses = [{
            "propertyId": "suumo:nc_12345004",
            "sourceId": "nc_12345004",
            "builtAgeYears": 7,
        }]
        rows, _ = main.backfill_discovery_records_from_houses(discovery, houses)
        self.assertEqual(rows[0]["constructionAgeYears"], 2)

        ambiguous_discovery = [{
            "propertyId": "suumo:nc_12345005",
            "sourceId": "nc_12345005",
        }]
        ambiguous_houses = [
            {"propertyId": "suumo:nc_12345005", "sourceId": "nc_12345005", "builtAgeYears": 2},
            {"propertyId": "suumo:nc_12345005", "sourceId": "nc_12345005", "builtAgeYears": 3},
        ]
        rows, _ = main.backfill_discovery_records_from_houses(
            ambiguous_discovery, ambiguous_houses
        )
        self.assertNotIn("constructionAgeYears", rows[0])
        self.assertNotIn("builtAgeYears", rows[0])

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
