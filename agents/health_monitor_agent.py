"""
agents/health_monitor_agent.py — v1.13 Phase 2A 健康监控 Agent

职责：
  1. check_errors(window_minutes=15)
     - 扫 agent_errors 近 15 分钟、resolved=0 的错误
     - 按 agent_name 分组，计数
     - 每个出错 agent 生成 1 条 push_alert:
         alert_type='data_source_down'（沿用现有枚举）
         priority='yellow' 当 count ≤ 2；'red' 当 count > 2
         content: {agent, note, signals (前 3 条 error_message), count}
     - entity_key = alert_agent_errors_{agent_name}_{bucket15min}
       同一桶 + 同 agent 只生成一条（push_queue 的 SELECT-before-INSERT 幂等）

  2. daily_heartbeat()（每天 09:30 KST 触发）
     - 从 knowledge.db + queue.db 汇总：
         total_recs / yesterday_new / active_industries / pending_push /
         errors_24h / last_info_unit_at
     - 生成 1 条 push_alert:
         alert_type='system_health'
         priority='white'
         content: {note 一句话总览, signals 4 行关键指标, stats 原始字段}
     - entity_key = alert_system_health_heartbeat_{KST_date}
       每日只推一条

**生产者 producer='health_monitor'**，供 push_consumer.deliver_pending
按 producer 分类（可选过滤）。

**run_check_errors / run_daily_heartbeat** 包 run_with_error_handling，
不会因健康扫描自身失败把 ScoutRunner 带崩。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from agents.base import BaseAgent, RuleViolation
from infra.db_manager import DatabaseManager
from infra.push_queue import PushQueue
from utils.time_utils import now_utc


ERROR_WINDOW_MINUTES = 15
ERROR_BUCKET_SECONDS = 900  # 15 分钟桶
YELLOW_RED_THRESHOLD = 2     # count ≤ 阈值 → yellow；> 阈值 → red

KST = ZoneInfo("Asia/Seoul")


class HealthMonitorAgent(BaseAgent):
    """v1.13 健康监控：近期错误扫描 + 每日心跳。"""

    def __init__(
        self,
        kdb: DatabaseManager,
        push_queue: PushQueue,
    ):
        if kdb is None:
            raise RuleViolation("HealthMonitorAgent requires kdb")
        if push_queue is None:
            raise RuleViolation("HealthMonitorAgent requires push_queue")
        super().__init__(name="health_monitor", db=kdb)
        self.kdb = kdb
        self.push_queue = push_queue

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """BaseAgent 要求的默认入口 = check_errors（15 分钟窗口）。"""
        return self.run_check_errors()

    # ─────────────── 1. check_errors ───────────────

    def run_check_errors(self, window_minutes: int = ERROR_WINDOW_MINUTES) -> Dict[str, Any]:
        result = self.run_with_error_handling(self._check_errors_impl, window_minutes)
        if result is None:
            return {"checked_errors": 0, "alerts_pushed": 0, "ok": False, "ts_utc": now_utc()}
        return result

    def _check_errors_impl(self, window_minutes: int) -> Dict[str, Any]:
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat()

        rows = self.kdb.query(
            """SELECT id, agent_name, error_type, error_message, occurred_at
               FROM agent_errors
               WHERE occurred_at > ? AND resolved = 0
               ORDER BY occurred_at ASC""",
            (cutoff,),
        )

        # 按 agent_name 分组
        groups: Dict[str, List[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault(r["agent_name"], []).append(r)

        bucket = int(time.time()) // ERROR_BUCKET_SECONDS
        pushed: List[Dict[str, Any]] = []
        for agent_name, errs in groups.items():
            count = len(errs)
            priority = "red" if count > YELLOW_RED_THRESHOLD else "yellow"
            signals = [
                f"[{e['error_type']}] {(e['error_message'] or '')[:100]}"
                for e in errs[:3]
            ]
            content = {
                "agent": agent_name,
                "count": count,
                "window_minutes": window_minutes,
                "note": (
                    f"{agent_name} 最近 {window_minutes} 分钟内 {count} 次错误"
                    f"（{priority} 级）"
                ),
                "signals": signals,
            }
            entity_key = f"alert_agent_errors_{agent_name}_{bucket}"
            try:
                event_id = self.push_queue.push_alert(
                    alert_type="data_source_down",
                    content=content,
                    priority=priority,
                    producer="health_monitor",
                    entity_key=entity_key,
                )
                pushed.append({
                    "agent": agent_name, "count": count,
                    "priority": priority, "event_id": event_id,
                })
                self.logger.info(
                    f"[health:errors] {agent_name} count={count} "
                    f"priority={priority} event_id={event_id[:8]}"
                )
            except Exception as e:
                self.logger.warning(
                    f"[health:errors] push_alert failed for {agent_name}: {e}"
                )

        return {
            "checked_errors": len(rows),
            "agents_with_errors": len(groups),
            "alerts_pushed": len(pushed),
            "window_minutes": window_minutes,
            "pushed": pushed,
            "ts_utc": now_utc(),
            "ok": True,
        }

    # ─────────────── 2. daily_heartbeat ───────────────

    def run_daily_heartbeat(self) -> Dict[str, Any]:
        result = self.run_with_error_handling(self._daily_heartbeat_impl)
        if result is None:
            return {"event_id": None, "ok": False, "ts_utc": now_utc()}
        return result

    def _daily_heartbeat_impl(self) -> Dict[str, Any]:
        stats = self._collect_heartbeat_stats()

        note_parts = [
            f"Scout 运行中（推荐 {stats['total_recs']} 条，昨日新增 {stats['yesterday_new_recs']}）"
        ]
        if stats["last_info_unit_at"]:
            note_parts.append(f"最后采集 {stats['last_info_unit_at_kst']}")
        note = "，".join(note_parts)

        signals = [
            f"活跃行业: {stats['active_industries']} / {stats['total_industries']}",
            f"待推送: {stats['pending_push']}",
            f"24h 错误: {stats['errors_24h']}",
            f"最后采集: {stats['last_info_unit_at_kst'] or 'N/A'}",
        ]

        content: Dict[str, Any] = {
            "note": note,
            "signals": signals,
            **stats,
        }
        kst_date = _today_kst_date_str()
        entity_key = f"alert_system_health_heartbeat_{kst_date}"

        try:
            event_id = self.push_queue.push_alert(
                alert_type="system_health",
                content=content,
                priority="white",
                producer="health_monitor",
                entity_key=entity_key,
            )
            self.logger.info(
                f"[health:heartbeat] pushed event_id={event_id[:8]} "
                f"stats={stats}"
            )
            return {"event_id": event_id, "stats": stats, "ok": True, "ts_utc": now_utc()}
        except Exception as e:
            self.logger.warning(f"[health:heartbeat] push failed: {e}")
            return {"event_id": None, "error": str(e), "ok": False, "ts_utc": now_utc()}

    def _collect_heartbeat_stats(self) -> Dict[str, Any]:
        """汇总 knowledge.db + queue.db 关键指标。不 raise — 单项失败返默认值。"""
        stats: Dict[str, Any] = {
            "total_recs": 0,
            "yesterday_new_recs": 0,
            "active_industries": 0,
            "total_industries": 0,
            "pending_push": 0,
            "errors_24h": 0,
            "last_info_unit_at": None,
            "last_info_unit_at_kst": None,
        }

        try:
            row = self.kdb.query_one("SELECT COUNT(*) AS c FROM recommendations")
            stats["total_recs"] = int(row["c"]) if row else 0
        except sqlite3.Error:
            pass

        try:
            # 昨日（KST） UTC 区间：[昨日 00:00 KST → 今日 00:00 KST)
            now_kst = datetime.now(tz=KST)
            today_kst_midnight = now_kst.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            yesterday_kst_midnight = today_kst_midnight - timedelta(days=1)
            y_start_utc = yesterday_kst_midnight.astimezone(timezone.utc).isoformat()
            y_end_utc = today_kst_midnight.astimezone(timezone.utc).isoformat()
            row = self.kdb.query_one(
                """SELECT COUNT(*) AS c FROM recommendations
                   WHERE recommended_at >= ? AND recommended_at < ?""",
                (y_start_utc, y_end_utc),
            )
            stats["yesterday_new_recs"] = int(row["c"]) if row else 0
        except sqlite3.Error:
            pass

        try:
            row = self.kdb.query_one(
                """SELECT
                     SUM(CASE WHEN zone='active' THEN 1 ELSE 0 END) AS act,
                     COUNT(*) AS total
                   FROM watchlist"""
            )
            if row:
                stats["active_industries"] = int(row["act"] or 0)
                stats["total_industries"] = int(row["total"] or 0)
        except sqlite3.Error:
            pass

        try:
            qs = self.push_queue.queue_stats()
            stats["pending_push"] = int(qs.get("pending", 0))
        except Exception:
            pass

        try:
            cutoff_24h = (
                datetime.now(tz=timezone.utc) - timedelta(hours=24)
            ).isoformat()
            row = self.kdb.query_one(
                "SELECT COUNT(*) AS c FROM agent_errors WHERE occurred_at > ?",
                (cutoff_24h,),
            )
            stats["errors_24h"] = int(row["c"]) if row else 0
        except sqlite3.Error:
            pass

        try:
            row = self.kdb.query_one("SELECT MAX(created_at) AS ts FROM info_units")
            if row and row["ts"]:
                stats["last_info_unit_at"] = row["ts"]
                # 转 KST 显示
                try:
                    dt = datetime.fromisoformat(row["ts"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    stats["last_info_unit_at_kst"] = dt.astimezone(KST).strftime(
                        "%m-%d %H:%M KST"
                    )
                except ValueError:
                    stats["last_info_unit_at_kst"] = row["ts"][:16]
        except sqlite3.Error:
            pass

        return stats


# ═══════════════════ 工具 ═══════════════════

def _today_kst_date_str() -> str:
    return datetime.now(tz=KST).strftime("%Y%m%d")
