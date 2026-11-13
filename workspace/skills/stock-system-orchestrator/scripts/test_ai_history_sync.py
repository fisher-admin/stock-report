#!/usr/bin/env python3
"""回归测试：stage4_ai_backfill_loop.sync_ai_history_to_analysis_dir 把 daily_stock_analysis 的
analysis_history（raw_result=to_dict）同步到 orchestrator 的 ai_analysis/{date}.json。

根治 selection↔AI 链路断点（prebreakout AI 生成进 DB 却没同步到 ai_analysis、致 source_hits=0、当日发不出去）。
断言：① 本次新生成记录被同步、{date}.json 含该 code；② since 过滤排除旧记录；③ 不覆盖同日 O2C 条目；
④ 同步后 stage4_ai_publish 的 source_hits>0（命中 prebreakout top20）。

运行：python3 test_ai_history_sync.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage4_ai_backfill_loop as bk  # noqa: E402
import stage4_ai_publish  # noqa: E402
from orchestrator_common import normalize_code  # noqa: E402


def _to_dict(code: str, name: str, score: int) -> dict:
    """模拟 AnalysisResult.to_dict 的最小结构（含 stage4_ai_publish 关心的字段）。"""
    return {
        "code": code,
        "name": name,
        "sentiment_score": score,
        "trend_prediction": "震荡",
        "operation_advice": "观望",
        "decision_type": "hold",
        "confidence_level": "中",
        "analysis_summary": f"{name} 测试分析",
        "dashboard": {"core_conclusion": {"one_sentence": "测试结论"}},
        "success": True,
    }


def _seed_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE analysis_history (id INTEGER PRIMARY KEY, code TEXT, name TEXT, "
        "sentiment_score INTEGER, raw_result TEXT, created_at TEXT)"
    )
    # 旧记录（since 之前，应被排除）：同一 code 的过期分析
    conn.execute(
        "INSERT INTO analysis_history (code,name,sentiment_score,raw_result,created_at) VALUES (?,?,?,?,?)",
        ("600519.SH", "贵州茅台", 99, json.dumps(_to_dict("600519.SH", "贵州茅台", 99)), "2026-01-01 09:00:00"),
    )
    # 本次新记录（since 之后，应被同步）：覆盖旧记录
    conn.execute(
        "INSERT INTO analysis_history (code,name,sentiment_score,raw_result,created_at) VALUES (?,?,?,?,?)",
        ("600519.SH", "贵州茅台", 60, json.dumps(_to_dict("600519.SH", "贵州茅台", 60)), "2026-01-01 15:31:10.123"),
    )
    # 另一只本次新记录
    conn.execute(
        "INSERT INTO analysis_history (code,name,sentiment_score,raw_result,created_at) VALUES (?,?,?,?,?)",
        ("000001.SZ", "平安银行", 55, json.dumps(_to_dict("000001.SZ", "平安银行", 55)), "2026-01-01 15:32:00.000"),
    )
    conn.commit()
    conn.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    orig_root = bk.DAILY_ANALYSIS_ROOT
    orig_dir = stage4_ai_publish.AI_ANALYSIS_DIR
    try:
        da_root = tmp / "daily_stock_analysis"
        _seed_db(da_root / "data" / "stock_analysis.db")
        ai_dir = tmp / "ai_analysis"
        ai_dir.mkdir(parents=True, exist_ok=True)
        # 预置同日 O2C 条目（不同 code），验证同步不覆盖
        date = "20260101"
        (ai_dir / f"{date}.json").write_text(
            json.dumps([{
                "code": "603608.SH",
                "ai_score": 68,
                "ai_source_kind": "o2c_factor_llm_analysis",
                "ai_source_date": date,
                "ai_analysis_date": date,
                "ai_evidence_time": "2026-01-01 18:00:00",
            }],
                       ensure_ascii=False),
            encoding="utf-8",
        )
        bk.DAILY_ANALYSIS_ROOT = da_root
        stage4_ai_publish.AI_ANALYSIS_DIR = ai_dir

        # since = 新记录之前一刻，旧记录(09:00)在之前应排除
        since = time.mktime(time.strptime("2026-01-01 15:00:00", "%Y-%m-%d %H:%M:%S"))
        res = bk.sync_ai_history_to_analysis_dir(["600519.SH", "000001.SZ"], date, since)
        assert res.get("synced") == 2, f"应同步 2 条新记录, got {res}"

        items = json.loads((ai_dir / f"{date}.json").read_text(encoding="utf-8"))
        by_code = {normalize_code(it.get("code")): it for it in items}
        # ① 两只 prebreakout 被同步
        assert "600519" in by_code and "000001" in by_code, by_code.keys()
        # ② since 过滤：取的是 15:31 的新记录(score=60)，不是 09:00 旧记录(score=99)
        assert by_code["600519"]["sentiment_score"] == 60, "应取新记录 score=60 而非旧 99"
        assert by_code["600519"]["ai_source_date"] == date
        assert by_code["600519"]["ai_analysis_date"] == date
        assert by_code["600519"]["ai_evidence_time"].startswith("2026-01-01")
        # ③ 不覆盖同日 O2C 条目
        assert "603608" in by_code and by_code["603608"].get("ai_source_kind") == "o2c_factor_llm_analysis"
        print("✓ 同步新记录 + since 过滤旧记录 + 保留 O2C 条目")

        # ④ source_hits：用 stage4_ai_publish 的读取链路确认命中
        merged_map, _ = stage4_ai_publish.load_candidate_sources(date)
        hits = sum(1 for c in ("600519", "000001") if c in merged_map)
        assert hits == 2, f"load_candidate_sources 应命中 2, got {hits}; map keys={list(merged_map)[:5]}"
        print(f"✓ stage4_ai_publish.load_candidate_sources 命中 prebreakout {hits}/2 (source_hits>0)")

        # ⑤ 推荐日之后才生成的历史回填永久排除，即使文件名和 ai_source_date 伪装成推荐日。
        future_backfill = _to_dict("600000.SH", "浦发银行", 90)
        future_backfill.update(
            {
                "ai_source_date": date,
                "ai_analysis_date": date,
                "ai_evidence_time": "2026-01-02 09:00:00",
            }
        )
        items.append(future_backfill)
        (ai_dir / f"{date}.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        guarded_map, guarded_summary = stage4_ai_publish.load_historical_ai_map_for_trade_date(date)
        assert "600000" not in guarded_map, "未来回填不得进入历史 AI 证据"
        assert guarded_summary.get("excluded_future_or_undated") == 1, guarded_summary
        print("✓ 未来日期回填被永久排除")

        # 边界：无新记录时不报错、不破坏文件
        res2 = bk.sync_ai_history_to_analysis_dir(["600519.SH"], date,
                                                  time.mktime(time.strptime("2026-01-01 23:59:59", "%Y-%m-%d %H:%M:%S")))
        assert res2.get("synced") == 0, res2
        print("✓ 无新记录时安全返回 synced=0")

        print("\nsync_ai_history_to_analysis_dir 回归测试全部通过 ✓")
        return 0
    finally:
        bk.DAILY_ANALYSIS_ROOT = orig_root
        stage4_ai_publish.AI_ANALYSIS_DIR = orig_dir
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
