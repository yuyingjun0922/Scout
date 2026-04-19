"""tests/test_health_monitor.py — v1.15 告警抑制单元测试

覆盖 _is_suppressed 纯函数（不触 DB / push_queue）。集成路径由 manual
smoke 验证（scripts/test_health_monitor.py）+ test_main.py 的调度覆盖。
"""
from datetime import datetime, timezone

from agents.health_monitor_agent import (
    SUPPRESSED_ERRORS,
    _is_suppressed,
)


FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2026, 4, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 4, 20, tzinfo=timezone.utc)


def _row(msg):
    """sqlite3.Row 替身 —— 只需要 ['error_message'] 下标访问。"""
    return {"error_message": msg}


class TestIsSuppressed:
    def test_matches_agent_and_pattern(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = _is_suppressed(
            "akshare_s4",
            [_row("RemoteDisconnected: Remote end closed")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is not None
        assert hit["pattern"] == "RemoteDisconnected"
        assert hit["reason"] == "r"

    def test_wrong_agent_not_suppressed(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = _is_suppressed(
            "paper_d4",
            [_row("RemoteDisconnected")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_non_matching_message_not_suppressed(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = _is_suppressed(
            "akshare_s4",
            [_row("NetworkTimeout: read timeout")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_expired_suppression_ignored(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": PAST}}
        hit = _is_suppressed(
            "akshare_s4",
            [_row("RemoteDisconnected")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_empty_error_message_handled(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = _is_suppressed(
            "akshare_s4",
            [_row(None), _row("")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_mixed_errors_any_match_suppresses(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = _is_suppressed(
            "akshare_s4",
            [_row("Timeout"), _row("RemoteDisconnected: boom"), _row("EOF")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is not None

    def test_default_uses_module_suppressed_errors(self):
        """不传 suppression_map 时走默认的 SUPPRESSED_ERRORS。"""
        # akshare_s4 + RemoteDisconnected 在清单里（2026-04-27 KST 前有效）
        # 用一个 2026-04-20 的 NOW 确保 until 未过
        hit = _is_suppressed(
            "akshare_s4",
            [_row("RemoteDisconnected: boom")],
            now_utc_dt=NOW,
        )
        assert hit is not None
        assert hit["pattern"] == "RemoteDisconnected"


class TestDefaultSuppressionList:
    def test_akshare_entries_present(self):
        keys = set(SUPPRESSED_ERRORS.keys())
        assert ("akshare_s4", "RemoteDisconnected") in keys
        assert ("akshare_s4", "ConnectionError") in keys

    def test_paper_d4_not_suppressed(self):
        """paper_d4 不在清单内，健康告警应正常触发。"""
        for (agent, _pattern) in SUPPRESSED_ERRORS.keys():
            assert agent != "paper_d4"

    def test_all_entries_have_until_and_reason(self):
        for key, cfg in SUPPRESSED_ERRORS.items():
            assert "reason" in cfg and isinstance(cfg["reason"], str) and cfg["reason"]
            assert "until" in cfg and isinstance(cfg["until"], datetime)
            # until 必须 tz-aware（防止和 datetime.now(tz=UTC) 比较失败）
            assert cfg["until"].tzinfo is not None
