"""
tests/test_db_init.py — Step 2数据库建表测试

覆盖：
    - knowledge.db / queue.db能创建
    - 21张表全部存在（v1.03 加 master_analysis）
    - 所有索引都存在
    - 所有表初始行数为0
    - DatabaseManager能写入+读取
    - read_snapshot快照隔离正确
    - 外键约束生效
"""
import sqlite3
from pathlib import Path

import pytest

from knowledge.init_db import init_database, EXPECTED_TABLES
from knowledge.init_queue_db import init_queue_db
from infra.db_manager import DatabaseManager
from utils.time_utils import now_utc


EXPECTED_INDEXES = [
    "idx_iu_source_time", "idx_iu_status", "idx_iu_chain",
    "idx_wl_name", "idx_wl_zone",
    "idx_ic_from",
    "idx_rs_industry", "idx_rs_market", "idx_rs_global",
    "idx_iim_info", "idx_iim_industry",
    "idx_sf_stock",
    "idx_ec_status", "idx_ec_tag",
    "idx_mdl_industry",
    "idx_gc_name",
    "idx_llm_agent_time",
    "idx_ae_type_time",
    "idx_rec_stock", "idx_rec_mode",
    "idx_ud_recommend",
    "idx_pt_recommend",
    "idx_rr_recommend",
    "idx_rs_stock",
    "idx_ma_stock_master", "idx_ma_time",
]


# ═══ fixtures ═══

@pytest.fixture
def knowledge_db(tmp_path):
    db = tmp_path / "knowledge.db"
    init_database(db)
    return db


@pytest.fixture
def queue_db(tmp_path):
    db = tmp_path / "queue.db"
    init_queue_db(db)
    return db


# ═══ knowledge.db基本存在性 ═══

def test_knowledge_db_created(knowledge_db):
    assert knowledge_db.exists()
    assert knowledge_db.stat().st_size > 0


def test_queue_db_created(queue_db):
    assert queue_db.exists()
    assert queue_db.stat().st_size > 0


# ═══ 21张表（v1.03 加 master_analysis）═══

def test_knowledge_db_has_exactly_21_tables(knowledge_db):
    conn = sqlite3.connect(str(knowledge_db))
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    conn.close()
    assert len(tables) == 21, f"Expected 21 tables, got {len(tables)}: {sorted(tables)}"


def test_knowledge_db_all_expected_tables_present(knowledge_db):
    conn = sqlite3.connect(str(knowledge_db))
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    for t in EXPECTED_TABLES:
        assert t in tables, f"Missing table: {t}"


def test_knowledge_db_all_tables_empty(knowledge_db):
    """所有表初始应为0行"""
    conn = sqlite3.connect(str(knowledge_db))
    for t in EXPECTED_TABLES:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"Table {t} not empty: {n} rows"
    conn.close()


# ═══ 索引 ═══

def test_knowledge_db_all_expected_indexes_present(knowledge_db):
    conn = sqlite3.connect(str(knowledge_db))
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    conn.close()
    missing = [idx for idx in EXPECTED_INDEXES if idx not in indexes]
    assert not missing, f"Missing indexes: {missing}"


# ═══ queue.db ═══

def test_queue_db_has_message_queue_table(queue_db):
    conn = sqlite3.connect(str(queue_db))
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    conn.close()
    assert "message_queue" in tables


def test_queue_db_index_present(queue_db):
    conn = sqlite3.connect(str(queue_db))
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    conn.close()
    assert "idx_mq_queue_status" in indexes


def test_queue_db_event_id_unique(queue_db):
    """event_id UNIQUE防重复消费"""
    conn = sqlite3.connect(str(queue_db))
    conn.execute(
        "INSERT INTO message_queue (event_id, queue_name, created_at) VALUES (?, ?, ?)",
        ("uuid-1", "push_outbox", now_utc()),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO message_queue (event_id, queue_name, created_at) VALUES (?, ?, ?)",
            ("uuid-1", "push_outbox", now_utc()),
        )
    conn.close()


# ═══ DatabaseManager ═══

def test_db_manager_write_and_read(knowledge_db):
    db = DatabaseManager(knowledge_db)
    try:
        t = now_utc()
        db.write(
            "INSERT INTO system_meta (key, value, updated_at) VALUES (?, ?, ?)",
            ("cold_start_at", t, t),
        )
        row = db.query_one(
            "SELECT key, value FROM system_meta WHERE key=?",
            ("cold_start_at",),
        )
        assert row is not None
        assert row["key"] == "cold_start_at"
        assert row["value"] == t
    finally:
        db.close()


def test_db_manager_write_rolls_back_on_error(knowledge_db):
    """写入失败时事务回滚"""
    db = DatabaseManager(knowledge_db)
    try:
        db.write(
            "INSERT INTO watchlist (industry_name, entered_at) VALUES (?, ?)",
            ("半导体", now_utc()),
        )
        # 再次写同名应因UNIQUE失败
        with pytest.raises(sqlite3.IntegrityError):
            db.write(
                "INSERT INTO watchlist (industry_name, entered_at) VALUES (?, ?)",
                ("半导体", now_utc()),
            )
        # 第一条仍在
        rows = db.query("SELECT COUNT(*) AS n FROM watchlist WHERE industry_name=?", ("半导体",))
        assert rows[0]["n"] == 1
    finally:
        db.close()


def test_db_manager_snapshot_isolation(knowledge_db):
    """v1.57决策6：read_snapshot只读started_at之前创建的记录"""
    db = DatabaseManager(knowledge_db)
    try:
        t_old = "2026-04-17T00:00:00+00:00"
        t_boundary = "2026-04-17T12:00:00+00:00"
        t_new = "2026-04-18T00:00:00+00:00"

        db.write(
            "INSERT INTO info_units (id, source, timestamp, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("snap_old", "D1", t_old, t_old, t_old),
        )
        db.write(
            "INSERT INTO info_units (id, source, timestamp, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("snap_new", "D1", t_new, t_new, t_new),
        )

        rows = db.read_snapshot(t_boundary)
        ids = [r["id"] for r in rows]
        assert "snap_old" in ids
        assert "snap_new" not in ids
    finally:
        db.close()


def test_db_manager_foreign_key_enforcement(knowledge_db):
    """PRAGMA foreign_keys=ON：插入不存在的FK应失败"""
    db = DatabaseManager(knowledge_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.write(
                "INSERT INTO info_industry_map (info_unit_id, industry_name, created_at) "
                "VALUES (?, ?, ?)",
                ("does_not_exist", "半导体", now_utc()),
            )
    finally:
        db.close()


def test_db_manager_context_manager_closes(knowledge_db):
    with DatabaseManager(knowledge_db) as db:
        assert db.conn is not None
    assert db.conn is None
