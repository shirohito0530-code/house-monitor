#!/usr/bin/env python3
# Safely apply the Phase 1/2 overlay to an existing house-monitor checkout.
from pathlib import Path
import sys

def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}; no changes written")
    return text.replace(old, new, 1)

def main():
    if len(sys.argv) != 2:
        print("Usage: python apply_phase12.py /path/to/house-monitor", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    main_py, index_html = root / "scripts/main.py", root / "index.html"
    workflow = root / ".github/workflows/update.yml"
    if not all(p.exists() for p in (main_py, index_html, workflow)):
        raise RuntimeError("Target does not look like the expected house-monitor repository")
    m, h, w = (p.read_text(encoding="utf-8") for p in (main_py, index_html, workflow))

    import_anchor = "# ============================================================\n# Constants\n# ============================================================\n"
    import_block = "try:\n    from detail_enrichment import enrich_detail_record, summarize_field_coverage\nexcept ImportError:\n    from scripts.detail_enrichment import enrich_detail_record, summarize_field_coverage\n\n\n"
    if "from detail_enrichment import enrich_detail_record" not in m:
        m = replace_once(m, import_anchor, import_block + import_anchor, "main.py import")
    m = m.replace('MAIN_PARSER_VERSION = "2026-10-14-v44-detail-field-coverage-counter-fix"', 'MAIN_PARSER_VERSION = "2026-10-18-v45-phase12-field-audit-lifecycle"', 1)
    if "enrich_detail_record(prop)" not in m:
        anchor = "            promote_detail_fields(prop)\n"
        m = replace_once(m, anchor, anchor + "            enrich_detail_record(prop)\n", "main.py detail enrichment")
    if '"fieldExtractionAudit": summarize_field_coverage' not in m:
        anchor = '        "fieldCoverage": field_coverage,\n'
        m = replace_once(m, anchor, anchor + '        "fieldExtractionAudit": summarize_field_coverage(deduplicated_houses),\n', "main.py coverage summary")

    if 'id="propertyLifecycleFilter"' not in h:
        anchor = '''        <select id="propertyQuality">
          <option value="">全品質</option>
          <option value="complete">COMPLETE</option>
          <option value="partial">PARTIAL</option>
          <option value="fetch_error">FETCH ERROR</option>
          <option value="unknown">UNKNOWN</option>
        </select>
'''
        new = anchor + '''
        <select id="propertyLifecycleFilter" aria-label="掲載状態">
          <option value="">全掲載状態</option>
          <option value="active">掲載中</option>
          <option value="possibly_ended">終了の可能性あり</option>
          <option value="observed_ended">掲載終了（検索未検出）</option>
          <option value="confirmed_ended">掲載終了（明示確認）</option>
        </select>
'''
        h = replace_once(h, anchor, new, "index lifecycle filter")
    if 'id="missingFieldFilter"' not in h:
        anchor = '''        <label class="filter-check">
          <input id="detailFetchedOnly" type="checkbox">
          詳細取得済みのみ
        </label>
'''
        new = anchor + '''
        <select id="missingFieldFilter" aria-label="不足項目">
          <option value="">不足項目：すべて</option>
          <option value="landAreaM2">土地面積が不足</option>
          <option value="buildingAreaM2">建物面積が不足</option>
          <option value="layout">間取りが不足</option>
          <option value="constructionMonth">築年月が不足</option>
          <option value="anyCore">主要項目のいずれかが不足</option>
        </select>
'''
        h = replace_once(h, anchor, new, "index missing-field filter")
    if "const lifecycleFilter =" not in h:
        anchor = '  const walkLimit = document.getElementById("propertyWalkFilter").value;\n'
        h = replace_once(h, anchor, anchor + '  const lifecycleFilter = document.getElementById("propertyLifecycleFilter")?.value || "";\n  const missingFieldFilter = document.getElementById("missingFieldFilter")?.value || "";\n', "index filter state")
    if "const matchesLifecycle =" not in h:
        anchor = '''      const matchesFetched =
        !detailFetchedOnly ||
        isDetailFetched(property);
'''
        new = anchor + '''
      const lifecycleStatus = getListingStatus(property);
      const matchesLifecycle = !lifecycleFilter || lifecycleStatus === lifecycleFilter;
      const coreFields = ["landAreaM2", "buildingAreaM2", "layout", "constructionMonth"];
      const hasField = field => {
        const value = property[field] ?? detail[field];
        return value !== null && value !== undefined && String(value).trim() !== "" && String(value).trim() !== "-";
      };
      const matchesMissingField = !missingFieldFilter || (
        missingFieldFilter === "anyCore"
          ? coreFields.some(field => !hasField(field))
          : !hasField(missingFieldFilter)
      );
'''
        h = replace_once(h, anchor, new, "index filter predicate")
    if "matchesLifecycle &&" not in h:
        anchor = '''        matchesFetched &&
        matchesRecent &&
        matchesUserStatus &&
        matchesSchoolDecision
'''
        new = '''        matchesFetched &&
        matchesLifecycle &&
        matchesMissingField &&
        matchesRecent &&
        matchesUserStatus &&
        matchesSchoolDecision
'''
        h = replace_once(h, anchor, new, "index filter return")
    if '"propertyLifecycleFilter"' not in h[h.find("const propertyFilterInputs"):]:
        anchor = '    "propertyQuality",\n'
        h = replace_once(h, anchor, anchor + '    "propertyLifecycleFilter",\n    "missingFieldFilter",\n', "index filter listeners")
    if 'document.getElementById("propertyLifecycleFilter").value = ""' not in h:
        anchor = '      document.getElementById("propertyQuality").value = "";\n'
        h = replace_once(h, anchor, anchor + '      document.getElementById("propertyLifecycleFilter").value = "";\n      document.getElementById("missingFieldFilter").value = "";\n', "index reset")
    h = h.replace('content="2026-10-12-listing-lifecycle-filters-v3"', 'content="2026-10-18-phase12-lifecycle-field-filter"', 1)

    if "python scripts/validate_db.py" not in w:
        anchor = "      - name: Validate output JSON and identity consistency\n"
        w = replace_once(w, anchor, "      - name: Validate Phase 1/2 data quality\n        run: python scripts/validate_db.py\n\n" + anchor, "workflow validator")
    w = w.replace("git add data/discovered_listings.json data/houses.json", "git add data/discovered_listings.json data/houses.json data/summary.json", 1)

    # Write only after all anchor checks have passed.
    (root / "scripts/listing_lifecycle.py").write_text((Path(__file__).parent / "scripts/listing_lifecycle.py").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "scripts/detail_enrichment.py").write_text((Path(__file__).parent / "scripts/detail_enrichment.py").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "scripts/validate_db.py").write_text((Path(__file__).parent / "scripts/validate_db.py").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests/test_phase12.py").write_text((Path(__file__).parent / "tests/test_phase12.py").read_text(encoding="utf-8"), encoding="utf-8")
    main_py.write_text(m, encoding="utf-8")
    index_html.write_text(h, encoding="utf-8")
    workflow.write_text(w, encoding="utf-8")
    print("Phase 1/2 overlay applied. Review git diff before committing.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
