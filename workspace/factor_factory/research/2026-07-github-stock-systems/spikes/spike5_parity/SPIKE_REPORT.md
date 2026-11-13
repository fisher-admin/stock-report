# Spike-5 报告 · 表达式 parity: 5 因子含 warm-up/缺失/停牌/复权/ties

- **规格**: PHASE2_TIERA_REPORT.md §Spike-5; 输入 qlib.md §2.1-2.2/§3/§5.1 停牌契约, lean.md §5.2 口径 API
- **执行日**: 2026-07-11 · python 3.14.3 / pandas 3.0.0 / numpy 2.4.2 (venv=/Users/fisher/.openclaw/venv)
- **结论**: **通过** —— 七条验收全部实测 PASS(0 FAIL, 0 PENDING), 附 2 项真实数据新发现 + 3 条规格修订建议
- **全量复跑**: `cd <本目录> && /Users/fisher/.openclaw/venv/bin/python run_all.py` (exit=0, 35 tests)

## 1. 交付物

```
spike5_parity/
  parity_lib/            # additive 新模块, 不触碰任何生产脚本
    registry.py          # FactorSpec: required_lookback/required_forward/lookback_basis/ties/price_mode; 注册即门
    loader.py            # load_panel_for: 扩窗查询再截断 + 欠窗 raise LookbackUnderflow + require_full_sessions 开关
    calendar.py          # 交易日历 shift(交易日, 非自然日), 越界即炸
    panel.py             # 日历对齐宽表; 停牌=NaN 行, 全模块无任何 ffill/fillna 调用(结构保证)
    prices.py            # load_prices(panel, *, mode, asof): raw|hfq|qfq_asof, mode 必填无默认
    tswin.py             # 窗口决定论算子(见 §4 发现2 —— rtol=0 的实现前提)
    factors.py           # 5 因子 + 注册
    audit.py             # lookback±1 低报审计(值变=CI 拒绝) + exact_equal(rtol=0)
    sources.py           # SampleSource/CacheSource(只读)/ClippedSource(毒)/DropRowsSource(合成停牌)
    errors.py            # LookbackUnderflow/UnregisteredFactorError/AdjustmentModeError/PanelHoleError/...
  data/                  # tushare 代理拉取的真实小样本(30票 20240102-20260710, 609交易日; 复跑 fetch_sample_data.py)
  tests/test_a1..a7      # 七条验收逐条对应
  tests/test_52_regression.py   # ★永久资产: #52 滚动窗事故回归(不随 spike 回滚删除)
  scripts/run_ties_cross_version.sh  # 验收6 跨 pandas 版本复跑
  run_all.py             # 全量验收入口
```

**5 因子与元数据**(注册表实录):

| 因子 | 边界 | required_lookback | lookback_basis(摘) | ties | price_mode |
|---|---|---|---|---|---|
| ma60_diff | warm-up 长窗 | 60 | MA60 回看59 + diff1 回看1 | — | raw |
| ewm_ret20 | warm-up 形态二 | 138 | EWM(span20) qlib 式截断 int(ln1e-6/ln(1-α))=138 根收益并**硬窗口化**; +1 根收盘 | — | raw |
| pv_corr20 | 缺失 NaN | 20 | corr20(回看19)+收益/量变各1根; 窗含 NaN 即 NaN | — | raw |
| cs_rank_ret5 | ties | 5 | ret5 回看5; 截面 rank 逐日无状态不加窗 | average | raw |
| mom20_hfq | 复权 | 20 | 动量20回看20; 复权敏感强制 hfq | — | hfq |

**数据**: 主测=真实小样本(tushare 代理: 30 票跨主板/创业/科创, 609 交易日, 含 165 处真实除权事件与 688981.SH 真实停牌 20250901-20250908); 辅测=生产 backtest_cache 日线(~5457 票/日, **严格只读**)。用小样本的原因: cache 覆盖仅 184 个开市日(且有洞, 见发现1), 撑不起 [T-250,T]+lb138 的窗深; 代理拉小样本被规格允许。合成样本仅 2 处且已注明: a4 的受控停牌对照(DropRowsSource 删行)、a6 的并列值截面。

