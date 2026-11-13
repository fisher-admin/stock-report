#!/usr/bin/env python3
"""Public maintenance mode: switch, stock-level scrub, publisher gate integration."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import maintenance_mode as mm  # noqa: E402
from test_final_publication_gate import Fixture, batch_docs  # noqa: E402

ON = {"enabled": True, "source": "test", "title": mm.DEFAULT_TITLE, "title_en": mm.DEFAULT_TITLE_EN,
      "message": mm.DEFAULT_MESSAGE, "since": "2026-10-09T22:00:00+08:00"}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


class ResolveStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.flag = Path(self.tmp.name) / "config" / "stock_maintenance_mode.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_flag(self, payload):
        self.flag.parent.mkdir(parents=True, exist_ok=True)
        self.flag.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    def test_default_is_off(self):
        state = mm.resolve_state(env={}, flag_file=self.flag)
        self.assertEqual((state["enabled"], state["source"]), (False, "default"))

    def test_file_enables_with_custom_text(self):
        self.write_flag({"enabled": True, "message": "定制说明", "since": "2026-10-09"})
        state = mm.resolve_state(env={}, flag_file=self.flag)
        self.assertEqual((state["enabled"], state["source"], state["message"]), (True, "file", "定制说明"))
        self.assertEqual(state["title"], mm.DEFAULT_TITLE)

    def test_env_overrides_file_both_ways(self):
        self.write_flag({"enabled": True})
        self.assertFalse(mm.resolve_state(env={mm.FLAG_ENV: "false"}, flag_file=self.flag)["enabled"])
        self.write_flag({"enabled": False})
        state = mm.resolve_state(env={mm.FLAG_ENV: "TRUE"}, flag_file=self.flag)
        self.assertEqual((state["enabled"], state["source"]), (True, "env"))

    def test_malformed_configuration_fails_closed(self):
        with self.assertRaises(mm.MaintenanceConfigError):
            mm.resolve_state(env={mm.FLAG_ENV: "maybe"}, flag_file=self.flag)
        self.write_flag("{not json")
        with self.assertRaises(mm.MaintenanceConfigError):
            mm.resolve_state(env={}, flag_file=self.flag)
        self.write_flag({"enabled": "yes"})
        with self.assertRaises(mm.MaintenanceConfigError):
            mm.resolve_state(env={}, flag_file=self.flag)


class ScrubTests(unittest.TestCase):
    def test_stock_rows_code_lists_and_code_keys_are_withheld(self):
        names: set[str] = set()
        payload = {
            "candidates": [{"code": "601860", "name": "紫金银行", "score": 83}],
            "hot": [{"ts_code": "000651.SZ", "stock_name": "格力电器"}],
            "overlap_codes": ["002863.SZ"],
            "bare_codes": ["601985", "300129"],
            "months": ["202609", "202610"],
            "by_code": {"600512.SH": {"w": 1}, "summary": 2},
            "sector": {"industry": "银行", "represent_stock_name": "紫金银行", "represent_stock_code": "601860"},
            "regime": "中性",
        }
        out, removed = mm.scrub(payload, names)
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["hot"], [])
        self.assertEqual(out["overlap_codes"], [])
        self.assertEqual(out["bare_codes"], [])
        self.assertEqual(out["months"], ["202609", "202610"])
        self.assertEqual(out["by_code"], {"summary": 2})
        self.assertEqual(out["sector"], {"industry": "银行"})
        self.assertEqual(out["regime"], "中性")
        self.assertEqual(removed, 8)  # 1 row + 1 row + 1 code + 2 bare codes + 1 key + 2 stock fields
        self.assertEqual(names, {"紫金银行", "格力电器"})
        self.assertEqual(mm.residual_stock_refs(out), 0)

    def test_nested_non_stock_names_are_not_collected(self):
        names: set[str] = set()
        mm.scrub({"rows": [{"code": "601860", "name": "紫金银行", "factor": {"name": "macd"}}]}, names)
        self.assertEqual(names, {"紫金银行"})

    def test_free_text_naming_a_withheld_stock_is_redacted(self):
        out, hits = mm.redact_names({"advice": "再从 中科环保、格力电器 中挑选", "summary": "市场中性"}, {"格力电器"})
        self.assertEqual(out["advice"], mm.REDACTED_TEXT)
        self.assertEqual(out["summary"], "市场中性")
        self.assertEqual(hits, 1)

    def test_suffixed_code_inside_text_counts_as_residual(self):
        self.assertEqual(mm.residual_stock_refs({"note": "关注 000651.SZ"}), 1)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        latest = self.repo / "data" / "latest"
        latest.mkdir(parents=True)
        files = {
            "run_manifest.json": {"run_id": "stock-x", "candidate_role_counts": {"main": 1, "watch": 19}},
            "candidate_state.json": {"latest_trade_date": "20261009", "role_counts": {"main": 1, "watch": 19},
                                     "candidates": [{"code": "601860", "name": "紫金银行"}]},
            "system_verdict.json": {"final_action": {"action": "execute", "label": "执行"}},
            "dual_track_state.json": {"honesty_banner": "流程正常不代表策略有效",
                                      "short_track_strategies": [{"strategy_id": "a", "candidates": [{"ts_code": "601860.SH"}]}]},
            "market_state.json": {"midday": {"advice": "优先关注紫金银行"}, "regime": "中性"},
            "execution_state.json": {"total_execution_count": 20, "watch_count": 20, "strategy_counts": {"a": 20},
                                     "executions": [{"code": "601860"}], "sample_trade_days": 36},
        }
        for name, payload in files.items():
            (latest / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, name):
        return json.loads((self.repo / "data" / "latest" / name).read_text(encoding="utf-8"))

    def test_disabled_changes_nothing(self):
        before = self.load("candidate_state.json")
        summary = mm.apply(self.repo, {**ON, "enabled": False})
        self.assertEqual(summary["enabled"], False)
        self.assertEqual(self.load("candidate_state.json"), before)

    def test_enabled_withholds_picks_and_stamps_calibration_status(self):
        summary = mm.apply(self.repo, ON, applied_at="2026-10-09T22:00:00+08:00")
        self.assertEqual(summary["residual"], {})
        cand = self.load("candidate_state.json")
        self.assertEqual(cand["candidates"], [])
        self.assertEqual(cand["role_counts"], {"main": 0, "watch": 0})
        self.assertTrue(mm.is_active(cand))
        manifest = self.load("run_manifest.json")
        self.assertTrue(mm.is_active(manifest))
        self.assertEqual(manifest["candidate_role_counts"], {"main": 0, "watch": 0})
        self.assertEqual(manifest["maintenance"]["title"], mm.DEFAULT_TITLE)
        action = self.load("system_verdict.json")["final_action"]
        self.assertEqual((action["action"], action["maintenance"], action["label"]), ("observe_only", True, mm.DEFAULT_TITLE))
        dual = self.load("dual_track_state.json")
        self.assertEqual(dual["short_track_strategies"][0]["candidates"], [])
        self.assertIn(mm.DEFAULT_TITLE, dual["honesty_banner"])
        market = self.load("market_state.json")
        self.assertEqual(market["midday"]["advice"], mm.REDACTED_TEXT)
        self.assertEqual(market["regime"], "中性")
        self.assertNotIn("maintenance", market)
        execution = self.load("execution_state.json")
        self.assertEqual((execution["total_execution_count"], execution["watch_count"]), (0, 0))
        self.assertEqual(execution["strategy_counts"], {"a": 0})
        self.assertEqual(execution["executions"], [])
        self.assertEqual(execution["sample_trade_days"], 36)  # non-pick statistics are kept

    def test_unscrubbable_residual_aborts_without_writing_anything(self):
        (self.repo / "data" / "picks.csv").write_text("ts_code\n601860.SH\n", encoding="utf-8")
        before = self.load("candidate_state.json")
        with self.assertRaises(mm.MaintenanceScrubError):
            mm.apply(self.repo, ON)
        self.assertEqual(self.load("candidate_state.json"), before)

    def test_ignored_untracked_files_are_out_of_scope(self):
        (self.repo / ".gitignore").write_text("data/local.json\n", encoding="utf-8")
        (self.repo / "data" / "local.json").write_text('{"x": "601860.SH"}', encoding="utf-8")
        self.assertNotIn("data/local.json", mm.publishable_data_files(self.repo))
        mm.apply(self.repo, ON)

    def test_cli_enable_status_apply_disable(self):
        flag = self.repo / "flag.json"

        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.dict("os.environ", {}, clear=False) as env:
                env.pop(mm.FLAG_ENV, None)
                code = mm.main(["--flag-file", str(flag), *args])
            return code, json.loads(out.getvalue())

        self.assertEqual(run("enable", "--message", "校准中")[1]["enabled"], True)
        code, summary = run("apply", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        self.assertEqual(self.load("candidate_state.json")["candidates"], [])
        self.assertEqual(self.load("run_manifest.json")["maintenance"]["message"], "校准中")
        self.assertEqual(run("disable")[1]["enabled"], False)
        self.assertEqual(json.loads(flag.read_text())["enabled"], False)


class GateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.write(batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930"))
        self.f.commit("previous published batch")
        self.f.receipt("stock-20260930T113015Z", "20260930")

    def tearDown(self):
        self.f.close()

    @staticmethod
    def maintenance(docs, codes_withheld=True):
        marker = {"enabled": True, "title": mm.DEFAULT_TITLE}
        docs["run_manifest.json"]["maintenance"] = marker
        docs["candidate_state.json"]["maintenance"] = marker
        if codes_withheld:
            docs["candidate_state.json"]["candidates"] = []
        return docs

    def test_new_batch_in_maintenance_passes_with_withheld_list(self):
        self.f.write(self.maintenance(batch_docs()))
        result = self.f.evaluate(expected_run_id="stock-20261008T113000Z", expected_trade_date="20261008")
        self.assertTrue(result["passed"], result["blockers"])
        self.assertTrue(result["checks"]["maintenance"])

    def test_empty_list_without_maintenance_still_blocks(self):
        self.f.write(batch_docs(codes=()))
        self.assertIn("CANDIDATE_LIST_EMPTY", self.f.evaluate()["blockers"])

    def test_maintenance_with_visible_candidates_blocks(self):
        self.f.write(self.maintenance(batch_docs(), codes_withheld=False))
        self.assertIn("MAINTENANCE_CANDIDATES_NOT_WITHHELD", self.f.evaluate()["blockers"])

    def test_inconsistent_markers_block(self):
        docs = self.maintenance(batch_docs())
        del docs["candidate_state.json"]["maintenance"]
        self.f.write(docs)
        self.assertIn("MAINTENANCE_MARKERS_INCONSISTENT", self.f.evaluate()["blockers"])

    def test_refresh_entering_and_leaving_maintenance(self):
        entering = self.maintenance(batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930"))
        self.f.write(entering)
        result = self.f.evaluate()
        self.assertEqual(result["mode"], "refresh")
        self.assertTrue(result["passed"], result["blockers"])
        self.f.commit("maintenance published")
        self.f.write(batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930"))
        result = self.f.evaluate()
        self.assertTrue(result["passed"], result["blockers"])
        self.assertEqual(result["checks"]["maintenance_transition"], {"head": True, "current": False})

    def test_halt_still_blocks_new_batch_in_maintenance(self):
        self.f.write(self.maintenance(batch_docs(action="halt")))
        self.assertIn("VERDICT_HALT", self.f.evaluate()["blockers"])


if __name__ == "__main__":
    unittest.main()
