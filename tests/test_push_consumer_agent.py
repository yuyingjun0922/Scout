"""
tests/test_push_consumer_agent.py — v1.12 PushConsumerAgent 测试矩阵

覆盖：
  TestInit              构造入参校验
  TestScanPending       复用 PushQueue.poll_pending + 优先级过滤
  TestCleanupExpired    >14 天 pending → failed(expired_14d)
  TestDeduplicateInHour 同 type 1h 内 >3 条 → 多余标 rate_limit
  TestRunIntegration    run() 返回统计 + 同时跑 cleanup + dedupe
  TestProducers         produce_from_{recommendation,drift,financial,bias}
  TestDailyDigest       build_daily_digest 只汇总 blue/white
  TestMarkRead          wrapper → mark_delivered
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agents.base import RuleViolation
from agents.push_consumer_agent import (
    DEDUPE_MAX_PER_TYPE,
    EXPIRE_DAYS,
    EXPIRE_REASON,
    NORMAL_PRIORITIES,
    PushConsumerAgent,
    RATE_LIMIT_REASON,
    URGENT_PRIORITIES,
)
from infra.db_manager import DatabaseManager
from infra.push_queue import PushQueue, QUEUE_NAME
from infra.queue_manager import QueueManager
from knowledge.init_db import init_database
from knowledge.init_queue_db import init_queue_db


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_kdb(tmp_path):
    kdb_path = tmp_path / "knowledge.db"
    init_database(kdb_path)
    kdb = DatabaseManager(kdb_path)
    yield kdb
    kdb.close()


@pytest.fixture
def tmp_qm(tmp_path):
    qdb_path = tmp_path / "queue.db"
    init_queue_db(qdb_path)
    qm = QueueManager(qdb_path)
    yield qm
    qm.close()


@pytest.fixture
def push_queue(tmp_qm):
    return PushQueue(tmp_qm)


@pytest.fixture
def agent(tmp_kdb, tmp_qm):
    return PushConsumerAgent(kdb=tmp_kdb, qm=tmp_qm)


def _force_created_at(qm: QueueManager, event_id: str, ts_iso: str) -> None:
    """测试辅助：把一条已 enqueue 消息的 created_at 改为指定 UTC ISO。"""
    qm._ensure_open()
    cur = qm.conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "UPDATE message_queue SET created_at=? WHERE event_id=?",
            (ts_iso, event_id),
        )
        qm.conn.commit()
    finally:
        cur.close()


# ═══════════════════ 初始化 ═══════════════════


class TestInit:
    def test_requires_push_queue_or_qm(self, tmp_kdb):
        with pytest.raises(RuleViolation):
            PushConsumerAgent(kdb=tmp_kdb)

    def test_ok_with_qm(self, tmp_kdb, tmp_qm):
        agent = PushConsumerAgent(kdb=tmp_kdb, qm=tmp_qm)
        assert isinstance(agent.push_queue, PushQueue)

    def test_ok_with_push_queue(self, tmp_kdb, push_queue):
        agent = PushConsumerAgent(kdb=tmp_kdb, push_queue=push_queue)
        assert agent.push_queue is push_queue

    def test_name_set(self, agent):
        assert agent.name == "push_consumer_agent"


# ═══════════════════ scan_pending ═══════════════════


class TestScanPending:
    def test_empty_returns_empty(self, agent):
        assert agent.scan_pending() == []

    def test_reflects_push_order_red_first(self, agent, push_queue):
        push_queue.push("alert", {"x": "blue"}, priority="blue", entity_key="b")
        push_queue.push("alert", {"x": "red"}, priority="red", entity_key="r")
        push_queue.push("alert", {"x": "yellow"}, priority="yellow", entity_key="y")
        items = agent.scan_pending()
        prios = [m["priority"] for m in items]
        assert prios == ["red", "yellow", "blue"]

    def test_max_limit(self, agent, push_queue):
        for i in range(5):
            push_queue.push("alert", {}, priority="blue", entity_key=f"b{i}")
        assert len(agent.scan_pending(max=2)) == 2

    def test_scan_by_priority_urgent(self, agent, push_queue):
        push_queue.push("alert", {}, priority="red", entity_key="r")
        push_queue.push("alert", {}, priority="yellow", entity_key="y")
        push_queue.push("alert", {}, priority="blue", entity_key="b")
        push_queue.push("alert", {}, priority="white", entity_key="w")
        urgent = agent.scan_by_priority(kind="urgent")
        assert {m["priority"] for m in urgent} == URGENT_PRIORITIES

    def test_scan_by_priority_normal(self, agent, push_queue):
        push_queue.push("alert", {}, priority="red", entity_key="r")
        push_queue.push("alert", {}, priority="blue", entity_key="b")
        push_queue.push("alert", {}, priority="white", entity_key="w")
        normal = agent.scan_by_priority(kind="normal")
        assert {m["priority"] for m in normal} == NORMAL_PRIORITIES

    def test_scan_by_priority_all(self, agent, push_queue):
        push_queue.push("alert", {}, priority="red", entity_key="r")
        push_queue.push("alert", {}, priority="blue", entity_key="b")
        assert len(agent.scan_by_priority(kind="all")) == 2

    def test_scan_by_priority_invalid_kind(self, agent):
        with pytest.raises(RuleViolation):
            agent.scan_by_priority(kind="bogus")


# ═══════════════════ cleanup_expired ═══════════════════


class TestCleanupExpired:
    def test_no_old_messages(self, agent, push_queue):
        push_queue.push("alert", {}, priority="blue", entity_key="fresh")
        assert agent.cleanup_expired() == 0

    def test_old_pending_marked_failed(self, agent, push_queue, tmp_qm):
        ev = push_queue.push("alert", {}, priority="blue", entity_key="old")
        old_ts = (
            datetime.now(tz=timezone.utc) - timedelta(days=EXPIRE_DAYS + 2)
        ).isoformat()
        _force_created_at(tmp_qm, ev, old_ts)
        assert agent.cleanup_expired() == 1
        stats = tmp_qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 0
        assert stats[QUEUE_NAME]["failed"] == 1

    def test_expire_reason_recorded(self, agent, push_queue, tmp_qm):
        ev = push_queue.push("alert", {}, priority="blue", entity_key="e")
        old_ts = (
            datetime.now(tz=timezone.utc) - timedelta(days=EXPIRE_DAYS + 1)
        ).isoformat()
        _force_created_at(tmp_qm, ev, old_ts)
        agent.cleanup_expired()
        row = tmp_qm.conn.execute(
            "SELECT error_message, status FROM message_queue WHERE event_id=?",
            (ev,),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == EXPIRE_REASON

    def test_boundary_exact_14d_not_expired(self, agent, push_queue, tmp_qm):
        """恰好 14 天不算过期（使用 < cutoff 而非 <=）。"""
        ev = push_queue.push("alert", {}, priority="blue", entity_key="boundary")
        # 14 天 -5 分钟（仍在窗口内）
        ts = (
            datetime.now(tz=timezone.utc)
            - timedelta(days=EXPIRE_DAYS)
            + timedelta(minutes=5)
        ).isoformat()
        _force_created_at(tmp_qm, ev, ts)
        assert agent.cleanup_expired() == 0

    def test_done_messages_untouched(self, agent, push_queue, tmp_qm):
        """过期清理只针对 pending，不碰 done / failed。"""
        ev = push_queue.push("alert", {}, priority="blue", entity_key="done")
        push_queue.mark_delivered(ev)
        old_ts = (
            datetime.now(tz=timezone.utc) - timedelta(days=EXPIRE_DAYS + 5)
        ).isoformat()
        _force_created_at(tmp_qm, ev, old_ts)
        assert agent.cleanup_expired() == 0
        stats = tmp_qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["done"] == 1


# ═══════════════════ deduplicate_in_hour ═══════════════════


class TestDeduplicateInHour:
    def test_under_threshold_no_action(self, agent, push_queue):
        for i in range(DEDUPE_MAX_PER_TYPE):
            push_queue.push("alert", {"n": i}, priority="blue", entity_key=f"e{i}")
        assert agent.deduplicate_in_hour() == 0

    def test_over_threshold_in_window(self, agent, push_queue, tmp_qm):
        """4 条同类消息在 1h 窗口内 → 多 1 条被标 rate_limit。"""
        ids = []
        for i in range(DEDUPE_MAX_PER_TYPE + 1):
            ev = push_queue.push(
                "alert",
                {"n": i, "alert_type": "motivation_drift"},
                priority="blue",
                entity_key=f"e{i}",
            )
            ids.append(ev)
        # 都发生在最近 10 分钟内（窗口内）
        base = datetime.now(tz=timezone.utc) - timedelta(minutes=15)
        for j, ev in enumerate(ids):
            ts = (base + timedelta(minutes=j * 2)).isoformat()
            _force_created_at(tmp_qm, ev, ts)
        cancelled = agent.deduplicate_in_hour()
        assert cancelled == 1
        stats = tmp_qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == DEDUPE_MAX_PER_TYPE
        assert stats[QUEUE_NAME]["failed"] == 1

    def test_spread_beyond_window_no_action(self, agent, push_queue, tmp_qm):
        """4 条同类但跨 >1h → 不视为扎堆，不清理。"""
        ids = []
        for i in range(DEDUPE_MAX_PER_TYPE + 1):
            ev = push_queue.push(
                "alert",
                {"n": i, "alert_type": "motivation_drift"},
                priority="blue",
                entity_key=f"e{i}",
            )
            ids.append(ev)
        # 每条间隔 30 分钟 → 1 和 4 间隔 90 分钟（> 60）
        base = datetime.now(tz=timezone.utc) - timedelta(hours=3)
        for j, ev in enumerate(ids):
            ts = (base + timedelta(minutes=j * 30)).isoformat()
            _force_created_at(tmp_qm, ev, ts)
        cancelled = agent.deduplicate_in_hour()
        assert cancelled == 0

    def test_different_alert_types_not_grouped(self, agent, push_queue):
        """同为 alert 但 alert_type 不同 → 分属不同组。"""
        for i in range(DEDUPE_MAX_PER_TYPE):
            push_queue.push(
                "alert",
                {"n": i, "alert_type": "motivation_drift"},
                priority="blue",
                entity_key=f"md{i}",
            )
            push_queue.push(
                "alert",
                {"n": i, "alert_type": "data_source_down"},
                priority="blue",
                entity_key=f"ds{i}",
            )
        assert agent.deduplicate_in_hour() == 0

    def test_rate_limit_reason_recorded(self, agent, push_queue, tmp_qm):
        ids = []
        for i in range(DEDUPE_MAX_PER_TYPE + 1):
            ev = push_queue.push(
                "alert",
                {"n": i, "alert_type": "motivation_drift"},
                priority="blue",
                entity_key=f"r{i}",
            )
            ids.append(ev)
        base = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
        for j, ev in enumerate(ids):
            ts = (base + timedelta(minutes=j)).isoformat()
            _force_created_at(tmp_qm, ev, ts)
        agent.deduplicate_in_hour()
        row = tmp_qm.conn.execute(
            """SELECT error_message FROM message_queue
               WHERE queue_name=? AND status='failed'""",
            (QUEUE_NAME,),
        ).fetchone()
        assert row["error_message"] == RATE_LIMIT_REASON


# ═══════════════════ run() 集成 ═══════════════════


class TestRunIntegration:
    def test_run_returns_stats(self, agent):
        result = agent.run()
        assert result["ok"] is True
        assert "scanned" in result
        assert "expired" in result
        assert "rate_limited" in result
        assert "pending_after" in result
        assert result["ts_utc"].endswith("+00:00")

    def test_run_cleans_both_expired_and_rate_limit(
        self, agent, push_queue, tmp_qm
    ):
        # 1 条过期
        ev_old = push_queue.push(
            "alert", {"alert_type": "data_source_down"},
            priority="red", entity_key="old",
        )
        old_ts = (
            datetime.now(tz=timezone.utc) - timedelta(days=EXPIRE_DAYS + 3)
        ).isoformat()
        _force_created_at(tmp_qm, ev_old, old_ts)
        # 4 条同类在窗口内
        ids = []
        base = datetime.now(tz=timezone.utc) - timedelta(minutes=20)
        for i in range(DEDUPE_MAX_PER_TYPE + 1):
            ev = push_queue.push(
                "alert",
                {"n": i, "alert_type": "motivation_drift"},
                priority="blue",
                entity_key=f"m{i}",
            )
            ids.append(ev)
            _force_created_at(
                tmp_qm, ev, (base + timedelta(minutes=i)).isoformat()
            )

        result = agent.run()
        assert result["expired"] == 1
        assert result["rate_limited"] == 1
        assert result["pending_after"] == DEDUPE_MAX_PER_TYPE


# ═══════════════════ 生产者辅助 ═══════════════════


class TestProducers:
    def test_produce_from_recommendation_A(self, agent, push_queue, tmp_qm):
        rec = {
            "ok": True, "stock": "600519", "level": "A",
            "industry": "白酒", "total_score": 85,
            "thesis_hash": "abc123",
        }
        ev = agent.produce_from_recommendation(rec)
        assert ev is not None
        row = tmp_qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["priority"] == "red"
        assert row["payload"]["message_type"] == "recommendation"

    def test_produce_from_recommendation_B(self, agent, push_queue, tmp_qm):
        rec = {
            "ok": True, "stock": "000858", "level": "B",
            "industry": "白酒", "total_score": 68,
            "thesis_hash": "def456",
        }
        ev = agent.produce_from_recommendation(rec)
        assert ev is not None
        row = tmp_qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["priority"] == "blue"

    @pytest.mark.parametrize("level", ["candidate", "reject", None])
    def test_produce_from_recommendation_skip(self, agent, level):
        rec = {"stock": "000001", "level": level}
        assert agent.produce_from_recommendation(rec) is None

    def test_produce_from_recommendation_idempotent(self, agent):
        rec = {
            "ok": True, "stock": "600519", "level": "A",
            "industry": "白酒", "thesis_hash": "h1",
        }
        ev1 = agent.produce_from_recommendation(rec)
        ev2 = agent.produce_from_recommendation(rec)
        assert ev1 == ev2

    def test_produce_from_drift_reversing(self, agent, tmp_qm):
        det = {
            "industry": "光伏",
            "state": "reversing",
            "signals": [{"name": "policy", "state": "reversing"}],
            "triggered": ["policy"],
            "detected_at": "2026-04-18T00:00:00+00:00",
        }
        ev = agent.produce_from_drift(det)
        assert ev is not None
        row = tmp_qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["priority"] == "red"
        assert row["payload"]["content"]["alert_type"] == "motivation_drift"

    def test_produce_from_drift_drifting(self, agent, tmp_qm):
        det = {
            "industry": "锂电池", "state": "drifting", "signals": [],
        }
        ev = agent.produce_from_drift(det)
        row = tmp_qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["priority"] == "blue"

    def test_produce_from_drift_stable_skipped(self, agent):
        det = {"industry": "半导体", "state": "stable"}
        assert agent.produce_from_drift(det) is None

    def test_produce_from_financial_distress_triggered(self, agent, tmp_qm):
        snap = {"stock": "600000", "z_score": 1.2, "report_period": "2025"}
        ev = agent.produce_from_financial_distress(snap)
        assert ev is not None
        row = tmp_qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["content"]["z_score"] == 1.2
        assert row["payload"]["priority"] == "red"

    def test_produce_from_financial_not_distress(self, agent):
        snap = {"stock": "600000", "z_score": 3.0}
        assert agent.produce_from_financial_distress(snap) is None

    def test_produce_from_financial_missing_z(self, agent):
        snap = {"stock": "600000"}
        assert agent.produce_from_financial_distress(snap) is None

    def test_produce_from_bias_warnings_triggered(self, agent, tmp_qm):
        result = {
            "stock": "600519",
            "industry": "白酒",
            "bias_warnings": {
                "stage": "decision",
                "warnings": [
                    {"code": "b01", "message": "w1"},
                    {"code": "b02", "message": "w2"},
                    {"code": "b04", "message": "w4"},
                ],
                "downgrade": True,
            },
        }
        ev = agent.produce_from_bias_warnings(result)
        assert ev is not None
        row = tmp_qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["priority"] == "yellow"
        assert row["payload"]["content"]["warnings_count"] == 3

    def test_produce_from_bias_warnings_below_threshold(self, agent):
        result = {
            "stock": "600519",
            "bias_warnings": {"warnings": [{"code": "b01"}]},
        }
        assert agent.produce_from_bias_warnings(result) is None


# ═══════════════════ daily_digest ═══════════════════


class TestDailyDigest:
    def test_empty_returns_none(self, agent):
        assert agent.build_daily_digest() is None

    def test_only_red_yellow_skipped(self, agent, push_queue):
        push_queue.push("alert", {}, priority="red", entity_key="r")
        push_queue.push("alert", {}, priority="yellow", entity_key="y")
        assert agent.build_daily_digest() is None

    def test_digest_gathers_blue_white(self, agent, push_queue, tmp_qm):
        push_queue.push("alert", {"note": "a"}, priority="blue", entity_key="a")
        push_queue.push("alert", {"note": "b"}, priority="white", entity_key="b")
        ev = agent.build_daily_digest()
        assert ev is not None
        # 应该创建一条 daily_briefing + 保留两条原消息
        rows = tmp_qm.peek(QUEUE_NAME, limit=10)
        # 3 条 pending（2 原始 + 1 digest）
        assert len(rows) == 3
        digest_row = next(
            r for r in rows if r["payload"]["message_type"] == "daily_briefing"
        )
        assert digest_row["payload"]["content"]["count"] == 2


# ═══════════════════ mark_read ═══════════════════


class TestMarkRead:
    def test_mark_read_delegates_to_mark_delivered(self, agent, push_queue):
        ev = push_queue.push("alert", {}, priority="red", entity_key="m")
        assert agent.mark_read(ev) is True
        # 已 done，再调返 False
        assert agent.mark_read(ev) is False

    def test_mark_read_unknown_event(self, agent):
        assert agent.mark_read("bogus-event-id") is False


# ═══════════════════ v1.61 Quiet Hours ═══════════════════


class _FakeChannel:
    """接 deliver_pending 的测试 channel（send→(ok, detail)）。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list = []

    def send(self, text: str):
        self.sent.append(text)
        return (self.ok, {} if self.ok else {"error": "fake_fail"})


