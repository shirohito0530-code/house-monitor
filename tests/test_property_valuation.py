import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from property_valuation import build_property_valuation


class PropertyValuationTests(unittest.TestCase):
    def test_multifactor_scoring_and_land_building_components(self):
        item = {
            "propertyId": "x", "address": "千葉県柏市若柴",
            "area": "柏の葉キャンパス", "currentPrice": 42000000,
            "landAreaM2": 120, "buildingAreaM2": 95,
            "constructionYear": 2015, "structure": "木造",
            "targetStationWalkMinutes": 8, "daysListed": 90,
            "firstPrice": 45000000, "schoolDistrictStatus": "unconfirmed",
        }
        rows = []
        for i, price in enumerate([40000000, 42000000, 43000000, 44000000, 41000000]):
            rows.append({
                "_cityName": "柏市", "Type": "宅地(土地と建物)",
                "TradePrice": str(price), "Area": "120",
                "TotalFloorArea": "95", "BuildingYear": "2015年",
                "Structure": "木造", "DistrictName": "若柴",
            })
        result = build_property_valuation(item, rows)
        self.assertIsNotNone(result["score"])
        self.assertIn("estimatedLandValueYen", result)
        self.assertIsNotNone(result["estimatedLandValueYen"])
        self.assertIsNotNone(result["estimatedBuildingValueYen"])
        self.assertIn("price", result["components"])
        self.assertIn("premium", result["components"])
        self.assertTrue(result["reasons"])
        self.assertTrue(result["disclaimer"])

    def test_missing_data_is_not_reported_as_safe_or_high_confidence(self):
        result = build_property_valuation({"address": "千葉県柏市若柴"}, [])
        self.assertEqual(result["confidence"], "low")
        self.assertIsNone(result["estimatedBaseValueYen"])
        self.assertTrue(result["limitations"])
        self.assertIn("データ不足", result["label"])

    def test_outside_city_has_no_transaction_comparables(self):
        result = build_property_valuation({
            "address": "埼玉県さいたま市", "currentPrice": 30000000,
            "landAreaM2": 100, "buildingAreaM2": 90, "constructionYear": 2010,
        }, [{"_cityName": "柏市", "TradePrice": 40000000, "Area": 120, "TotalFloorArea": 90}])
        self.assertEqual(result["comparableCount"], 0)


if __name__ == "__main__":
    unittest.main()
