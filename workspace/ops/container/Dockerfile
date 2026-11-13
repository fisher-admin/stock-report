# Stock-system production runtime, stage 1/3: orchestrator env (phase 4A, 2026-10-30).
# The three locked Python environments live in images at the exact host paths
# the code expects, so a host-side cleanup can no longer delete them (10-20
# incident) and the runtime is rebuilt from this repo.  Built in three stacked
# images (Dockerfile -> Dockerfile.ai -> Dockerfile.qlib) by build.sh so each
# step is one layer with its build tools purged in the same RUN: no builder
# stage, no duplicated venvs, and the build cache is pruned between steps
# (10-30 disk-full incident: three multi-stage attempts left 14 GB of cache).
#   orchestrator: /Users/fisher/.openclaw/venv                      (py3.14)
#   ai:           <workspace>/daily_stock_analysis/venv            (py3.11)  Dockerfile.ai
#   qlib:         <workspace>/factor_factory/qlib_lab/venv         (py3.9)   Dockerfile.qlib
FROM python:3.14-slim
ARG WS=/Users/fisher/.openclaw/workspace
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 UV_NO_CACHE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/pythons \
    TZ=Asia/Shanghai HOME=/Users/fisher STOCK_RUNTIME=container \
    STOCK_SYSTEM_ROOT=/Users/fisher/.openclaw STOCK_SYSTEM_WORKSPACE=$WS STOCK_WORKSPACE=$WS \
    OPENCLAW_WORKSPACE=$WS OPENCLAW_WORKSPACE_DIR=$WS \
    STOCK_SYSTEM_PYTHON=/Users/fisher/.openclaw/venv/bin/python STOCK_PYTHON=/Users/fisher/.openclaw/venv/bin/python
RUN apt-get update && apt-get install -y --no-install-recommends git tzdata ca-certificates libgomp1 procps \
    && rm -rf /var/lib/apt/lists/* && pip install uv==0.9.29
COPY requirements-orchestrator.txt /tmp/
RUN python -m venv /Users/fisher/.openclaw/venv \
    && /Users/fisher/.openclaw/venv/bin/pip install -r /tmp/requirements-orchestrator.txt \
    && rm -rf /tmp/*.txt /Users/fisher/.cache
WORKDIR $WS
