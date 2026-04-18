"""
tests/test_recommendation_agent.py — RecommendationAgent 测试矩阵（v1.07）

覆盖：
  - Phase 1 硬底线：4 类触发 + 全 PASS
  - Phase 2 6 维度：每维至少 happy / 缺数据 / 边界
  - Phase 3 综合验证：GateAgent / Master / Bias 加减分
  - Phase 4 级别分桶
  - 反方卡片构建
  - 持久化 + 幂等（thesis_hash）
  - analyze 错误降级（永不抛）
  - 全量 run() / load_universe 过滤
  - _level_from_score / _make_dim 工具
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agents.recommendation_agent import (
    GAP_FILLABILITY_FATAL,
    LEVEL_A,
    LEVEL_A_MIN,
    LEVEL_B,
    LEVEL_B_MIN,
    LEVEL_CANDIDATE,
    LEVEL_CANDIDATE_MIN,
    LEVEL_REJECT,
    POLICY_LOOKBACK_DAYS,
    VERIFY_BIAS_WARN_THRESHOLD,
    VERIFY_BONUS_GATE,
    VERIFY_BONUS_MASTER,
    VERIFY_GATE_SIGNAL_THRESHOLD,
    VERIFY_MASTER_MIN_POSITIVES,
    VERIFY_PENALTY_BIAS,
    WEIGHTS,
    WEIGHT_TOTAL,
    Z_SCORE_DISTRESS,
    Z_SCORE_SAFE,
    CounterCard,
    DimensionScore,
    HardGateResult,
    RecommendationAgent,
    VerificationDelta,
    _make_dim,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "rec_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture
def agent(tmp_db):
    return RecommendationAgent(tmp_db)


def _utc(offset_days: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=offset_days)
    ).isoformat()


def _seed_industry(
    db: DatabaseManager,
    industry: str = "AI算力",
    industry_id: int = 1,
    *,
    motivation_uncertainty: str | None = "low",
    gap_fillability: int | None = 4,
):
    db.write(
        """INSERT INTO industry_dict
           (industry, aliases, cyclical, scout_range, sub_industries, confidence)
           VALUES (?, '[]', 0, 'active', '[]', 'approved')""",
        (industry,),
    )
    db.write(
        """INSERT INTO watchlist
           (industry_id, industry_name, zone, source_type, early_signal,
            motivation_uncertainty, gap_fillability)
           VALUES (?, ?, 'active', 'manual', 0, ?, ?)""",
        (industry_id, industry, motivation_uncertainty, gap_fillability),
    )


def _seed_stock(
    db: DatabaseManager,
    stock_code: str,
    industry: str = "AI算力",
    industry_id: int = 1,
    market: str = "A",
    confidence: str = "approved",
    status: str = "active",
):
    db.write(
        """INSERT INTO related_stocks
           (industry_id, industry, stock_code, stock_name, market,
            discovery_source, discovered_at, confidence, status, updated_at)
           VALUES (?, ?, ?, ?, ?, 'test', ?, ?, ?, ?)""",
        (industry_id, industry, stock_code, f"NAME_{stock_code}",
         market, _utc(), confidence, status, _utc()),
    )


def _seed_info_unit(
    db: DatabaseManager,
    *,
    uid: str,
    industry: str = "AI算力",
    direction: str = "supportive",
    source: str = "D1",
    content: str = "AI 行业政策利好",
    days_ago: int = 1,
):
    db.write(
        """INSERT INTO info_units
           (id, source, source_credibility, timestamp, category, content,
            related_industries, policy_direction, schema_version,
            created_at, updated_at)
           VALUES (?, ?, 'high', ?, 'policy', ?, ?, ?, '1.0', ?, ?)""",
        (uid, source, _utc(-days_ago), content,
         json.dumps([industry], ensure_ascii=False),
         direction, _utc(), _utc()),
    )


def _seed_financials(
    db: DatabaseManager,
    stock: str,
    z_score: float | None = 3.0,
    peg_ratio: float | None = 1.2,
    pe_ttm: float = 18.0,
):
    db.write(
        """INSERT INTO stock_financials
           (stock, report_period, z_score, peg_ratio, pe_ttm, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (stock, "2026-03-31", z_score, peg_ratio, pe_ttm, _utc()),
    )


