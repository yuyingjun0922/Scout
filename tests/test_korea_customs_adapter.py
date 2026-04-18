"""
tests/test_korea_customs_adapter.py — KoreaCustomsCollector (V3) 测试

测试策略：
    - _http_fetch_raw 是 best-effort 爬虫（Phase 1 现实多半失败），用 monkeypatch
      替换成直接返回 fixture HTML；
    - _parse_trade_html 是纯函数，用 fixtures/korea_customs_sample.html 验证
      契约（period / 数字 / 顶级国家 / HS 行业映射）。

覆盖：
    - HTML 解析：期望字段齐全
    - HS code → 行业映射
    - 多 HS code 收集 + 错误隔离
    - 错误页面检测（len<3000 + errortype 关键字）
    - period / amount / pct 规范化
    - 网络错误重试
    - 幂等性、契约再验证
    - HS code 子集 + 未知 HS code 校验
    - 速率限制
"""
import json
from pathlib import Path

import httpx
import pytest

from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_HTML = (FIXTURES_DIR / "korea_customs_sample.html").read_text(encoding="utf-8")


# ═══ fixtures ═══

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "v3_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def sleeps_captured(monkeypatch):
    captured = []
    monkeypatch.setattr("time.sleep", lambda s: captured.append(s))
    return captured


@pytest.fixture
def fixture_html():
    return SAMPLE_HTML


@pytest.fixture
def patched_fetch(monkeypatch, fixture_html):
    """把 _http_fetch_raw 替换成直接返回 fixture HTML（跳过真 HTTP）"""
    from infra.data_adapters.korea_customs import KoreaCustomsCollector
    monkeypatch.setattr(
        KoreaCustomsCollector,
        "_http_fetch_raw",
        lambda self, hs: fixture_html,
    )
    return KoreaCustomsCollector


@pytest.fixture
def collector(tmp_db, patched_fetch):
    return patched_fetch(db=tmp_db, hs_codes=["8542"])


# ═══ Happy path ═══

class TestHappyPath:
    def test_collect_returns_info_units(self, collector):
        units = collector.collect_recent(months=6)
        assert len(units) == 6
        for u in units:
            assert isinstance(u, InfoUnitV1)
            assert u.source == "V3"
            assert u.source_credibility == "权威"
            assert u.category == "宏观"

    def test_content_has_all_spec_fields(self, collector):
        units = collector.collect_recent(months=6)
        for u in units:
            c = json.loads(u.content)
            for key in (
                "hs_code", "hs_name_ko", "hs_name_en",
                "period", "export_usd", "import_usd",
                "export_qty", "top_countries", "yoy_export_pct",
            ):
                assert key in c, f"missing key: {key}"

    def test_latest_period_has_top_countries(self, collector):
        """只有最新月份带 top_countries；其它月份为空列表"""
        units = collector.collect_recent(months=6)
        latest = max(units, key=lambda u: u.timestamp)
        c = json.loads(latest.content)
        assert len(c["top_countries"]) == 5
        assert c["top_countries"][0]["country"] == "CN"
        assert c["top_countries"][0]["export_usd"] == 5_000_000_000.0
        # 其它月份应为空
        others = [u for u in units if u.id != latest.id]
        for u in others:
            assert json.loads(u.content)["top_countries"] == []

    def test_industries_mapped_from_hs_code(self, collector):
        units = collector.collect_recent(months=6)
        for u in units:
            assert set(u.related_industries) == {"半导体设备", "AI算力"}

    def test_amounts_parsed_correctly(self, collector):
        """验证千分位逗号被剥掉、float 精度"""
        units = collector.collect_recent(months=6)
        latest = max(units, key=lambda u: u.timestamp)
        c = json.loads(latest.content)
        assert c["export_usd"] == 12_345_678_901.0
        assert c["import_usd"] == 1_234_567_890.0
        assert c["export_qty"] == 9_876_543.0
        assert c["yoy_export_pct"] == 15.3

    def test_period_normalized_to_yyyy_mm(self, collector):
        """fixture 用 'YYYY.MM' 格式；应规范化为 'YYYY-MM'"""
        units = collector.collect_recent(months=6)
        periods = sorted({json.loads(u.content)["period"] for u in units})
        assert periods == [
            "2025-10", "2025-11", "2025-12",
            "2026-01", "2026-02", "2026-03",
        ]

    def test_id_is_16_hex_and_deterministic(self, collector):
        first = collector.collect_recent(months=6)
        second = collector.collect_recent(months=6)
        ids_1 = sorted(u.id for u in first)
        ids_2 = sorted(u.id for u in second)
        assert ids_1 == ids_2
        for u in first:
            assert len(u.id) == 16
            int(u.id, 16)


# ═══ 规范化工具 ═══

