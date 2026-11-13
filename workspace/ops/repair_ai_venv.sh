#!/bin/bash
# Repair the AI-analysis venv for daily_stock_analysis (workspace copy).
#
# Root cause being fixed: the venv's `jiter` package is corrupted
# ("No module named 'jiter.jiter'"), which kills every LiteLLM/OpenAI call.
# Since 2026-06-05 the pipeline has been publishing stale AI fields
# (publish_mode=base_only, pending 20/20).
#
# Usage:  bash ~/.openclaw/workspace/ops/repair_ai_venv.sh
set -euo pipefail

ROOT="$HOME/.openclaw/workspace/daily_stock_analysis"
cd "$ROOT"

PY_BIN="${PYTHON_BIN:-}"
if [ -z "$PY_BIN" ]; then
  for cand in python3.11 python3.12 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY_BIN="$cand"; break; fi
  done
fi
echo "==> 使用解释器: $($PY_BIN --version 2>&1)"

if [ -d venv ]; then
  BACKUP="venv.broken.$(date +%Y%m%d%H%M%S)"
  echo "==> 备份损坏 venv -> $BACKUP"
  mv venv "$BACKUP"
fi

echo "==> 重建 venv"
"$PY_BIN" -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip wheel >/dev/null

echo "==> 安装依赖（requirements.txt）"
pip install -r requirements.txt

echo "==> 强制重装 LLM 关键链（jiter/litellm/openai）"
pip install --force-reinstall --no-cache-dir jiter litellm openai

echo "==> 冒烟测试 1/2: 关键模块导入"
python - <<'PY'
import jiter, litellm, openai, pandas, akshare  # noqa
print("imports OK:",
      "jiter", getattr(jiter, "__version__", "?"),
      "| litellm", getattr(litellm, "__version__", "?"),
      "| openai", getattr(openai, "__version__", "?"))
PY

echo "==> 冒烟测试 2/2: 真实 LLM 调用（读取 .env 配置，约消耗几个 token）"
python - <<'PY'
import os
from pathlib import Path

env_path = Path(".env")
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import litellm

models = [os.environ.get("LITELLM_MODEL") or f"openai/{os.environ.get('OPENAI_MODEL','')}"]
models += [m.strip() for m in (os.environ.get("LITELLM_FALLBACK_MODELS") or "").split(",") if m.strip()]

ok = False
for model in models:
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "回复: OK"}],
            max_tokens=8,
            timeout=60,
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
        )
        print(f"✅ {model} -> {resp.choices[0].message.content!r}")
        ok = True
        break
    except Exception as e:
        print(f"⚠️ {model} 失败: {type(e).__name__}: {e}")

raise SystemExit(0 if ok else 1)
PY

echo ""
echo "🎉 venv 修复完成。建议立即补齐积压的 AI 分析："
echo "   cd $ROOT && source venv/bin/activate && \\"
echo "   python3 bridge_selection_to_analysis.py --source strategy-suite --top 20"
