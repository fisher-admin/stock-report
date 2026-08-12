#!/usr/bin/env python3
"""
A股量化选股Pipeline v3.0
完整流程: 更新数据 → 因子打分 → 生成报告 → 推送GitHub → 发送摘要

策略: 九因子多维轮动 (EMA+Stochastic+动量+流动性+波动控制+强度+MA均线+量比+MACD)
数据源: Tushare Pro (试用接口)
"""

import tushare as ts
import pandas as pd
import numpy as np
import json
import os
import sys
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 配置 ==========
# Credentials come from env vars or ~/.openclaw/workspace/.secrets/ (no plaintext in code).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from credentials import get_tushare_token, get_tushare_http_url  # noqa: E402

# 2026-06-28：去 Tushare 化——token 改为非必需，缺失/失效不再让模块导入即崩；
# 交易日历主用 akshare（新浪），行情/因子/筹码已走免费源（akshare_sina / 本地推算）。
TUSHARE_TOKEN = get_tushare_token(required=False)
TUSHARE_HTTP_URL = get_tushare_http_url()

STOCK_SYSTEM_ROOT = Path(os.environ.get("STOCK_SYSTEM_ROOT", str(Path(__file__).resolve().parents[3])))
WORKSPACE_DIR = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", str(STOCK_SYSTEM_ROOT / "workspace")))
STOCK_DATA_DIR = Path(os.environ.get("OPENCLAW_STOCK_DATA_DIR", str(WORKSPACE_DIR / "stock_data")))
ROOT_DIR = STOCK_DATA_DIR / "01-root"
INCREMENTAL_DIR = STOCK_DATA_DIR / "02-incremental"
WORKING_DIR = STOCK_DATA_DIR / "03-working"

GITHUB_REPO = "fisher-admin/stock-report"
REPORT_OUTPUT = "/tmp/stock-report-data.json"

TOP_N = 20
MAX_WORKERS = 5

# 策略模式标记
USE_PREBREAKOUT = False


# ========== 因子配置 v4.1 (威科夫+共振体系+Alpha量稳定性) ==========
# v4.1: 加入 turnover_stability 10% (STA production_ready + LTA全周期ICIR通过)
# 权重调整: momentum 10%→7%, volatility_health 7%→5%, kdj_position 5%→4%, liquidity 8%→6%
FACTOR_CONFIG = {
    "name": "Fisher选股",
    "version": "4.1",
    "factors": {
        # === 核心层 (70%) ===
        # MACD+BOLL双线共振: DIF>0且close>boll_mid
        "macd_boll_resonance": {"weight": 0.20, "desc": "MACD+BOLL共振"},
        # 筹码集中度: <10%强庄
        "chip_concentration": {"weight": 0.15, "desc": "筹码集中度"},
        # 量价关系: 放量滞涨=危险, 缩量回调=健康
        "volume_price": {"weight": 0.15, "desc": "量价关系"},
        # RSI+放量验证: 超卖+放量=黄金坑
        "rsi_volume": {"weight": 0.10, "desc": "RSI放量验证"},
        # 筹码支撑: close≈weight_avg+winner_rate适中
        "chip_support": {"weight": 0.10, "desc": "筹码支撑"},
        # === 辅助层 (30%) ===
        # 动量: 回测唯一有效因子
        "momentum": {"weight": 0.07, "desc": "20日动量"},
        # 量稳定性: Alpha验证 STA production_ready + LTA 20/40/60d全通过
        "turnover_stability": {"weight": 0.10, "desc": "量稳定性(Alpha双验证)"},
        # 流动性: 排除杀猪盘
        "liquidity": {"weight": 0.06, "desc": "流动性"},
        # 波动健康度: 适度波动>极低波动(反直觉)
        "volatility_health": {"weight": 0.05, "desc": "波动健康度"},
        # KDJ位置: 超卖区加分
        "kdj_position": {"weight": 0.04, "desc": "KDJ位置"},
    }  # 合计 = 1.00
}


PREBREAKOUT_CONFIG = {
    "name": "Fisher启动前夕",
    "version": "4.3",  # v4.3: turnover_stability 6%→12% (ICIR_5d=0.80 验证); volume_warmup 7%→5%, rsi_volume 8%→6%, liquidity 6%→4%
    "factors": {
        "chip_concentration":    {"weight": 0.22, "desc": "筹码集中度"},
        "volatility_squeeze":    {"weight": 0.18, "desc": "波动收敛"},
        "macd_early_signal":     {"weight": 0.16, "desc": "MACD早期信号"},
        "chip_support":          {"weight": 0.12, "desc": "筹码支撑"},
        "volume_warmup":         {"weight": 0.05, "desc": "温和放量"},  # 7%→5% 让位给量稳定性
        "rsi_volume":            {"weight": 0.06, "desc": "RSI放量验证"},  # 8%→6%
        "liquidity":             {"weight": 0.04, "desc": "流动性"},  # 6%→4%
        "momentum":              {"weight": 0.04, "desc": "动量(低权重)"},
        "kdj_position":          {"weight": 0.01, "desc": "KDJ位置"},
        "turnover_stability":    {"weight": 0.12, "desc": "量稳定性(Alpha验证 ICIR_5d=0.80)"},  # 6%→12%
    },  # 合计 = 1.00
    "hard_filters": {
        "max_daily_change": 6.0,
        "max_5d_change": 15.0,
        "max_ma20_bias": 8.0,
        "max_volume_ratio_stagnant": 2.5,
    }
}


def prebreakout_hard_filter(factors):
    """启动前夕硬过滤: 剔除已经飞了的票"""
    hf = PREBREAKOUT_CONFIG['hard_filters']
    change = abs(factors.get('change_pct', 0))
    if change > hf['max_daily_change']:
        return False
    mom = factors.get('momentum_raw', factors.get('momentum', 0))
    if mom > hf['max_5d_change']:
        return False
    close = factors.get('price', 0)
    boll_mid = factors.get('boll_mid', 0)
    if boll_mid > 0 and close > 0:
        bias = (close - boll_mid) / boll_mid * 100
        if bias > hf['max_ma20_bias']:
            return False
    vr = factors.get('volume_ratio', 1)
    chg = factors.get('change_pct', 0)
    if vr > hf['max_volume_ratio_stagnant'] and abs(chg) < 1:
        return False
    return True


