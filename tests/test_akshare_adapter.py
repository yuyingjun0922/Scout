"""
tests/test_akshare_adapter.py — AkShareCollector 测试（mock akshare，不碰网络）

覆盖：
    - 正常路径：mock DataFrame → 正确的 InfoUnitV1 列表
    - content 是 JSON 且字段齐全
    - 行业从股票名关键词推断（茅台 → 白酒）
    - InfoUnitV1 契约满足（id 16-hex，source=S4，credibility=权威，category=公司）
    - 幂等性：持久化两次只入一次
    - 异常：空 DataFrame → DataMissingError → 'data' 错 + 空列表
    - 异常：ConnectionError → network → 重试 3 次后落库
    - 异常：None DataFrame 也视为空
    - 速率限制：多只股票之间触发 sleep
    - 重建 Pydantic：从输出再走一次契约验证
"""
import json

import pandas as pd
import pytest

from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══ fixtures ═══

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "akshare_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture
def sleeps_captured(monkeypatch):
    """拦截所有 time.sleep，记录参数，不真 sleep"""
    captured = []
    monkeypatch.setattr("time.sleep", lambda s: captured.append(s))
    return captured


@pytest.fixture
def fake_daily_df():
    """AkShare stock_zh_a_hist 的仿真返回"""
    return pd.DataFrame({
        "日期": ["2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16", "2026-04-17"],
        "开盘": [1800.0, 1815.0, 1820.0, 1830.0, 1840.0],
        "收盘": [1810.0, 1820.0, 1828.0, 1835.0, 1850.0],
        "最高": [1825.0, 1830.0, 1835.0, 1842.0, 1860.0],
        "最低": [1795.0, 1810.0, 1815.0, 1825.0, 1835.0],
        "成交量": [12345.0, 23456.0, 34567.0, 45678.0, 56789.0],
        "涨跌幅": [0.56, 0.55, 0.44, 0.38, 0.82],
        "名称": ["贵州茅台"] * 5,
    })


@pytest.fixture
def patch_akshare(monkeypatch, fake_daily_df):
    """把 akshare.stock_zh_a_hist 换成返回 fake_daily_df 的函数"""
    import akshare as ak
    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **kw: fake_daily_df)
    return ak


@pytest.fixture
def collector(tmp_db):
    from infra.data_adapters.akshare_wrapper import AkShareCollector
    return AkShareCollector(db=tmp_db, symbols=["600519"])


# ═══ 正常路径 ═══

class TestHappyPath:
    def test_collect_recent_returns_info_units(self, collector, patch_akshare, sleeps_captured):
        units = collector.collect_recent(days=5)
        assert len(units) == 5
        for u in units:
            assert isinstance(u, InfoUnitV1)
            assert u.source == "S4"
            assert u.source_credibility == "权威"
            assert u.category == "公司"

    def test_content_is_valid_json(self, collector, patch_akshare, sleeps_captured):
        units = collector.collect_recent(days=5)
        content = json.loads(units[0].content)
        assert content["symbol"] == "600519"
        assert "close" in content
        assert "open" in content
        assert "pct_change" in content
        assert "trading_date" in content

    def test_content_preserves_numeric_precision(self, collector, patch_akshare, sleeps_captured):
        units = collector.collect_recent(days=5)
        last = json.loads(units[-1].content)
        assert last["close"] == 1850.0
        assert last["volume"] == 56789.0

    def test_industry_inferred_from_name(self, collector, patch_akshare, sleeps_captured):
        """茅台 → 白酒"""
        units = collector.collect_recent(days=5)
        assert "白酒" in units[0].related_industries

    def test_id_is_16_hex(self, collector, patch_akshare, sleeps_captured):
        units = collector.collect_recent(days=5)
        for u in units:
            assert len(u.id) == 16
            int(u.id, 16)  # 不抛错即合法 hex

    def test_timestamp_is_utc_iso8601(self, collector, patch_akshare, sleeps_captured):
        units = collector.collect_recent(days=5)
        for u in units:
            assert "T07:00:00+00:00" in u.timestamp

    def test_tail_limits_to_days(self, tmp_db, fake_daily_df, sleeps_captured, monkeypatch):
        """days=3 应只取最后 3 行"""
        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **kw: fake_daily_df)

        from infra.data_adapters.akshare_wrapper import AkShareCollector
        c = AkShareCollector(db=tmp_db, symbols=["600519"])
        units = c.collect_recent(days=3)
        assert len(units) == 3
        # 拿的是尾部 3 天
        dates = [json.loads(u.content)["trading_date"] for u in units]
        assert dates == ["2026-04-15", "2026-04-16", "2026-04-17"]


