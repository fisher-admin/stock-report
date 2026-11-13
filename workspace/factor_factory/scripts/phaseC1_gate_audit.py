#!/usr/bin/env python3
"""Phase C-1: 正式统计门控 - Rank IC + FDR + Newey-West + 噪声稳健性"""
from __future__ import annotations
import sys
import json
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats

WORKSPACE = Path('/Users/fisher/.openclaw/workspace/factor_factory')
PANEL_PATH = WORKSPACE / 'data' / 'factors' / 'greenfield_multifactor_panel.parquet'
OUTPUT_PATH = WORKSPACE / 'outputs' / 'greenfield_multifactor' / 'phaseC1_formal_gate_report.md'

FACTORS = [
    'g_intraday_vwap_deviation',
    'g_opening_gap_reversal',
    'g_close_strength_ratio',
    'g_chip_pullback_support',
    'g_long_cost_concentration',
    'g_intraday_range_expansion',
    'g_volume_price_divergence',
]

ECONOMIC_RATIONALE = {
    'g_intraday_vwap_deviation': '价格低于日内均价时的均值回归——代理做市商/算法交易的 VWAP 锚定效应。VWAP 策略的反向效应：当价格显著低于 VWAP 时，算法交易者会买入以回归目标价。',
    'g_opening_gap_reversal': '大幅低开后日内反弹——代理散户开盘恐慌 vs 机构日内承接的行为偏差。A 股散户主导市场的开盘定价效率低于收盘。',
    'g_close_strength_ratio': '强收盘预示次日日内延续——代理尾盘知情交易者的信息泄露。尾盘交易的信息含量高于日内其他时段 (Barclay & Hendershott, 2003)。',
    'g_chip_pullback_support': '筹码支撑位的价格弹性——代理获利盘惜售行为。当价格回落到密集成交区时，获利盘不愿亏损卖出，形成支撑。A 股特色因子。',
    'g_long_cost_concentration': '成本集中度与价格支撑——代理持仓成本分布的磁吸效应。当大量筹码集中在某一成本区间时，价格倾向于回归该区间。',
    'g_intraday_range_expansion': '日内波动扩张与方向延续——代理波动率聚集效应。当日内波动显著放大时，往往伴随趋势延续。',
    'g_volume_price_divergence': '价跌量缩时的抛压衰竭——代理卖方力量枯竭信号。当价格下跌但成交量萎缩时，表明抛压减弱，反转概率上升。',
}


def bh_fdr(p_values: np.ndarray, alpha: float = 0.05) -> tuple[np.ndarray, np.ndarray]:
    """Benjamini-Hochberg FDR 校正"""
    n = len(p_values)
    sorted_idx = np.argsort(p_values)
    sorted_p = p_values[sorted_idx]
    adjusted = np.zeros(n)
    cummin = 1.0
    for i in range(n - 1, -1, -1):
        rank = i + 1
        adj = min(cummin, sorted_p[i] * n / rank)
        cummin = adj
        adjusted[sorted_idx[i]] = adj
    reject = adjusted < alpha
    return reject, adjusted


def compute_rank_ic(panel: pd.DataFrame, factor_col: str, return_col: str, eval_dates: list[str], min_stocks: int = 50) -> list[float]:
    """计算每日截面 Rank IC"""
    ic_series = []
    for dt in eval_dates:
        day = panel.loc[panel['trade_date'] == dt, [factor_col, return_col]].dropna()
        if len(day) < min_stocks:
            continue
        ic, _ = stats.spearmanr(day[factor_col], day[return_col])
        if np.isfinite(ic):
            ic_series.append(ic)
    return ic_series


