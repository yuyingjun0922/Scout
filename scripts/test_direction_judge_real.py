#!/usr/bin/env python
"""
scripts/test_direction_judge_real.py — Step 9 真 Gemma 烟囱

读 test_knowledge.db（默认）或 knowledge.db（--prod-db），生成周报或执行
跨信号验证，真连 Ollama 出 AI 分析。

前置：
    1. Ollama 运行中：http://localhost:11434（`ollama serve`）
    2. 模型已拉：`ollama pull gemma4:e4b`
    3. test_knowledge.db 至少有一点 D1/D4/V1 数据（先跑 test_govcn_real / test_paper_real）

用法：
    python scripts/test_direction_judge_real.py                     # 默认：半导体 行业周报
    python scripts/test_direction_judge_real.py --industry 新能源汽车
    python scripts/test_direction_judge_real.py --task weekly_paper
    python scripts/test_direction_judge_real.py --task cross_signal --industry 半导体
    python scripts/test_direction_judge_real.py --no-gemma          # 纯数据，不调 LLM
    python scripts/test_direction_judge_real.py --no-save           # 不落盘 reports/
    python scripts/test_direction_judge_real.py --prod-db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.direction_judge import DirectionJudgeAgent  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


def ping_ollama(host: str) -> bool:
    try:
        import httpx
        r = httpx.get(f"{host}/api/tags", timeout=3.0)
        return r.status_code == 200
    except Exception as e:
        print(f"[ping] {type(e).__name__}: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="DirectionJudgeAgent real-network smoke test")
    parser.add_argument("--task", choices=["weekly_industry", "weekly_paper", "cross_signal"],
                        default="weekly_industry")
    parser.add_argument("--industry", default="半导体",
                        help="weekly_industry / cross_signal 用；传 '*' 遍历 active watchlist")
    parser.add_argument("--top-n", type=int, default=10,
                        help="weekly_paper 排行榜长度")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--model", default="gemma4:e4b")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--no-gemma", action="store_true",
                        help="不调 Gemma，只出数据部分")
    parser.add_argument("--no-save", action="store_true",
                        help="不写 reports/ 目录")
    parser.add_argument("--prod-db", action="store_true")
    parser.add_argument("--preview-chars", type=int, default=2000,
                        help="截屏输出长度（完整周报会落盘）")
    args = parser.parse_args()

    # ── Ollama 健康 ──
    gemma_online = False
    if not args.no_gemma:
        print(f"[ping] {args.host} ...")
        gemma_online = ping_ollama(args.host)
        if gemma_online:
            print(f"[ollama] online，model={args.model}")
        else:
            print(f"[ollama] 不可达 — 周报将降级（banner 代替 AI 分析），继续")

    # ── DB ──
    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name
    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        # 数据健康检查
        stats = db.query(
            "SELECT source, COUNT(*) AS n FROM info_units GROUP BY source ORDER BY source"
        )
        if not stats:
            print()
            print("[warn] DB 空。先跑采集脚本：")
            print("   python scripts/test_govcn_real.py --keyword 半导体")
            print("   python scripts/test_paper_real.py")
            return 1
        print("[db] info_units 按信源分布：")
        for r in stats:
            print(f"  {r['source']}: {r['n']}")

        agent = DirectionJudgeAgent(
            db=db,
            ollama_host=args.host,
            model=args.model,
            timeout=args.timeout,
        )

        use_gemma = not args.no_gemma
        save = not args.no_save

        # ── 分派 ──
        if args.task == "cross_signal":
            print(f"\n[task] cross_signal_validation(industry={args.industry!r})")
            result = agent.cross_signal_validation(args.industry)
            if result is None:
                print("[fail] agent.run 返回 None（查 agent_errors）")
                return 1
            print()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.task == "weekly_industry":
            industry_arg = None if args.industry == "*" else args.industry
            print(f"\n[task] weekly_industry_report(industry={industry_arg!r}, "
                  f"use_gemma={use_gemma}, save={save})")
            report, path = agent.weekly_industry_report(
                industry_name=industry_arg,
                use_gemma=use_gemma,
                save=save,
            )
            print()
            print(report[: args.preview_chars])
            if len(report) > args.preview_chars:
                print(f"\n... (truncated, 完整 {len(report)} 字符)")
            if path:
                print(f"\n[saved] {path}")
        else:  # weekly_paper
            print(f"\n[task] weekly_paper_report(top_n={args.top_n}, "
                  f"use_gemma={use_gemma}, save={save})")
            report, path = agent.weekly_paper_report(
                top_n=args.top_n,
                use_gemma=use_gemma,
                save=save,
            )
            print()
            print(report[: args.preview_chars])
            if len(report) > args.preview_chars:
                print(f"\n... (truncated, 完整 {len(report)} 字符)")
            if path:
                print(f"\n[saved] {path}")

        # ── LLM 记账快照 ──
        llm_rows = db.query(
            "SELECT prompt_version, tokens_used FROM llm_invocations "
            "WHERE agent_name='direction_judge' ORDER BY id DESC LIMIT 5"
        )
        if llm_rows:
            print(f"\n[llm_invocations] direction_judge 最近 {len(llm_rows)} 条:")
            total = 0
            for r in llm_rows:
                print(f"  {r['prompt_version']}  tokens={r['tokens_used']}")
                total += int(r["tokens_used"] or 0)
            print(f"  合计 tokens: {total}（cost_cents=0）")

        # ── 错误快照 ──
        err_rows = db.query(
            "SELECT error_type, COUNT(*) AS n FROM agent_errors "
            "WHERE agent_name='direction_judge' GROUP BY error_type"
        )
        if err_rows:
            print(f"\n[agent_errors] direction_judge 本库累计:")
            for r in err_rows:
                print(f"  {r['error_type']}: {r['n']}")
        else:
            print(f"\n[agent_errors] direction_judge 零错误")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
