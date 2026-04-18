"""
tests/test_nbs_adapter.py — NBSCollector (V1) 测试

覆盖：
    - Happy path：3 指标 × 6 月 = 18 条 InfoUnitV1
    - content JSON 字段完整（indicator/value/period/mom_change/value_kind）
    - source=V1 / credibility=权威 / category=宏观
    - id 幂等（hash(V1+indicator+period)）
    - 列名变动：子串回退仍能识别
    - 列完全缺失 → ParseError
    - AkShare 函数不存在 → ParseError
    - AkShare 调用抛 ConnectionError → NetworkError 重试
    - 空 DataFrame → DataMissingError
    - NaN 值跳过
    - period 规范化：'YYYY年MM月份' / 'YYYY-MM' / pd.Timestamp 都 OK
    - mom_change 第一条 None，其余算差
    - months 参数限制输出条数
    - indicators 参数只采子集
    - 未知 indicator 初始化时报错
    - 一个指标失败不影响另两个
    - 速率限制
    - 契约再验证
"""
import json

import pandas as pd
import pytest

from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══ 公共 fixtures ═══

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "nbs_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def sleeps_captured(monkeypatch):
    captured = []
    monkeypatch.setattr("time.sleep", lambda s: captured.append(s))
    return captured


# ═══ 仿真 DataFrame 构造器 ═══

def _pmi_df(periods=None):
    periods = periods or ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]
    return pd.DataFrame({
        "月份": periods,
        "制造业-指数": [49.8, 50.3, 50.5, 49.9, 50.2, 50.5][:len(periods)],
    })


def _shrzgm_df(periods=None):
    periods = periods or ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]
    return pd.DataFrame({
        "月份": periods,
        "社会融资规模增量": [32000.0, 35000.0, 31000.0, 28000.0, 33000.0, 38000.0][:len(periods)],
    })


def _m2_df(periods=None):
    periods = periods or ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]
    return pd.DataFrame({
        "日期": periods,
        "今值": [8.1, 8.3, 8.5, 8.4, 8.9, 9.1][:len(periods)],
    })


@pytest.fixture
def patch_akshare(monkeypatch):
    """让 ak.macro_china_pmi / shrzgm / m2_yearly 返回标准的 fake 数据"""
    import akshare as ak
    monkeypatch.setattr(ak, "macro_china_pmi", lambda: _pmi_df())
    monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: _shrzgm_df())
    monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: _m2_df())
    return ak


@pytest.fixture
def collector(tmp_db):
    from infra.data_adapters.nbs import NBSCollector
    return NBSCollector(db=tmp_db)


# ═══ Happy path ═══

class TestHappyPath:
    def test_collect_returns_3_indicators_x_6_months(self, collector, patch_akshare):
        units = collector.collect_recent(months=6)
        assert len(units) == 18  # 3 × 6

    def test_each_unit_is_info_unit_v1(self, collector, patch_akshare):
        units = collector.collect_recent(months=6)
        for u in units:
            assert isinstance(u, InfoUnitV1)
            assert u.source == "V1"
            assert u.source_credibility == "权威"
            assert u.category == "宏观"
            assert u.related_industries == []

    def test_content_fields_present(self, collector, patch_akshare):
        units = collector.collect_recent(months=6)
        for u in units:
            c = json.loads(u.content)
            assert {"indicator", "value", "value_kind", "period", "mom_change"} <= set(c)

    @pytest.mark.parametrize(
        "indicator,value_kind",
        [
            ("PMI", "index"),
            ("社融", "amount_yi_yuan"),
            ("M2", "yoy_pct"),
        ],
    )
    def test_value_kind_correct(self, collector, patch_akshare, indicator, value_kind):
        units = collector.collect_recent(months=6)
        hits = [u for u in units if json.loads(u.content)["indicator"] == indicator]
        assert hits
        for u in hits:
            assert json.loads(u.content)["value_kind"] == value_kind

    def test_timestamp_is_period_01_utc(self, collector, patch_akshare):
        units = collector.collect_recent(months=6)
        for u in units:
            c = json.loads(u.content)
            assert u.timestamp == f"{c['period']}-01T00:00:00+00:00"

    def test_id_is_16_hex(self, collector, patch_akshare):
        units = collector.collect_recent(months=6)
        for u in units:
            assert len(u.id) == 16
            int(u.id, 16)

    def test_months_limits_output(self, collector, patch_akshare):
        units = collector.collect_recent(months=3)
        # 3 indicators × 3 months = 9
        assert len(units) == 9

    def test_latest_periods_returned(self, collector, patch_akshare):
        """取最近 3 个月 → 应是 2026-01 / 2026-02 / 2026-03"""
        units = collector.collect_recent(months=3)
        pmi = sorted(
            [u for u in units if json.loads(u.content)["indicator"] == "PMI"],
            key=lambda u: json.loads(u.content)["period"],
        )
        periods = [json.loads(u.content)["period"] for u in pmi]
        assert periods == ["2026-01", "2026-02", "2026-03"]


