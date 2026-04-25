"""
半导体设备 watchlist 5 字段填写 (D' 验证实验, 2026-04-25)

背景:
  TD-013 发现 industry_dict / watchlist 数据孤岛问题, 决策推迟到 Phase 2A.
  今天先填半导体设备 5 个不依赖 sub_industries/sub_market_signals 的字段,
  端到端验证"补 watchlist 字段对 RecommendationAgent 输出"的实际影响.

5 个字段:
  1. gap_fillability  = 4 (整体加权,子赛道 EUV=1/刻蚀=5 等)
  2. gap_analysis     = JSON 3 类 (import_substitution + technical + supply_demand)
  3. thesis           = 260 字增强版
  4. kill_conditions  = JSON 4 条
  5. motivation_detail = JSON dominant=[1,2] uncertainty=low policy_base_count=15

预期影响 (诚实评估):
  - 仅 d3 评分会变 (gap_fillability=4 → mapping 100 → +7.5 总分)
  - d2 不变 (受 motivation_drift JSON 引号 latent bug 影响, 今天不修)
  - gap_analysis/thesis/kill_conditions/motivation_detail 不进 scoring path
    (为未来 LLM Stage 2 / 审计 / 推送展示存)

用法:
  python scripts/update_semi_eq_5fields.py            # dry-run, 默认
  python scripts/update_semi_eq_5fields.py --apply    # 真写

备份:
  data/backups/knowledge.db.pre_semi_eq_20260425 (已生成)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get("SCOUT_DB_PATH", r"D:\13700F\Scout\data\knowledge.db")
INDUSTRY = "半导体设备"

# ============ 5 字段值 (硬编码, 用户 2026-04-25 spec) ============

GAP_FILLABILITY = 4

GAP_ANALYSIS = {
    "gap_types": ["import_substitution", "technical", "supply_demand"],
    "scope": "market_specific",
    "import_substitution": {
        "current_import_share": 0.78,
        "current_localization_rate": 0.22,
        "hs_code_watchlist": ["84864000", "84863000", "90230080"],
        "trend_3y": "上升中(2017→2025 从 13% → 22%, 加速期)",
        "scope": "market_specific (CN)",
        "evidence": "海关HS 84864000(集成电路制造设备)国产化率2017年13%→2024年20%→2025年22%(东吴证券)"
    },
    "technical": {
        "benchmark_target": "ASML EUV + AMAT/LAM/KLA/TEL 全套设备",
        "current_level": "刻蚀23%(加速期),薄膜沉积22.7%,CMP/清洗20-28%,离子注入3.1%,光刻~1%",
        "bottleneck": "EUV光源+镜头(Carl Zeiss)+极紫外光刻胶(JSR/TOK)",
        "scope": "market_specific (CN)",
        "evidence": "BIS 2022-10 + 2023-10 + 2024-12 三轮加严; 14nm以下国产化仅~10%"
    },
    "supply_demand": {
        "demand_driver": "AI HBM超级周期 + 长鑫科技IPO拉动扩产",
        "current_state": "2024 DRAM设备+40.2%(195亿美元) / 2025 NAND+42.5%(137亿美元)",
        "trend": "2026预计中国大陆设备销售4414亿元(从2025年3645亿,+21%)",
        "scope": "global+market_specific",
        "evidence": "SEMI数据 / SK海力士HBM3E量产 / 三星HBM4验证中"
    }
}

THESIS = (
    "国产替代+技术主权双动机硬,大基金三期(3440亿,2024-05立项)+ BIS出口管制"
    "(22/23/24三轮加严)叠加。设备国产化率从2017年13%升至2025年22%,28nm"
    "及以上>80%但14nm以下仅~10%。子赛道差异巨大: 光刻~1%(EUV不可破)、"
    "刻蚀23%/薄膜22.7%(加速期)、CMP/清洗/热处理20-28%、离子注入3.1%、"
    "量测<25%。2025-2026三大新催化: 1)AI HBM超级周期(2024 DRAM设备+40.2% / "
    "2025 NAND+42.5%); 2)长鑫科技科创板IPO推进+中芯/华虹28nm扩产; "
    "3)产业整合(中微收购众硅形成\"刻蚀+薄膜+量测+湿法\"四大平台)。"
    "预计2026国产设备新签订单+30-50%(东吴证券)。三地联动验证: A股北方华创/"
    "中微/拓荆/华海清科 + KR SK海力士/三星fab扩产 + US ASML/AMAT/LAM"
    "管制趋势。核心风险: 美管制扩大到成熟制程 / EUV短期不可破 / 长鑫IPO进度"
    "不及预期。3-5年持有逻辑成立。"
)

KILL_CONDITIONS = [
    "美国对华成熟制程 SME (DUV/刻蚀/沉积/CMP) 出口管制全面取消 → 国产替代必要性消失",
    "中芯+华虹+长鑫 SME 采购国产占比 > 80% 持续 4 季度 + 新增产能/现有 < 20% → 进口替代缺口已填满",
    "大基金三期资金到位率 < 30% 持续 12 个月 → 国家意志衰减",
    "中芯+华虹+长鑫 capex 同比连续 4 季下降 > 30% → 下游 fab 需求消失"
]

MOTIVATION_DETAIL = {
    "dominant": [1, 2],
    "candidates": [
        {"level": 1, "confidence": 0.85, "evidence_count": 12, "label": "国家安全"},
        {"level": 2, "confidence": 0.85, "evidence_count": 12, "label": "技术主权"},
        {"level": 4, "confidence": 0.30, "evidence_count": 5, "label": "产业升级"},
        {"level": 7, "confidence": 0.20, "evidence_count": 3, "label": "对外贸易"}
    ],
    "uncertainty": "low",
    "evaluator_model": "claude-opus-4-7",
    "policy_base_count": 15,
    "evaluated_at": "2026-04-25T00:00:00+00:00"
}

# ============ 主流程 ============

UPDATE_SQL = """
UPDATE watchlist SET
    gap_fillability  = ?,
    gap_analysis     = ?,
    thesis           = ?,
    kill_conditions  = ?,
    motivation_detail = ?
