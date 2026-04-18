"""
tests/test_bias_checker.py — BiasChecker 测试矩阵 (v1.04)

覆盖：
  - 4 个 check 各 3-4 个测试（触发 / 不触发 / 缺数据 → None）
  - stage 路由
  - downgrade_threshold 累计降级
  - 配置开关
  - check() 异常兜底
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.bias_checker import (
    STAGE_CHECKS,
    VALID_STAGES,
    BiasChecker,
    BiasReport,
    load_bias_config,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "bias_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture
def checker(tmp_db):
    return BiasChecker(tmp_db)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed_info_unit(
    db: DatabaseManager,
    *,
    uid: str,
    industry: str = "半导体",
    days_ago: int = 5,
    policy_direction: str = "neutral",
    content: str = "正常更新",
    source: str = "D1",
) -> None:
    ts = _ts_ago(days_ago)
    db.write(
        """INSERT INTO info_units
           (id, source, timestamp, content, related_industries,
            policy_direction, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (uid, source, ts, content, f'["{industry}"]',
         policy_direction, _now_iso(), _now_iso()),
    )


def _seed_track(db: DatabaseManager, stock: str, industry: str) -> None:
    db.write(
        """INSERT INTO track_list (stock, industry, company_name, updated_at)
           VALUES (?, ?, ?, ?)""",
        (stock, industry, f"公司_{stock}", _now_iso()),
    )


