#!/usr/bin/env python3
"""generate_view_summaries.py — 为公开页面生成结果摘要。

一、热力摘要（v2 起有）：
  market_industry_heatmap.json（约 3.8MB，30 个交易日全量历史）和
  industry_heatmap.json（约 824KB，63 个推荐日全量历史）原先被每个页面整文件下载。
  前端的热力视图只渲染最新一天，因此这里预生成轻量 *_latest.json：
    data/recommendation_analytics/market_industry_heatmap_latest.json
    data/recommendation_analytics/industry_heatmap_latest.json
  全量文件只在本机作为输入，公开页面仅加载 *_latest.json。

二、历史战绩摘要（v3 新增，DESIGN-V3.md 第 3 节）：
  review_state_unified.json（约 1.5MB，全量推荐明细）裁剪为
    data/latest/review_track_latest.json
  仅保留顶层 generated_at / trade_date、策略汇总和组合级 daily_comparison。
  stock_rows、数据库路径、运行备注等本机明细均不写入公开文件。

三、情绪因子摘要（v3 新增，替代 legacy 静态 sentiment.html）：
  review_state_unified.json 的 stock_rows 聚合为
    data/latest/sentiment_state.json
  口径：基于近 N 个交易日 AI 对「推荐个股」的观点聚合，非全市场情绪。
  ai_view 为中文复合值（如「持有/加仓」「卖出/观望」），按首关键字归一到四桶：
    买入/加仓类 → 看多；持有/持仓/小仓位类 → 中性偏多；
    观望类 → 中性；卖出类 → 看空（无法归类的非空值并入「中性」并计数）。
  输出 distribution 计数与占比、avg_ai_score（有 ai_score 行的均值）、
  以及最近 ~20 个推荐日的 daily_series（看多占比 / 平均分 / 样本数）供趋势图。

接入方式（Mac 流水线）：
  在生成/更新上述全量文件之后、git add 之前执行：
      python3 generate_view_summaries.py
  脚本只依赖标准库，幂等，可重复执行。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ANALYTICS_DIR = REPO_ROOT / "data" / "recommendation_analytics"
LATEST_DIR = REPO_ROOT / "data" / "latest"

# 情绪因子摘要：聚合近 N 个推荐日的 AI 个股观点（每个推荐日给 daily_series 一个点）。
SENTIMENT_WINDOW_DAYS = 20

# 情绪桶（四类，顺序即展示顺序）。
SENTIMENT_BUCKETS = ("看多", "中性偏多", "中性", "看空")

# (源文件, 输出文件, 行内日期字段, 顶层最新日期字段, 排名字段)
TARGETS = [
    (
        "market_industry_heatmap.json",
        "market_industry_heatmap_latest.json",
        "trade_date",
        "latest_trade_date",
        "market_heat_rank",
    ),
    (
        "industry_heatmap.json",
        "industry_heatmap_latest.json",
        "recommend_date",
        "latest_recommend_date",
        "heat_rank",
    ),
]


def summarize(source: Path, dest: Path, date_field: str, latest_field: str, rank_field: str) -> dict:
    doc = json.loads(source.read_text(encoding="utf-8"))
    latest = str(doc.get(latest_field) or "")
    rows = doc.get("rows") or []
    latest_rows = [row for row in rows if str(row.get(date_field) or "") == latest]
    latest_rows.sort(key=lambda row: float(row.get(rank_field) or 9999))
    out = dict(doc)
    out["rows"] = latest_rows
    out["summarized"] = True
    out["summarized_from_rows"] = len(rows)
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {
        "source": source.name,
        "dest": dest.name,
        "latest": latest,
        "rows_in": len(rows),
        "rows_out": len(latest_rows),
        "bytes_in": source.stat().st_size,
        "bytes_out": dest.stat().st_size,
    }


def summarize_review_track(source: Path, dest: Path) -> dict:
    """review_state_unified.json -> review_track_latest.json（公开复盘结果）。

    全量 stock_rows 只用于在本机计算归因，绝不进入公开结果。
    """
    doc = json.loads(source.read_text(encoding="utf-8"))
    rows = doc.get("stock_rows") or []
    observed_costs = sorted(
        {
            float(row["round_trip_cost"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("round_trip_cost"), (int, float))
        }
    )
    round_trip_cost = observed_costs[0] if len(observed_costs) == 1 else None

    public_strategy_fields = {
        "generated_at",
        "strategy_id",
        "strategy_source",
        "strategy_name",
        "trade_date",
        "source_date",
        "total_rows",
        "row_count",
        "main_count",
        "watch_count",
        "avoid_count",
        "latest_recommend_date",
        "latest_raw_recommend_date",
        "latest_evaluable_recommend_date",
        "latest_date_row_count",
        "latest_price_date",
        "date_range",
        "unique_stock_count",
        "performance",
        "ai_view_stats",
        "sector_stats",
        "score_bucket_stats",
        "top_repeat_recommendations",
    }

    strategies_in = doc.get("strategies") or {}
    strategies_out = {}
    for sid, summary in strategies_in.items():
        if isinstance(summary, dict):
            slim = {key: value for key, value in summary.items() if key in public_strategy_fields}
            # 用全量 stock_rows 重算分层归因，覆盖后端只统计「最新一天」的版本（次日未结算→全空）。
            attr = attribution_for(rows, sid)
            if attr["settled_rows"]:
                slim["sector_stats"] = attr["sector_stats"]
                slim["ai_view_stats"] = attr["ai_view_stats"]
                slim["score_bucket_stats"] = attr["score_bucket_stats"]
            strategies_out[sid] = slim
        else:
            strategies_out[sid] = summary

    out = {
        "public_contract_version": "public_results_v1",
        "generated_at": doc.get("generated_at"),
        "trade_date": doc.get("trade_date"),
        "strategies": strategies_out,
        "daily_comparison": doc.get("daily_comparison") or [],
        "methodology": {
            "signal_timing": "T close after signal; T+1 open_qfq entry",
            "one_day_return": "T+1 open_qfq to T+1 close_qfq net return",
            "round_trip_cost": round_trip_cost,
            "cost_included": round_trip_cost is not None,
            "stress_round_trip_cost": 0.005,
            "benchmark": "all_a_tradable_equal_weight",
        },
        "detail_storage": "local_only",
        "summarized": True,
        "summarized_from_rows": len(rows),
    }
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {
        "source": source.name,
        "dest": dest.name,
        "latest": str(doc.get("trade_date") or ""),
        "rows_in": len(rows),
        "rows_out": 0,
        "bytes_in": source.stat().st_size,
        "bytes_out": dest.stat().st_size,
    }


def normalize_ai_view(raw: object) -> str | None:
    """中文 ai_view 复合值 → 四桶之一（看多/中性偏多/中性/看空）。

    取首关键字判定：先看第一个分隔片段（/、（、空格 之前），命中即归类。
    复合值如「持有/加仓」「卖出/观望」「持仓者持有/空仓者观望」按首词归一。
    空值（None / 空串）返回 None（不计入样本）；非空但无法归类的并入「中性」。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # 取首关键字：到第一个分隔符（/ （ ( 空格 、）为止。
    head = text
    for sep in ("/", "（", "(", " ", "、"):
        idx = head.find(sep)
        if idx > 0:
            head = head[:idx]
    head = head.strip()

    # 买入 / 加仓 类 → 看多。
    if head.startswith("买入") or head.startswith("加仓"):
        return "看多"
    # 卖出 类 → 看空（含「卖出/观望」「卖出/减仓」）。
    if head.startswith("卖出"):
        return "看空"
    # 观望 类 → 中性（含「观望（严禁追高）」「观望/减仓」）。
    if head.startswith("观望"):
        return "中性"
    # 持有 / 持仓 / 小仓位 类 → 中性偏多（含「持有/加仓」「持仓者持有/空仓者观望」）。
    if head.startswith("持有") or head.startswith("持仓") or head.startswith("小仓位"):
        return "中性偏多"
    # 非空但无法归类：保守并入「中性」（计入样本，不丢弃也不假装看多）。
    return "中性"


