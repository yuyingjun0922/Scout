"""
tests/test_motivation_drift_agent.py — MotivationDriftAgent 测试矩阵 (v1.08)

覆盖：
  - 4 个信号检测函数（policy / keyword / financial / cross）
  - 聚合规则（reversing > drifting > stable + 计数）
  - 持久化（watchlist.motivation_drift + last_drift_at）
  - load_universe（zone='active' 过滤）
  - run() batch 汇总结构
  - get_status / detect 友好入口
  - 缺数据降级 → stable
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agents.motivation_drift_agent import (
    FINANCIAL_DRIFT_RATIO,
    LOOKBACK_7D,
    LOOKBACK_30D,
    MODERATE_NEG_THRESHOLD,
    RESTRICTIVE_DRIFT_THRESHOLD_30D,
    STATE_DRIFTING,
    STATE_REVERSING,
    STATE_STABLE,
    Z_DISTRESS,
    Z_SAFE,
    DriftDetection,
    MotivationDriftAgent,
    SignalResult,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "drift_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture
def agent(tmp_db):
    return MotivationDriftAgent(tmp_db)


def _utc(offset_days: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=offset_days)
    ).isoformat()


def _seed_watchlist(
    db: DatabaseManager,
    industry: str = "AI算力",
    industry_id: int = 1,
    zone: str = "active",
    motivation_drift: str = "stable",
):
    db.write(
        """INSERT INTO watchlist
           (industry_id, industry_name, zone, source_type, motivation_drift)
           VALUES (?, ?, ?, 'manual', ?)""",
        (industry_id, industry, zone, motivation_drift),
    )


def _seed_info(
    db: DatabaseManager,
    *,
    uid: str,
    industry: str = "AI算力",
    direction: str | None = None,
    source: str = "D1",
    content: str = "AI 行业政策",
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


def _seed_stock(
    db: DatabaseManager,
    stock_code: str,
    industry: str = "AI算力",
    industry_id: int = 1,
):
    db.write(
        """INSERT INTO related_stocks
           (industry_id, industry, stock_code, stock_name, market,
            discovery_source, discovered_at, confidence, status, updated_at)
           VALUES (?, ?, ?, ?, 'A', 'test', ?, 'approved', 'active', ?)""",
        (industry_id, industry, stock_code, f"NAME_{stock_code}",
         _utc(), _utc()),
    )


def _seed_financials(
    db: DatabaseManager,
    stock: str,
    z_score: float | None = 3.0,
    report_period: str = "2026-03-31",
):
    db.write(
        """INSERT INTO stock_financials
           (stock, report_period, z_score, peg_ratio, pe_ttm, updated_at)
           VALUES (?, ?, ?, 1.2, 18.0, ?)""",
        (stock, report_period, z_score, _utc()),
    )


# ═══════════════════ 工具 / 聚合 ═══════════════════


class TestAggregate:
    def test_all_stable(self):
        sigs = [SignalResult(name=n, state=STATE_STABLE, note="") for n in "abcd"]
        assert MotivationDriftAgent._aggregate(sigs) == STATE_STABLE

    def test_one_drifting(self):
        sigs = [
            SignalResult(name="a", state=STATE_DRIFTING, note=""),
            SignalResult(name="b", state=STATE_STABLE, note=""),
        ]
        assert MotivationDriftAgent._aggregate(sigs) == STATE_DRIFTING

    def test_two_drifting(self):
        sigs = [
            SignalResult(name="a", state=STATE_DRIFTING, note=""),
            SignalResult(name="b", state=STATE_DRIFTING, note=""),
            SignalResult(name="c", state=STATE_STABLE, note=""),
        ]
        assert MotivationDriftAgent._aggregate(sigs) == STATE_DRIFTING

    def test_one_reversing_overrides_all(self):
        sigs = [
            SignalResult(name="a", state=STATE_DRIFTING, note=""),
            SignalResult(name="b", state=STATE_DRIFTING, note=""),
            SignalResult(name="c", state=STATE_REVERSING, note=""),
            SignalResult(name="d", state=STATE_STABLE, note=""),
        ]
        assert MotivationDriftAgent._aggregate(sigs) == STATE_REVERSING

    def test_only_reversing(self):
        sigs = [SignalResult(name="a", state=STATE_REVERSING, note="")]
        assert MotivationDriftAgent._aggregate(sigs) == STATE_REVERSING


# ═══════════════════ 信号 A: 政策 ═══════════════════


class TestSignalPolicy:
    def test_no_data_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        sig = agent._signal_policy("AI算力")
        assert sig.state == STATE_STABLE
        assert sig.evidence["restrictive_30d"] == 0

    def test_one_restrictive_drifting(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="r1", direction="restrictive",
            content="限制扩张", days_ago=10,
        )
        sig = agent._signal_policy("AI算力")
        assert sig.state == STATE_DRIFTING
        assert sig.evidence["restrictive_30d"] == 1

    def test_two_restrictive_drifting(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        for i in range(RESTRICTIVE_DRIFT_THRESHOLD_30D):
            _seed_info(
                tmp_db, uid=f"r{i}", direction="restrictive",
                content="限制扩张", days_ago=10,
            )
        sig = agent._signal_policy("AI算力")
        assert sig.state == STATE_DRIFTING
        assert sig.evidence["restrictive_30d"] >= RESTRICTIVE_DRIFT_THRESHOLD_30D

    def test_funded_restrictive_within_7d_reversing(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="rf1", direction="restrictive",
            content="设立专项资金限制 AI 出口",
            days_ago=3,
        )
        sig = agent._signal_policy("AI算力")
        assert sig.state == STATE_REVERSING
        assert sig.evidence["restrictive_funded_7d"] == 1

    def test_funded_restrictive_outside_7d_only_drifting(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="rf2", direction="restrictive",
            content="设立专项资金限制 AI 出口",
            days_ago=15,  # 在 30d 内但出 7d
        )
        sig = agent._signal_policy("AI算力")
        assert sig.state == STATE_DRIFTING  # 1 条 restrictive → drifting
        assert sig.evidence["restrictive_funded_7d"] == 0

    def test_outside_30d_window_ignored(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="rold", direction="restrictive",
            content="禁止", days_ago=LOOKBACK_30D + 5,
        )
        sig = agent._signal_policy("AI算力")
        assert sig.state == STATE_STABLE


# ═══════════════════ 信号 B: 关键词 ═══════════════════


class TestSignalKeyword:
    def test_no_data_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        sig = agent._signal_keyword("AI算力")
        assert sig.state == STATE_STABLE

    def test_severe_within_7d_reversing(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="k1", source="S4",
            content="HBM 价格暴跌，多家厂商亏损", days_ago=2,
        )
        sig = agent._signal_keyword("AI算力")
        assert sig.state == STATE_REVERSING
        assert "暴跌" in sig.evidence["severe_keywords_7d"]

    def test_severe_outside_7d_no_reversing(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="k2", source="S4",
            content="腰斩", days_ago=15,
        )
        sig = agent._signal_keyword("AI算力")
        # severe 在 7d 外不计 reversing；moderate 阈值未达 → stable
        assert sig.state == STATE_STABLE

    def test_three_moderate_30d_drifting(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="k3", source="S4",
            content="行业产能过剩，开始降价，部分厂商退出",
            days_ago=10,
        )
        sig = agent._signal_keyword("AI算力")
        assert sig.state == STATE_DRIFTING
        assert sig.evidence["moderate_keyword_hits_30d"] >= MODERATE_NEG_THRESHOLD

    def test_two_moderate_below_threshold_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="k4", source="S4",
            content="略有降价但需求稳定，部分回调",
            days_ago=10,
        )
        sig = agent._signal_keyword("AI算力")
        # "降价" + "回调" = 2 < 3
        assert sig.evidence["moderate_keyword_hits_30d"] == 2
        assert sig.state == STATE_STABLE


# ═══════════════════ 信号 C: 财务 ═══════════════════


class TestSignalFinancial:
    def test_no_stocks_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        sig = agent._signal_financial("AI算力")
        assert sig.state == STATE_STABLE
        assert sig.evidence["sample_size"] == 0

    def test_one_distress_reversing(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_stock(tmp_db, "600001")
        _seed_financials(tmp_db, "600001", z_score=1.0)
        sig = agent._signal_financial("AI算力")
        assert sig.state == STATE_REVERSING
        assert sig.evidence["distress_count"] == 1

    def test_high_weak_ratio_drifting(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        # 4 只股票，3 只 z=2.0（< SAFE 但 > DISTRESS），1 只健康 → 75% weak
        for i, code in enumerate(["600001", "600002", "600003", "600004"]):
            _seed_stock(tmp_db, code)
            _seed_financials(tmp_db, code, z_score=2.0 if i < 3 else 3.0)
        sig = agent._signal_financial("AI算力")
        assert sig.state == STATE_DRIFTING
        assert sig.evidence["weak_count"] == 3

    def test_low_weak_ratio_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        for i, code in enumerate(["600001", "600002", "600003", "600004"]):
            _seed_stock(tmp_db, code)
            _seed_financials(tmp_db, code, z_score=3.0)
        sig = agent._signal_financial("AI算力")
        assert sig.state == STATE_STABLE
        assert sig.evidence["weak_count"] == 0

    def test_only_latest_period_used(self, tmp_db, agent):
        """同 stock 多个 report_period 应只看最新。"""
        _seed_watchlist(tmp_db)
        _seed_stock(tmp_db, "600001")
        _seed_financials(tmp_db, "600001", z_score=1.0, report_period="2025-12-31")
        _seed_financials(tmp_db, "600001", z_score=3.0, report_period="2026-03-31")
        sig = agent._signal_financial("AI算力")
        # 最新 z=3.0 → 不 distress
        assert sig.state == STATE_STABLE


# ═══════════════════ 信号 D: 冲突 ═══════════════════


class TestSignalCross:
    def test_no_data_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        sig = agent._signal_cross("AI算力")
        assert sig.state == STATE_STABLE

    def test_only_supportive_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(tmp_db, uid="c1", direction="supportive", days_ago=2)
        _seed_info(tmp_db, uid="c2", direction="supportive", days_ago=3)
        sig = agent._signal_cross("AI算力")
        assert sig.state == STATE_STABLE

    def test_both_within_7d_drifting(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(tmp_db, uid="c1", direction="supportive", days_ago=2)
        _seed_info(tmp_db, uid="c2", direction="restrictive", days_ago=3)
        sig = agent._signal_cross("AI算力")
        assert sig.state == STATE_DRIFTING
        assert sig.evidence["supportive_7d"] == 1
        assert sig.evidence["restrictive_7d"] == 1

    def test_outside_7d_ignored(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(tmp_db, uid="c1", direction="supportive", days_ago=2)
        _seed_info(tmp_db, uid="c2", direction="restrictive", days_ago=15)
        sig = agent._signal_cross("AI算力")
        assert sig.state == STATE_STABLE


# ═══════════════════ detect / 持久化 ═══════════════════


class TestDetectAndPersist:
    def test_detect_returns_dict_with_signals(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        out = agent.detect("AI算力")
        assert "state" in out
        assert "signals" in out
        assert len(out["signals"]) == 4
        names = {s["name"] for s in out["signals"]}
        assert names == {"policy", "keyword", "financial", "cross"}

    def test_detect_empty_industry_returns_error_dict(self, agent):
        out = agent.detect("")
        assert "error" in out

    def test_persist_updates_watchlist_stable(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        d = DriftDetection(
            industry="AI算力", state=STATE_STABLE,
            signals=[], detected_at=_utc(),
        )
        agent._persist(d)
        row = tmp_db.query_one(
            "SELECT motivation_drift, motivation_last_drift_at FROM watchlist WHERE industry_name = ?",
            ("AI算力",),
        )
        assert row["motivation_drift"] == STATE_STABLE
        # stable 不更新 last_drift_at
        assert row["motivation_last_drift_at"] is None

    def test_persist_updates_watchlist_drifting(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        d = DriftDetection(
            industry="AI算力", state=STATE_DRIFTING,
            signals=[], detected_at=_utc(),
        )
        agent._persist(d)
        row = tmp_db.query_one(
            "SELECT motivation_drift, motivation_last_drift_at FROM watchlist WHERE industry_name = ?",
            ("AI算力",),
        )
        assert row["motivation_drift"] == STATE_DRIFTING
        assert row["motivation_last_drift_at"] is not None

    def test_persist_updates_watchlist_reversing(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        d = DriftDetection(
            industry="AI算力", state=STATE_REVERSING,
            signals=[], detected_at=_utc(),
        )
        agent._persist(d)
        row = tmp_db.query_one(
            "SELECT motivation_drift, motivation_last_drift_at FROM watchlist WHERE industry_name = ?",
            ("AI算力",),
        )
        assert row["motivation_drift"] == STATE_REVERSING
        assert row["motivation_last_drift_at"] is not None


# ═══════════════════ get_status ═══════════════════


class TestGetStatus:
    def test_status_for_stable_industry(self, tmp_db, agent):
        _seed_watchlist(tmp_db, motivation_drift="stable")
        out = agent.get_status("AI算力")
        assert out["ok"]
        assert out["state"] == STATE_STABLE
        assert out["last_drift_at"] is None

    def test_status_for_unknown_industry(self, tmp_db, agent):
        out = agent.get_status("不存在的行业")
        assert out["ok"] is False
        assert "not in watchlist" in out["error"]

    def test_status_returns_drift_state(self, tmp_db, agent):
        _seed_watchlist(tmp_db, motivation_drift="reversing")
        out = agent.get_status("AI算力")
        assert out["state"] == STATE_REVERSING


# ═══════════════════ 宇宙 / batch ═══════════════════


class TestUniverseAndBatch:
    def test_load_universe_only_active(self, tmp_db, agent):
        _seed_watchlist(tmp_db, industry="AI算力", industry_id=1, zone="active")
        _seed_watchlist(tmp_db, industry="HBM", industry_id=2, zone="cold")
        _seed_watchlist(tmp_db, industry="储能", industry_id=3, zone="active")
        u = agent.load_universe()
        assert "AI算力" in u
        assert "储能" in u
        assert "HBM" not in u

    def test_run_batch_summary_structure(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        result = agent.run()
        assert result is not None
        assert set(result.keys()) >= {
            "processed", "stable", "drifting", "reversing", "ts_utc", "results",
        }
        assert result["processed"] == 1
        assert result["stable"] + result["drifting"] + result["reversing"] == 1

    def test_run_batch_persists_state(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        # 注入 reversing 触发数据
        _seed_info(
            tmp_db, uid="r1", direction="restrictive",
            content="设立专项资金限制 AI 出口", days_ago=3,
        )
        agent.run()
        row = tmp_db.query_one(
            "SELECT motivation_drift FROM watchlist WHERE industry_name = ?",
            ("AI算力",),
        )
        assert row["motivation_drift"] == STATE_REVERSING

    def test_run_with_multiple_industries(self, tmp_db, agent):
        _seed_watchlist(tmp_db, industry="AI算力", industry_id=1)
        _seed_watchlist(tmp_db, industry="HBM", industry_id=2)
        _seed_watchlist(tmp_db, industry="储能", industry_id=3)
        # AI 算力 → reversing
        _seed_info(
            tmp_db, uid="r1", industry="AI算力", direction="restrictive",
            content="设立专项资金限制", days_ago=3,
        )
        # HBM → reversing (severe keyword)
        _seed_info(
            tmp_db, uid="r2", industry="HBM",
            content="HBM 价格暴跌", days_ago=2,
        )
        # 储能 stays stable
        result = agent.run()
        assert result["processed"] == 3
        assert result["reversing"] >= 2
        states = {r["industry"]: r["state"] for r in result["results"]}
        assert states["AI算力"] == STATE_REVERSING
        assert states["HBM"] == STATE_REVERSING
        assert states["储能"] == STATE_STABLE


# ═══════════════════ 端到端 _detect_one ═══════════════════


class TestDetectOne:
    def test_detect_stable_path(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        d = agent._detect_one("AI算力")
        assert d.state == STATE_STABLE
        assert len(d.signals) == 4
        assert all(s.state == STATE_STABLE for s in d.signals)

    def test_detect_one_reversing_via_policy(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        _seed_info(
            tmp_db, uid="rf", direction="restrictive",
            content="设立专项资金限制扩张", days_ago=2,
        )
        d = agent._detect_one("AI算力")
        assert d.state == STATE_REVERSING

    def test_detect_one_drifting_via_two_signals(self, tmp_db, agent):
        _seed_watchlist(tmp_db)
        # signal A: 1 restrictive 30d → drifting
        _seed_info(
            tmp_db, uid="r1", direction="restrictive",
            content="限制项目落地", days_ago=10,
        )
        # signal D: 7d 内 sup + res → drifting
        _seed_info(
            tmp_db, uid="s1", direction="supportive",
            content="鼓励发展", days_ago=2,
        )
        _seed_info(
            tmp_db, uid="r2", direction="restrictive",
            content="补贴退坡", days_ago=3,
        )
        d = agent._detect_one("AI算力")
        # 多个 drifting 信号 → drifting (无 reversing)
        assert d.state in (STATE_DRIFTING, STATE_REVERSING)


# ═══════════════════ DataclassExports ═══════════════════


class TestDataclasses:
    def test_signal_result_to_dict(self):
        s = SignalResult(name="x", state=STATE_DRIFTING, note="n", evidence={"a": 1})
        out = s.to_dict()
        assert out == {"name": "x", "state": STATE_DRIFTING, "note": "n", "evidence": {"a": 1}}

    def test_drift_detection_to_dict(self):
        d = DriftDetection(
            industry="X", state=STATE_DRIFTING,
            signals=[
                SignalResult(name="a", state=STATE_DRIFTING, note=""),
                SignalResult(name="b", state=STATE_STABLE, note=""),
            ],
            detected_at="t",
        )
        out = d.to_dict()
        assert out["industry"] == "X"
        assert out["state"] == STATE_DRIFTING
        assert out["triggered"] == ["a"]
        assert len(out["signals"]) == 2
