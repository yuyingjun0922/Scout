"""
agents/push_consumer_agent.py — 推送消费 Agent (v1.12 Phase 2A 最终任务)

Phase 2A 定位：MCP 是主通道（Claude Desktop / OpenClaw 主动拉取），push_outbox
只是"未读收件箱"。本 Agent 负责让收件箱保持整洁：

职责（操作手册 §12 / v1.12 task spec）：
  1. 定期扫描 pending  → 复用 PushQueue.poll_pending
  2. 按优先级排序       → PushQueue 已自带（red > yellow > blue > white）
  3. 过期清理           → >14 天 pending 自动标 failed（reason='expired'）
  4. 频率控制           → 同类型消息 1 小时窗口内最多 3 条，超出者标 failed
                         （reason='rate_limit_1h_3'），保最老 3 条
  5. 推送到备用通道     → Phase 2A 留白（无 Telegram/WeChat/邮件 consumer）；
                         框架就位，Phase 2B 接入。

run() 返回 {scanned, expired, rate_limited, pending_after, ts_utc}。

调度：
  - hourly IntervalTrigger → run()
  - 09:30 KST CronTrigger → build_daily_digest()

兼容性：
  - 不破坏现有 PushQueue 生产者（DirectionJudge / main.py alert）
  - 新 Agent 走 produce_from_recommendation / produce_from_drift / ... 辅助
    方法直接往 push_outbox 写
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from agents.base import BaseAgent, RuleViolation
from infra.db_manager import DatabaseManager
from infra.push_queue import (
    PRIORITY_ORDER,
    PushQueue,
    QUEUE_NAME,
    VALID_ALERT_TYPES,
)
from infra.queue_manager import QueueManager
from utils.time_utils import now_utc


# ═══════════════════ 常量 ═══════════════════

EXPIRE_DAYS = 14                       # pending > 14 天 → failed(expired)
DEDUPE_WINDOW_MINUTES = 60             # 同类型 1 小时窗口
DEDUPE_MAX_PER_TYPE = 3                # 同类型最多 3 条

# urgent / normal 分类（供 MCP get_pending_messages 过滤）
URGENT_PRIORITIES = frozenset({"red", "yellow"})
NORMAL_PRIORITIES = frozenset({"blue", "white"})

EXPIRE_REASON = "expired_14d"
RATE_LIMIT_REASON = f"rate_limit_1h_{DEDUPE_MAX_PER_TYPE}"


# ═══════════════════ Agent ═══════════════════

class PushConsumerAgent(BaseAgent):
    """v1.12 推送消费 Agent。

    构造方式：
        agent = PushConsumerAgent(kdb=kdb, qm=qm)
        # 或透传已创建的 push_queue
        agent = PushConsumerAgent(kdb=kdb, push_queue=push_queue)
    """

    def __init__(
        self,
        kdb: DatabaseManager,
        qm: Optional[QueueManager] = None,
        push_queue: Optional[PushQueue] = None,
    ):
        if push_queue is None and qm is None:
            raise RuleViolation("PushConsumerAgent requires push_queue or qm")
        super().__init__(name="push_consumer_agent", db=kdb)
        self.push_queue = push_queue or PushQueue(qm)
        # qm 引用保留用于直接 SQL（cleanup / dedupe 需要）
        self.qm = push_queue.qm if push_queue is not None else qm

    # ─────────────── 主入口 ───────────────

    def run(self) -> Dict[str, Any]:
        """hourly 入口：cleanup_expired + deduplicate_in_hour + 扫描统计。

        永不抛：内部吞错，记录到 agent_errors。
        """
        result = self.run_with_error_handling(self._run_impl)
        if result is None:
            return {
                "scanned": 0,
                "expired": 0,
                "rate_limited": 0,
                "pending_after": 0,
                "ts_utc": now_utc(),
                "ok": False,
            }
        return result

    def _run_impl(self) -> Dict[str, Any]:
        expired = self.cleanup_expired()
        rate_limited = self.deduplicate_in_hour()
        pending = self.push_queue.queue_stats().get("pending", 0)
        scanned = expired + rate_limited + pending
        result = {
            "scanned": scanned,
            "expired": expired,
            "rate_limited": rate_limited,
            "pending_after": pending,
            "ts_utc": now_utc(),
            "ok": True,
        }
        self.logger.info(
            f"[push_consumer] expired={expired} rate_limited={rate_limited} "
            f"pending_after={pending}"
        )
        return result

    # ─────────────── 扫描 / 观察 ───────────────

    def scan_pending(self, max: int = 50) -> List[Dict[str, Any]]:
        """按优先级排序的待推消息（read-only，不改状态）。直接复用 PushQueue。"""
        return self.push_queue.poll_pending(max=max)

    def scan_by_priority(
        self,
        kind: str = "all",
        max: int = 50,
    ) -> List[Dict[str, Any]]:
        """kind: 'all' | 'urgent' (red/yellow) | 'normal' (blue/white)."""
        if kind not in ("all", "urgent", "normal"):
            raise RuleViolation(
                f"kind must be 'all'|'urgent'|'normal', got {kind!r}"
            )
        items = self.scan_pending(max=max * 2)  # 先多取，再过滤
        if kind == "all":
            return items[:max]
        allow = URGENT_PRIORITIES if kind == "urgent" else NORMAL_PRIORITIES
        filtered = [m for m in items if m.get("priority") in allow]
        return filtered[:max]

    # ─────────────── 过期清理 ───────────────

    def cleanup_expired(self) -> int:
        """pending > EXPIRE_DAYS 天 → failed(reason=expired_14d)。返回清理条数。"""
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(days=EXPIRE_DAYS)
        ).isoformat()
        try:
            self.qm._ensure_open()
            cur = self.qm.conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                updated = cur.execute(
                    """UPDATE message_queue
                       SET status='failed',
                           processed_at=?,
                           error_message=?
                       WHERE queue_name=?
                         AND status='pending'
                         AND created_at < ?""",
                    (now_utc(), EXPIRE_REASON, QUEUE_NAME, cutoff),
                )
                self.qm.conn.commit()
                return int(updated.rowcount)
            except Exception:
                self.qm.conn.rollback()
                raise
            finally:
                cur.close()
        except sqlite3.Error as e:
            self.logger.warning(f"[push_consumer] cleanup_expired DB error: {e}")
            return 0

    # ─────────────── 频率去重 ───────────────

    def deduplicate_in_hour(self) -> int:
        """同 (message_type, alert_type) 分组，1h 窗口内保留最老 3 条，其余标 failed。

        使用"从最新一条起回看 1h"的滑动窗口定义：
            对每组，以最新一条 created_at 为锚点，往前 1h 内若超过 3 条 → 多余标 rate_limit。
        返回被标记的条数。
        """
        try:
            self.qm._ensure_open()
            cur = self.qm.conn.cursor()
            try:
                rows = cur.execute(
                    """SELECT event_id, entity_key, payload, created_at
                       FROM message_queue
                       WHERE queue_name=? AND status='pending'
                       ORDER BY created_at ASC""",
                    (QUEUE_NAME,),
                ).fetchall()
            finally:
                cur.close()
        except sqlite3.Error as e:
            self.logger.warning(
                f"[push_consumer] deduplicate_in_hour scan DB error: {e}"
            )
            return 0

        if not rows:
            return 0

        # 按 group key 分桶
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else {}
            except (ValueError, TypeError):
                payload = {}
            msg_type = payload.get("message_type") or ""
            alert_type = (payload.get("content") or {}).get("alert_type") or ""
            key = f"{msg_type}|{alert_type}"
            groups.setdefault(key, []).append({
                "event_id": r["event_id"],
                "entity_key": r["entity_key"],
                "created_at": r["created_at"] or "",
            })

        # 找出每组内 1h 窗口 ≥ DEDUPE_MAX_PER_TYPE+1 的多余条
        to_cancel: List[str] = []
        window = timedelta(minutes=DEDUPE_WINDOW_MINUTES)
        for key, items in groups.items():
            if len(items) <= DEDUPE_MAX_PER_TYPE:
                continue
            # items 已按 created_at ASC 排序（SQL ORDER BY）
            # 以每条为窗口起点，若 [i, i+DEDUPE_MAX_PER_TYPE] 都在 1h 内 → 多余
            parsed = []
            for it in items:
                try:
                    dt = datetime.fromisoformat(it["created_at"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                parsed.append((dt, it))
            parsed.sort(key=lambda p: p[0])

            # 滑动窗口：若 parsed[j+MAX] - parsed[j] <= window，则 parsed[j+MAX] 是多余
            for j in range(len(parsed) - DEDUPE_MAX_PER_TYPE):
                anchor_dt = parsed[j][0]
                victim_dt, victim_it = parsed[j + DEDUPE_MAX_PER_TYPE]
                if (victim_dt - anchor_dt) <= window:
                    to_cancel.append(victim_it["event_id"])

        if not to_cancel:
            return 0

        # 去重 event_ids（同一条可能被多窗口标记）
        unique_ids = list(dict.fromkeys(to_cancel))
        cancelled = 0
        for event_id in unique_ids:
            try:
                self.qm._ensure_open()
                cur = self.qm.conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    updated = cur.execute(
                        """UPDATE message_queue
                           SET status='failed',
                               processed_at=?,
                               error_message=?
                           WHERE event_id=? AND queue_name=?
                             AND status='pending'""",
                        (now_utc(), RATE_LIMIT_REASON, event_id, QUEUE_NAME),
                    )
                    self.qm.conn.commit()
                    cancelled += int(updated.rowcount)
                except Exception:
                    self.qm.conn.rollback()
                    raise
                finally:
                    cur.close()
            except sqlite3.Error as e:
                self.logger.warning(
                    f"[push_consumer] dedupe cancel {event_id}: {e}"
                )
        return cancelled

    # ─────────────── 辅助：生产者（给其它 Agent 用） ───────────────

    def produce_from_recommendation(
        self,
        rec: Dict[str, Any],
    ) -> Optional[str]:
        """RecommendationAgent 结果 → push_outbox。

        规则：
            A 级别 → message_type='recommendation', priority='red'（立即）
            B 级别 → message_type='recommendation', priority='blue'（日常）
            candidate / reject / 错误 / 缺级别 → 跳过

        entity_key = recommendation_{stock}_{thesis_hash}，同 thesis 只推一次。
        返回 event_id；跳过时返 None。
        """
        if not isinstance(rec, dict):
            raise RuleViolation("rec must be dict")
        level = rec.get("level")
        stock = rec.get("stock")
        if not stock or level not in ("A", "B"):
            return None
        priority = "red" if level == "A" else "blue"
        thesis = rec.get("thesis_hash") or rec.get("recommended_at") or ""
        entity_key = f"recommendation_{stock}_{thesis}"
        content = {
            "stock": stock,
            "industry": rec.get("industry"),
            "level": level,
            "total_score": rec.get("total_score"),
            "dimensions": rec.get("dimensions"),
            "counter_card": rec.get("counter_card"),
            "recommended_at": rec.get("recommended_at"),
        }
        return self.push_queue.push(
            message_type="recommendation",
            content=content,
            priority=priority,
            producer="recommendation_agent",
            entity_key=entity_key,
        )

    def produce_from_drift(
        self,
        detection: Dict[str, Any],
    ) -> Optional[str]:
        """MotivationDrift 结果 → push_outbox（alert）。

        规则：
            state='reversing' → priority='red'（立即）
            state='drifting'  → priority='blue'（日常摘要）
            state='stable'    → 跳过

        entity_key 复用 push_alert 默认（alert_motivation_drift_{KST_date}）。
        但因同行业同日可能多次检测，这里 override 为
        alert_motivation_drift_{industry}_{KST_date} 保证行业粒度去重。
        """
        if not isinstance(detection, dict):
            raise RuleViolation("detection must be dict")
        state = detection.get("state")
        industry = detection.get("industry")
        if not industry or state not in ("reversing", "drifting"):
            return None
        priority = "red" if state == "reversing" else "blue"
        kst_date = _today_kst_date_str()
        entity_key = f"alert_motivation_drift_{industry}_{kst_date}"
        content = {
            "industry": industry,
            "state": state,
            "signals": detection.get("signals") or [],
            "triggered": detection.get("triggered") or [],
            "detected_at": detection.get("detected_at"),
        }
        return self.push_queue.push_alert(
            alert_type="motivation_drift",
            content=content,
            priority=priority,
            producer="motivation_drift_agent",
            entity_key=entity_key,
        )

    def produce_from_financial_distress(
        self,
        snapshot: Dict[str, Any],
    ) -> Optional[str]:
        """FinancialAgent Z''-1995 < 1.81 → push_alert（failure_level_change）。

        snapshot 期望字段：{stock, z_score, report_period}。
        entity_key = alert_z_distress_{stock}_{KST_date}。
        """
        if not isinstance(snapshot, dict):
            raise RuleViolation("snapshot must be dict")
        stock = snapshot.get("stock")
        z = snapshot.get("z_score")
        if not stock or z is None:
            return None
        if z >= 1.81:  # 非困境区间不推送
            return None
        kst_date = _today_kst_date_str()
        entity_key = f"alert_z_distress_{stock}_{kst_date}"
        content = {
            "stock": stock,
            "z_score": z,
            "report_period": snapshot.get("report_period"),
            "threshold": 1.81,
            "note": f"Z''-1995={z} < 1.81（困境区），建议复核",
        }
        return self.push_queue.push_alert(
            alert_type="failure_level_change",
            content=content,
            priority="red",
            producer="financial_agent",
            entity_key=entity_key,
        )

    def produce_from_bias_warnings(
        self,
        result: Dict[str, Any],
    ) -> Optional[str]:
        """BiasChecker 结果 → push_alert。

        触发条件：bias_warnings.warnings 条数 ≥ 3（与 downgrade 同阈值）。
        entity_key = alert_bias_{stock_or_industry}_{KST_date}。
        """
        if not isinstance(result, dict):
            raise RuleViolation("result must be dict")
        warnings_block = result.get("bias_warnings") or {}
        warnings = warnings_block.get("warnings") or []
        if len(warnings) < 3:
            return None
        anchor = (
            result.get("stock")
            or result.get("industry")
            or "unknown"
        )
        kst_date = _today_kst_date_str()
        entity_key = f"alert_bias_{anchor}_{kst_date}"
        content = {
            "anchor": anchor,
            "stock": result.get("stock"),
            "industry": result.get("industry"),
            "stage": warnings_block.get("stage"),
            "warnings_count": len(warnings),
            "downgrade": warnings_block.get("downgrade", False),
            "warnings": warnings[:5],  # 只带前 5 条样本
        }
        # bias 警告归 failure_level_change 类（意味决策级别会被下调）
        return self.push_queue.push_alert(
            alert_type="failure_level_change",
            content=content,
            priority="yellow",
            producer="bias_checker",
            entity_key=entity_key,
        )

    # ─────────────── 日终 digest（09:30 KST）───────────────

    def build_daily_digest(self) -> Optional[str]:
        """把当前 pending 中 blue/white 消息汇总成一条 daily_briefing 推送。

        - red/yellow 保持单独推送（urgent）不进 digest
        - 若 blue/white pending 为空 → 返 None（不推空简报）

        digest 自身用 push_daily_briefing（entity_key 带 KST 日期，天然去重）
        """
        normal_items = self.scan_by_priority(kind="normal", max=50)
        if not normal_items:
            return None
        # digest content
        summary: List[Dict[str, Any]] = []
        for m in normal_items:
            summary.append({
                "event_id": m.get("event_id"),
                "message_type": m.get("message_type"),
                "priority": m.get("priority"),
                "preview": _preview_content(m.get("content")),
                "created_at": m.get("created_at"),
            })
        content = {
            "digest_kind": "normal_pending",
            "count": len(summary),
            "items": summary,
            "note": (
                f"{len(summary)} 条 blue/white 待读消息。通过 MCP "
                "get_pending_messages(kind='normal') 逐条查看，"
                "mark_read(event_id) 标为已读。"
            ),
        }
        return self.push_queue.push_daily_briefing(
            content=content, priority="blue", producer="push_consumer_agent",
        )

    # ─────────────── 已读标记（供 MCP 调用）───────────────

    def mark_read(self, event_id: str) -> bool:
        """MCP 用户点 mark_read → PushQueue.mark_delivered。"""
        return self.push_queue.mark_delivered(event_id)

    # ─────────────── 外部通道投递（v1.13 QQ）───────────────

    def deliver_pending(
        self,
        channel: Any,
        max: int = 10,
        producer: Optional[str] = None,
        entity_key_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把 pending 消息通过 channel.send(text) 投递到外部（QQ/Telegram 等）。

        channel 接口契约：`send(text: str) -> (ok: bool, detail: dict)`，不 raise。

        过滤：
            producer          — 只投递指定 producer（例如 'manual_test'）
            entity_key_prefix — entity_key.startswith(前缀) 才投递

        语义：
            ok=True  → PushQueue.mark_delivered(event_id)
            ok=False → PushQueue.mark_failed(event_id, reason)
                       （retries < MAX 会被 push_queue 回滚为 pending，MAX 后才 failed）

        返回：{delivered, failed, skipped_filter, total_considered, errors[≤5], ts_utc}
        """
        if channel is None or not hasattr(channel, "send"):
            raise RuleViolation("channel must expose send(text)->(ok,detail)")

        # 多取一些再在内存过滤（queue 里 filter 代价也不小）
        raw_pending = self.push_queue.poll_pending(max=max * 3)
        considered: List[Dict[str, Any]] = []
        skipped_filter = 0
        for m in raw_pending:
            if producer is not None and m.get("producer") != producer:
                skipped_filter += 1
                continue
            if entity_key_prefix is not None and not (m.get("entity_key") or "").startswith(
                entity_key_prefix
            ):
                skipped_filter += 1
                continue
            considered.append(m)
            if len(considered) >= max:
                break

        delivered = 0
        failed = 0
        errors: List[Dict[str, Any]] = []
        for m in considered:
            text = _format_for_qq(m)
            try:
                ok, detail = channel.send(text)
            except Exception as e:
                ok, detail = False, {"error": f"channel_raised: {type(e).__name__}: {e}"}

            if ok:
                self.push_queue.mark_delivered(m["event_id"])
                delivered += 1
                self.logger.info(
                    f"[push_consumer] delivered event_id={m['event_id']} "
                    f"msg_type={m.get('message_type')} priority={m.get('priority')}"
                )
            else:
                reason = str(detail.get("error") or detail.get("http_status") or "unknown")[:120]
                self.push_queue.mark_failed(m["event_id"], f"channel_send: {reason}")
                failed += 1
                if len(errors) < 5:
                    errors.append({"event_id": m["event_id"], "detail": detail})
                self.logger.warning(
                    f"[push_consumer] send_failed event_id={m['event_id']} detail={detail}"
                )

        return {
            "delivered": delivered,
            "failed": failed,
            "skipped_filter": skipped_filter,
            "total_considered": len(considered),
            "errors": errors,
            "ts_utc": now_utc(),
        }