def summarize_sentiment(source: Path, dest: Path) -> dict:
    """review_state_unified.json -> sentiment_state.json（情绪因子页轻量数据）。

    聚合 stock_rows 的 AI 个股观点，口径为「推荐个股 AI 观点」，非全市场情绪。
    """
    doc = json.loads(source.read_text(encoding="utf-8"))
    rows = doc.get("stock_rows") or []

    # 最近 N 个推荐日（按 recommend_date 降序去重）。
    all_dates = sorted(
        {str(row.get("recommend_date") or "") for row in rows if str(row.get("recommend_date") or "")},
        reverse=True,
    )
    window_dates = set(all_dates[:SENTIMENT_WINDOW_DAYS])

    # 窗口内样本（用于分布与平均分）。
    window_rows = [row for row in rows if str(row.get("recommend_date") or "") in window_dates]

    distribution = {bucket: 0 for bucket in SENTIMENT_BUCKETS}
    sample_count = 0
    score_values: list[float] = []
    for row in window_rows:
        bucket = normalize_ai_view(row.get("ai_view"))
        if bucket is None:
            continue
        distribution[bucket] += 1
        sample_count += 1
        score = row.get("ai_score")
        if isinstance(score, (int, float)):
            score_values.append(float(score))

    dist_out = {}
    for bucket in SENTIMENT_BUCKETS:
        count = distribution[bucket]
        ratio = round(count / sample_count, 4) if sample_count else 0.0
        dist_out[bucket] = {"count": count, "ratio": ratio}

    avg_ai_score = round(sum(score_values) / len(score_values), 2) if score_values else None

    # daily_series：每个推荐日一个点（按日期升序，便于趋势图从左到右）。
    daily_series = []
    for date in sorted(window_dates):
        day_rows = [row for row in window_rows if str(row.get("recommend_date") or "") == date]
        day_buckets = [normalize_ai_view(row.get("ai_view")) for row in day_rows]
        day_sample = sum(1 for b in day_buckets if b is not None)
        bullish = sum(1 for b in day_buckets if b == "看多")
        bullish_ratio = round(bullish / day_sample, 4) if day_sample else 0.0
        day_scores = [
            float(row.get("ai_score"))
            for row in day_rows
            if isinstance(row.get("ai_score"), (int, float))
        ]
        avg_score = round(sum(day_scores) / len(day_scores), 2) if day_scores else None
        daily_series.append(
            {
                "date": date,
                "bullish_ratio": bullish_ratio,
                "avg_score": avg_score,
                "sample": day_sample,
            }
        )

    out = {
        "generated_at": doc.get("generated_at"),
        "trade_date": doc.get("trade_date"),
        "source": "基于近 N 个交易日 AI 对推荐个股的观点聚合，非全市场情绪",
        "window_days": len(window_dates),
        "sample_count": sample_count,
        "distribution": dist_out,
        "avg_ai_score": avg_ai_score,
        "daily_series": daily_series,
        "summarized": True,
        "summarized_from_rows": len(rows),
    }
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {
        "source": source.name,
        "dest": dest.name,
        "latest": str(doc.get("trade_date") or ""),
        "rows_in": len(rows),
        "rows_out": sample_count,
        "bytes_in": source.stat().st_size,
        "bytes_out": dest.stat().st_size,
        "distribution": dist_out,
        "avg_ai_score": avg_ai_score,
    }