## 2. 七条验收逐条记录

| # | 验收条件 | 结果 | 复跑命令(均在 tests/ 下, python=/Users/fisher/.openclaw/venv/bin/python) |
|---|---|---|---|
| 1 | 恒等回放 rtol=0 | **PASS** | `python test_a1_identity_replay.py` |
| 2 | 欠窗必炸 | **PASS** | `python test_a2_underflow.py` |
| 3 | 低报审计 lookback±1 | **PASS** | `python test_a3_lookback_audit.py` |
| 4 | 停牌 NaN 不 ffill | **PASS** | `python test_a4_suspension.py` |
| 5 | 复权 raw vs hfq + qfq 无 asof 即 raise | **PASS** | `python test_a5_adjustment.py` |
| 6 | ties 声明锁定 + 换 pandas 版本值不变 | **PASS** | `python test_a6_ties.py` + `zsh scripts/run_ties_cross_version.sh` |
| 7 | 未注册禁入 | **PASS** | `python test_a7_registry_gate.py` |
| ★ | #52 永久回归 | **PASS** | `python test_52_regression.py` |

### 验收1 恒等回放(rtol=0)
- 主测: 5 因子 × A=[20250630,20260710](251日) vs B=[20260703,20260710](6日), 重叠段逐元素 `(a==b)|(isnan&isnan)` 全真, max_abs_diff=0.0; 附加"重叠段非全 NaN"断言防假通过。ewm_ret20 的 A 窗扩到 20241202(138 根), 全部落在样本内。
- 辅测(生产 cache 全截面 ~5400 票): 因 cache 数据洞(发现1), 用连续清洁段 T=20260407, [T-30,T] vs [T-5,T], 跑 lb≤60 的 ma60_diff/pv_corr20/cs_rank_ret5, 逐元素相等。universe=窗口内代码并集(A⊇B), 比较在 B 的代码集上——截面 rank 只消费当日有效值, 与并集大小无关(实测相等佐证)。
- 输出摘录: `test_identity_replay_all_factors ... ok / test_identity_replay_cache ... ok  — Ran 2 tests: OK`

### 验收2 欠窗必炸
- 毒样本: ClippedSource 把源起点裁到 ext_start 后 1 个交易日 → 5 因子全部 `raise LookbackUnderflow`(摘录: `need 20250627, panel starts 20250630` 形态)。
- 边界不虚炸: 恰好覆盖 ext_start 时正常返回。
- 日历越界同炸: 查询窗过早、shift(-138) 越过日历首日 → LookbackUnderflow。
- 附加真实毒样本(非人造): `require_full_sessions=True` 下跨 20260427/28 洞装载 → `PanelHoleError`(见发现1)。

### 验收3 低报审计
- 5 因子 +1 扰动全等(`plus1_equal=True, max_abs_diff=0.0`), -1 扰动全部值变(`minus1_changes_output=True`, 声明紧致不虚胖)。实测明细(可复跑):
```
{'factor': 'ma60_diff',   'lookback': 60,  'plus1_equal': True, 'minus1_changes_output': True}
{'factor': 'ewm_ret20',   'lookback': 138, 'plus1_equal': True, 'minus1_changes_output': True}
{'factor': 'pv_corr20',   'lookback': 20,  'plus1_equal': True, 'minus1_changes_output': True}
{'factor': 'cs_rank_ret5','lookback': 5,   'plus1_equal': True, 'minus1_changes_output': True}
{'factor': 'mom20_hfq',   'lookback': 20,  'plus1_equal': True, 'minus1_changes_output': True}
```
- 审计有牙齿(负向对照×2): ①ma60_diff 故意声明 59 → `LookbackUnderReported`; ②EWM 若不硬窗口化(裸 `pandas.ewm` 全历史)+声明138 → 同样被抓。后者证明审计能逮"隐性无限记忆"。

