"""
tests/test_master_agent.py — MasterAgent 测试矩阵（v1.03）

覆盖：
  - 5 大师各 2-3 个评分测试（happy/缺数据/边界）
  - analyze_stock 集成（写入 master_analysis 5 行 + 文字报告）
  - load_universe 过滤逻辑
  - stock_financials 无该股 → 数据不足降级
  - 工具函数 _band / _safe_ratio
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agents.master_agent import (
    MASTER_NAMES,
    MasterAgent,
    _band,
    _bucket_label,
    _safe_ratio,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "master_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture
def agent(tmp_db):
    return MasterAgent(tmp_db)


def _seed_financials(db: DatabaseManager, **overrides) -> str:
    """插入一行 stock_financials；返回 stock 代码。"""
    defaults = dict(
        stock="600519",
        report_period="2025-12-31",
        revenue=1.0e10,         # 100 亿
        net_profit=2.5e9,       # 25 亿 → 净利率 25%
        z_score=4.5,
        pe_ttm=18.0,
        eps_cagr_3y=0.18,       # 18% 增长
        peg_ratio=1.0,          # 18/18
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    defaults.update(overrides)
    db.write(
        """INSERT INTO stock_financials
           (stock, report_period, revenue, net_profit,
            z_score, pe_ttm, eps_cagr_3y, peg_ratio, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        tuple(defaults[k] for k in (
            "stock", "report_period", "revenue", "net_profit",
            "z_score", "pe_ttm", "eps_cagr_3y", "peg_ratio", "updated_at",
        )),
    )
    return defaults["stock"]


# ═══════════════════ 工具函数 ═══════════════════


class TestHelpers:
    def test_safe_ratio_happy(self):
        assert _safe_ratio(10, 4) == 2.5

    def test_safe_ratio_zero_denominator(self):
        assert _safe_ratio(10, 0) is None

    def test_safe_ratio_none(self):
        assert _safe_ratio(None, 4) is None
        assert _safe_ratio(10, None) is None

    def test_band_falls_in_segment(self):
        bands = [
            ((None, 10), 0, "low"),
            ((10, 20), 50, "mid"),
            ((20, None), 100, "high"),
        ]
        assert _band(5, bands) == (0, "low")
        assert _band(15, bands) == (50, "mid")
        assert _band(25, bands) == (100, "high")

    def test_band_inclusive_low_exclusive_high(self):
        bands = [((None, 10), 0, "low"), ((10, 20), 50, "mid")]
        assert _band(10, bands) == (50, "mid")  # boundary 落 mid
        assert _band(9.99, bands) == (0, "low")

    def test_band_none_value(self):
        bands = [((None, 10), 0, "low")]
        assert _band(None, bands, none_score=42, none_note="缺") == (42, "缺")

    def test_bucket_label_thresholds(self):
        assert "强烈推荐" in _bucket_label(80)
        assert "值得关注" in _bucket_label(60)
        assert "有保留" in _bucket_label(40)
        assert "不建议" in _bucket_label(0)


# ═══════════════════ 巴菲特 ═══════════════════


class TestBuffett:
    def test_happy(self):
        """PE=18, CAGR=18%, 净利率 25% → 30+30+30 = 90"""
        fin = {"pe_ttm": 18.0, "eps_cagr_3y": 0.18,
               "revenue": 100.0, "net_profit": 25.0}
        r = MasterAgent._score_buffett(fin)
        assert r.master == "buffett"
        assert r.score == 90
        assert "强烈推荐" in r.verdict
        # 护城河子项始终为 None（需主观判断）
        assert r.details["moat"]["score"] is None

    def test_high_pe_low_growth(self):
        """PE=50（>40 → 0）, CAGR=2%（→10）, 净利率 3%（→5） = 15"""
        fin = {"pe_ttm": 50.0, "eps_cagr_3y": 0.02,
               "revenue": 100.0, "net_profit": 3.0}
        r = MasterAgent._score_buffett(fin)
        assert r.score == 15

    def test_all_none(self):
        """全部数据缺失 → 0 分 + 各子项标"数据不足"。"""
        fin = {"pe_ttm": None, "eps_cagr_3y": None,
               "revenue": None, "net_profit": None}
        r = MasterAgent._score_buffett(fin)
        assert r.score == 0
        assert "数据不足" in r.details["safety_margin"]["note"]


# ═══════════════════ 芒格 ═══════════════════


