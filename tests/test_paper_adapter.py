"""
tests/test_paper_adapter.py — PaperCollector (D4) 测试

覆盖：
    - Happy path：两源解析、输出 InfoUnitV1、content JSON、category=科研、credibility=参考
    - 跨源 DOI 去重 + industries 合并
    - 关键词驱动的行业反推
    - industries 参数过滤
    - 日期截断（超过 days 的论文剔除）
    - HTTP 错误分类：Timeout/Network/Transport → network；4xx → parse；5xx/429 → network
    - 空数据 / 不完整字段容错
    - 幂等性
    - 契约再验证
    - 速率限制（arXiv/S2 独立计时）
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══ fixtures + 常量 ═══

def _iso_date(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "paper_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def sleeps_captured(monkeypatch):
    """所有测试自动跳过 sleep，并记录调用参数"""
    captured = []
    monkeypatch.setattr("time.sleep", lambda s: captured.append(s))
    return captured


# ═══ mock 响应 ═══

def _arxiv_xml(published_days_ago: int = 1, doi: str = "10.1234/test.2026.12345") -> str:
    pub = (datetime.now(timezone.utc) - timedelta(days=published_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doi_tag = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <title>Advanced Lithography for 2nm Nodes</title>
    <summary>We present a new EUV lithography technique for 2nm process nodes.</summary>
    <author><name>Alice Chen</name></author>
    <author><name>Bob Li</name></author>
    <published>{pub}</published>
    <updated>{pub}</updated>
    {doi_tag}
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.99999v1</id>
    <title>Etching Process Optimization</title>
    <summary>A novel plasma etching approach.</summary>
    <author><name>Carol Zhang</name></author>
    <published>{pub}</published>
    <updated>{pub}</updated>
  </entry>
</feed>
"""


def _s2_json(
    published_days_ago: int = 1,
    doi: str = "10.1234/hbm.2026.001",
    arxiv_id: str = "2401.11111",
) -> dict:
    pub = (datetime.now(timezone.utc) - timedelta(days=published_days_ago)).strftime("%Y-%m-%d")
    return {
        "total": 2,
        "offset": 0,
        "data": [
            {
                "paperId": "abc123",
                "title": "HBM3E Stack Density Improvements",
                "abstract": "We study HBM3E stacking techniques.",
                "authors": [{"name": "David Kim"}, {"name": "Ella Park"}],
                "venue": "ISSCC",
                "citationCount": 42,
                "externalIds": {"DOI": doi, "ArXiv": arxiv_id},
                "publicationDate": pub,
                "url": "https://www.semanticscholar.org/paper/abc123",
            },
            {
                "paperId": "xyz789",
                "title": "Old Paper",
                "abstract": "...",
                "authors": [{"name": "Frank Liu"}],
                "venue": "Nature",
                "citationCount": 100,
                "externalIds": {"DOI": "10.1234/old.2020.001"},
                "publicationDate": "2020-01-01",  # 非常老 → 应被截断
                "url": "https://www.semanticscholar.org/paper/xyz789",
            },
        ],
    }


