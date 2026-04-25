"""
TD-020 dry-run: 反推三市场差异权重对推荐评分的影响。

只读 + 不部署 + 不改任何表。
读 recommendations.dimensions_detail (含 d1-d6 score 0-100) + 用新权重重算。

权重 (v1.0 反推基线, 用户 2026-04-25 spec):
  A:  d1=15, d2=10, d3=20, d4=20, d5=20, d6=15  (合计 100, 政策驱动适中, 缺口/数据/财务加重)
  KR: d1=12, d2= 8, d3=15, d4=20, d5=30, d6=15  (合计 100, 平衡)
  US: d1= 5, d2= 5, d3=15, d4=15, d5=35, d6=25  (合计 100, 财务驱动)
默认 (空 / 非 A/KR/US): 按 A 权重, 标注 unknown

旧权重 (现行 recommendation_agent.py):
  ALL: d1=15, d2=15, d3=15, d4=10, d5=15, d6=10  (合计 80, 同套权重不区分 market)

输出: docs/td020_dryrun_2026-04-25.md (markdown 表 + 汇总)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("SCOUT_DB_PATH", r"D:\13700F\Scout\data\knowledge.db")
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "docs" / "td020_dryrun_2026-04-25.md"

WEIGHTS_BY_MARKET = {
    "A":  {"d1": 15, "d2": 10, "d3": 20, "d4": 20, "d5": 20, "d6": 15},
    "KR": {"d1": 12, "d2":  8, "d3": 15, "d4": 20, "d5": 30, "d6": 15},
    "US": {"d1":  5, "d2":  5, "d3": 15, "d4": 15, "d5": 35, "d6": 25},
}

OLD_WEIGHTS = {"d1": 15, "d2": 15, "d3": 15, "d4": 10, "d5": 15, "d6": 10}
OLD_TOTAL = sum(OLD_WEIGHTS.values())  # 80
NEW_TOTAL = 100

DEFAULTS = {"d1": 25, "d2": 75, "d3": 50, "d4": 20, "d5": 50, "d6": 50}

LEVEL_A_MIN = 75
LEVEL_B_MIN = 60
LEVEL_CANDIDATE_MIN = 40

LEVEL_RANK = {"A": 4, "B": 3, "candidate": 2, "reject": 1}

SPOTLIGHT_STOCKS = ("002371", "688012", "688082")


def level_from_score(s: float) -> str:
    if s >= LEVEL_A_MIN:
        return "A"
    if s >= LEVEL_B_MIN:
        return "B"
    if s >= LEVEL_CANDIDATE_MIN:
        return "candidate"
    return "reject"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    industries = {
        r[0]: r[1]
        for r in conn.execute("SELECT industry_id, industry_name FROM watchlist")
    }

    market_map = {}
    for r in conn.execute(
        "SELECT stock_code, industry_id, market FROM related_stocks WHERE status='active'"
    ):
        market_map[(r[0], r[1])] = r[2]
    market_fallback = {}
    for r in conn.execute(
        "SELECT stock_code, market FROM related_stocks WHERE status='active'"
    ):
        market_fallback.setdefault(r[0], r[1])

    rows = conn.execute(
        "SELECT id, stock, industry_id, dimensions_detail, total_score, "
        "recommend_level, recommended_at FROM recommendations"
    ).fetchall()

    results = []
    parse_fail = 0

    for r in rows:
        try:
            d = json.loads(r["dimensions_detail"]) if r["dimensions_detail"] else None
            if not isinstance(d, dict):
                raise ValueError("not a dict")
            dims = d.get("dimensions", {})
            verify = (d.get("phase3") or {}).get("delta", 0) or 0
        except (json.JSONDecodeError, TypeError, ValueError):
            parse_fail += 1
            continue

        scores = {}
        for dim in ("d1", "d2", "d3", "d4", "d5", "d6"):
            v = dims.get(dim)
            if isinstance(v, dict) and v.get("score") is not None:
                scores[dim] = v["score"]
            else:
                scores[dim] = DEFAULTS[dim]

        market = market_map.get((r["stock"], r["industry_id"])) or market_fallback.get(r["stock"])
        if market in ("A", "KR", "US"):
            weights_key = market
            market_label = market
        else:
            weights_key = "A"
            market_label = f"unknown({market or 'null'})"

        old_norm = sum(scores[d] * OLD_WEIGHTS[d] for d in scores) / OLD_TOTAL
        new_norm = sum(scores[d] * WEIGHTS_BY_MARKET[weights_key][d] for d in scores) / NEW_TOTAL
        new_total = max(0.0, min(100.0, new_norm + verify))
        delta = round(new_total - r["total_score"], 2)

        results.append({
            "id": r["id"],
            "stock": r["stock"],
            "market_label": market_label,
            "market_actual": market or "null",
            "industry": industries.get(r["industry_id"], "?"),
            "old_total": round(r["total_score"], 2) if r["total_score"] is not None else None,
            "new_total": round(new_total, 2),
            "delta": delta,
            "old_level": r["recommend_level"],
            "new_level": level_from_score(new_total),
            "recommended_at": (r["recommended_at"] or "")[:10],
        })

    results.sort(key=lambda x: -abs(x["delta"]))

    upgrade = sum(1 for x in results if LEVEL_RANK[x["new_level"]] > LEVEL_RANK[x["old_level"]])
    downgrade = sum(1 for x in results if LEVEL_RANK[x["new_level"]] < LEVEL_RANK[x["old_level"]])
    no_change = sum(1 for x in results if x["new_level"] == x["old_level"])

    market_dist = {}
    for x in results:
        a = x["market_actual"]
        market_dist[a] = market_dist.get(a, 0) + 1

    spotlight = sorted(
        [x for x in results if x["stock"] in SPOTLIGHT_STOCKS],
        key=lambda x: (x["stock"], x["recommended_at"]),
    )

    top5_volatility = results[:5]
    worst_drops = sorted(results, key=lambda x: x["delta"])[:5]
    biggest_gains = sorted(results, key=lambda x: -x["delta"])[:5]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        write_markdown(
            f, results, parse_fail, upgrade, downgrade, no_change,
            market_dist, spotlight, top5_volatility, worst_drops, biggest_gains,
        )

    print(f"Done. total={len(results)}, parse_fail={parse_fail}")
    print(f"  upgrade={upgrade}, downgrade={downgrade}, no_change={no_change}")
    print(f"  market_dist={market_dist}")
    print(f"  out: {OUT_PATH}")
    return 0


def write_markdown(f, results, parse_fail, up, down, nc, mdist,
                   spotlight, top5, worst5, gain5):
    f.write("# TD-020 Dry-Run: 三市场差异权重模拟\n\n")
    f.write(f"> **日期**: {datetime.now(timezone.utc).isoformat()[:10]}\n")
    f.write("> **范围**: recommendations 全部 230 条记录\n")
    f.write("> **方法**: 用新权重重算每条记录, **不动数据库, 不部署**\n")
    f.write("> **保留**: phase3 verify delta 作为常量加在新分数上 (verify 与 market 无关)\n\n")

    f.write("## 权重对照\n\n")
    f.write("| 维度 | 旧权重 (所有 market) | A (CN) | KR | US |\n")
    f.write("|---|---|---|---|---|\n")
    for d in ("d1", "d2", "d3", "d4", "d5", "d6"):
        f.write(
            f"| {d} | {OLD_WEIGHTS[d]} | {WEIGHTS_BY_MARKET['A'][d]} | "
            f"{WEIGHTS_BY_MARKET['KR'][d]} | {WEIGHTS_BY_MARKET['US'][d]} |\n"
        )
    f.write(f"| **合计** | **{OLD_TOTAL}** | **{sum(WEIGHTS_BY_MARKET['A'].values())}** | "
            f"**{sum(WEIGHTS_BY_MARKET['KR'].values())}** | "
            f"**{sum(WEIGHTS_BY_MARKET['US'].values())}** |\n\n")

    f.write("## 汇总\n\n")
    f.write(f"- 总记录: {len(results)} 条 (parse_fail: {parse_fail} 条)\n")
    f.write(f"- 升级 (B→A 或 candidate→B 等): **{up}**\n")
    f.write(f"- 降级: **{down}**\n")
    f.write(f"- 级别无变化: **{nc}**\n\n")

    f.write("### Market 分布\n\n")
    f.write("| Market | 记录数 |\n|---|---|\n")
    for m in sorted(mdist.keys()):
        f.write(f"| {m} | {mdist[m]} |\n")
    f.write("\n")

    f.write("## 三只 A 级 spotlight\n\n")
    f.write("| stock | market | industry | recommended_at | old_total | new_total | delta | old_level | new_level |\n")
    f.write("|---|---|---|---|---|---|---|---|---|\n")
    for x in spotlight:
        delta_str = f"{x['delta']:+.2f}" if x['delta'] is not None else "?"
        f.write(
            f"| **{x['stock']}** | {x['market_label']} | {x['industry']} | "
            f"{x['recommended_at']} | {x['old_total']} | {x['new_total']} | "
            f"{delta_str} | {x['old_level']} | {x['new_level']} |\n"
        )
    f.write("\n")

    f.write("## Top 5 变化最大 (绝对值)\n\n")
    _write_table(f, top5)

    f.write("## Top 5 降幅最大 (delta 最负)\n\n")
    _write_table(f, worst5)

    f.write("## Top 5 升幅最大 (delta 最正)\n\n")
    _write_table(f, gain5)

    f.write("## 全 230 条按 |delta| 倒序\n\n")
    _write_table(f, results)


def _write_table(f, items):
    f.write("| stock | market | industry | recommended_at | old_total | new_total | delta | old_level | new_level |\n")
    f.write("|---|---|---|---|---|---|---|---|---|\n")
    for x in items:
        delta_str = f"{x['delta']:+.2f}" if x['delta'] is not None else "?"
        f.write(
            f"| {x['stock']} | {x['market_label']} | {x['industry']} | "
            f"{x['recommended_at']} | {x['old_total']} | {x['new_total']} | "
            f"{delta_str} | {x['old_level']} | {x['new_level']} |\n"
        )
    f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
