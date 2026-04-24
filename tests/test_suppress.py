"""TD-002 回归测试 — 覆盖 5 variants × 4 time windows × 3 agent_names。"""
from datetime import datetime, timezone

import pytest

from utils.suppress import is_suppressed


@pytest.fixture
def stock_suppressions():
    """stock suppressions (不依赖 yaml 文件)."""
    return {
        ("akshare_s4", "RemoteDisconnected"): {
            "reason": "test",
            "until": datetime(2026, 5, 1, 14, 59, 59, tzinfo=timezone.utc),  # KST 23:59:59
            "tracking": "TD-003",
        },
        ("akshare_s4", "ConnectionError"): {
            "reason": "test",
            "until": datetime(2026, 5, 1, 14, 59, 59, tzinfo=timezone.utc),  # KST 23:59:59
            "tracking": "TD-003",
        },
    }


ERROR_VARIANTS = [
    "AkShare network err for 000858: ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed'))",
    "ConnectionError: HTTPSConnectionPool(host='push2.eastmoney.com', port=443)",
    "urllib3.exceptions.ProtocolError: ('Connection aborted.', RemoteDisconnected)",
    "requests.exceptions.ConnectionError: something",
    "",  # 空字符串（防 None dereference）
]


@pytest.mark.parametrize("err_msg", ERROR_VARIANTS[:4])  # 前 4 个应命中
@pytest.mark.parametrize(
    "now_tuple,should_hit,desc",
    [
        ((2026, 4, 20, 6, 0), True, "04-20 06:00 UTC = KST 15:00, before until"),
        ((2026, 4, 23, 14, 59), True, "04-23 14:59 UTC = KST 23:59, 1s before until"),
        ((2026, 5, 1, 14, 59), True, "05-01 KST 23:59:59, boundary"),
        ((2026, 5, 1, 15, 1), False, "05-01 KST 00:01 05-02, after until"),
    ],
)
def test_suppress_time_boundaries(err_msg, now_tuple, should_hit, desc, stock_suppressions):
    now = datetime(*now_tuple, tzinfo=timezone.utc)
    r = is_suppressed(
        "akshare_s4",
        [{"error_message": err_msg}],
        suppression_map=stock_suppressions,
        now_utc_dt=now,
    )
    assert (r is not None) == should_hit, f"Expected {should_hit} for {desc}, got {r}"


def test_suppress_empty_error_msg(stock_suppressions):
    """空字符串的 error_message 不崩溃。"""
    now = datetime(2026, 4, 20, 6, 0, tzinfo=timezone.utc)
    r = is_suppressed(
        "akshare_s4",
        [{"error_message": ""}],
        suppression_map=stock_suppressions,
        now_utc_dt=now,
    )
    assert r is None


def test_suppress_none_error_msg(stock_suppressions):
    """None error_message 不崩溃（代码里有 or '' 兜底）."""
    now = datetime(2026, 4, 20, 6, 0, tzinfo=timezone.utc)
    r = is_suppressed(
        "akshare_s4",
        [{"error_message": None}],
        suppression_map=stock_suppressions,
        now_utc_dt=now,
    )
    assert r is None


def test_suppress_wrong_agent_name(stock_suppressions):
    """agent_name 不匹配 → 不命中。"""
    now = datetime(2026, 4, 20, 6, 0, tzinfo=timezone.utc)
    r = is_suppressed(
        "signal_collector",  # 不是 akshare_s4
        [{"error_message": ERROR_VARIANTS[0]}],
        suppression_map=stock_suppressions,
        now_utc_dt=now,
    )
    assert r is None


def test_yaml_integration(tmp_path, monkeypatch):
    """从 yaml 读取真实配置。"""
    yaml_content = """
suppressions:
  - agent_name: test_agent
    pattern: TestError
    reason: unit test
    until: '2099-12-31T23:59:59+09:00'
"""
    p = tmp_path / "suppressions.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    import utils.suppress as sup
    monkeypatch.setattr(sup, "_CONFIG_PATH", p)
    monkeypatch.setattr(sup, "_cache", None)

    r = is_suppressed("test_agent", [{"error_message": "got TestError here"}])
    assert r is not None
    assert r["reason"] == "unit test"