def _mock_response(text: str = "", json_data=None, status: int = 200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    return resp


@pytest.fixture
def patch_http(monkeypatch):
    """默认：arXiv URL 返 XML；S2 URL 返 JSON；其它 404"""
    def fake_get(url, params=None, **kw):
        if "arxiv.org" in url:
            return _mock_response(text=_arxiv_xml())
        if "semanticscholar.org" in url:
            return _mock_response(json_data=_s2_json())
        return _mock_response(status=404)
    monkeypatch.setattr(httpx, "get", fake_get)
    return fake_get


@pytest.fixture
def collector(tmp_db):
    from infra.data_adapters.arxiv_semantic import PaperCollector
    return PaperCollector(
        db=tmp_db,
        industries_keywords={"半导体设备": ["lithography"]},
    )


# ═══ Happy path ═══

class TestHappyPath:
    def test_collect_returns_info_units(self, collector, patch_http):
        units = collector.collect_recent(days=7)
        assert len(units) >= 1
        for u in units:
            assert isinstance(u, InfoUnitV1)
            assert u.source == "D4"
            assert u.source_credibility == "参考"
            assert u.category == "科研"

    def test_content_has_paper_fields(self, collector, patch_http):
        units = collector.collect_recent(days=7)
        content = json.loads(units[0].content)
        for key in ("title", "authors", "abstract", "venue", "link", "source_api"):
            assert key in content

    def test_industries_tagged_from_keyword(self, collector, patch_http):
        """查 lithography → 打 '半导体设备' 标签"""
        units = collector.collect_recent(days=7)
        assert any("半导体设备" in u.related_industries for u in units)

    def test_id_is_16_hex(self, collector, patch_http):
        units = collector.collect_recent(days=7)
        for u in units:
            assert len(u.id) == 16
            int(u.id, 16)

    def test_timestamp_utc_iso8601(self, collector, patch_http):
        units = collector.collect_recent(days=7)
        for u in units:
            assert u.timestamp.endswith("+00:00") or u.timestamp.endswith("Z")


# ═══ 去重合并 ═══

class TestDedupeAndMerge:
    def test_same_doi_across_sources_dedup(self, tmp_db, monkeypatch):
        """两源返回同 DOI 的论文，最后只留一条"""
        shared_doi = "10.9999/shared.doi"

        def fake_get(url, params=None, **kw):
            if "arxiv.org" in url:
                return _mock_response(text=_arxiv_xml(doi=shared_doi))
            if "semanticscholar.org" in url:
                return _mock_response(json_data=_s2_json(doi=shared_doi))
            return _mock_response(status=404)
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        units = c.collect_recent(days=7)

        # DOI 相同的那一条去重
        dois = []
        for u in units:
            dois.append(json.loads(u.content).get("doi"))
        assert dois.count(shared_doi) == 1

    def test_industries_merge_across_keywords(self, tmp_db, monkeypatch):
        """同一篇论文在两个行业的关键词下都命中，industries 应合并"""
        shared_doi = "10.9999/crosscut.doi"

        def fake_get(url, params=None, **kw):
            # 每次都返回同一篇 DOI 的论文（模拟被所有关键词命中）
            if "arxiv.org" in url:
                return _mock_response(text=_arxiv_xml(doi=shared_doi))
            if "semanticscholar.org" in url:
                return _mock_response(json_data=_s2_json(doi=shared_doi))
            return _mock_response(status=404)
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(
            db=tmp_db,
            industries_keywords={
                "HBM": ["HBM"],
                "半导体设备": ["lithography"],
            },
        )
        units = c.collect_recent(days=7)

        # 找共享 DOI 那一条，应两个行业都在
        target = [u for u in units if json.loads(u.content).get("doi") == shared_doi]
        assert target
        assert set(target[0].related_industries) == {"HBM", "半导体设备"}


# ═══ industries 参数过滤 ═══

def test_industries_param_filters_queries(tmp_db, monkeypatch):
    """指定 industries=['HBM'] 时只查 HBM 关键词"""
    arxiv_calls = []
    s2_calls = []

    def fake_get(url, params=None, **kw):
        q = (params or {}).get("search_query") or (params or {}).get("query") or ""
        if "arxiv.org" in url:
            arxiv_calls.append(q)
            return _mock_response(text=_arxiv_xml())
        if "semanticscholar.org" in url:
            s2_calls.append(q)
            return _mock_response(json_data=_s2_json())
        return _mock_response(status=404)
    monkeypatch.setattr(httpx, "get", fake_get)

    from infra.data_adapters.arxiv_semantic import PaperCollector
    c = PaperCollector(
        db=tmp_db,
        industries_keywords={
            "HBM": ["HBM"],
            "固态电池": ["solid state battery"],
        },
    )
    c.collect_recent(days=7, industries=["HBM"])

    joined = " ".join(arxiv_calls + s2_calls)
    assert "HBM" in joined
    assert "solid state battery" not in joined


def test_empty_industries_keywords_returns_empty(tmp_db):
    from infra.data_adapters.arxiv_semantic import PaperCollector
    c = PaperCollector(db=tmp_db, industries_keywords={})
    assert c.collect_recent(days=7) == []


# ═══ 日期截断 ═══

def test_old_papers_excluded(collector, patch_http):
    """S2 fixture 里有一篇 2020 的旧论文，应被 cutoff 过滤掉"""
    units = collector.collect_recent(days=7)
    for u in units:
        content = json.loads(u.content)
        assert content["title"] != "Old Paper"


def test_arxiv_old_entry_excluded(tmp_db, monkeypatch):
    def fake_get(url, params=None, **kw):
        if "arxiv.org" in url:
            return _mock_response(text=_arxiv_xml(published_days_ago=365))
        if "semanticscholar.org" in url:
            return _mock_response(json_data={"data": []})
        return _mock_response(status=404)
    monkeypatch.setattr(httpx, "get", fake_get)

    from infra.data_adapters.arxiv_semantic import PaperCollector
    c = PaperCollector(db=tmp_db, industries_keywords={"半导体设备": ["lithography"]})
    assert c.collect_recent(days=7) == []


# ═══ HTTP 错误分类 ═══

class TestHttpErrorHandling:
    def test_timeout_classified_as_network(self, tmp_db, monkeypatch):
        def raise_timeout(*a, **kw):
            raise httpx.TimeoutException("timed out")
        monkeypatch.setattr(httpx, "get", raise_timeout)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"半导体设备": ["lithography"]})
        c.collect_recent(days=7)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("paper_d4",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    def test_connect_error_classified_as_network(self, tmp_db, monkeypatch):
        def raise_net(*a, **kw):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(httpx, "get", raise_net)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        c.collect_recent(days=7)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("paper_d4",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_5xx_and_429_classified_as_network(self, tmp_db, monkeypatch, status):
        def fake_get(url, **kw):
            return _mock_response(status=status)
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        c.collect_recent(days=7)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("paper_d4",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_4xx_classified_as_parse(self, tmp_db, monkeypatch, status):
        def fake_get(url, **kw):
            return _mock_response(status=status, text="client error")
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        c.collect_recent(days=7)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("paper_d4",),
        )
        assert any(r["error_type"] == "parse" for r in rows)

    def test_5xx_network_error_retries(self, tmp_db, monkeypatch):
        """5xx 是 network 错→走重试。每次 collect 触发 1+MAX_RETRIES 次 arXiv 调用"""
        calls = []
        def fake_get(url, **kw):
            if "arxiv.org" in url:
                calls.append(url)
                return _mock_response(status=503)
            return _mock_response(json_data={"data": []})
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        c.collect_recent(days=7)
        # 1 个行业 × 1 个关键词 = 1 轮；每轮 arxiv 1 + 重试 MAX_RETRIES
        assert len(calls) == 1 + c.MAX_RETRIES


# ═══ 容错：空 / 不完整 ═══

class TestTolerance:
    def test_empty_s2_data_ok(self, tmp_db, monkeypatch):
        def fake_get(url, **kw):
            if "arxiv.org" in url:
                return _mock_response(text='<?xml version="1.0"?><feed></feed>')
            return _mock_response(json_data={"data": []})
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        assert c.collect_recent(days=7) == []

    def test_s2_paper_without_publication_date_skipped(self, tmp_db, monkeypatch):
        def fake_get(url, **kw):
            if "arxiv.org" in url:
                return _mock_response(text='<?xml version="1.0"?><feed></feed>')
            return _mock_response(json_data={
                "data": [{"title": "no date paper", "publicationDate": None, "externalIds": {}}],
            })
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        assert c.collect_recent(days=7) == []

    def test_s2_non_dict_response_is_parse_error(self, tmp_db, monkeypatch):
        def fake_get(url, **kw):
            if "arxiv.org" in url:
                return _mock_response(text='<?xml version="1.0"?><feed></feed>')
            return _mock_response(json_data=["unexpected", "list"])
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, industries_keywords={"HBM": ["HBM"]})
        c.collect_recent(days=7)
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("paper_d4",),
        )
        assert any(r["error_type"] == "parse" for r in rows)