### 验收4 停牌
- **真实停牌**(688981.SH, 20250901-20250908, tushare 代理数据缺行): 面板 6 日 close 全 NaN, 停牌前一日/复牌日有值(前有值后 NaN=结构上未 ffill), 5 因子停牌日输出全 NaN。
- 结构不变式: 面板 NaN 数 == 日历格点数 − 原始行数(逐缺行留白, 全库无 fillna 调用)。
- **合成受控停牌**[已注明合成]: 600519.SH 删 5 日行 vs 无停牌对照 —— 差异日集合精确等于按各因子滞后集推导的"污染带"(ma60_diff: 停牌±60 全窗; mom20_hfq: 端点 {0,20} 两条带), 带外逐元素与对照相等, 复牌后窗口移出停牌段的**次一交易日**与对照精确一致 → 复牌差异完全可解释可预测。cs_rank 其它 29 票仅在停牌票 ret5 可用性变化日受影响(30→29 池), 其余日逐元素相等。
- **生产 cache 真实停牌**: 300052.SZ@20260415 缺行, 前后日在场 → NaN 不被前值填充, pv_corr20 于 0415/0416 均 NaN(窗口严格传播)。

### 验收5 复权
- 数据=真实除权事件: 查询窗 [20250630,20260710] 内 52 处 adj_factor 变化(全样本窗 165 处), 非合成。
- 显式断言三件: ①除权窗内 `mom_raw != mom_hfq` 且符号 `sign(mom_hfq−mom_raw)==sign(adj比−1)`; ②值域 `mom_hfq−mom_raw == (r−1)(1+mom_raw)` (rtol 1e-9); ③无除权窗两口径一致(rtol 1e-12)。恒等式 `(1+mom_hfq)=(1+mom_raw)·adj(t)/adj(t−20)` 全面板逐元素成立(rtol 1e-12)。
- qfq 守卫四连: `mode='qfq_asof'` 无 asof → AdjustmentModeError; `mode='qfq'` 别名 → 拒; asof 在日历外 → 拒; 不传 mode → TypeError(keyword-only 无默认)。`qfq(t,asof)=raw·adj/adj(asof)` 恒等 + asof 当日 qfq==raw 实测成立。
- 注册表层: mom20_hfq 声明 `price_mode='hfq'`; `price_mode='qfq_asof'` 的注册被 RegistryError 拒(qfq 因子须另携 asof 策略, 本 spike 拒收)。

### 验收6 ties
- 注册声明 `ties='average'`; 手算金值锁定: 截面 [0.10, 0.20×3, 0.30, NaN] → average+pct = [1/5, 3/5, 3/5, 5/5, 3/5, NaN] 逐元素精确相等; 对照证明声明有内容(method='min' 时并列组=2/5≠3/5)。
- **换 pandas 版本重跑值不变(实测, 非 PENDING)**: 独立 venv 复跑同一测试 —
```
--- 主 venv ---   python=3.14.3 pandas=3.0.0 numpy=2.4.2  Ran 3 tests OK
--- 异版本 venv --- python=3.14.3 pandas=2.3.3 numpy=2.5.1  Ran 3 tests OK
```
金值为手工推导常数, 任何版本下语义漂移都会使测试失败 → 金值即锁。

### 验收7 未注册禁入
- `gauntlet_admit('rogue_momentum_v9')` 与 `load_panel_for(['rogue_momentum_v9'],...)` 均 `UnregisteredFactorError`(装载器与闯关闸同门)。
- 注册即门: required_lookback=None / lookback_basis 空白 / 截面因子无 ties / required_forward<0 四种残缺注册全部 RegistryError; 5 个正式因子全部过闸。

### ★ #52 永久回归(tests/test_52_regression.py)
三道闸: R1 恒等回放(查询窗长短不得改变重叠值) + R2 欠窗硬失败 + R3 注册即门。文件头标注 **PERMANENT ASSET, 不随 spike 回滚删除**(规格回滚条款)。自包含(依赖 spike data/ 内快照数据), 离线可跑, <1s。

## 3. 隔离核查(git status)