def _to_float(value):
    try:
        f = float(value)
        return f if f == f else None  # 排除 NaN
    except (TypeError, ValueError):
        return None


def _ai_view_bucket(raw) -> str:
    """AI 观点归一到「买入/持有/观望/卖出/其他/未标注」（取复合值的首段判定）。"""
    s = str(raw or "").strip()
    if not s:
        return "未标注"
    head = s.split("/")[0]
    if "买" in head or "介入" in head:
        return "买入"
    if "卖" in head or "减仓" in head or "清仓" in head:
        return "卖出"
    if "持" in head or "加仓" in head:
        return "持有"
    if "观" in head:
        return "观望"
    return "其他"


_SCORE_EDGES = [50, 55, 60, 65, 70, 75, 80]


def _score_bucket(score) -> str:
    s = _to_float(score)
    if s is None:
        return "未评分"
    if s < _SCORE_EDGES[0]:
        return f"<{_SCORE_EDGES[0]}"
    for lo, hi in zip(_SCORE_EDGES, _SCORE_EDGES[1:]):
        if s < hi:
            return f"{lo}-{hi}"
    return f"{_SCORE_EDGES[-1]}+"


_BUCKET_ORDER = ["<50", "50-55", "55-60", "60-65", "65-70", "70-75", "75-80", "80+", "未评分"]
SECTOR_MIN_COUNT = 5  # 行业分层最小样本：低于此的小样本行业不单列（噪音大、无统计意义）


