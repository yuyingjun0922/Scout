"""
tests/test_financials_adapter.py — FinancialsAdapter 单测

覆盖：
    - _to_em_symbol 前缀映射（SH600519 / SZ000858 / 非法报错）
    - _to_float NaN/None/空串兜底
    - _period_str 日期截断
    - fetch_balance_sheet 空 DataFrame → DataMissingError
    - fetch_balance_sheet 网络错 → NetworkError
    - fetch_balance_sheet ParseError 路径
    - fetch_pe_ttm 缓存复用
    - reset_spot_cache
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from agents.base import DataMissingError, NetworkError, ParseError
from infra.data_adapters.financials import (
    FinancialsAdapter,
    _to_em_symbol,
    _to_float,
)


class TestHelpers:
    def test_em_symbol_sh(self):
        assert _to_em_symbol("600519") == "SH600519"

    def test_em_symbol_sz(self):
        assert _to_em_symbol("000858") == "SZ000858"
        assert _to_em_symbol("300750") == "SZ300750"

    def test_em_symbol_invalid_short(self):
        with pytest.raises(ValueError):
            _to_em_symbol("12345")

    def test_em_symbol_invalid_nondigit(self):
        with pytest.raises(ValueError):
            _to_em_symbol("60051A")

    def test_to_float_basic(self):
        assert _to_float("1.5") == 1.5
        assert _to_float(2) == 2.0

    def test_to_float_none_default(self):
        assert _to_float(None) is None
        assert _to_float(None, default=0.0) == 0.0

    def test_to_float_nan_default(self):
        assert _to_float(float("nan")) is None
        assert _to_float(float("nan"), default=0.0) == 0.0

    def test_to_float_empty_str(self):
        assert _to_float("") is None
        assert _to_float("xx") is None

    def test_period_str(self):
        assert FinancialsAdapter._period_str("2025-12-31 00:00:00") == "2025-12-31"
        assert FinancialsAdapter._period_str(None) is None


@pytest.fixture(autouse=True)
def reset_spot():
    """每个测试前后重置 spot 缓存"""
    FinancialsAdapter.reset_spot_cache()
    yield
    FinancialsAdapter.reset_spot_cache()


@pytest.fixture
def adapter():
    a = FinancialsAdapter()
    # 强制 rate_limit 不真睡
    a.MIN_INTERVAL_SECONDS = 0
    return a


class TestBalanceSheet:
    def test_empty_dataframe_raises_data_missing(self, adapter):
        with patch("akshare.stock_balance_sheet_by_yearly_em", return_value=pd.DataFrame()):
            with pytest.raises(DataMissingError):
                adapter.fetch_balance_sheet("600519")

    def test_network_error_raises_network(self, adapter):
        with patch("akshare.stock_balance_sheet_by_yearly_em",
                   side_effect=ConnectionError("dns")):
            with pytest.raises(NetworkError):
                adapter.fetch_balance_sheet("600519")

    def test_timeout_msg_remapped_to_network(self, adapter):
        with patch("akshare.stock_balance_sheet_by_yearly_em",
                   side_effect=Exception("read timeout occurred")):
            with pytest.raises(NetworkError):
                adapter.fetch_balance_sheet("600519")

    def test_parse_error_path(self, adapter):
        with patch("akshare.stock_balance_sheet_by_yearly_em",
                   side_effect=ValueError("bad json")):
            with pytest.raises(ParseError):
                adapter.fetch_balance_sheet("600519")

    def test_happy_path_extracts_fields(self, adapter):
        df = pd.DataFrame([{
            "REPORT_DATE": "2025-12-31 00:00:00",
            "TOTAL_ASSETS": 100.0,
            "TOTAL_CURRENT_ASSETS": 60.0,
            "TOTAL_CURRENT_LIAB": 30.0,
            "TOTAL_LIABILITIES": 40.0,
            "TOTAL_EQUITY": 60.0,
            "UNASSIGN_RPOFIT": 25.0,
            "SURPLUS_RESERVE": 5.0,
        }])
        with patch("akshare.stock_balance_sheet_by_yearly_em", return_value=df):
            bs = adapter.fetch_balance_sheet("600519")
        assert bs["report_period"] == "2025-12-31"
        assert bs["total_assets"] == 100.0
        assert bs["total_current_assets"] == 60.0
        assert bs["total_current_liab"] == 30.0
        assert bs["total_liabilities"] == 40.0
        assert bs["total_equity"] == 60.0
        assert bs["retained_earnings"] == 30.0  # 25 + 5


class TestIncomeStatement:
    def test_empty_raises_data_missing(self, adapter):
        with patch("akshare.stock_profit_sheet_by_yearly_em", return_value=pd.DataFrame()):
            with pytest.raises(DataMissingError):
                adapter.fetch_income_statement("600519")

    def test_ebit_calculation(self, adapter):
        df = pd.DataFrame([{
            "REPORT_DATE": "2025-12-31 00:00:00",
            "TOTAL_OPERATE_INCOME": 1000.0,
            "PARENT_NETPROFIT": 200.0,
            "TOTAL_PROFIT": 250.0,
            "FINANCE_EXPENSE": 30.0,
        }])
        with patch("akshare.stock_profit_sheet_by_yearly_em", return_value=df):
            is_ = adapter.fetch_income_statement("600519")
        assert is_["revenue"] == 1000.0
        assert is_["net_profit"] == 200.0
        assert is_["ebit"] == 280.0  # 250 + 30

    def test_ebit_finance_expense_missing_treated_as_zero(self, adapter):
        df = pd.DataFrame([{
            "REPORT_DATE": "2025-12-31 00:00:00",
            "TOTAL_OPERATE_INCOME": 500.0,
            "PARENT_NETPROFIT": 100.0,
            "TOTAL_PROFIT": 120.0,
            "FINANCE_EXPENSE": None,
        }])
        with patch("akshare.stock_profit_sheet_by_yearly_em", return_value=df):
            is_ = adapter.fetch_income_statement("600519")
        assert is_["ebit"] == 120.0

    def test_ebit_total_profit_missing_returns_none(self, adapter):
        df = pd.DataFrame([{
            "REPORT_DATE": "2025-12-31 00:00:00",
            "TOTAL_OPERATE_INCOME": 500.0,
            "PARENT_NETPROFIT": 100.0,
            "TOTAL_PROFIT": None,
            "FINANCE_EXPENSE": 30.0,
        }])
        with patch("akshare.stock_profit_sheet_by_yearly_em", return_value=df):
            is_ = adapter.fetch_income_statement("600519")
        assert is_["ebit"] is None


class TestPETTM:
    def test_caches_spot_table(self, adapter):
        df = pd.DataFrame([
            {"代码": "600519", "市盈率-动态": 21.5},
            {"代码": "300750", "市盈率-动态": 35.2},
        ])
        with patch("akshare.stock_zh_a_spot_em", return_value=df) as mock:
            pe1 = adapter.fetch_pe_ttm("600519")
            pe2 = adapter.fetch_pe_ttm("300750")
            assert pe1 == 21.5
            assert pe2 == 35.2
            assert mock.call_count == 1  # 缓存复用

    def test_unknown_code_returns_none(self, adapter):
        df = pd.DataFrame([{"代码": "600519", "市盈率-动态": 21.5}])
        with patch("akshare.stock_zh_a_spot_em", return_value=df):
            assert adapter.fetch_pe_ttm("999999") is None

    def test_negative_pe_passes_through(self, adapter):
        # 适配器只负责取值，不在此处过滤负值
        df = pd.DataFrame([{"代码": "600519", "市盈率-动态": -8.0}])
        with patch("akshare.stock_zh_a_spot_em", return_value=df):
            assert adapter.fetch_pe_ttm("600519") == -8.0

    def test_network_error_on_spot(self, adapter):
        with patch("akshare.stock_zh_a_spot_em",
                   side_effect=ConnectionError("net")):
            with pytest.raises(NetworkError):
                adapter.fetch_pe_ttm("600519")

    def test_reset_cache(self, adapter):
        df1 = pd.DataFrame([{"代码": "600519", "市盈率-动态": 10.0}])
        df2 = pd.DataFrame([{"代码": "600519", "市盈率-动态": 20.0}])
        with patch("akshare.stock_zh_a_spot_em", side_effect=[df1, df2]):
            assert adapter.fetch_pe_ttm("600519") == 10.0
            FinancialsAdapter.reset_spot_cache()
            assert adapter.fetch_pe_ttm("600519") == 20.0


class TestEPSHistory:
    def test_filters_to_annual_only(self, adapter):
        df = pd.DataFrame([
            {"日期": "2025-12-31", "摊薄每股收益(元)": 50.0},
            {"日期": "2025-09-30", "摊薄每股收益(元)": 40.0},  # 季报，丢
            {"日期": "2024-12-31", "摊薄每股收益(元)": 45.0},
            {"日期": "2023-12-31", "摊薄每股收益(元)": 35.0},
        ])
        with patch("akshare.stock_financial_analysis_indicator", return_value=df):
            hist = adapter.fetch_eps_history("600519", years=4)
        periods = [h["period"] for h in hist]
        assert "2025-12-31" in periods
        assert "2024-12-31" in periods
        assert "2023-12-31" in periods
        assert "2025-09-30" not in periods

    def test_sorted_desc(self, adapter):
        df = pd.DataFrame([
            {"日期": "2023-12-31", "摊薄每股收益(元)": 30.0},
            {"日期": "2025-12-31", "摊薄每股收益(元)": 50.0},
            {"日期": "2024-12-31", "摊薄每股收益(元)": 40.0},
        ])
        with patch("akshare.stock_financial_analysis_indicator", return_value=df):
            hist = adapter.fetch_eps_history("600519")
        assert [h["period"] for h in hist] == ["2025-12-31", "2024-12-31", "2023-12-31"]

    def test_empty_returns_empty_list(self, adapter):
        with patch("akshare.stock_financial_analysis_indicator",
                   return_value=pd.DataFrame()):
            assert adapter.fetch_eps_history("600519") == []

    def test_network_error_propagates(self, adapter):
        with patch("akshare.stock_financial_analysis_indicator",
                   side_effect=TimeoutError("slow")):
            with pytest.raises(NetworkError):
                adapter.fetch_eps_history("600519")


class TestFetchSnapshot:
    def test_aggregates_all_endpoints(self, adapter):
        bs_df = pd.DataFrame([{
            "REPORT_DATE": "2025-12-31 00:00:00",
            "TOTAL_ASSETS": 100.0,
            "TOTAL_CURRENT_ASSETS": 60.0,
            "TOTAL_CURRENT_LIAB": 30.0,
            "TOTAL_LIABILITIES": 40.0,
            "TOTAL_EQUITY": 60.0,
            "UNASSIGN_RPOFIT": 25.0,
            "SURPLUS_RESERVE": 5.0,
        }])
        is_df = pd.DataFrame([{
            "REPORT_DATE": "2025-12-31 00:00:00",
            "TOTAL_OPERATE_INCOME": 1000.0,
            "PARENT_NETPROFIT": 200.0,
            "TOTAL_PROFIT": 250.0,
            "FINANCE_EXPENSE": 30.0,
        }])
        spot_df = pd.DataFrame([{"代码": "600519", "市盈率-动态": 21.0}])
        eps_df = pd.DataFrame([
            {"日期": "2025-12-31", "摊薄每股收益(元)": 50.0},
            {"日期": "2024-12-31", "摊薄每股收益(元)": 40.0},
            {"日期": "2023-12-31", "摊薄每股收益(元)": 30.0},
            {"日期": "2022-12-31", "摊薄每股收益(元)": 25.0},
        ])
        with patch("akshare.stock_balance_sheet_by_yearly_em", return_value=bs_df), \
             patch("akshare.stock_profit_sheet_by_yearly_em", return_value=is_df), \
             patch("akshare.stock_zh_a_spot_em", return_value=spot_df), \
             patch("akshare.stock_financial_analysis_indicator", return_value=eps_df):
            snap = adapter.fetch_snapshot("600519")

        assert snap["stock"] == "600519"
        assert snap["report_period"] == "2025-12-31"
        assert snap["total_assets"] == 100.0
        assert snap["ebit"] == 280.0
        assert snap["pe_ttm"] == 21.0
        assert len(snap["eps_history"]) == 4
