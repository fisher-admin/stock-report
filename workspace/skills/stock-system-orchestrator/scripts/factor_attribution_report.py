#!/usr/bin/env python3
"""CLI for factor IC attribution diagnostics (diagnostic_only)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import factor_attribution as fa
import factor_attribution_engine as engine


def resolve_refresh_days(
    *,
    refresh_days: int | None,
    max_days: int | None,
    as_of: str,
    env: dict[str, str] | None = None,
) -> int | None:
    """None = rebuild every panel; 0 = missing panels only; N>0 = last N plus missing.

    Daily latest runs default to 1 (the new session) plus any holes. Full rebuild
    is opt-in via OPENCLAW_FACTOR_ATTR_FULL=1.
    """
    env = env if env is not None else os.environ
    if refresh_days is not None:
        return int(refresh_days)
    if max_days is not None:
        return int(max_days)
    env_max = str(env.get("OPENCLAW_FACTOR_ATTR_MAX_DAYS") or "").strip()
    if env_max.isdigit():
        return int(env_max)
    if env.get("OPENCLAW_FACTOR_ATTR_FULL"):
        return None
    if as_of == "latest":
        return 1
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factor IC attribution report (diagnostic_only)")
    parser.add_argument("--as-of", default="latest", help="YYYYMMDD or latest")
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=None,
        help="rebuild last N day panels plus holes (0=holes only, omit=daily default 1). Never shrinks latest aggregation.",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=None,
        help="deprecated alias of --refresh-days; does not truncate IC sample",
    )
    parser.add_argument("--no-fetch", action="store_true", help="do not call tushare for missing PIT")
    parser.add_argument("--workspace", default="")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else None
    refresh_days = resolve_refresh_days(
        refresh_days=args.refresh_days,
        max_days=args.max_days,
        as_of=args.as_of,
    )
    try:
        result = engine.run_attribution(
            workspace=workspace,
            as_of=args.as_of,
            refresh_days=refresh_days,
            fetch_missing=not args.no_fetch,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    paths = fa.build_paths(Path(args.workspace) if args.workspace else engine._workspace())
    print(
        json.dumps(
            {
                "ok": True,
                "evidence_scope": result["evidence_scope"],
                "as_of": result["coverage"]["as_of"],
                "aggregation_scope": result["coverage"].get("aggregation_scope"),
                "signal_days_ok": result["coverage"]["signal_days_ok"],
                "signal_days_considered": result["coverage"].get("signal_days_considered"),
                "json": str(paths.latest_json),
                "md": str(paths.latest_md),
                "reverse_phase2_trigger": result["reverse_test"]["meets_phase2_trigger"],
                "homology_flag": result["homology"]["flag_same_factor_family"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
