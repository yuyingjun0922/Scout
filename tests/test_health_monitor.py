"""tests/test_health_monitor.py — v1.16 告警抑制单元测试（外化后）

覆盖 is_suppressed 纯函数 + yaml 配置健全性。参数化矩阵测试见
tests/test_suppress.py。
"""
from datetime import datetime, timezone

from utils.suppress import get_suppressions, is_suppressed


FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2026, 4, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 4, 20, tzinfo=timezone.utc)


def _row(msg):
    """sqlite3.Row 替身 —— 只需要 ['error_message'] 下标访问。"""
    return {"error_message": msg}


class TestIsSuppressed:
    def test_matches_agent_and_pattern(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = is_suppressed(
            "akshare_s4",
            [_row("RemoteDisconnected: Remote end closed")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is not None
        assert hit["pattern"] == "RemoteDisconnected"
        assert hit["reason"] == "r"

    def test_wrong_agent_not_suppressed(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = is_suppressed(
            "paper_d4",
            [_row("RemoteDisconnected")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_non_matching_message_not_suppressed(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = is_suppressed(
            "akshare_s4",
            [_row("NetworkTimeout: read timeout")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_expired_suppression_ignored(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": PAST}}
        hit = is_suppressed(
            "akshare_s4",
            [_row("RemoteDisconnected")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_empty_error_message_handled(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = is_suppressed(
            "akshare_s4",
            [_row(None), _row("")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is None

    def test_mixed_errors_any_match_suppresses(self):
        m = {("akshare_s4", "RemoteDisconnected"): {"reason": "r", "until": FAR_FUTURE}}
        hit = is_suppressed(
            "akshare_s4",
            [_row("Timeout"), _row("RemoteDisconnected: boom"), _row("EOF")],
            suppression_map=m, now_utc_dt=NOW,
        )
        assert hit is not None

    def test_default_uses_yaml_config(self):
        """不传 suppression_map 时从 config/suppressions.yaml 读。"""
        # akshare_s4 + RemoteDisconnected 在清单里（2026-05-01 KST 前有效）
        # 用一个 2026-04-20 的 NOW 确保 until 未过
        hit = is_suppressed(
            "akshare_s4",
            [_row("RemoteDisconnected: boom")],
            now_utc_dt=NOW,
        )
        assert hit is not None
        assert hit["pattern"] == "RemoteDisconnected"


class TestYamlConfigHealth:
    """v1.16 起规则来源是 config/suppressions.yaml；此组验证 yaml 配置健全性。"""

    def test_akshare_entries_present(self):
        keys = set(get_suppressions(force_reload=True).keys())
        assert ("akshare_s4", "RemoteDisconnected") in keys
        assert ("akshare_s4", "ConnectionError") in keys

    def test_paper_d4_not_suppressed(self):
        """paper_d4 不在清单内，健康告警应正常触发。"""
        for (agent, _pattern) in get_suppressions(force_reload=True).keys():
            assert agent != "paper_d4"

    def test_all_entries_have_until_and_reason(self):
        for _key, cfg in get_suppressions(force_reload=True).items():
            assert "reason" in cfg and isinstance(cfg["reason"], str) and cfg["reason"]
            assert "until" in cfg and isinstance(cfg["until"], datetime)
            # until 必须 tz-aware（防止和 datetime.now(tz=UTC) 比较失败）
            assert cfg["until"].tzinfo is not None