# ═══════════════════ 工具 ═══════════════════

_MT_PREFIX = {
    "alert": "🔴",
    "recommendation": "🏆",
    "daily_briefing": "📅",
    "weekly_report": "📊",
}


def _format_for_qq(m: Dict[str, Any], max_len: int = 900) -> str:
    """结构化 push 消息 dict → 适合 QQ C2C 的短文本（≤ max_len 字符）。

    不同 message_type 分支展示：保留 stock / industry / state / alert_type 等关键字段。
    未知类型 fallback 为 content JSON 截断。
    """
    mt = m.get("message_type") or "message"
    priority = m.get("priority") or "blue"
    content = m.get("content") or {}
    head = f"{_MT_PREFIX.get(mt, '📨')} [{mt} · {priority}]"
    lines: List[str] = [head]

    if mt == "alert":
        at = content.get("alert_type") or "unknown"
        # v1.13 心跳特判：🟢 替代默认 🔴
        if at == "system_health":
            lines[0] = f"🟢 [health · {priority}]"
        lines.append(f"类型: {at}")
        for k, label in (
            ("industry", "行业"),
            ("state", "状态"),
            ("stock", "股票"),
            ("agent", "Agent"),
            ("z_score", "Z分"),
            ("anchor", "锚点"),
            ("note", "备注"),
        ):
            v = content.get(k)
            if v not in (None, ""):
                lines.append(f"{label}: {v}")
        sigs = content.get("signals")
        if isinstance(sigs, list) and sigs:
            lines.append(f"信号数: {len(sigs)}")
    elif mt == "recommendation":
        lines.append(
            f"股票: {content.get('stock', '?')}  等级: {content.get('level', '?')}  "
            f"总分: {content.get('total_score', '?')}"
        )
        if content.get("industry"):
            lines.append(f"行业: {content['industry']}")
        cc = content.get("counter_card")
        if isinstance(cc, dict):
            risks = cc.get("risks") or []
            if risks:
                lines.append(f"风险: {risks[0]}")
    elif mt == "daily_briefing":
        count = content.get("count")
        if count is not None:
            lines.append(f"待读: {count} 条")
        h = content.get("highlights") or []
        if isinstance(h, list) and h:
            lines.append("Highlights:")
            for x in h[:5]:
                lines.append(f"· {x}")
        note = content.get("note")
        if isinstance(note, str) and note:
            lines.append(note[:200])
    elif mt == "weekly_report":
        rt = content.get("report_type") or "report"
        rd = content.get("report_date") or ""
        lines.append(f"类型: {rt}  日期: {rd}")
        preview = content.get("report_preview") or ""
        if preview:
            lines.append(preview[:500])
    else:
        try:
            lines.append(json.dumps(content, ensure_ascii=False)[:500])
        except Exception:
            lines.append(str(content)[:500])

    text = "\n".join(lines)
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def _preview_content(content: Any, max_len: int = 200) -> str:
    """content dict → 简短中文预览字符串（给 digest 用）。"""
    if not isinstance(content, dict):
        return str(content)[:max_len] if content else ""
    # 常见字段优先
    for key in ("note", "message", "title", "stock", "industry"):
        v = content.get(key)
        if isinstance(v, str) and v:
            return v[:max_len]
    # fallback：dump 前几 key
    try:
        brief = {
            k: v for k, v in list(content.items())[:3]
            if not isinstance(v, (list, dict))
        }
        return json.dumps(brief, ensure_ascii=False)[:max_len]
    except Exception:
        return str(content)[:max_len]


def _today_kst_date_str() -> str:
    """当前 KST 日期 YYYYMMDD。延迟 import 避免循环。"""
    from zoneinfo import ZoneInfo
    return datetime.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