def score_stock_prebreakout(factors):
    """启动前夕评分: 蓄势待发，非追涨"""
    if factors is None:
        return 0, {}
    scores = {}
    close = factors.get('price', 0)

    # 1. 筹码集中度 (22%) — 连续函数：带宽越窄越高分，避免分档同分
    chip_conc = float(factors.get('chip_concentration_raw', 0) or 0)
    if chip_conc > 0:
        # 基础线性惩罚 + 高带宽附加惩罚，低区间也保留细微差异
        raw = 98 - 180 * chip_conc - 220 * max(chip_conc - 0.12, 0)
        scores['chip_concentration'] = float(np.clip(raw, 8, 98))
    else:
        scores['chip_concentration'] = 50.0

    # 2. 波动收敛 (18%) — 连续函数：带宽越窄分越高，避免大面积同分
    boll_upper = factors.get('boll_upper', 0) or 0
    boll_lower = factors.get('boll_lower', 0) or 0
    boll_mid = factors.get('boll_mid', 0) or 0
    if boll_mid > 0 and boll_upper > boll_lower:
        bandwidth = (boll_upper - boll_lower) / boll_mid
        # 经验区间约[0.03,0.30+]，线性映射到[95,10]
        raw = 95 - (bandwidth - 0.03) * (85 / 0.27)
        scores['volatility_squeeze'] = float(np.clip(raw, 10, 95))
    else:
        scores['volatility_squeeze'] = 50.0

    # 3. MACD早期信号 (16%) — 连续函数：兼顾金叉强度与零轴邻近度
    dif = factors.get('macd_dif_ts', factors.get('macd_dif', 0)) or 0
    dea = factors.get('macd_dea_ts', factors.get('macd_dea', 0)) or 0
    cross = dif - dea  # 金叉强度
    near_zero = max(0.0, 1.0 - min(abs(dif), 1.2) / 1.2)  # 越靠近零轴越高
    cross_norm = np.tanh(cross * 3)  # [-1,1]
    # 基础50分 + 金叉贡献(±28) + 零轴邻近贡献(0~17)
    raw = 50 + 28 * cross_norm + 17 * near_zero
    scores['macd_early_signal'] = float(np.clip(raw, 10, 95))
    # 4. 筹码支撑 (12%) — 连续函数：价格贴近加权成本 + 合理获利盘更优
    wavg = float(factors.get('weight_avg', 0) or 0)
    wr = float(factors.get('winner_rate_raw', 50) or 50)
    if wavg > 0 and close > 0:
        ratio = (close - wavg) / wavg
        # 最优点位在略高于成本(+1%)，偏离越大连续扣分
        support_core = 90 - 520 * abs(ratio - 0.01)
        # 获利盘以45%附近最优，过高/过低都降分
        wr_bonus = 18 * max(0.0, 1.0 - abs(wr - 45) / 45)
        raw = support_core + wr_bonus - 8
        scores['chip_support'] = float(np.clip(raw, 10, 96))
    else:
        scores['chip_support'] = 50.0

    # 5. 温和放量 (12%) — 连续函数：以1.4为最佳中心，偏离越大扣分
    vr = factors.get('volume_ratio', 1) or 1
    raw_vr = 90 - 45 * abs(vr - 1.4)
    if vr > 2.8:
        raw_vr -= (vr - 2.8) * 20
    scores['volume_warmup'] = float(np.clip(raw_vr, 10, 92))

    # 6. RSI放量验证 (8%) — 连续函数：偏低位 + 温和放量更优
    rsi = factors.get('rsi_6', 50) or 50
    # rsi_target≈38（偏低但未极端）
    rsi_term = max(0.0, 1.0 - abs(rsi - 38) / 42)
    vr_term = max(0.0, 1.0 - abs(vr - 1.35) / 1.35)
    raw_rsi = 25 + 45 * rsi_term + 25 * vr_term
    if rsi > 75:
        raw_rsi -= (rsi - 75) * 1.2
    scores['rsi_volume'] = float(np.clip(raw_rsi, 10, 92))

    # 7. 流动性 (6%) — 连续函数：对数压缩避免阶梯同分
    liq = max(float(factors.get('liquidity', 0) or 0), 0.0)
    liq_norm = np.log1p(min(liq, 5000)) / np.log1p(5000)
    scores['liquidity'] = float(np.clip(12 + 78 * liq_norm, 10, 90))

    # 8. 动量 (4%) — 连续函数：轻微上涨最佳，过热与大跌扣分
    mom = float(factors.get('momentum', 0) or 0)
    # 峰值在 +1.8%，宽度约 5%
    raw_mom = 82 - ((mom - 1.8) ** 2) * 3.2
    scores['momentum'] = float(np.clip(raw_mom, 10, 88))

    # 9. KDJ位置 (1%) — 连续函数：K偏低位且K>D更优
    kdj_k = float(factors.get('kdj_k', factors.get('stochastic_k', 50)) or 50)
    kdj_d = float(factors.get('kdj_d', factors.get('stochastic_d', 50)) or 50)
    pos_term = max(0.0, 1.0 - abs(kdj_k - 32) / 55)
    cross_term = np.tanh((kdj_k - kdj_d) / 10.0)
    raw_kdj = 35 + 38 * pos_term + 18 * cross_term
    scores['kdj_position'] = float(np.clip(raw_kdj, 10, 88))

    # 10. 量稳定性 (6%) — Alpha研究验证 (ICIR_5d=1.13)
    # turnover_stability = -std(log(vol+1), 5d)，越稳定越高
    # 典型值域: [-2.0, -0.05]，映射到 [10, 95]
    _ts_val = factors.get('turnover_stability')
    ts = -0.5 if _ts_val is None else float(_ts_val)  # fix: 0.0 (perfectly stable) was falsy-coerced to -0.5
    # -0.1 附近为稳定（高分），-1.5 以下为剧烈波动（低分）
    raw_ts = 95 + (ts + 0.1) * 55  # 线性: -0.1→95, -1.5→18
    scores['turnover_stability'] = float(np.clip(raw_ts, 10, 95))

    for k in scores:
        scores[k] = float(np.clip(scores[k], 0, 100))

    config = PREBREAKOUT_CONFIG['factors']
    total = sum(scores.get(k, 0) * config[k]['weight'] for k in config)
    return round(float(total), 1), scores


def init_tushare():
    """初始化Tushare Pro接口"""
    pro = ts.pro_api(TUSHARE_TOKEN)
    pro._DataApi__token = TUSHARE_TOKEN
    pro._DataApi__http_url = TUSHARE_HTTP_URL
    return pro


# ========== Step 1: 更新增量数据 ==========

_AK_TRADE_DATES_CACHE = None


def _ak_trade_dates():
    """免费交易日历（akshare→新浪），返回升序 yyyymmdd 列表；失败返回 None。进程内缓存。"""
    global _AK_TRADE_DATES_CACHE
    if _AK_TRADE_DATES_CACHE is not None:
        return _AK_TRADE_DATES_CACHE or None
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = sorted({str(d).replace('-', '') for d in df['trade_date'].astype(str)})
        _AK_TRADE_DATES_CACHE = dates
        return dates
    except Exception:
        _AK_TRADE_DATES_CACHE = []
        return None