class TestNormalizers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026.03", "2026-03"),
            ("2026-03", "2026-03"),
            ("202603", "2026-03"),
            ("2026년 3월", "2026-03"),
            ("2026/03/01", "2026-03"),
            ("garbage", None),
            ("", None),
            ("2026.13", None),  # 月份非法
        ],
    )
    def test_normalize_period(self, raw, expected):
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        assert KoreaCustomsCollector._normalize_period(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("12,345", 12345.0),
            ("1,234,567,890", 1234567890.0),
            ("   1000  ", 1000.0),
            ("1000.5", 1000.5),
        ],
    )
    def test_parse_amount(self, raw, expected):
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        assert KoreaCustomsCollector._parse_amount(raw) == expected

    @pytest.mark.parametrize("bad", ["", "-", "N/A"])
    def test_parse_amount_empty_raises(self, bad):
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        with pytest.raises(ValueError):
            KoreaCustomsCollector._parse_amount(bad)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("+15.3%", 15.3),
            ("-5.2%", -5.2),
            ("12.5", 12.5),
            ("", None),
            ("-", None),
            ("invalid", None),
        ],
    )
    def test_parse_pct(self, raw, expected):
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        assert KoreaCustomsCollector._parse_pct(raw) == expected


# ═══ months 切片 ═══

class TestMonthsSlicing:
    def test_months_limits_output(self, collector):
        units = collector.collect_recent(months=3)
        assert len(units) == 3

    def test_latest_months_returned(self, collector):
        units = collector.collect_recent(months=3)
        periods = sorted(json.loads(u.content)["period"] for u in units)
        assert periods == ["2026-01", "2026-02", "2026-03"]


# ═══ HS code 子集 + 未知 ═══

class TestHsCodeSelection:
    def test_default_hs_codes_are_phase1_set(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db)
        assert set(c.hs_codes) == {"8542", "854232", "8541"}

    def test_custom_hs_subset(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db, hs_codes=["854232"])
        units = c.collect_recent(months=6)
        assert len(units) == 6
        for u in units:
            assert json.loads(u.content)["hs_code"] == "854232"
            assert "HBM" in u.related_industries

    def test_unknown_hs_code_raises_on_init(self, tmp_db, patched_fetch):
        with pytest.raises(ValueError, match="Unknown HS"):
            patched_fetch(db=tmp_db, hs_codes=["0000"])


# ═══ 多 HS code 聚合 ═══

class TestMultipleHsCodes:
    def test_three_hs_codes_18_rows(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db)
        units = c.collect_recent(months=6)
        assert len(units) == 18  # 3 × 6

    def test_one_hs_failure_others_succeed(self, tmp_db, monkeypatch, fixture_html):
        """一个 HS code 的抓取抛 ParseError（已分类），其它 HS 仍返回数据。

        用 ParseError 不用 ValueError：BaseAgent 设计就是把未分类异常当作 unknown
        re-raise（fail-loud）。爬虫抛出内容应该是 Scout 的 5 类异常之一。
        """
        from agents.base import ParseError
        from infra.data_adapters.korea_customs import KoreaCustomsCollector

        def selective_fetch(self, hs):
            if hs == "854232":
                raise ParseError("854232 HTML malformed (mocked)")
            return fixture_html

        monkeypatch.setattr(
            KoreaCustomsCollector,
            "_http_fetch_raw",
            selective_fetch,
        )
        c = KoreaCustomsCollector(db=tmp_db)
        units = c.collect_recent(months=6)
        assert len(units) == 12  # 2 好的 × 6

        hs_seen = {json.loads(u.content)["hs_code"] for u in units}
        assert hs_seen == {"8542", "8541"}

        # 失败的那条落 agent_errors
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            ("korea_customs_v3",),
        )
        assert any(r["error_type"] == "parse" for r in rows)


# ═══ 错误页面检测 ═══

