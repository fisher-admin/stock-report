import importlib.util
import sys
import types
import unittest
import tushare
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]


class SharedDataClientTests(unittest.TestCase):
    def test_scheduled_consumers_use_configured_provider(self):
        paths = [
            ("skills/stock-system-orchestrator/scripts/generate_midday_analysis.py", "_tushare_pro"),
            ("skills/stock-system-orchestrator/scripts/generate_latest_states.py", "_ls_tushare_pro"),
            ("skills/stock-system-orchestrator/scripts/ingest_minute_data.py", "get_pro"),
            ("skills/stock-system-orchestrator/scripts/ingest_auction_data.py", "get_pro"),
            ("skills/stock-system-orchestrator/scripts/generate_s3_tracking.py", "get_pro"),
            ("factor_factory/contracts/tushare_client.py", "get_pro"),
        ]
        for relative, function in paths:
            with self.subTest(consumer=relative):
                client = object()
                pipeline = types.ModuleType("pipeline")
                pipeline.init_tushare = Mock(return_value=client)
                spec = importlib.util.spec_from_file_location("provider_consumer_test", ROOT / relative)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with patch.dict(sys.modules, {"pipeline": pipeline}):
                    self.assertIs(getattr(module, function)(), client)
                    pipeline.init_tushare.assert_called_once()


if __name__ == "__main__":
    unittest.main()