class TestMunger:
    def test_pass(self):
        """Z=3.0, CAGR=10%, 净利润正 → 通过。"""
        fin = {"z_score": 3.0, "eps_cagr_3y": 0.10,
               "revenue": 100.0, "net_profit": 10.0}
        r = MasterAgent._score_munger(fin)
        assert r.score is None  # 芒格不打分
        assert r.details["passed"] is True
        assert "通过" in r.verdict

    def test_fail_distress(self):
        """Z<1.10 → 排除。"""
        fin = {"z_score": 0.8, "eps_cagr_3y": 0.10,
               "revenue": 100.0, "net_profit": 10.0}
        r = MasterAgent._score_munger(fin)
        assert r.details["passed"] is False
        assert any("困境" in x for x in r.details["fail_reasons"])

    def test_fail_loss(self):
        """净利率为负 → 排除。"""
        fin = {"z_score": 3.0, "eps_cagr_3y": 0.05,
               "revenue": 100.0, "net_profit": -5.0}
        r = MasterAgent._score_munger(fin)
        assert r.details["passed"] is False
        assert any("亏损" in x for x in r.details["fail_reasons"])

    def test_fail_severe_decline(self):
        """CAGR < -10% → 排除。"""
        fin = {"z_score": 3.0, "eps_cagr_3y": -0.20,
               "revenue": 100.0, "net_profit": 5.0}
        r = MasterAgent._score_munger(fin)
        assert r.details["passed"] is False
        assert any("衰退" in x for x in r.details["fail_reasons"])

    def test_fail_no_data(self):
        """Z 与 CAGR 都缺失 → 排除（信息不足）。"""
        fin = {"z_score": None, "eps_cagr_3y": None,
               "revenue": None, "net_profit": None}
        r = MasterAgent._score_munger(fin)
        assert r.details["passed"] is False
        assert any("信息不足" in x for x in r.details["fail_reasons"])


# ═══════════════════ 段永平 ═══════════════════


class TestDuan:
    def test_happy(self):
        """净利率 30%（→50）, CAGR 18%（→30）, Z=3.0（→20） = 100"""
        fin = {"revenue": 100.0, "net_profit": 30.0,
               "eps_cagr_3y": 0.18, "z_score": 3.0}
        r = MasterAgent._score_duan(fin)
        assert r.score == 100
        # FCF 子项必为 None + 标 "数据不足"
        assert r.details["free_cash_flow"]["score"] is None
        assert "数据不足" in r.details["free_cash_flow"]["note"]

    def test_low_margin(self):
        """净利率 5%（→0）, 其他普通"""
        fin = {"revenue": 100.0, "net_profit": 5.0,
               "eps_cagr_3y": 0.10, "z_score": 2.0}
        r = MasterAgent._score_duan(fin)
        # 0 + 20 + 10 = 30
        assert r.score == 30

    def test_all_none(self):
        fin = {"revenue": None, "net_profit": None,
               "eps_cagr_3y": None, "z_score": None}
        r = MasterAgent._score_duan(fin)
        assert r.score == 0


# ═══════════════════ 林奇 ═══════════════════


class TestLynch:
    def test_happy_low_peg_high_growth(self):
        """PEG=0.4（→60）, CAGR 30%（→40） = 100"""
        fin = {"peg_ratio": 0.4, "eps_cagr_3y": 0.30}
        r = MasterAgent._score_lynch(fin)
        assert r.score == 100

    def test_high_peg(self):
        """PEG=2.5（→0）, CAGR 10%（→20） = 20"""
        fin = {"peg_ratio": 2.5, "eps_cagr_3y": 0.10}
        r = MasterAgent._score_lynch(fin)
        assert r.score == 20

    def test_negative_growth_excluded(self):
        """CAGR<0 → 林奇 growth=0 + PEG None → 0"""
        fin = {"peg_ratio": None, "eps_cagr_3y": -0.05}
        r = MasterAgent._score_lynch(fin)
        assert r.score == 0
        assert "数据不足" in r.details["peg"]["note"]

    def test_missing_peg(self):
        """PEG None + CAGR 18% → 0+30 = 30"""
        fin = {"peg_ratio": None, "eps_cagr_3y": 0.18}
        r = MasterAgent._score_lynch(fin)
        assert r.score == 30


# ═══════════════════ 费雪 ═══════════════════


class TestFisher:
    def test_happy(self):
        """CAGR 25%（→60）, 净利率 25%（→40） = 100"""
        fin = {"eps_cagr_3y": 0.25, "revenue": 100.0, "net_profit": 25.0}
        r = MasterAgent._score_fisher(fin)
        assert r.score == 100
        assert r.details["rd_intensity"]["score"] is None

    def test_low_growth(self):
        """CAGR 3%（→0）, 净利率 12%（→25） = 25"""
        fin = {"eps_cagr_3y": 0.03, "revenue": 100.0, "net_profit": 12.0}
        r = MasterAgent._score_fisher(fin)
        assert r.score == 25

    def test_missing_growth(self):
        """CAGR 缺 → 0 + 净利率 25% → 40"""
        fin = {"eps_cagr_3y": None, "revenue": 100.0, "net_profit": 25.0}
        r = MasterAgent._score_fisher(fin)
        assert r.score == 40
        assert "数据不足" in r.details["long_term_growth"]["note"]


