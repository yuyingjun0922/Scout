"""
infra/push_queue.py — v1.66 Phase 1 Step 11 推送队列封装

基于 Step 6 的 QueueManager，专门处理 `push_outbox` 队列。不重新实现队列机制。

职责：
    - 生产者 API：push / push_alert / push_daily_briefing / push_weekly_report
    - 订阅者 API（为 OpenClaw 等设计）：poll_pending / mark_delivered / mark_failed
    - 防重复：通过 entity_key（如 daily_briefing_20260418）+ SELECT-before-INSERT

消息统一结构（payload JSON）：
    {
        "message_type": "daily_briefing" | "weekly_report" | "alert" | "recommendation",
        "priority":     "red" | "yellow" | "blue" | "white",
        "content":      <dict>,
        "created_at":   UTC ISO 8601,
        "target_channel": "openclaw"   # Phase 1 only
    }

订阅者流程（OpenClaw 视角）：
    1. poll_pending(max=N)  → 按 priority 降序拿 N 条 pending（read-only，不改状态）
    2. 尝试送达
    3. 成功 → mark_delivered(event_id)     → status='done'
       失败 → mark_failed(event_id, reason) → retries+1，< MAX 回 pending 下次再拿，
                                               >= MAX 标 failed

优先级：red > yellow > blue > white（operation manual 6.3）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from agents.base import RuleViolation
from infra.queue_manager import QueueManager
from utils.time_utils import now_utc


QUEUE_NAME = "push_outbox"
KST = ZoneInfo("Asia/Seoul")

# 消息类型（operation manual 3.x）
VALID_MESSAGE_TYPES = frozenset({
    "daily_briefing",
    "weekly_report",
    "alert",
    "recommendation",
})

# 优先级（operation manual 6.3）
PRIORITIES = ("red", "yellow", "blue", "white")
VALID_PRIORITIES = frozenset(PRIORITIES)
PRIORITY_ORDER: Dict[str, int] = {p: i for i, p in enumerate(PRIORITIES)}
# red=0（最高）, white=3（最低）

# Alert 子类型
VALID_ALERT_TYPES = frozenset({
    "failure_level_change",
    "motivation_drift",
    "data_source_down",
    "cost_exceeded",
    "system_health",        # v1.13 心跳（每日 09:30 KST health_monitor）
})

# 周报 type
VALID_REPORT_TYPES = frozenset({"industry", "paper"})

DEFAULT_PRODUCER = "scout"
DEFAULT_TARGET_CHANNEL = "openclaw"

# 每日简报推送时间（KST 07:30）
DAILY_BRIEFING_HOUR_KST = 7
DAILY_BRIEFING_MINUTE_KST = 30


class PushQueue:
    """推送队列 — 专用 push_outbox，支持生产 + 订阅两端。"""

    def __init__(
        self,
        queue_manager: QueueManager,
        target_channel: str = DEFAULT_TARGET_CHANNEL,
    ):
        if queue_manager is None:
            raise RuleViolation("queue_manager is required")
        if not isinstance(target_channel, str) or not target_channel.strip():
            raise RuleViolation("target_channel must be non-empty str")
        self.qm = queue_manager
        self.target_channel = target_channel.strip()

    # ══════════════════════ 生产 ══════════════════════

    def push(
        self,
        message_type: str,
        content: Dict[str, Any],
        priority: str = "blue",
        producer: str = DEFAULT_PRODUCER,
        entity_key: Optional[str] = None,
        next_push_at: Optional[str] = None,
    ) -> str:
        """推送消息到 push_outbox。

        返回 event_id。若 entity_key 已存在（同 queue_name）→ 返回原 event_id，
        不创建新行（SELECT-before-INSERT 幂等）。

        Raises:
            RuleViolation: message_type / priority 未知，或 content 非 dict，
                           或 producer 为空
        """
        if message_type not in VALID_MESSAGE_TYPES:
            raise RuleViolation(
                f"unknown message_type {message_type!r}; "
                f"allowed: {sorted(VALID_MESSAGE_TYPES)}"
            )
        if priority not in VALID_PRIORITIES:
            raise RuleViolation(
                f"unknown priority {priority!r}; allowed: {PRIORITIES}"
            )
        if not isinstance(content, dict):
            raise RuleViolation(
                f"content must be dict, got {type(content).__name__}"
            )
        if not isinstance(producer, str) or not producer.strip():
            raise RuleViolation("producer must be non-empty str")

        # 幂等检查：entity_key 是否已存在（不分 status，包含 pending/processing/done/failed）
        if entity_key:
            self.qm._ensure_open()
            existing = self.qm.conn.execute(
                """SELECT event_id FROM message_queue
                   WHERE queue_name = ? AND entity_key = ?
                   ORDER BY id ASC LIMIT 1""",
                (QUEUE_NAME, entity_key),
            ).fetchone()
            if existing is not None:
                return existing["event_id"]

        payload: Dict[str, Any] = {
            "message_type": message_type,
            "priority": priority,
            "content": content,
            "created_at": now_utc(),
            "target_channel": self.target_channel,
        }
        if next_push_at:
            payload["next_push_at"] = next_push_at

        return self.qm.enqueue(
            queue_name=QUEUE_NAME,
            payload=payload,
            producer=producer.strip(),
            entity_key=entity_key,
        )

    def push_alert(
        self,
        alert_type: str,
        content: Dict[str, Any],
        priority: str = "red",
        producer: str = DEFAULT_PRODUCER,
        entity_key: Optional[str] = None,
    ) -> str:
        """快捷方法：推送 🔴 alert。

        默认 priority='red'；entity_key 未给时默认 `alert_{alert_type}_{KST_date}`
        以防同类告警一天推多条。
        """
        if alert_type not in VALID_ALERT_TYPES:
            raise RuleViolation(
                f"unknown alert_type {alert_type!r}; "
                f"allowed: {sorted(VALID_ALERT_TYPES)}"
            )
        if not isinstance(content, dict):
            raise RuleViolation("content must be dict")

        enriched = dict(content)
        enriched.setdefault("alert_type", alert_type)

        if entity_key is None:
            entity_key = f"alert_{alert_type}_{_today_kst_date_str()}"

        return self.push(
            message_type="alert",
            content=enriched,
            priority=priority,
            producer=producer,
            entity_key=entity_key,
        )

    def push_daily_briefing(
        self,
        content: Dict[str, Any],
        priority: str = "blue",
        producer: str = DEFAULT_PRODUCER,
        target_date: Optional[str] = None,
    ) -> str:
        """每日 07:30 KST 简报。同日同 briefing 只存一条（entity_key 去重）。

        Args:
            content: 简报内容 dict
            target_date: YYYYMMDD（KST 日期）；None → 今日 KST 日期

        next_push_at 自动算为"下一个 07:30 KST"（UTC 表示）。
        """
        if not isinstance(content, dict):
            raise RuleViolation("content must be dict")
        if target_date is None:
            target_date = _today_kst_date_str()
        if not _is_valid_yyyymmdd(target_date):
            raise RuleViolation(
                f"target_date must be YYYYMMDD, got {target_date!r}"
            )

        entity_key = f"daily_briefing_{target_date}"
        next_push_at = _next_daily_briefing_time_utc()

        return self.push(
            message_type="daily_briefing",
            content=content,
            priority=priority,
            producer=producer,
            entity_key=entity_key,
            next_push_at=next_push_at,
        )

    def push_weekly_report(
        self,
        report_type: str,
        content: Dict[str, Any],
        priority: str = "blue",
        producer: str = DEFAULT_PRODUCER,
        target_date: Optional[str] = None,
    ) -> str:
        """周度报告推送（industry / paper）。同日同类型去重。"""
        if report_type not in VALID_REPORT_TYPES:
            raise RuleViolation(
                f"unknown report_type {report_type!r}; "
                f"allowed: {sorted(VALID_REPORT_TYPES)}"
            )
        if not isinstance(content, dict):
            raise RuleViolation("content must be dict")
        if target_date is None:
            target_date = _today_kst_date_str()
        if not _is_valid_yyyymmdd(target_date):
            raise RuleViolation(
                f"target_date must be YYYYMMDD, got {target_date!r}"
            )

        entity_key = f"weekly_{report_type}_{target_date}"
        enriched = dict(content)
        enriched.setdefault("report_type", report_type)

        return self.push(
            message_type="weekly_report",
            content=enriched,
            priority=priority,
            producer=producer,
            entity_key=entity_key,
        )

    # ══════════════════════ 订阅 ══════════════════════

    def poll_pending(self, max: int = 10) -> List[Dict[str, Any]]:
        """订阅者读取 pending 消息，按 priority 降序（red 先）排序。

        不改状态（订阅者主动 mark_delivered / mark_failed）。
        同 priority 内按 created_at 升序（FIFO）。
        """
        if not isinstance(max, int) or max < 1:
            raise RuleViolation("max must be int >= 1")

        self.qm._ensure_open()
        cur = self.qm.conn.cursor()
        try:
            rows = cur.execute(
                """SELECT * FROM message_queue
                   WHERE queue_name = ? AND status = 'pending'
                   ORDER BY id ASC""",
                (QUEUE_NAME,),
            ).fetchall()
        finally:
            cur.close()

        enriched = [_row_to_push_dict(r) for r in rows]
        # Priority 排序（red=0 最先），同级按 created_at 升序（FIFO）
        enriched.sort(
            key=lambda m: (
                PRIORITY_ORDER.get(m.get("priority", ""), 999),
                m.get("created_at") or "",
            )
        )
        return enriched[:max]

    def mark_delivered(self, event_id: str) -> bool:
        """标记已送达（status='done'）。接受 pending / processing 两种状态。"""
        if not isinstance(event_id, str) or not event_id:
            raise RuleViolation("event_id must be non-empty str")

        self.qm._ensure_open()
        cur = self.qm.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            updated = cur.execute(
                """UPDATE message_queue
                   SET status='done', processed_at=?, error_message=NULL
                   WHERE event_id=? AND queue_name=?
                     AND status IN ('pending', 'processing')""",
                (now_utc(), event_id, QUEUE_NAME),
            )
            self.qm.conn.commit()
            return updated.rowcount > 0
        except Exception:
            self.qm.conn.rollback()
            raise
        finally:
            cur.close()

    def mark_failed(self, event_id: str, reason: str = "") -> bool:
        """标记发送失败 — 镜像 QueueManager.nack 的重试逻辑。

        retries+1：若 < MAX_RETRIES → pending（下次 poll_pending 会再拿到）；
        否则 → failed（终态，需人工介入）。
        接受 pending / processing 两种当前状态；done / failed → 返 False。
        """
        if not isinstance(event_id, str) or not event_id:
            raise RuleViolation("event_id must be non-empty str")

        self.qm._ensure_open()
        cur = self.qm.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                """SELECT retries, status FROM message_queue
                   WHERE event_id=? AND queue_name=?""",
                (event_id, QUEUE_NAME),
            ).fetchone()
            if row is None or row["status"] in ("done", "failed"):
                self.qm.conn.commit()
                return False

            new_retries = int(row["retries"]) + 1
            if new_retries < QueueManager.MAX_RETRIES:
                new_status = "pending"
                new_processed_at: Optional[str] = None
            else:
                new_status = "failed"
                new_processed_at = now_utc()

            updated = cur.execute(
                """UPDATE message_queue
                   SET status=?, retries=?, processed_at=?, error_message=?
                   WHERE event_id=? AND queue_name=?
                     AND status IN ('pending', 'processing')""",
                (
                    new_status,
                    new_retries,
                    new_processed_at,
                    reason or "",
                    event_id,
                    QUEUE_NAME,
                ),
            )
            self.qm.conn.commit()
            return updated.rowcount > 0
        except Exception:
            self.qm.conn.rollback()
            raise
        finally:
            cur.close()

    # ══════════════════════ 观察性 ══════════════════════

    def queue_stats(self) -> Dict[str, int]:
        """push_outbox 队列的 pending/processing/done/failed 计数。"""
        stats = self.qm.queue_stats(QUEUE_NAME)
        return stats.get(QUEUE_NAME, {
            "pending": 0, "processing": 0, "done": 0, "failed": 0,
        })


# ══════════════════════ 内部辅助 ══════════════════════


def _row_to_push_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """message_queue 行 → push 结构化字典（把 payload 展开到顶层便于订阅者消费）。"""
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (ValueError, TypeError):
        payload = {}
    return {
        "event_id": row["event_id"],
        "entity_key": row["entity_key"],
        "producer": row["producer"],
        "status": row["status"],
        "retries": int(row["retries"] or 0),
        "db_created_at": row["created_at"],
        # 从 payload 平铺出常用字段
        "message_type": payload.get("message_type"),
        "priority": payload.get("priority"),
        "content": payload.get("content") or {},
        "created_at": payload.get("created_at"),
        "target_channel": payload.get("target_channel"),
        "next_push_at": payload.get("next_push_at"),
    }


def _today_kst_date_str() -> str:
    """当前 KST 日期 YYYYMMDD 字符串"""
    return datetime.now(tz=KST).strftime("%Y%m%d")


def _is_valid_yyyymmdd(s: str) -> bool:
    if not isinstance(s, str) or len(s) != 8 or not s.isdigit():
        return False
    try:
        datetime.strptime(s, "%Y%m%d")
        return True
    except ValueError:
        return False


def _next_daily_briefing_time_utc(
    now_kst: Optional[datetime] = None,
) -> str:
    """下一个 07:30 KST 的 UTC ISO 8601 字符串。

    若当前 KST 时刻 <  07:30 → 返今天 07:30 KST
    若当前 KST 时刻 >= 07:30 → 返明天 07:30 KST
    """
    if now_kst is None:
        now_kst = datetime.now(tz=KST)
    briefing_today_kst = now_kst.replace(
        hour=DAILY_BRIEFING_HOUR_KST,
        minute=DAILY_BRIEFING_MINUTE_KST,
        second=0,
        microsecond=0,
    )
    if now_kst < briefing_today_kst:
        target = briefing_today_kst
    else:
        target = briefing_today_kst + timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()
