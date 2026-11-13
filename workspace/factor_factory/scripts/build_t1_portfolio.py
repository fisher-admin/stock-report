#!/usr/bin/env python3
"""Build T+1 Alpha191 portfolio from whitelist factors."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", "/Users/fisher/.openclaw/workspace"))
FACTORS_DIR = Path(__file__).parent.parent / "data" / "factors"
OUTPUT_DIR = Path(__file__).parent.parent / "outputs" / "t1_alpha191"
CACHE_DIR = WORKSPACE / "stock_data" / "03-working" / "backtest_cache"
STOCK_LIST = WORKSPACE / "stock_data" / "01-root" / "stock_list.csv"
T1_DB_PATH = WORKSPACE / "stock_data" / "03-working" / "strategy_research" / "t1_factor_v1" / "t1_factor_v1.db"


def load_data() -> pd.DataFrame:
    """Load factor panel with labels (left join to keep all factor data)."""
    panel = pd.read_parquet(FACTORS_DIR / "gtja_alpha191_panel.parquet")
    labels = pd.read_parquet(FACTORS_DIR / "t1_labels.parquet")
    # Left join to keep all factor data, even without labels
    return panel.merge(labels, on=["ts_code", "trade_date"], how="left")


def load_whitelist() -> list[str]:
    """Load whitelist factors."""
    with open(OUTPUT_DIR / "factor_whitelist.json") as f:
        return json.load(f)["factors"]


def compute_factor_score(df: pd.DataFrame, factor_col: str) -> float:
    """Compute composite score for factor selection."""
    subset = df[[factor_col, "label_exec_return", "trade_date"]].dropna()
    if len(subset) < 100:
        return -999
    
    # Rank IC
    ic = subset[factor_col].corr(subset["label_exec_return"], method="spearman")
    
    # ICIR (simplified - using cross-sectional std)
    daily_ic = df.groupby("trade_date").apply(
        lambda g: g[[factor_col, "label_exec_return"]].dropna()[factor_col].corr(
            g[[factor_col, "label_exec_return"]].dropna()["label_exec_return"], 
            method="spearman"
        ) if len(g[[factor_col, "label_exec_return"]].dropna()) > 30 else np.nan
    )
    icir = daily_ic.mean() / daily_ic.std() if daily_ic.std() > 0 else 0
    
    # Top quintile return
    subset["rank"] = subset.groupby("trade_date")[factor_col].rank(pct=True)
    top_return = subset[subset["rank"] >= 0.8]["label_exec_return"].mean()
    
    # Composite score (weighted)
    score = abs(ic) * 0.4 + abs(icir) * 0.3 + max(0, top_return) * 0.3
    return score


def greedy_factor_selection(df: pd.DataFrame, factors: list[str], max_factors: int = 12) -> list[str]:
    """Greedy factor selection with walk-forward validation."""
    selected = []
    remaining = factors.copy()
    
    # Sort by individual score
    scores = {}
    for f in factors:
        scores[f] = compute_factor_score(df, f)
    remaining.sort(key=lambda x: scores[x], reverse=True)
    
    print(f"Initial factor scores:")
    for f in remaining[:10]:
        print(f"  {f}: {scores[f]:.4f}")
    
    # Greedy selection
    for _ in range(min(max_factors, len(remaining))):
        best_factor = None
        best_improvement = -999
        
        for f in remaining:
            # Test adding this factor
            test_factors = selected + [f]
            
            # Simple walk-forward test (last 20 days)
            recent_dates = sorted(df["trade_date"].unique())[-20:]
            test_df = df[df["trade_date"].isin(recent_dates)]
            
            # Compute combined score (equal weight for now)
            combined_score = 0
            for col in test_factors:
                subset = test_df[[col, "label_exec_return"]].dropna()
                if len(subset) > 30:
                    ic = subset[col].corr(subset["label_exec_return"], method="spearman")
                    combined_score += abs(ic)
            
            improvement = combined_score / len(test_factors) if test_factors else 0
            
            if improvement > best_improvement:
                best_improvement = improvement
                best_factor = f
        
        if best_factor and best_improvement > 0.005:  # Minimum improvement threshold
            selected.append(best_factor)
            remaining.remove(best_factor)
            print(f"Added {best_factor} (improvement: {best_improvement:.4f})")
        else:
            break
    
    return selected


def compute_weights(df: pd.DataFrame, factors: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """因子权重 + 方向（2026-06 重写）。
    返回 (weights, signs)：weights 用 |ICIR|（更稳，退 |IC|）做相对权重；signs=sign(IC) 用于把负向因子翻正。
    旧实现对 IC 取 abs 又裸加原始值，等于把负向因子当正向加、抵消有效信号——已废弃。"""
    weights: dict[str, float] = {}
    signs: dict[str, float] = {}
    for f in factors:
        subset = df[[f, "label_exec_return", "trade_date"]].dropna()
        if len(subset) < 100:
            weights[f] = 0.0
            signs[f] = 0.0
            continue
        ic = subset[f].corr(subset["label_exec_return"], method="spearman")
        if not np.isfinite(ic):
            weights[f] = 0.0
            signs[f] = 0.0
            continue
        signs[f] = 1.0 if ic >= 0 else -1.0
        # ICIR = 日度截面 IC 的均值/标准差（剔除常数日的 NaN）。
        daily_ic = subset.groupby("trade_date").apply(
            lambda g: g[f].corr(g["label_exec_return"], method="spearman")
        ).dropna()
        std = daily_ic.std(ddof=0)
        icir = (daily_ic.mean() / std) if std and np.isfinite(std) and std > 0 else 0.0
        weights[f] = abs(icir) if (np.isfinite(icir) and icir != 0) else abs(ic)

    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}
    for f in weights:
        weights[f] = min(0.25, weights[f])
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}
    return weights, signs


def composite_score(day_df: pd.DataFrame, factors: list[str], signs: dict[str, float], weights: dict[str, float]) -> pd.Series:
    """当日截面合成分：每个因子先按当日 z-score 标准化（消除 300-500 倍量纲差），
    乘 sign(IC) 方向对齐（负向因子翻正），再按权重求和。标准化后缺失/常数因子记 0（中性、不贡献），
    不再用 fillna(0) 把原始量纲值当分裸加。"""
    score = pd.Series(0.0, index=day_df.index)
    for f in factors:
        w = weights.get(f, 0.0)
        s = signs.get(f, 0.0)
        if w == 0 or s == 0 or f not in day_df.columns:
            continue
        col = day_df[f].astype(float)
        sd = col.std(ddof=0)
        if not sd or not np.isfinite(sd) or sd == 0:
            continue
        z = ((col - col.mean()) / sd).fillna(0.0)
        score = score + s * w * z
    return score


def compute_oos_gate(
    df: pd.DataFrame,
    factors: list[str],
    *,
    train: int = 60,
    test: int = 20,
    top_n: int = 20,
    cost: float = 0.002,
    icir_threshold: float = 0.30,
) -> dict:
    """真·样本外净 IC 闸门（2026-06 新增）。

    逐窗只用「训练窗」compute_weights 拟合方向/权重，再在「测试窗」用合成分测截面 rank-IC 与 top20
    扣费净超额，训练/测试严格不重叠。判定能否作为「可交易信号」：要求 OOS |ICIR|≥阈值 且扣费后净超额>0；
    否则锁研究观察、绝不发买入。这是「验证不达标即暂停」原则的可执行落点，取代样本内自评数字。
    """
    target = "label_exec_return"
    dates = sorted(df["trade_date"].unique())
    ics: list[float] = []
    net_excess: list[float] = []
    for i in range(train, len(dates) - test + 1, test):
        train_df = df[df["trade_date"].isin(dates[i - train:i])]
        w, s = compute_weights(train_df, factors)
        for d in dates[i:i + test]:
            day = df[df["trade_date"] == d].copy()
            if len(day) < 30:
                continue
            day["score"] = composite_score(day, factors, s, w)
            sub = day[["score", target]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(sub) < 30 or sub["score"].nunique() < 5:
                continue
            ics.append(float(sub["score"].corr(sub[target], method="spearman")))
            tr = day.nlargest(top_n, "score")[target].replace([np.inf, -np.inf], np.nan).dropna()
            ur = day[target].replace([np.inf, -np.inf], np.nan).dropna()
            if len(tr) and len(ur):
                ne = float(tr.mean() - ur.mean() - cost)
                if np.isfinite(ne):  # 防残留 inf/nan 标签污染均值（且 -Infinity 是非法 JSON）
                    net_excess.append(ne)
    n = len(ics)
    ics = [x for x in ics if np.isfinite(x)]
    ic_mean = float(np.mean(ics)) if ics else None
    ic_std = float(np.std(ics, ddof=1)) if len(ics) > 1 else None
    icir = (ic_mean / ic_std) if (ic_std and ic_std > 0 and ic_mean is not None) else None
    net_mean = float(np.mean(net_excess)) if net_excess else None
    net_pos = float(np.mean([x > 0 for x in net_excess])) if net_excess else None
    # JSON 安全：任何非有限值降级为 None（避免 -Infinity 写进发布 JSON 让前端 JSON.parse 崩）。
    if ic_mean is not None and not np.isfinite(ic_mean):
        ic_mean = None
    if icir is not None and not np.isfinite(icir):
        icir = None
    if net_mean is not None and not np.isfinite(net_mean):
        net_mean = None
    tradeable = bool(
        icir is not None and abs(icir) >= icir_threshold and net_mean is not None and net_mean > 0
    )
    return {
        "tradeable": tradeable,
        "signal_status": "credible_signal" if tradeable else "no_credible_signal",
        "method": "walk_forward_oos: 训练窗拟合→测试窗评估，可执行口径(T+1开→T+2开)，已扣 round-trip 成本",
        "icir_threshold": icir_threshold,
        "round_trip_cost": cost,
        "oos_days": n,
        "oos_rank_ic_mean": round(ic_mean, 5) if ic_mean is not None else None,
        "oos_icir": round(icir, 4) if icir is not None else None,
        "top20_net_excess_mean_daily": round(net_mean, 6) if net_mean is not None else None,
        "top20_net_excess_positive_rate": round(net_pos, 4) if net_pos is not None else None,
        "note": (
            "样本外净IC达标，可作为可交易信号"
            if tradeable
            else "样本外无可信信号（ICIR未达阈值或扣费后净超额≤0）：仅供因子研究观察，不构成任何买卖建议"
        ),
    }


def write_t1_evidence_db(trade_date, top20_df, selected, signs, weights, factor_ic, signal_gate, in_sample) -> dict:
    """把 T1 因子证据 + 回测摘要写入策略库（#5：此前 factor_evidence/backtest_summary 长期为空，
    导致「每套策略保存完整因子证据/回测」未落地）。幂等：按 trade_date 覆盖。对外业绩以样本外为准。"""
    import sqlite3
    if not T1_DB_PATH.parent.exists():
        return {"written": False, "reason": "db_dir_missing"}
    sid = "t1_factor_v1"
    now = datetime.now().isoformat()
    conn = sqlite3.connect(T1_DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS factor_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "strategy_id TEXT, trade_date TEXT, stock_code TEXT, factor_name TEXT, factor_value REAL, "
            "factor_rank INTEGER, factor_ic REAL, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS backtest_summary (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "strategy_id TEXT, trade_date TEXT, win_rate_pct REAL, avg_return_pct REAL, max_drawdown_pct REAL, "
            "sharpe_ratio REAL, sample_count INTEGER, out_of_sample_win_rate REAL, out_of_sample_avg_return REAL, "
            "payload_json TEXT, created_at TEXT)"
        )
        conn.execute("DELETE FROM factor_evidence WHERE strategy_id=? AND trade_date=?", (sid, str(trade_date)))
        conn.execute("DELETE FROM backtest_summary WHERE strategy_id=? AND trade_date=?", (sid, str(trade_date)))
        fe = 0
        for rank, (_, row) in enumerate(top20_df.iterrows(), start=1):
            ts = str(row["ts_code"])
            code = ts.split(".")[0] if "." in ts else ts
            for f in selected:
                if f not in row or pd.isna(row[f]) or weights.get(f, 0.0) <= 0:
                    continue
                conn.execute(
                    "INSERT INTO factor_evidence (strategy_id,trade_date,stock_code,factor_name,factor_value,"
                    "factor_rank,factor_ic,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (sid, str(trade_date), code, f, float(row[f]), rank,
                     float(factor_ic.get(f, 0.0)) * float(signs.get(f, 1.0)), now),
                )
                fe += 1
        oos = signal_gate or {}
        bs = in_sample or {}
        conn.execute(
            "INSERT INTO backtest_summary (strategy_id,trade_date,win_rate_pct,avg_return_pct,max_drawdown_pct,"
            "sharpe_ratio,sample_count,out_of_sample_win_rate,out_of_sample_avg_return,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid, str(trade_date),
                round(float(bs.get("hit_rate") or 0.0) * 100, 3),          # 样本内胜率%（标注非业绩）
                round(float(bs.get("total_return") or 0.0) * 100, 3),
                round(float(bs.get("max_drawdown") or 0.0) * 100, 3),
                round(float(bs.get("sharpe") or 0.0), 4),
                int(oos.get("oos_days") or 0),
                round(float(oos.get("top20_net_excess_positive_rate") or 0.0) * 100, 3),  # 样本外净超额胜率%
                round(float(oos.get("top20_net_excess_mean_daily") or 0.0) * 100, 4),     # 样本外日均净超额%
                json.dumps(
                    {"out_of_sample_authoritative": oos, "in_sample_warning": bs,
                     "tradeable": oos.get("tradeable"), "oos_icir": oos.get("oos_icir")},
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        conn.commit()
        return {"written": True, "factor_evidence_rows": fe, "backtest_summary_rows": 1}
    except sqlite3.Error as exc:
        return {"written": False, "error": str(exc)}
    finally:
        conn.close()


def load_stock_name_map(trade_date: str) -> dict[str, str]:
    names: dict[str, str] = {}
    daily_path = CACHE_DIR / f"daily_{trade_date}.parquet"
    if daily_path.exists():
        daily = pd.read_parquet(daily_path)
        if "ts_code" in daily.columns and "name" in daily.columns:
            for _, row in daily[["ts_code", "name"]].iterrows():
                code = str(row.get("ts_code") or "")
                name = str(row.get("name") or "")
                if code and name and name != "nan":
                    names[code] = name
                    names[code.split(".")[0]] = name
    if STOCK_LIST.exists():
        stock_list = pd.read_csv(STOCK_LIST)
        for _, row in stock_list.iterrows():
            code = str(row.get("ts_code") or "")
            name = str(row.get("name") or "")
            if code and name and name != "nan":
                names.setdefault(code, name)
                names.setdefault(code.split(".")[0], name)
    return names



def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} rows")
    
    whitelist = load_whitelist()
    print(f"Whitelist factors: {len(whitelist)}")
    
    # Greedy factor selection
    selected = greedy_factor_selection(df, whitelist, max_factors=12)
    print(f"\nSelected {len(selected)} factors:")
    for f in selected:
        print(f"  {f}")
    
    # Compute weights + 方向
    weights, signs = compute_weights(df, selected)
    print(f"\nFactor weights / 方向:")
    for f, w in weights.items():
        print(f"  {f}: w={w:.3f} sign={signs.get(f, 0):+.0f}")
    
    # Compute validation metrics
    recent_dates = sorted(df["trade_date"].unique())[-20:]
    recent_df = df[df["trade_date"].isin(recent_dates)]
    
    # Compute daily returns for the portfolio
    daily_returns = []
    for date in recent_dates:
        day_df = recent_df[recent_df["trade_date"] == date].copy()
        if len(day_df) < 10:
            continue
        
        # 方向对齐 + 截面标准化的合成分
        day_df["score"] = composite_score(day_df, selected, signs, weights)

        # Top 20
        top20 = day_df.nlargest(20, "score")
        avg_return = top20["label_exec_return"].mean()
        daily_returns.append({"date": date, "return": avg_return})
    
    returns_df = pd.DataFrame(daily_returns)
    
    # Validation metrics
    metrics = {
        "sample_days": len(returns_df),
        "avg_return": float(returns_df["return"].mean()),
        "hit_rate": float((returns_df["return"] > 0).mean()),
        "max_drawdown": float(((1 + returns_df["return"]).cumprod() / (1 + returns_df["return"]).cumprod().expanding().max() - 1).min()),
        "sharpe": float(returns_df["return"].mean() / returns_df["return"].std() * np.sqrt(252)) if returns_df["return"].std() > 0 else 0
    }
    
    print(f"\nValidation metrics (last 20 days):")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    
    # Get latest trade date
    latest_trade_date = sorted(df["trade_date"].unique())[-1]
    latest_df = df[df["trade_date"] == latest_trade_date].copy()
    
    # Compute latest top20 using factor scores only (no T+2 validation)
    latest_df["score"] = composite_score(latest_df, selected, signs, weights)
    # 当日截面百分位（真实分位，用于 action 分层与 ai_score，替代 50+score*180 线性假分）。
    latest_df["score_pct"] = latest_df["score"].rank(pct=True)

    top20_df = latest_df.nlargest(20, "score")

    # Build top20 list
    name_map = load_stock_name_map(str(latest_trade_date))
    top20_list = []
    for rank, (_, row) in enumerate(top20_df.iterrows(), start=1):
        ts_code = row["ts_code"]
        stock_code = ts_code.split(".")[0] if "." in ts_code else ts_code
        stock_name = name_map.get(ts_code) or name_map.get(stock_code) or ""
        score = float(row["score"])

        # 动作分层（2026-06 诚实化）：T1 未通过样本外验证，一律不给「买入/主攻」指令，仅作研究观察。
        raw_action = "watch"
        action = "watch"
        operation_advice = "观察"
        # 用「当日 top20 内名次」描述相对强弱（全市场分位对 top 选股恒≈100%、无区分度，反误导）。
        ai_advice = f"T1 因子当日 top20 第 {rank}/20 位（研究观察·未样本外验证·勿据此交易）"

        # 诚实化（2026-06）：T1 是模板/因子研究、非真实深度 AI，故 ai_score 置 None（无真实 AI 评分，
        # 不让 top1 显示「AI 100」误导）；相对强弱放进显式命名的 factor_rank_in_top20（1=最强）。
        factor_rank_in_top20 = rank
        ai_score = None
        ai_summary = (
            f"{stock_name or stock_code} 进入 T1 因子模型当日 top20（30 因子·WorldQuant-101 子集，非完整191），"
            f"方向对齐+截面标准化合成分当日 top20 第 {rank}/20 位。本策略处于研究阶段、未通过样本外验证，"
            "仅作因子研究信号展示（模板说明、非深度 AI 分析），不构成任何买卖建议。"
        )
        top20_list.append({
            "ts_code": ts_code,
            "code": stock_code,
            "stock_code": stock_code,
            "name": stock_name,
            "stock_name": stock_name,
            "score": score,
            "factor_rank_in_top20": factor_rank_in_top20,
            "raw_action": raw_action,
            "action": action,
            "strategy_source": "t1_factor",
            "ai_advice": ai_advice,
            "operation_advice": operation_advice,
            "ai_score": ai_score,
            "ai_summary": ai_summary,
            "ai_source_kind": "t1_factor_template_note",
            "ai_source_name": "T1 因子模板说明（非深度AI）",
            "ai_source_date": str(latest_trade_date),
            "ai_analysis_date": datetime.now().isoformat(),
            "recommend_date": str(latest_trade_date),
        })
    
    # Check if we have T+2 validation data for this date
    has_t2_validation = len(df[(df["trade_date"] == latest_trade_date) & df["label_exec_return"].notna()]) > 100

    # 真·样本外净 IC 闸门：决定 T1 能否作为可交易信号（不达标即锁研究观察、不发买入）。
    signal_gate = compute_oos_gate(df, selected)
    print(
        f"\n净IC闸门: tradeable={signal_gate['tradeable']} "
        f"OOS_ICIR={signal_gate['oos_icir']} "
        f"top20净超额/日={signal_gate['top20_net_excess_mean_daily']} "
        f"({signal_gate['note']})"
    )

    # Save research state
    research_state = {
        "strategy_id": "t1_factor_v1",
        "factor_source": "gtja_alpha191",
        "status": "research" if has_t2_validation else "research_preview",
        "generated_at": datetime.now().isoformat(),
        "latest_trade_date": latest_trade_date,
        "has_t2_validation": has_t2_validation,
        # 净 IC 闸门结论：tradeable=False 时前端/发布层应显示「研究观察·无可信信号」而非可交易推荐。
        "tradeable": signal_gate["tradeable"],
        "signal_gate": signal_gate,
        "top20": top20_list,
        "backtest_summary": {
            "_warning": "以下为样本内验证窗自评，非样本外业绩，不可作为实盘预期；可交易性以 signal_gate 为准。",
            "is_in_sample": True,
            "hit_rate": float((returns_df["return"] > 0).mean()),
            "total_return": float(returns_df["return"].sum()),
            "sharpe": float(returns_df["return"].mean() / returns_df["return"].std() * np.sqrt(252)) if returns_df["return"].std() > 0 else 0,
            "max_drawdown": float(((1 + returns_df["return"]).cumprod() / (1 + returns_df["return"]).cumprod().expanding().max() - 1).min())
        },
        "portfolio": {
            "factors": selected,
            "weights": weights,
            "validation_metrics": metrics
        }
    }
    
    with open(OUTPUT_DIR / "research_state_t1.json", "w") as f:
        json.dump(research_state, f, ensure_ascii=False, indent=2)
    print(f"Saved research state to {OUTPUT_DIR / 'research_state_t1.json'}")
    
    # Also save to LATEST_DIR for publication layer
    LATEST_DIR = Path(os.environ.get("OPENCLAW_PUBLISHED_REPO", str(WORKSPACE / "stock-report"))) / "data" / "latest"
    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    with open(LATEST_DIR / "research_state_t1.json", "w") as f:
        json.dump(research_state, f, ensure_ascii=False, indent=2)
    print(f"Saved research state to {LATEST_DIR / 'research_state_t1.json'}")

    # #5：把因子证据 + 回测摘要写入 T1 策略库（此前 factor_evidence/backtest_summary 长期为空）。
    factor_ic = {}
    for f in selected:
        sub = df[[f, "label_exec_return"]].replace([np.inf, -np.inf], np.nan).dropna()
        factor_ic[f] = float(sub[f].corr(sub["label_exec_return"], method="spearman")) if len(sub) > 30 else 0.0
    evidence = write_t1_evidence_db(
        latest_trade_date, top20_df, selected, signs, weights, factor_ic, signal_gate,
        research_state["backtest_summary"],
    )
    print(f"T1 证据入库(#5): {evidence}")

    # Save portfolio spec
    portfolio = {
        "strategy_id": "t1_factor_v1",
        "factor_source": "gtja_alpha191",
        "selected_factors": selected,
        "weights": weights,
        "validation_metrics": metrics,
        "label_policy": "label_exec_return",
        "universe_policy": "main+gem",
        "cost_model": {"buy": 0.001, "sell": 0.001},
        "created_at": datetime.now().isoformat()
    }
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "portfolio_spec.json", "w") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
    
    print(f"Saved portfolio spec to {OUTPUT_DIR / 'portfolio_spec.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
