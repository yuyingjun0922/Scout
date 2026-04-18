#!/usr/bin/env python
"""
scripts/test_govcn_real.py — D1 国务院 gov.cn 真网络烟囱测试

数据源：sousuo.www.gov.cn/search-gov/data（静态 JSON API）

默认写 data/test_knowledge.db。加 --prod-db 写主库。

手工跑：
    python scripts/test_govcn_real.py
    python scripts/test_govcn_real.py --keyword 半导体 --days 7
    python scripts/test_govcn_real.py --keywords 半导体,芯片,人工智能
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.data_adapters.gov_cn import GovCNCollector  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="gov.cn (D1) real-network smoke test")
    parser.add_argument("--keyword", default="半导体", help="单关键词；与 --keywords 二选一")
    parser.add_argument(
        "--keywords",
        default=None,
        help="逗号分隔多关键词，覆盖 --keyword；如 '半导体,芯片,人工智能'",
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--prod-db",
        action="store_true",
        help="写 data/knowledge.db 主库（默认写 data/test_knowledge.db）",
    )
    args = parser.parse_args()

    if args.keywords:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        kws = [args.keyword]

    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name
    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        collector = GovCNCollector(db=db, keywords=kws)

        print(f"[D1 gov.cn] keywords={kws} days={args.days} db={db_name}")
        print(f"[fetch] sousuo.www.gov.cn/search-gov/data (1.5s/keyword)...")

        units = collector.collect_recent(days=args.days)

        if not units:
            print()
            print("[warn] no policies returned. 可能原因：")
            print(f"  - 最近 {args.days} 天无匹配 {kws!r} 的政策")
            print(f"  - gov.cn API 变动 / 反爬")
            print(f"  查 agent_errors：")
            print(
                f"  sqlite3 {db_path} "
                "\"SELECT error_type, substr(error_message, 1, 200) FROM agent_errors "
                "WHERE agent_name='govcn_d1' ORDER BY occurred_at DESC LIMIT 5\""
            )
            return 1

        print(f"\n[ok] got {len(units)} policies (deduped across keywords):")
        for u in units[:10]:
            c = json.loads(u.content)
            print()
            print(f"  📜 {c['title']}")
            print(f"     publisher={c['publisher']} doc={c['doc_number'] or '-'}")
            print(f"     published={c['published_date']} (issued={c['issued_date']})")
            print(f"     subject={c['subject'] or '-'}  category={c['source_category']}")
            print(f"     keyword_hits={c['keyword_hits']}  industries={u.related_industries}")
            print(f"     url={c['url']}")
            summary = c["summary"][:120].replace("\n", " ")
            if summary:
                print(f"     summary: {summary}...")

        added = collector.persist_batch(units)
        print(f"\n[db] persisted {added} new rows (duplicates skipped)")

        total = db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='D1'"
        )["n"]
        print(f"[db] total D1 rows in {db_name}: {total}")

        errors = db.query(
            "SELECT error_type, COUNT(*) AS n FROM agent_errors "
            "WHERE agent_name='govcn_d1' GROUP BY error_type"
        )
        if errors:
            print("\n[errors] agent_errors summary:")
            for r in errors:
                print(f"  {r['error_type']}: {r['n']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