# ═══ 幂等性 ═══

class TestIdempotency:
    def test_persist_twice_only_inserts_once(self, collector, patch_http):
        units = collector.collect_recent(days=7)
        first = collector.persist_batch(units)
        second = collector.persist_batch(units)
        assert first >= 1
        assert second == 0

    def test_run_returns_inserted_count(self, collector, patch_http, tmp_db):
        n = collector.run(days=7)
        assert n >= 1
        total = tmp_db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='D4'"
        )["n"]
        assert total == n


# ═══ 契约再验证 ═══

def test_output_passes_info_unit_v1_contract(collector, patch_http):
    units = collector.collect_recent(days=7)
    for u in units:
        recon = InfoUnitV1(**u.model_dump())
        assert recon.id == u.id
        assert recon.source == "D4"


# ═══ 速率限制 ═══

class TestRateLimit:
    def test_arxiv_rate_limit_between_calls(self, tmp_db, monkeypatch, sleeps_captured):
        """两个关键词 → arXiv 两次调用 → 第二次前 sleep ≥ 3s"""
        def fake_get(url, **kw):
            if "arxiv.org" in url:
                return _mock_response(text='<?xml version="1.0"?><feed></feed>')
            return _mock_response(json_data={"data": []})
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(
            db=tmp_db,
            industries_keywords={"半导体设备": ["lithography", "etching"]},
        )
        c.collect_recent(days=7)

        # 至少一次 sleep 接近 ARXIV_MIN_INTERVAL
        assert any(s >= c.ARXIV_MIN_INTERVAL - 0.1 for s in sleeps_captured)

    def test_arxiv_and_s2_have_independent_timers(self, tmp_db, monkeypatch, sleeps_captured):
        """arXiv 调完紧接调 S2，S2 不应因为 arXiv 的时间戳而 sleep"""
        def fake_get(url, **kw):
            if "arxiv.org" in url:
                return _mock_response(text='<?xml version="1.0"?><feed></feed>')
            return _mock_response(json_data={"data": []})
        monkeypatch.setattr(httpx, "get", fake_get)

        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(
            db=tmp_db,
            industries_keywords={"HBM": ["HBM"]},
        )
        c.collect_recent(days=7)

        # 单关键词：arXiv 首次、S2 首次，都不应 sleep（两个 last_call 都是 0）
        positive = [s for s in sleeps_captured if s > 0]
        assert positive == [], f"unexpected sleeps: {positive}"


# ═══ 实例化 ═══

class TestInstantiation:
    def test_default_keywords_map(self, tmp_db):
        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db)
        assert "半导体设备" in c.industries_keywords
        assert "HBM" in c.industries_keywords

    def test_inherits_both_base_attrs(self, tmp_db):
        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db)
        # Collector
        assert c.SOURCE_CODE == "D4"
        assert c.CREDIBILITY == "参考"
        # BaseAgent
        assert c.name == "paper_d4"
        assert c.MAX_RETRIES == 3

    def test_custom_name(self, tmp_db):
        from infra.data_adapters.arxiv_semantic import PaperCollector
        c = PaperCollector(db=tmp_db, name="custom_paper")
        assert c.name == "custom_paper"
