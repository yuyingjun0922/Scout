"""
infra/db_manager.py — v1.57决策6并发控制

核心职责：
    1) write/write_many：BEGIN IMMEDIATE事务，防止并发写冲突
    2) read_snapshot：基于启动时间戳的快照隔离（Agent循环不读自己的新写）

用法示例：
    db = DatabaseManager("data/knowledge.db")
    db.write("INSERT INTO info_units (...) VALUES (?, ?, ...)", params)
    rows = db.read_snapshot(started_at=now_utc())
"""
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union


class DatabaseManager:
    """SQLite并发控制管理器（v1.57决策6）"""

    def __init__(self, db_path: Union[str, Path], timeout: float = 30.0):
        self.db_path = str(db_path)
        self.timeout = timeout
        # isolation_level=None：autocommit模式，显式BEGIN/COMMIT控制事务
        self.conn: Optional[sqlite3.Connection] = sqlite3.connect(
            self.db_path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def write(self, sql: str, params: Iterable[Any] = ()) -> int:
        """单条写入。使用BEGIN IMMEDIATE获取写锁，防止并发写冲突。

        返回lastrowid（AUTOINCREMENT表插入后的rowid；其它情况可能为0）。
        """
        if self.conn is None:
            raise RuntimeError("DatabaseManager is closed")
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(sql, tuple(params))
            self.conn.commit()
            return cur.lastrowid
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def write_many(self, sql: str, params_list: Iterable[Iterable[Any]]) -> None:
        """批量写入，单事务。"""
        if self.conn is None:
            raise RuntimeError("DatabaseManager is closed")
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.executemany(sql, [tuple(p) for p in params_list])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        """查询，返回全部行。"""
        if self.conn is None:
            raise RuntimeError("DatabaseManager is closed")
        cur = self.conn.cursor()
        try:
            cur.execute(sql, tuple(params))
            return cur.fetchall()
        finally:
            cur.close()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        """查询单条。"""
        if self.conn is None:
            raise RuntimeError("DatabaseManager is closed")
        cur = self.conn.cursor()
        try:
            cur.execute(sql, tuple(params))
            return cur.fetchone()
        finally:
            cur.close()

    def read_snapshot(
        self,
        started_at: str,
        table: str = "info_units",
        time_column: str = "created_at",
    ) -> List[sqlite3.Row]:
        """快照读取（v1.57决策6）。

        Agent启动时用now_utc()记录started_at，后续读取只返回该时间点之前的记录，
        避免同一Agent循环内读到自己刚写入的数据。默认读取info_units.created_at。
        """
        if self.conn is None:
            raise RuntimeError("DatabaseManager is closed")
        sql = f"SELECT * FROM {table} WHERE {time_column} < ?"
        return self.query(sql, (started_at,))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
