import os
import unittest
from pathlib import Path

import stock_message_formatter as fmt


class StockMessageFormatterTests(unittest.TestCase):
    def test_midday_wrapper_references_existing_contract(self):
        hermes_home = Path(os.environ.get("HERMES_HOME", "/Users/fisher/.hermes"))
        wrapper = hermes_home / "scripts/stock/midday_analysis.sh"
        text = wrapper.read_text(encoding="utf-8")
        self.assertIn("generate_midday_analysis.py", text)
        self.assertIn("midday_analysis_latest.json", text)
        self.assertNotIn("publish_midday_analysis.py", text)
        self.assertNotIn("midday_market_latest.json", text)

    def test_format_morning_brief_chinese(self):
        brief = {
            "trade_date": "20260820",
            "regime": "震荡上行",
            "risk_score": "25",
            "focus_sectors": ["电子", "计算机"],
            "ai_summary": "早盘情绪偏多，科技成长领跑。",
        }
        verdict = {
            "final_action": {"label": "观察"},
            "pipeline_status": {"execution_authority": "observe_only_no_auto_order"},
        }
        msg = fmt.format_morning_brief(brief, verdict)
        self.assertIn("🌅 【A股市场晨判】", msg)
        self.assertIn("震荡上行", msg)
        self.assertIn("电子、计算机", msg)
        self.assertIn("observe_only_no_auto_order", msg)

    def test_format_midday_analysis_chinese(self):
        analysis = {
            "trade_date": "20260820",
            "market_view_midday": {"regime": "分化盘整", "risk_score": "35"},
            "ai_summary": "午盘量能平稳，注意板块轮动。",
        }
        verdict = {"final_action": {"label": "观察"}}
        msg = fmt.format_midday_analysis(analysis, verdict)
        self.assertIn("☀️ 【A股午盘分析】", msg)
        self.assertIn("分化盘整", msg)

    def test_format_watchdog_message_silent_when_healthy(self):
        msg = fmt.format_watchdog_message({"healthy": True})
        self.assertEqual(msg, "")

    def test_format_watchdog_message_chinese_alert(self):
        report = {
            "healthy": False,
            "expected_trading_day": "20260820",
            "stale_findings": ["数据库最新行情日落后 1 个交易日"],
            "last_run": {"ok": False, "selection_ok": False},
        }
        msg = fmt.format_watchdog_message(report)
        self.assertIn("⚠️ 【选股数据可能停更】", msg)
        self.assertIn("20260820", msg)
        self.assertIn("最近一次选股主线没有跑完", msg)


if __name__ == "__main__":
    unittest.main()