# ═══ 契约验证 ═══

def test_output_passes_info_unit_v1_contract(collector, patch_akshare, sleeps_captured):
    """输出再次走 Pydantic 验证应通过"""
    units = collector.collect_recent(days=5)
    for u in units:
        recon = InfoUnitV1(**u.model_dump())
        assert recon.id == u.id
        assert recon.source == "S4"


# ═══ 幂等性 ═══

class TestIdempotency:
    def test_persist_twice_only_inserts_once(self, collector, patch_akshare, sleeps_captured):
        units = collector.collect_recent(days=5)
        first = collector.persist_batch(units)
        second = collector.persist_batch(units)
        assert first == 5
        assert second == 0

    def test_run_full_pipeline_returns_inserted_count(self, collector, patch_akshare, sleeps_captured, tmp_db):
        count = collector.run(days=5)
        assert count == 5
        assert tmp_db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='S4'"
        )["n"] == 5

    def test_run_second_time_is_noop(self, collector, patch_akshare, sleeps_captured, tmp_db):
        collector.run(days=5)
        second = collector.run(days=5)
        assert second == 0
        assert tmp_db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='S4'"
        )["n"] == 5


# ═══ 异常 cases ═══

class TestErrorCases:
    def test_empty_df_triggers_data_missing(self, collector, tmp_db, sleeps_captured, monkeypatch):
        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **kw: pd.DataFrame())

        units = collector.collect_recent(days=5)
        assert units == []

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            ("akshare_s4",),
        )
        assert any(r["error_type"] == "data" for r in rows)

    def test_none_df_also_treated_as_missing(self, collector, tmp_db, sleeps_captured, monkeypatch):
        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **kw: None)

        units = collector.collect_recent(days=5)
        assert units == []
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            ("akshare_s4",),
        )
        assert any(r["error_type"] == "data" for r in rows)

    def test_connection_error_retries_3_times(self, collector, tmp_db, sleeps_captured, monkeypatch):
        calls = []
        def raise_conn(**kw):
            calls.append(1)
            raise ConnectionError("connection refused")

        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", raise_conn)

        units = collector.collect_recent(days=5)
        assert units == []
        # 1 次原始 + 3 次重试 = 4 次调用
        assert len(calls) == 1 + collector.MAX_RETRIES

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            ("akshare_s4",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    def test_timeout_error_classified_as_network(self, collector, tmp_db, sleeps_captured, monkeypatch):
        def raise_timeout(**kw):
            raise TimeoutError("timed out")
        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", raise_timeout)

        collector.collect_recent(days=5)
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            ("akshare_s4",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    def test_generic_akshare_error_is_parse(self, collector, tmp_db, sleeps_captured, monkeypatch):
        """AkShare 抛出非网络类异常 → 归入 parse 错"""
        def raise_value_err(**kw):
            raise ValueError("symbol not found")
        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", raise_value_err)

        units = collector.collect_recent(days=5)
        assert units == []

        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("akshare_s4",),
        )
        assert any(r["error_type"] == "parse" for r in rows)

    def test_malformed_df_missing_column_is_parse_error(self, collector, tmp_db, sleeps_captured, monkeypatch):
        """DataFrame 缺必填列 → ParseError；raw 片段保留"""
        bad_df = pd.DataFrame({
            "日期": ["2026-04-17"],
            "开盘": [100.0],
            # 缺少 '收盘' 等列
        })
        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **kw: bad_df)

        units = collector.collect_recent(days=5)
        assert units == []

        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("akshare_s4",),
        )
        parse_rows = [r for r in rows if r["error_type"] == "parse"]
        assert parse_rows
        # raw 片段应保留
        assert "raw=" in parse_rows[0]["error_message"]

    def test_one_symbol_fails_others_still_collected(self, tmp_db, fake_daily_df, sleeps_captured, monkeypatch):
        """错误隔离：一只股票失败不影响其他股票"""
        call_count = [0]

        def selective(**kw):
            call_count[0] += 1
            symbol = kw.get("symbol")
            if symbol == "BAD":
                raise ValueError("bad symbol")
            return fake_daily_df

        import akshare as ak
        monkeypatch.setattr(ak, "stock_zh_a_hist", selective)

        from infra.data_adapters.akshare_wrapper import AkShareCollector
        c = AkShareCollector(db=tmp_db, symbols=["600519", "BAD", "300750"])
        units = c.collect_recent(days=5)

        # 2 个好股票各 5 条，共 10 条
        assert len(units) == 10
        symbols_in_output = {json.loads(u.content)["symbol"] for u in units}
        assert "600519" in symbols_in_output
        assert "300750" in symbols_in_output
        assert "BAD" not in symbols_in_output