def _agg_groups(rows, key_fn):
    groups = {}
    for r in rows:
        key = key_fn(r)
        if key is None:
            continue
        g = groups.setdefault(key, {"n": 0, "codes": set(), "ret_sum": 0.0, "ret_n": 0,
                                    "cum_sum": 0.0, "cum_n": 0, "score_sum": 0.0, "score_n": 0, "win": 0})
        g["n"] += 1
        code = r.get("stock_code")
        if code:
            g["codes"].add(code)
        nd = _to_float(r.get("next_day_return_pct"))
        if nd is not None:
            g["ret_sum"] += nd
            g["ret_n"] += 1
            if nd > 0:
                g["win"] += 1
        cum = _to_float(r.get("cumulative_return_pct"))
        if cum is not None:
            g["cum_sum"] += cum
            g["cum_n"] += 1
        sc = _to_float(r.get("ai_score"))
        if sc is not None:
            g["score_sum"] += sc
            g["score_n"] += 1
    return groups


def _group_metrics(g: dict) -> dict:
    return {
        "recommendation_count": g["n"],
        "unique_stock_count": len(g["codes"]),
        "avg_next_day_return_pct": round(g["ret_sum"] / g["ret_n"], 4) if g["ret_n"] else None,
        "avg_cumulative_return_pct": round(g["cum_sum"] / g["cum_n"], 4) if g["cum_n"] else None,
        "avg_ai_score": round(g["score_sum"] / g["score_n"], 2) if g["score_n"] else None,
        "hit_rate": round(g["win"] / g["ret_n"] * 100, 2) if g["ret_n"] else None,
    }


def attribution_for(stock_rows, strategy_id: str | None = None) -> dict:
    """从全量 stock_rows 重算分层归因（按行业 / AI 观点 / 评分段），仅含真实结算记录。
    覆盖后端只统计「最新一天」的 sector_stats 等，使分层基于全部历史，可用于策略评价/回测。"""
    rows = [r for r in stock_rows if r and (strategy_id is None or r.get("strategy_id") == strategy_id)]
    settled = [r for r in rows if _to_float(r.get("next_day_return_pct")) is not None]
    base = settled if settled else rows  # 若全未结算，退回全部（至少给出推荐数）

    sec = _agg_groups(base, lambda r: str(r.get("sector_name") or "未标注"))
    sector_all = [{"sector_name": name, **_group_metrics(g)} for name, g in sec.items()]
    # 只保留有统计意义的行业（推荐数≥5），避免单样本噪音排到前面误导；按收益降序。
    sector_kept = [s for s in sector_all if s["recommendation_count"] >= SECTOR_MIN_COUNT]
    sector_stats = sorted(
        sector_kept,
        key=lambda x: (x["avg_next_day_return_pct"] is None, -(x["avg_next_day_return_pct"] or 0)),
    )
    av = _agg_groups(base, lambda r: _ai_view_bucket(r.get("ai_view")))
    ai_view_stats = [{"ai_view": name, **_group_metrics(g)} for name, g in av.items()]
    sc = _agg_groups(base, lambda r: _score_bucket(r.get("ai_score")))
    score_bucket_stats = sorted(
        ({"bucket": name, **_group_metrics(g)} for name, g in sc.items()),
        key=lambda x: _BUCKET_ORDER.index(x["bucket"]) if x["bucket"] in _BUCKET_ORDER else 99,
    )
    return {"sector_stats": sector_stats, "ai_view_stats": ai_view_stats, "score_bucket_stats": score_bucket_stats,
            "settled_rows": len(settled), "total_rows": len(rows)}


