"""
knowledge/init_queue_db.py — 创建queue.db（独立消息队列数据库）

对应Scout系统蓝图v1.61 Step 2.2。独立DB避免与knowledge.db锁冲突。
event_id (UUID) UNIQUE防重复消费。

运行方式：
    python knowledge/init_queue_db.py
"""
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "queue.db"


QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE,                   -- UUID防重复消费
    entity_key TEXT,
    queue_name TEXT,                        -- collection_to_knowledge/knowledge_to_analysis/push_outbox
    producer TEXT,
    payload TEXT,                           -- JSON
    status TEXT DEFAULT 'pending',          -- pending/processing/done/failed
    retries INTEGER DEFAULT 0,
    created_at TEXT,                        -- UTC
    processed_at TEXT,                      -- UTC
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_mq_queue_status ON message_queue(queue_name, status);
"""


def init_queue_db(db_path=DEFAULT_DB_PATH):
    """创建queue.db及其message_queue表+索引。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(QUEUE_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    path = init_queue_db()
    conn = sqlite3.connect(str(path))
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    indexes = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    conn.close()
    print(f"[ok] queue.db created at: {path}")
    print(f"[ok] tables: {tables}")
    print(f"[ok] indexes: {indexes}")
