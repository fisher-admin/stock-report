"""Real-data cached/uncached parity check without changing experiment artifacts."""
import json
import sys
import time
from pathlib import Path

workspace = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(workspace / "skills/stock-system-orchestrator/scripts"))
import prebreakout_factory as factory

dates = ["20260902", "20260903", "20260904", "20260907"]
exp = next(e for e in factory.load_experiments() if e.get("status", "active") == "active")
industries = factory.shadow.load_industry_map()
names = factory.shadow.load_name_map()
control = factory.backtest_real_production(dates, cost=0.26)
start = time.monotonic()
expected = factory.backtest_experiment(exp, dates, industries, names, control)
uncached = time.monotonic() - start
cache = {}
start = time.monotonic()
first = factory.backtest_experiment(exp, dates, industries, names, control, score_cache=cache)
cold = time.monotonic() - start
start = time.monotonic()
second = factory.backtest_experiment(exp, dates, industries, names, control, score_cache=cache)
warm = time.monotonic() - start
assert expected == first == second, "cached evaluation changed the result"
print(json.dumps({"uncached_seconds": uncached, "cold_seconds": cold,
                  "warm_seconds": warm, "cached_dates": len(cache),
                  "exact_result_match": True, "days": len(dates) - 1}))