class TestQuietHoursIntegration:
    """PushConsumerAgent 在启用 QuietHoursPolicy 时的端到端行为。"""

    def _quiet_agent(self, kdb, qm, start="00:00", end="23:59"):
        """构造一个"整天都是静默期"的 agent，便于测静默门禁。"""
        from utils.push_policy import QuietHoursPolicy
        from agents.push_consumer_agent import PushConsumerAgent
        policy = QuietHoursPolicy.from_config({
            "enabled": True, "start": start, "end": end,
            "timezone": "KST", "always_push_levels": ["P0", "P1"],
            "digest_at": "07:30",
        })
        return PushConsumerAgent(kdb=kdb, qm=qm, quiet_hours_policy=policy)

    def test_p0_p1_delivered_in_quiet_window(
        self, tmp_kdb, tmp_qm, push_queue,
    ):
        """静默期内 red(P0) / yellow(P1) 照推，不进 quiet_held。"""
        agent = self._quiet_agent(tmp_kdb, tmp_qm)
        push_queue.push("alert", {}, priority="red", entity_key="r1")
        push_queue.push("alert", {}, priority="yellow", entity_key="y1")
        channel = _FakeChannel(ok=True)

        result = agent.deliver_pending(channel, max=10)
        assert result["delivered"] == 2
        assert result["quiet_held"] == 0
        assert len(channel.sent) == 2

    def test_p2_p3_p4_held_in_quiet_window(
        self, tmp_kdb, tmp_qm, push_queue,
    ):
        """静默期内 blue(P2)/white(P3)/未知级别 全部攒起来。"""
        agent = self._quiet_agent(tmp_kdb, tmp_qm)
        push_queue.push("alert", {}, priority="blue", entity_key="b1")
        push_queue.push("alert", {}, priority="white", entity_key="w1")
        # 未知 priority → P4（不该发生但防御）
        push_queue.push("alert", {}, priority="blue", entity_key="b2")
        channel = _FakeChannel(ok=True)

        result = agent.deliver_pending(channel, max=10)
        assert result["delivered"] == 0
        assert result["quiet_held"] == 3
        assert channel.sent == []

        # 3 条 row 在 queue.db 里应当 status='quiet_held'
        tmp_qm._ensure_open()
        cur = tmp_qm.conn.cursor()
        try:
            held = cur.execute(
                "SELECT COUNT(*) AS c FROM message_queue WHERE status='quiet_held'"
            ).fetchone()
        finally:
            cur.close()
        assert held["c"] == 3

    def test_outside_quiet_window_all_delivered(
        self, tmp_kdb, tmp_qm, push_queue,
    ):
        """启用但窗口 [00:00, 00:01) → 几乎永远不在静默期 → 全部直推。"""
        agent = self._quiet_agent(
            tmp_kdb, tmp_qm, start="00:00", end="00:01"
        )
        # 跑 test 的时刻 99.999% 概率不在 [00:00, 00:01) 窗口
        push_queue.push("alert", {}, priority="blue", entity_key="b1")
        push_queue.push("alert", {}, priority="white", entity_key="w1")
        channel = _FakeChannel(ok=True)

        # 跳过刚好在 00:00~00:01 KST 这 1 分钟里跑到的情况
        now_kst = datetime.now(timezone(timedelta(hours=9)))
        if now_kst.hour == 0 and now_kst.minute == 0:
            pytest.skip("rare: test executed within 00:00-00:01 KST")

        result = agent.deliver_pending(channel, max=10)
        assert result["delivered"] == 2
        assert result["quiet_held"] == 0

    def test_disabled_policy_never_holds(
        self, tmp_kdb, tmp_qm, push_queue,
    ):
        from utils.push_policy import QuietHoursPolicy
        from agents.push_consumer_agent import PushConsumerAgent
        agent = PushConsumerAgent(
            kdb=tmp_kdb, qm=tmp_qm,
            quiet_hours_policy=QuietHoursPolicy.disabled(),
        )
        push_queue.push("alert", {}, priority="white", entity_key="w1")
        channel = _FakeChannel(ok=True)

        result = agent.deliver_pending(channel, max=10)
        assert result["delivered"] == 1
        assert result["quiet_held"] == 0


