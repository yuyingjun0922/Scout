"""
infra/queue_manager.py — v1.57 消息队列管理器（queue.db 独立）

Phase 1 队列白名单：
    - collection_to_knowledge : 采集 → 知识层
    - knowledge_to_analysis   : 知识 → 分析层
    - push_outbox             : 推送出站

并发安全：
    - enqueue    : 单行 INSERT OR IGNORE（event_id UNIQUE 防重复消费）
    - dequeue    : BEGIN IMMEDIATE 持写锁 + 条件 UPDATE（WHERE status='pending'），
                   杜绝两个 worker 双取同一条
    - ack / nack : 条件 UPDATE（WHERE status='processing'），只有处于 processing
                   状态的消息能被 ack/nack

错误处理（与 BaseAgent 错误矩阵对齐）：
    - 入参非法（未知 queue_name / 空 producer / payload 非 dict 等）→ RuleViolation
      被 BaseAgent 的 'rule' 分支记录
    - SQLite 低层错误（db locked / 磁盘满）→ 让原生异常上抛，BaseAgent 归 'unknown'
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agents.base import RuleViolation
from utils.time_utils import now_utc


class QueueManager:
    """SQLite 消息队列管理器（operates on queue.db）"""

    QUEUE_NAMES = frozenset({
        "collection_to_knowledge",
        "knowledge_to_analysis",
        "push_outbox",
    })

    STATUSES = frozenset({"pending", "processing", "done", "failed"})

    MAX_RETRIES: int = 3  # nack 使 retries==MAX_RETRIES 时标 failed

    def __init__(
        self,
        queue_db_path: Union[str, Path],
        timeout: float = 30.0,
    ):
        self.db_path = str(queue_db_path)
        self.timeout = timeout
        # isolation_level=None：autocommit 模式，显式管理 BEGIN/COMMIT
        self.conn: Optional[sqlite3.Connection] = sqlite3.connect(
            self.db_path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    # ── 白名单验证 ──

    def _validate_queue_name(self, queue_name: str) -> None:
        if not isinstance(queue_name, str) or not queue_name:
            raise RuleViolation(
                f"queue_name must be non-empty str, got {queue_name!r}"
            )
        if queue_name not in self.QUEUE_NAMES:
            raise RuleViolation(
                f"unknown queue_name {queue_name!r}; "
                f"allowed: {sorted(self.QUEUE_NAMES)}"
            )

    # ── enqueue ──

    def enqueue(
        self,
        queue_name: str,
        payload: Dict[str, Any],
        producer: str,
        entity_key: Optional[str] = None,
    ) -> str:
        """入队，返回生成的 event_id。"""
        self._validate_queue_name(queue_name)
        if not isinstance(payload, dict):
            raise RuleViolation(
                f"payload must be dict, got {type(payload).__name__}"
            )
        if not isinstance(producer, str) or not producer:
            raise RuleViolation("producer must be non-empty str")

        event_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False)
        created_at = now_utc()

        self._ensure_open()
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                """INSERT OR IGNORE INTO message_queue
                   (event_id, entity_key, queue_name, producer, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_id, entity_key, queue_name, producer, payload_json, created_at),
            )
            self.conn.commit()
            return event_id
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    # ── dequeue（原子） ──

    def dequeue(
        self,
        queue_name: str,
        consumer: str,
    ) -> Optional[Dict[str, Any]]:
        """原子取一条 pending 消息并标 processing。空队列返 None。

        BEGIN IMMEDIATE 获取 SQLite 写锁 → SELECT 最老 pending → UPDATE 条件转
        processing。两个 worker 同时调用时 SQLite 会串行化；后到的那个看到 0 pending
        返回 None。
        """
        self._validate_queue_name(queue_name)
        if not isinstance(consumer, str) or not consumer:
            raise RuleViolation("consumer must be non-empty str")

        self._ensure_open()
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                """SELECT * FROM message_queue
                   WHERE queue_name=? AND status='pending'
                   ORDER BY id ASC
                   LIMIT 1""",
                (queue_name,),
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None

            processed_at = now_utc()
            updated = cur.execute(
                """UPDATE message_queue
                   SET status='processing', processed_at=?
                   WHERE event_id=? AND status='pending'""",
                (processed_at, row["event_id"]),
            )
            if updated.rowcount == 0:
                # 理论上 BEGIN IMMEDIATE 下不会走到这（写锁独占），保守处理
                self.conn.commit()
                return None

            self.conn.commit()
            return self._row_to_dict(row, override_status="processing", override_processed_at=processed_at)
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    # ── ack ──

    def ack(self, event_id: str) -> bool:
        """标记消息完成。必须当前为 processing；否则返 False（不抛）。"""
        if not isinstance(event_id, str) or not event_id:
            raise RuleViolation("event_id must be non-empty str")

        self._ensure_open()
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            updated = cur.execute(
                """UPDATE message_queue
                   SET status='done', processed_at=?, error_message=NULL
                   WHERE event_id=? AND status='processing'""",
                (now_utc(), event_id),
            )
            self.conn.commit()
            return updated.rowcount > 0
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    # ── nack（重试或失败） ──

    def nack(self, event_id: str, error_message: str = "") -> bool:
        """标记消息失败。retries+1：新值 < MAX_RETRIES 回 pending；否则标 failed。

        只能 nack 当前为 processing 的消息；否则返 False。
        """
        if not isinstance(event_id, str) or not event_id:
            raise RuleViolation("event_id must be non-empty str")

        self._ensure_open()
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                """SELECT retries, status FROM message_queue WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            if row is None or row["status"] != "processing":
                self.conn.commit()
                return False

            new_retries = int(row["retries"]) + 1
            if new_retries < self.MAX_RETRIES:
                # 重新排队
                new_status = "pending"
                new_processed_at: Optional[str] = None
            else:
                new_status = "failed"
                new_processed_at = now_utc()

            updated = cur.execute(
                """UPDATE message_queue
                   SET status=?, retries=?, processed_at=?, error_message=?
                   WHERE event_id=? AND status='processing'""",
                (
                    new_status,
                    new_retries,
                    new_processed_at,
                    error_message or "",
                    event_id,
                ),
            )
            self.conn.commit()
            return updated.rowcount > 0
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    # ── peek（只读） ──

    def peek(self, queue_name: str, limit: int = 10) -> List[Dict[str, Any]]:
        """查看队列头 N 条 pending 消息（不改状态）。用于调试/监控。"""
        self._validate_queue_name(queue_name)
        if not isinstance(limit, int) or limit < 1:
            raise RuleViolation("limit must be int >= 1")

        self._ensure_open()
        cur = self.conn.cursor()
        try:
            rows = cur.execute(
                """SELECT * FROM message_queue
                   WHERE queue_name=? AND status='pending'
                   ORDER BY id ASC
                   LIMIT ?""",
                (queue_name, limit),
            ).fetchall()
        finally:
            cur.close()

        return [self._row_to_dict(r) for r in rows]

    # ── 统计 ──

    def queue_stats(
        self,
        queue_name: Optional[str] = None,
    ) -> Dict[str, Dict[str, int]]:
        """返回 {queue_name: {pending/processing/done/failed: count}}。"""
        if queue_name is not None:
            self._validate_queue_name(queue_name)

        self._ensure_open()
        cur = self.conn.cursor()
        try:
            if queue_name is None:
                rows = cur.execute(
                    """SELECT queue_name, status, COUNT(*) AS n
                       FROM message_queue
                       GROUP BY queue_name, status"""
                ).fetchall()
            else:
                rows = cur.execute(
                    """SELECT queue_name, status, COUNT(*) AS n
                       FROM message_queue WHERE queue_name=?
                       GROUP BY queue_name, status""",
                    (queue_name,),
                ).fetchall()
        finally:
            cur.close()

        result: Dict[str, Dict[str, int]] = {}
        for row in rows:
            qn = row["queue_name"]
            st = row["status"]
            result.setdefault(
                qn,
                {"pending": 0, "processing": 0, "done": 0, "failed": 0},
            )
            if st in result[qn]:
                result[qn][st] = int(row["n"])

        # 补零：询问的 queue（或所有白名单队列）即使无数据也给完整键
        targets = [queue_name] if queue_name else list(self.QUEUE_NAMES)
        for qn in targets:
            result.setdefault(
                qn,
                {"pending": 0, "processing": 0, "done": 0, "failed": 0},
            )
        return result

    # ── 清理 ──

    def purge_old_done(self, days: int = 30) -> int:
        """删除 N 天前已 done 的消息。返回删除行数。"""
        if not isinstance(days, int) or days < 0:
            raise RuleViolation("days must be int >= 0")

        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(days=days)
        ).isoformat()

        self._ensure_open()
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            deleted = cur.execute(
                """DELETE FROM message_queue
                   WHERE status='done' AND processed_at < ?""",
                (cutoff,),
            )
            self.conn.commit()
            return int(deleted.rowcount)
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    # ── 生命周期 ──

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ── 内部 ──

    def _ensure_open(self) -> None:
        if self.conn is None:
            raise RuleViolation("QueueManager is closed")

    @staticmethod
    def _row_to_dict(
        row: sqlite3.Row,
        override_status: Optional[str] = None,
        override_processed_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "entity_key": row["entity_key"],
            "queue_name": row["queue_name"],
            "producer": row["producer"],
            "payload": json.loads(row["payload"]) if row["payload"] else {},
            "status": override_status or row["status"],
            "retries": int(row["retries"]),
            "created_at": row["created_at"],
            "processed_at": override_processed_at or row["processed_at"],
            "error_message": row["error_message"],
        }