WHERE industry_name = ?
"""


def truncate(s, n=80):
    if s is None:
        return "NULL"
    s = str(s).replace("\n", " ").replace("\r", " ")
    return s[:n] + ("..." if len(s) > n else "")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="半导体设备 5 字段更新 (默认 dry-run)")
    ap.add_argument("--apply", action="store_true", help="真写, 默认 dry-run")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"[ERR] DB not found: {DB_PATH}")
        return 2

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT industry_id, industry_name, gap_fillability, gap_analysis, "
        "thesis, kill_conditions, motivation_detail, motivation_drift, "
        "motivation_levels, motivation_uncertainty "
        "FROM watchlist WHERE industry_name = ?",
        (INDUSTRY,)
    ).fetchone()

    if not row:
        print(f"[ERR] {INDUSTRY} not in watchlist")
        return 3

    bar = "=" * 70
    print(bar)
    print(f"半导体设备 watchlist 5 字段更新 (industry_id={row['industry_id']})")
    print(f"模式: {'APPLY (真写)' if args.apply else 'DRY-RUN (只看, 不写)'}")
    print(bar)
    print()

    new_vals = {
        "gap_fillability":   GAP_FILLABILITY,
        "gap_analysis":      json.dumps(GAP_ANALYSIS, ensure_ascii=False),
        "thesis":            THESIS,
        "kill_conditions":   json.dumps(KILL_CONDITIONS, ensure_ascii=False),
        "motivation_detail": json.dumps(MOTIVATION_DETAIL, ensure_ascii=False),
    }

    print("--- before / after 对比 ---")
    for k, new in new_vals.items():
        old = row[k]
        print(f"\n  字段: {k}")
        print(f"    OLD: {truncate(old, 100)}")
        print(f"    NEW: {truncate(new, 100)}")

    print()
    print("--- 关联字段 (本次不改, 仅参考) ---")
    print(f"  motivation_drift   = {row['motivation_drift']!r}  "
          "(注意: JSON 引号 latent bug, d2 仍走 default 75)")
    print(f"  motivation_levels  = {row['motivation_levels']!r}  "
          "(已是 [1,2] 镜像, 不需改)")
    print(f"  motivation_uncertainty = {row['motivation_uncertainty']!r}  "
          "(d2 不读, 不影响评分; 留给未来 stage 2)")

    print()
    print("--- 执行 SQL ---")
    print(UPDATE_SQL.strip())
    print()
    print("参数 (顺序对应 ?):")
    print(f"  1. gap_fillability  = {GAP_FILLABILITY}")
    print(f"  2. gap_analysis     = {len(new_vals['gap_analysis'])} 字符 JSON")
    print(f"  3. thesis           = {len(THESIS)} 字符")
    print(f"  4. kill_conditions  = JSON 数组 {len(KILL_CONDITIONS)} 条")
    print(f"  5. motivation_detail = JSON, dominant=[1,2] uncertainty=low")
    print(f"  6. industry_name    = {INDUSTRY!r}")

    print()
    print("--- 预期 RecommendationAgent 输出影响 ---")
    print("  d3: gap_fillability mapping {4 → 100}, weight 15 → +7.5 总分")
    print("  d2: 不变 (motivation_drift latent bug, 今天不修)")
    print("  其他维度: 不变 (d1/d4/d5/d6 不读这 5 字段)")
    print("  →  002371/688012 总分预期 +5~10 点 (取决于 verify 修正)")

    if args.apply:
        params = (
            GAP_FILLABILITY,
            new_vals["gap_analysis"],
            THESIS,
            new_vals["kill_conditions"],
            new_vals["motivation_detail"],
            INDUSTRY,
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(UPDATE_SQL, params)
            affected = cur.rowcount
            conn.commit()
            print()
            print(f"[APPLIED] UPDATE committed. rows affected = {affected}")
        except Exception as e:
            conn.rollback()
            print(f"\n[ERR] rollback: {e}")
            return 4
    else:
        print()
        print("[DRY-RUN] 未写入. 用 --apply 真写.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