# ═══ 速率限制 ═══

def test_rate_limit_triggers_sleep_between_symbols(tmp_db, fake_daily_df, sleeps_captured, monkeypatch):
    """两只股票之间应触发 rate-limit sleep（>0）"""
    import akshare as ak
    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **kw: fake_daily_df)

    from infra.data_adapters.akshare_wrapper import AkShareCollector
    c = AkShareCollector(db=tmp_db, symbols=["600519", "300750"])
    c.collect_recent(days=5)

    # 第一次调用没 sleep；第二次调用前的 _rate_limit 应 sleep(>0)
    positive_sleeps = [s for s in sleeps_captured if s > 0]
    assert positive_sleeps, "expected at least one positive sleep between symbols"


def test_rate_limit_min_interval_class_attr(collector):
    """MIN_INTERVAL_SECONDS 可被子类覆盖"""
    assert collector.MIN_INTERVAL_SECONDS == 0.5


# ═══ 实例化约束（继承 Collector + BaseAgent 的交叉验证） ═══

class TestInstantiation:
    def test_default_symbols_used_when_none_passed(self, tmp_db):
        from infra.data_adapters.akshare_wrapper import AkShareCollector
        c = AkShareCollector(db=tmp_db)
        assert c.symbols == AkShareCollector.DEFAULT_SYMBOLS

    def test_custom_symbols_override(self, tmp_db):
        from infra.data_adapters.akshare_wrapper import AkShareCollector
        c = AkShareCollector(db=tmp_db, symbols=["000001", "000002"])
        assert c.symbols == ["000001", "000002"]

    def test_inherits_both_base_attrs(self, tmp_db):
        from infra.data_adapters.akshare_wrapper import AkShareCollector
        c = AkShareCollector(db=tmp_db)
        # Collector contract
        assert c.SOURCE_CODE == "S4"
        assert c.CREDIBILITY == "权威"
        assert c.persist_batch is not None
        # BaseAgent contract
        assert c.name == "akshare_s4"
        assert c.MAX_RETRIES == 3
        assert c.run_with_error_handling is not None

    def test_custom_name(self, tmp_db):
        from infra.data_adapters.akshare_wrapper import AkShareCollector
        c = AkShareCollector(db=tmp_db, name="custom_ak")
        assert c.name == "custom_ak"