def _strategy_metrics(daily_rows: list) -> dict | None:
    """逐日净值序列 -> 进阶绩效指标（与前端 review.js computePerformanceMetrics 同口径）。"""
    rows = sorted(
        (d for d in daily_rows if _to_float(d.get("avg_next_day_return_pct")) is not None),
        key=lambda d: str(d.get("recommend_date") or ""),
    )
    rets = [_to_float(d["avg_next_day_return_pct"]) / 100 for d in rows]
    n = len(rets)
    if n == 0:
        return None
    ann = 252
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sd = var ** 0.5
    nav = 1.0
    peak = 1.0
    maxdd = 0.0
    for r in rets:
        nav *= 1 + r
        peak = max(peak, nav)
        if peak > 0:
            maxdd = max(maxdd, (peak - nav) / peak)
    ann_ret = nav ** (ann / n) - 1
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    streak = cur = 0
    for r in rets:
        cur = cur + 1 if r < 0 else 0
        streak = max(streak, cur)
    hits = [_to_float(d.get("next_day_hit_rate_pct")) for d in rows]
    hits = [h for h in hits if h is not None]
    return {
        "trading_days": n,
        "cumulative_return_pct": round((nav - 1) * 100, 4),
        "final_nav": round(nav, 6),
        "max_drawdown_pct": round(maxdd * 100, 4),
        "annualized_return_pct": round(ann_ret * 100, 4),
        "annualized_vol_pct": round(sd * (ann ** 0.5) * 100, 4),
        "sharpe": round((mean / sd) * (ann ** 0.5), 4) if sd > 0 else None,
        "calmar": round(ann_ret / maxdd, 4) if maxdd > 0 else None,
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "avg_win_pct": round(gross_win / len(wins) * 100, 4) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses) * 100, 4) if losses else None,
        "max_consec_loss_days": streak,
        "avg_hit_rate_pct": round(sum(hits) / len(hits), 2) if hits else None,
    }


def export_strategy_evaluation(source: Path, dest: Path) -> dict:
    """策略评价小结：进阶绩效指标 + 分层归因，离线回测/评价依据，随 git 清理保留。"""
    doc = json.loads(source.read_text(encoding="utf-8"))
    daily_all = doc.get("daily_comparison") or []
    strategies_in = doc.get("strategies") or {}
    out_strategies = {}
    for sid, sv in strategies_in.items():
        if not isinstance(sv, dict):
            continue
        daily = [d for d in daily_all if d.get("strategy_id") == sid] or (daily_all if sid == "prebreakout_v41" else [])
        attr = attribution_for(doc.get("stock_rows") or [], sid)
        out_strategies[sid] = {
            "strategy_name": sv.get("strategy_name", sid),
            "date_range": sv.get("date_range"),
            "total_rows": sv.get("total_rows"),
            "unique_stock_count": sv.get("unique_stock_count"),
            "settled_rows": attr["settled_rows"],
            "metrics": _strategy_metrics(daily),
            "by_sector": attr["sector_stats"],
            "by_ai_view": attr["ai_view_stats"],
            "by_score_bucket": attr["score_bucket_stats"],
            "top_repeat": sv.get("top_repeat_recommendations", []),
        }
    out = {
        "generated_at": doc.get("generated_at"),
        "trade_date": doc.get("trade_date"),
        "methodology": "等权组合 / T日收盘后信号 / T+1复权开盘成交 / 已扣0.30%往返成本 / 无风险利率取0 / 252日年化；指标与前端历史战绩页同口径。",
        "strategies": out_strategies,
    }
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dest": str(dest.relative_to(REPO_ROOT)), "strategies": len(out_strategies), "bytes": dest.stat().st_size}