```
$ git -C /Users/fisher/.openclaw/workspace status --short | grep -v "^??"   # 非新增项
 M factor_factory/scripts/build_advanced_portfolio.py      # 会话开始前已存在(见会话起始快照)
 M skills/...(9项) / M stock_data/...(alpha_mining 2项+stock_list)  # 同上, 均非本 spike 所触
 D/M stock_data/03-working/backtest_cache/{cyq_perf_*, stk_factor_*}.parquet (64 D + 64 M)
   —— 全部在会话起始 git 快照中即已如此; 本 spike 对 backtest_cache 仅做 pd.read_parquet 只读, 零写入
$ git status --short --untracked-files=all .../spikes/spike5_parity/
 ?? spike5_parity/{parity_lib,tests,scripts,data,run_all.py,fetch_sample_data.py,SPIKE_REPORT.md}  # 全部为本目录内新增
```
未修改: skills/ 生产脚本、panel_full、冻结 json、recommendations.db、backtest_cache 既有文件、stock-report 克隆。新代码/新 parquet 全部在 spike 目录内(additive)。

## 4. 关键发现(超出验收清单的实测事实)

1. **生产 backtest_cache 日线有 31 个开市日数据洞**(覆盖区间 20251009-20260710 共 184 开市日, 实有 153 文件): 缺 20251015-20251118 连续块(25日) + 20260408/09/10 + 20260417 + 20260427/28。对以 cache 为源的滚动窗计算是 **#52 同型风险**——中段洞不会触发起点守卫, 只会静默整日 NaN 传播(诚实但沉默)。spike 据此给装载器加了 `require_full_sessions` 开关(全市场面板应开启, 个股面板停牌合法故非默认), 并用真实洞做了毒样本测试。**建议生产侧核实这些日期是否需要回补, 或在管线加同类日历完整性断言。**
2. **pandas rolling 聚合不满足 rtol=0 parity(实现级教训)**: pandas 的 roll_mean/roll_var 是滑动累加实现, 浮点累积状态依赖序列起点 → 同一窗口在不同装载起点下可差最后 ulp, 恒等回放会以极小差破功。本 spike 的 tswin.py 用 sliding_window_view 对每窗独立归约(归约顺序只由窗宽决定)达成精确 rtol=0。**规格含义: "rtol=0 恒等"不只是装载器性质, 还约束因子算子的实现方式**——应写进闯关机文档(qlib 用 loc 截断天然回避了此问题是因为它也重算, 我方脚本因子若混用 pandas rolling 需换算子或放宽为 rtol≤1e-12 并声明)。
3. **EWM 必须"硬窗口化"而非仅声明截断值**: 若用裸 `pandas.ewm`(全历史)+保守 lookback 声明, +1 扰动审计立即抓出(负向对照2实测)。正确姿势=因子定义本身截断到 K 根(qlib 截断公式定 K), 声明与实现一致。
4. universe 语义: 装载 universe=窗口内出现代码并集, 长窗⊇短窗; 截面因子与并集大小无关(NaN 不参与 rank), cache 全截面实测确认。生产版建议显式支持 codes 参数以钉死人群。

## 5. 规格修订建议(不改判决, 供 Wave 实施稿吸收)

1. 验收1 追加一句: "因子算子实现须窗口决定论(每窗独立归约); 禁用起点依赖的滑动累加实现"(发现2)。
2. 装载器规格加可选项 `require_full_sessions`(全市场面板默认开), 把中段数据洞从"静默整日 NaN"升级为硬失败(发现1); 对应新错误类型 PanelHoleError。
3. 日历 shift 的**前向**越界目前复用 LookbackUnderflow(语义错位, spike 为省类型); 生产版应拆 CalendarOutOfRange。
4. 复跑注意: `python -m unittest tests.test_xx` 形式因无 __init__.py 不可用, 以 `run_all.py` 或直接执行测试文件为准(本报告命令均已按此给出)。

## 6. 成本与回滚

- 实耗 ≈ 1 个工作日(规格预估 2-3 天; 数据小样本化+复用真实停牌/除权事件省去造数)。
- 回滚: 删除本目录即零残留(未动任何调用点); **tests/test_52_regression.py + data/ 快照按规格保留为永久资产**。切换生产调用点属后续单一 commit, 不在本 spike 范围。