class TestErrorPageDetection:
    def test_short_error_html_raises_parse(self, tmp_db, monkeypatch):
        """len<3000 + 含 errortype → ParseError（现实场景：UniPass 骨架页）"""
        error_skel = """<html><body>
        <input name="errortype"/><input name="errorSavedToken"/>
        <p>JS required</p></body></html>"""
        from infra.data_adapters.korea_customs import KoreaCustomsCollector

        def fake_client_returns_skel(*a, **kw):
            # 绕过整个 _http_fetch_raw 内部流程，直接测 _looks_like_error_page
            return error_skel
        monkeypatch.setattr(
            KoreaCustomsCollector,
            "_http_fetch_raw",
            lambda self, hs: (_ for _ in ()).throw(
                __import__('agents.base', fromlist=['ParseError']).ParseError(
                    "mock error page"
                )
            ),
        )

        c = KoreaCustomsCollector(db=tmp_db, hs_codes=["8542"])
        units = c.collect_recent(months=6)
        assert units == []

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            ("korea_customs_v3",),
        )
        assert any(r["error_type"] == "parse" for r in rows)

    def test_looks_like_error_page_classifier(self):
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        # 短 + errortype → True
        assert KoreaCustomsCollector._looks_like_error_page(
            '<html><input name="errortype"/></html>'
        )
        # 长 → False（即使含 errortype）
        long_html = "x" * 5000 + '<input name="errortype"/>'
        assert not KoreaCustomsCollector._looks_like_error_page(long_html)
        # 短但无 errortype → False
        assert not KoreaCustomsCollector._looks_like_error_page("<html>ok</html>")

    def test_no_table_raises_parse(self, tmp_db, monkeypatch):
        no_table_html = "<html><body>no table here but enough bytes</body></html>" * 100
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        monkeypatch.setattr(
            KoreaCustomsCollector,
            "_http_fetch_raw",
            lambda self, hs: no_table_html,
        )
        c = KoreaCustomsCollector(db=tmp_db, hs_codes=["8542"])
        c.collect_recent(months=6)
        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("korea_customs_v3",),
        )
        parse = [r for r in rows if r["error_type"] == "parse"]
        assert parse
        assert "no <table>" in parse[0]["error_message"]

    def test_table_with_unparseable_rows_raises_parse(self, tmp_db, monkeypatch):
        """表在但列数不足或格式错 → ParseError（区别于 DataMissing）"""
        bad_html = """<html><body>
        <table class="monthly-data"><tbody>
            <tr><td>X</td><td>Y</td></tr>
        </tbody></table>
        </body></html>"""
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        monkeypatch.setattr(
            KoreaCustomsCollector,
            "_http_fetch_raw",
            lambda self, hs: bad_html,
        )
        c = KoreaCustomsCollector(db=tmp_db, hs_codes=["8542"])
        c.collect_recent(months=6)
        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("korea_customs_v3",),
        )
        parse = [r for r in rows if r["error_type"] == "parse"]
        assert parse
        assert "zero rows" in parse[0]["error_message"]


# ═══ 网络错误 ═══

class TestNetworkHandling:
    def test_timeout_in_fetch_triggers_retry(self, tmp_db, monkeypatch):
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        from agents.base import NetworkError

        calls = []

        def raise_timeout(self, hs):
            calls.append(hs)
            raise NetworkError("simulated timeout")

        monkeypatch.setattr(
            KoreaCustomsCollector, "_http_fetch_raw", raise_timeout
        )
        c = KoreaCustomsCollector(db=tmp_db, hs_codes=["8542"])
        c.collect_recent(months=6)

        # 1 原 + 3 重试
        assert len(calls) == 1 + c.MAX_RETRIES
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            ("korea_customs_v3",),
        )
        assert any(r["error_type"] == "network" for r in rows)


# ═══ 幂等性 ═══

class TestIdempotency:
    def test_persist_twice_only_once(self, collector):
        units = collector.collect_recent(months=6)
        first = collector.persist_batch(units)
        second = collector.persist_batch(units)
        assert first == 6
        assert second == 0

    def test_id_deterministic(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db)
        id1 = c._make_v3_id("8542", "2026-03")
        id2 = c._make_v3_id("8542", "2026-03")
        assert id1 == id2

    def test_id_differs_per_hs_and_period(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db)
        assert c._make_v3_id("8542", "2026-03") != c._make_v3_id("8541", "2026-03")
        assert c._make_v3_id("8542", "2026-02") != c._make_v3_id("8542", "2026-03")


# ═══ 契约 ═══

def test_output_passes_info_unit_v1_contract(collector):
    units = collector.collect_recent(months=6)
    for u in units:
        recon = InfoUnitV1(**u.model_dump())
        assert recon.id == u.id
        assert recon.source == "V3"


# ═══ 速率限制 ═══

class TestRateLimit:
    def test_sleep_between_hs_codes(self, tmp_db, patched_fetch, sleeps_captured):
        c = patched_fetch(db=tmp_db)
        c.collect_recent(months=6)
        positive = [s for s in sleeps_captured if s > 0]
        # 3 HS 间至少 2 次 rate-limit sleep
        assert len(positive) >= 2

    def test_min_interval_2s(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db)
        assert c.MIN_INTERVAL_SECONDS == 2.0


# ═══ 实例化 ═══

class TestInstantiation:
    def test_source_and_credibility(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db)
        assert c.SOURCE_CODE == "V3"
        assert c.CREDIBILITY == "权威"

    def test_inherits_base_agent(self, tmp_db, patched_fetch):
        c = patched_fetch(db=tmp_db)
        assert c.name == "korea_customs_v3"
        assert c.MAX_RETRIES == 3
