import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_business_status as dbs  # noqa: E402

NOW = datetime(2026, 10, 8, 20, 46)


class DailyBusinessStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Path(self.tmp.name)
        (self.h / "trading_calendar.json").write_text(json.dumps({
            "source": "rds_trade_cal", "generated_at": "2026-10-08 19:30:00", "from": "20260801",
            "to": "20261129", "open_dates": ["20260930", "20261008"]}))
        self.jobs = self.h / "jobs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, name, payload):
        path = self.h / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False))

    def status(self, rc=0):
        return dbs.build_status("20261008", health_dir=self.h, hermes_jobs=self.jobs, closure_rc=rc,
                                calendar_path=self.h / "trading_calendar.json", now=NOW)

    def test_upstream_failure_is_reported_not_masked(self):
        self.w("orchestrator_run.json", {"generated_at": "2026-10-08 19:30:07", "run_id": "r", "trade_date": "20261008",
                                         "selection_ok": False, "status": "failed", "failed_stage": "dataPreparation",
                                         "stages": [{"stage": "dataPreparation", "ok": False}]})
        self.w("publication_validation.json", {"checked_at": "2026-10-08T19:42:00+08:00", "passed": False,
                                               "blockers": ["VERDICT_HALT"]})
        text = dbs.render(self.status())
        self.assertIn("选股：❌ 失败（阶段 dataPreparation", text)
        self.assertIn("被最终发布检查拦截：VERDICT_HALT", text)
        self.assertIn("今日未播报", text)
        self.assertIn("收盘核算：✅ 完成", text)

    def test_success_requires_remote_confirmed_receipt_and_hermes_record(self):
        self.w("orchestrator_run.json", {"generated_at": "2026-10-08 19:30:07", "run_id": "r", "trade_date": "20261008",
                                         "selection_ok": True, "status": "succeeded", "stages": []})
        self.w("publication_validation.json", {"checked_at": "2026-10-08T19:42:00+08:00", "passed": True,
                                               "mode": "new_batch", "pushed_sha": "abc123", "push_outcome": "pushed"})
        self.w("deployment_receipt.json", {"remote_confirmed": True, "local_sha": "abc123", "remote_sha": "abc123"})
        self.w("delivery_receipt.json", {"status": "handed_to_hermes_stdout", "handed_at": "2026-10-08T20:00:03+08:00",
                                         "recorded_at": "2026-10-08T20:00:03+08:00", "trade_date": "20261008"})
        self.jobs.write_text(json.dumps({"jobs": [{"name": dbs.BROADCAST_JOB_NAME, "last_run_at": "2026-10-08T20:00:05+08:00",
                                                    "last_status": "ok", "last_delivery_error": None}]}))
        text = dbs.render(self.status())
        self.assertIn("已推送并远端确认 abc123", text)
        self.assertIn("Hermes 记录无投递错误（无消息ID）", text)
        self.jobs.write_text(json.dumps({"jobs": [{"name": dbs.BROADCAST_JOB_NAME, "last_run_at": "2026-10-08T20:00:05+08:00",
                                                    "last_status": "ok", "last_delivery_error": "discord timeout"}]}))
        self.assertIn("Hermes 记录异常", dbs.render(self.status()))

    def test_receipt_mismatch_is_not_reported_as_confirmed(self):
        self.w("publication_validation.json", {"checked_at": "2026-10-08T19:42:00+08:00", "passed": True,
                                               "mode": "new_batch", "pushed_sha": "abc123"})
        self.w("deployment_receipt.json", {"remote_confirmed": True, "local_sha": "zzz", "remote_sha": "zzz"})
        self.assertIn("远端回执不一致", dbs.render(self.status()))

    def test_closure_failure_and_missing_run(self):
        text = dbs.render(self.status(rc=1))
        self.assertIn("今日没有主管线运行记录", text)
        self.assertIn("收盘核算：⚠️ 异常（退出码 1）", text)

    def test_cli_writes_durable_record(self):
        proc = subprocess.run([sys.executable, str(Path(dbs.__file__)), "--date", "20261008", "--closure-rc", "0",
                               "--health-dir", str(self.h), "--hermes-jobs", str(self.jobs)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.h / "daily_business_status/20261008.json").is_file())

    def test_zero_paid_retired_mode_renders_cleanly(self):
        ws = self.h / "ws"
        h = ws / "stock_data/03-working/health"
        h.mkdir(parents=True, exist_ok=True)
        (ws / "config").mkdir(parents=True, exist_ok=True)
        (ws / "config/v43_retirement.json").write_text(json.dumps({"retired": True}))
        (h / "trading_calendar.json").write_text(json.dumps({
            "source": "selfheal", "generated_at": "2026-11-20 19:30:00", "from": "20260801",
            "to": "20270119", "open_dates": ["20261119", "20261120"]}))
        (h / "zero_paid_stage1.json").write_text(json.dumps({
            "target_trade_date": "20261120", "ok": True, "seconds": 120.5,
            "steps": {"daily": {"rows": 5572}, "pit_snapshot": {"rows": {"universe": 5924}}}}))
        (h / "zero_paid_daily").mkdir(parents=True, exist_ok=True)
        (h / "zero_paid_daily/run_20261120.json").write_text(json.dumps({
            "date": "20261120", "problems": [], "reconcile": {"verdict": "ok"},
            "prices": {"verdict": "ok"}, "em_ann": {"status": "ok"}}))
        (h / "ew6_publish").mkdir(parents=True, exist_ok=True)
        (h / "ew6_publish/publication_validation.json").write_text(json.dumps({
            "passed": True, "batch": "ew6_20261120", "mode": "v43_retired_ew6",
            "pushed_sha": "55d733bf28f8d2ccd498d1424966b39a9c7e4241", "pushed_at": "2026-11-20T23:45:28+08:00"}))
        (h / "deployment_receipt.json").write_text(json.dumps({
            "remote_confirmed": True, "local_sha": "55d733bf28f8d2ccd498d1424966b39a9c7e4241"}))
        fs_dir = ws / "factor_factory/data/forward_shadow"
        fs_dir.mkdir(parents=True, exist_ok=True)
        (fs_dir / "state.json").write_text(json.dumps({
            "signal_date": "20261120", "status": "recorded", "is_rebalance": False,
            "universe": 2248, "composite_coverage": 0.9987}))
        jobs = self.h / "jobs_zp.json"
        jobs.write_text(json.dumps({"jobs": [{
            "name": "股票系统-多账本前向影子multi_v1·晚间",
            "last_run_at": "2026-11-20T23:50:16+08:00", "last_status": "ok", "last_error": None}]}))
        st = dbs.build_status("20261120", health_dir=h, hermes_jobs=jobs, closure_rc=0,
                              calendar_path=h / "trading_calendar.json", now=datetime(2026, 11, 20, 23, 55))
        self.assertEqual(st["mode"], "zero_paid_v43_retired")
        text = dbs.render(st)
        self.assertIn("零付费模式", text)
        self.assertIn("盘后准备(stage1)：✅ 完成", text)
        self.assertIn("零付费核对(A1/A4/A5)：✅ 全部通过", text)
        self.assertIn("EW6 影子记账：✅ 已记录", text)
        self.assertIn("页面发布(#/today EW6)：✅ 已推送并远端确认 55d733bf28f8", text)
        self.assertIn("多账本影子(multi_v1)：✅ 晚间已就绪", text)
        self.assertNotIn("今日没有主管线运行记录", text)
        self.assertNotIn("远端回执不一致", text)

    # ---- zero-paid multi_v1 / forward_shadow_v2 (2026-11-26) ----
    def _zero_paid_ws(self, multi=None, v2=None, job=None, gaps=None):
        ws = self.h / "ws2"
        h = ws / "stock_data/03-working/health"
        h.mkdir(parents=True, exist_ok=True)
        (ws / "config").mkdir(parents=True, exist_ok=True)
        (ws / "config/v43_retirement.json").write_text(json.dumps({"retired": True}))
        (h / "trading_calendar.json").write_text(json.dumps({
            "source": "selfheal", "generated_at": "2026-11-26 08:30:00", "from": "20260801",
            "to": "20270119", "open_dates": ["20261124", "20261125", "20261126"]}))
        fs = ws / "factor_factory/data/forward_shadow"
        if multi is not None:
            (fs / "multi_v1").mkdir(parents=True, exist_ok=True)
            (fs / "multi_v1/state.json").write_text(json.dumps(multi))
        if gaps is not None:
            (fs / "multi_v1").mkdir(parents=True, exist_ok=True)
            (fs / "multi_v1/gaps.jsonl").write_text("\n".join(json.dumps(g) for g in gaps) + "\n")
        if v2 is not None:
            (fs / "v2").mkdir(parents=True, exist_ok=True)
            (fs / "v2/state.json").write_text(json.dumps(v2))
        jobs = self.h / "jobs_zp2.json"
        jobs.write_text(json.dumps({"jobs": [job] if job else []}))
        return h, jobs

    def _zp_text(self, h, jobs, date="20261126"):
        st = dbs.build_status(date, health_dir=h, hermes_jobs=jobs, closure_rc=0,
                              calendar_path=h / "trading_calendar.json", now=datetime(2026, 11, 26, 20, 0))
        self.assertEqual(st["mode"], "zero_paid_v43_retired")
        return st, dbs.render(st)

    def test_multi_v1_decided_prior_session_is_booked_not_unrun(self):
        h, jobs = self._zero_paid_ws(multi={"signal_date": "20261125", "status": "decided", "official": True})
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["multi_v1"]["state"], "decided_prior_session")
        self.assertIn("多账本影子(multi_v1)：✅ 已正式入账", text)
        self.assertNotIn("多账本影子(multi_v1)：— 今日尚未运行", text)

    def test_multi_v1_decided_same_day_is_booked(self):
        h, jobs = self._zero_paid_ws(multi={"signal_date": "20261125", "status": "decided"})
        st, text = self._zp_text(h, jobs, date="20261125")
        self.assertEqual(st["multi_v1"]["state"], "decided")
        self.assertIn("多账本影子(multi_v1)：✅ 已正式入账", text)

    def test_multi_v1_older_signal_is_still_not_run(self):
        h, jobs = self._zero_paid_ws(multi={"signal_date": "20261124", "status": "decided"})
        _, text = self._zp_text(h, jobs)
        self.assertIn("多账本影子(multi_v1)：— 今日尚未运行", text)

    def test_multi_v1_failed_job_today_is_not_masked_by_prior_decision(self):
        h, jobs = self._zero_paid_ws(
            multi={"signal_date": "20261125", "status": "decided"},
            job={"name": "股票系统-多账本前向影子multi_v1·晚间", "last_run_at": "2026-11-26T19:50:00+08:00",
                 "last_status": "error", "last_error": "boom"})
        _, text = self._zp_text(h, jobs)
        self.assertIn("多账本影子(multi_v1)：❌ 失败（boom）", text)

    def test_forward_shadow_v2_absent_renders_nothing(self):
        h, jobs = self._zero_paid_ws(multi={"signal_date": "20261125", "status": "decided"})
        st, text = self._zp_text(h, jobs)
        self.assertIsNone(st["forward_shadow_v2"])
        self.assertNotIn("forward_shadow_v2", text)

    def test_forward_shadow_v2_contract_without_state_renders_not_run_yet(self):
        h, jobs = self._zero_paid_ws(multi={"signal_date": "20261125", "status": "decided"})
        c = self.h / "ws2/factor_factory/contracts/forward_shadow_v2.json"
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_text('{"spec_id": "forward_shadow_v2"}')
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"], {"state": "not_run_yet"})
        self.assertIn("前向影子 v2(forward_shadow_v2)：— 尚未首跑（等待 23:40）", text)

    def test_forward_shadow_v2_healthy(self):
        h, jobs = self._zero_paid_ws(v2={"signal_date": "20261125", "status": "recorded",
                                         "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.93,
                                         "ocf_nonfin_cov": 0.90, "product_valid": True})
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "healthy")
        self.assertIn("前向影子 v2(forward_shadow_v2)：✅ 正常（信号日 20261125，状态 recorded）", text)
        self.assertNotIn("⚠️ 降级", text)

    def test_forward_shadow_v2_low_coverage_degrades(self):
        for field in ("qarp_real_dedt_cov", "acc_real_cov", "ocf_nonfin_cov"):
            v2 = {"signal_date": "20261125", "status": "recorded", "qarp_real_dedt_cov": 0.95,
                  "acc_real_cov": 0.95, "ocf_nonfin_cov": 0.90, "product_valid": True}
            v2[field] = 0.42
            h, jobs = self._zero_paid_ws(v2=v2)
            st, text = self._zp_text(h, jobs)
            self.assertEqual(st["forward_shadow_v2"]["state"], "degraded", field)
            self.assertIn("前向影子 v2(forward_shadow_v2)：⚠️ 降级", text)
            self.assertIn(f"{field}=0.42", text)

    def test_forward_shadow_v2_missing_or_out_of_range_cov_and_product_valid_degrade(self):
        # Missing single coverage field (e.g. ocf_nonfin_cov omitted) must degrade, never healthy
        h, jobs = self._zero_paid_ws(v2={"signal_date": "20261125", "status": "recorded",
                                         "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.95})
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertIn("ocf_nonfin_cov=missing", text)

        # Out-of-range (>1.0, <0.0) or bool coverage must degrade
        for bad in (1.2, -0.1, True):
            h, jobs = self._zero_paid_ws(v2={"signal_date": "20261125", "status": "recorded",
                                             "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.95,
                                             "ocf_nonfin_cov": bad, "product_valid": True})
            st, _ = self._zp_text(h, jobs)
            self.assertEqual(st["forward_shadow_v2"]["state"], "degraded", repr(bad))

        # product_valid=False degrades
        h, jobs = self._zero_paid_ws(v2={"signal_date": "20261125", "status": "decided", "official": True,
                                         "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.95,
                                         "ocf_nonfin_cov": 0.90, "product_valid": False})
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertIn("product_valid=false", text)

    def test_forward_shadow_v2_degraded_pillars_reported(self):
        h, jobs = self._zero_paid_ws(v2={"signal_date": "20261125", "status": "recorded",
                                         "qarp_real_dedt_cov": 0.99, "acc_real_cov": 0.99,
                                         "ocf_nonfin_cov": 0.95, "product_valid": True,
                                         "degraded_pillars": ["quality"]})
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertIn("降级支柱 quality", text)


    def test_multi_v1_non_official_via_flag_books_or_gaps(self):
        # 1. official=False
        h, jobs = self._zero_paid_ws(multi={"signal_date": "20261125", "status": "decided", "official": False})
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["multi_v1"]["state"], "decided_non_official")
        self.assertIn("多账本影子(multi_v1)：⚠️ 已入账但含非正式账本（信号日 20261125）", text)
        self.assertNotIn("✅ 已正式入账", text)

        # 2. official=True but one book is False (same-day)
        h, jobs = self._zero_paid_ws(multi={
            "signal_date": "20261125", "status": "decided", "official": True,
            "official_books": {"ew6_retain_15_30": True, "aegis_core7p_10_30": False},
        })
        st, text = self._zp_text(h, jobs, date="20261125")
        self.assertEqual(st["multi_v1"]["state"], "decided_non_official")
        self.assertIn("已入账但含非正式账本（信号日 20261125）", text)
        self.assertIn("aegis_core7p_10_30", text)

        # 3. gaps.jsonl has matching signal_date
        h, jobs = self._zero_paid_ws(
            multi={"signal_date": "20261125", "status": "decided", "official": True},
            gaps=[{"signal_date": "20261125", "reason": "core7p_ann_stale"}],
        )
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["multi_v1"]["state"], "decided_non_official")
        self.assertTrue(st["multi_v1"]["has_gap"])

        # 4. gaps.jsonl only has an older signal_date -> still official
        h, jobs = self._zero_paid_ws(
            multi={"signal_date": "20261125", "status": "decided", "official": True},
            gaps=[{"signal_date": "20261124", "reason": "old_gap"}],
        )
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["multi_v1"]["state"], "decided_prior_session")
        self.assertIn("✅ 已正式入账", text)

    def test_forward_shadow_v2_ocf_missing_cov_non_official_and_stale(self):
        # 1. ocf_nonfin_cov < 0.85 -> degraded
        h, jobs = self._zero_paid_ws(v2={
            "signal_date": "20261125", "status": "decided", "official": True,
            "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.95, "ocf_nonfin_cov": 0.80,
        })
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertIn("ocf_nonfin_cov=0.8", text)

        # 2. status=="decided" with all coverage fields missing -> degraded
        h, jobs = self._zero_paid_ws(v2={"signal_date": "20261125", "status": "decided", "official": True})
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertIn("coverage_missing", text)

        # 3. official=False or official_books has False -> non_official
        h, jobs = self._zero_paid_ws(v2={
            "signal_date": "20261125", "status": "decided", "official": True,
            "official_books": {"core7p_native_10_30": False, "ew6_retain_15_30": True},
            "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.95, "ocf_nonfin_cov": 0.90,
        })
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "non_official")
        self.assertIn("前向影子 v2(forward_shadow_v2)：⚠️ 非正式", text)
        self.assertIn("core7p_native_10_30", text)

        # 4. stale signal_date -> stale
        h, jobs = self._zero_paid_ws(v2={
            "signal_date": "20261124", "status": "decided", "official": True,
            "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.95, "ocf_nonfin_cov": 0.90,
        })
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "stale")
        self.assertIn("前向影子 v2(forward_shadow_v2)：⚠️ 滞后", text)

    def test_forward_shadow_v2_non_boolean_official_books_flags_non_official(self):
        for bad in ("false", None, 0, "yes"):
            h, jobs = self._zero_paid_ws(v2={
                "signal_date": "20261125", "status": "decided", "official": True,
                "official_books": {"core7p_native_10_30": bad, "ew6_retain_15_30": True},
                "qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.95, "ocf_nonfin_cov": 0.90,
                "product_valid": True,
            })
            st, text = self._zp_text(h, jobs)
            self.assertEqual(st["forward_shadow_v2"]["state"], "non_official", repr(bad))
            self.assertEqual(st["forward_shadow_v2"]["non_official_books"], ["core7p_native_10_30"])
            self.assertIn("前向影子 v2(forward_shadow_v2)：⚠️ 非正式", text)

    def test_forward_shadow_v2_gaps_without_state_renders_degraded(self):
        from unittest import mock

        h, jobs = self._zero_paid_ws(multi={"signal_date": "20261125", "status": "decided"})
        ws = self.h / "ws2"
        c = ws / "factor_factory/contracts/forward_shadow_v2.json"
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_text('{"spec_id": "forward_shadow_v2"}', encoding="utf-8")
        gaps_p = ws / "factor_factory/data/forward_shadow/v2/gaps.jsonl"
        gaps_p.parent.mkdir(parents=True, exist_ok=True)

        # Empty / whitespace-only gaps.jsonl -> still not_run_yet
        for empty_body in ("", "  \n\n"):
            gaps_p.write_text(empty_body, encoding="utf-8")
            st, text = self._zp_text(h, jobs)
            self.assertEqual(st["forward_shadow_v2"], {"state": "not_run_yet"})
            self.assertIn("尚未首跑", text)

        # Valid gaps.jsonl -> degraded with status=gap_logged & latest signal_date/reason
        gaps_p.write_text(
            json.dumps({"signal_date": "20261124", "reason": "older_gap"}) + "\n"
            + json.dumps({"signal_date": "20261125", "reason": "forward_shadow_v1 not recorded by T+1 09:30"}) + "\n",
            encoding="utf-8",
        )
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertEqual(st["forward_shadow_v2"]["status"], "gap_logged")
        self.assertEqual(st["forward_shadow_v2"]["signal_date"], "20261125")
        self.assertIn("gap_logged:forward_shadow_v1 not recorded by T+1 09:30", st["forward_shadow_v2"]["problems"])
        self.assertIn("前向影子 v2(forward_shadow_v2)：⚠️ 降级（信号日 20261125，状态 gap_logged；gap_logged:forward_shadow_v1 not recorded by T+1 09:30）", text)

        # Malformed JSONL -> degraded cleanly
        gaps_p.write_text("{not-json\n[1, 2]\n", encoding="utf-8")
        st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertEqual(st["forward_shadow_v2"]["status"], "gap_logged")
        self.assertEqual(st["forward_shadow_v2"]["problems"], ["gaps_jsonl_malformed_without_state"])
        self.assertIn("前向影子 v2(forward_shadow_v2)：⚠️ 降级", text)

        # Unreadable JSONL -> degraded cleanly
        orig_read_text = Path.read_text

        def fail_gaps(path_self, *args, **kwargs):
            if path_self == gaps_p:
                raise PermissionError("denied")
            return orig_read_text(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", fail_gaps):
            st, text = self._zp_text(h, jobs)
        self.assertEqual(st["forward_shadow_v2"]["state"], "degraded")
        self.assertEqual(st["forward_shadow_v2"]["status"], "gap_logged")
        self.assertEqual(st["forward_shadow_v2"]["problems"], ["gaps_jsonl_unreadable:PermissionError"])
        self.assertIn("前向影子 v2(forward_shadow_v2)：⚠️ 降级", text)

    def test_default_hermes_jobs_prefers_hermes_home(self):
        import os
        from unittest import mock
        (self.h / "cron").mkdir()
        (self.h / "cron/jobs.json").write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.h)}):
            self.assertEqual(dbs.default_hermes_jobs(), self.h / "cron/jobs.json")

    def test_previous_session_skips_closed_days(self):
        cal = json.loads((self.h / "trading_calendar.json").read_text(encoding="utf-8"))
        opened = sorted(map(str, cal["open_dates"]))
        self.assertEqual(dbs.previous_session(opened[-1], self.h), opened[-2])
        with self.assertRaises(dbs.exchange_calendar.CalendarError):
            dbs.previous_session(opened[0], self.h)


if __name__ == "__main__":
    unittest.main()
