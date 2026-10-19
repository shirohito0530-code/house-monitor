import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import market_pipeline as mp


class AreaClassificationTests(unittest.TestCase):
    def test_outside_city_rejected(self):
        status, reason, area = mp.classify_area({"address": "埼玉県さいたま市浦和区", "sourceUrl": "https://suumo.jp/ikkodate/saitama/sc_urawa/nc_12345678/"})
        self.assertEqual(status, "rejected")
        self.assertIn("outside", reason)
        self.assertIsNone(area)

    def test_target_city_station_and_walk_accepted(self):
        status, reason, area = mp.classify_area({"address": "千葉県柏市若柴123", "targetStation": "柏の葉キャンパス", "targetStationWalkMinutes": 10, "sourceUrl": "https://suumo.jp/chukoikkodate/chiba/sc_kashiwa/nc_12345678/"})
        self.assertEqual(status, "accepted")
        self.assertEqual(area, "柏の葉キャンパス")

    def test_missing_walk_review_not_accepted(self):
        status, reason, area = mp.classify_area({"address": "千葉県流山市市野谷", "targetStation": "流山おおたかの森", "sourceUrl": "https://suumo.jp/chukoikkodate/chiba/sc_nagareyama/nc_12345678/"})
        self.assertEqual(status, "review")
        self.assertEqual(area, "流山おおたかの森")

    def test_city_url_mismatch_is_review(self):
        status, reason, area = mp.classify_area({"address": "千葉県柏市若柴123", "targetStation": "柏の葉キャンパス", "targetStationWalkMinutes": 10, "sourceUrl": "https://suumo.jp/chukoikkodate/chiba/sc_nagareyama/nc_12345678/"})
        self.assertEqual(status, "review")
        self.assertEqual(reason, "address_url_city_mismatch")


class DuplicateTests(unittest.TestCase):
    def test_town_only_address_never_auto_merges(self):
        a = {"propertyId": "a", "address": "千葉県柏市若柴", "landAreaM2": 120, "buildingAreaM2": 95, "constructionYear": 2015}
        b = {"propertyId": "b", "address": "千葉県柏市若柴", "landAreaM2": 120, "buildingAreaM2": 95, "constructionYear": 2015}
        self.assertFalse(mp.high_confidence_same(a, b))

    def test_full_address_and_areas_can_auto_group(self):
        a = {"propertyId": "a", "address": "千葉県柏市若柴123-1", "landAreaM2": 120, "buildingAreaM2": 95, "constructionYear": 2015}
        b = {"propertyId": "b", "address": "千葉県柏市若柴123番地1", "landAreaM2": 120.5, "buildingAreaM2": 95.4, "constructionYear": 2015}
        self.assertTrue(mp.high_confidence_same(a, b))

    def test_manual_same_decision_merges_logically(self):
        a = {"propertyId": "a", "address": "千葉県柏市若柴123-1", "landAreaM2": 120, "buildingAreaM2": 95, "constructionYear": 2015}
        b = {"propertyId": "b", "address": "千葉県柏市若柴123番地1", "landAreaM2": 120.5, "buildingAreaM2": 95.4, "constructionYear": 2015}
        result = mp.build_duplicate_groups([a, b], [], {})
        self.assertEqual(result["mergedListingCount"], 1)
        self.assertEqual(a["canonicalPropertyId"], b["canonicalPropertyId"])

    def test_manual_same_decision_applies_to_review_candidate(self):
        a = {"propertyId": "a", "address": "千葉県柏市若柴123-1", "landAreaM2": 120, "buildingAreaM2": 95, "constructionYear": 2015}
        b = {"propertyId": "b", "address": "千葉県柏市若柴123-1", "landAreaM2": 120, "buildingAreaM2": 110, "constructionYear": 2015}
        candidate_id = mp.stable_hash("a|b", "dup:")
        initial = mp.build_duplicate_groups([a.copy(), b.copy()], [], {})
        self.assertTrue(any(x["candidateId"] == candidate_id for x in initial["reviewCandidates"]))
        result = mp.build_duplicate_groups([a, b], [], {candidate_id: "same"})
        self.assertEqual(result["mergedListingCount"], 1)
        self.assertEqual(a["canonicalPropertyId"], b["canonicalPropertyId"])

    def test_manual_different_decision_removes_candidate(self):
        a = {"propertyId": "a", "address": "千葉県柏市若柴123-1", "landAreaM2": 120, "buildingAreaM2": 95, "constructionYear": 2015}
        b = {"propertyId": "b", "address": "千葉県柏市若柴123-1", "landAreaM2": 120, "buildingAreaM2": 110, "constructionYear": 2015}
        candidate_id = mp.stable_hash("a|b", "dup:")
        result = mp.build_duplicate_groups([a, b], [], {candidate_id: "different"})
        self.assertEqual(result["mergedListingCount"], 0)
        self.assertFalse(result["reviewCandidates"])


class MarketIndicatorTests(unittest.TestCase):
    def test_transaction_metric(self):
        rows = [
            {"_cityName": "柏市", "TradePrice": 40000000, "Area": 120},
            {"_cityName": "柏市", "TradePrice": 45000000, "Area": 130},
            {"_cityName": "柏市", "TradePrice": 50000000, "Area": 140},
        ]
        metrics = mp.build_transaction_metrics(rows)
        self.assertEqual(metrics["柏の葉キャンパス"]["transactionCount"], 3)
        self.assertEqual(metrics["柏の葉キャンパス"]["medianTransactionPriceYen"], 45000000)

    def test_score_explains_reduction(self):
        now = mp.utc_now()
        item = {"currentPrice": 40000000, "firstPrice": 45000000, "priceReductionRate": 11.1, "daysListed": 100}
        result = mp.score_property(item, [], [], now)
        self.assertIsNotNone(result["score"])
        self.assertIn("値下げ", " ".join(result["reasons"]))
        self.assertIn(result["label"], {"割安候補", "相場並み", "割高候補"})

    def test_school_district_stays_unconfirmed_without_exact_verification(self):
        status, reason = mp.school_status({"address": "千葉県柏市若柴123"}, {"verifiedAddresses": [], "officialSource": "official"})
        self.assertEqual(status, "unconfirmed")


if __name__ == "__main__":
    unittest.main()
