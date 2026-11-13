import copy
import inspect
import unittest
from unittest import mock

import prebreakout_factory as factory


class FactoryRunCacheTests(unittest.TestCase):
    def test_same_day_scoring_is_reused_without_changing_results(self):
        self.assertIn("score_cache", inspect.signature(factory.backtest_experiment).parameters)
        scored = [{"ts_code": f"{i:06d}.SZ", "score": 80, "sub_scores": {}}
                  for i in range(60)]
        original = copy.deepcopy(scored)
        dates = ["20260903", "20260904", "20260907"]
        exp = {"id": "test", "gates": {"market_gate": False}}
        control = {"status": "unavailable", "summary": {}}
        with mock.patch.object(factory, "score_universe", return_value=scored) as score, \
             mock.patch.object(factory.shadow, "market_mom20", return_value=(0, "test")), \
             mock.patch.object(factory, "t1_o2c_map", return_value={r["ts_code"]: 1.0 for r in scored}):
            expected = factory.backtest_experiment(exp, dates, {}, {}, control)
            score.reset_mock()
            cache = {}
            first = factory.backtest_experiment(exp, dates, {}, {}, control, score_cache=cache)
            second = factory.backtest_experiment(exp, dates, {}, {}, control, score_cache=cache)
            self.assertEqual(score.call_count, 2)
            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertEqual(scored, original)