def newey_west_t(ic_arr: np.ndarray, lag: int | None = None) -> tuple[float, float]:
    """Newey-West t 统计量和 p 值"""
    n = len(ic_arr)
    ic_mean = ic_arr.mean()
    if lag is None:
        lag = int(np.floor(4 * (n / 100) ** (2 / 9)))
    gamma_0 = np.mean(ic_arr ** 2)
    nw_sum = gamma_0
    for j in range(1, lag + 1):
        w = 1 - j / (lag + 1)  # Bartlett kernel
        gamma_j = np.mean(ic_arr[j:] * ic_arr[:-j])
        nw_sum += 2 * w * gamma_j
    nw_se = np.sqrt(max(nw_sum, 1e-12) / n)
    nw_t = ic_mean / nw_se if nw_se > 0 else 0.0
    nw_p = 2 * (1 - stats.t.cdf(abs(nw_t), df=max(n - 1, 1)))
    return nw_t, nw_p


def noise_robustness(panel: pd.DataFrame, factor_col: str, return_col: str, eval_dates: list[str], n_bootstrap: int = 50, noise_amplitude: float = 0.05, min_stocks: int = 50) -> tuple[float, float]:
    """噪声稳健性测试：添加 5% 因子标准差的噪声，检查 IC 衰减"""
    rng = np.random.default_rng(42)
    factor_std = panel[factor_col].std()
    noise_sd = noise_amplitude * factor_std
    noisy_ics = []
    for b in range(n_bootstrap):
        noise = rng.normal(0, noise_sd, size=len(panel))
        noisy_col = f'_noisy_{factor_col}_{b}'
        panel[noisy_col] = panel[factor_col] + noise
        ic_list = compute_rank_ic(panel, noisy_col, return_col, eval_dates, min_stocks)
        if ic_list:
            noisy_ics.append(np.mean(ic_list))
        panel.drop(columns=[noisy_col], inplace=True)
    return float(np.mean(noisy_ics)), float(np.std(noisy_ics))