# ═══════════════════ 工具函数 ═══════════════════


class TestHelpers:
    def test_make_dim_weighted(self):
        d = _make_dim("d1", 80, 15, "note", {"k": "v"})
        assert d.code == "d1"
        assert d.score == 80
        assert d.weight == 15
        assert d.weighted == 12.0
        assert d.note == "note"
        assert d.evidence == {"k": "v"}

    def test_dimension_score_to_dict(self):
        d = _make_dim("d2", 50, 15, "x", {})
        out = d.to_dict()
        assert set(out.keys()) == {"code", "score", "weight", "weighted", "note", "evidence"}
        assert out["weighted"] == 7.5

    def test_weights_total_80(self):
        assert WEIGHT_TOTAL == 80
        assert WEIGHTS["d1"] + WEIGHTS["d2"] + WEIGHTS["d3"] == 45
        assert WEIGHTS["d4"] + WEIGHTS["d5"] + WEIGHTS["d6"] == 35


class TestLevelFromScore:
    def test_a_threshold(self):
        assert RecommendationAgent._level_from_score(LEVEL_A_MIN) == LEVEL_A
        assert RecommendationAgent._level_from_score(99.9) == LEVEL_A

    def test_b_threshold(self):
        assert RecommendationAgent._level_from_score(LEVEL_B_MIN) == LEVEL_B
        assert RecommendationAgent._level_from_score(74.9) == LEVEL_B

    def test_candidate_threshold(self):
        assert RecommendationAgent._level_from_score(LEVEL_CANDIDATE_MIN) == LEVEL_CANDIDATE
        assert RecommendationAgent._level_from_score(59.9) == LEVEL_CANDIDATE

    def test_reject_threshold(self):
        assert RecommendationAgent._level_from_score(0) == LEVEL_REJECT
        assert RecommendationAgent._level_from_score(39.9) == LEVEL_REJECT


# ═══════════════════ Phase 1 硬底线 ═══════════════════


