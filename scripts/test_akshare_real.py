#!/usr/bin/env python
"""
scripts/test_akshare_real.py — AkShare adapter 真实网络验证

不纳入 pytest（需要真实网络 + 真实 AkShare 调用）。

手工跑：
    python scripts/test_akshare_real.py
    python scripts/test_akshare_real.py --symbol 300750 --days 3
    python scripts/test_akshare_real.py --symbol 600519 --days 7 --db /tmp/test.db

默认测试 600519（贵州茅台）最近 7 天。
写入 data/knowledge.db 的 info_units 表（source=S4）。
"""
import argparse
import json
import sys
from pathlib import Path

# 让脚本能 import 项目模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.data_adapters.akshare_wrapper import AkShareCollector  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AkShare adapter real-network smoke test")
    parser.add_argument("--symbol", default="600519", help="A 股代码，默认 600519 贵州茅台")
    parser.add_argument("--days", type=int, default=7, help="采集最近 N 天，默认 7")
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "knowledge.db"),
        help="目标 SQLite 路径，默认 data/knowledge.db",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        collector = AkShareCollector(db=db, symbols=[args.symbol])
        print(f"[fetch] symbol={args.symbol} days={args.days}")
        units = collector.collect_recent(days=args.days)

        if not units:
            print("[warn] no data returned. Possible causes:")
            print("  - 网络未通 / AkShare 服务异常")
            print("  - 股票代码不存在或停牌")
            print("  - 已写入 agent_errors 表，可查：")
            print(f"    sqlite3 {db_path} \"SELECT * FROM agent_errors WHERE agent_name='akshare_s4' ORDER BY occurred_at DESC LIMIT 5\"")
            return 1

        print(f"[ok] got {len(units)} info_units:")
        for u in units[:5]:
            c = json.loads(u.content)
            print(f"  id={u.id} date={c['trading_date']} close={c['close']} industries={u.related_industries}")

        added = collector.persist_batch(units)
        print(f"[db] persisted {added} new rows (duplicates skipped)")

        total = db.query_one("SELECT COUNT(*) AS n FROM info_units WHERE source='S4'")["n"]
        print(f"[db] total S4 rows now: {total}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