# ═══════════════════ analyze_stock 集成 ═══════════════════


class TestAnalyzeStock:
    def test_full_analysis_writes_5_rows(self, agent, tmp_db):
        symbol = _seed_financials(tmp_db)
        out = agent.analyze_stock(symbol)
        assert out["ok"] is True
        assert out["stock"] == symbol
        assert out["report_period"] == "2025-12-31"
        assert len(out["results"]) == 5
        masters = {r["master"] for r in out["results"]}
        assert masters == set(MASTER_NAMES)
        # 报告含中文 + 5 大师标签
        assert "巴菲特" in out["report"]
        assert "芒格" in out["report"]
        assert "段永平" in out["report"]
        # DB 落 5 行
        rows = tmp_db.query(
            "SELECT * FROM master_analysis WHERE stock=?", (symbol,)
        )
        assert len(rows) == 5

    def test_analyze_persists_history_appends(self, agent, tmp_db):
        """连续两次分析 → master_analysis 累积 10 行。"""
        symbol = _seed_financials(tmp_db)
        agent.analyze_stock(symbol)
        agent.analyze_stock(symbol)
        rows = tmp_db.query(
            "SELECT COUNT(*) AS c FROM master_analysis WHERE stock=?", (symbol,)
        )
        assert rows[0]["c"] == 10

    def test_no_financials_returns_data_insufficient(self, agent):
        """stock_financials 没有该股 → ok=False + 提示信息。"""
        out = agent.analyze_stock("999999")
        assert out["ok"] is False
        assert "financial_agent" in out["error"]
        assert out["results"] == []
        # 报告仍是字符串（友好降级）
        assert "999999" in out["report"]

    def test_invalid_symbol_returns_failure(self, agent):
        """空 symbol → ok=False。"""
        out = agent.analyze_stock("")
        assert out["ok"] is False
        assert "symbol" in out["error"]

    def test_buffett_score_makes_sense(self, agent, tmp_db):
        """种子数据 → 巴菲特 90 分（PE 18 = 30, CAGR 18% = 30, 净利率 25% = 30）。"""
        symbol = _seed_financials(tmp_db)
        out = agent.analyze_stock(symbol)
        buffett = next(r for r in out["results"] if r["master"] == "buffett")
        assert buffett["score"] == 90

    def test_munger_passes_on_seed(self, agent, tmp_db):
        symbol = _seed_financials(tmp_db)
        out = agent.analyze_stock(symbol)
        munger = next(r for r in out["results"] if r["master"] == "munger")
        assert munger["score"] is None
        assert munger["details"]["passed"] is True

    def test_details_serializable(self, agent, tmp_db):
        """落库的 details 应可 JSON parse。"""
        symbol = _seed_financials(tmp_db)
        agent.analyze_stock(symbol)
        rows = tmp_db.query(
            "SELECT details FROM master_analysis WHERE stock=?", (symbol,)
        )
        for row in rows:
            parsed = json.loads(row["details"])
            assert isinstance(parsed, dict)


# ═══════════════════ load_universe ═══════════════════


class TestLoadUniverse:
    def test_filters_correctly(self, agent, tmp_db):
        """A 股 + 非 staging + active + 6 位代码 才入选。"""
        rows = [
            # 入选
            ("600519", "A", "approved", "active"),
            ("000858", "A", "promoted", "active"),
            # 排除：staging
            ("000001", "A", "staging", "active"),
            # 排除：market
            ("005930", "KR", "approved", "active"),
            # 排除：status
            ("600036", "A", "approved", "dormant"),
            # 排除：长度
            ("123", "A", "approved", "active"),
        ]
        for code, market, conf, status in rows:
            tmp_db.write(
                """INSERT INTO related_stocks
                   (stock_code, market, confidence, status, industry)
                   VALUES (?, ?, ?, ?, 'X')""",
                (code, market, conf, status),
            )
        result = agent.load_universe()
        assert sorted(result) == ["000858", "600519"]


# ═══════════════════ run() 入口（错误包装）═══════════════════


class TestRunEntry:
    def test_run_returns_result_for_seeded_symbol(self, agent, tmp_db):
        symbol = _seed_financials(tmp_db)
        out = agent.run(symbol)
        assert out is not None
        assert out["ok"] is True

    def test_run_returns_none_when_data_missing(self, agent):
        """run() 经过 BaseAgent 错误处理：DataMissingError → None。"""
        out = agent.run("999999")
        assert out is None
