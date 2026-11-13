# VPS → GitHub 源码镜像

- 目标：公开仓 `fisher-admin/stock-report` 的独立分支 `vps-source`（不触发 main 的 Pages 部署与 CI）。单向：VPS 是源，GitHub 是镜像。公开前脱敏：不含 CLAUDE.md、服务单元与环境样例；文档里的公网 IP 与邮箱替换为占位符；代码里出现公网 IP 或邮箱则整次拦截。
- 机制：`sync.py` 用 rsync 白名单（`workspace.filter`）把源码复制到 `/srv/hermes-stock/git-mirror/stock-report-vps-source/`，
  先过体积闸门（单文件 > 1MB 不入库），再过凭据扫描，然后提交并推送。凭据扫描命中则整次不提交不推送，退出码 2。
- 同步范围：`workspace/` 下的 `factor_factory/{research,scripts,config,configs,contracts,tests,docs,pit}`、`skills/stock-system-orchestrator`、
  `ops`、`scripts`、`fisher-quant`、`docs`、`config`；以及 `tools` 中的两个脚本（`/srv/hermes-stock/deploy` 属运维内容，不同步）。
- 不同步：数据（`stock_data`、`factor_factory/data|outputs`）、分钟缓存、虚拟环境、日志、`*.env`、`.secrets`、`memory/` 与个人文件、
  `stock-report`（它有自己的仓库）、`source/hermes-agent`（上游项目）。
- 凭据：VPS 上 ubuntu 用户已有的 `~/.ssh/stock_report_deploy`（只对 stock-report 有写权限的 deploy key）。
- 调度：systemd `hermes-stock-github-sync.timer`，每 6 小时一次；单元文件的源在 `/srv/hermes-stock/deploy/`。
- 状态：`/srv/hermes-stock/git-mirror/state/last_run.json`（status: ok / blocked_secret_scan / push_failed）。
- 误报放行：核对后把 `路径:规则名` 加进 `secret_scan_allow.txt`。
- 手动运行：`sudo -u ubuntu python3 /srv/hermes-stock/workspace/ops/github_sync/sync.py [--dry-run|--no-push]`
