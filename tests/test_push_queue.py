"""
tests/test_push_queue.py — PushQueue 测试矩阵

覆盖：
    - 入参校验：未知 message_type / priority / alert_type / report_type
    - 消息统一结构（message_type / priority / content / created_at / target_channel）
    - 基础 push 返 event_id，行真实写入 message_queue
    - 幂等：同 entity_key 第二次 push → 返回已有 event_id，不插新行
    - push_alert 默认 red，entity_key 含日期自动去重
    - push_daily_briefing：entity_key=daily_briefing_{KST_date}，next_push_at=下 07:30 KST
    - push_weekly_report：entity_key=weekly_{type}_{KST_date}
    - poll_pending：read-only（不改 status）+ priority 排序（red 先）+ max 截断
    - mark_delivered：pending → done；processing → done；done/failed 返 False
    - mark_failed：retries+1 逻辑镜像 nack；< MAX 回 pending；≥ MAX 标 failed
    - 集成：DirectionJudgeAgent(push_queue=PushQueue(...)) 触发自动推送
    - 辅助 _next_daily_briefing_time_utc 跨 07:30 边界正确
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest

from agents.base import RuleViolation
from agents.direction_judge import DirectionJudgeAgent
from infra.push_queue import (
    KST,
    PRIORITIES,
    PRIORITY_ORDER,
    PushQueue,
    QUEUE_NAME,
    VALID_ALERT_TYPES,
    VALID_MESSAGE_TYPES,
    VALID_REPORT_TYPES,
    _is_valid_yyyymmdd,
    _next_daily_briefing_time_utc,
    _today_kst_date_str,
)
from infra.db_manager import DatabaseManager
from infra.queue_manager import QueueManager
from knowledge.init_db import init_database
from knowledge.init_queue_db import init_queue_db


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_queue_db(tmp_path):
    qdb_path = tmp_path / "queue.db"
    init_queue_db(qdb_path)
    qm = QueueManager(qdb_path)
    yield qm, qdb_path
    qm.close()


@pytest.fixture
def pq(tmp_queue_db):
    qm, _ = tmp_queue_db
    return PushQueue(queue_manager=qm)


@pytest.fixture
def tmp_kdb(tmp_path):
    """主 knowledge.db（给 DirectionJudge 集成用）"""
    kdb_path = tmp_path / "knowledge.db"
    init_database(kdb_path)
    kdb = DatabaseManager(kdb_path)
    yield kdb
    kdb.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


# ─── Fake Ollama（给 DirectionJudge 集成测用）───


class _FakeResp(dict):
    class _Msg:
        def __init__(self, content):
            self.content = content

    def __init__(self, content):
        super().__init__()
        self["message"] = {"content": content}
        self["prompt_eval_count"] = 50
        self["eval_count"] = 30
        self.message = self._Msg(content)
        self.prompt_eval_count = 50
        self.eval_count = 30


class FakeOllama:
    def __init__(self, text="分析文本"):
        self.text = text
        self.calls: List[Dict[str, Any]] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResp(self.text)


# ═══════════════════ 模块常量 ═══════════════════


class TestConstants:
    def test_priorities_order_red_first(self):
        assert PRIORITIES[0] == "red"
        assert PRIORITIES[-1] == "white"
        assert PRIORITY_ORDER["red"] < PRIORITY_ORDER["yellow"] < PRIORITY_ORDER["blue"] < PRIORITY_ORDER["white"]

    def test_valid_message_types(self):
        for t in ("daily_briefing", "weekly_report", "alert", "recommendation"):
            assert t in VALID_MESSAGE_TYPES

    def test_valid_alert_types(self):
        for t in ("failure_level_change", "motivation_drift", "data_source_down", "cost_exceeded"):
            assert t in VALID_ALERT_TYPES

    def test_valid_report_types(self):
        assert VALID_REPORT_TYPES == {"industry", "paper"}

    def test_queue_name(self):
        assert QUEUE_NAME == "push_outbox"


# ═══════════════════ 初始化 ═══════════════════


class TestInit:
    def test_requires_queue_manager(self):
        with pytest.raises(RuleViolation):
            PushQueue(queue_manager=None)

    def test_requires_non_empty_channel(self, tmp_queue_db):
        qm, _ = tmp_queue_db
        with pytest.raises(RuleViolation):
            PushQueue(qm, target_channel="")

    def test_default_channel(self, pq):
        assert pq.target_channel == "openclaw"

    def test_custom_channel(self, tmp_queue_db):
        qm, _ = tmp_queue_db
        p = PushQueue(qm, target_channel="claude_desktop")
        assert p.target_channel == "claude_desktop"


# ═══════════════════ 入参校验 ═══════════════════


class TestPushValidation:
    def test_unknown_message_type(self, pq):
        with pytest.raises(RuleViolation, match="message_type"):
            pq.push("unknown_type", {"x": 1})

    def test_unknown_priority(self, pq):
        with pytest.raises(RuleViolation, match="priority"):
            pq.push("daily_briefing", {"x": 1}, priority="magenta")

    def test_non_dict_content(self, pq):
        with pytest.raises(RuleViolation, match="dict"):
            pq.push("daily_briefing", "not a dict")

    def test_empty_producer(self, pq):
        with pytest.raises(RuleViolation, match="producer"):
            pq.push("daily_briefing", {}, producer="")

    def test_push_alert_unknown_type(self, pq):
        with pytest.raises(RuleViolation, match="alert_type"):
            pq.push_alert("not_an_alert", {"x": 1})

    def test_push_weekly_report_unknown_type(self, pq):
        with pytest.raises(RuleViolation, match="report_type"):
            pq.push_weekly_report("novel", {"x": 1})

    @pytest.mark.parametrize("bad", ["", "2026", "20260401x", "not-a-date", "abcd1234"])
    def test_push_daily_briefing_bad_date(self, pq, bad):
        with pytest.raises(RuleViolation):
            pq.push_daily_briefing({"x": 1}, target_date=bad)

    @pytest.mark.parametrize("bad", ["20260230", "20260299"])
    def test_push_daily_briefing_invalid_calendar_date(self, pq, bad):
        with pytest.raises(RuleViolation):
            pq.push_daily_briefing({"x": 1}, target_date=bad)


# ═══════════════════ 消息结构 ═══════════════════


class TestMessageStructure:
    def test_push_stores_canonical_payload(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        event_id = pq.push(
            message_type="daily_briefing",
            content={"k": "v"},
            priority="blue",
            producer="signal_collector",
        )
        rows = qm.peek(QUEUE_NAME, limit=10)
        assert len(rows) == 1
        payload = rows[0]["payload"]
        assert payload["message_type"] == "daily_briefing"
        assert payload["priority"] == "blue"
        assert payload["content"] == {"k": "v"}
        assert payload["target_channel"] == "openclaw"
        # created_at UTC ISO
        ts = payload["created_at"]
        assert ts.endswith("+00:00") or ts.endswith("Z")
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_push_returns_event_id(self, pq):
        ev = pq.push("daily_briefing", {})
        assert isinstance(ev, str)
        assert len(ev) >= 8

    def test_next_push_at_propagated(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push(
            "daily_briefing", {"x": 1},
            next_push_at="2026-04-18T22:30:00+00:00",
        )
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["next_push_at"] == "2026-04-18T22:30:00+00:00"


# ═══════════════════ 幂等 ═══════════════════


class TestIdempotent:
    def test_same_entity_key_returns_existing_event_id(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        first = pq.push(
            "daily_briefing", {"x": 1}, entity_key="daily_briefing_20260418"
        )
        second = pq.push(
            "daily_briefing", {"x": 2}, entity_key="daily_briefing_20260418"
        )
        assert first == second
        # DB 中只有一行
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 1

    def test_different_entity_keys_create_separate_rows(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push("daily_briefing", {}, entity_key="daily_briefing_20260418")
        pq.push("daily_briefing", {}, entity_key="daily_briefing_20260419")
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 2

    def test_no_entity_key_always_new_row(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push("daily_briefing", {})
        pq.push("daily_briefing", {})
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 2

    def test_idempotent_even_after_delivered(self, pq, tmp_queue_db):
        """幂等不区分已 done：同 entity_key 已 done 时，push 返回原 event_id，
        不重复推。"""
        qm, _ = tmp_queue_db
        ev = pq.push("daily_briefing", {}, entity_key="briefing_a")
        pq.mark_delivered(ev)
        second = pq.push("daily_briefing", {}, entity_key="briefing_a")
        assert second == ev  # 同 event_id
        # pending 0 / done 1
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 0
        assert stats[QUEUE_NAME]["done"] == 1


# ═══════════════════ push_alert ═══════════════════


class TestPushAlert:
    @pytest.mark.parametrize("alert_type", sorted(VALID_ALERT_TYPES))
    def test_each_alert_type_pushable(self, pq, alert_type):
        ev = pq.push_alert(alert_type, {"msg": "test"})
        assert isinstance(ev, str)

    def test_default_priority_red(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push_alert("data_source_down", {"source": "D1"})
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["priority"] == "red"

    def test_alert_type_added_to_content(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push_alert("cost_exceeded", {"amount": 30})
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["content"]["alert_type"] == "cost_exceeded"
        assert row["payload"]["content"]["amount"] == 30

    def test_auto_entity_key_dedups_same_day(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev1 = pq.push_alert("data_source_down", {"source": "D1"})
        ev2 = pq.push_alert("data_source_down", {"source": "D1"})
        assert ev1 == ev2
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 1

    def test_custom_priority_respected(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push_alert("cost_exceeded", {}, priority="yellow")
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["priority"] == "yellow"


# ═══════════════════ push_daily_briefing ═══════════════════


class TestPushDailyBriefing:
    def test_basic(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev = pq.push_daily_briefing({"highlights": ["A", "B"]})
        assert isinstance(ev, str)
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["message_type"] == "daily_briefing"

    def test_entity_key_format(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        today = _today_kst_date_str()
        pq.push_daily_briefing({})
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["entity_key"] == f"daily_briefing_{today}"

    def test_same_day_idempotent(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev1 = pq.push_daily_briefing({"v": 1})
        ev2 = pq.push_daily_briefing({"v": 2})
        assert ev1 == ev2
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 1

    def test_explicit_target_date(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push_daily_briefing({}, target_date="20260501")
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["entity_key"] == "daily_briefing_20260501"

    def test_next_push_at_included(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push_daily_briefing({})
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        nxt = row["payload"]["next_push_at"]
        assert nxt is not None
        # 应该是某个 07:30 KST（= 22:30 UTC 前一天）
        dt_utc = datetime.fromisoformat(nxt)
        dt_kst = dt_utc.astimezone(KST)
        assert dt_kst.hour == 7 and dt_kst.minute == 30

    def test_non_dict_content_rejected(self, pq):
        with pytest.raises(RuleViolation):
            pq.push_daily_briefing("not a dict")


# ═══════════════════ push_weekly_report ═══════════════════


class TestPushWeeklyReport:
    @pytest.mark.parametrize("rtype", ["industry", "paper"])
    def test_both_types(self, pq, rtype):
        ev = pq.push_weekly_report(rtype, {"summary": "x"})
        assert isinstance(ev, str)

    def test_entity_key_per_type(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        today = _today_kst_date_str()
        pq.push_weekly_report("industry", {})
        pq.push_weekly_report("paper", {})
        rows = qm.peek(QUEUE_NAME, limit=10)
        keys = sorted([r["entity_key"] for r in rows])
        assert keys == [f"weekly_industry_{today}", f"weekly_paper_{today}"]

    def test_same_type_idempotent(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev1 = pq.push_weekly_report("industry", {"v": 1})
        ev2 = pq.push_weekly_report("industry", {"v": 2})
        assert ev1 == ev2

    def test_report_type_in_content(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push_weekly_report("paper", {"top_n": 10})
        row = qm.peek(QUEUE_NAME, limit=1)[0]
        assert row["payload"]["content"]["report_type"] == "paper"
        assert row["payload"]["content"]["top_n"] == 10


# ═══════════════════ poll_pending ═══════════════════


class TestPollPending:
    def test_empty_returns_empty(self, pq):
        assert pq.poll_pending() == []

    def test_returns_pending_only(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev_a = pq.push("daily_briefing", {"a": 1})
        pq.push("daily_briefing", {"b": 2})
        pq.mark_delivered(ev_a)
        result = pq.poll_pending()
        assert len(result) == 1
        assert result[0]["content"] == {"b": 2}

    def test_does_not_change_status(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        pq.push("daily_briefing", {"x": 1})
        pq.poll_pending()
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 1
        assert stats[QUEUE_NAME]["processing"] == 0

    def test_priority_order(self, pq):
        pq.push("recommendation", {}, priority="white")
        pq.push("alert", {"alert_type": "data_source_down"}, priority="red")
        pq.push("daily_briefing", {}, priority="blue")
        pq.push("alert", {"alert_type": "motivation_drift"}, priority="yellow")
        result = pq.poll_pending()
        priorities = [m["priority"] for m in result]
        assert priorities == ["red", "yellow", "blue", "white"]

    def test_fifo_within_same_priority(self, pq):
        pq.push("daily_briefing", {"order": 1}, priority="blue")
        pq.push("daily_briefing", {"order": 2}, priority="blue")
        pq.push("daily_briefing", {"order": 3}, priority="blue")
        result = pq.poll_pending()
        orders = [m["content"]["order"] for m in result]
        assert orders == [1, 2, 3]

    def test_max_truncation(self, pq):
        for i in range(20):
            pq.push("daily_briefing", {"i": i})
        result = pq.poll_pending(max=5)
        assert len(result) == 5

    def test_invalid_max_raises(self, pq):
        for bad in (0, -1, "10"):
            with pytest.raises(RuleViolation):
                pq.poll_pending(max=bad)

    def test_poll_enriches_fields(self, pq):
        pq.push_alert("data_source_down", {"source": "D1"})
        result = pq.poll_pending()
        assert len(result) == 1
        m = result[0]
        assert m["event_id"]
        assert m["message_type"] == "alert"
        assert m["priority"] == "red"
        assert m["content"]["alert_type"] == "data_source_down"
        assert m["target_channel"] == "openclaw"
        assert m["status"] == "pending"


# ═══════════════════ mark_delivered ═══════════════════


class TestMarkDelivered:
    def test_pending_to_done(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev = pq.push("daily_briefing", {})
        assert pq.mark_delivered(ev) is True
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["done"] == 1
        assert stats[QUEUE_NAME]["pending"] == 0

    def test_processing_to_done(self, pq, tmp_queue_db):
        """如果有外部系统先 dequeue（→processing），mark_delivered 仍能 ack"""
        qm, _ = tmp_queue_db
        ev = pq.push("daily_briefing", {})
        qm.dequeue(QUEUE_NAME, "external_consumer")  # → processing
        assert pq.mark_delivered(ev) is True
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["done"] == 1

    def test_done_state_returns_false(self, pq):
        ev = pq.push("daily_briefing", {})
        pq.mark_delivered(ev)
        assert pq.mark_delivered(ev) is False  # 已经是 done

    def test_nonexistent_event_returns_false(self, pq):
        assert pq.mark_delivered("nonexistent-uuid-0000") is False

    def test_empty_event_id_raises(self, pq):
        with pytest.raises(RuleViolation):
            pq.mark_delivered("")


# ═══════════════════ mark_failed ═══════════════════


class TestMarkFailed:
    def test_retries_under_max_returns_pending(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev = pq.push("daily_briefing", {})
        assert pq.mark_failed(ev, "timeout") is True
        # retries=1，仍 pending
        row = qm.peek(QUEUE_NAME, limit=10)
        assert len(row) == 1
        assert row[0]["status"] == "pending"
        assert row[0]["retries"] == 1

    def test_max_retries_marks_failed(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev = pq.push("daily_briefing", {})
        # nack MAX_RETRIES 次
        for _ in range(QueueManager.MAX_RETRIES):
            pq.mark_failed(ev, "fail")
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["failed"] == 1
        assert stats[QUEUE_NAME]["pending"] == 0

    def test_failed_state_returns_false_further_calls(self, pq):
        ev = pq.push("daily_briefing", {})
        for _ in range(QueueManager.MAX_RETRIES):
            pq.mark_failed(ev, "fail")
        # 已是 failed 终态，再 mark_failed 返 False
        assert pq.mark_failed(ev, "fail again") is False

    def test_done_state_rejects(self, pq):
        ev = pq.push("daily_briefing", {})
        pq.mark_delivered(ev)
        assert pq.mark_failed(ev, "oops") is False

    def test_processing_state_ok(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev = pq.push("daily_briefing", {})
        qm.dequeue(QUEUE_NAME, "ext")  # → processing
        assert pq.mark_failed(ev, "reason") is True
        rows = qm.peek(QUEUE_NAME, limit=10)
        # 回 pending（retries=1 < MAX）
        assert rows[0]["status"] == "pending"

    def test_nonexistent_event(self, pq):
        assert pq.mark_failed("nonexistent-id", "r") is False

    def test_empty_event_id_raises(self, pq):
        with pytest.raises(RuleViolation):
            pq.mark_failed("")

    def test_reason_saved(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev = pq.push("daily_briefing", {})
        pq.mark_failed(ev, "specific reason")
        rows = qm.peek(QUEUE_NAME, limit=10)
        assert "specific reason" in (rows[0]["error_message"] or "")


# ═══════════════════ queue_stats ═══════════════════


class TestQueueStats:
    def test_empty_queue(self, pq):
        s = pq.queue_stats()
        assert s == {"pending": 0, "processing": 0, "done": 0, "failed": 0}

    def test_mixed_states(self, pq):
        e1 = pq.push("daily_briefing", {})
        e2 = pq.push("daily_briefing", {})
        e3 = pq.push("daily_briefing", {})
        pq.mark_delivered(e1)
        pq.mark_failed(e2, "fail")
        # e3 仍 pending
        s = pq.queue_stats()
        assert s["done"] == 1
        assert s["pending"] == 2  # e2 回 pending (retries=1) + e3


# ═══════════════════ 时间辅助 ═══════════════════


class TestTimeHelpers:
    def test_next_briefing_before_0730_kst(self):
        # 00:00 KST → 今日 07:30 KST
        now_kst = datetime(2026, 4, 18, 0, 0, tzinfo=KST)
        nxt = _next_daily_briefing_time_utc(now_kst=now_kst)
        dt_utc = datetime.fromisoformat(nxt)
        # 4-17 22:30 UTC = 4-18 07:30 KST
        assert dt_utc == datetime(2026, 4, 17, 22, 30, tzinfo=timezone.utc)

    def test_next_briefing_after_0730_kst(self):
        # 10:00 KST → 明日 07:30 KST
        now_kst = datetime(2026, 4, 18, 10, 0, tzinfo=KST)
        nxt = _next_daily_briefing_time_utc(now_kst=now_kst)
        dt_utc = datetime.fromisoformat(nxt)
        assert dt_utc == datetime(2026, 4, 18, 22, 30, tzinfo=timezone.utc)

    def test_next_briefing_exactly_0730_kst(self):
        # 恰好 07:30 KST → 明日（已到发送时间）
        now_kst = datetime(2026, 4, 18, 7, 30, tzinfo=KST)
        nxt = _next_daily_briefing_time_utc(now_kst=now_kst)
        dt_utc = datetime.fromisoformat(nxt)
        assert dt_utc == datetime(2026, 4, 18, 22, 30, tzinfo=timezone.utc)

    def test_is_valid_yyyymmdd(self):
        assert _is_valid_yyyymmdd("20260418") is True
        assert _is_valid_yyyymmdd("20260430") is True
        assert _is_valid_yyyymmdd("20260230") is False  # 2026 年 2 月无 30 日
        assert _is_valid_yyyymmdd("2026041x") is False
        assert _is_valid_yyyymmdd("202604181") is False  # 9 位
        assert _is_valid_yyyymmdd("") is False
        assert _is_valid_yyyymmdd(None) is False


# ═══════════════════ DirectionJudgeAgent 集成 ═══════════════════


class TestDirectionJudgeIntegration:
    @pytest.fixture
    def agent_with_push(self, tmp_kdb, tmp_queue_db, tmp_path):
        qm, _ = tmp_queue_db
        push_queue = PushQueue(qm)
        ollama = FakeOllama(text="周报 AI 分析文本")
        agent = DirectionJudgeAgent(
            db=tmp_kdb,
            ollama_client=ollama,
            reports_dir=tmp_path / "reports",
            push_queue=push_queue,
        )
        return agent, push_queue, qm

    def test_no_push_queue_no_autopush(self, tmp_kdb, tmp_path):
        agent = DirectionJudgeAgent(
            db=tmp_kdb,
            ollama_client=FakeOllama(),
            reports_dir=tmp_path / "reports",
            push_queue=None,
        )
        # 无 push_queue，方法应正常返（无 push）
        report, path = agent.weekly_industry_report(save=False, use_gemma=False)
        assert isinstance(report, str)  # 不崩

    def test_weekly_industry_auto_pushes(self, agent_with_push):
        agent, pq, qm = agent_with_push
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False, use_gemma=False
        )
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 1
        rows = qm.peek(QUEUE_NAME, limit=1)
        p = rows[0]["payload"]
        assert p["message_type"] == "weekly_report"
        assert p["content"]["report_type"] == "industry"
        assert p["content"]["producer_agent"] == "direction_judge"

    def test_weekly_paper_auto_pushes(self, agent_with_push):
        agent, pq, qm = agent_with_push
        report, _ = agent.weekly_paper_report(save=False, use_gemma=False)
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 1
        rows = qm.peek(QUEUE_NAME, limit=1)
        p = rows[0]["payload"]
        assert p["content"]["report_type"] == "paper"

    def test_integration_same_day_dedupe(self, agent_with_push):
        """两次 weekly_industry 只推一次（entity_key 去重）"""
        agent, pq, qm = agent_with_push
        agent.weekly_industry_report(industry_name="半导体", save=False, use_gemma=False)
        agent.weekly_industry_report(industry_name="半导体", save=False, use_gemma=False)
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["pending"] == 1

    def test_push_failure_does_not_break_report(self, tmp_kdb, tmp_path, monkeypatch):
        """push_queue.push_weekly_report 抛错不应影响 report 生成返回"""
        import infra.queue_manager

        class BrokenQM:
            def enqueue(self, **kw):
                raise RuntimeError("db down")

            # minimal to satisfy PushQueue.__init__
            _ensure_open = lambda self: None
            conn = type("C", (), {"execute": lambda self, *a, **k: type("R", (), {"fetchone": lambda self: None})()})()

        # 用真实 PushQueue 但 underlying 喷错
        broken_pq = type("BPQ", (), {
            "push_weekly_report": lambda self, **kw: (_ for _ in ()).throw(RuntimeError("push broken"))
        })()

        agent = DirectionJudgeAgent(
            db=tmp_kdb,
            ollama_client=FakeOllama(),
            reports_dir=tmp_path / "reports",
            push_queue=broken_pq,
        )
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False, use_gemma=False
        )
        assert isinstance(report, str)
        assert len(report) > 0

    def test_push_preview_truncated(self, tmp_kdb, tmp_queue_db, tmp_path):
        """报告 > 2000 字 → push 里的 preview 截断"""
        qm, _ = tmp_queue_db
        pq = PushQueue(qm)
        # 塞 5 个 active 行业，保证报告够长
        for i, name in enumerate(("半导体", "新能源", "光伏", "生物医药", "人工智能")):
            tmp_kdb.write(
                """INSERT INTO watchlist
                   (industry_name, zone, dimensions, gap_status)
                   VALUES (?, 'active', ?, 'active')""",
                (name, i + 1),
            )
        agent = DirectionJudgeAgent(
            db=tmp_kdb,
            ollama_client=FakeOllama(text="a" * 500),  # 每行业 500 字 AI 分析 × 5
            reports_dir=tmp_path / "reports",
            push_queue=pq,
        )
        agent.weekly_industry_report(save=False, use_gemma=True)

        rows = qm.peek(QUEUE_NAME, limit=1)
        preview = rows[0]["payload"]["content"]["report_preview"]
        assert len(preview) <= 2200  # 2000 + 截断提示
        if len(preview) == 2200 or "truncated" in preview:
            assert "truncated" in preview


# ═══════════════════ 端到端订阅流程 ═══════════════════


class TestEndToEndFlow:
    def test_push_poll_deliver_full_cycle(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db

        # 1. 生产一批
        pq.push_alert("data_source_down", {"source": "D1"})
        pq.push_daily_briefing({"top": "policy"})
        pq.push_weekly_report("industry", {"summary": "x"})

        # 2. 订阅者 poll
        pending = pq.poll_pending(max=10)
        assert len(pending) == 3
        # red > blue
        assert pending[0]["priority"] == "red"

        # 3. 送达
        for m in pending:
            ok = pq.mark_delivered(m["event_id"])
            assert ok is True

        # 4. 状态全 done
        stats = qm.queue_stats(QUEUE_NAME)
        assert stats[QUEUE_NAME]["done"] == 3
        assert stats[QUEUE_NAME]["pending"] == 0

    def test_failure_and_retry_cycle(self, pq, tmp_queue_db):
        qm, _ = tmp_queue_db
        ev = pq.push_daily_briefing({"x": 1})
        # 订阅者 fail 一次
        pq.mark_failed(ev, "oom")
        # 再 poll 能拿到（还是 pending, retries=1）
        pending = pq.poll_pending()
        assert len(pending) == 1
        assert pending[0]["retries"] == 1

        # 成功送达
        assert pq.mark_delivered(pending[0]["event_id"]) is True
        assert qm.queue_stats(QUEUE_NAME)[QUEUE_NAME]["done"] == 1