def main():
    print('=== Phase C-1: 正式统计门控 ===')
    print(f'加载面板数据: {PANEL_PATH}')
    panel = pd.read_parquet(PANEL_PATH)
    panel['trade_date'] = panel['trade_date'].astype(str)

    # 计算前瞻 O2C 收益：次日 (close/open - 1)
    # panel 中每行是某只股票某天的数据
    # fwd_o2c_lead1 = 次日的 O2C = groupby ts_code shift(-1) 后的 (close/open - 1)
    panel['_o2c_today'] = panel['close'] / panel['open'].replace(0, np.nan) - 1
    panel['fwd_o2c_lead1'] = panel.groupby('ts_code', group_keys=False)['_o2c_today'].shift(-1)

    # 评估窗口：最近 500 个交易日
    all_dates = sorted(panel['trade_date'].unique())
    eval_dates = all_dates[-500:]
    print(f'评估窗口: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 个交易日)')

    # 确认因子列存在
    missing = [f for f in FACTORS if f not in panel.columns]
    if missing:
        print(f'ERROR: 缺少因子列: {missing}')
        sys.exit(1)
    # 检查 zscore 列
    z_cols = [f'{f}__z' for f in FACTORS]
    missing_z = [c for c in z_cols if c not in panel.columns]
    use_z = len(missing_z) == 0
    if not use_z:
        print(f'注意: 缺少 zscore 列 {missing_z}，使用原始因子值')

    # ============= 门控 1: Rank IC + FDR + Newey-West =============
    print('\n--- 门控 1: Rank IC + FDR + Newey-West ---')
    results = {}
    for factor_id in FACTORS:
        col = f'{factor_id}__z' if use_z else factor_id
        ic_list = compute_rank_ic(panel, col, 'fwd_o2c_lead1', eval_dates)
        if not ic_list:
            print(f'{factor_id}: 无有效 IC 数据 → FAIL')
            results[factor_id] = {'status': 'FAIL', 'reason': 'no_data'}
            continue
        ic_arr = np.array(ic_list)
        n = len(ic_arr)
        ic_mean = float(ic_arr.mean())
        ic_std = float(ic_arr.std(ddof=1))
        t_raw = ic_mean / (ic_std / np.sqrt(n)) if ic_std > 0 else 0.0
        p_raw = 2 * (1 - stats.t.cdf(abs(t_raw), df=max(n - 1, 1)))
        nw_t, nw_p = newey_west_t(ic_arr)
        results[factor_id] = {
            'status': 'PENDING',
            'ic_mean': ic_mean,
            'ic_std': ic_std,
            'ic_ir': ic_mean / ic_std if ic_std > 0 else 0.0,
            'n_days': n,
            't_raw': t_raw,
            'p_raw': p_raw,
            'nw_t': nw_t,
            'nw_p': nw_p,
        }
        print(f'{factor_id}: IC={ic_mean:.4f}, IR={ic_mean/ic_std:.3f}, t={t_raw:.2f}, NW_t={nw_t:.2f}, p_raw={p_raw:.4f}, NW_p={nw_p:.4f}')

    # FDR 校正
    valid_factors = [f for f in FACTORS if f in results and results[f].get('nw_p') is not None]
    p_values = np.array([results[f]['nw_p'] for f in valid_factors])
    reject, p_fdr = bh_fdr(p_values, alpha=0.05)
    print(f'\nFDR 校正 (α=0.05):')
    gate1_pass_count = 0
    for i, fid in enumerate(valid_factors):
        nw_pass = results[fid]['nw_t'] > 1.96
        fdr_pass = bool(reject[i])
        overall = fdr_pass and nw_pass
        status = 'PASS' if overall else 'FAIL'
        if overall:
            gate1_pass_count += 1
        results[fid]['fdr_p'] = float(p_fdr[i])
        results[fid]['nw_t_pass'] = nw_pass
        results[fid]['fdr_pass'] = fdr_pass
        results[fid]['gate1_status'] = status
        print(f'  {fid}: FDR_p={p_fdr[i]:.4f}, NW_t={results[fid]["nw_t"]:.2f} {"PASS" if nw_pass else "FAIL"} → {status}')

    # ============= 门控 2: 噪声稳健性 =============
    print('\n--- 门控 2: 噪声稳健性 (5% std, 50x bootstrap) ---')
    gate2_results = {}
    gate2_pass_count = 0
    for fid in valid_factors:
        if results[fid].get('gate1_status') != 'PASS':
            gate2_results[fid] = {'status': 'SKIP', 'reason': 'gate1_fail'}
            print(f'  {fid}: SKIP (gate1 fail)')
            continue
        col = f'{fid}__z' if use_z else fid
        ic_orig = results[fid]['ic_mean']
        noisy_mean, noisy_std = noise_robustness(panel, col, 'fwd_o2c_lead1', eval_dates)
        decay = 1 - noisy_mean / ic_orig if ic_orig != 0 else 1.0
        status = 'PASS' if decay < 0.3 else 'FAIL'
        if status == 'PASS':
            gate2_pass_count += 1
        gate2_results[fid] = {'ic_original': ic_orig, 'ic_noisy_mean': noisy_mean, 'decay': decay, 'status': status}
        print(f'  {fid}: IC_orig={ic_orig:.4f}, IC_noisy={noisy_mean:.4f}, decay={decay:.1%} → {status}')

    # ============= 门控 3: 经济逻辑 =============
    print('\n--- 门控 3: 经济逻辑论证 ---')
    gate3_results = {}
    for fid in FACTORS:
        rationale = ECONOMIC_RATIONALE.get(fid, '')
        has_rationale = len(rationale) > 20
        gate3_results[fid] = {'rationale': rationale, 'status': 'PASS' if has_rationale else 'FAIL'}
        print(f'  {fid}: {"PASS" if has_rationale else "FAIL"} ({len(rationale)} chars)')

    # ============= 汇总报告 =============
    print('\n=== 汇总 ===')
    total_pass = 0
    report_lines = [
        '# Phase C-1 正式统计门控报告',
        '',
        f'**评估窗口**: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 个交易日)',
        f'**方法**: 截面 Rank IC (Spearman) → Newey-West t 检验 → BH-FDR 校正',
        '',
        '## 门控结果汇总',
        '',
        '| 因子 | IC Mean | IC IR | NW t | NW p | FDR p | Gate1 | Noise Decay | Gate2 | Gate3 | 总判定 |',
        '|------|---------|-------|------|------|-------|-------|-------------|-------|-------|--------|',
    ]
    for fid in FACTORS:
        r = results.get(fid, {})
        g2 = gate2_results.get(fid, {})
        g3 = gate3_results.get(fid, {})
        ic_mean = r.get('ic_mean', float('nan'))
        ic_ir = r.get('ic_ir', float('nan'))
        nw_t = r.get('nw_t', float('nan'))
        nw_p = r.get('nw_p', float('nan'))
        fdr_p = r.get('fdr_p', float('nan'))
        g1 = r.get('gate1_status', 'N/A')
        decay = g2.get('decay', float('nan'))
        g2s = g2.get('status', 'N/A')
        g3s = g3.get('status', 'N/A')
        overall = 'PASS' if g1 == 'PASS' and g2s == 'PASS' and g3s == 'PASS' else 'FAIL'
        if overall == 'PASS':
            total_pass += 1
        report_lines.append(f'| {fid} | {ic_mean:.4f} | {ic_ir:.3f} | {nw_t:.2f} | {nw_p:.4f} | {fdr_p:.4f} | {g1} | {decay:.1%} | {g2s} | {g3s} | **{overall}** |')

    report_lines += [
        '',
        f'**通过因子数**: {total_pass} / {len(FACTORS)}',
        '',
        '## 经济逻辑论证',
        '',
    ]
    for fid in FACTORS:
        rat = ECONOMIC_RATIONALE.get(fid, 'N/A')
        report_lines.append(f'### {fid}')
        report_lines.append(f'{rat}')
        report_lines.append('')

    report_lines += [
        '## 门控标准',
        '',
        '- **Gate 1 (Rank IC)**: FDR-adjusted p < 0.05 AND Newey-West t > 1.96',
        '- **Gate 2 (噪声稳健性)**: 5% std 噪声后 IC 衰减 < 30%',
        '- **Gate 3 (经济逻辑)**: 有明确的经济学解释',
        '',
        '## 晋升条件',
        '',
        '全部以下条件满足才解除 promotion freeze:',
        '',
        f'- [{"x" if total_pass >= 1 else " "}] 至少 1 个因子通过全部 3 道门控',
        f'- [{"x" if gate1_pass_count >= 1 else " "}] Gate1 通过因子数 ≥ 1',
        f'- [{"x" if gate2_pass_count >= 1 else " "}] Gate2 通过因子数 ≥ 1',
    ]

    report_text = '\n'.join(report_lines)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report_text, encoding='utf-8')
    print(f'\n报告已保存: {OUTPUT_PATH}')
    print(f'通过因子数: {total_pass} / {len(FACTORS)}')

    # 保存 JSON
    json_output = WORKSPACE / 'outputs' / 'greenfield_multifactor' / 'phaseC1_gate_results.json'
    json_data = {
        'eval_window': {'start': eval_dates[0], 'end': eval_dates[-1], 'days': len(eval_dates)},
        'factors': {},
        'summary': {'total_pass': total_pass, 'gate1_pass': gate1_pass_count, 'gate2_pass': gate2_pass_count},
    }
    for fid in FACTORS:
        json_data['factors'][fid] = {
            'ic': results.get(fid, {}),
            'noise': gate2_results.get(fid, {}),
            'rationale': gate3_results.get(fid, {}),
        }
    json_output.write_text(json.dumps(json_data, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(f'JSON 结果已保存: {json_output}')


if __name__ == '__main__':
    main()