def _seed_rejected(
    db: DatabaseManager, stock: str, industry_id: int, reason: str
) -> None:
    db.write(
        """INSERT INTO rejected_stocks
           (stock, industry_id, rejected_at, reject_reason, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (stock, industry_id, _now_iso(), reason, _now_iso()),
    )


def _seed_watchlist(db: DatabaseManager, name: str) -> int:
    db.write(
        """INSERT INTO watchlist (industry_name) VALUES (?)""",
        (name,),
    )
    row = db.query_one(
        "SELECT industry_id FROM watchlist WHERE industry_name=?", (name,)
    )
    return row["industry_id"]


def _seed_related_removed(
    db: DatabaseManager, stock: str, industry: str, *, removed_reason: str = "kill"
) -> None:
    db.write(
        """INSERT INTO related_stocks
           (stock_code, industry, market, status, removed_reason, updated_at)
           VALUES (?, ?, 'A', 'removed', ?, ?)""",
        (stock, industry, removed_reason, _now_iso()),
    )


def _seed_financial(
    db: DatabaseManager, stock: str, period: str, revenue: float
) -> None:
    db.write(
        """INSERT INTO stock_financials (stock, report_period, revenue, updated_at)
           VALUES (?, ?, ?, ?)""",
        (stock, period, revenue, _now_iso()),
    )


# ═══════════════════ B01 确认偏误 ═══════════════════


class TestB01Confirmation:
    def test_no_industry_returns_none(self, checker):
        out = checker._check_b01_confirmation({})
        assert out is None

    def test_under_threshold_returns_empty(self, checker, tmp_db):
        # 阈值 3，只塞 2 条 restrictive
        for i in range(2):
            _seed_info_unit(
                tmp_db, uid=f"u{i}", policy_direction="restrictive",
                content="政策限制条款"
            )
        out = checker._check_b01_confirmation(
            {"industry": "半导体", "direction": "supportive"}
        )
        assert out == []

    def test_above_threshold_warns(self, checker, tmp_db):
        for i in range(4):
            _seed_info_unit(
                tmp_db, uid=f"u{i}", policy_direction="restrictive",
                content="行业整改通知"
            )
        out = checker._check_b01_confirmation(
            {"industry": "半导体", "direction": "supportive"}
        )
        assert len(out) == 1
        assert out[0].code == "b01"
        assert "确认偏误" in out[0].message
        assert out[0].evidence["negative_count"] == 4

    def test_keyword_only_match(self, checker, tmp_db):
        """policy_direction 不是 restrictive，但 content 命中关键词。"""
        for i in range(3):
            _seed_info_unit(
                tmp_db, uid=f"k{i}", policy_direction="neutral",
                content=f"传出公司被调查的消息 {i}"
            )
        out = checker._check_b01_confirmation(
            {"industry": "半导体", "direction": "supportive"}
        )
        assert len(out) == 1
        assert out[0].evidence["negative_count"] >= 3

    def test_lookback_filters_old_signals(self, checker, tmp_db):
        """超过 90 天的信号不应计入。"""
        for i in range(5):
            _seed_info_unit(
                tmp_db, uid=f"old{i}", days_ago=200,
                policy_direction="restrictive",
                content="历史限制条款"
            )
        out = checker._check_b01_confirmation(
            {"industry": "半导体", "direction": "supportive"}
        )
        assert out == []

    def test_restrictive_direction_skipped(self, checker, tmp_db):
        """方向已是 restrictive → 不需提醒，返 []。"""
        for i in range(5):
            _seed_info_unit(
                tmp_db, uid=f"r{i}", policy_direction="restrictive",
                content="限制"
            )
        out = checker._check_b01_confirmation(
            {"industry": "半导体", "direction": "restrictive"}
        )
        assert out == []


# ═══════════════════ B02 幸存者偏差 ═══════════════════


class TestB02Survivorship:
    def test_no_industry_returns_none(self, checker):
        assert checker._check_b02_survivorship({}) is None

    def test_no_removed_returns_empty(self, checker, tmp_db):
        _seed_watchlist(tmp_db, "半导体")
        out = checker._check_b02_survivorship({"industry": "半导体"})
        assert out == []

    def test_rejected_only(self, checker, tmp_db):
        ind_id = _seed_watchlist(tmp_db, "半导体")
        _seed_rejected(tmp_db, "111111", ind_id, "财务造假")
        _seed_rejected(tmp_db, "222222", ind_id, "技术过时")
        out = checker._check_b02_survivorship({"industry": "半导体"})
        assert len(out) == 1
        assert out[0].code == "b02"
        assert out[0].evidence["removed_count"] == 2

    def test_related_stocks_removed(self, checker, tmp_db):
        _seed_related_removed(tmp_db, "333333", "半导体")
        out = checker._check_b02_survivorship({"industry": "半导体"})
        assert len(out) == 1
        assert out[0].evidence["removed_count"] == 1

    def test_combined_sources(self, checker, tmp_db):
        ind_id = _seed_watchlist(tmp_db, "新能源车")
        _seed_rejected(tmp_db, "100001", ind_id, "失败")
        _seed_related_removed(tmp_db, "100002", "新能源车")
        out = checker._check_b02_survivorship({"industry": "新能源车"})
        assert out[0].evidence["removed_count"] == 2


# ═══════════════════ B03 基数效应 ═══════════════════


class TestB03BaseEffect:
    def test_no_stock_returns_none(self, checker):
        assert checker._check_b03_base_effect({}) is None

    def test_insufficient_history_returns_none(self, checker, tmp_db):
        """只有 2 个年报 → 数据不足。"""
        _seed_financial(tmp_db, "600519", "2024-12-31", 1000.0)
        _seed_financial(tmp_db, "600519", "2025-12-31", 1500.0)
        out = checker._check_b03_base_effect({"stock": "600519"})
        assert out is None

    def test_triggers_on_low_base_rebound(self, checker, tmp_db):
        """去年 -50%，今年 +60% → 触发。"""
        _seed_financial(tmp_db, "600519", "2023-12-31", 200.0)
        _seed_financial(tmp_db, "600519", "2024-12-31", 100.0)  # -50%
        _seed_financial(tmp_db, "600519", "2025-12-31", 160.0)  # +60%
        out = checker._check_b03_base_effect({"stock": "600519"})
        assert len(out) == 1
        assert out[0].code == "b03"
        assert "基数效应" in out[0].message
        assert out[0].evidence["yoy_now_pct"] == 60.0
        assert out[0].evidence["yoy_prev_pct"] == -50.0

    def test_no_warning_on_normal_growth(self, checker, tmp_db):
        """每年都涨 10% → 不触发。"""
        _seed_financial(tmp_db, "600519", "2023-12-31", 100.0)
        _seed_financial(tmp_db, "600519", "2024-12-31", 110.0)
        _seed_financial(tmp_db, "600519", "2025-12-31", 121.0)
        out = checker._check_b03_base_effect({"stock": "600519"})
        assert out == []

    def test_high_growth_no_prev_drop(self, checker, tmp_db):
        """今年 +50% 但去年也 +30% → 不属于基数效应。"""
        _seed_financial(tmp_db, "600519", "2023-12-31", 100.0)
        _seed_financial(tmp_db, "600519", "2024-12-31", 130.0)
        _seed_financial(tmp_db, "600519", "2025-12-31", 195.0)
        out = checker._check_b03_base_effect({"stock": "600519"})
        assert out == []


# ═══════════════════ B04 行业集中度 ═══════════════════


class TestB04Concentration:
    def test_no_industry_returns_none(self, checker):
        assert checker._check_b04_concentration({}) is None

    def test_under_threshold(self, checker, tmp_db):
        for i in range(3):
            _seed_track(tmp_db, f"code{i}", "半导体")
        out = checker._check_b04_concentration({"industry": "半导体"})
        assert out == []  # 阈值 5

    def test_at_threshold_warns(self, checker, tmp_db):
        for i in range(5):
            _seed_track(tmp_db, f"code{i}", "半导体")
        out = checker._check_b04_concentration({"industry": "半导体"})
        assert len(out) == 1
        assert out[0].code == "b04"
        assert out[0].evidence["track_list_count"] == 5

    def test_high_severity_at_double_threshold(self, checker, tmp_db):
        for i in range(10):
            _seed_track(tmp_db, f"code{i}", "半导体")
        out = checker._check_b04_concentration({"industry": "半导体"})
        assert out[0].severity == "high"

    def test_other_industry_not_counted(self, checker, tmp_db):
        for i in range(5):
            _seed_track(tmp_db, f"code{i}", "新能源")
        out = checker._check_b04_concentration({"industry": "半导体"})
        assert out == []


# ═══════════════════ stage 路由 ═══════════════════


class TestStageRouting:
    def test_direction_stage_runs_only_b01(self, checker, tmp_db):
        for i in range(3):
            _seed_track(tmp_db, f"c{i}", "半导体")
        for i in range(5):
            _seed_track(tmp_db, f"d{i}", "半导体")
        out = checker.check(
            {"industry": "半导体", "stock": "600519", "direction": "supportive"},
            stage="direction",
        )
        bw = out["bias_warnings"]
        # b04 不在 direction 中 → 不应 fire
        assert bw["counts"]["b04"] is None
        assert bw["counts"]["b01"] is not None
        assert bw["stage"] == "direction"

    def test_decision_stage_runs_b03_b04(self, checker, tmp_db):
        for i in range(5):
            _seed_track(tmp_db, f"x{i}", "半导体")
        out = checker.check(
            {"industry": "半导体", "stock": "600519"}, stage="decision"
        )
        bw = out["bias_warnings"]
        assert bw["counts"]["b04"] == 1
        # b01 / b02 不在 decision → counts 为 None
        assert bw["counts"]["b01"] is None
        assert bw["counts"]["b02"] is None

    def test_review_runs_b02(self, checker, tmp_db):
        ind_id = _seed_watchlist(tmp_db, "半导体")
        _seed_rejected(tmp_db, "100001", ind_id, "fraud")
        out = checker.check({"industry": "半导体"}, stage="review")
        assert out["bias_warnings"]["counts"]["b02"] == 1

    def test_unknown_stage_falls_back_to_all(self, checker):
        out = checker.check({"industry": "半导体"}, stage="bogus")
        assert out["bias_warnings"]["stage"] == "all"


# ═══════════════════ downgrade 累计 ═══════════════════


class TestDowngrade:
    def test_three_warnings_triggers_downgrade(self, checker, tmp_db):
        # B01: 4 条 restrictive
        for i in range(4):
            _seed_info_unit(
                tmp_db, uid=f"n{i}", policy_direction="restrictive",
                content="禁止类政策"
            )
        # B02: 1 条已否决
        ind_id = _seed_watchlist(tmp_db, "半导体")
        _seed_rejected(tmp_db, "100001", ind_id, "kill")
        # B04: 5 条 track
        for i in range(5):
            _seed_track(tmp_db, f"t{i}", "半导体")
        # B03 不会 fire（无 financials）
        out = checker.check(
            {"industry": "半导体", "stock": "600519", "direction": "supportive"},
            stage="all",
        )
        bw = out["bias_warnings"]
        assert len(bw["warnings"]) == 3
        assert bw["downgrade"] is True

    def test_two_warnings_no_downgrade(self, checker, tmp_db):
        for i in range(4):
            _seed_info_unit(
                tmp_db, uid=f"n{i}", policy_direction="restrictive",
                content="禁止"
            )
        for i in range(5):
            _seed_track(tmp_db, f"t{i}", "半导体")
        out = checker.check(
            {"industry": "半导体", "direction": "supportive"}, stage="all"
        )
        assert len(out["bias_warnings"]["warnings"]) == 2
        assert out["bias_warnings"]["downgrade"] is False


# ═══════════════════ 配置开关 ═══════════════════


class TestConfigToggle:
    def test_disabling_check_skips_it(self, tmp_db):
        cfg = {"b04_concentration_risk": False}
        c = BiasChecker(tmp_db, config=cfg)
        for i in range(10):
            _seed_track(tmp_db, f"c{i}", "半导体")
        out = c.check({"industry": "半导体"}, stage="decision")
        # b04 disabled → counts.b04 仍为 None（未跑）
        assert out["bias_warnings"]["counts"]["b04"] is None

    def test_load_bias_config_returns_defaults_when_missing(self, tmp_path):
        cfg = load_bias_config(tmp_path / "nope.yaml")
        assert cfg["concentration_threshold"] == 5
        assert cfg["downgrade_threshold"] == 3

    def test_load_bias_config_reads_yaml(self):
        cfg = load_bias_config()
        assert cfg["b01_confirmation_bias"] is True
        assert "禁止" in cfg["b01_negative_keywords"]


# ═══════════════════ check() 错误兜底 ═══════════════════


class TestCheckRobustness:
    def test_internal_error_does_not_propagate(self, checker, monkeypatch):
        def boom(_):
            raise RuntimeError("boom")
        monkeypatch.setattr(checker, "_check_b01_confirmation", boom)
        out = checker.check({"industry": "半导体"}, stage="direction")
        # 没崩，counts.b01 = None（因 unknown 错被吞 → None）
        assert out["bias_warnings"]["counts"]["b01"] is None
        assert out["bias_warnings"]["downgrade"] is False

    def test_input_dict_not_mutated(self, checker):
        original = {"industry": "半导体"}
        original_copy = dict(original)
        checker.check(original, stage="direction")
        assert original == original_copy

    def test_run_alias(self, checker):
        out = checker.run({"industry": "半导体"}, "direction")
        assert "bias_warnings" in out


# ═══════════════════ 集成场景 ═══════════════════


class TestIntegrationScenarios:
    def test_direction_scenario_triggers_b01(self, checker, tmp_db):
        for i in range(4):
            _seed_info_unit(
                tmp_db, uid=f"i{i}", industry="半导体",
                policy_direction="restrictive",
                content="行业暴跌"
            )
        out = checker.check(
            {"industry": "半导体", "direction": "supportive"}, stage="direction"
        )
        warnings = out["bias_warnings"]["warnings"]
        assert any(w["code"] == "b01" for w in warnings)
        assert out["bias_warnings"]["downgrade"] is False

    def test_decision_scenario_triggers_b04(self, checker, tmp_db):
        for i in range(6):
            _seed_track(tmp_db, f"x{i}", "新能源")
        out = checker.check(
            {"industry": "新能源", "stock": "300750"}, stage="decision"
        )
        warnings = out["bias_warnings"]["warnings"]
        assert any(w["code"] == "b04" for w in warnings)


# ═══════════════════ STAGE_CHECKS 完整性 ═══════════════════


class TestStageMatrix:
    def test_all_stages_have_checks(self):
        for stage in VALID_STAGES:
            assert STAGE_CHECKS[stage], f"{stage} has empty check list"

    def test_all_codes_are_valid(self):
        valid = {"b01", "b02", "b03", "b04"}
        for stage, codes in STAGE_CHECKS.items():
            for c in codes:
                assert c in valid, f"{stage} references unknown {c}"


# ═══════════════════ BiasReport 序列化 ═══════════════════


class TestBiasReport:
    def test_to_dict_round_trip(self):
        r = BiasReport(
            code="b01", severity="medium", message="test",
            evidence={"k": 1},
        )
        d = r.to_dict()
        assert d["code"] == "b01"
        assert d["evidence"]["k"] == 1
