"""
tests/test_govcn_adapter.py — GovCNCollector (D1) 测试

覆盖：
    - Happy path：API JSON → InfoUnitV1
    - 三个 category (gongwen/bumenfile/gongbao) 合并收集
    - 文号/URL/标题 三级去重键
    - 同文件多关键词 → keyword_hits 合并 + industries 并集
    - 发文机关识别：puborg 优先；puborg 空时从 pcode 前缀兜底
    - 日期 cutoff：pubtime < cutoff 被剔
    - 字段规范化：pubtimeStr / pcode 空 / childtype 拆分
    - HTTP 错误分类
    - 网络重试
    - 契约验证
    - 速率限制
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══ fixtures ═══

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "govcn_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def sleeps_captured(monkeypatch):
    captured = []
    monkeypatch.setattr("time.sleep", lambda s: captured.append(s))
    return captured


def _now_minus_days_ms(days: float) -> int:
    return int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)


def _make_item(
    title: str,
    pcode: str = "",
    puborg: str = "",
    days_ago: float = 1,
    url: str = "",
    summary: str = "简要内容",
    childtype: str = "",
    ptime_days_ago: float = None,
) -> dict:
    """构造一条 API item（按实际响应字段）"""
    pubtime_ms = _now_minus_days_ms(days_ago)
    ptime_ms = (
        _now_minus_days_ms(ptime_days_ago) if ptime_days_ago is not None else pubtime_ms
    )
    return {
        "title": title,
        "pcode": pcode,
        "puborg": puborg,
        "pubtimeStr": datetime.fromtimestamp(pubtime_ms / 1000, tz=timezone.utc).strftime("%Y.%m.%d"),
        "pubtime": pubtime_ms,
        "ptime": ptime_ms,
        "url": url,
        "summary": summary,
        "childtype": childtype,
        "id": "00000000",
    }


def _make_response(gongwen=None, bumenfile=None, gongbao=None, otherfile=None) -> dict:
    """构造完整 API JSON 响应"""
    cat_map = {}
    for cat, items in [
        ("gongwen", gongwen),
        ("bumenfile", bumenfile),
        ("gongbao", gongbao),
        ("otherfile", otherfile),
    ]:
        if items is not None:
            cat_map[cat] = {"listVO": items}
    return {
        "code": 200,
        "msg": "操作成功",
        "data": None,
        "searchVO": {
            "totalCount": sum(len(v or []) for v in [gongwen, bumenfile, gongbao, otherfile]),
            "catMap": cat_map,
        },
    }


def _mock_httpx_response(json_data: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data)
    resp.text = json.dumps(json_data, ensure_ascii=False) if json_data else ""
    return resp


@pytest.fixture
def default_mock_response():
    """默认 mock：每个 category 1-2 条与"半导体"相关的假数据"""
    return _make_response(
        gongwen=[
            _make_item(
                title="关于促进集成电路产业高质量发展的若干意见",
                pcode="国办发〔2026〕12号",
                puborg="国务院办公厅",
                days_ago=2,
                url="https://www.gov.cn/zhengce/zhengceku/202604/content_7000001.htm",
                summary="为促进集成电路产业高质量发展，推动半导体产业链...",
                childtype="工业、交通\\电子信息",
            ),
        ],
        bumenfile=[
            _make_item(
                title="工业和信息化部关于半导体材料专项的通知",
                pcode="工信部电子〔2026〕5号",
                puborg="工业和信息化部",
                days_ago=3,
                url="https://www.gov.cn/zhengce/zhengceku/202604/content_7000002.htm",
                summary="为推进半导体材料产业升级...",
                childtype="工业、交通\\电子信息",
            ),
        ],
        gongbao=[
            _make_item(
                title="国务院关于新一代人工智能发展规划的通知",
                pcode="国发〔2026〕7号",
                puborg="国务院",
                days_ago=5,
                url="https://www.gov.cn/gongbao/2026/issue_12666/content_7000003.html",
                summary="推动人工智能技术与实体经济深度融合...",
                childtype="科技、教育\\科学技术",
            ),
        ],
    )


@pytest.fixture
def patched_httpx(monkeypatch, default_mock_response):
    def fake_get(url, params=None, **kw):
        return _mock_httpx_response(default_mock_response)
    monkeypatch.setattr(httpx, "get", fake_get)
    return fake_get


@pytest.fixture
def collector(tmp_db):
    from infra.data_adapters.gov_cn import GovCNCollector
    return GovCNCollector(db=tmp_db, keywords=["半导体"])


# ═══ Happy path ═══

class TestHappyPath:
    def test_collect_returns_info_units(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        assert len(units) >= 1
        for u in units:
            assert isinstance(u, InfoUnitV1)
            assert u.source == "D1"
            assert u.source_credibility == "权威"
            assert u.category == "政策"
            assert u.policy_direction is None  # Phase 1 规则

    def test_content_spec_fields_present(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        for u in units:
            c = json.loads(u.content)
            for key in (
                "title", "publisher", "doc_number",
                "issued_date", "published_date",
                "subject", "url", "summary",
                "keyword_hits", "source_category",
            ):
                assert key in c, f"missing key: {key}"

    def test_three_categories_collected_by_default(self, collector, patched_httpx):
        """default=gongwen+bumenfile+gongbao → 3 条"""
        units = collector.collect_recent(days=7)
        cats = {json.loads(u.content)["source_category"] for u in units}
        assert cats == {"gongwen", "bumenfile", "gongbao"}

    def test_otherfile_excluded_by_default(self, tmp_db, monkeypatch):
        payload = _make_response(
            gongwen=[_make_item(title="policy A", pcode="国发〔2026〕1号", days_ago=1)],
            otherfile=[_make_item(title="news interpretation", pcode="", days_ago=1)],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))

        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        titles = [json.loads(u.content)["title"] for u in units]
        assert "policy A" in titles
        assert "news interpretation" not in titles

    def test_otherfile_included_when_requested(self, tmp_db, monkeypatch):
        payload = _make_response(
            gongwen=[_make_item(title="policy A", pcode="国发〔2026〕1号", days_ago=1)],
            otherfile=[_make_item(title="news interpretation", pcode="", days_ago=1)],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))

        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(
            db=tmp_db,
            keywords=["半导体"],
            categories=["gongwen", "bumenfile", "gongbao", "otherfile"],
        )
        units = c.collect_recent(days=7)
        titles = [json.loads(u.content)["title"] for u in units]
        assert "news interpretation" in titles

    def test_id_is_16_hex(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        for u in units:
            assert len(u.id) == 16
            int(u.id, 16)

    def test_timestamp_utc_iso8601(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        for u in units:
            assert u.timestamp.endswith("+00:00") or u.timestamp.endswith("Z")

    def test_keyword_hits_populated(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        for u in units:
            hits = json.loads(u.content)["keyword_hits"]
            assert "半导体" in hits

    def test_industries_mapped_from_keyword(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        for u in units:
            assert set(u.related_industries) == {"半导体设备", "AI算力"}

    def test_subject_extracted_from_childtype(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        subjects = {json.loads(u.content)["subject"] for u in units}
        # '工业、交通\\电子信息' → '工业、交通'
        assert "工业、交通" in subjects


# ═══ 去重合并 ═══

class TestDedupAndMerge:
    def test_same_doc_number_dedup_single_keyword(self, tmp_db, monkeypatch):
        """gongwen + bumenfile 返回同一 pcode → 只保留一条"""
        shared_pcode = "国办发〔2026〕12号"
        payload = _make_response(
            gongwen=[_make_item(title="dup A", pcode=shared_pcode, puborg="国务院办公厅", days_ago=1)],
            bumenfile=[_make_item(title="dup A (different title)", pcode=shared_pcode, days_ago=1)],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))

        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        assert len(units) == 1

    def test_same_doc_across_keywords_merges_hits(self, tmp_db, monkeypatch):
        """同一 pcode 被不同关键词命中 → keyword_hits 合并"""
        shared = "国办发〔2026〕12号"
        payload = _make_response(
            gongwen=[_make_item(title="AI chip policy", pcode=shared, puborg="国务院办公厅", days_ago=1)],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))

        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体", "芯片", "人工智能"])
        units = c.collect_recent(days=7)

        assert len(units) == 1
        hits = set(json.loads(units[0].content)["keyword_hits"])
        assert hits == {"半导体", "芯片", "人工智能"}
        # industries 并集：半导体设备 ∪ AI算力
        assert set(units[0].related_industries) == {"半导体设备", "AI算力"}

    def test_dedup_fallback_url_when_no_pcode(self, tmp_db, monkeypatch):
        """otherfile/gongbao 常无 pcode → url 作去重键"""
        shared_url = "https://www.gov.cn/some/doc.htm"
        payload = _make_response(
            gongbao=[_make_item(title="doc with no pcode", pcode="", url=shared_url, days_ago=1)],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))

        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体", "芯片"])
        units = c.collect_recent(days=7)
        assert len(units) == 1


# ═══ 发文机关识别 ═══

class TestPublisherDetection:
    def test_puborg_used_when_present(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        pubs = {json.loads(u.content)["publisher"] for u in units}
        assert "国务院办公厅" in pubs
        assert "工业和信息化部" in pubs

    @pytest.mark.parametrize(
        "pcode,expected",
        [
            ("国发〔2026〕1号", "国务院"),
            ("国办发〔2026〕12号", "国务院办公厅"),
            ("国办函〔2026〕14号", "国务院办公厅"),
            ("国函〔2026〕5号", "国务院"),
            ("国令〔2026〕1号", "国务院"),
            ("国办发明电〔2026〕2号", "国务院办公厅"),
            ("工信部电子〔2026〕5号", "工业和信息化部"),
            ("工信部令第 12号", "工业和信息化部"),
            ("发改能源〔2026〕100号", "国家发展和改革委员会"),
            ("财办建〔2026〕14号", "财政部办公厅"),
            ("财税〔2026〕3号", "财政部"),
        ],
    )
    def test_publisher_inferred_from_pcode(self, pcode, expected):
        from infra.data_adapters.gov_cn import GovCNCollector
        assert GovCNCollector._infer_publisher_from_pcode(pcode) == expected

    def test_unknown_pcode_returns_none(self):
        from infra.data_adapters.gov_cn import GovCNCollector
        assert GovCNCollector._infer_publisher_from_pcode("XXYY〔2026〕1号") is None
        assert GovCNCollector._infer_publisher_from_pcode(None) is None
        assert GovCNCollector._infer_publisher_from_pcode("") is None

    def test_publisher_fallback_when_puborg_empty(self, tmp_db, monkeypatch):
        """puborg 空但 pcode 在 → 兜底识别"""
        payload = _make_response(
            gongwen=[_make_item(title="X", pcode="国办发〔2026〕99号", puborg="", days_ago=1)],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        assert json.loads(units[0].content)["publisher"] == "国务院办公厅"

    def test_publisher_none_when_no_puborg_no_pcode(self, tmp_db, monkeypatch):
        payload = _make_response(
            gongbao=[_make_item(
                title="X", pcode="", puborg="",
                url="https://www.gov.cn/x", days_ago=1,
            )],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        assert json.loads(units[0].content)["publisher"] is None


# ═══ 日期截断 ═══

class TestDateCutoff:
    def test_old_items_filtered(self, tmp_db, monkeypatch):
        payload = _make_response(
            gongwen=[
                _make_item(title="recent", pcode="国发〔2026〕1号", days_ago=2),
                _make_item(title="old", pcode="国发〔2025〕99号", days_ago=60),
            ],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        titles = {json.loads(u.content)["title"] for u in units}
        assert titles == {"recent"}

    def test_issued_date_uses_ptime_when_present(self, tmp_db, monkeypatch):
        """ptime<pubtime：issued_date(成文) ≤ published_date(发布)"""
        payload = _make_response(
            gongwen=[_make_item(
                title="X", pcode="国发〔2026〕1号",
                days_ago=2,         # published 2 天前
                ptime_days_ago=5,   # issued 5 天前
            )],
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        c0 = json.loads(units[0].content)
        assert c0["issued_date"] < c0["published_date"]

    def test_issued_date_falls_back_to_published(self, tmp_db, monkeypatch):
        """ptime=0：issued_date == published_date"""
        payload = _make_response(
            gongwen=[_make_item(
                title="X", pcode="国发〔2026〕1号",
                days_ago=2, ptime_days_ago=0,
            )],
        )
        # 手动把 ptime 设 0
        payload["searchVO"]["catMap"]["gongwen"]["listVO"][0]["ptime"] = 0
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        c0 = json.loads(units[0].content)
        assert c0["issued_date"] == c0["published_date"]


# ═══ 幂等性 ═══

class TestIdempotency:
    def test_persist_twice_only_inserts_once(self, collector, patched_httpx):
        units = collector.collect_recent(days=7)
        first = collector.persist_batch(units)
        second = collector.persist_batch(units)
        assert first >= 1
        assert second == 0

    def test_id_uses_doc_number_when_present(self, tmp_db):
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db)
        id1 = c._make_policy_id("国办发〔2026〕12号", "2026-04-15")
        id2 = c._make_policy_id("国办发〔2026〕12号", "2026-04-15")
        assert id1 == id2

    def test_id_differs_per_doc(self, tmp_db):
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db)
        id1 = c._make_policy_id("国办发〔2026〕12号", "2026-04-15")
        id2 = c._make_policy_id("国办发〔2026〕13号", "2026-04-15")
        assert id1 != id2


# ═══ 异常处理 ═══

class TestErrorHandling:
    def test_timeout_is_network_error(self, tmp_db, monkeypatch):
        def raise_timeout(*a, **kw):
            raise httpx.TimeoutException("timed out")
        monkeypatch.setattr(httpx, "get", raise_timeout)
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        c.collect_recent(days=7)
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("govcn_d1",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_5xx_and_429_classified_as_network(self, tmp_db, monkeypatch, status):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(None, status=status))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        c.collect_recent(days=7)
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("govcn_d1",),
        )
        assert any(r["error_type"] == "network" for r in rows)

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_4xx_classified_as_parse(self, tmp_db, monkeypatch, status):
        def fake_get(*a, **k):
            resp = MagicMock()
            resp.status_code = status
            resp.text = "not found"
            resp.json = MagicMock(side_effect=ValueError("no json"))
            return resp
        monkeypatch.setattr(httpx, "get", fake_get)
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        c.collect_recent(days=7)
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("govcn_d1",),
        )
        assert any(r["error_type"] == "parse" for r in rows)

    def test_json_decode_error_is_parse(self, tmp_db, monkeypatch):
        def fake_get(*a, **k):
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "<html>not json</html>"
            resp.json = MagicMock(side_effect=ValueError("bad json"))
            return resp
        monkeypatch.setattr(httpx, "get", fake_get)
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        c.collect_recent(days=7)
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("govcn_d1",),
        )
        parse_rows = [r for r in rows if r["error_type"] == "parse"]
        assert parse_rows

    def test_empty_catmap_ok(self, tmp_db, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(_make_response()))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        assert c.collect_recent(days=7) == []

    def test_malformed_item_skipped(self, tmp_db, monkeypatch):
        """缺 title 的 item 应 skip，不影响好的 item"""
        payload = _make_response(gongwen=[
            {"title": "", "pcode": "", "pubtime": 0},  # 完全空
            _make_item(title="good", pcode="国发〔2026〕1号", days_ago=1),
        ])
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        assert len(units) == 1
        assert json.loads(units[0].content)["title"] == "good"


# ═══ 契约 ═══

def test_output_passes_info_unit_v1_contract(collector, patched_httpx):
    units = collector.collect_recent(days=7)
    for u in units:
        recon = InfoUnitV1(**u.model_dump())
        assert recon.id == u.id
        assert recon.source == "D1"


# ═══ HTML 清洗 ═══

class TestCleanText:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("政府工作报告<br/>—2026年3月5日", "政府工作报告 —2026年3月5日"),
            ("支持<em>芯片</em>自主研发", "支持芯片自主研发"),
            ("plain text", "plain text"),
            ("", ""),
            ("  multiple   spaces  ", "multiple spaces"),
            ("<br/><br/><em>only tags</em>", "only tags"),
            ("国务院办公厅　关于", "国务院办公厅 关于"),  # 全角空格也折叠
        ],
    )
    def test_clean(self, raw, expected):
        from infra.data_adapters.gov_cn import GovCNCollector
        assert GovCNCollector._clean_text(raw) == expected

    def test_title_html_stripped_in_parsed_output(self, tmp_db, monkeypatch):
        payload = _make_response(gongwen=[
            _make_item(
                title="政府工作报告<br/>——2026年3月5日",
                pcode="国发〔2026〕1号",
                days_ago=1,
                summary="新质生产力发展 <em>芯片</em> 自主研发有新突破",
            ),
        ])
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(payload))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体"])
        units = c.collect_recent(days=7)
        c0 = json.loads(units[0].content)
        assert "<br" not in c0["title"]
        assert "<em>" not in c0["summary"]
        assert "芯片" in c0["summary"]  # 内容保留


# ═══ pubtimeStr 回退解析 ═══

class TestPubTimeStrFallback:
    @pytest.mark.parametrize(
        "s,expect_nonzero",
        [
            ("2026.04.17", True),
            ("2026-04-17", True),
            ("2026/04/17", True),
            ("", False),
            ("garbage", False),
            ("2026.13.40", False),  # 非法月日
        ],
    )
    def test_parse(self, s, expect_nonzero):
        from infra.data_adapters.gov_cn import GovCNCollector
        ms = GovCNCollector._parse_pubtimestr_to_ms(s)
        if expect_nonzero:
            assert ms > 0
        else:
            assert ms == 0


# ═══ 速率限制 ═══

class TestRateLimit:
    def test_sleep_between_keywords(self, tmp_db, monkeypatch, sleeps_captured, default_mock_response):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_httpx_response(default_mock_response))
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, keywords=["半导体", "芯片", "算力"])
        c.collect_recent(days=7)
        positive = [s for s in sleeps_captured if s > 0]
        assert len(positive) >= 2  # 3 个关键词间至少 2 次 rate-limit sleep

    def test_min_interval_1_5s(self, collector):
        assert collector.MIN_INTERVAL_SECONDS == 1.5


# ═══ 实例化 ═══

class TestInstantiation:
    def test_default_keywords(self, tmp_db):
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db)
        assert "半导体" in c.keywords
        assert "HBM" in c.keywords
        assert len(c.keywords) == len(GovCNCollector.DEFAULT_KEYWORDS)

    def test_default_categories(self, tmp_db):
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db)
        assert c.categories == ["gongwen", "bumenfile", "gongbao"]

    def test_custom_categories(self, tmp_db):
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db, categories=["gongwen"])
        assert c.categories == ["gongwen"]

    def test_unknown_category_raises(self, tmp_db):
        from infra.data_adapters.gov_cn import GovCNCollector
        with pytest.raises(ValueError, match="Unknown categories"):
            GovCNCollector(db=tmp_db, categories=["bogus"])

    def test_inherits_base_agent(self, tmp_db):
        from infra.data_adapters.gov_cn import GovCNCollector
        c = GovCNCollector(db=tmp_db)
        assert c.name == "govcn_d1"
        assert c.MAX_RETRIES == 3
