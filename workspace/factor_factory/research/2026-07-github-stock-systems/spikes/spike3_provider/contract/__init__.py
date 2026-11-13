"""Spike-3 provider contract layer (additive, isolated).

Implements the openbb.md §2.0 common skeleton (StrictQuery / StrictRow
extra='forbid' / FetchMeta / TET three-stage / self-check harness) over three
heterogeneous interface families:

  1. daily bars  — tushare proxy `daily` + akshare EM `stock_zh_a_hist`
                   + akshare Sina `stock_zh_a_daily` (dual/tri source)
  2. names/industry — tushare `stock_basic` vs akshare `stock_info_a_code_name`
  3. announcements — `stock_yjyg_em` family (eastmoney datacenter, raw 27-key
                     named contract equivalent to akshare's 28-col positional)

Nothing here touches production paths; all outputs land inside the spike dir.
"""
