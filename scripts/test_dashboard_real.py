#!/usr/bin/env python
"""
scripts/test_dashboard_real.py — Step 8 Dashboard 真数据烟囱

读取 test_knowledge.db（默认）或 knowledge.db（--prod-db），为指定行业生成
dashboard 并打印文本格式。如果 DB 里没数据，脚本告诉你先跑 D1 / D4 / V1
采集脚本填充数据。

用法：
    python scripts/test_dashboard_real.py
    python scripts/test_dashboard_real.py --industry 半导体 --days 30
    python scripts/test_dashboard_real.py --industry 新能源汽车
    python scripts/test_dashboard_real.py --all      # 跑 watchlist.zone='active' 全部
    python scripts/test_dashboard_real.py --prod-db  # 读 data/knowledge.db
    python scripts/test_dashboard_real.py --json     # 输出 JSON 而非文本
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.dashboard import (  # noqa: E402
    build_industry_dashboard,
    format_dashboard_as_text,
    get_all_active_industries_dashboards,
)
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


def print_hint_for_empty_db(db: DatabaseManager) -> None:
    """DB 空时给出可操作的提示。"""
    total = db.query_one("SELECT COUNT(*) AS n FROM info_units")["n"]
    if total > 0:
        return
    print()
    print("[warn] DB 里一条 info_units 都没有。先跑采集脚本填数据：")
    print("   python scripts/test_govcn_real.py --keyword 半导体 --days 7")
    print("   python scripts/test_akshare_real.py")
    print("   python scripts/test_nbs_real.py")
    print("   python scripts/test_paper_real.py")


def main() -> int:
    parser = argparse.ArgumentParser(description="Industry Dashboard real-data smoke test")
    parser.add_argument("--industry", default="半导体")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--prod-db", action="store_true")
    parser.add_argument("--all", action="store_true", help="dump all active industries")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    args = parser.parse_args()

    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name
    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        print_hint_for_empty_db(db)

        if args.all:
            dashboards = get_all_active_industries_dashboards(db, days=args.days)
            if not dashboards:
                print()
                print("[info] watchlist 里无 zone='active' 行业。跑单行业：")
                print(f"   python {Path(__file__).name} --industry 半导体")
                return 1
            for d in dashboards:
                if args.json:
                    print(json.dumps(d, ensure_ascii=False, indent=2))
                    print()
                else:
                    print(format_dashboard_as_text(d))
                    print()
            return 0

        dashboard = build_industry_dashboard(args.industry, db, days=args.days)

        if args.json:
            print(json.dumps(dashboard, ensure_ascii=False, indent=2))
        else:
            print(format_dashboard_as_text(dashboard))

        # ── 额外：表级 sanity check ──
        stats = db.query(
            "SELECT source, COUNT(*) AS n FROM info_units GROUP BY source "
            "ORDER BY source"
        )
        if stats:
            print()
            print(f"[sanity] info_units 按信源分布（全库，非窗口）：")
            for r in stats:
                print(f"  {r['source']}: {r['n']}")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
