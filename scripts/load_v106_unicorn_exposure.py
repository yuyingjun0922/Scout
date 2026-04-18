"""v1.06 — 独角兽曝光行业冷启动（1 个行业，12 只股票）

Idempotent loader. Safe to re-run. Does NOT mutate existing rows.

Scope:
  - industry_dict: +1 (独角兽曝光, scout_range='active', cyclical=0)
  - watchlist:    +1 (zone='active')
  - related_stocks: +12 (US x10 + JP x1 + HK x1)
    Note: 首次引入 JP / HK 市场（market 字段无 CHECK 约束，可直接写入）

Defaults applied per related_stocks row:
  - discovery_source = 'manual_v106_unicorn'
  - discovered_at = now (UTC)
  - confidence = 'approved'   (人工精挑细选，直接 approved)
  - status = 'active'

Idempotency:
  - industry_dict: PRIMARY KEY(industry) → SELECT-before-INSERT
  - watchlist: UNIQUE(industry_name) → SELECT-before-INSERT
  - related_stocks: UNIQUE(industry, stock_code) → SELECT-before-INSERT
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge.db"

INDUSTRY = "独角兽曝光"
ALIASES = [
    "AI独角兽",
    "OpenAI供应链",
    "Anthropic投资者",
    "SpaceX供应链",
    "Vision Fund",
    "独角兽产业链",
]
DISCOVERY_SOURCE = "manual_v106_unicorn"
CONFIDENCE = "approved"
STATUS = "active"

# (stock_code, stock_name, market, sub_industry|None)
STOCKS: list[tuple[str, str, str, str | None]] = [
    ("MSFT",  "Microsoft",          "US", "OpenAI主投资者/Azure算力"),
    ("GOOGL", "Alphabet",           "US", "Anthropic投资者/TPU"),
    ("AMZN",  "Amazon",             "US", "Anthropic投资者/AWS算力"),
    ("AVGO",  "Broadcom",           "US", "OpenAI ASIC供应链"),
    ("ANET",  "Arista Networks",    "US", "数据中心网络/AI集群"),
    ("COHR",  "Coherent",           "US", "光模块/AI互联"),
    ("VRT",   "Vertiv Holdings",    "US", "数据中心液冷+电源"),
    ("RKLB",  "Rocket Lab USA",     "US", "SpaceX竞品/小火箭"),
    ("ASTS",  "AST SpaceMobile",    "US", "Starlink类卫星互联网"),
    ("ARKK",  "ARK Innovation ETF", "US", "独角兽主题ETF"),
    ("9984",  "SoftBank Group",     "JP", "Vision Fund/OpenAI/ARM"),
    ("00700", "Tencent Holdings",   "HK", "AI独角兽全球投资者"),
]


def upsert_industry_dict(con: sqlite3.Connection, now: str) -> bool:
    """Return True if inserted, False if already present."""
    exists = con.execute(
        "SELECT 1 FROM industry_dict WHERE industry=?", (INDUSTRY,)
    ).fetchone()
    if exists:
        return False
    con.execute(
        """INSERT INTO industry_dict (
               industry, aliases, cyclical, scout_range,
               sub_industries, confidence,
               last_change_reason, last_change_by
           ) VALUES (?, ?, 0, 'active', '[]', 'approved', ?, 'v1.06-unicorn-exposure')""",
        (INDUSTRY, json.dumps(ALIASES, ensure_ascii=False),
         f"v1.06 新增 {INDUSTRY} 行业（OpenAI/Anthropic/SpaceX 等独角兽产业链曝光）"),
    )
    return True


def upsert_watchlist(con: sqlite3.Connection) -> tuple[bool, int]:
    """Return (inserted, industry_id)."""
    row = con.execute(
        "SELECT industry_id FROM watchlist WHERE industry_name=?", (INDUSTRY,)
    ).fetchone()
    if row:
        return False, row[0]
    cur = con.execute(
        """INSERT INTO watchlist (
               industry_name, industry_aliases, zone,
               source_type, early_signal
           ) VALUES (?, ?, 'active', 'manual', 0)""",
        (INDUSTRY, json.dumps(ALIASES, ensure_ascii=False)),
    )
    return True, cur.lastrowid


def upsert_related_stocks(
    con: sqlite3.Connection, industry_id: int, now: str, dry_run: bool
) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    for code, name, market, sub in STOCKS:
        exists = con.execute(
            "SELECT 1 FROM related_stocks WHERE industry=? AND stock_code=?",
            (INDUSTRY, code),
        ).fetchone()
        if exists:
            skipped += 1
            continue
        if dry_run:
            inserted += 1
            continue
        con.execute(
            """INSERT INTO related_stocks
               (industry_id, industry, sub_industry, stock_code, stock_name, market,
                discovery_source, discovered_at, confidence, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (industry_id, INDUSTRY, sub, code, name, market,
             DISCOVERY_SOURCE, now, CONFIDENCE, STATUS, now),
        )
        inserted += 1
    return inserted, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not write")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        before = {
            "industry_dict": con.execute("SELECT COUNT(*) FROM industry_dict").fetchone()[0],
            "watchlist": con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0],
            "watchlist_active": con.execute(
                "SELECT COUNT(*) FROM watchlist WHERE zone='active'"
            ).fetchone()[0],
            "related_stocks": con.execute("SELECT COUNT(*) FROM related_stocks").fetchone()[0],
        }

        with con:
            con.execute("BEGIN IMMEDIATE")
            if args.dry_run:
                # Use existing id if present, else 0 placeholder
                row = con.execute(
                    "SELECT industry_id FROM watchlist WHERE industry_name=?", (INDUSTRY,)
                ).fetchone()
                industry_id = row[0] if row else 0
                dict_inserted = not con.execute(
                    "SELECT 1 FROM industry_dict WHERE industry=?", (INDUSTRY,)
                ).fetchone()
                watch_inserted = row is None
            else:
                dict_inserted = upsert_industry_dict(con, now)
                watch_inserted, industry_id = upsert_watchlist(con)
            stocks_inserted, stocks_skipped = upsert_related_stocks(
                con, industry_id, now, args.dry_run
            )

        after = {
            "industry_dict": con.execute("SELECT COUNT(*) FROM industry_dict").fetchone()[0],
            "watchlist": con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0],
            "watchlist_active": con.execute(
                "SELECT COUNT(*) FROM watchlist WHERE zone='active'"
            ).fetchone()[0],
            "related_stocks": con.execute("SELECT COUNT(*) FROM related_stocks").fetchone()[0],
        }

        print()
        print("=== v1.06 unicorn exposure load ===")
        print(f"  Mode:                  {'DRY-RUN' if args.dry_run else 'WRITE'}")
        print(f"  industry_dict:         {before['industry_dict']} -> {after['industry_dict']}"
              f"  (inserted={int(dict_inserted)})")
        print(f"  watchlist (total):     {before['watchlist']} -> {after['watchlist']}"
              f"  (inserted={int(watch_inserted)})")
        print(f"  watchlist (active):    {before['watchlist_active']} -> {after['watchlist_active']}")
        print(f"  related_stocks:        {before['related_stocks']} -> {after['related_stocks']}"
              f"  (inserted={stocks_inserted}, skipped={stocks_skipped})")

        # Distribution by industry x market for the new industry
        print()
        print(f"=== {INDUSTRY} stocks (by market) ===")
        for r in con.execute(
            """SELECT market, COUNT(*) AS n,
                      GROUP_CONCAT(stock_code, ', ') AS tickers
               FROM related_stocks
               WHERE industry=?
               GROUP BY market
               ORDER BY market""",
            (INDUSTRY,),
        ):
            print(f"  [{r['n']}] {r['market']}: {r['tickers']}")

        # Global market distribution
        print()
        print("=== related_stocks total by market ===")
        for r in con.execute(
            "SELECT market, COUNT(*) FROM related_stocks GROUP BY market ORDER BY market"
        ):
            print(f"  {r[0]}: {r[1]}")

    finally:
        con.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
