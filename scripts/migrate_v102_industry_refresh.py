"""v1.02 — 行业字典扩展：加入 6 个新行业到 industry_dict + watchlist

Idempotent migration. Safe to re-run.

Adds (industry_dict.confidence='approved'):
  - 半导体材料 (active, cyclical=1)
  - 新材料 (active, cyclical=1)
  - 生物制造 (active, cyclical=0)
  - 低空经济 (active, cyclical=0)
  - 消费升级/IP经济 (observation, cyclical=1)
  - 量子计算 (observation, cyclical=0)

Verification:
  - industry_dict: 0 -> 6 rows
  - watchlist:    15 -> 21 rows (18 active + 3 observation)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge.db"


NEW_INDUSTRIES = [
    {
        "industry": "半导体材料",
        "aliases": ["semiconductor materials", "光刻胶", "硅片", "电子特气", "抛光垫"],
        "cyclical": 1,
        "scout_range": "active",
        "sub_industries": [
            {"name": "光刻胶", "fillability": 3},
            {"name": "硅片/抛光垫", "fillability": 4},
            {"name": "电子特气", "fillability": 3},
        ],
        "watchlist_zone": "active",
    },
    {
        "industry": "新材料",
        "aliases": ["advanced materials", "先进材料", "电池材料", "光电材料"],
        "cyclical": 1,
        "scout_range": "active",
        "sub_industries": [
            {"name": "电池材料", "fillability": 4},
            {"name": "光电材料", "fillability": 3},
            {"name": "生物医用材料", "fillability": 2},
        ],
        "watchlist_zone": "active",
    },
    {
        "industry": "生物制造",
        "aliases": ["biomanufacturing", "合成生物学", "细胞培养", "精准制造"],
        "cyclical": 0,
        "scout_range": "active",
        "sub_industries": [
            {"name": "合成生物学", "fillability": 3},
            {"name": "细胞培养", "fillability": 2},
            {"name": "精准医疗制造", "fillability": 2},
        ],
        "watchlist_zone": "active",
    },
    {
        "industry": "低空经济",
        "aliases": ["low altitude economy", "eVTOL", "无人机", "空中物流"],
        "cyclical": 0,
        "scout_range": "active",
        "sub_industries": [
            {"name": "eVTOL", "fillability": 3},
            {"name": "无人机应用", "fillability": 3},
        ],
        "watchlist_zone": "active",
    },
    {
        "industry": "消费升级/IP经济",
        "aliases": ["消费升级", "IP经济", "设计师玩具", "游戏", "娱乐"],
        "cyclical": 1,
        "scout_range": "observation",
        "sub_industries": [],
        "watchlist_zone": "observation",
    },
    {
        "industry": "量子计算",
        "aliases": ["quantum computing", "量子硬件", "量子软件"],
        "cyclical": 0,
        "scout_range": "observation",
        "sub_industries": [],
        "watchlist_zone": "observation",
    },
]


def migrate(db_path: Path) -> tuple[int, int]:
    """Return (industry_dict_added, watchlist_added)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    added_dict = 0
    added_watch = 0
    try:
        cur = conn.cursor()

        for spec in NEW_INDUSTRIES:
            name = spec["industry"]
            aliases_json = json.dumps(spec["aliases"], ensure_ascii=False)
            sub_json = json.dumps(spec["sub_industries"], ensure_ascii=False)

            # industry_dict
            cur.execute("SELECT 1 FROM industry_dict WHERE industry=?", (name,))
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO industry_dict (
                        industry, aliases, cyclical, scout_range,
                        sub_industries, confidence,
                        last_change_reason, last_change_by
                    ) VALUES (?, ?, ?, ?, ?, 'approved', ?, 'v1.02-industry-refresh')
                    """,
                    (
                        name,
                        aliases_json,
                        spec["cyclical"],
                        spec["scout_range"],
                        sub_json,
                        f"v1.02 行业字典扩展：新增 {name}",
                    ),
                )
                added_dict += 1

            # watchlist
            cur.execute("SELECT 1 FROM watchlist WHERE industry_name=?", (name,))
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO watchlist (
                        industry_name, industry_aliases, zone,
                        source_type, early_signal
                    ) VALUES (?, ?, ?, 'system', 0)
                    """,
                    (name, aliases_json, spec["watchlist_zone"]),
                )
                added_watch += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return added_dict, added_watch


def verify(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM industry_dict WHERE confidence='approved'")
        dict_approved = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM industry_dict")
        dict_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM watchlist")
        watch_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM watchlist WHERE zone='active'")
        watch_active = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM watchlist WHERE zone='observation'")
        watch_observation = cur.fetchone()[0]
        cur.execute(
            "SELECT industry, scout_range, cyclical, confidence FROM industry_dict ORDER BY industry"
        )
        dict_rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "dict_total": dict_total,
        "dict_approved": dict_approved,
        "watch_total": watch_total,
        "watch_active": watch_active,
        "watch_observation": watch_observation,
        "dict_rows": dict_rows,
    }


def main() -> int:
    if not DB_PATH.exists():
        print(f"FAIL: DB not found at {DB_PATH}")
        return 1
    added_dict, added_watch = migrate(DB_PATH)
    print(f"Inserted into industry_dict: {added_dict}")
    print(f"Inserted into watchlist:     {added_watch}")
    print()

    v = verify(DB_PATH)
    print(f"industry_dict total:       {v['dict_total']}")
    print(f"industry_dict approved:    {v['dict_approved']}")
    print(f"watchlist total:           {v['watch_total']}")
    print(f"watchlist active:          {v['watch_active']}")
    print(f"watchlist observation:     {v['watch_observation']}")
    print()
    print("industry_dict rows:")
    for row in v["dict_rows"]:
        print(f"  {row}")

    expected_dict = 6
    expected_watch = 21
    expected_active = 18
    expected_obs = 3

    failures = []
    if v["dict_total"] != expected_dict:
        failures.append(f"industry_dict: expected {expected_dict}, got {v['dict_total']}")
    if v["watch_total"] != expected_watch:
        failures.append(f"watchlist: expected {expected_watch}, got {v['watch_total']}")
    if v["watch_active"] != expected_active:
        failures.append(
            f"watchlist active: expected {expected_active}, got {v['watch_active']}"
        )
    if v["watch_observation"] != expected_obs:
        failures.append(
            f"watchlist observation: expected {expected_obs}, got {v['watch_observation']}"
        )

    if failures:
        print()
        print("VERIFICATION FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 2

    print()
    print("OK: v1.02 industry refresh verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