# ═══ mom_change ═══

class TestMomChange:
    def test_first_row_mom_is_none(self, collector, patch_akshare):
        units = collector.collect_recent(months=6)
        pmi = sorted(
            [u for u in units if json.loads(u.content)["indicator"] == "PMI"],
            key=lambda u: json.loads(u.content)["period"],
        )
        assert json.loads(pmi[0].content)["mom_change"] is None

    def test_subsequent_rows_mom_is_diff(self, collector, patch_akshare):
        """PMI: 49.8, 50.3 → mom=0.5；50.5 → 0.2"""
        units = collector.collect_recent(months=6)
        pmi = sorted(
            [u for u in units if json.loads(u.content)["indicator"] == "PMI"],
            key=lambda u: json.loads(u.content)["period"],
        )
        assert json.loads(pmi[1].content)["mom_change"] == pytest.approx(0.5)
        assert json.loads(pmi[2].content)["mom_change"] == pytest.approx(0.2)
        assert json.loads(pmi[3].content)["mom_change"] == pytest.approx(-0.6)


# ═══ 列名容错 ═══

class TestColumnFallback:
    def test_alt_pmi_column_name_works(self, tmp_db, monkeypatch):
        """AkShare 改叫 '制造业PMI' 时子串匹配仍能识别"""
        alt_df = pd.DataFrame({
            "日期": ["2026-02", "2026-03"],
            "制造业PMI": [50.2, 50.5],  # 不同的列名
        })
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", lambda: alt_df)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        units = c.collect_recent(months=2)
        assert len(units) == 2
        assert json.loads(units[-1].content)["value"] == 50.5

    def test_unknown_columns_raise_parse_error(self, tmp_db, monkeypatch):
        """所有列都认不出 → ParseError（异常可见性）"""
        weird_df = pd.DataFrame({
            "X": ["2026-03"],
            "Y": [50.0],
        })
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", lambda: weird_df)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        c.collect_recent(months=2)

        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("nbs_v1",),
        )
        parse_rows = [r for r in rows if r["error_type"] == "parse"]
        assert parse_rows
        assert "cannot find" in parse_rows[0]["error_message"]


# ═══ period 规范化 ═══

class TestPeriodNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-03", "2026-03"),
            ("2026年03月份", "2026-03"),
            ("2026年3月", "2026-03"),
            ("2026-03-01", "2026-03"),
            ("2026/03", "2026-03"),
            ("2026.03", "2026-03"),
            ("garbage", None),
            ("", None),
        ],
    )
    def test_normalize(self, raw, expected):
        from infra.data_adapters.nbs import NBSCollector
        assert NBSCollector._normalize_period(raw) == expected

    def test_pandas_timestamp_normalized(self, tmp_db, monkeypatch):
        """pd.Timestamp 对象经 strftime 归一化"""
        ts_df = pd.DataFrame({
            "月份": pd.to_datetime(["2026-02-01", "2026-03-01"]),
            "制造业-指数": [50.2, 50.5],
        })
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", lambda: ts_df)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        units = c.collect_recent(months=2)
        periods = sorted(json.loads(u.content)["period"] for u in units)
        assert periods == ["2026-02", "2026-03"]


# ═══ 异常分支 ═══

class TestErrorHandling:
    def test_missing_ak_function_is_parse_error(self, tmp_db, monkeypatch):
        """AkShare 没有该函数 → ParseError"""
        import akshare as ak
        monkeypatch.delattr(ak, "macro_china_pmi", raising=False)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        c.collect_recent(months=6)

        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("nbs_v1",),
        )
        parse = [r for r in rows if r["error_type"] == "parse"]
        assert parse
        assert "no function" in parse[0]["error_message"]

    def test_empty_df_is_data_missing(self, tmp_db, monkeypatch):
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: _shrzgm_df())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: _m2_df())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        c.collect_recent(months=6)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("nbs_v1",),
        )
        assert any(r["error_type"] == "data" for r in rows)

    def test_all_rows_nan_becomes_data_missing(self, tmp_db, monkeypatch):
        nan_df = pd.DataFrame({
            "月份": ["2026-02", "2026-03"],
            "制造业-指数": [float("nan"), float("nan")],
        })
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", lambda: nan_df)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        c.collect_recent(months=6)

        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("nbs_v1",),
        )
        data_rows = [r for r in rows if r["error_type"] == "data"]
        assert data_rows
        assert "no usable rows" in data_rows[0]["error_message"]

    def test_mixed_nan_rows_skipped(self, tmp_db, monkeypatch):
        """部分 NaN 跳过，剩下的还能采到"""
        mixed_df = pd.DataFrame({
            "月份": ["2026-01", "2026-02", "2026-03"],
            "制造业-指数": [49.9, float("nan"), 50.5],
        })
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", lambda: mixed_df)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        units = c.collect_recent(months=6)
        assert len(units) == 2  # 跳掉 NaN 那一个
        periods = sorted(json.loads(u.content)["period"] for u in units)
        assert periods == ["2026-01", "2026-03"]

    def test_connection_error_retries(self, tmp_db, monkeypatch, sleeps_captured):
        calls = []
        def raise_conn():
            calls.append(1)
            raise ConnectionError("boom")
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", raise_conn)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        c.collect_recent(months=6)

        # 1 原始 + 3 重试
        assert len(calls) == 1 + c.MAX_RETRIES
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("nbs_v1",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    def test_net_like_string_routes_to_network(self, tmp_db, monkeypatch, sleeps_captured):
        """AkShare 内部有时包装成 RuntimeError('timeout'): 文本识别归 network"""
        def raise_wrapped():
            raise RuntimeError("Connection timeout while fetching page")
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", raise_wrapped)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: pd.DataFrame())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: pd.DataFrame())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        c.collect_recent(months=6)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("nbs_v1",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    def test_one_indicator_failure_others_succeed(self, tmp_db, monkeypatch):
        """PMI 失败但社融/M2 正常 → 得到 12 条（2 × 6）"""
        def raise_pmi():
            raise ValueError("pmi fetch failed")
        import akshare as ak
        monkeypatch.setattr(ak, "macro_china_pmi", raise_pmi)
        monkeypatch.setattr(ak, "macro_china_shrzgm", lambda: _shrzgm_df())
        monkeypatch.setattr(ak, "macro_china_m2_yearly", lambda: _m2_df())

        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db)
        units = c.collect_recent(months=6)
        assert len(units) == 12

        indicators_seen = {json.loads(u.content)["indicator"] for u in units}
        assert indicators_seen == {"社融", "M2"}


