"""Migration: llm_invocations 加 v1.48 LLM 抽象层字段

加 6 个字段（全部 NULL-able，单条写入时 writer 按需填）：
  - input_tokens INTEGER       — prompt tokens（拆分 tokens_used 的上半）
  - output_tokens INTEGER      — completion tokens（拆分 tokens_used 的下半）
  - cost_usd_cents REAL        — USD 分（浮点）；对比旧 cost_cents INTEGER
  - latency_ms INTEGER         — 单次调用墙钟耗时
  - provider TEXT              — llm_providers.* 的 key（gemma_local / deepseek_v3 / ...）
  - fallback_used TEXT         — 若发生降级，记录原 primary 名；否则 NULL

旧 tokens_used / cost_cents 保留做向后兼容；新 writer 同时写新旧字段
（tokens_used = input_tokens + output_tokens, cost_cents = int(cost_usd_cents)）。

幂等：用 PRAGMA table_info 过滤已存在列，重复跑不报错。

用法:
  python scripts/migrations/2026-04-25_llm_invocations_v148_fields.py
  SCOUT_DB_PATH=/tmp/xxx.db python scripts/migrations/...  # 指定 DB
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(r"D:\13700F\Scout\data\knowledge.db")

FIELDS = [
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cost_usd_cents", "REAL"),
    ("latency_ms", "INTEGER"),
    ("provider", "TEXT"),
    ("fallback_used", "TEXT"),
]


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # 手动控制 BEGIN/COMMIT

    try:
        existing = existing_columns(conn, "llm_invocations")
        added, skipped = [], []
        conn.execute("BEGIN IMMEDIATE")
        for name, type_ in FIELDS:
            if name in existing:
                skipped.append(name)
                continue
            conn.execute(
                f"ALTER TABLE llm_invocations ADD COLUMN {name} {type_}"
            )
            added.append(name)
        conn.execute("COMMIT")

        # 验证
        post = existing_columns(conn, "llm_invocations")
        missing = [n for n, _ in FIELDS if n not in post]
        if missing:
            raise RuntimeError(
                f"post-migration verify failed: missing columns {missing}"
            )

        return {
            "db": str(db_path),
            "added": added,
            "skipped_already_exists": skipped,
            "total_columns_after": len(post),
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def main() -> int:
    db_path = Path(os.environ.get("SCOUT_DB_PATH", DEFAULT_DB))
    result = migrate(db_path)
    print(f"[migration] llm_invocations v1.48 fields applied")
    print(f"  db: {result['db']}")
    print(f"  added: {result['added']}")
    print(f"  skipped (already exist): {result['skipped_already_exists']}")
    print(f"  total columns after: {result['total_columns_after']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
