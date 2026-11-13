# OpenBB 深读报告 (Phase2 Tier-A, 轨道=OpenBB, 议程=§3.3, 缺口=G4+G10)

- 对象: OpenBB-finance/OpenBB @ 冻结SHA `1c74893140292944e71ff5cdd9536edf12f05483` (manifest/repos.json)
- 方法: 全程 `curl raw.githubusercontent.com/<repo>/<SHA>/<path>` + `gh api git/trees`(recursive, truncated=false, 2710条路径), 零克隆, 零本地依赖引入
- 引证格式: `path#Lx-Ly@1c74893140`; **path 一律为仓根全路径**(平台代码带 `openbb_platform/` 前缀), 可直接拼接 raw URL 复核; 同段落内紧接同一文件的追加行号(裸 `#Lx-Ly`)沿用最近一次全路径
- **许可纪律声明: 本报告不含任何 OpenBB 可复制代码片段。所有机制以描述/接口形状/我方自有记号伪代码给出; "我方设计稿"与"pytest 模板"为本方原创产物, 仅借鉴思想。**

---

## 0. 文首结论: LICENSE 全文逐条复核判决 (议程第一步, 做完才动其他)

**判决: OpenBB 主仓 = 纯净 AGPL-3.0, 无任何附加条款、无额外限制、无补充许可、无商用双许可 rider。Phase1 卡的"NOASSERTION 系自定义序言所致"结论成立且从"读前 80 行"升级为全文机器级核验。**

复核方法(可复现):
1. 取冻结 SHA 下 `LICENSE` 全文(665 行), 与 GNU 官方 canonical `agpl-3.0.txt`(661 行)做**分词级 SequenceMatcher 全文比对**(空白归一化后逐词)。
2. 结果: 双方词数完全相等(5535 词 vs 5535 词), **全文仅 1 处非空白差异**——正文条目编号 "0. Definitions." 在 OpenBB 副本中被写成 "1. Definitions."(`LICENSE#L65@1c74893140`; 疑似 markdown 渲染器自动重编号的搬运痕迹, 后续 §1-§17 编号与 canonical 完全一致, 无任何条款文本改动, 无法律效果)。
3. 仓自有前言仅 3 行: 版权行 + "All files in this repository are licensed under the GNU Affero General Public License v3.0"(`LICENSE#L1-L3@1c74893140`)。**该声明是许可授予的宣示, 不是附加条款。**
4. 仓内其余许可文件盘点(git tree 全量 grep, 无遗漏): `desktop/LICENSE` 为 **MIT**(21 行); `desktop/src-tauri/LICENSE` 为**根 AGPL LICENSE 的逐字节全文副本**(665 行, `diff -q` 证实相同)——**desktop/ 目录内部即混合许可: 仅 desktop/LICENSE 覆盖范围是 MIT, src-tauri 子树仍是 AGPL, 不得据 MIT 借用 src-tauri 代码**。desktop/ 是 Tauri 桌面壳, 与我方零交集, 不改变平台判决; 无 COPYING/NOTICE/例外文件。(勘误: 本报告上一版误称两文件均为 MIT, 已更正。)
5. 包元数据交叉验证: `openbb_platform/pyproject.toml#L6@1c74893140` 与 `openbb_platform/providers/sec/pyproject.toml#L6@1c74893140` 均声明 SPDX `AGPL-3.0-only`——**"-only", 无 or-later 弹性, 无 dual-license**。

对我方的硬约束(重申+细化):
- **禁复制任何代码片段**(含测试/配置/文档中的代码块); 机制描述、接口形状、字段名清单(事实性 API 表面)可借鉴。
- **禁引入任何 openbb-* 包为依赖**(AGPL §13 网络条款: 我方发布页/网关若链接其代码即触发全源开放义务)。
- 本报告及下游实施稿均满足以上两条。
- **G10 状态**: manifest 侧勘误已写入 `manifest/repos.json` 的 `manifest_amendments`(本轮核实在案)——按 G10 定义(manifest 与卡的 AGPL 结论不同步), **G10 已闭合**。
- **附加建议(超出 G10 范围, 待用户拍板)**: `local/red_lines.md` 第 3 条"OpenBB 许可为 NOASSERTION, 深读先核实条款再定"已过时——核实完毕, 建议(红线文件系 Phase0 冻结, 不擅动)追加勘误行: "OpenBB=AGPL-3.0-only 全文核验(deep/openbb.md §0), 按 GPL 同级红线执行"。该建议不影响 G10 闭合判定。

---

## 1. TET 契约骨架机制盘点 (为设计稿做地基)

### 1.1 机制事实 (全部为机制描述, 无代码)

**Fetcher 泛型契约** `openbb_platform/core/openbb_core/provider/abstract/fetcher.py#L36-L85@1c74893140`:
- 一个 `Fetcher[Q, R]` 泛型基类, Q=查询模型, R=返回类型(通常是行模型列表)。
- 三段管线 TET: `transform_query(dict)->Q` → `extract_data(Q, credentials)->原始payload` → `transform_data(Q, raw)->R`。三段全是 staticmethod, 无实例状态。
- 子类钩子(`#L60-L71`): 子类化时强制校验"必须实现 extract 或 aextract 之一, 否则 NotImplementedError"; 若实现了异步版则异步版接管——**装配期(而非调用期)强制契约完整性**。
- `fetch_data`(`#L73-L85`)按序串三段; 允许返回 `AnnotatedResult`(`openbb_platform/core/openbb_core/provider/abstract/annotated_result.py@1c74893140`: result+metadata 双字段容器)让 fetcher 附带元数据。

