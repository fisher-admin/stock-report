"""Read-only profiling: cached research panels; does not publish or select a champion."""
import cProfile
import pstats
import sys
import time
from pathlib import Path

scripts = Path(__file__).resolve().parents[3] / "skills/stock-system-orchestrator/scripts"
sys.path.insert(0, str(scripts))
import pandas as pd
import prebreakout_factory_search_daily as daily

paths = sorted(daily.s1.CACHE_SCORES_DIR.glob("subscores_*.parquet"))
paths = [p for p in paths if p.stem[-8:] <= "20260907"][-61:]
panel = {p.stem[-8:]: pd.read_parquet(p) for p in paths}
dates = sorted(panel)
pairs = list(zip(dates[:-1], dates[1:]))
returns = {nd: daily.s1.o2c_for_date(nd) for _, nd in pairs}
industries = daily.s1.industry_map()
caps = {}
prof = cProfile.Profile()
start = time.monotonic()
prof.enable()
for _ in range(5):
    result = daily.evaluate(panel, pairs, daily.PROD_W, None, daily.base_gates(), returns, industries, caps)
prof.disable()
print({"seconds": time.monotonic() - start, "days": result["days"], "panels": len(panel)})
pstats.Stats(prof).sort_stats("cumulative").print_stats(22)
