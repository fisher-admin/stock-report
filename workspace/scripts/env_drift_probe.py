#!/usr/bin/env python3
"""Probe Python environment drift for scheduled stock tasks.

Enforces three locked Python environments:
1. Orchestrator: /Users/fisher/.openclaw/venv/bin/python
2. AI: /Users/fisher/.openclaw/workspace/daily_stock_analysis/venv/bin/python3
3. Qlib: /Users/fisher/.openclaw/workspace/factor_factory/qlib_lab/venv/bin/python
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", "/Users/fisher/.openclaw/workspace"))
OUT = WORKSPACE / "stock_data/03-working/health/env_drift_probe.json"

_orch = os.environ.get("STOCK_SYSTEM_PYTHON") or os.environ.get("STOCK_SYSTEM_VENV")
if _orch and not Path(_orch).name.startswith("python"):
    _orch = str(Path(_orch) / "bin/python")
_ai = os.environ.get("STOCK_SYSTEM_AI_VENV", str(WORKSPACE / "daily_stock_analysis/venv/bin/python3"))
_qlib = os.environ.get("STOCK_SYSTEM_QLIB_VENV", str(WORKSPACE / "factor_factory/qlib_lab/venv/bin/python"))
LOCKED_ENVIRONMENTS = {
    "orchestrator": Path(_orch) if _orch else Path("/Users/fisher/.openclaw/venv/bin/python"),
    "ai": Path(_ai),
    "qlib": Path(_qlib),
}

ORCHESTRATOR_MODULES = [
    "pandas",
    "numpy",
    "tushare",
]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def probe_module(name: str) -> dict:
    try:
        mod = importlib.import_module(name)
        return {
            "ok": True,
            "module": name,
            "file": getattr(mod, "__file__", None),
            "version": getattr(mod, "__version__", None),
        }
    except Exception as exc:
        return {
            "ok": False,
            "module": name,
            "error": f"{type(exc).__name__}: {exc}",
        }


def probe_python_env(name: str, path: Path) -> dict:
    if not path.is_file() or not os.access(path, os.X_OK):
        return {
            "ok": False,
            "name": name,
            "path": str(path),
            "error": "executable_not_found_or_not_executable",
        }
    try:
        proc = subprocess.run(
            [str(path), "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "ok": proc.returncode == 0,
            "name": name,
            "path": str(path),
            "version": (proc.stdout or "").strip(),
            "returncode": proc.returncode,
        }
    except Exception as exc:
        return {
            "ok": False,
            "name": name,
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_probe() -> dict:
    current_exec = Path(sys.executable).resolve()
    disallowed_python = str(current_exec) in ("/usr/bin/python3", "/usr/bin/python", "/bin/python3")

    env_probes = {
        name: probe_python_env(name, path)
        for name, path in LOCKED_ENVIRONMENTS.items()
    }
    module_probes = {name: probe_module(name) for name in ORCHESTRATOR_MODULES}

    # orchestrator and ai must be ok; qlib is checked if present
    envs_ok = (
        env_probes["orchestrator"]["ok"]
        and env_probes["ai"]["ok"]
        and (not LOCKED_ENVIRONMENTS["qlib"].exists() or env_probes["qlib"]["ok"])
    )
    modules_ok = all(m.get("ok") for m in module_probes.values())
    overall_ok = not disallowed_python and envs_ok and modules_ok

    payload = {
        "generated_at": now_str(),
        "current_python": str(current_exec),
        "disallowed_python": disallowed_python,
        "locked_environments": env_probes,
        "modules": module_probes,
        "ok": overall_ok,
    }
    return payload


def main() -> int:
    payload = run_probe()
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(OUT))
    except Exception:
        pass
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
