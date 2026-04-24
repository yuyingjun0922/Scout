"""Migration: user_decisions 加 v1.69 决策字段扩展（TD-008 相关）

加 5 个字段（全部 NULL-able）：
  - reasoning TEXT               — 用户决策时的思考文字
  - emotion TEXT                 — confident/hesitant/fomo/fear/anchoring/contrarian
  - confidence INTEGER (1-10)    — 用户自评决策信心
  - time_spent_seconds INTEGER   — 决策花费秒数
  - pre_mortem TEXT (JSON)       — 3 个失败场景 JSON

这 5 字段暂时写 NULL；等 QQ receive_reply 上线后 PushConsumerAgent 收用户 QQ
回复时再填。见 TD-008。

幂等：用 PRAGMA table_info 过滤已存在列，重复跑不报错。

用法:
  python scripts/migrations/2026-04-24_user_decisions_v169_fields.py
  SCOUT_DB_PATH=/tmp/xxx.db python scripts/migrations/...  # 指定 DB
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(r"D:\13700F\Scout\data\knowledge.db")

FIELDS = [
    ("reasoning", "TEXT"),
    ("emotion", "TEXT"),                      # confident/hesitant/fomo/fear/anchoring/contrarian
    ("confidence", "INTEGER"),                # 1-10
    ("time_spent_seconds", "INTEGER"),
    ("pre_mortem", "TEXT"),                   # JSON: 3 个失败场景
]


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # 手动控制 BEGIN/COMMIT

    try:
        existing = existing_columns(conn, "user_decisions")
        added, skipped = [], []
        conn.execute("BEGIN IMMEDIATE")
        for name, type_ in FIELDS:
            if name in existing:
                skipped.append(name)
                continue
            conn.execute(f"ALTER TABLE user_decisions ADD COLUMN {name} {type_}")
            added.append(name)
        conn.execute("COMMIT")

        # 验证
        post = existing_columns(conn, "user_decisions")
        missing = [n for n, _ in FIELDS if n not in post]
        if missing:
            raise RuntimeError(f"post-migration verify failed: missing columns {missing}")

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
    print(f"[migration] user_decisions v1.69 fields applied")
    print(f"  db: {result['db']}")
    print(f"  added: {result['added']}")
    print(f"  skipped (already exist): {result['skipped_already_exists']}")
    print(f"  total columns after: {result['total_columns_after']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
