"""
tests/test_financial_agent.py — FinancialAgent 测试矩阵

覆盖：
    - Z''-1995 公式正确性（happy + 缺字段 + 总资产=0 + 负债=0）
    - EPS CAGR（happy + 不足 4 点 + 起点≤0 + 终点≤0）
    - PEG（happy + PE 负 + CAGR 负 + 0 增长）
    - load_universe 过滤逻辑（market/confidence/status/length）
    - upsert_snapshot 幂等（同 stock+period 重写）
    - 单只失败不阻塞批次（DataMissing → 计入 failed）
    - run() 返回结构 + spot 缓存复位
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.base import DataMissingError
from agents.financial_agent import FinancialAgent, FinancialSnapshot
from infra.data_adapters.financials import FinancialsAdapter
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "fin_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture
def agent(tmp_db):
    """FinancialAgent with a stub adapter that does nothing by default."""
    stub_adapter = MagicMock(spec=FinancialsAdapter)
    return FinancialAgent(tmp_db, adapter=stub_adapter)


# ═══════════════════ Z''-1995 ═══════════════════

class TestZDoublePrime:
    def test_happy(self):
        """X1=0.3 X2=0.3 X3=0.05 X4=1.5 → 6.56*0.3+3.26*0.3+6.72*0.05+1.05*1.5
        = 1.968 + 0.978 + 0.336 + 1.575 = 4.857"""
        z = FinancialAgent.compute_z_double_prime({
            "total_assets": 100.0,
            "total_current_assets": 60.0,
            "total_current_liab": 30.0,
            "retained_earnings": 30.0,
            "ebit": 5.0,
            "total_equity": 60.0,
            "total_liabilities": 40.0,
        })
        assert z is not None
        assert abs(z - 4.857) < 0.01

    def test_distress_band(self):
        """全部低 → 应 < 1.10"""
        z = FinancialAgent.compute_z_double_prime({
            "total_assets": 100.0,
            "total_current_assets": 5.0,
            "total_current_liab": 50.0,
            "retained_earnings": -10.0,
            "ebit": -5.0,
            "total_equity": 5.0,
            "total_liabilities": 95.0,
        })
        assert z is not None
        assert z < 1.10

    def test_missing_total_assets(self):
        z = FinancialAgent.compute_z_double_prime({
            "total_assets": None, "total_current_assets": 60, "total_current_liab": 30,
            "retained_earnings": 25, "ebit": 5, "total_equity": 60, "total_liabilities": 40,
        })
        assert z is None

    def test_zero_total_assets(self):
        z = FinancialAgent.compute_z_double_prime({
            "total_assets": 0.0, "total_current_assets": 60, "total_current_liab": 30,
            "retained_earnings": 25, "ebit": 5, "total_equity": 60, "total_liabilities": 40,
        })
        assert z is None

    def test_zero_total_liabilities(self):
        z = FinancialAgent.compute_z_double_prime({
            "total_assets": 100, "total_current_assets": 60, "total_current_liab": 30,
            "retained_earnings": 25, "ebit": 5, "total_equity": 100, "total_liabilities": 0,
        })
        assert z is None

    def test_missing_retained_earnings(self):
        z = FinancialAgent.compute_z_double_prime({
            "total_assets": 100, "total_current_assets": 60, "total_current_liab": 30,
            "retained_earnings": None, "ebit": 5, "total_equity": 60, "total_liabilities": 40,
        })
        assert z is None


# ═══════════════════ EPS CAGR ═══════════════════

class TestEPSCagr:
    def test_happy_3y(self):
        """20→40 over 3 years: CAGR = (40/20)^(1/3) - 1 ≈ 0.2599"""
        cagr = FinancialAgent.compute_eps_cagr_3y([
            {"period": "2025-12-31", "eps": 40.0},
            {"period": "2024-12-31", "eps": 35.0},
            {"period": "2023-12-31", "eps": 28.0},
            {"period": "2022-12-31", "eps": 20.0},
        ])
        assert cagr is not None
        assert abs(cagr - 0.2599) < 0.001

    def test_too_few_points(self):
        cagr = FinancialAgent.compute_eps_cagr_3y([
            {"period": "2025-12-31", "eps": 40.0},
            {"period": "2024-12-31", "eps": 30.0},
        ])
        assert cagr is None

    def test_zero_starting_eps(self):
        cagr = FinancialAgent.compute_eps_cagr_3y([
            {"period": "2025", "eps": 40.0},
            {"period": "2024", "eps": 30.0},
            {"period": "2023", "eps": 20.0},
            {"period": "2022", "eps": 0.0},
        ])
        assert cagr is None

    def test_negative_starting_eps(self):
        cagr = FinancialAgent.compute_eps_cagr_3y([
            {"period": "2025", "eps": 40.0},
            {"period": "2024", "eps": 30.0},
            {"period": "2023", "eps": 20.0},
            {"period": "2022", "eps": -5.0},
        ])
        assert cagr is None

    def test_negative_latest_eps(self):
        cagr = FinancialAgent.compute_eps_cagr_3y([
            {"period": "2025", "eps": -1.0},
            {"period": "2024", "eps": 30.0},
            {"period": "2023", "eps": 20.0},
            {"period": "2022", "eps": 10.0},
        ])
        assert cagr is None

    def test_empty(self):
        assert FinancialAgent.compute_eps_cagr_3y([]) is None
        assert FinancialAgent.compute_eps_cagr_3y(None) is None


# ═══════════════════ PEG ═══════════════════

class TestPEG:
    def test_happy(self):
        # PE=20, CAGR=0.20 → PEG = 20 / 20 = 1.0
        peg = FinancialAgent.compute_peg(20.0, 0.20)
        assert peg == 1.0

    def test_cheap(self):
        # PE=15, CAGR=0.30 → PEG = 15 / 30 = 0.5
        peg = FinancialAgent.compute_peg(15.0, 0.30)
        assert peg == 0.5

    def test_pe_negative_returns_none(self):
        assert FinancialAgent.compute_peg(-10.0, 0.20) is None

    def test_pe_zero_returns_none(self):
        assert FinancialAgent.compute_peg(0.0, 0.20) is None

    def test_cagr_negative_returns_none(self):
        assert FinancialAgent.compute_peg(20.0, -0.10) is None

    def test_cagr_zero_returns_none(self):
        assert FinancialAgent.compute_peg(20.0, 0.0) is None

    def test_either_none_returns_none(self):
        assert FinancialAgent.compute_peg(None, 0.20) is None
        assert FinancialAgent.compute_peg(20.0, None) is None


# ═══════════════════ load_universe ═══════════════════

class TestLoadUniverse:
    def _seed(self, db, rows):
        for r in rows:
            db.write(
                """INSERT INTO related_stocks
                   (industry, stock_code, market, confidence, status)
                   VALUES (?, ?, ?, ?, ?)""",
                r,
            )

    def test_filters_market_a(self, agent, tmp_db):
        self._seed(tmp_db, [
            ("半导体", "600519", "A", "confirmed", "active"),
            ("半导体", "005930", "KR", "confirmed", "active"),
            ("半导体", "AAPL", "US", "confirmed", "active"),
        ])
        universe = agent.load_universe()
        assert universe == ["600519"]

    def test_excludes_staging(self, agent, tmp_db):
        self._seed(tmp_db, [
            ("半导体", "600519", "A", "confirmed", "active"),
            ("半导体", "300750", "A", "staging", "active"),
        ])
        universe = agent.load_universe()
        assert "600519" in universe
        assert "300750" not in universe

    def test_excludes_inactive(self, agent, tmp_db):
        self._seed(tmp_db, [
            ("半导体", "600519", "A", "confirmed", "dormant"),
            ("半导体", "300750", "A", "confirmed", "active"),
        ])
        universe = agent.load_universe()
        assert universe == ["300750"]

    def test_excludes_invalid_codes(self, agent, tmp_db):
        self._seed(tmp_db, [
            ("半导体", "600519", "A", "confirmed", "active"),
            ("半导体", "12345", "A", "confirmed", "active"),  # 5 位
            ("半导体", "1234567", "A", "confirmed", "active"),  # 7 位
        ])
        universe = agent.load_universe()
        assert universe == ["600519"]

    def test_dedup(self, agent, tmp_db):
        self._seed(tmp_db, [
            ("半导体", "600519", "A", "confirmed", "active"),
            ("白酒", "600519", "A", "confirmed", "active"),
        ])
        universe = agent.load_universe()
        assert universe == ["600519"]


# ═══════════════════ upsert ═══════════════════

class TestUpsert:
    def test_insert_then_replace(self, agent, tmp_db):
        snap1 = FinancialSnapshot(
            stock="600519", report_period="2024-12-31",
            revenue=100.0, net_profit=20.0, z_score=3.5,
            pe_ttm=25.0, eps_cagr_3y=0.20, peg_ratio=1.25,
        )
        agent.upsert_snapshot(snap1)
        rows = tmp_db.query(
            "SELECT z_score FROM stock_financials WHERE stock = ?", ("600519",)
        )
        assert len(rows) == 1
        assert rows[0]["z_score"] == 3.5

        # 同 period 再 upsert → 替换不重复
        snap2 = FinancialSnapshot(
            stock="600519", report_period="2024-12-31",
            revenue=110.0, net_profit=22.0, z_score=4.0,
            pe_ttm=26.0, eps_cagr_3y=0.22, peg_ratio=1.18,
        )
        agent.upsert_snapshot(snap2)
        rows = tmp_db.query(
            "SELECT z_score, peg_ratio FROM stock_financials WHERE stock = ?",
            ("600519",),
        )
        assert len(rows) == 1
        assert rows[0]["z_score"] == 4.0
        assert rows[0]["peg_ratio"] == 1.18

    def test_different_periods_coexist(self, agent, tmp_db):
        snap_old = FinancialSnapshot(
            stock="600519", report_period="2023-12-31", z_score=3.0
        )
        snap_new = FinancialSnapshot(
            stock="600519", report_period="2024-12-31", z_score=4.0
        )
        agent.upsert_snapshot(snap_old)
        agent.upsert_snapshot(snap_new)
        rows = tmp_db.query(
            "SELECT report_period, z_score FROM stock_financials "
            "WHERE stock = ? ORDER BY report_period",
            ("600519",),
        )
        assert len(rows) == 2


# ═══════════════════ batch run ═══════════════════

class TestRunBatch:
    def test_one_failure_does_not_block_others(self, tmp_db):
        # seed universe
        for code in ("600519", "300750", "000858"):
            tmp_db.write(
                """INSERT INTO related_stocks
                   (industry, stock_code, market, confidence, status)
                   VALUES (?, ?, ?, ?, ?)""",
                ("test", code, "A", "confirmed", "active"),
            )

        adapter = MagicMock(spec=FinancialsAdapter)

        def side(code):
            if code == "300750":
                raise DataMissingError("simulated")
            return {
                "stock": code,
                "report_period": "2024-12-31",
                "total_assets": 100.0,
                "total_current_assets": 60.0,
                "total_current_liab": 30.0,
                "retained_earnings": 25.0,
                "ebit": 5.0,
                "total_equity": 60.0,
                "total_liabilities": 40.0,
                "revenue": 1000.0,
                "net_profit": 200.0,
                "pe_ttm": 25.0,
                "eps_history": [
                    {"period": "2024-12-31", "eps": 40.0},
                    {"period": "2023-12-31", "eps": 35.0},
                    {"period": "2022-12-31", "eps": 28.0},
                    {"period": "2021-12-31", "eps": 20.0},
                ],
            }

        adapter.fetch_snapshot.side_effect = side
        agent = FinancialAgent(tmp_db, adapter=adapter)
        result = agent.run()

        assert result is not None
        assert result["processed"] == 3
        assert result["succeeded"] == 2
        assert result["failed"] == 1

        rows = tmp_db.query("SELECT stock FROM stock_financials")
        stocks = sorted(r["stock"] for r in rows)
        assert stocks == ["000858", "600519"]

    def test_empty_universe(self, tmp_db):
        adapter = MagicMock(spec=FinancialsAdapter)
        agent = FinancialAgent(tmp_db, adapter=adapter)
        result = agent.run()
        assert result["processed"] == 0
        assert result["succeeded"] == 0
        assert result["failed"] == 0
        adapter.fetch_snapshot.assert_not_called()

    def test_resets_spot_cache_after_run(self, tmp_db, monkeypatch):
        # 加一只股票避免 universe 空时跳过 reset
        tmp_db.write(
            """INSERT INTO related_stocks
               (industry, stock_code, market, confidence, status)
               VALUES (?, ?, ?, ?, ?)""",
            ("test", "600519", "A", "confirmed", "active"),
        )
        adapter = MagicMock(spec=FinancialsAdapter)
        adapter.fetch_snapshot.return_value = {
            "stock": "600519", "report_period": "2024-12-31",
            "total_assets": None,  # 触发 z=None 但不会 fail
            "revenue": 100.0, "net_profit": 20.0,
            "pe_ttm": None, "eps_history": [],
        }
        agent = FinancialAgent(tmp_db, adapter=adapter)

        called = {"n": 0}
        original = FinancialsAdapter.reset_spot_cache

        def tracked():
            called["n"] += 1
            original()

        monkeypatch.setattr(FinancialsAdapter, "reset_spot_cache", tracked)
        agent.run()
        assert called["n"] >= 1
