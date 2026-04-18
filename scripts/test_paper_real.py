#!/usr/bin/env python
"""
scripts/test_paper_real.py — PaperCollector (D4) 真实网络烟囱测试

默认写入 data/test_knowledge.db（独立测试库，避免污染主库）。
加 --prod-db 标志写入 data/knowledge.db。

手工跑：
    python scripts/test_paper_real.py
    python scripts/test_paper_real.py --keyword "semiconductor equipment" --days 30
    python scripts/test_paper_real.py --industry HBM --days 7 --prod-db

注意：
    - arXiv 官方要求 ≥3s 调用间隔（代码已做限流）
    - Semantic Scholar 免费额度 100/5min
    - 不带 API key 可能偶发 429，走 BaseAgent 的 network 重试
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.data_adapters.arxiv_semantic import PaperCollector  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="PaperCollector real-network smoke test")
    parser.add_argument(
        "--keyword",
        default="semiconductor equipment",
        help="自定义关键词；设了此项会忽略 --industry 对应的内置映射",
    )
    parser.add_argument(
        "--industry",
        default="半导体设备",
        help="行业标签（用于给查到的论文打 related_industries）",
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--prod-db",
        action="store_true",
        help="写入 data/knowledge.db 主库（默认写 data/test_knowledge.db）",
    )
    args = parser.parse_args()

    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name

    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        kw_map = {args.industry: [args.keyword]}
        collector = PaperCollector(db=db, industries_keywords=kw_map)

        print(f"[fetch] industry={args.industry} keyword={args.keyword!r} days={args.days}")
        print(f"[fetch] using db={db_path}")
        print("[fetch] arXiv (≥3s interval) + Semantic Scholar (≥3s interval)...")

        units = collector.collect_recent(days=args.days)

        if not units:
            print("[warn] no papers returned. Reasons could be:")
            print("  - 关键词近 N 天无命中")
            print("  - 两源均 429/5xx（查 agent_errors）")
            print(
                f"    sqlite3 {db_path} \"SELECT error_type, error_message FROM agent_errors "
                f"WHERE agent_name='paper_d4' ORDER BY occurred_at DESC LIMIT 5\""
            )
            return 1

        print(f"[ok] got {len(units)} merged papers:")
        for u in units[:5]:
            c = json.loads(u.content)
            citations = c.get("citations")
            source_api = c.get("source_api")
            doi = c.get("doi") or c.get("arxiv_id") or "-"
            print(
                f"  id={u.id} date={u.timestamp[:10]} src={source_api} "
                f"citations={citations} industries={u.related_industries}"
            )
            print(f"    title={c['title'][:80]}")
            print(f"    doi/arxiv={doi}")

        added = collector.persist_batch(units)
        print(f"[db] persisted {added} new rows (duplicates skipped)")

        total = db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='D4'"
        )["n"]
        print(f"[db] total D4 rows in {db_name}: {total}")

        errors = db.query(
            "SELECT error_type, COUNT(*) AS n FROM agent_errors "
            "WHERE agent_name='paper_d4' GROUP BY error_type"
        )
        if errors:
            print("[errors] agent_errors summary:")
            for row in errors:
                print(f"  {row['error_type']}: {row['n']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