class TestPhase1HardGates:
    def test_all_pass_no_negative_data(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_stock(tmp_db, "600001")
        gate = agent._phase1_hard_gates("600001", "AI算力")
        assert gate.passed
        assert gate.fail_reasons == []
        assert gate.checked["policy_fatal_count"] == 0
        assert gate.checked["gap_fillability"] == 4

    def test_policy_fatal_triggers(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_info_unit(
            tmp_db, uid="r1", direction="restrictive",
            content="禁止 AI 出口", days_ago=10,
        )
        gate = agent._phase1_hard_gates("600001", "AI算力")
        assert not gate.passed
        assert any("policy_fatal" in r for r in gate.fail_reasons)

    def test_policy_fatal_outside_lookback_window_passes(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_info_unit(
            tmp_db, uid="rold", direction="restrictive",
            days_ago=POLICY_LOOKBACK_DAYS + 30,
        )
        gate = agent._phase1_hard_gates("600001", "AI算力")
        assert gate.passed

    def test_z_score_distress_triggers(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_financials(tmp_db, "600002", z_score=1.0, peg_ratio=1.0)
        gate = agent._phase1_hard_gates("600002", "AI算力")
        assert not gate.passed
        assert any("z_score_distress" in r for r in gate.fail_reasons)

    def test_z_score_at_threshold_passes(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        # Exactly threshold should not trigger (strict <)
        _seed_financials(tmp_db, "600003", z_score=Z_SCORE_DISTRESS, peg_ratio=1.0)
        gate = agent._phase1_hard_gates("600003", "AI算力")
        assert gate.passed

    def test_z_score_missing_passes(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        gate = agent._phase1_hard_gates("600099", "AI算力")
        assert gate.passed
        assert gate.checked["z_score"] is None

    def test_gap_fillability_unfillable_triggers(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=1)
        gate = agent._phase1_hard_gates("600004", "AI算力")
        assert not gate.passed
        assert any("gap_unfillable" in r for r in gate.fail_reasons)

    def test_gap_fillability_at_threshold_passes(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=GAP_FILLABILITY_FATAL)
        gate = agent._phase1_hard_gates("600005", "AI算力")
        assert gate.passed

    def test_risk_flag_2_triggers(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        tmp_db.write(
            """INSERT INTO track_list (stock, market, risk_flag, risk_detail)
               VALUES (?, 'A', 2, 'major risk')""",
            ("600006",),
        )
        gate = agent._phase1_hard_gates("600006", "AI算力")
        assert not gate.passed
        assert any("risk_flag" in r for r in gate.fail_reasons)

    def test_in_rejected_stocks_triggers(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        tmp_db.write(
            """INSERT INTO rejected_stocks (stock, rejected_at, reject_reason)
               VALUES (?, ?, 'demo')""",
            ("600007", _utc()),
        )
        gate = agent._phase1_hard_gates("600007", "AI算力")
        assert not gate.passed
        assert any("rejected_stocks" in r for r in gate.fail_reasons)

    def test_no_industry_skips_policy_check(self, tmp_db, agent):
        gate = agent._phase1_hard_gates("999999", None)
        assert gate.passed
        assert gate.checked["policy_fatal_count"] is None


# ═══════════════════ Phase 2 6 维度 ═══════════════════


class TestD1PolicyFunding:
    def test_funded_keyword(self, tmp_db, agent):
        _seed_industry(tmp_db)
        _seed_info_unit(
            tmp_db, uid="f1", direction="supportive",
            content="设立专项资金支持算力建设",
        )
        d = agent._d1_policy_funding("AI算力")
        assert d.score == 100
        assert d.evidence["funded_count"] == 1

    def test_mandatory_keyword(self, tmp_db, agent):
        _seed_industry(tmp_db)
        _seed_info_unit(
            tmp_db, uid="m1", direction="supportive",
            content="必须按时完成算力部署任务",
        )
        d = agent._d1_policy_funding("AI算力")
        assert d.score == 75
        assert d.evidence["mandatory_count"] == 1

    def test_directive_only(self, tmp_db, agent):
        _seed_industry(tmp_db)
        _seed_info_unit(
            tmp_db, uid="x1", direction="supportive",
            content="鼓励发展 AI 算力",
        )
        d = agent._d1_policy_funding("AI算力")
        assert d.score == 25
        assert d.evidence["level"] == "directive"

    def test_no_data_default_directive(self, tmp_db, agent):
        _seed_industry(tmp_db)
        d = agent._d1_policy_funding("AI算力")
        assert d.score == 25
        assert d.evidence["level"] == "default_directive"

    def test_no_industry(self, agent):
        d = agent._d1_policy_funding(None)
        assert d.score == 25


class TestD2Motivation:
    def test_low_uncertainty_stable(self, tmp_db, agent):
        _seed_industry(tmp_db, motivation_uncertainty="low")
        d = agent._d2_motivation_persistence("AI算力")
        assert d.score == 100
        assert d.evidence["label"] == "stable"

    def test_medium_uncertainty_drifting(self, tmp_db, agent):
        _seed_industry(tmp_db, motivation_uncertainty="medium")
        d = agent._d2_motivation_persistence("AI算力")
        assert d.score == 50
        assert d.evidence["label"] == "drifting"

    def test_high_uncertainty_reversing(self, tmp_db, agent):
        _seed_industry(tmp_db, motivation_uncertainty="high")
        d = agent._d2_motivation_persistence("AI算力")
        assert d.score == 0
        assert d.evidence["label"] == "reversing"

    def test_missing_default_75(self, tmp_db, agent):
        _seed_industry(tmp_db, motivation_uncertainty=None)
        d = agent._d2_motivation_persistence("AI算力")
        assert d.score == 75

    def test_no_industry(self, agent):
        d = agent._d2_motivation_persistence(None)
        assert d.score == 75


class TestD3GapFillability:
    @pytest.mark.parametrize("gap,expected", [
        (5, 100), (4, 100), (3, 75), (2, 50), (1, 25), (0, 0),
    ])
    def test_gap_score_matrix(self, tmp_db, agent, gap, expected):
        _seed_industry(tmp_db, gap_fillability=gap)
        d = agent._d3_gap_fillability("AI算力")
        assert d.score == expected

    def test_missing_default_50(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=None)
        d = agent._d3_gap_fillability("AI算力")
        assert d.score == 50

    def test_no_industry(self, agent):
        d = agent._d3_gap_fillability(None)
        assert d.score == 50


class TestD4DataVerification:
    def test_full_count(self, tmp_db, agent):
        _seed_industry(tmp_db)
        for i in range(6):
            _seed_info_unit(tmp_db, uid=f"v1_{i}", source="V1")
        d = agent._d4_data_verification("AI算力")
        assert d.score == 100
        assert d.evidence["verify_count_90d"] == 6

    def test_partial_count(self, tmp_db, agent):
        _seed_industry(tmp_db)
        _seed_info_unit(tmp_db, uid="v3a", source="V3")
        _seed_info_unit(tmp_db, uid="v3b", source="V3")
        d = agent._d4_data_verification("AI算力")
        assert d.score == 50

    def test_zero_count(self, tmp_db, agent):
        _seed_industry(tmp_db)
        d = agent._d4_data_verification("AI算力")
        assert d.score == 20

    def test_d1_source_excluded(self, tmp_db, agent):
        _seed_industry(tmp_db)
        for i in range(5):
            _seed_info_unit(tmp_db, uid=f"d1_{i}", source="D1")
        d = agent._d4_data_verification("AI算力")
        assert d.score == 20  # D1 not counted


class TestD5StockFinancials:
    def test_safe_and_cheap_full_score(self, tmp_db, agent):
        _seed_financials(tmp_db, "600001", z_score=3.5, peg_ratio=1.0)
        d = agent._d5_stock_financials("600001")
        assert d.score == 100

    def test_safe_only(self, tmp_db, agent):
        _seed_financials(tmp_db, "600002", z_score=3.0, peg_ratio=2.0)
        d = agent._d5_stock_financials("600002")
        assert d.score == 75

    def test_cheap_only(self, tmp_db, agent):
        _seed_financials(tmp_db, "600003", z_score=2.0, peg_ratio=1.0)
        d = agent._d5_stock_financials("600003")
        assert d.score == 75

    def test_distress_zero(self, tmp_db, agent):
        _seed_financials(tmp_db, "600004", z_score=0.5, peg_ratio=2.0)
        d = agent._d5_stock_financials("600004")
        assert d.score == 0

    def test_grey_zone(self, tmp_db, agent):
        _seed_financials(tmp_db, "600005", z_score=2.0, peg_ratio=2.0)
        d = agent._d5_stock_financials("600005")
        assert d.score == 50

    def test_missing_data_default_50(self, agent):
        d = agent._d5_stock_financials("999999")
        assert d.score == 50
        assert d.evidence.get("data_missing") is True

    def test_z_missing_default_50(self, tmp_db, agent):
        _seed_financials(tmp_db, "600006", z_score=None, peg_ratio=1.0)
        d = agent._d5_stock_financials("600006")
        assert d.score == 50


class TestD6Valuation:
    def test_peg_cheap(self, tmp_db, agent):
        _seed_financials(tmp_db, "600001", peg_ratio=1.0)
        d = agent._d6_valuation("600001")
        assert d.score == 100

    def test_peg_mid(self, tmp_db, agent):
        _seed_financials(tmp_db, "600002", peg_ratio=2.0)
        d = agent._d6_valuation("600002")
        assert d.score == 50

    def test_peg_high(self, tmp_db, agent):
        _seed_financials(tmp_db, "600003", peg_ratio=3.0)
        d = agent._d6_valuation("600003")
        assert d.score == 25

    def test_peg_negative(self, tmp_db, agent):
        _seed_financials(tmp_db, "600004", peg_ratio=-1.0)
        d = agent._d6_valuation("600004")
        assert d.score == 50

    def test_missing(self, agent):
        d = agent._d6_valuation("999999")
        assert d.score == 50


# ═══════════════════ Phase 3 综合验证 ═══════════════════


class TestPhase3Verify:
    def test_no_signals_no_delta(self, tmp_db, agent):
        verify = agent._phase3_verify("600001", "AI算力", 50.0)
        assert verify.delta == 0
        assert verify.gate_signals_count == 0

    def test_gate_signals_bonus(self, tmp_db, agent):
        _seed_industry(tmp_db)
        # 5 信号即触发
        for i in range(VERIFY_GATE_SIGNAL_THRESHOLD):
            _seed_info_unit(tmp_db, uid=f"gs_{i}", source="V1")
        verify = agent._phase3_verify("600001", "AI算力", 50.0)
        assert verify.delta == VERIFY_BONUS_GATE
        assert verify.gate_signals_count >= VERIFY_GATE_SIGNAL_THRESHOLD

    def test_master_positive_count_no_data_returns_none(self, tmp_db, agent):
        # 缺 financials → MasterAgent lazy invoke 也会返 ok=False → None
        cnt = agent._master_positive_count("999999")
        assert cnt is None or cnt == 0

    def test_master_positive_count_from_existing_records(self, tmp_db, agent):
        ts = _utc()
        for name, score in [
            ("buffett", 80), ("munger", 70), ("duan", 65),
            ("lynch", 30), ("fisher", 40),
        ]:
            tmp_db.write(
                """INSERT INTO master_analysis
                   (stock, master_name, score, verdict, details, analyzed_at)
                   VALUES (?, ?, ?, 'pass', '{}', ?)""",
                ("600001", name, score, ts),
            )
        cnt = agent._master_positive_count("600001")
        assert cnt == 3  # 80/70/65 ≥ 60

    def test_bias_warning_count_safe(self, agent):
        cnt = agent._bias_warning_count("600001", "AI算力")
        # bias_checker 数据不足时返 0 或 None
        assert cnt is None or isinstance(cnt, int)


# ═══════════════════ analyze 主入口 ═══════════════════


class TestAnalyze:
    def test_analyze_unknown_symbol_data_missing(self, agent):
        out = agent.analyze("999999")
        assert out["ok"] is True  # _analyze_one 容忍无 industry
        assert out["level"] in (LEVEL_A, LEVEL_B, LEVEL_CANDIDATE, LEVEL_REJECT)
        assert "report" in out

    def test_analyze_invalid_symbol_returns_error(self, agent):
        out = agent.analyze("")
        assert out["ok"] is False
        assert out["level"] == LEVEL_REJECT

    def test_analyze_full_industry_path_no_financials(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4, motivation_uncertainty="low")
        _seed_stock(tmp_db, "600001")
        out = agent.analyze("600001")
        assert out["ok"]
        assert out["industry"] == "AI算力"
        assert out["industry_id"] == 1
        assert out["phase1_passed"]
        assert "dimensions" in out
        # 6 维度都返
        assert set(out["dimensions"].keys()) == {"d1", "d2", "d3", "d4", "d5", "d6"}

    def test_analyze_returns_counter_card(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_stock(tmp_db, "600001")
        out = agent.analyze("600001")
        cc = out["counter_card"]
        assert "risks" in cc
        assert "data_gaps" in cc
        assert "contrary_signals" in cc

    def test_analyze_hard_gate_fail_returns_reject(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=1)  # < FATAL
        _seed_stock(tmp_db, "600002")
        out = agent.analyze("600002")
        assert out["level"] == LEVEL_REJECT
        assert not out["phase1_passed"]
        assert any("gap_unfillable" in r for r in out["phase1_fail_reasons"])

    def test_analyze_high_score_path_b_or_a(self, tmp_db, agent):
        # 注入完美数据：政策 funded、stable、gap=5、5+ V1 信号、Z safe + PEG cheap
        _seed_industry(
            tmp_db, gap_fillability=5, motivation_uncertainty="low",
        )
        _seed_stock(tmp_db, "600100")
        _seed_info_unit(
            tmp_db, uid="fund_pol", direction="supportive",
            content="设立专项资金 100 亿支持 AI算力 建设",
        )
        for i in range(6):
            _seed_info_unit(tmp_db, uid=f"v_{i}", source="V1")
        _seed_financials(tmp_db, "600100", z_score=3.0, peg_ratio=1.0)
        out = agent.analyze("600100")
        assert out["ok"]
        # 6 维度全 100 → 100 → +5 (gate) ≥ A
        assert out["level"] in (LEVEL_A, LEVEL_B)
        assert out["total_score"] >= 60


# ═══════════════════ 反方卡片 ═══════════════════


class TestCounterCard:
    def test_counter_card_includes_low_dim(self, tmp_db, agent):
        _seed_industry(tmp_db)
        _seed_stock(tmp_db, "600001")
        out = agent.generate_counter_card("600001")
        assert out["ok"]
        assert "counter_card" in out
        # d4 通常 ≤25（无 V1/V3/D4）
        cc = out["counter_card"]
        assert any("d4" in r for r in cc["risks"])

    def test_counter_card_invalid_symbol(self, agent):
        out = agent.generate_counter_card("")
        assert out["ok"] is False


# ═══════════════════ 持久化 ═══════════════════


class TestPersistence:
    def test_persist_writes_recommendations_row(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_stock(tmp_db, "600001")
        agent.analyze("600001")
        rows = tmp_db.query(
            "SELECT * FROM recommendations WHERE stock=?", ("600001",)
        )
        assert len(rows) == 1
        assert rows[0]["stock"] == "600001"
        assert rows[0]["recommend_level"] in (
            LEVEL_A, LEVEL_B, LEVEL_CANDIDATE, LEVEL_REJECT
        )
        # dimensions_detail 应是 valid JSON
        details = json.loads(rows[0]["dimensions_detail"])
        assert "dimensions" in details
        assert "phase1" in details
        assert "phase3" in details

    def test_thesis_hash_is_stable(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_stock(tmp_db, "600001")
        out1 = agent.analyze("600001")
        out2 = agent.analyze("600001")
        assert out1["thesis_hash"] == out2["thesis_hash"]


# ═══════════════════ load_universe / run 全量 ═══════════════════


class TestUniverseAndRun:
    def test_load_universe_filters_by_market_and_status(self, tmp_db, agent):
        _seed_industry(tmp_db)
        _seed_stock(tmp_db, "600001", market="A", status="active",
                    confidence="approved")
        _seed_stock(tmp_db, "600002", market="A", status="dormant",
                    confidence="approved")
        _seed_stock(tmp_db, "AAPL", market="US", status="active",
                    confidence="approved")
        _seed_stock(tmp_db, "600003", market="A", status="active",
                    confidence="staging")  # excluded
        universe = agent.load_universe()
        assert "600001" in universe
        assert "600002" not in universe
        assert "AAPL" not in universe
        assert "600003" not in universe

    def test_load_universe_excludes_non_6_digit(self, tmp_db, agent):
        _seed_industry(tmp_db)
        _seed_stock(tmp_db, "60000", market="A", confidence="approved")  # 5 digits
        universe = agent.load_universe()
        assert "60000" not in universe

    def test_run_batch_summary_structure(self, tmp_db, agent):
        _seed_industry(tmp_db, gap_fillability=4)
        _seed_stock(tmp_db, "600001")
        _seed_stock(tmp_db, "600002", industry_id=1, industry="AI算力")
        result = agent.run()
        assert result is not None
        assert result["processed"] == 2
        assert "levels" in result
        assert set(result["levels"].keys()) == {
            LEVEL_A, LEVEL_B, LEVEL_CANDIDATE, LEVEL_REJECT,
        }


# ═══════════════════ 综合积分计算 ═══════════════════


class TestScoring:
    def test_six_dim_default_score_around_46(self, agent):
        """无任何数据时所有维度走默认值，计算总分。

        d1=25*15 + d2=75*15 + d3=50*15 + d4=20*10 + d5=50*15 + d6=50*10
        = 375 + 1125 + 750 + 200 + 750 + 500 = 3700 / 80 = 46.25
        """
        out = agent.analyze("999999")
        assert out["raw_score"] == pytest.approx(46.25, abs=0.01)

    def test_score_clamped_to_100(self, tmp_db, agent):
        _seed_industry(
            tmp_db, gap_fillability=5, motivation_uncertainty="low",
        )
        _seed_stock(tmp_db, "600001")
        _seed_info_unit(
            tmp_db, uid="big1", direction="supportive",
            content="设立专项资金 100 亿支持 AI算力",
        )
        for i in range(10):
            _seed_info_unit(tmp_db, uid=f"vv_{i}", source="V1")
        _seed_financials(tmp_db, "600001", z_score=4.0, peg_ratio=1.0)
        out = agent.analyze("600001")
        assert out["total_score"] <= 100.0
