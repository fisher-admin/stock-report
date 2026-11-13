"""Regression tests for the stock AI Copilot OAuth route."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from generate_o2c_ai import (
    has_model_credentials,
    llm_complete,
    model_chain,
    prepare_runtime_env,
)


class GenerateO2cAiCopilotTests(unittest.TestCase):
    @mock.patch("generate_o2c_ai.read_hermes_copilot_tokens", return_value=("fake_copilot_token", "https://api.githubcopilot.com"))
    @mock.patch("generate_o2c_ai.read_hermes_xai_token", return_value="")
    def test_prepare_runtime_env_resolves_copilot_oauth_when_xai_missing(self, _mock_xai, _mock_copilot) -> None:
        env = {
            "LITELLM_MODEL": "copilot/gemini-3.7-flash",
            "LITELLM_FALLBACK_MODELS": "xai/grok-4.6",
        }

        runtime = prepare_runtime_env(env)

        self.assertEqual(runtime.get("COPILOT_API_TOKEN"), "fake_copilot_token")
        self.assertTrue(runtime.get("COPILOT_API_BASE", "").startswith("https://"))
        self.assertTrue(has_model_credentials(runtime, model_chain(runtime)))

    @mock.patch("litellm.completion")
    def test_llm_complete_delegates_to_gateway(self, mock_litellm) -> None:
        mock_choice = mock.MagicMock()
        mock_choice.message.content = "STOCK_COPILOT_OAUTH_TEST"
        mock_resp = mock.MagicMock()
        mock_resp.choices = [mock_choice]
        mock_litellm.return_value = mock_resp

        with mock.patch("stock_model_gateway.read_hermes_copilot_tokens", return_value=("mock-token", "https://api.githubcopilot.com")):
            result = llm_complete(
                "只回复 STOCK_COPILOT_OAUTH_TEST",
                {},
                ["copilot/gemini-3.7-flash"],
                max_tokens=512,
                timeout=60,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "STOCK_COPILOT_OAUTH_TEST")
        self.assertTrue(mock_litellm.called)


if __name__ == "__main__":
    unittest.main()
