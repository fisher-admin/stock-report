"""export_cohort_monitor: aggregates-only public contract (no member lists), explicit unavailable fallback."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cohort_monitor as ex  # noqa: E402

SRC = {
    "contract_version": "forward_shadow_cohort_v1", "status": "ok", "spec_hash": "70f0e2009fa1508e", "as_of": "20261028",
    "ledger_cohorts": 3, "official_cohorts": 3, "late_excluded": [], "matured": {"h1": 3, "h5": 2},
    "stats": {"h5": {"cohort_top100": {"cohorts": 2, "mean_excess": 0.004, "nw_t_excess": 1.1}}},
    "verdict": {"status": "insufficient_sample", "cohorts": 2, "required": 120},
    "regime_h5": {}, "rolling20_h5": [], "sleeve_curve": [], "blocked": {}, "impact_stress": {},
    "overlap_top100": [{"signal_date": "20261021", "overlap": 0.8}],
    "windows": [{"signal_date": "20261020", "entry": "20261021", "exit": "20261027", "window": "x",
                 "cohort_top20": 0.01, "cohort_top100": [{"code": "600000.SH"}], "cohort_avoid70": 0.002, "bench_ew": 0.001,
                 "members": ["600000.SH"]}],
    "pending": [{"signal_date": "20261027", "window_start": "20261028", "universe": ["600000.SH"]}],
    "universe": ["600000.SH"], "cohort_top100": [{"code": "600000.SH"}],
}


class ExportCohortMonitorTest(unittest.TestCase):
    def test_aggregates_only(self):
        doc = ex.build(SRC, "now", maintenance=True, spec={"evaluation": {"primary": "p", "caveat": "c"}})
        self.assertEqual(doc["contract_version"], "cohort_monitor_v1")
        self.assertEqual(doc["execution_authority"], "observe_only_no_auto_order")
        self.assertTrue(doc["members_hidden"] and doc["maintenance"])
        text = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn("600000", text)
        for k in ("\"universe\"", "\"members\"", "overlap_top100", "\"cohort_top100\": ["):
            self.assertNotIn(k, text)
        w = doc["windows"][0]
        self.assertEqual(w["window"], "20261020 → 20261021～20261027")
        self.assertIsNone(w["cohort_top100"])
        self.assertEqual(w["cohort_top20"], 0.01)
        self.assertEqual(doc["pending"], [{"signal_date": "20261027", "window_start": "20261028"}])
        self.assertEqual(doc["spec"]["caveat"], "c")

    def test_missing_or_wrong_version_is_unavailable(self):
        self.assertEqual(ex.build(None, "now")["status"], "unavailable")
        self.assertEqual(ex.build({"contract_version": "tier_monitor_v1"}, "now")["status"], "unavailable")

    def test_main_writes_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.json"
            src.write_text(json.dumps(SRC), encoding="utf-8")
            self.assertEqual(ex.main(["--repo", tmp, "--src", str(src)]), 0)
            out = json.loads((Path(tmp) / ex.OUT_REL).read_text(encoding="utf-8"))
            self.assertEqual(out["status"], "ok")


if __name__ == "__main__":
    unittest.main()