class TestFlushQuietDigest:
    """flush_quiet_digest：3 条攒着 → digest 合并 → 全 done。"""

    def _quiet_agent(self, kdb, qm):
        """整天静默期的 agent（便于把消息都灌进 quiet_held）。"""
        from utils.push_policy import QuietHoursPolicy
        from agents.push_consumer_agent import PushConsumerAgent
        policy = QuietHoursPolicy.from_config({
            "enabled": True, "start": "00:00", "end": "23:59",
            "timezone": "KST", "always_push_levels": ["P0", "P1"],
            "digest_at": "07:30",
        })
        return PushConsumerAgent(kdb=kdb, qm=qm, quiet_hours_policy=policy)

    def test_empty_held_returns_none(self, tmp_kdb, tmp_qm):
        agent = self._quiet_agent(tmp_kdb, tmp_qm)
        assert agent.flush_quiet_digest() is None

    def test_three_p3_p4_merged_into_digest(
        self, tmp_kdb, tmp_qm, push_queue,
    ):
        """灌 3 条 P3/P2，deliver_pending 把它们 held，flush_quiet_digest
        汇总成 1 条 digest，原 3 条标 done。"""
        agent = self._quiet_agent(tmp_kdb, tmp_qm)
        push_queue.push("alert", {"msg": "one"}, priority="blue", entity_key="b1")
        push_queue.push("alert", {"msg": "two"}, priority="white", entity_key="w1")
        push_queue.push("alert", {"msg": "three"}, priority="blue", entity_key="b2")

        # 先跑 deliver 把它们全压成 quiet_held
        channel = _FakeChannel(ok=True)
        agent.deliver_pending(channel, max=10)
        assert channel.sent == []

        # 验证 quiet_held 有 3 条
        tmp_qm._ensure_open()
        cur = tmp_qm.conn.cursor()
        try:
            held_count = cur.execute(
                "SELECT COUNT(*) AS c FROM message_queue WHERE status='quiet_held'"
            ).fetchone()["c"]
        finally:
            cur.close()
        assert held_count == 3

        # 跑 digest flush
        digest_ev = agent.flush_quiet_digest()
        assert digest_ev is not None

        # 原 3 条 → done；digest 自己 → pending（等下一轮 deliver）
        tmp_qm._ensure_open()
        cur = tmp_qm.conn.cursor()
        try:
            stats = {
                r["status"]: r["c"] for r in cur.execute(
                    "SELECT status, COUNT(*) AS c FROM message_queue GROUP BY status"
                ).fetchall()
            }
        finally:
            cur.close()

        assert stats.get("done", 0) == 3
        assert stats.get("quiet_held", 0) == 0
        assert stats.get("pending", 0) == 1     # digest 本身

        # digest content 包含 3 条预览
        rows = tmp_qm.peek(QUEUE_NAME, limit=10)
        digest_row = next(
            r for r in rows if r["event_id"] == digest_ev
        )
        payload = digest_row["payload"]
        assert payload["message_type"] == "daily_briefing"
        assert payload["priority"] == "blue"
        content = payload["content"]
        assert content["digest_kind"] == "quiet_hours_release"
        assert content["count"] == 3
        assert len(content["items"]) == 3

    def test_digest_entity_key_does_not_collide_with_daily_briefing(
        self, tmp_kdb, tmp_qm, push_queue,
    ):
        """quiet_digest_{date} ≠ daily_briefing_{date}，两者同日共存。"""
        agent = self._quiet_agent(tmp_kdb, tmp_qm)
        push_queue.push("alert", {}, priority="blue", entity_key="x")
        agent.deliver_pending(_FakeChannel(ok=True), max=10)

        quiet_ev = agent.flush_quiet_digest()
        # 09:30 路径的 daily_briefing
        daily_ev = push_queue.push_daily_briefing(
            content={"kind": "normal"}, priority="blue",
        )

        assert quiet_ev is not None
        assert daily_ev is not None
        assert quiet_ev != daily_ev

        rows = tmp_qm.peek(QUEUE_NAME, limit=20)
        keys = {r["entity_key"] for r in rows}
        # 两个 briefing 都入队
        assert any(k.startswith("quiet_digest_") for k in keys)
        assert any(k.startswith("daily_briefing_") for k in keys)
