import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from listing_lifecycle import normalize_status, transition_status
from detail_enrichment import enrich_detail_record, summarize_field_coverage

class LifecycleTests(unittest.TestCase):
    def test_search_failure_does_not_increment_misses(self):
        record = {"status": "active", "missedSearchRuns": 0}
        transition_status(record, healthy_search_completed=False)
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["missedSearchRuns"], 0)
    def test_two_healthy_misses_end_observation(self):
        record = {"status": "active", "missedSearchRuns": 0}
        transition_status(record, healthy_search_completed=True, observed_now="2026-01-01T00:00:00+00:00")
        self.assertEqual(record["status"], "possibly_ended")
        transition_status(record, healthy_search_completed=True, observed_now="2026-01-02T00:00:00+00:00")
        self.assertEqual(record["status"], "observed_ended")
    def test_explicit_evidence_confirms_end(self):
        record = {"status": "active"}
        transition_status(record, explicit_end_evidence="掲載を終了しました")
        self.assertEqual(record["status"], "confirmed_ended")
    def test_legacy_confirmed_without_evidence_is_downgraded(self):
        self.assertEqual(normalize_status({"status": "confirmed_ended"}), "observed_ended")

class EnrichmentTests(unittest.TestCase):
    def test_promotes_area_without_guessing(self):
        record = {"detail": {"landAreaM2": "120.5㎡", "extractionAudit": {"landAreaM2": {"selected_confidence": 0.95}}}}
        enrich_detail_record(record)
        self.assertEqual(record["landAreaM2"], 120.5)
        self.assertEqual(record["land"], 120.5)
        self.assertEqual(record["fieldExtractionAudit"]["buildingAreaM2"]["status"], "missing")
    def test_coverage(self):
        result = summarize_field_coverage([{"landAreaM2": 100}, {}])
        self.assertEqual(result["fields"]["landAreaM2"]["count"], 1)
        self.assertEqual(result["fields"]["landAreaM2"]["missing"], 1)

if __name__ == "__main__":
    unittest.main()
