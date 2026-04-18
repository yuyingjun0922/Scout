#!/usr/bin/env python
"""
scripts/test_signal_collector_real.py — Step 7 真 Ollama/Gemma 烟囱测试

前置：
    1. Ollama 运行中：http://localhost:11434  （`ollama serve`）
    2. Gemma 模型已拉取：`ollama pull gemma4:e4b`（或 config 指定的模型）

默认写 data/test_knowledge.db，加 --prod-db 写主库。

用法：
    python scripts/test_signal_collector_real.py
    python scripts/test_signal_collector_real.py --model gemma3:4b
    python scripts/test_signal_collector_real.py --case supportive
    python scripts/test_signal_collector_real.py --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.signal_collector import SignalCollectorAgent  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


CASES: Dict[str, Dict[str, str]] = {
    "supportive": {
        "source": "D1",
        "title": "国务院推进智能网联汽车发展规划",
        "published_date": "2026-04-15",
        "raw_text": (
            "国务院印发《新能源汽车产业发展规划（2025-2030）》，大力推进智能网联"
            "汽车发展，加快充电基础设施建设，对符合条件的车企给予税收减免支持。"
            "政策旨在推动我国新能源汽车产业高质量发展，鼓励技术创新与产业协同。"
        ),
    },
    "restrictive_hard": {
        "source": "D1",
        "title": "工信部严禁钢铁新增产能",
        "published_date": "2026-04-10",
        "raw_text": (
            "工信部发文严禁钢铁行业新增产能，对违规产能予以取缔，已备案但未开工"
            "项目须限期整改。文件同时要求各地不得以任何形式变相增加钢铁产能，"
            "违反规定的企业将被列入失信名单。"
        ),
    },
    "mixed": {
        "source": "D1",
        "title": "发改委对光伏行业下达新规",
        "published_date": "2026-04-12",
        "raw_text": (
            "发改委对光伏行业下达新规：对低端组件产能进行调控，鼓励 N 型高效"
            "电池技术投入研发，对创新企业给予补贴。政策体现了产业升级的双向"
            "思路——淘汰落后、培育先进。"
        ),
    },
    "multi_interp": {
        "source": "V3",
        "title": "海关总署2026年3月数据",
        "published_date": "2026-04-08",
        "raw_text": (
            "海关总署发布 2026 年 3 月数据：半导体设备进口额同比上升 18.2%，"
            "出口额同比下降 4.5%。具体子项中，光刻机进口增长显著，存储芯片"
            "出口出现回落。"
        ),
    },
    "neutral": {
        "source": "V1",
        "title": "国家统计局3月PMI数据",
        "published_date": "2026-04-01",
        "raw_text": (
            "国家统计局发布 2026 年 3 月制造业采购经理指数（PMI）为 50.8，"
            "环比上升 0.3 个百分点，已连续 3 个月位于荣枯线以上。主要分项中，"
            "生产指数 52.1、新订单指数 51.4。"
        ),
    },
    "low_conf": {
        "source": "D4",
        "title": "arXiv 论文：A Novel Approach to Battery Electrolyte",
        "published_date": "2026-04-10",
        "raw_text": (
            "We propose a novel electrolyte formulation for lithium-ion batteries "
            "that reduces thermal runaway risk by 18% in accelerated tests. "
            "Further validation in commercial cells is needed."
        ),
    },
}


def ping_ollama(host: str) -> bool:
    """看 Ollama 是否在 host 上响应。"""
    try:
        import httpx
        r = httpx.get(f"{host}/api/tags", timeout=3.0)
        return r.status_code == 200
    except Exception as e:
        print(f"[ping] {type(e).__name__}: {e}")
        return False


def list_models(host: str):
    try:
        import httpx
        r = httpx.get(f"{host}/api/tags", timeout=5.0)
        data = r.json()
        return [m.get("name") for m in data.get("models", [])]
    except Exception:
        return []


def run_case(agent: SignalCollectorAgent, name: str, case: Dict[str, str], verbose: bool) -> bool:
    print(f"\n{'=' * 72}")
    print(f"[case] {name}")
    print(f"  source={case['source']}  title={case['title']}")
    print(f"  raw_text (前 80 字): {case['raw_text'][:80]}...")
    print(f"  {'─' * 60}")

    unit = agent.run(
        raw_text=case["raw_text"],
        source=case["source"],
        title=case["title"],
        published_date=case["published_date"],
    )

    if unit is None:
        print(f"  [fail] agent.run returned None (查 agent_errors)")
        return False

    content = json.loads(unit.content)
    print(f"  [ok] policy_direction = {unit.policy_direction}")
    print(f"       mixed_subtype    = {unit.mixed_subtype}")
    print(f"       credibility      = {unit.source_credibility}")
    print(f"       industries       = {unit.related_industries}")
    print(f"       category         = {unit.category}")
    print(f"       rules_override   = {content['rules_override']}")
    print(f"       raw_confidence   = {content['raw_confidence']:.2f}")
    print(f"       summary          = {content['summary'][:80]}")
    if verbose:
        print(f"       reasoning        = {content['reasoning'][:200]}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="SignalCollectorAgent real-network smoke test")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--model", default="gemma4:e4b")
    parser.add_argument(
        "--case", choices=list(CASES.keys()) + ["all"], default="all"
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--prod-db", action="store_true")
    args = parser.parse_args()

    # ── Ollama 健康检查 ──
    print(f"[ping] {args.host} ...")
    if not ping_ollama(args.host):
        print()
        print(f"[FAIL] Ollama 不可达。")
        print(f"  1. 启动: ollama serve")
        print(f"  2. 拉模型: ollama pull {args.model}")
        print(f"  3. 验证: curl {args.host}/api/tags")
        return 2

    models = list_models(args.host)
    print(f"[ollama] 可用模型: {models}")
    if args.model not in models and not any(args.model in m for m in models):
        print(
            f"[warn] 模型 {args.model!r} 不在列表里。尝试 "
            f"`ollama pull {args.model}`，或改 --model 指向已有模型。"
        )

    # ── DB ──
    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name
    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        agent = SignalCollectorAgent(
            db=db,
            ollama_host=args.host,
            model=args.model,
            timeout=args.timeout,
        )

        cases_to_run = [args.case] if args.case != "all" else list(CASES.keys())

        successes = 0
        for name in cases_to_run:
            ok = run_case(agent, name, CASES[name], args.verbose)
            if ok:
                successes += 1

        print(f"\n{'=' * 72}")
        print(f"[summary] {successes}/{len(cases_to_run)} case(s) succeeded")

        # ── llm_invocations 快照 ──
        llm_rows = db.query(
            "SELECT prompt_version, model_name, tokens_used "
            "FROM llm_invocations WHERE agent_name='signal_collector' "
            "ORDER BY id DESC LIMIT 10"
        )
        if llm_rows:
            print(f"\n[llm_invocations] 最近 {len(llm_rows)} 条:")
            total_tokens = 0
            for r in llm_rows:
                print(
                    f"  {r['prompt_version']}  "
                    f"model={r['model_name']}  tokens={r['tokens_used']}"
                )
                total_tokens += int(r["tokens_used"] or 0)
            print(f"  总 tokens: {total_tokens}（Gemma 本地，cost_cents=0）")

        # ── agent_errors 快照 ──
        err_rows = db.query(
            "SELECT error_type, COUNT(*) AS n FROM agent_errors "
            "WHERE agent_name='signal_collector' GROUP BY error_type"
        )
        if err_rows:
            print(f"\n[agent_errors] 本次 signal_collector 错误:")
            for r in err_rows:
                print(f"  {r['error_type']}: {r['n']}")
        else:
            print(f"\n[agent_errors] signal_collector 零错误")

        return 0 if successes == len(cases_to_run) else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