def get_trade_date(pro=None, date_str=None):
    """获取最近的交易日。2026-06-28 去 Tushare 化：主用 akshare(新浪)免费日历，pro 不再必需。"""
    if date_str is None:
        date_str = datetime.now().strftime('%Y%m%d')
    dates = _ak_trade_dates()
    if dates:
        ds = set(dates)
        if date_str in ds:
            return date_str
        prev = [d for d in dates if d <= date_str]
        if prev:
            return prev[-1]
    # akshare 不可用时的最后退路：若仍有可用 Tushare 接口则试一次（token 有效才会成功），否则返回 None
    if pro is not None and TUSHARE_TOKEN:
        try:
            cal = pro.trade_cal(exchange='SSE', start_date=date_str, end_date=date_str)
            if cal is not None and len(cal) > 0 and cal.iloc[0]['is_open'] == 1:
                return date_str
            start = (datetime.strptime(date_str, '%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
            cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=date_str, fields='cal_date,is_open')
            if cal is not None:
                open_days = cal[cal['is_open'] == 1].sort_values('cal_date', ascending=False)
                if len(open_days) > 0:
                    return open_days.iloc[0]['cal_date']
        except Exception:
            pass
    return None


def update_incremental(pro, trade_date):
    """更新当日增量数据"""
    print(f"📥 更新增量数据: {trade_date}")
    inc_dir = INCREMENTAL_DIR / trade_date
    inc_dir.mkdir(parents=True, exist_ok=True)

    inc_file = inc_dir / f"{trade_date}.parquet"
    if inc_file.exists():
        print(f"   已存在，跳过")
        return pd.read_parquet(inc_file)

    # 拉取全市场当日数据
    df = pro.daily(trade_date=trade_date)
    if df is not None and len(df) > 0:
        df.to_parquet(inc_file, index=False)
        print(f"   ✅ 获取 {len(df)} 条记录")
    else:
        print(f"   ⚠️ 无数据")
    return df


# ========== Step 2: 加载历史+增量数据 ==========

def load_stock_data(symbol, trade_date):
    """加载单只股票的完整数据 (根数据 + 增量)"""
    root_file = ROOT_DIR / f"{symbol}.parquet"
    if not root_file.exists():
        return None

    df = pd.read_parquet(root_file)

    # 合并增量数据
    for inc_dir in sorted(INCREMENTAL_DIR.iterdir()):
        if not inc_dir.is_dir():
            continue
        for inc_file in inc_dir.glob("*.parquet"):
            try:
                inc_df = pd.read_parquet(inc_file)
                ts_code = f"{symbol.zfill(6)}.{'SZ' if symbol.zfill(6).startswith(('0','3')) else 'SH' if symbol.zfill(6).startswith('6') else 'BJ'}"
                stock_inc = inc_df[inc_df['ts_code'] == ts_code]
                if len(stock_inc) > 0:
                    df = pd.concat([df, stock_inc], ignore_index=True)
            except Exception:
                continue

    if len(df) == 0:
        return None

    df = df.drop_duplicates(subset=['trade_date'], keep='last')
    df = df.sort_values('trade_date').reset_index(drop=True)
    return df


# ========== Step 3: 九因子多维轮动计算 ==========

def calc_ema(series, span):
    """计算EMA"""
    return series.ewm(span=span, adjust=False).mean()


def calc_factors(df):
    """
    计算John四重轮动因子
    返回各因子得分 (0-100)
    """
    if df is None or len(df) < 60:
        return None

    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    vol = df['vol'].values
    n = len(close)

    result = {}

    # 1. 动量因子: 20日涨幅
    if n >= 20:
        mom = (close[-1] - close[-20]) / close[-20] * 100
        result['momentum'] = mom
        result['momentum_raw'] = mom
    else:
        return None

    # 2. EMA趋势: EMA12 vs EMA26
    close_s = pd.Series(close)
    ema12 = calc_ema(close_s, 12).iloc[-1]
    ema26 = calc_ema(close_s, 26).iloc[-1]
    ema_diff_pct = (ema12 - ema26) / ema26 * 100
    result['ema_trend'] = ema_diff_pct
    result['ema12'] = ema12
    result['ema26'] = ema26

    # 3. Stochastic (%K, %D) - 14日
    period = 14
    if n >= period:
        low_14 = pd.Series(low).rolling(period).min().iloc[-1]
        high_14 = pd.Series(high).rolling(period).max().iloc[-1]
        if high_14 != low_14:
            k = (close[-1] - low_14) / (high_14 - low_14) * 100
        else:
            k = 50
        # %D = 3日SMA of %K
        k_series = []
        for i in range(max(0, n - 20), n):
            l14 = pd.Series(low[max(0, i - period + 1):i + 1]).min()
            h14 = pd.Series(high[max(0, i - period + 1):i + 1]).max()
            if h14 != l14:
                k_series.append((close[i] - l14) / (h14 - l14) * 100)
            else:
                k_series.append(50)
        d = np.mean(k_series[-3:]) if len(k_series) >= 3 else k
        result['stochastic'] = k
        result['stochastic_k'] = k
        result['stochastic_d'] = d
    else:
        return None

    # 4. MACD
    ema12_s = calc_ema(close_s, 12)
    ema26_s = calc_ema(close_s, 26)
    dif = ema12_s - ema26_s
    dea = calc_ema(dif, 9)
    macd_bar = (dif - dea) * 2
    result['macd'] = macd_bar.iloc[-1]
    result['macd_dif'] = dif.iloc[-1]
    result['macd_dea'] = dea.iloc[-1]
    # MACD信号强度: 归一化
    macd_strength = macd_bar.iloc[-1] / close[-1] * 100 if close[-1] > 0 else 0
    result['macd_strength'] = macd_strength

    # 5. 量比: 5日均量 / 20日均量
    if n >= 20:
        vol_5 = np.mean(vol[-5:])
        vol_20 = np.mean(vol[-20:])
        vr = vol_5 / vol_20 if vol_20 > 0 else 1
        result['volume_ratio'] = vr
    else:
        return None

    # 5b. 量稳定性 (turnover_stability_proxy): -std(log(vol+1), 5日)
    # 来源: 短线Alpha研究验证 (ICIR_1d=0.67, 3d=0.99, 5d=1.13) — 唯一全域达标因子
    # 含义: 量稳定 → 分高；量剧烈波动 → 分低
    if n >= 5:
        log_vol_5 = np.log1p(vol[-5:].astype(float))
        result['turnover_stability'] = -float(np.std(log_vol_5))
    else:
        result['turnover_stability'] = 0.0

    # 6. 流动性: 用成交额近似 (amount字段，单位千元)
    if 'amount' in df.columns and n >= 5:
        amt = df['amount'].values
        result['liquidity'] = np.mean(amt[-5:]) / 10000  # 转万元，作为流动性指标
    elif 'turnover_rate' in df.columns and n >= 5:
        tr = df['turnover_rate'].values
        result['liquidity'] = np.mean(tr[-5:])
    else:
        result['liquidity'] = 0

    # 7. 波动控制: 20日收益率标准差 (低波动高分)
    if n >= 20:
        returns = np.diff(close[-21:]) / close[-21:-1]
        result['volatility'] = np.std(returns) * 100  # 百分比
    else:
        result['volatility'] = 0

    # 8. 强度: 个股20日涨幅 vs 大盘涨幅的超额收益
    # 大盘涨幅在scan_all_stocks中注入，这里先存个股涨幅
    result['strength_raw'] = result.get('momentum', 0)

    # 9. MA均线多头排列度
    if n >= 60:
        ma5 = np.mean(close[-5:])
        ma10 = np.mean(close[-10:])
        ma20 = np.mean(close[-20:])
        ma60 = np.mean(close[-60:])
        # 计算排列度: 每满足一层+25分
        align_score = 0
        if ma5 > ma10: align_score += 25
        if ma10 > ma20: align_score += 25
        if ma20 > ma60: align_score += 25
        # 额外: 价格在MA5之上
        if close[-1] > ma5: align_score += 25
        result['ma_align'] = align_score
        result['ma5'] = ma5
        result['ma10'] = ma10
        result['ma20'] = ma20
        result['ma60'] = ma60
    else:
        result['ma_align'] = 0

    # 额外信息
    result['price'] = close[-1]
    result['change_pct'] = (close[-1] - close[-2]) / close[-2] * 100 if n >= 2 else 0
    result['trade_date'] = df['trade_date'].iloc[-1]

    return result


def score_stock(factors):
    """
    威科夫共振评分体系 v4.1
    核心层70% + 辅助层30% (含Alpha验证量稳定性因子)
    """
    if factors is None:
        return 0, {}

    scores = {}
    close = factors.get('price', 0)

    # ========== 核心层 (70%) ==========

    # 1. MACD+BOLL双线共振 (20%)
    # 文章核心: "MACD看方向，布林带中轨上下车"
    dif = factors.get('macd_dif_ts', factors.get('macd_dif', 0)) or 0
    dea = factors.get('macd_dea_ts', factors.get('macd_dea', 0)) or 0
    boll_mid = factors.get('boll_mid', 0) or 0
    boll_upper = factors.get('boll_upper', 0) or 0
    boll_lower = factors.get('boll_lower', 0) or 0

    macd_score = 0
    boll_score = 0
    # MACD方向: DIF>0且金叉(DIF>DEA)最强
    if dif > 0 and dif > dea:
        macd_score = 100
    elif dif > 0:
        macd_score = 70
    elif dif > dea:  # 零轴下金叉，底背离可能
        macd_score = 50
    else:
        macd_score = 20

    # BOLL位置: close vs 中轨
    if boll_mid > 0 and close > 0:
        if close >= boll_mid:
            # 中轨之上，越接近上轨越强
            if boll_upper > boll_mid:
                boll_score = 60 + min(40, (close - boll_mid) / (boll_upper - boll_mid) * 40)
            else:
                boll_score = 80
        else:
            # 中轨之下，风险区
            if boll_mid > boll_lower:
                boll_score = max(0, (close - boll_lower) / (boll_mid - boll_lower) * 50)
            else:
                boll_score = 20
    else:
        macd_score = 50
        boll_score = 50

    # 共振: 两者都强才给高分
    scores['macd_boll_resonance'] = macd_score * 0.5 + boll_score * 0.5

    # 2. 筹码集中度 (15%)
    # 文章核心: <10%强庄, <15%优秀, <20%控盘
    chip_conc = factors.get('chip_concentration_raw', 0)
    if chip_conc > 0:
        if chip_conc < 0.10:
            scores['chip_concentration'] = 90 + (0.10 - chip_conc) / 0.10 * 10
        elif chip_conc < 0.15:
            scores['chip_concentration'] = 75 + (0.15 - chip_conc) / 0.05 * 15
        elif chip_conc < 0.20:
            scores['chip_concentration'] = 60 + (0.20 - chip_conc) / 0.05 * 15
        elif chip_conc < 0.30:
            scores['chip_concentration'] = 40 + (0.30 - chip_conc) / 0.10 * 20
        else:
            scores['chip_concentration'] = max(0, 40 - (chip_conc - 0.30) * 100)
    else:
        scores['chip_concentration'] = 50

    # 3. 量价关系 (15%)
    # 文章核心: 放量滞涨=出货, 缩量回调=洗盘(健康), 价涨量增=健康上涨
    vr = factors.get('volume_ratio', 1)
    change = factors.get('change_pct', 0)
    mom = factors.get('momentum', 0)

    if change > 2 and vr > 1.2:
        # 价涨量增: 健康上涨
        scores['volume_price'] = 75 + min(25, change * 3)
    elif change > 0 and vr < 0.8:
        # 缩量上涨: 筹码锁定好
        scores['volume_price'] = 70
    elif change < -1 and vr < 0.8:
        # 缩量回调: 洗盘特征(正面)
        scores['volume_price'] = 65
    elif abs(change) < 1 and vr > 2.0:
        # 放量滞涨: 出货信号(危险!)
        scores['volume_price'] = max(0, 30 - (vr - 2) * 15)
    elif change < -3 and vr > 1.5:
        # 放量暴跌: 恐慌
        scores['volume_price'] = max(0, 20 - abs(change) * 2)
    else:
        # 正常
        scores['volume_price'] = 50 + change * 2

    scores['volume_price'] = np.clip(scores['volume_price'], 0, 100)

    # 4. RSI+放量验证 (10%)
    # 文章核心: 超卖+放量=黄金坑, 超买+放量滞涨=顶部
    rsi = factors.get('rsi_6', 50) or 50
    if rsi < 30 and vr > 1.2:
        # 超卖+放量: 黄金坑!
        scores['rsi_volume'] = 85 + min(15, (30 - rsi) + (vr - 1) * 10)
    elif rsi < 30:
        # 超卖但无量: 可能继续跌
        scores['rsi_volume'] = 60 + (30 - rsi)
    elif rsi > 70 and vr > 1.5 and change < 1:
        # 超买+放量滞涨: 顶部!
        scores['rsi_volume'] = max(0, 20 - (rsi - 70))
    elif rsi > 70:
        # 超买: 风险
        scores['rsi_volume'] = max(10, 40 - (rsi - 70) * 2)
    elif 40 <= rsi <= 60:
        # 中性区: 健康
        scores['rsi_volume'] = 55 + (60 - abs(rsi - 50)) * 0.5
    else:
        scores['rsi_volume'] = 50

    scores['rsi_volume'] = np.clip(scores['rsi_volume'], 0, 100)

    # 5. 筹码支撑 (10%)
    # 文章核心: close接近weight_avg=底部支撑, winner_rate 30-60%最佳
    wavg = factors.get('weight_avg', 0) or 0
    wr = factors.get('winner_rate_raw', 50) or 50

    support_score = 50
    if wavg > 0 and close > 0:
        price_vs_cost = (close - wavg) / wavg
        if -0.05 <= price_vs_cost <= 0.10:
            # 接近成本区: 强支撑
            support_score = 75 + (1 - abs(price_vs_cost) * 10) * 25
        elif price_vs_cost > 0.10:
            # 远离成本: 获利盘多
            support_score = max(20, 75 - price_vs_cost * 150)
        else:
            # 跌破成本: 套牢盘压力
            support_score = max(10, 50 + price_vs_cost * 200)

    # winner_rate修正: 30-60%最佳(主力吸筹完但未大幅拉升)
    wr_bonus = 0
    if 30 <= wr <= 60:
        wr_bonus = 15
    elif 20 <= wr < 30:
        wr_bonus = 10  # 超卖区可能是底部
    elif wr > 80:
        wr_bonus = -15  # 获利盘太多

    scores['chip_support'] = np.clip(support_score + wr_bonus, 0, 100)

    # ========== 辅助层 (30%) ==========

    # 6. 动量 (10%) - 回测唯一有效因子
    mom_raw = factors.get('momentum', 0)
    scores['momentum'] = np.clip((mom_raw + 10) / 40 * 100, 0, 100)

    # 7. 流动性 (8%) - 排除杀猪盘
    # 文章核心: 流动性枯竭才是真正风险
    liq = factors.get('liquidity', 0)
    if liq >= 2000:
        scores['liquidity'] = 80 + min(20, (liq - 2000) / 3000 * 20)
    elif liq >= 500:
        scores['liquidity'] = 50 + (liq - 500) / 1500 * 30
    elif liq >= 100:
        scores['liquidity'] = 20 + (liq - 100) / 400 * 30
    else:
        # 极低流动性: 杀猪盘风险
        scores['liquidity'] = max(0, liq / 100 * 20)

    # 8. 波动健康度 (7%) - 反直觉!
    # 文章核心: 极低波动=杀猪盘, 适度波动=健康市场
    vol_std = factors.get('volatility', 2)
    if 1.5 <= vol_std <= 4.0:
        # 适度波动: 最健康
        scores['volatility_health'] = 70 + (1 - abs(vol_std - 2.5) / 1.5) * 30
    elif vol_std < 0.5:
        # 极低波动: 杀猪盘嫌疑!
        scores['volatility_health'] = max(0, vol_std / 0.5 * 30)
    elif vol_std < 1.5:
        scores['volatility_health'] = 40 + (vol_std - 0.5) / 1.0 * 30
    else:
        # 高波动: 风险但有流动性
        scores['volatility_health'] = max(20, 70 - (vol_std - 4) * 10)

    # 9. KDJ位置 (5%)
    kdj_k = factors.get('kdj_k', factors.get('stochastic_k', 50)) or 50
    kdj_d = factors.get('kdj_d', factors.get('stochastic_d', 50)) or 50
    if kdj_k < 20:
        # 超卖区: 反弹机会
        scores['kdj_position'] = 75 + (20 - kdj_k)
    elif kdj_k > 80:
        # 超买区: 风险(但强势可能钝化)
        scores['kdj_position'] = max(20, 50 - (kdj_k - 80))
    elif kdj_k > kdj_d:
        # 金叉状态
        scores['kdj_position'] = 55 + min(20, (kdj_k - kdj_d))
    else:
        scores['kdj_position'] = 40

    scores['kdj_position'] = np.clip(scores['kdj_position'], 0, 100)

    # 量稳定性 (turnover_stability) — v4.1新增，Alpha双验证
    # STA: production_ready; LTA: 20d/40d/60d ICIR全通过
    # 原始值 = -std(log(vol+1), 5d)，越稳定越接近0，越不稳定越负
    _ts_val = factors.get('turnover_stability')
    ts = -0.5 if _ts_val is None else float(_ts_val)  # fix: 0.0 (perfectly stable) was falsy-coerced to -0.5
    # 映射: ts≈0(极稳定)→90+分; ts=-0.3(较稳定)→65分; ts<-0.8(剧烈波动)→15分
    scores['turnover_stability'] = float(np.clip(50 + (ts + 0.05) * 80, 10, 95))

    # ========== 加权总分 ==========
    config = FACTOR_CONFIG['factors']
    total = 0
    for key, cfg in config.items():
        total += scores.get(key, 0) * cfg['weight']

    # 确保所有值都是Python原生类型
    scores = {k: float(v) for k, v in scores.items()}

    return round(float(total), 1), scores


# ========== Step 4: 全市场扫描 ==========

def fetch_bulk_data(pro, trade_date):
    """批量拉取全市场技术因子和筹码数据"""
    stk_df = None
    cyq_df = None

    try:
        print("   📡 拉取stk_factor...")
        stk_df = pro.stk_factor(trade_date=trade_date)
        if stk_df is not None and len(stk_df) > 0:
            stk_df = stk_df.set_index('ts_code')
            print(f"   ✅ stk_factor: {len(stk_df)} 只")
        time.sleep(1)
    except Exception as e:
        print(f"   ⚠️ stk_factor拉取失败: {e}")

    try:
        print("   📡 拉取cyq_perf...")
        cyq_df = pro.cyq_perf(trade_date=trade_date)
        if cyq_df is not None and len(cyq_df) > 0:
            cyq_df = cyq_df.set_index('ts_code')
            print(f"   ✅ cyq_perf: {len(cyq_df)} 只")
        time.sleep(1)
    except Exception as e:
        print(f"   ⚠️ cyq_perf拉取失败: {e}")

    return stk_df, cyq_df


def merge_bulk_factors(factors, ts_code, stk_df, cyq_df):
    """将批量数据merge到单只股票的factors中"""
    # 合并stk_factor数据
    if stk_df is not None and ts_code in stk_df.index:
        row = stk_df.loc[ts_code]
        factors['macd_dif_ts'] = row.get('macd_dif', None)
        factors['macd_dea_ts'] = row.get('macd_dea', None)
        factors['macd_ts'] = row.get('macd', None)
        factors['kdj_k'] = row.get('kdj_k', None)
        factors['kdj_d'] = row.get('kdj_d', None)
        factors['rsi_6'] = row.get('rsi_6', None)
        factors['rsi_12'] = row.get('rsi_12', None)
        factors['boll_upper'] = row.get('boll_upper', None)
        factors['boll_mid'] = row.get('boll_mid', None)
        factors['boll_lower'] = row.get('boll_lower', None)
        factors['cci'] = row.get('cci', None)

    # 合并cyq_perf数据
    if cyq_df is not None and ts_code in cyq_df.index:
        row = cyq_df.loc[ts_code]
        cost_85 = row.get('cost_85pct', 0) or 0
        cost_15 = row.get('cost_15pct', 0) or 0
        close = factors.get('price', 1)
        factors['chip_concentration_raw'] = (cost_85 - cost_15) / close if close > 0 else 0
        factors['winner_rate_raw'] = row.get('winner_rate', 50)
        factors['weight_avg'] = row.get('weight_avg', 0)
        factors['cost_50pct'] = row.get('cost_50pct', 0)

    return factors


def scan_all_stocks(trade_date):
    """扫描全市场股票，计算因子得分"""
    print(f"🔍 全市场扫描开始...")

    stock_list_file = ROOT_DIR / "stock_list.csv"
    if not stock_list_file.exists():
        print("❌ stock_list.csv 不存在")
        return []

    stock_list = pd.read_csv(stock_list_file)
    # 过滤: 排除ST、科创板688
    stock_list = stock_list[~stock_list['name'].str.contains('ST|退', na=False)]
    stock_list = stock_list[~stock_list['ts_code'].str.startswith('68')]
    stock_list = stock_list[~stock_list['ts_code'].str.endswith('.BJ')]

    total = len(stock_list)
    print(f"   待扫描: {total} 只")

    # 批量拉取Tushare技术因子和筹码数据
    pro = init_tushare()
    stk_df, cyq_df = fetch_bulk_data(pro, trade_date)

    # 获取大盘(沪深300)20日涨幅作为强度基准
    benchmark_mom = 0
    try:
        bench_file = ROOT_DIR / "000300.parquet"
        if bench_file.exists():
            bench_df = pd.read_parquet(bench_file)
            bench_df = bench_df.sort_values('trade_date')
            if len(bench_df) >= 20:
                bc = bench_df['close'].values
                benchmark_mom = (bc[-1] - bc[-20]) / bc[-20] * 100
                print(f"   📊 沪深300基准: 20日涨幅 {benchmark_mom:.2f}%")
        else:
            print("   ⚠️ 无沪深300数据，强度因子使用绝对动量")
    except:
        print("   ⚠️ 沪深300数据获取失败，强度因子使用绝对动量")

    results = []
    errors = 0

    for idx, row in stock_list.iterrows():
        symbol = row['ts_code'].split('.')[0]  # 始终从ts_code提取，保证6位零填充
        name = row.get('name', symbol)
        ts_code = row['ts_code']

        try:
            df = load_stock_data(str(symbol), trade_date)
            if df is None or len(df) < 60:
                continue

            factors = calc_factors(df)
            if factors is None:
                continue

            # 合并Tushare批量数据
            factors = merge_bulk_factors(factors, ts_code, stk_df, cyq_df)

            # 注入强度因子: 个股涨幅 - 大盘涨幅
            factors['strength'] = factors.get('strength_raw', 0) - benchmark_mom

            # 根据策略选择打分函数
            if USE_PREBREAKOUT:
                # 启动前夕模式：先硬过滤
                if not prebreakout_hard_filter(factors):
                    continue
                total_score, sub_scores = score_stock_prebreakout(factors)
            else:
                total_score, sub_scores = score_stock(factors)

            results.append({
                'code': str(symbol),
                'ts_code': ts_code,
                'name': name,
                'score': total_score,
                'price': factors['price'],
                'change_pct': round(factors['change_pct'], 2),
                'momentum': round(factors['momentum_raw'], 2),
                'ema_trend': round(factors['ema_trend'], 3),
                'stochastic_k': round(factors.get('stochastic_k', 0), 1),
                'stochastic_d': round(factors.get('stochastic_d', 0), 1),
                'macd': round(factors.get('macd', 0), 3),
                'macd_dif': round(factors.get('macd_dif_ts', factors.get('macd_dif', 0)) or 0, 3),
                'macd_dea': round(factors.get('macd_dea_ts', factors.get('macd_dea', 0)) or 0, 3),
                'volume_ratio': round(factors.get('volume_ratio', 0), 2),
                'liquidity': round(factors.get('liquidity', 0), 2),
                'volatility': round(factors.get('volatility', 0), 2),
                'strength': round(factors.get('strength', 0), 2),
                'ma_align': round(factors.get('ma_align', 0), 1),
                'rsi_6': round(factors.get('rsi_6', 0) or 0, 1),
                'boll_mid': round(factors.get('boll_mid', 0) or 0, 2),
                'chip_conc': round(factors.get('chip_concentration_raw', 0), 4),
                'winner_rate': round(factors.get('winner_rate_raw', 0) or 0, 1),
                'weight_avg': round(factors.get('weight_avg', 0) or 0, 2),
                'sub_scores': {k: round(v, 1) for k, v in sub_scores.items()},
                'trade_date': factors['trade_date'],
            })
        except Exception as e:
            errors += 1
            continue

        if (idx + 1) % 500 == 0:
            print(f"   进度: {idx + 1}/{total}, 有效: {len(results)}, 错误: {errors}")

    print(f"   ✅ 扫描完成: {len(results)} 只有效, {errors} 只错误")

    # 按得分排序
    results.sort(key=lambda x: x['score'], reverse=True)
    return results


# ========== Step 5: 生成报告JSON ==========

def generate_report(results, trade_date):
    """生成报告JSON"""
    top = results[:TOP_N]

    for i, stock in enumerate(top):
        stock['rank'] = i + 1

    report = {
        "report": {
            "title": "A股量化选股报告",
            "subtitle": f"策略: {FACTOR_CONFIG['name']}",
            "date": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "version": "3.0",
            "total_scanned": len(results),
            "top_n": TOP_N,
        },
        "strategy": FACTOR_CONFIG,
        "recommendations": top,
        "statistics": {
            "avg_score": round(np.mean([r['score'] for r in results]), 1) if results else 0,
            "max_score": results[0]['score'] if results else 0,
            "min_score": results[-1]['score'] if results else 0,
            "top20_avg": round(np.mean([r['score'] for r in top]), 1) if top else 0,
        },
        "risk_warning": [
            "本报告仅供参考，不构成投资建议",
            "历史表现不代表未来收益",
            "请根据自身风险承受能力谨慎决策",
        ]
    }

    with open(REPORT_OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"📄 报告已生成: {REPORT_OUTPUT}")
    return report


# ========== Step 6: 推送GitHub Pages ==========

def push_to_github(trade_date):
    """推送报告到GitHub Pages"""
    print(f"🚀 推送到GitHub...")

    repo_dir = WORKING_DIR / "stock-report-repo"

    # clone or pull
    if (repo_dir / ".git").exists():
        subprocess.run(["git", "pull"], cwd=repo_dir, capture_output=True)
    else:
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["gh", "repo", "clone", GITHUB_REPO, str(repo_dir)],
            capture_output=True
        )

    # 读取pipeline生成的报告
    with open(REPORT_OUTPUT, 'r', encoding='utf-8') as f:
        report = json.load(f)

    top_stocks = report['recommendations']
    date_str = report['report']['date']

    # 1. 更新 data.json (含因子评分)
    factor_desc = {
        'macd_boll_resonance': 'MACD+BOLL共振',
        'chip_concentration': '筹码集中度',
        'volume_price': '量价关系',
        'rsi_volume': 'RSI放量验证',
        'chip_support': '筹码支撑',
        'momentum': '20日动量',
        'turnover_stability': '量稳定性(Alpha双验证)',
        'liquidity': '流动性',
        'volatility_health': '波动健康度',
        'kdj_position': 'KDJ位置',
    }
    def _backtest_display_block():
        """诚实化整改 2026-06-12：禁止硬编码回测数（曾长期展示2月旧数 +15.89%/Sharpe1.71）。
        一律从 backtest_result.json 实读；缺失则明示状态。"""
        path = WORKING_DIR / "backtest_result.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            summary = data.get("summary") or {}
            if summary:
                return {
                    "period": f"{summary.get('days', '-')}天 (rolling)",
                    "total_return": f"{float(summary.get('total_return', 0)):+.2f}%",
                    "sharpe": float(summary.get("sharpe", 0)),
                    "win_rate": f"{float(summary.get('win_rate', 0)):.1f}%",
                    "max_drawdown": f"{float(summary.get('max_drawdown', 0)):.2f}%",
                    "source": "backtest_result.json",
                    "methodology_note": "简化口径：T+1收盘等权、未计费用滑点，结果偏乐观",
                }
        except Exception:
            pass
        return {"status": "unavailable", "note": "回测结果文件缺失，指标暂不展示"}

    data_json = {
        "update_time": date_str,
        "total_stocks": report['report']['total_scanned'],
        "data_source": "Tushare Pro",
        "strategy": f"{FACTOR_CONFIG['name']} v{FACTOR_CONFIG['version']}",
        "factor_weights": {factor_desc.get(k, k): f"{v['weight']*100:.0f}%" for k, v in FACTOR_CONFIG['factors'].items()},
        "backtest": _backtest_display_block(),
        "top20": []
    }
    for s in top_stocks[:TOP_N]:
        entry = {
            "code": s['ts_code'],
            "name": s['name'],
            "close": s['price'],
            "change": s['change_pct'],
            "score": s['score'],
            "rank": s['rank'],
            "rsi_6": s.get('rsi_6', 0),
            "macd_dif": s.get('macd_dif', 0),
            "boll_mid": s.get('boll_mid', 0),
            "chip_conc": s.get('chip_conc', 0),
            "winner_rate": s.get('winner_rate', 0),
            "weight_avg": s.get('weight_avg', 0),
            "volume_ratio": s.get('volume_ratio', 1),
            "factor_scores": {},
        }
        for k, v in s.get('sub_scores', {}).items():
            entry['factor_scores'][factor_desc.get(k, k)] = round(v, 1)
        data_json['top20'].append(entry)

    with open(repo_dir / "data.json", 'w', encoding='utf-8') as f:
        json.dump(data_json, f, ensure_ascii=False, indent=2)

    # 2. 也保存完整报告到 data/
    data_dir = repo_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(REPORT_OUTPUT, data_dir / "latest.json")
    hist_dir = data_dir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPORT_OUTPUT, hist_dir / f"{trade_date}.json")

    # commit & push
    # errors="replace"(20260710夜): push被pre-push合同守卫拒绝时, 拒绝输出偶含非UTF-8字节
    # (0xbc), text=True严格解码直接抛UnicodeDecodeError——两策略选股全部完成却以rc=1收场。
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True)
    result = subprocess.run(
        ["git", "commit", "-m", f"📊 选股报告 {trade_date}"],
        cwd=repo_dir, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if "nothing to commit" in result.stdout + result.stderr:
        print("   无变更，跳过推送")
        return None

    push_result = subprocess.run(
        ["git", "push"], cwd=repo_dir, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if push_result.returncode == 0:
        url = f"https://fisher-admin.github.io/stock-report/"
        print(f"   ✅ 推送成功: {url}")
        return url
    else:
        print(f"   ❌ 推送失败: {push_result.stderr}")
        return None


# ========== Step 7: 生成摘要 ==========

def generate_summary(report, url=None, verify_text=None):
    """生成发送到频道的摘要文本"""
    date = report['report']['date']
    top = report['recommendations'][:10]
    stats = report['statistics']

    lines = []

    # 验证摘要放在最前面
    if verify_text:
        lines.append(verify_text)
        lines.append("")

    lines.extend([
        f"📊 A股选股报告 {date}",
        f"策略: {FACTOR_CONFIG['name']} v{FACTOR_CONFIG['version']} | 扫描: {report['report']['total_scanned']}只",
        "",
        "🏆 TOP 10:",
    ])

    for s in top:
        # 红涨绿跌
        arrow = "🔴" if s['change_pct'] >= 0 else "🟢"
        code = s['code'].zfill(6)
        lines.append(
            f"{s['rank']:>2}. {s['name']}({code}) "
            f"得分:{s['score']} {arrow}{s['change_pct']:+.1f}% "
            f"¥{s['price']}"
        )

    lines.append("")
    lines.append(f"📈 TOP20均分: {stats['top20_avg']} | 全市场均分: {stats['avg_score']}")

    if url:
        lines.append(f"🔗 完整报告: <{url}>")

    lines.append("\n⚠️ 仅供参考，不构成投资建议")

    return "\n".join(lines)


# ========== Step 0: 日度验证 (选股前执行) ==========

HISTORY_DIR = WORKING_DIR / "selection_history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def save_selection(trade_date, results):
    """保存当日选股结果供次日验证"""
    top = results[:TOP_N]
    data = {
        'trade_date': trade_date,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'stocks': [{
            'ts_code': s['ts_code'],
            'name': s['name'],
            'score': s['score'],
            'price': s['price'],
            'sub_scores': s['sub_scores'],
        } for s in top]
    }
    with open(HISTORY_DIR / f"{trade_date}.json", 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_prev_trade_date(pro, trade_date):
    """获取前一个交易日"""
    start = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
    end = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end)
    if cal is not None:
        opens = cal[cal['is_open'] == 1].sort_values('cal_date', ascending=False)
        if len(opens) > 0:
            return opens.iloc[0]['cal_date']
    return None


def daily_verify(pro, trade_date):
    """日度验证: 检查昨日TOP20今日实际表现"""
    prev_date = get_prev_trade_date(pro, trade_date)
    if not prev_date:
        return None

    hist_file = HISTORY_DIR / f"{prev_date}.json"
    if not hist_file.exists():
        print(f"📋 无昨日({prev_date})选股记录，跳过验证")
        return None

    with open(hist_file) as f:
        prev = json.load(f)

    # 拉取今日行情
    daily_df = pro.daily(trade_date=trade_date)
    if daily_df is None or len(daily_df) == 0:
        print(f"   ⚠️ 今日行情数据缺失")
        return None

    daily_idx = daily_df.set_index('ts_code')

    # 计算每只股票的实际表现
    results = []
    for s in prev['stocks']:
        tc = s['ts_code']
        if tc in daily_idx.index:
            r = daily_idx.loc[tc]
            pct = float(r.get('pct_chg', r.get('pct_change', 0)) or 0)
            results.append({
                'ts_code': tc,
                'name': s['name'],
                'score': s['score'],
                'sub_scores': s.get('sub_scores', {}),
                'actual_return': pct,
                'prev_price': s['price'],
                'today_close': float(r.get('close', 0)),
            })

    if not results:
        return None

    # 统计
    returns = [r['actual_return'] for r in results]
    avg_ret = np.mean(returns)
    win_count = sum(1 for r in returns if r > 0)
    win_rate = win_count / len(returns) * 100

    # 大盘对比 (用全市场均值近似)
    market_avg = daily_df['pct_chg'].mean() if 'pct_chg' in daily_df.columns else 0
    excess = avg_ret - market_avg

    # 因子表现分析: 高分因子 vs 实际收益的相关性
    factor_names = list(FACTOR_CONFIG['factors'].keys())
    factor_perf = {}
    for f in factor_names:
        f_scores = [r['sub_scores'].get(f, 0) for r in results]
        f_returns = [r['actual_return'] for r in results]
        if len(f_scores) > 5 and np.std(f_scores) > 0:
            corr = np.corrcoef(f_scores, f_returns)[0][1]
            if not np.isnan(corr):
                factor_perf[f] = round(corr, 4)

    # 最佳和最差
    results.sort(key=lambda x: x['actual_return'], reverse=True)
    best = results[0]
    worst = results[-1]

    verify = {
        'prev_date': prev_date,
        'today_date': trade_date,
        'n_stocks': len(results),
        'avg_return': round(avg_ret, 2),
        'market_avg': round(market_avg, 2),
        'excess_return': round(excess, 2),
        'win_rate': round(win_rate, 1),
        'win_count': win_count,
        'best': {'name': best['name'], 'return': round(best['actual_return'], 2)},
        'worst': {'name': worst['name'], 'return': round(worst['actual_return'], 2)},
        'factor_corr': factor_perf,
        'details': results,
    }

    # 保存验证结果
    verify_dir = WORKING_DIR / "verify_history"
    verify_dir.mkdir(parents=True, exist_ok=True)
    with open(verify_dir / f"{trade_date}.json", 'w', encoding='utf-8') as f:
        json.dump(verify, f, ensure_ascii=False, indent=2)

    return verify


def format_verify_summary(verify):
    """格式化验证摘要"""
    if not verify:
        return ""

    factor_desc = {k: v['desc'] for k, v in FACTOR_CONFIG['factors'].items()}

    lines = [
        f"📋 昨日选股验证 ({verify['prev_date']}→{verify['today_date']})",
        f"   TOP{verify['n_stocks']}均收益: {verify['avg_return']:+.2f}% | 大盘: {verify['market_avg']:+.2f}% | 超额: {verify['excess_return']:+.2f}%",
        f"   胜率: {verify['win_rate']:.0f}% ({verify['win_count']}/{verify['n_stocks']})",
        f"   最佳: {verify['best']['name']} {verify['best']['return']:+.2f}% | 最差: {verify['worst']['name']} {verify['worst']['return']:+.2f}%",
    ]

    # 因子有效性
    if verify['factor_corr']:
        effective = [(factor_desc.get(k, k), v) for k, v in verify['factor_corr'].items() if abs(v) > 0.15]
        if effective:
            effective.sort(key=lambda x: abs(x[1]), reverse=True)
            lines.append("   因子表现:")
            for name, corr in effective[:3]:
                icon = "✅" if corr > 0 else "⚠️"
                lines.append(f"     {icon} {name}: IC={corr:+.3f}")

    return "\n".join(lines)


# ========== 周度复盘 ==========

def weekly_review(pro, trade_date):
    """周五深度复盘: 汇总本周选股表现"""
    # 判断是否周五
    dt = datetime.strptime(trade_date, '%Y%m%d')
    if dt.weekday() != 4:  # 0=周一, 4=周五
        return None

    print("📊 周五深度复盘...")

    verify_dir = WORKING_DIR / "verify_history"
    if not verify_dir.exists():
        return None

    # 收集本周验证数据 (往前找5个交易日)
    start = (dt - timedelta(days=7)).strftime('%Y%m%d')
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=trade_date)
    if cal is None:
        return None
    week_dates = cal[(cal['is_open'] == 1) & (cal['cal_date'] <= trade_date)].sort_values('cal_date')['cal_date'].tolist()[-5:]

    week_data = []
    for d in week_dates:
        vf = verify_dir / f"{d}.json"
        if vf.exists():
            with open(vf) as f:
                week_data.append(json.load(f))

    if not week_data:
        print("   无本周验证数据")
        return None

    # 汇总统计
    all_returns = [v['avg_return'] for v in week_data]
    cum_ret = 1
    for r in all_returns:
        cum_ret *= (1 + r / 100)
    cum_ret = (cum_ret - 1) * 100

    all_excess = [v['excess_return'] for v in week_data]
    all_win = [v['win_rate'] for v in week_data]

    # 因子周度IC
    factor_names = list(FACTOR_CONFIG['factors'].keys())
    factor_desc = {k: v['desc'] for k, v in FACTOR_CONFIG['factors'].items()}
    weekly_ic = {f: [] for f in factor_names}
    for v in week_data:
        for f, ic in v.get('factor_corr', {}).items():
            weekly_ic[f].append(ic)

    # 生成周报
    lines = [
        "=" * 50,
        f"📊 Fisher选股 周度复盘",
        f"📅 {week_dates[0]} ~ {week_dates[-1]} ({len(week_data)}天有验证数据)",
        "=" * 50,
        f"📈 周累计收益: {cum_ret:+.2f}%",
        f"📈 日均收益: {np.mean(all_returns):+.2f}%",
        f"📈 日均超额: {np.mean(all_excess):+.2f}% (vs 大盘)",
        f"📈 平均胜率: {np.mean(all_win):.1f}%",
        "",
        "📊 因子周度IC:",
    ]

    factor_analysis = []
    for f in factor_names:
        ics = weekly_ic[f]
        if ics:
            avg_ic = np.mean(ics)
            desc = factor_desc.get(f, f)
            if avg_ic > 0.15:
                status = "🔥 强有效"
            elif avg_ic > 0.05:
                status = "✅ 有效"
            elif avg_ic > -0.05:
                status = "➖ 中性"
            elif avg_ic > -0.15:
                status = "⚠️ 弱负"
            else:
                status = "❌ 失效"
            lines.append(f"   {desc:<16} IC={avg_ic:+.3f} {status}")
            factor_analysis.append((f, desc, avg_ic, status))

    # 调参建议
    lines.append("")
    lines.append("💡 调参建议:")
    suggestions = []
    for f, desc, ic, status in factor_analysis:
        weight = FACTOR_CONFIG['factors'][f]['weight']
        if ic > 0.15 and weight < 0.20:
            suggestions.append(f"   📈 {desc} 表现优异(IC={ic:+.3f})，建议提升权重")
        elif ic < -0.10 and weight > 0.05:
            suggestions.append(f"   📉 {desc} 本周失效(IC={ic:+.3f})，建议降低权重或观察")

    if suggestions:
        lines.extend(suggestions)
    else:
        lines.append("   ✅ 各因子表现正常，暂无调整建议")

    review_text = "\n".join(lines)

    # 保存周报
    review_dir = WORKING_DIR / "weekly_reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    with open(review_dir / f"{trade_date}.txt", 'w', encoding='utf-8') as f:
        f.write(review_text)

    # 也保存JSON
    review_json = {
        'week': f"{week_dates[0]}~{week_dates[-1]}",
        'days': len(week_data),
        'cum_return': round(cum_ret, 2),
        'avg_daily': round(np.mean(all_returns), 2),
        'avg_excess': round(np.mean(all_excess), 2),
        'avg_win_rate': round(np.mean(all_win), 1),
        'factor_ic': {f: round(np.mean(weekly_ic[f]), 4) for f in factor_names if weekly_ic[f]},
        'suggestions': suggestions,
    }
    with open(review_dir / f"{trade_date}.json", 'w', encoding='utf-8') as f:
        json.dump(review_json, f, ensure_ascii=False, indent=2)

    return review_text


# ========== Main ==========

def main():
    """主流程: 验证 → 选股 → 报告 → 推送"""
    print("=" * 60)
    print(f"📊 Fisher选股 v4.0 Pipeline")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 初始化
    pro = init_tushare()

    # 获取交易日
    import os
    _date_arg = os.environ.get('PIPELINE_TARGET_DATE', None)
    trade_date = get_trade_date(pro, _date_arg)
    if not trade_date:
        print("❌ 无法获取交易日，退出")
        return None
    print(f"📅 交易日: {trade_date}")

    # Step 0: 日度验证 (选股前)
    print("\n" + "-" * 40)
    print("📋 Step 0: 昨日选股验证")
    print("-" * 40)
    verify = daily_verify(pro, trade_date)
    verify_text = format_verify_summary(verify)
    if verify_text:
        print(verify_text)
    else:
        print("   (无历史记录，跳过)")

    # Step 0.5: 周五深度复盘
    review_text = weekly_review(pro, trade_date)
    if review_text:
        print("\n" + review_text)

    # Step 1: 更新增量数据
    print("\n" + "-" * 40)
    update_incremental(pro, trade_date)

    # Step 2-3: 全市场扫描 + 因子打分
    results = scan_all_stocks(trade_date)
    if not results:
        print("❌ 无有效结果，退出")
        return None

    # 保存选股结果供明日验证
    save_selection(trade_date, results)

    # Step 4: 生成报告
    report = generate_report(results, trade_date)

    # 将验证数据注入报告
    if verify:
        report['verification'] = {
            'prev_date': verify['prev_date'],
            'avg_return': verify['avg_return'],
            'excess_return': verify['excess_return'],
            'win_rate': verify['win_rate'],
        }

    # Step 5: 推送GitHub
    url = push_to_github(trade_date)

    # Step 5.5: 同步到Fisher Tracker
    sync_to_tracker(trade_date, results)

    # Step 6: 生成摘要 (含验证)
    summary = generate_summary(report, url, verify_text)

    print("\n" + "=" * 60)
    print(summary)
    if review_text:
        print("\n" + review_text)
    print("=" * 60)

    # 输出摘要到文件供cron读取
    summary_file = WORKING_DIR / "latest_summary.txt"
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    full_summary = summary
    if review_text:
        full_summary += "\n\n" + review_text
    with open(summary_file, 'w', encoding='utf-8') as f:
        f.write(full_summary)

    print(f"\n✅ Pipeline完成!")
    return full_summary


def sync_to_tracker(trade_date, results):
    """同步选股结果到Fisher Tracker数据库"""
    import aiosqlite, json, os

    TRACKER_DB = os.environ.get(
        "TRACKER_DB", str(Path.home() / "fisher-tracker/data/tracker.db")
    )
    if not os.path.exists(TRACKER_DB):
        print(f"[Tracker] 数据库不存在: {TRACKER_DB}")
        return

    async def _sync():
        async with aiosqlite.connect(TRACKER_DB) as db:
            db.row_factory = aiosqlite.Row

            # 检查是否已存在
            cur = await db.execute(
                "SELECT COUNT(*) as cnt FROM daily_selections WHERE trade_date=?",
                (trade_date,)
            )
            if (await cur.fetchone())["cnt"] > 0:
                print(f"[Tracker] {trade_date} 已存在，跳过")
                return

            # 写入选股结果
            for i, s in enumerate(results):
                await db.execute("""
                    INSERT OR IGNORE INTO daily_selections
                    (trade_date, ts_code, name, score, rank, close_price, change_pct, factor_scores, strategy_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_date,
                    s.get("ts_code", ""),
                    s.get("name", ""),
                    s.get("score", 0),
                    i + 1,
                    s.get("price", 0),
                    s.get("change_pct", 0),
                    json.dumps(s.get("sub_scores", {}), ensure_ascii=False),
                    "v4.0",
                ))

            # 写入汇总
            avg_score = sum(s.get("score", 0) for s in results) / len(results) if results else 0
            await db.execute("""
                INSERT OR REPLACE INTO daily_summary
                (trade_date, total_stocks, strategy, avg_score, factor_weights)
                VALUES (?, ?, ?, ?, ?)
            """, (
                trade_date,
                len(results),
                "Fisher选股 v4.0",
                round(avg_score, 1),
                json.dumps({
                    "MACD_BOLL共振": "20%",
                    "筹码集中度": "15%",
                    "量价关系": "15%",
                    "RSI放量验证": "10%",
                    "筹码支撑": "10%",
                    "动量": "10%",
                    "流动性": "8%",
                    "波动健康度": "7%",
                    "KDJ位置": "5%",
                }, ensure_ascii=False),
            ))

            await db.commit()
            print(f"[Tracker] {trade_date} 同步完成: {len(results)}只")

    import asyncio
    asyncio.run(_sync())


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--strategy', '-s', default='v4.0',
                       choices=['v4.0', 'prebreakout', 'v4.1'],
                       help='选择策略: v4.0=基线, prebreakout/v4.1=启动前夕')
    parser.add_argument('--date', '-d', default=None,
                       help='指定交易日 YYYYMMDD，默认为今天')
    args = parser.parse_args()

    # 传递日期参数
    import os
    if args.date:
        os.environ['PIPELINE_TARGET_DATE'] = args.date

    # 动态切换策略配置
    if args.strategy in ['prebreakout', 'v4.1']:
        FACTOR_CONFIG['name'] = 'Fisher启动前夕'
        FACTOR_CONFIG['version'] = '4.1'
        # 替换为prebreakout因子
        FACTOR_CONFIG['factors'] = PREBREAKOUT_CONFIG['factors'].copy()
        # 重置权重
        for k, v in FACTOR_CONFIG['factors'].items():
            v['weight'] = PREBREAKOUT_CONFIG['factors'][k]['weight']
        # 标记使用prebreakout打分函数
        USE_PREBREAKOUT = True
        print(f"[策略切换] 使用启动前夕 v4.1 模式")

    main()
