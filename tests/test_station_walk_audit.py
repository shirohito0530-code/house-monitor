import unittest
from scripts.audit_station_walk import identity_keys, inspect_row, parse_walk
class StationWalkAuditTests(unittest.TestCase):
    def test_parse_numeric_walk(self):
        self.assertEqual(parse_walk(8),8.0); self.assertEqual(parse_walk("徒歩12分"),12.0)
        self.assertIsNone(parse_walk(None)); self.assertIsNone(parse_walk(True))
        self.assertIsNone(parse_walk(0)); self.assertIsNone(parse_walk(121))
    def test_nested_detail_walk(self):
        row={"propertyId":"suumo:nc_12345678","detail":{"targetStationWalkMinutes":9,"targetStationWalkAvailable":True,"transportRaw":"柏の葉キャンパス駅 徒歩9分"}}
        x=inspect_row(row);self.assertEqual(x["walkMinutes"],9.0);self.assertEqual(x["category"],"walk_value_found")
        self.assertEqual(x["rawTransportPath"],"detail.transportRaw")
    def test_raw_without_walk(self):
        x=inspect_row({"propertyId":"suumo:nc_12345679","transportRaw":"柏の葉キャンパス駅 徒歩8分"})
        self.assertIsNone(x["walkMinutes"]);self.assertEqual(x["category"],"raw_transport_without_walk")
    def test_false_availability(self):
        x=inspect_row({"propertyId":"suumo:nc_12345680","targetStationWalkMinutes":10,"targetStationWalkAvailable":False})
        self.assertEqual(x["category"],"walk_value_but_unavailable_flag")
    def test_suumo_url_id(self):
        keys=identity_keys({"propertyId":"legacy-id","sourceUrl":"https://suumo.jp/chukoikkodate/nc_12345678/"})
        self.assertIn(("suumo_listing_id","nc_12345678"),keys)
if __name__=="__main__":unittest.main()
