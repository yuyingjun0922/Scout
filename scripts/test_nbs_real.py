#!/usr/bin/env python
"""
scripts/test_nbs_real.py — NBSCollector (V1) 真实网络烟囱测试

默认写 data/test_knowledge.db。加 --prod-db 写主库。

手工跑：
    python scripts/test_nbs_real.py
    python scripts/test_nbs_real.py --months 3
    python scripts/test_nbs_real.py --indicator PMI
    python scripts/test_nbs_real.py --prod-db
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.data_adapters.nbs import NBSCollector  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="NBS (V1) real-network smoke test")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument(
        "--indicator",
        default=None,
        choices=list(NBSCollector.INDICATORS.keys()),
        help="单个指标；默认全采 PMI/社融/M2",
    )
    parser.add_argument(
        "--prod-db",
        action="store_true",
        help="写 data/knowledge.db 主库（默认写 data/test_knowledge.db）",
    )
    args = parser.parse_args()

    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name

    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        indicators = [args.indicator] if args.indicator else None
        collector = NBSCollector(db=db, indicators=indicators)

        print(f"[V1 NBS] months={args.months} indicators={collector.indicators} db={db_name}")
        print("[fetch] AkShare ≥3s/call，预计 ~10s")

        units = collector.collect_recent(months=args.months)

        if not units:
            print("[warn] no data returned. 查 agent_errors：")
            print(
                f"  sqlite3 {db_path} \"SELECT error_type, error_message FROM agent_errors "
                f"WHERE agent_name='nbs_v1' ORDER BY occurred_at DESC LIMIT 10\""
            )
            return 1

        # 按指标聚合展示
        by_indicator = {}
        for u in units:
            c = json.loads(u.content)
            by_indicator.setdefault(c["indicator"], []).append(c)

        for indicator, rows in by_indicator.items():
            rows.sort(key=lambda x: x["period"])
            print(f"\n[{indicator}] {len(rows)} rows (value_kind={rows[0]['value_kind']})")
            for r in rows:
                mom = f" (mom={r['mom_change']:+.2f})" if r["mom_change"] is not None else ""
                print(f"  {r['period']}: {r['value']}{mom}")

        added = collector.persist_batch(units)
        print(f"\n[db] persisted {added} new rows (duplicates skipped)")

        total = db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='V1'"
        )["n"]
        print(f"[db] total V1 rows in {db_name}: {total}")

        errors = db.query(
            "SELECT error_type, COUNT(*) AS n FROM agent_errors "
            "WHERE agent_name='nbs_v1' GROUP BY error_type"
        )
        if errors:
            print("[errors] agent_errors summary:")
            for r in errors:
                print(f"  {r['error_type']}: {r['n']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
