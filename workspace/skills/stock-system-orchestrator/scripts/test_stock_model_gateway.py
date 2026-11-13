import unittest
from unittest import mock

import stock_model_gateway as gw


class StockModelGatewayTests(unittest.TestCase):
    @mock.patch("stock_model_gateway.read_hermes_copilot_tokens", return_value=("fake-copilot-token", "https://api.githubcopilot.com"))
    @mock.patch("stock_model_gateway.read_hermes_xai_token", return_value="fake-xai-token")
    def test_primary_copilot_succeeds(self, _mock_xai, _mock_copilot):
        def fake_llm(prompt, model, **kwargs):
            if "copilot" in model:
                return "copilot response"
            raise RuntimeError("should not reach fallback")

        res = gw.call_model_gateway("hello", ["copilot/gemini-3.7-flash", "xai/grok-4.6"], llm_func=fake_llm)
        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], "copilot/gemini-3.7-flash")
        self.assertEqual(res["content"], "copilot response")

    @mock.patch("stock_model_gateway.read_hermes_copilot_tokens", return_value=("fake-copilot-token", "https://api.githubcopilot.com"))
    @mock.patch("stock_model_gateway.read_hermes_xai_token", return_value="fake-xai-token")
    def test_primary_fails_falls_back_to_xai(self, _mock_xai, _mock_copilot):
        def fake_llm(prompt, model, **kwargs):
            if "copilot" in model:
                raise ConnectionError("Copilot gateway timeout")
            if "xai" in model:
                return "xai fallback response"
            raise RuntimeError("unknown model")

        res = gw.call_model_gateway("hello", ["copilot/gemini-3.7-flash", "xai/grok-4.6"], llm_func=fake_llm)
        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], "xai/grok-4.6")
        self.assertEqual(res["content"], "xai fallback response")

    @mock.patch("stock_model_gateway.read_hermes_copilot_tokens", return_value=("fake-copilot-token", "https://api.githubcopilot.com"))
    @mock.patch("stock_model_gateway.read_hermes_xai_token", return_value="fake-xai-token")
    def test_all_models_fail_reports_clean_error(self, _mock_xai, _mock_copilot):
        def fake_llm(prompt, model, **kwargs):
            raise TimeoutError(f"{model} timed out")

        res = gw.call_model_gateway("hello", ["copilot/gemini-3.7-flash", "xai/grok-4.6"], llm_func=fake_llm)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["model"])
        self.assertIn("timed out", res["error"])


if __name__ == "__main__":
    unittest.main()