**内置逐段自检** `fetcher.py#L115-L233@1c74893140` (`test()` 方法):
- Query 段断言: 非空、类型与泛型声明一致、入参值在 query 上恒等可取回(`#L152-L159`)。
- Extract 段断言: 非空、字段与行模型对齐, 以及**阶段隔离断言**——"extract 的输出必须尚未被 transform"(通过断言 raw 条目**不是**行模型子类实现, `#L173-L178`, `#L183-L185`)。这是防"fetcher 内偷跑标准化"导致管线阶段责任泄漏的机制, 全仓最值得借鉴的一件。
- Transform 段断言: 非空、行模型类型/字段/返回容器类型三重校验(`#L189-L233`)。

**标准模型层的三处契约软化(反面, 设计稿全部反其道)**:
1. `openbb_platform/core/openbb_core/provider/abstract/data.py#L77-L85@1c74893140`: 行模型基类 `extra='allow'` + camel/snake 双向 alias 生成器——任意未声明字段透传, "标准"模型在不同 provider 下实际形状不同。
2. `openbb_platform/core/openbb_core/provider/abstract/query_params.py#L61@1c74893140`: 查询模型同样 `extra='allow'`; 不支持参数在 `openbb_platform/core/openbb_core/app/query.py#L36-L64@1c74893140` 被 warn-then-drop(且 `openbb_platform/core/openbb_core/app/model/preferences.py#L25@1c74893140` `show_warnings=False` 默认关)——**传错参数默认无声消失**。
3. `openbb_platform/core/openbb_core/provider/abstract/data.py#L71,L87-L96@1c74893140`: `__alias_dict__` 每源字段名映射挂在**数据模型类上**——模型被具体源的命名细节污染, 换源要改模型。
- 有价值的正面细节: `ForceInt` 之类 BeforeValidator 做输入强转(同上 data.py `#L15-L23`); `__json_schema_extra__` 把 per-provider 参数能力(如"是否允许多标的")合并进同一字段的 schema(同上 query_params.py `#L17-L40` 文档), 使"同一参数在不同源的能力差异"机器可读。

**RegistryMap 双图谱** `openbb_platform/core/openbb_core/provider/registry_map.py#L71-L106,L142-L181@1c74893140`:
- 装载期内省每个 fetcher 的 pydantic 模型, 沿 MRO 链把字段按"定义文件是否位于 standard_models 目录"切成 standard(平台规范) 与 extra(源私有) 两份图谱(`#L172` 用文件路径判归属)。
- 价值: 跨源 schema 对账的机器可读基础——**"源私有字段显式标记, 不混入标准字段"**。
- 附注: 装载失败非 DEBUG 仅 warning 不中断(registry, Phase1 卡已证), 属 fail-open 反面。

### 1.2 判决表 (逐文件)

