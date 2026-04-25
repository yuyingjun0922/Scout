"""
tests/test_push_policy.py — v1.61 Quiet Hours 策略单测

覆盖：
  TestExtractAlertLevel       payload→P-level 提取（含 priority 回退、默认 P4）
  TestQuietHoursPolicy        from_config / disabled / is_in_quiet_window
  TestShouldPushNow           核心决策矩阵（always_push / 窗口内外 / disabled）
  TestGetQuietHoursDigest     message_queue 里 status='quiet_held' SELECT
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone

import pytest
from zoneinfo import ZoneInfo

from config.loader import QuietHoursConfig
from infra.queue_manager import QueueManager
from knowledge.init_queue_db import init_queue_db
from utils.push_policy import (
    ALERT_LEVELS,
    DEFAULT_ALERT_LEVEL,
    KST,
    PRIORITY_TO_ALERT_LEVEL,
    PUSH_OUTBOX_QUEUE,
    QUIET_HELD_STATUS,
    QuietHoursPolicy,
    extract_alert_level,
    get_quiet_hours_digest,
    should_push_now,
)


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_qm(tmp_path):
    qdb_path = tmp_path / "queue.db"
    init_queue_db(qdb_path)
    qm = QueueManager(qdb_path)
    yield qm
    qm.close()


@pytest.fixture
def default_quiet_cfg() -> QuietHoursConfig:
    """00:00-07:30 KST、P0/P1 照推、07:30 digest — 与 config.yaml 默认一致。"""
    return QuietHoursConfig(
        enabled=True,
        start="00:00",
        end="07:30",
        timezone="KST",
        always_push_levels=["P0", "P1"],
        digest_at="07:30",
    )


def _kst(h: int, m: int = 0) -> datetime:
    """构造 KST 本地时间（给 should_push_now 喂带 tzinfo 的时刻）。"""
    today = datetime.now(tz=KST).date()
    return datetime.combine(today, time(h, m), tzinfo=KST)


# ═══════════════════ extract_alert_level ═══════════════════


class TestExtractAlertLevel:
    def test_none_payload_defaults_p4(self):
        assert extract_alert_level(None) == DEFAULT_ALERT_LEVEL

    def test_malformed_json_string_defaults_p4(self):
        assert extract_alert_level("{not json") == DEFAULT_ALERT_LEVEL

    def test_non_dict_payload_defaults_p4(self):
        assert extract_alert_level([1, 2, 3]) == DEFAULT_ALERT_LEVEL
        assert extract_alert_level(42) == DEFAULT_ALERT_LEVEL

    def test_explicit_top_level_alert_level(self):
        assert extract_alert_level({"alert_level": "P0"}) == "P0"
        assert extract_alert_level({"alert_level": "P3"}) == "P3"

    def test_unknown_top_level_falls_through_to_priority(self):
        # alert_level 非法 + priority 合法 → 走 priority 回退
        p = {"alert_level": "PX", "priority": "red"}
        assert extract_alert_level(p) == "P0"

    def test_nested_content_alert_level(self):
        p = {"content": {"alert_level": "P2"}}
        assert extract_alert_level(p) == "P2"

    def test_priority_fallback_red_to_p0(self):
        assert extract_alert_level({"priority": "red"}) == "P0"

    def test_priority_fallback_yellow_to_p1(self):
        assert extract_alert_level({"priority": "yellow"}) == "P1"

    def test_priority_fallback_blue_to_p2(self):
        assert extract_alert_level({"priority": "blue"}) == "P2"

    def test_priority_fallback_white_to_p3(self):
        assert extract_alert_level({"priority": "white"}) == "P3"

    def test_unknown_priority_defaults_p4(self):
        assert extract_alert_level({"priority": "purple"}) == DEFAULT_ALERT_LEVEL

    def test_no_alert_no_priority_defaults_p4(self):
        assert extract_alert_level({"content": {}}) == DEFAULT_ALERT_LEVEL

    def test_json_string_input_ok(self):
        s = json.dumps({"priority": "red"})
        assert extract_alert_level(s) == "P0"

    def test_priority_mapping_covers_4_levels(self):
        assert set(PRIORITY_TO_ALERT_LEVEL.values()) == {"P0", "P1", "P2", "P3"}


# ═══════════════════ QuietHoursPolicy ═══════════════════


class TestQuietHoursPolicy:
    def test_disabled_factory(self):
        p = QuietHoursPolicy.disabled()
        assert p.enabled is False
        # 所有级别都视为 always_push（静默期检查后还会因 enabled=False 直接 True）
        assert p.always_push_levels >= frozenset({"P0", "P4"})

    def test_from_config_pydantic(self, default_quiet_cfg):
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert p.enabled is True
        assert p.start == time(0, 0)
        assert p.end == time(7, 30)
        assert p.always_push_levels == frozenset({"P0", "P1"})
        assert p.digest_at == time(7, 30)
        assert p.tz == KST

    def test_from_config_dict(self):
        p = QuietHoursPolicy.from_config({
            "enabled": True, "start": "22:00", "end": "06:00",
            "timezone": "KST", "always_push_levels": ["P0"],
            "digest_at": "06:00",
        })
        assert p.start == time(22, 0)
        assert p.end == time(6, 0)
        assert p.always_push_levels == frozenset({"P0"})

    def test_from_config_none_returns_disabled(self):
        p = QuietHoursPolicy.from_config(None)
        assert p.enabled is False

    def test_from_config_enabled_false_returns_disabled(self):
        p = QuietHoursPolicy.from_config({"enabled": False})
        assert p.enabled is False

    def test_from_config_rejects_unknown_level(self):
        with pytest.raises(ValueError, match="unknown"):
            QuietHoursPolicy.from_config({
                "enabled": True, "always_push_levels": ["P9"],
            })

    def test_is_in_quiet_window_standard(self, default_quiet_cfg):
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert p.is_in_quiet_window(_kst(3, 0)) is True      # 03:00 静默
        assert p.is_in_quiet_window(_kst(7, 0)) is True      # 07:00 静默（< 07:30）
        assert p.is_in_quiet_window(_kst(7, 30)) is False    # 07:30 = end（半开）
        assert p.is_in_quiet_window(_kst(8, 0)) is False     # 08:00 非静默
        assert p.is_in_quiet_window(_kst(22, 0)) is False    # 22:00 非静默

    def test_is_in_quiet_window_cross_midnight(self):
        """start=23:00 end=07:00 → 23-23:59 + 00-06:59 都静默"""
        p = QuietHoursPolicy.from_config({
            "enabled": True, "start": "23:00", "end": "07:00",
        })
        assert p.is_in_quiet_window(_kst(23, 30)) is True
        assert p.is_in_quiet_window(_kst(2, 0)) is True
        assert p.is_in_quiet_window(_kst(6, 59)) is True
        assert p.is_in_quiet_window(_kst(7, 0)) is False
        assert p.is_in_quiet_window(_kst(12, 0)) is False
        assert p.is_in_quiet_window(_kst(22, 59)) is False

    def test_disabled_never_in_quiet_window(self):
        p = QuietHoursPolicy.disabled()
        for h in (0, 6, 12, 23):
            assert p.is_in_quiet_window(_kst(h)) is False


# ═══════════════════ should_push_now ═══════════════════


class TestShouldPushNow:
    def test_p0_in_quiet_window_always_pushes(self, default_quiet_cfg):
        """静默期内 P0 should_push_now=True（always_push_levels）"""
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert should_push_now(p, "P0", _kst(3, 0)) is True

    def test_p1_in_quiet_window_always_pushes(self, default_quiet_cfg):
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert should_push_now(p, "P1", _kst(3, 0)) is True

    def test_p4_in_quiet_window_should_not_push(self, default_quiet_cfg):
        """静默期内 P4 should_push_now=False（攒到 digest）"""
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert should_push_now(p, "P4", _kst(3, 0)) is False

    def test_p2_p3_in_quiet_window_should_not_push(self, default_quiet_cfg):
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert should_push_now(p, "P2", _kst(3, 0)) is False
        assert should_push_now(p, "P3", _kst(3, 0)) is False

    def test_any_level_outside_quiet_window_pushes(self, default_quiet_cfg):
        """非静默期任何级别都 True"""
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        for level in ALERT_LEVELS:
            assert should_push_now(p, level, _kst(10, 0)) is True

    def test_disabled_policy_always_pushes(self):
        p = QuietHoursPolicy.disabled()
        for level in ALERT_LEVELS:
            for h in (0, 6, 12, 23):
                assert should_push_now(p, level, _kst(h, 0)) is True

    def test_unknown_level_treated_as_p4(self, default_quiet_cfg):
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert should_push_now(p, "P9", _kst(3, 0)) is False  # 归一 P4 → 静默

    def test_naive_datetime_rejected(self, default_quiet_cfg):
        """non-always-push 级别才会走到 tzinfo 检查（always_push 先行短路）。"""
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        with pytest.raises(ValueError, match="tz-aware"):
            should_push_now(p, "P3", datetime(2026, 4, 25, 3, 0))

    def test_utc_datetime_converted_to_policy_tz(self, default_quiet_cfg):
        """喂 UTC 18:00 → KST 03:00 → 静默"""
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        utc_3am_kst = datetime(2026, 4, 25, 18, 0, tzinfo=timezone.utc)
        assert should_push_now(p, "P3", utc_3am_kst) is False
        # 同一时刻 P0 照推
        assert should_push_now(p, "P0", utc_3am_kst) is True

    def test_end_boundary_exclusive(self, default_quiet_cfg):
        """07:30:00 正好 = end → 不在静默窗口内 → push"""
        p = QuietHoursPolicy.from_config(default_quiet_cfg)
        assert should_push_now(p, "P3", _kst(7, 30)) is True


# ═══════════════════ get_quiet_hours_digest ═══════════════════


class TestGetQuietHoursDigest:
    def _enqueue(self, qm: QueueManager, payload: dict, entity_key: str) -> str:
        return qm.enqueue(
            queue_name=PUSH_OUTBOX_QUEUE,
            payload=payload,
            producer="test_producer",
            entity_key=entity_key,
        )

    def _set_status(self, qm: QueueManager, event_id: str, status: str) -> None:
        qm._ensure_open()
        cur = qm.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "UPDATE message_queue SET status=? WHERE event_id=?",
                (status, event_id),
            )
            qm.conn.commit()
        finally:
            cur.close()

    def test_empty_queue_returns_empty(self, tmp_qm):
        assert get_quiet_hours_digest(tmp_qm) == []

    def test_returns_only_quiet_held(self, tmp_qm):
        ev1 = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "white", "content": {},
        }, "k1")
        ev2 = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "blue", "content": {},
        }, "k2")
        ev3 = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "red", "content": {},
        }, "k3")
        # ev1 held, ev2 held, ev3 pending (urgent 不攒)
        self._set_status(tmp_qm, ev1, QUIET_HELD_STATUS)
        self._set_status(tmp_qm, ev2, QUIET_HELD_STATUS)

        digest = get_quiet_hours_digest(tmp_qm)
        ids = {d["event_id"] for d in digest}
        assert ids == {ev1, ev2}

    def test_result_ordered_by_insertion(self, tmp_qm):
        evs = []
        for i in range(3):
            ev = self._enqueue(tmp_qm, {
                "message_type": "alert", "priority": "blue",
                "content": {"n": i},
            }, f"k{i}")
            self._set_status(tmp_qm, ev, QUIET_HELD_STATUS)
            evs.append(ev)

        digest = get_quiet_hours_digest(tmp_qm)
        assert [d["event_id"] for d in digest] == evs

    def test_digest_enriches_with_alert_level(self, tmp_qm):
        ev = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "blue",
            "content": {"msg": "x"},
        }, "k1")
        self._set_status(tmp_qm, ev, QUIET_HELD_STATUS)

        digest = get_quiet_hours_digest(tmp_qm)
        assert len(digest) == 1
        assert digest[0]["alert_level"] == "P2"   # blue → P2
        assert digest[0]["content"] == {"msg": "x"}

    def test_done_failed_pending_excluded(self, tmp_qm):
        """只返 quiet_held；done/failed/pending 全排除。"""
        ev_held = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "blue", "content": {},
        }, "held")
        ev_done = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "blue", "content": {},
        }, "done")
        ev_pending = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "blue", "content": {},
        }, "pending")
        ev_failed = self._enqueue(tmp_qm, {
            "message_type": "alert", "priority": "blue", "content": {},
        }, "failed")
        self._set_status(tmp_qm, ev_held, QUIET_HELD_STATUS)
        self._set_status(tmp_qm, ev_done, "done")
        self._set_status(tmp_qm, ev_failed, "failed")
        # ev_pending 保持默认 pending

        digest = get_quiet_hours_digest(tmp_qm)
        assert len(digest) == 1
        assert digest[0]["event_id"] == ev_held
