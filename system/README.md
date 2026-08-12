# 公开核心源码

本目录保存选股系统的可公开核心代码快照，用于说明和复核系统如何形成候选、如何评价策略，以及如何限制 GitHub 只接收公开结果。

它不包含任何本机行情、财务、公告、推荐数据库、历史逐股记录、AI 原始输出、凭据或缓存。运行时路径通过环境变量或用户目录解析，凭据只从环境变量或本机私密文件读取。

主要模块：

- `src/orchestrator/`：时点数据、短线三组影子策略、事件策略、推荐仓、发布合同与安全闸门
- `src/factor_factory/`：历史时点股票池、因子面板、成本模型和因子晋级审计
- `src/stock_analyzer/`：现有基准策略及回测适配层
- `tests/orchestrator/`：关键行为和完整性回归测试

建议先阅读仓库根目录的 `docs/ARCHITECTURE.md` 和 `docs/dual-track-implementation.md`。本目录是研究与观察系统，不包含自动下单功能。

## 本地验证

在仓库根目录运行：

```bash
PYTHONPATH="system/src/orchestrator:system/src/stock_analyzer" \
python3 -m unittest discover -s system/tests/orchestrator -p 'test_*.py'
```

需要 Python 依赖时可参考 `requirements.txt`。外部数据凭据不随仓库提供。