def main() -> int:
    ok = True
    for src_name, dest_name, date_field, latest_field, rank_field in TARGETS:
        source = ANALYTICS_DIR / src_name
        dest = ANALYTICS_DIR / dest_name
        if not source.exists():
            print(f"[skip] {source.relative_to(REPO_ROOT)} 不存在")
            continue
        try:
            info = summarize(source, dest, date_field, latest_field, rank_field)
        except Exception as exc:  # noqa: BLE001 - 报告并继续，不阻塞流水线
            print(f"[fail] {src_name}: {type(exc).__name__}: {exc}")
            ok = False
            continue
        print(
            f"[ok] {info['source']} -> {info['dest']}: "
            f"latest={info['latest']} rows {info['rows_in']} -> {info['rows_out']}, "
            f"{info['bytes_in'] / 1024:.0f}KB -> {info['bytes_out'] / 1024:.0f}KB"
        )

    # v3：历史战绩摘要（缺源文件只跳过，不阻塞热力摘要）。
    review_source = LATEST_DIR / "review_state_unified.json"
    review_dest = LATEST_DIR / "review_track_latest.json"
    if not review_source.exists():
        print(f"[skip] {review_source.relative_to(REPO_ROOT)} 不存在")
        if not review_dest.exists():
            print("[fail] review_track_latest.json 不存在且无统一复盘源，历史战绩页会加载失败")
            ok = False
    else:
        try:
            info = summarize_review_track(review_source, review_dest)
        except Exception as exc:  # noqa: BLE001 - 报告并继续，不阻塞流水线
            print(f"[fail] {review_source.name}: {type(exc).__name__}: {exc}")
            ok = False
        else:
            print(
                f"[ok] {info['source']} -> {info['dest']}: "
                f"latest={info['latest']} rows {info['rows_in']} -> {info['rows_out']}, "
                f"{info['bytes_in'] / 1024:.0f}KB -> {info['bytes_out'] / 1024:.0f}KB"
            )

    # v3：情绪因子摘要（缺源文件只跳过，不阻塞其余摘要）。
    sentiment_dest = LATEST_DIR / "sentiment_state.json"
    if not review_source.exists():
        print(f"[skip] {review_source.relative_to(REPO_ROOT)} 不存在（情绪因子摘要跳过）")
    else:
        try:
            info = summarize_sentiment(review_source, sentiment_dest)
        except Exception as exc:  # noqa: BLE001 - 报告并继续，不阻塞流水线
            print(f"[fail] sentiment_state.json: {type(exc).__name__}: {exc}")
            ok = False
        else:
            dist = info["distribution"]
            dist_text = " ".join(
                f"{name}={dist[name]['count']}({dist[name]['ratio'] * 100:.1f}%)"
                for name in SENTIMENT_BUCKETS
            )
            print(
                f"[ok] {info['source']} -> {info['dest']}: "
                f"latest={info['latest']} sample={info['rows_out']} "
                f"avg_score={info['avg_ai_score']} | {dist_text}, "
                f"{info['bytes_out'] / 1024:.1f}KB"
            )

    # 公开策略评价只输出汇总指标；逐股 CSV 永远不进入公开树。
    if review_source.exists():
        try:
            info = export_strategy_evaluation(review_source, LATEST_DIR / "strategy_evaluation.json")
            print(f"[ok] -> {info['dest']}: {info['strategies']} 个策略评价, {info['bytes'] / 1024:.1f}KB")
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] strategy_evaluation.json: {type(exc).__name__}: {exc}")
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
