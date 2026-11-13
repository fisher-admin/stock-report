#!/usr/bin/env python3
"""CheckPoint 1: Verify Phase 1 path migration is complete."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Inject env vars as OpenClaw would at runtime.
os.environ.setdefault("STOCK_SYSTEM_ROOT", "/Users/fisher/.openclaw")
os.environ.setdefault("STOCK_SYSTEM_WORKSPACE", "/Users/fisher/.openclaw/workspace")
os.environ.setdefault("STOCK_SYSTEM_VENV", "/Users/fisher/.openclaw/venv")
os.environ.setdefault("STOCK_SYSTEM_PYTHON", "/Users/fisher/.openclaw/venv/bin/python")

from python_env_bootstrap import bootstrap_openclaw_venv

bootstrap_info = bootstrap_openclaw_venv()

from orchestrator_common import (
    _STOCK_ROOT,
    WORKSPACE,
    VENV_DIR,
    PREFERRED_PYTHON,
    CACHE_DIR,
    HEALTH_DIR,
    WORKING_REPO,
    PUBLISHED_REPO,
)

results = {
    "STOCK_SYSTEM_ROOT": str(_STOCK_ROOT),
    "WORKSPACE": str(WORKSPACE),
    "VENV_DIR": str(VENV_DIR),
    "PREFERRED_PYTHON": str(PREFERRED_PYTHON),
    "CACHE_DIR": str(CACHE_DIR),
    "HEALTH_DIR": str(HEALTH_DIR),
    "WORKING_REPO": str(WORKING_REPO),
    "PUBLISHED_REPO": str(PUBLISHED_REPO),
    "bootstrap_info": bootstrap_info,
    "sys_executable": sys.executable,
    "checks": {},
}

# Validate no Hermes path is used.
checks = results["checks"]
for key in ("WORKSPACE", "VENV_DIR", "PREFERRED_PYTHON", "CACHE_DIR", "HEALTH_DIR"):
    val = results[key]
    checks[f"{key}_no_hermes"] = ".hermes" not in val
    checks[f"{key}_exists"] = Path(val).exists()

checks["python_executable_exists"] = Path(results["PREFERRED_PYTHON"]).exists()
checks["all_pass"] = all(checks.values())

def main() -> int:
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if checks["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
