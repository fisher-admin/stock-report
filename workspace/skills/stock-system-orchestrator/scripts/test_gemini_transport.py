"""Gemini API transport of the stock AI generator (2026-11-23): key lookup, cost, chain order, Copilot fallback."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_o2c_ai as g  # noqa: E402


class GeminiTransportTests(unittest.TestCase):
    def test_key_from_hermes_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("OTHER=1\nexport GEMINI_API_KEY='k-123'\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HERMES_HOME": d, "GEMINI_API_KEY": ""}):
                self.assertEqual(g.gemini_api_key({}), "k-123")

    def test_cost_counts_thoughts_as_output(self):
        usage = {"promptTokenCount": 1_000_000, "candidatesTokenCount": 500_000, "thoughtsTokenCount": 500_000}
        self.assertAlmostEqual(g.gemini_cost_usd(usage), 0.75 + 3.75)

    def test_chain_order_and_default(self):
        env = {"STOCK_AI_TRANSPORT": "gemini_api"}
        chain = g.effective_model_chain(env)
        self.assertEqual(chain[0], "gemini/gemini-3.8-flash")
        self.assertIn("claude-haiku-5.5", chain)
        with mock.patch.dict(os.environ, {"STOCK_AI_TRANSPORT": ""}):
            self.assertEqual(g.ai_transport({}), "copilot_sdk")

    def test_falls_back_to_copilot_when_gemini_fails(self):
        env = {"STOCK_AI_TRANSPORT": "gemini_api", "GEMINI_API_KEY": "k"}
        bad = {"ok": False, "model": "gemini/gemini-3.8-flash", "content": "", "error": "HTTP 503"}
        good = {"ok": True, "model": "claude-haiku-5.5", "content": "{}", "error": None}
        with mock.patch.object(g, "_gemini_complete_once", return_value=bad) as gem, \
                mock.patch.object(g, "_sdk_complete_once", return_value=good) as sdk:
            res = g.llm_complete("p", env, [], max_tokens=10, timeout=5)
        self.assertEqual(res["model"], "claude-haiku-5.5")
        gem.assert_called_once()
        sdk.assert_called_once()

    def test_gemini_success_skips_copilot(self):
        env = {"STOCK_AI_TRANSPORT": "gemini_api", "GEMINI_API_KEY": "k"}
        ok = {"ok": True, "model": "gemini/gemini-3.8-flash", "content": "{}", "error": None, "cost_usd": 0.004}
        with mock.patch.object(g, "_gemini_complete_once", return_value=ok), \
                mock.patch.object(g, "_sdk_complete_once") as sdk:
            res = g.llm_complete("p", env, [], max_tokens=10, timeout=5)
        self.assertEqual(res["model"], "gemini/gemini-3.8-flash")
        sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
