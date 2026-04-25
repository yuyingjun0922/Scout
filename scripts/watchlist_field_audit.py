"""
watchlist 数据完整度审计 (只读诊断)。

背景: 2026-04-25 发现 d3 维度 (真实缺口) 普遍走默认值 50,
      原因是 watchlist 表的 gap_fillability/gap_analysis 等字段大量 NULL。
      本脚本扫 active 行业,产出每字段填写状态报告 + CSV。

运行:
    python scripts/watchlist_field_audit.py

输出:
    - stdout 摘要 (按字段缺失率 + 按行业完整度)
    - logs/audit/YYYY-MM-DD_watchlist_field_audit.csv

注意:
    - 只读, 不动 watchlist 任何行
    - sub_industries 字段不存在于 schema (CLAUDE.md §5 TD),
      解析自 notes 的 [subs=...] 段
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

DB_PATH = os.environ.get("SCOUT_DB_PATH", r"D:\13700F\Scout\data\knowledge.db")
REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "logs" / "audit"

# 8 个待审计字段 (字段名, 影响维度, 影响等级 high/medium/low, 是否解析自 notes)
FIELDS = [
    ("gap_fillability",    "d3",    "high",   False),
    ("gap_analysis",       "d3",    "high",   False),
    ("sub_industries",     "d3/d5", "medium", True),   # 解析自 notes
    ("sub_market_signals", "d3/d5", "medium", False),
    ("motivation_levels",  "d2",    "high",   False),
    ("motivation_detail",  "d2",    "low",    False),
    ("thesis",             "-",     "medium", False),
    ("kill_conditions",    "-",     "low",    False),
]

IMPACT_BADGE = {"high": "🔴 high", "medium": "🟡 medium", "low": "🟢 low"}

SUGGESTED_ACTION = {
    "gap_fillability":    "手工填写 1-5 整数评分",
    "gap_analysis":       "JSON: 含 light/medium/heavy/critical 之一的对象",
    "sub_industries":     "更新 notes 字段 [subs=A,B,C] 段;或推动 schema 拆分(见 CLAUDE.md §5 TD)",
    "sub_market_signals": "JSON: v1.60 子市场分化信号 (e.g. {sub_winners:[],sub_losers:[]})",
    "motivation_levels":  "JSON 数组: 关键动机标签如 ['1_国家安全','2_技术主权']",
    "motivation_detail":  "JSON: v1.54 动机不确定度判断 (含 uncertainty: low/medium/high)",
    "thesis":             "人工填写投资论点 (1-2 段, 含核心赔率/胜率/时间窗判断)",
    "kill_conditions":    "JSON 数组: 反决定触发条件如 ['政策反转','3 季度业绩连续低于预期']",
}


def check_text_field(v) -> str:
    """TEXT 字段状态判断: filled / null / empty_string / partial"""
    if v is None:
        return "null"
    s = str(v).strip()
    if s == "":
        return "empty_string"
    if s in ("[]", "{}", "null"):
        return "partial"
    return "filled"


def check_int_field(v) -> str:
    """INTEGER 字段状态判断 (gap_fillability)"""
    if v is None:
        return "null"
    return "filled"


def check_subs_from_notes(notes) -> tuple[str, str]:
    """从 notes 解析 [subs=...] 段, 返回 (status, preview)"""
    if not notes:
        return ("null", "")
    m = re.search(r"\[subs=([^\]]*)\]", str(notes))
    if not m:
        return ("null", "(notes 无 [subs=...] 段)")
    content = m.group(1).strip()
    if not content:
        return ("partial", "[subs=]")
    return ("filled", f"[subs={content}]")


def value_preview(v, limit: int = 80) -> str:
    if v is None:
        return ""
    s = str(v).replace("\n", " ").replace("\r", " ")
    return s[:limit]


def audit_one_row(row: dict) -> list[dict]:
    """对一行 watchlist 产出 8 条字段记录"""
    out = []
    for field_name, dim, impact, from_notes in FIELDS:
        if from_notes:
            # sub_industries 从 notes 解析
            status, preview = check_subs_from_notes(row.get("notes"))
        elif field_name == "gap_fillability":
            status = check_int_field(row.get(field_name))
            preview = value_preview(row.get(field_name))
        else:
            status = check_text_field(row.get(field_name))
            preview = value_preview(row.get(field_name))

        action = "已填" if status == "filled" else SUGGESTED_ACTION.get(field_name, "")

        out.append({
            "industry_id": row["industry_id"],
            "industry_name": row["industry_name"],
            "zone": row["zone"],
            "field": field_name,
            "status": status,
            "impact": impact,
            "current_value_preview": preview,
            "suggested_action": action,
        })
    return out


def main() -> int:
    # Windows console UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not os.path.exists(DB_PATH):
        print(f"[ERR] DB not found: {DB_PATH}")
        return 2

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = AUDIT_DIR / f"{date.today().isoformat()}_watchlist_field_audit.csv"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 拉所有 active 行 (按 schema, 'active' 是默认 zone 值)
    cols_to_select = ["industry_id", "industry_name", "zone", "notes"] + \
                     [f for f, _, _, from_notes in FIELDS if not from_notes]
    sql = f"SELECT {','.join(cols_to_select)} FROM watchlist WHERE zone='active' ORDER BY industry_id"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]

    # 也拉一下其他 zone 的 count, 给 stdout 摘要用
    other_zones = conn.execute(
        "SELECT zone, COUNT(*) FROM watchlist WHERE zone != 'active' GROUP BY zone"
    ).fetchall()

    # 产出 CSV 行
    csv_rows = []
    for row in rows:
        csv_rows.extend(audit_one_row(row))

    # 写 CSV (UTF-8 BOM 让 Excel 直接打开不乱码)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "industry_id", "industry_name", "zone", "field", "status",
            "impact", "current_value_preview", "suggested_action",
        ])
        writer.writeheader()
        writer.writerows(csv_rows)

    # ---- stdout 摘要 ----
    n = len(rows)
    bar = "=" * 60
    print(bar)
    print(f"Watchlist 数据完整度审计 - {date.today().isoformat()}")
    print(bar)
    print()
    print(f"总计 active 行业: {n}")
    if other_zones:
        excluded = ", ".join(f"{z}={c}" for z, c in other_zones)
        print(f"(已排除非 active: {excluded})")
    print()

    # 按字段缺失率 (null + empty_string + partial 都算缺)
    print("按字段缺失率排序 (高到低):")
    field_stats = []
    for field_name, dim, impact, _ in FIELDS:
        missing = sum(1 for r in csv_rows if r["field"] == field_name and r["status"] != "filled")
        pct = (missing / n * 100) if n else 0
        field_stats.append((field_name, dim, impact, missing, pct))
    field_stats.sort(key=lambda x: -x[4])
    for field_name, dim, impact, missing, pct in field_stats:
        print(f"  {field_name:22s}: {missing}/{n} ({pct:5.1f}%) 缺失   "
              f"[{IMPACT_BADGE[impact]} impact, 影响维度 {dim}]")
    print()

    # 按行业完整度 (只显示 < 50% = 完成 < 4/8)
    print("按行业完整度排序 (低到高, 只显示完成 <4/8 字段的行业):")
    industry_stats = []
    for row in rows:
        per_industry = [r for r in csv_rows if r["industry_id"] == row["industry_id"]]
        filled = sum(1 for r in per_industry if r["status"] == "filled")
        missing_fields = [r["field"] for r in per_industry if r["status"] != "filled"]
        industry_stats.append((row["industry_name"], filled, missing_fields))
    industry_stats.sort(key=lambda x: x[1])
    shown = 0
    for name, filled, missing_fields in industry_stats:
        if filled >= 4:
            continue
        shown += 1
        print(f"  {name} (完成 {filled}/8 字段):")
        print(f"    缺: {', '.join(missing_fields)}")
    if shown == 0:
        print("  (所有 active 行业完成度都 ≥ 50%, 无需显示)")
    print()

    print(f"CSV 报告: {csv_path}")
    print(f"  总行数: {len(csv_rows)} ({n} 行业 × {len(FIELDS)} 字段)")
    print()
    print("注: sub_industries 解析自 notes 的 [subs=...] 段 "
          "(schema 暂无独立列, 见 CLAUDE.md §5 TD)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