| 文件(仓根全路径, @1c74893140, 均位于 `openbb_platform/core/openbb_core/` 下) | 判决 | 依据 |
|---|---|---|
| openbb_platform/core/openbb_core/provider/abstract/fetcher.py | **借思想**: TET 三段+装配期完整性校验+test() 逐段断言(尤其阶段隔离断言) | L36-85, L60-71, L173-178 |
| openbb_platform/core/openbb_core/provider/abstract/annotated_result.py | **借思想并强化**: 元数据通道由可选改强制(我方 FetchMeta) | 全文 20 行 |
| openbb_platform/core/openbb_core/provider/abstract/data.py | **反其道**: extra='allow'→forbid; alias 从模型挪到 fetcher 侧 | L71, L77-85 |
| openbb_platform/core/openbb_core/provider/abstract/query_params.py | **反其道**: extra='forbid'; 借 __json_schema_extra__ 的 per-source 能力标注思想 | L61, L17-40 |
| openbb_platform/core/openbb_core/app/query.py + app/model/preferences.py | **反其道**: 未知参数=硬错误; 告警默认开 | query L52-62; preferences L25 |
| openbb_platform/core/openbb_core/provider/registry_map.py | **借思想**: standard/extra 双图谱按"定义位置"机械切分 | L71-106, L172 |
| openbb_platform/core/openbb_core/provider/utils/helpers.py (amake_*) | **反面清单固化**(§5) | L342-461 |
| openbb_platform/core/openbb_core/app/model/obbject.py | 不借(统一信封对我方无增量; to_llm 见 §4) | L36-355 |
| openbb_platform/core/openbb_core/app/static/package_builder.py | **明确不借**: 安装期 4340 行 codegen+linter 运行时依赖(Phase1 反模式#3, 本轮未再投入) | 卡引证 |

---

## 2. 我方三张标准模型设计稿 (日线/交易日历/集合竞价) — Phase4 provider contract 实施底稿

防重复建设对照(system_inventory §1): 我方已有 tushare代理+akshare 双源、backtest_cache parquet、B0 竞价库——**本设计稿不新建数据获取, 只在现有拉取入口外包一层冻结契约**(升级差距: "provider 候选列表无新鲜度契约"+"无 Raw 不可变层"+"缓存可被盘中裸跑污染")。以下全部为我方自有记号。

### 2.0 共通骨架 (三张表共享)

```
# 我方记号伪代码 (非 OpenBB 代码)
class StrictQuery(BaseModel):                 # 反其道#1
    model_config = extra='forbid', frozen=True
    # 未知参数 => ValidationError 硬失败, 永不 warn-then-drop

class StrictRow(BaseModel):                   # 反其道#2
    model_config = extra='forbid', strict=True
    # 源字段名映射不放模型: 放各 fetcher 的 to_rows 里 (模型永远只认标准名)

class FetchMeta:                              # AnnotatedResult 强化版: 必填而非可选
    source: str                # "tushare_proxy" | "akshare_em" | "akshare_sina"
    source_version: str        # 库版本/接口版本 pin (akshare 卡: 版本入 run_manifest)
    fetched_at: datetime       # 本机拉取时刻 (纳入盘中裸跑护栏判断)
    request_fingerprint: str   # 参数哈希, 接 run_manifest 血统
    row_count: int
    failures: list[FailureRecord]   # (results, failures) 双通道, 见 §5

TET 三段 (每源一个 fetcher):
    to_query(params: dict) -> Q               # 校验+规范化, 失败即抛
    pull_raw(q: Q) -> RawPayload               # 只做 IO, 不做任何标准化
    to_rows(q: Q, raw) -> (list[Row], FetchMeta)

自检 harness (借 Fetcher.test() 思想, 我方实现):
    T 段: q 类型正确 + 入参值可从 q 恒等取回
    E 段: raw 非空 + **raw 中不得出现 Row 实例** (阶段隔离断言)
    T2段: 行全为 Row + 行数>0 + 各表不变式(见下) + failures 与覆盖率对账
PIT 验收 (红线2): 每张表附 as-of 断言——以任意历史 as_of 重放, available_at>as_of 的记录数=0
```

### 2.1 DailyBarV1 (日线)

| 字段 | 类型 | 校验/不变式 | 备注 |
|---|---|---|---|
| symbol | str | 正则 `^\d{6}\.(SH|SZ|BJ)$` | 统一 tushare 记号; akshare 裸 6 位码在 to_rows 补后缀 |
| trade_date | date | 必须 ∈ TradingCalendarV1 开市日 | 跨表校验, 拒绝日历外日期(myhhub 实时盖历史戳反例) |
| open/high/low/close | float | >0; `low<=min(open,close)`; `high>=max(open,close)` | 破裂→failure 记录, 不静默修 |
| pre_close | float | >0; 来自源, **禁止自算** | 复权口径事故隔离 |
| volume | int | ≥0; **单位=股** | akshare 手/股混用前科, 字段描述+断言双写单位 |
| amount | float | ≥0; **单位=元** | #7328 成交量误标 amount 前科 |
| adj_factor | float\|None | >0 | 独立列(FinRL 教训); **全表禁存前复权价**(qfq 请求时点口径前科) |
| is_suspended | bool | 必填 | 停牌显式化, 禁"缺行=停牌"隐含语义 |
| source/fetched_at | — | FetchMeta 冗余落行级 | 血统可行级追溯 |

行级不变式(T2 段断言): 非停牌行 `volume>0`; `|close/pre_close-1| <= 0.205`(主板/创业板混合上限, 北交所 30cm 白名单例外表)——越界不是修数据而是 failure+告警(涨跌停语义我方自建, 红线 D 类教训)。

**"无涨跌幅限制情形"例外表**(与北交所白名单同格式, 缺此表则合法行情触发 failure+告警洪泛):

| 例外类 | 判定规则(可机械推导) | 断言处置 |
|---|---|---|
| 新股上市首日(主板) | trade_date == 上市日(股票基础表 list_date) | 豁免 0.205 断言, 行打 `limit_exempt="ipo_day1"` |
| 科创板/创业板上市前 5 交易日 | symbol 板块 ∈ {688xxx, 300xxx/301xxx} 且 trade_date ∈ 上市后前 5 个开市日(用 TradingCalendarV1 推) | 豁免, `limit_exempt="star_gem_first5"` |
| 退市整理期首日 | 需退市整理期起始日数据源(tushare 代理可得); 无源时降级为人工白名单 | 豁免, `limit_exempt="delist_day1"` |
| 重大资产重组复牌(历史情形, 2020 年前主板规则) | 仅历史回补数据涉及; 静态白名单(股票+日期对) | 豁免, `limit_exempt="restructure_resume"` |

例外表原则: ①豁免只免"涨跌幅断言", OHLC 一致性/单位断言不豁免; ②`limit_exempt` 落行级, 下游因子可选择剔除; ③前两类由上市日期+板块**纯推导**(零维护), 后两类靠源/白名单(低频, 允许滞后维护); ④命中例外表之外的越界仍是 failure——例外表收窄告警面, 不放松契约。

### 2.2 TradingCalendarV1 (交易日历)

| 字段 | 类型 | 校验/不变式 | 备注 |
|---|---|---|---|
| cal_date | date | 唯一键 (exchange, cal_date) | |
| exchange | Literal["SSE","SZSE","BJSE"] | | |
| is_open | bool | | |
| prev_trade_date / next_trade_date | date | 物化, 装载期自检链完整 | 消费端禁自算 |
| horizon_end | date | **`horizon_end - today < 90天` → 硬告警** | akshare 静态日历只到 20261231 前科; 把"日历尽头"从隐患变成可断言字段 |
| source/fetched_at | — | | |

双源对账契约: akshare(新浪, 主用)与 tushare 代理逐日比对, **任一不一致日 → fail-closed 拒发布**(ZhuLinsen 日历 fail-open 当开市继续跑 = 反例); 对账结果落 run_manifest。

### 2.3 AuctionV1 (集合竞价)

| 字段 | 类型 | 校验/不变式 | 备注 |
|---|---|---|---|
| symbol / trade_date | 同上 | | |
| auction_time | **datetime(精确到秒, 带时区)** | ∈ [09:15,09:25] ∪ [14:57,15:00] | **禁降级为 date**——SEC accepted_date 被截断为 date 的反例(§3.1)是本字段的判例 |
| phase | Literal["opening_auction","closing_auction"] | 由 auction_time 推导且必须一致 | auction_close 口径审计前科(竞价额占全天 19-26%, 疑非 3 分钟窗): phase 显式化让口径可断言 |
| match_price | float | >0; 开盘竞价 phase 下 = 当日 open (对 DailyBarV1 跨表对账) | |
| match_volume / match_amount | int(股)/float(元) | ≤ 当日 volume/amount | 跨表不变式 |
| is_partial | bool | 盘中拉取时必须=true | 接"交易日收盘前禁裸跑"护栏; 落缓存禁 is_partial=true 行 |
| source/fetched_at | — | | |

实施顺序与成本: 共通骨架+日历(最小)0.5 天 → 日线 0.5-1 天 → 竞价 0.5 天; 全程包在 `skills/long-term-alpha-system/data_adapter.py` 与 factor_factory 拉取入口外面, 不动 parquet 存量。合计 **1.5-2.5 天**。验收: ①三表自检 harness 全绿; ②毒样本负向测试(喂未来日期/错单位/盘中部分 bar)全部被拒; ③PIT as-of 断言=0 违例; ④涨跌停例外表正反测试——科创板上市第 3 日 +35% 行必须通过且带 limit_exempt 标记, 普通主板日 +25% 行必须 failure。

---

## 3. G4-a: SEC/FRED 官方源的公告日 PIT 处理 (必补盲区, 卡自白 31/33 provider 未读的最高价值部分)

### 3.1 SEC provider: 全仓真正的 PIT 半成品, 三个机制+两个反面

**机制一: 公告三日期分离** (`openbb_platform/providers/sec/openbb_sec/models/company_filings.py#L96-L112,L284-L307@1c74893140`)
- 每条 filing 同时携带: `report_date`(财政期末=reportDate)、`filing_date`(申报日=filingDate)、`accepted_date`(EDGAR 受理时间戳=acceptanceDateTime, 源头为**秒级 datetime**)。
- transform 段用 filing_date(而非 report_date)做 start/end 过滤(`#L304-L307`)——**过滤锚点=可得性日期, 不是财政期末**。这正是 TradingAgents 卡 fiscalDateEnding 锚点反例的正解。
- **反面①(关键)**: 标准模型层把 accepted_date 一律截断成 date——`openbb_platform/core/openbb_core/provider/standard_models/company_filings.py#L39-L43@1c74893140` 的日期校验器对 filing_date 与 accepted_date 统一 `.date()` 化。EDGAR 受理时间的盘中/盘后区分(收盘前 vs 收盘后申报, 对事件研究是两个交易日)在平台层被丢弃。**我方规格: 公告类表的 `available_at` 必须是带时区 datetime, 校验器禁降精度**(与 FinGPT 卡"时间戳须保留到分钟+收盘前/后切割"同判)。

**机制二: pit_mode 双口径开关** (`openbb_platform/providers/sec/openbb_sec/models/balance_sheet.py#L36-L48,L669-L670@1c74893140`; 实现在 `openbb_platform/providers/sec/openbb_sec/utils/statement_schema/_extraction.py#L594-L687@1c74893140`)
- 财报查询暴露布尔参数 `pit_mode`: True=按**原始申报口径**(as originally reported)取数, False(默认)=按**最新重述口径**。
- 核心算法 compute_ref_filings(机制描述): 对每个报告期末日, 扫描该期所有 XBRL 事实的申报记录, 按表单类型白名单过滤(10-K 族/10-Q 族/6-K 族, `openbb_platform/providers/sec/openbb_sec/utils/statement_schema/_types.py#L9-L20@1c74893140` 的 frozenset 常量, 显式含 `/A` 修正表单), 再按期长天数窗口验真(年报 300-400 天、季报 60-135 天、半年报三窗口, `#L649-L659`)——**用"期长物理边界"防错并期**, 与我方 VWAP 单位物理边界自愈护栏同族思想。然后:
  - pit_mode=True → 每期取**最早** filed 日期为参考申报(原始 vintage);
  - pit_mode=False → 取**最晚** filed, 但带 **450 天守卫**(`#L667-L676`): 申报晚于期末 450 天以上的记录剔除, 防止"多年后财报里拖带的对比期数值"冒充该期数据。
- 季度数据默认(非 PIT)会被 `quarterly_ref_filings`(`#L690-L739`)覆写为**10-K vintage 优先**(年报中的重述对比数), pit_mode=True 跳过该覆写以保留 10-Q 原始申报——文档明言"为回测保真, 代价是 Q4 可能与 FY 不勾稽"(`openbb_platform/providers/sec/openbb_sec/utils/company_facts.py#L415-L419@1c74893140`)。
- 值选取的兜底方向(精确描述, `openbb_platform/providers/sec/openbb_sec/utils/statement_schema/_extraction.py#L427-L429@1c74893140`): 找不到恰好等于参考申报日的值时, **优先**回退到不晚于参考申报日的最近一笔(取 before 集合的 max); 但**当不存在任何更早申报时, 会取最早的一笔更晚申报**(min(filings))作为兜底, 且行级 source 会追加 "(fallback)" 标记——即兜底**并非绝对 PIT 安全**, 只是"更晚兜底"被显式标记、可下游过滤。**我方 as_first_reported 规格不照抄**: 无不晚于参考日的申报时**置缺失**(而非取更晚), 缺失计入覆盖率对账。

**机制三: 预披露显式溯源** (`openbb_platform/providers/sec/openbb_sec/models/balance_sheet.py#L36-L40@1c74893140`; `openbb_platform/providers/sec/openbb_sec/utils/company_facts.py#L226-L230@1c74893140`)
- `include_preliminary` 开关允许纳入 8-K 预披露数据(尚未上 10-Q/K 的期), 但输出中该期的 fiscal_period 被强制标注 "(Preliminary)" 后缀——**低置信数据可用但必须带显式标记**。另有 "Suspect Zero" 政策(`#L211-L220`): 推导/填补出的精确零值被改写 source 标记为 imputed-zero——**衍生值与申报值在行级 source 字段可区分**。

**对我方的直接映射** (PEAD/业绩预告/公告面数据适配层, Wave1):
1. 公告事实表增加 `available_at: datetime`(公告披露时刻, 分钟级)——已是红线 2 要求, 本节给出验收强化: **禁 date 降精度**(反面①判例)。
2. 财务快照表增加**双口径查询语义**: `as_first_reported`(=pit_mode True, 回测唯一合法口径) 与 `as_latest`(研究对照用), 二者物理分列或以 revision 链实现——与 qlib 轨道的双时间字段规格拼合, 本节贡献"**每期取最早申报**"的选取规则与"**450 天拖带守卫**"两个可直接抄的算法要点; 兜底规则我方**收紧**于 OpenBB(见机制二: 它无更早时会取更晚+fallback 标记, 我方无更早即置缺失)。
3. 预告/快报(我方 PEAD 用的业绩预告)沿用机制三: `is_preliminary` 标记列 + 正式财报到位后**不覆写**预告行(两行并存, revision 链相连)。
4. 负向测试四件: 喂"期末后 500 天的重述值"断言被 450 天守卫拒; 喂"未来 available_at"断言 as-of 查询不可见; 喂"预告+正式并存"断言 as_first_reported 取预告(若 include_preliminary)且带标记; 喂"仅有晚于参考日申报的期"断言 as_first_reported 置缺失(不复现 OpenBB 的更晚兜底)。
- 成本: 规格并入 qlib 轨道 PIT 设计稿 0.5 天; 适配层断言+负向测试 1 天。

### 3.2 FRED provider: 反面定性——官方有 vintage, 平台亲手扔掉

- FRED/ALFRED 官方 API 每条观测自带 `realtime_start/realtime_end`(该数值的有效版本区间, 即完整修订史)。OpenBB 的 series fetcher 在解析循环里**逐条 pop 丢弃这两个字段**(`openbb_platform/providers/fred/openbb_fred/models/series.py#L156-L157@1c74893140`), 只留最新值。
- **判决: OpenBB 的 FRED 面 = latest-revision-only, 无任何 PIT 能力。** 宏观数据(NFP/CPI 类)修订幅度大, 用其做历史研究即内建前视。若我方未来接宏观面, 必须直连 ALFRED vintage 语义, 不存在"借 OpenBB 现成处理"的选项——记入防重测/防误借注记, 0 成本。
- 次要盘点: FRED 经济日历(公告日期源)是对 fred.stlouisfed.org/releases/calendar 的 **HTML 刮取**(`openbb_platform/providers/fred/openbb_fred/models/economic_calendar.py#L19-L22,L67-L101@1c74893140`, 含表头行日期 ffill 的脆弱解析)——不可作公告时刻权威源。
- 有趣对照: FRED fetcher 的并发聚合对任何单序列异常**全量 raise**(`openbb_platform/providers/fred/openbb_fred/models/series.py#L186-L187@1c74893140`), 与同仓 amake_requests 的吞异常政策相反——**同一代码库两套失败哲学并存**, 佐证"失败政策必须是平台级契约而非各 fetcher 自由发挥"(我方 FetchMeta.failures 强制化的论据)。

---

## 4. G4-b: AI/MCP 面盘点 (卡自白整块缺失, 本轮补齐)

### 4.1 事实盘点 (extensions/mcp_server, FastMCP 架构)

- **总定位: OpenBB 的 "AI agents" 面 = 把 REST 路由自动转译成 MCP 工具目录 + 会话级工具治理。不存在任何"AI 分析/判断/评分"角色——AI 是数据平台的消费者, 平台自身零 LLM 调用。**(与我方"AI 永不改冻结量化分数"边界同构, 但它是"根本没有 AI 写入路径", 更彻底。)
- 转译机制 (`openbb_platform/extensions/mcp_server/openbb_mcp_server/app/app.py#L404-L534@1c74893140`): FastMCP.from_fastapi 把每条 FastAPI 路由变成 OpenAPITool; per-route 定制钩子做四件事——①工具命名=`{category}_{subcategory}_{tool}`(取自 URL 路径段, `#L423-L449`); ②参数与输出 **schema 压缩**(`#L457-L464`, 减 token); ③**描述裁剪**: 默认只保留简述, 完整响应 schema 描述要 `describe_responses` 显式开(`#L466-L473`, settings 默认 False)——工具目录的 token 成本被当一等公民管理; ④注册进 category_index 供发现。
- **会话级最小工具面** (同上 app.py `#L536-L543`; `openbb_platform/extensions/mcp_server/README.md#L295-L309@1c74893140` Tool Discovery 节): 启动先 disable 全部非 admin 工具再选择性 enable; `enable_tool_discovery` 模式下 agent 通过 browse(available_categories)→inspect(available_tools)→activate(activate_tools/category)→deactivate 四步**按需渐进激活**; 工具可见性**逐会话隔离**(多用户安全); 推荐 `--default-categories admin` 让连接时只有发现工具。
- 治理配置面 (README Settings 表 `#L211-L246`): `allowed_tool_categories`(硬白名单) 与 `default_tool_categories`(默认激活) 分离; server/client 双侧 basic auth; `mask_error_details`。
- 提示词/技能资产化: system prompt 以 MCP resource 暴露(`resource://system_prompt`), 客户端**不会自动使用**, 需要 onboarding 指令(README `#L310-L318`)——诚实标注了 MCP 协议在系统提示词分发上的空档; SKILL.md 以 `skill://<name>/SKILL.md` resource 分发, 支持 vendor(claude/cursor)技能目录(README Skills 节)。
- `OBBject.to_llm` (`openbb_platform/core/openbb_core/app/model/obbject.py#L337-L353@1c74893140`): 所谓 LLM 输出=DataFrame→JSON records 字符串(ISO 日期,秒粒度)。**无字段 as-of 标注、无来源标注、无截断策略**; 小瑕疵: 签名声明返回 dict/list, 实际返回 json 字符串(文档/实现脱节, K 类)。

### 4.2 判决 (对我方 Wave3 AI 消融/AI 角色边界)

| 机制 | 判决 | 理由/成本 |
|---|---|---|
| 会话级最小工具面(默认全关+渐进激活) | **借思想, 条件触发**: 我方若在 Wave3 给 AI 复盘/研究 agent 挂工具, 默认工具集=只读+最小, 逐会话激活并落日志 | 决策记录 0 天; 实施时 0.5 天 |
| 工具命名三段法+describe_responses 默认关 | **借**: token 成本当一等公民; 我方 AI 上下文注入已有此意识(mimo max_tokens 教训), upgrade=把"描述裁剪"写进 AI 生成器输入合同 | 0.5 天内 |
| allowed(硬门)/default(默认) 双层类目 | **借**: 对应我方"AI 可见数据面"白名单——AI 只许读 verified snapshot 类目, 冻结分数类目不进 allowed | 并入 Wave3 设计, 0 |
| to_llm 裸 records 序列化 | **反面**: 无 as-of/来源/单位标注的数据喂 AI = 我方 v2 合同的反例; 维持我方"每字段 as-of 标注"(FinGPT 卡同判) | 0 |
| MCP server 整体(FastMCP/REST 转译) | **不借**: 我方无多客户端工具平台需求; AI 链是直连 litellm 的封闭管线 | 0 |
| skills/SKILL.md 资源化分发 | 观察即可(我方 skill 体系已有等价物) | 0 |

**G4 AI 面结论: OpenBB 提供的是"AI 数据供给的工程治理"范本(工具目录/会话隔离/token 预算), 不提供"AI 角色边界"的裁决逻辑(它根本没有 AI 写入路径可言)。对我方 Wave3 的可转移物=上表前三条, 合计不足 1 天。**

---

## 5. amake_requests 吞异常反面清单 → 我方批量拉取审计检查单

事实 (`openbb_platform/core/openbb_core/provider/utils/helpers.py@1c74893140`, 机制描述):
- 单请求 `amake_request`(`#L342-L390`): 无重试、无退避、无熔断; 超时默认 10s 可被用户偏好覆盖(`#L376`); 失败策略完全外包给调用方。
- 批量 `amake_requests`(`#L393-L461`) 的失败语义, 逐条拆解为审计检查单:

| # | OpenBB 行为(锚) | 事故形态 | 我方审计断言 |
|---|---|---|---|
| 1 | 有任一 URL 成功 → 其余异常全部丢弃, 静默返回残缺 results(`#L445-L447,L455-L456`) | 残缺数据无错误信号(7/8 爆盘链根因形态) | 一切聚合拉取必须返回 (results, failures) 双通道; **禁任何 partial=success 路径** |
| 2 | 全失败时也只 raise `exceptions[0]`(`#L456`) | 首个异常掩盖系统性根因(如 30 个 URL 全 403 只见 1 个) | failures 全量落 run_manifest, 告警带失败分布(按 error_type 聚类) |
| 3 | 空响应 `if not result: continue` 静默跳过(`#L449-L450`) | **空载荷≠失败也≠成功**, 覆盖率黑洞: 请求数 vs 结果数永不勾稽 | 强制勾稽断言: `len(urls) == 成功数 + 失败数 + 显式空载荷数`; 空载荷单独计数进覆盖率 |
| 4 | 成功结果 extend 进扁平列表(`#L452-L453`) | **URL→结果身份丢失**: 无法回答"哪只股票缺了" | 批量拉取按 key(symbol/date) 返回 dict, 缺口可指名 |
| 5 | 仅 UnauthorizedError 与 raise_for_status 特权直抛(`#L435-L439`) | 隐性异常分级: 鉴权失败响亮, 数据缺失沉默——与数据完整性优先级颠倒 | 失败分级必须显式枚举(网络/鉴权/解析/空载荷/陈旧), 全部入 failures, 无特权豁免 |
| 6 | 重试/退避缺失(对照 amake_request) | 瞬时抖动被放大为数据缺口 | 我方 data_adapter 统一 timeout/重试/熔断包装维持(akshare 卡同判), 不信任库内部 |
| 7 | 同仓 FRED fetcher 却全量 raise(`openbb_platform/providers/fred/openbb_fred/models/series.py#L186-L187@1c74893140`) | 失败政策 per-fetcher 自由发挥 → 消费端无法建立预期 | 失败政策写进 provider 契约层(共通骨架 FetchMeta), fetcher 无权自定义语义 |

**审计对象(我方代码, 半天)**: `skills/long-term-alpha-system/data_adapter.py`、`skills/stock-analyzer/fix_missing.py`、`factor_factory/scripts/build_advanced_portfolio.py`、B0 竞价/分钟批量落地脚本、O2C 后验行情回填——凡出现"循环拉取+try-continue"处逐一对照上表 7 条; 违例最多的预期是 #3(空载荷不计数) 与 #4(身份丢失)。

---

## 6. VCR 录制回放 → 我方 3 个最脆 akshare 接口的 pytest-recording 模板

### 6.1 OpenBB 的运维资产事实
- 工具: **pytest-recorder**(OpenBB 系自研 vcrpy 包装, `openbb_platform/extensions/devtools/pyproject.toml#L22@1c74893140`, >=0.6.5), 提供 `record_http`(urllib3/aiohttp 拦截)与 `record_curl` 两种 marker——按传输层选拦截器(yfinance 走 curl 通道故用后者)。
- 布局: 每 provider `tests/record/{http|curl}/<测试模块名>/<测试函数名>_{urllib3_v2|curl}.yaml`——**cassette 与测试函数一一对应, 重录后 git diff 即漂移定位**(SEC 17 个 fetcher 全覆盖, `openbb_platform/providers/sec/tests/record/http/test_sec_fetchers/`)。
- 测试本体极薄: 每个 fetcher 测试=固定 params + 调 `fetcher.test()`(即 §1 的 TET 逐段断言在录制流量上离线重放)——**契约断言与网络录制解耦**, 测试只声明输入。
- 脱敏: 模块级 vcr_config fixture 声明 匹配键(method+uri)、header/query/post 参数过滤替换表、响应后处理钩子链(闭包工厂按 header 名或 body 特征改写, 如把整段 HTML 错误页换成占位符)、压缩响应解码、回放可重复(`openbb_platform/providers/yfinance/tests/test_yfinance_fetchers.py#L49-L116@1c74893140`; SEC 版更简: 只滤 User-Agent, `openbb_platform/providers/sec/tests/test_sec_fetchers.py#L40-L49@1c74893140`)。

### 6.2 我方模板 (原创, 用社区标准 pytest-recording/vcrpy, 非其自研包)

目标接口(最脆三件, 卡 relevance 已定): ①`tool_trade_date_hist_sina`(日历/新浪) ②`stock_zh_a_hist`(日线/东财) ③`stock_zh_a_hist_pre_min_em`(竞价分钟/东财)。三者均经 requests 出网, vcrpy 可拦截(tushare 代理为 HTTP API 同理, 后续扩展)。

```python
# tests/providers/conftest.py  (我方新文件)
import pytest

@pytest.fixture(scope="module")
def vcr_config():
    return {
        "record_mode": "none",              # CI 恒离线; 重录用命令行开关
        "match_on": ["method", "host", "path", "query"],
        "filter_headers": [("Cookie", "MOCK"), ("User-Agent", None)],
        "filter_query_parameters": [("token", "MOCK_TOKEN")],   # tushare 代理 key
        "decode_compressed_response": True,
        "allow_playback_repeats": True,
    }

# tests/providers/test_akshare_contracts.py  (每接口一测: 冻结契约断言跑在 cassette 上)
import pytest

@pytest.mark.vcr
def test_calendar_sina_contract(data_adapter):
    df = data_adapter.fetch_trade_calendar()
    assert list(df.columns) == FROZEN_CALENDAR_COLUMNS          # 列名冻结
    assert df["trade_date"].is_monotonic_increasing
    assert (df["trade_date"].max() - TODAY).days >= 90          # 日历尽头护栏

@pytest.mark.vcr
def test_daily_em_contract(data_adapter):
    df = data_adapter.fetch_daily("600000", "20240102", "20240110")
    assert FROZEN_DAILY_DTYPES == dict(df.dtypes.astype(str))   # dtype 冻结
    assert (df["成交量"] * 100 <= df["成交额"] / df["收盘"] * 1e4).any() is not None
    # ↑ 单位量级断言按我方冻结契约写死(手/股, 元), 破裂=上游漂移

@pytest.mark.vcr
def test_auction_pre_min_contract(data_adapter):
    df = data_adapter.fetch_auction_min("600000", "20240102")
    assert df["时间"].str.match(r"09:1[5-9]|09:2[0-5]").any()   # 竞价窗口在场
```

运维流程(写进升级前探针): 升级 akshare 前 `pytest --record-mode=rewrite tests/providers && git diff --stat tests/cassettes/` → cassette diff 即字段/形状漂移清单; CI 日常 `--block-network` 强制离线回放。验收条件: ①断网环境三测全绿; ②人为改 cassette 列名, 三测必须红(测试剧场自检, akshare 1268 接口 2 断言反例); ③cassette 中 grep 不到真实 token。成本: **半天**(接口已在 data_adapter 收口, 只补测试与 cassette)。

---

## G 条目闭合声明

- **G4 (OpenBB AI/MCP 面整块缺失 + SEC/FRED 官方源未读) — 闭合。**
  - AI/MCP 半: mcp_server 扩展的 app/service/settings/README 全读, AI 角色边界机制盘点+判决表见 §4(结论: 治理范本可借三小件, 无 AI 写入路径可裁决)。
  - PIT 半: SEC provider 读穿至 statement_schema/_extraction 层, 得 pit_mode 双口径/450 天守卫/预披露标注三机制+accepted_date 截断反面(§3.1); FRED 读 series/economic_calendar, 定性 latest-only 无 PIT(§3.2)。卡预言"SEC provider 可能有公告日 PIT 处理"**证实且超预期**(不止公告日, 有整套 vintage 选取算法)。
- **G10 (manifest 与卡的 AGPL 结论不同步) — 闭合。** manifest 侧勘误已写入 `manifest/repos.json` 的 manifest_amendments(本轮核实在案), 按 G10 定义闭合成立; 本报告 §0 另完成卡自白遗留的"LICENSE 全文逐条法务级复核"(全文机器 diff, 无附加条款, 含 desktop/ 混合许可勘误)。red_lines.md 第 3 条追加勘误行系**超出 G10 范围的附加建议**(待用户拍板, 冻结文件不擅动), 不影响闭合判定——与 §0 表述一致。
- 顺带部分推进(非本轨道 G 编号): 议程 §3.3 的 TET 设计稿、VCR 模板、amake_requests 反面清单三项交付物全部落地(§2/§6/§5)。

## coverage_confession

- **没读**: 其余 29 个 provider(fmp/intrinio/tiingo 等, 与 PIT/我方无增量预期); package_builder.py 本轮零投入(维持卡结论); cli/desktop/ 全目录; mcp_server 的 auth.py 实现细节与 9 个 tests 文件正文(只读了文件清单); logging 遥测落点(卡自白项, 本轮仍未核); technical/quantitative 算法实现; SEC 的 frames.py 只扫了排序行为(其 frame API 路径与 pit_mode 主链路无关), form4.py/nport 未读; FRED rate_limiter 实现未读(只见引用)。
- **不确定**: ①pit_mode 是 2026 年新代码(v4.7.x 系), 社区实战检验程度未知——450 天守卫等参数是否有 issue 讨论支撑未查, 引用时按"机制思想"而非"验证过的默认值"采信; ②"1. Definitions." 编号异常我判为渲染搬运痕迹, 法律上无效果的判断基于全文词级等同这一事实, 非律师意见; ③record_curl 与我方 requests 栈的差异未实测(我方模板选了社区 pytest-recording, 规避该不确定性); ④MCP per-session 隔离的并发实现(FastMCP 内部)未验证, 判决仅依 README 声明+app.py 装配逻辑。
- **取数通道**: 全部文件为 raw.githubusercontent 冻结 SHA 直取, git tree API truncated=false, 无二手转述。

## Wave 映射

| 产出 | 映射 | 成本 |
|---|---|---|
| §2 三张标准模型设计稿(共通骨架+日线/日历/竞价, extra='forbid'+双通道+PIT 验收) | **Wave1 数据可信**(=Phase4 provider contract 底稿) | 1.5-2.5 天 |
| §3.1 公告 PIT 规格增量(available_at 禁降精度/as_first_reported 口径/450 天拖带守卫/预告不覆写+标记/三件负向测试) | **Wave1 数据可信**(并入 qlib 轨道双时间规格) | 0.5 天规格+1 天断言 |
| §3.2 FRED latest-only 定性 | Wave1 防误借注记 | 0 |
| §5 批量拉取七条审计检查单+审计对象清单 | **Wave1 数据可信**(告警管道已有, 挂断言即可) | 0.5 天 |
| §6 VCR 三接口模板+重录 diff 探针流程 | **上游迁移项目**(akshare 升级前探针; 亦服务 Wave1) | 0.5 天 |
| §4.2 AI 工具面三借三不借判决表(最小工具面/描述裁剪/allowed 双层类目) | **Wave3 AI 消融**(设计输入, 非独立工期) | 决策记录 0; 实施期 <1 天 |
| §1.2 逐文件判决表+阶段隔离断言思想 | **Wave2 组合风险契约**间接(装配期完整性校验模式可移植到策略装配) | 随 §2 落地 |
| §0 许可判决+红线勘误建议 | 治理(红线 3 收口) | 用户拍板 1 行 |
