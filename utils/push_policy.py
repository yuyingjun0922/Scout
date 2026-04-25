"""
utils/push_policy.py — 勿扰时段 (Quiet Hours) 策略 (v1.61 P0-P4)

纯策略模块：决定"此刻此级别的消息能不能推"。消费侧的持久化（把 pending
改成 quiet_held / 07:30 digest flush）写在 PushConsumerAgent。

词表（P0 最急、P4 最低；对齐 v1.61）：
    P0 — critical         （红色告警、失效级跃升）
    P1 — high             （黄色告警）
    P2 — normal           （蓝色日常）
    P3 — low              （白色心跳类）
    P4 — lowest (default) （老数据或未声明 alert_level 的保守默认）

旧 priority(red/yellow/blue/white) → P0/P1/P2/P3 的兼容映射在
PRIORITY_TO_ALERT_LEVEL；任何无法判定的走 DEFAULT_ALERT_LEVEL=P4，
保守地被静默期吞掉。

使用示例：
    policy = QuietHoursPolicy.from_config(cfg.push.quiet_hours)
    level  = extract_alert_level(payload_dict)
    if should_push_now(policy, level):
        channel.send(text)
    else:
        qm.set_status(event_id, "quiet_held")
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, FrozenSet, List, Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
PUSH_OUTBOX_QUEUE = "push_outbox"     # 与 infra.push_queue.QUEUE_NAME 对齐

# ═══════════════════ 词表 ═══════════════════

ALERT_LEVELS: FrozenSet[str] = frozenset({"P0", "P1", "P2", "P3", "P4"})
DEFAULT_ALERT_LEVEL = "P4"

# 旧 priority → P 级（payload 没 alert_level 时的兼容路径）
PRIORITY_TO_ALERT_LEVEL: Dict[str, str] = {
    "red": "P0",
    "yellow": "P1",
    "blue": "P2",
    "white": "P3",
}

# 静默状态：加到 message_queue.status，schema 不变（status 本来就 TEXT）
QUIET_HELD_STATUS = "quiet_held"


# ═══════════════════ 策略对象 ═══════════════════


@dataclass(frozen=True)
class QuietHoursPolicy:
    """不可变策略快照。从 config.yaml 装配一次，运行期只读。"""

    enabled: bool
    start: time                              # 本地时间（self.tz 时区下的 HH:MM）
    end: time
    tz: ZoneInfo
    always_push_levels: FrozenSet[str]       # 其它级别静默期攒到 digest
    digest_at: time

    @classmethod
    def disabled(cls) -> "QuietHoursPolicy":
        """全开通（enabled=False）。策略对象仍存在，但 should_push_now 永远 True。"""
        return cls(
            enabled=False,
            start=time(0, 0),
            end=time(0, 0),
            tz=KST,
            always_push_levels=frozenset(ALERT_LEVELS),
            digest_at=time(7, 30),
        )

    @classmethod
    def from_config(cls, cfg: Any) -> "QuietHoursPolicy":
        """从 Pydantic QuietHoursConfig（或 dict）构造。None → disabled()。

        接受：
            - QuietHoursConfig (pydantic) — 走 model_dump
            - dict                         — 字段名一致
            - None                         — disabled
        """
        if cfg is None:
            return cls.disabled()
        if hasattr(cfg, "model_dump"):
            d = cfg.model_dump()
        elif isinstance(cfg, dict):
            d = dict(cfg)
        else:
            raise TypeError(
                f"QuietHoursPolicy.from_config: cfg must be pydantic/dict/None, "
                f"got {type(cfg).__name__}"
            )

        if not d.get("enabled", False):
            return cls.disabled()

        tz_name = d.get("timezone") or "KST"
        tz = KST if tz_name == "KST" else ZoneInfo(tz_name)

        levels_raw = d.get("always_push_levels") or ["P0", "P1"]
        unknown = [lv for lv in levels_raw if lv not in ALERT_LEVELS]
        if unknown:
            raise ValueError(
                f"always_push_levels contains unknown level(s): {unknown}; "
                f"allowed: {sorted(ALERT_LEVELS)}"
            )

        return cls(
            enabled=True,
            start=_parse_hhmm(d.get("start") or "00:00", "start"),
            end=_parse_hhmm(d.get("end") or "07:30", "end"),
            tz=tz,
            always_push_levels=frozenset(levels_raw),
            digest_at=_parse_hhmm(d.get("digest_at") or "07:30", "digest_at"),
        )

    def is_in_quiet_window(self, now_local: datetime) -> bool:
        """now_local 是否落在 [start, end) 静默窗口内。

        支持跨零点（start=23:00 end=07:00）。窗口半开：end 不含。
        now_local 必须是带 tzinfo 的 datetime（调用方保证，不自动本地化）。
        """
        if not self.enabled:
            return False
        t = now_local.timetz().replace(tzinfo=None)  # 只取 HH:MM:SS 做比较
        s, e = self.start, self.end
        if s == e:
            # 退化：空窗口
            return False
        if s < e:
            return s <= t < e
        # 跨零点：[s, 24:00) ∪ [0:00, e)
        return t >= s or t < e

    def is_always_push(self, alert_level: str) -> bool:
        return alert_level in self.always_push_levels


# ═══════════════════ 核心决策 ═══════════════════


def should_push_now(
    policy: QuietHoursPolicy,
    alert_level: str,
    now: Optional[datetime] = None,
) -> bool:
    """此级别此时刻是否直接推？False → 调用方应把消息状态改为 quiet_held。

    Args:
        policy:      QuietHoursPolicy（可能 disabled）
        alert_level: "P0"~"P4"。未知级别 → 归一到 P4
        now:         tz-aware datetime；None → 用 policy.tz 的当前时间

    Decision:
        - enabled=False                         → True
        - 级别在 always_push_levels              → True（永远直推）
        - 不在静默窗口                           → True
        - 在静默窗口 & 级别非 always_push_levels → False
    """
    if not policy.enabled:
        return True
    level = alert_level if alert_level in ALERT_LEVELS else DEFAULT_ALERT_LEVEL
    if policy.is_always_push(level):
        return True
    if now is None:
        now = datetime.now(tz=policy.tz)
    elif now.tzinfo is None:
        raise ValueError("should_push_now: `now` must be tz-aware")
    else:
        now = now.astimezone(policy.tz)
    return not policy.is_in_quiet_window(now)


def extract_alert_level(payload: Any) -> str:
    """从消息 payload 抽出 alert_level。

    兼容顺序：
      1. payload.alert_level 显式字段（未来标准写法）
      2. payload.content.alert_level（嵌套）
      3. payload.priority → PRIORITY_TO_ALERT_LEVEL 映射（老数据）
      4. 返 DEFAULT_ALERT_LEVEL (P4)

    payload 可以是 dict 或 JSON 字符串。
    """
    if payload is None:
        return DEFAULT_ALERT_LEVEL

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return DEFAULT_ALERT_LEVEL

    if not isinstance(payload, dict):
        return DEFAULT_ALERT_LEVEL

    # 1) 顶层 alert_level
    lv = payload.get("alert_level")
    if isinstance(lv, str) and lv in ALERT_LEVELS:
        return lv

    # 2) content.alert_level
    content = payload.get("content")
    if isinstance(content, dict):
        lv2 = content.get("alert_level")
        if isinstance(lv2, str) and lv2 in ALERT_LEVELS:
            return lv2

    # 3) priority 映射
    prio = payload.get("priority")
    if isinstance(prio, str) and prio in PRIORITY_TO_ALERT_LEVEL:
        return PRIORITY_TO_ALERT_LEVEL[prio]

    return DEFAULT_ALERT_LEVEL


# ═══════════════════ 队列查询 ═══════════════════


def get_quiet_hours_digest(qm: Any) -> List[Dict[str, Any]]:
    """查 push_outbox 中 status='quiet_held' 的条目，按 id 升序（等于攒入顺序）。

    返回 [{event_id, entity_key, producer, status, retries, db_created_at,
           message_type, priority, alert_level, content, created_at,
           target_channel, next_push_at}, ...]

    qm 是 QueueManager（需有 .conn 属性 + ._ensure_open()）。
    """
    qm._ensure_open()
    cur = qm.conn.cursor()
    try:
        rows = cur.execute(
            """SELECT * FROM message_queue
               WHERE queue_name = ? AND status = ?
               ORDER BY id ASC""",
            (PUSH_OUTBOX_QUEUE, QUIET_HELD_STATUS),
        ).fetchall()
    finally:
        cur.close()

    return [_row_to_digest_dict(r) for r in rows]


# ═══════════════════ 内部辅助 ═══════════════════


def _parse_hhmm(s: str, field_name: str) -> time:
    """'07:30' → time(7, 30)。失败抛 ValueError。"""
    if not isinstance(s, str) or ":" not in s:
        raise ValueError(
            f"push.quiet_hours.{field_name} must be 'HH:MM', got {s!r}"
        )
    try:
        hh, mm = s.split(":", 1)
        h, m = int(hh), int(mm)
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"push.quiet_hours.{field_name} malformed {s!r}: {e}"
        ) from e
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(
            f"push.quiet_hours.{field_name}={s!r} out of range HH:MM"
        )
    return time(h, m)


def _row_to_digest_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """镜像 infra.push_queue._row_to_push_dict，但多带一个 alert_level 字段。"""
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
        "message_type": payload.get("message_type"),
        "priority": payload.get("priority"),
        "alert_level": extract_alert_level(payload),
        "content": payload.get("content") or {},
        "created_at": payload.get("created_at"),
        "target_channel": payload.get("target_channel"),
        "next_push_at": payload.get("next_push_at"),
    }