# ═══ 幂等 ═══

class TestIdempotency:
    def test_persist_twice_only_inserts_once(self, collector, patch_akshare):
        units = collector.collect_recent(months=6)
        first = collector.persist_batch(units)
        second = collector.persist_batch(units)
        assert first == 18
        assert second == 0

    def test_run_full_pipeline(self, collector, patch_akshare, tmp_db):
        n = collector.run(months=6)
        assert n == 18
        total = tmp_db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='V1'"
        )["n"]
        assert total == 18

    def test_id_deterministic_same_indicator_period(self, tmp_db):
        """id = hash(V1 + indicator + period)：同样输入同样 id"""
        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db)
        id1 = c._make_nbs_id("PMI", "2026-03")
        id2 = c._make_nbs_id("PMI", "2026-03")
        assert id1 == id2

    def test_id_different_indicator_different(self, tmp_db):
        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db)
        assert c._make_nbs_id("PMI", "2026-03") != c._make_nbs_id("M2", "2026-03")

    def test_id_different_period_different(self, tmp_db):
        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db)
        assert c._make_nbs_id("PMI", "2026-02") != c._make_nbs_id("PMI", "2026-03")


# ═══ indicators 子集 ═══

class TestIndicatorSubset:
    def test_only_pmi_indicator(self, tmp_db, patch_akshare):
        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, indicators=["PMI"])
        units = c.collect_recent(months=6)
        assert len(units) == 6
        indicators_seen = {json.loads(u.content)["indicator"] for u in units}
        assert indicators_seen == {"PMI"}

    def test_unknown_indicator_raises_on_init(self, tmp_db):
        from infra.data_adapters.nbs import NBSCollector
        with pytest.raises(ValueError, match="Unknown indicators"):
            NBSCollector(db=tmp_db, indicators=["GDP"])  # GDP 不在 Phase 1 范围

    def test_default_indicators_are_all_three(self, tmp_db):
        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db)
        assert set(c.indicators) == {"PMI", "社融", "M2"}


# ═══ 契约再验证 ═══

def test_output_passes_info_unit_v1_contract(collector, patch_akshare):
    units = collector.collect_recent(months=6)
    for u in units:
        recon = InfoUnitV1(**u.model_dump())
        assert recon.id == u.id
        assert recon.source == "V1"


# ═══ 速率限制 ═══

class TestRateLimit:
    def test_sleep_between_indicators(self, collector, patch_akshare, sleeps_captured):
        collector.collect_recent(months=6)
        # 3 次 ak 调用，两次间隔需 sleep ≥ MIN_INTERVAL
        positive = [s for s in sleeps_captured if s > 0]
        assert len(positive) >= 2, f"expected ≥2 rate-limit sleeps, got {positive}"

    def test_min_interval_is_3s(self, collector):
        assert collector.MIN_INTERVAL_SECONDS == 3.0


# ═══ 实例化 ═══

class TestInstantiation:
    def test_source_and_credibility(self, collector):
        assert collector.SOURCE_CODE == "V1"
        assert collector.CREDIBILITY == "权威"

    def test_inherits_base_agent(self, collector):
        assert collector.name == "nbs_v1"
        assert collector.MAX_RETRIES == 3
        assert collector.run_with_error_handling is not None

    def test_custom_name(self, tmp_db):
        from infra.data_adapters.nbs import NBSCollector
        c = NBSCollector(db=tmp_db, name="nbs_custom")
        assert c.name == "nbs_custom"
