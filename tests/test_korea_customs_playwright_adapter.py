"""
tests/test_korea_customs_playwright_adapter.py — V3 Playwright 采集器测试 (v1.11)

测试策略（与 korea_customs_adapter.py 相似）:
    - _fetch_table_rows 会真启动 Playwright，mock 成直接返回预制 rows 矩阵
    - _rows_to_units 是纯函数，直接验证行 → InfoUnitV1 契约
    - 真网络 e2e 放到 scripts/test_v3_playwright_real.py（非 pytest）

覆盖:
    - 行 → InfoUnit 转换（正常/HS 过滤/表头跳过/金额解析失败）
    - HS2 → 行业映射
    - 幂等 id
    - DataMissingError 路径（空行/无匹配）
    - NetworkError 路径（Playwright 未装 / 启动失败）
    - persist_batch 集成
    - 速率限制
"""
import time
from pathlib import Path

import pytest

from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from infra.data_adapters.korea_customs_playwright import (
    HS2_INDUSTRY_MAP,
    KoreaCustomsPlaywrightCollector,
)
from knowledge.init_db import init_database


# ═══ fixtures ═══

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "v3_pw_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def sleeps_captured(monkeypatch):
    captured = []
    monkeypatch.setattr("time.sleep", lambda s: captured.append(s))
    yield captured


# 模拟 trade_table.rows 矩阵（对齐真实抓取结果）
# 2 行表头 + 10 行数据
SAMPLE_ROWS = [
    ["", "기간", "품목", "중량", "금액", "무역수지"],            # header row 0 (rowspan)
    ["HS코드", "품목명"],                                       # header row 1
    ["", "2026", "85", "전기기기와 그 부분품", "644,315.7", "88,753,776", "52,752,391"],
    ["", "2026", "84", "원자로・보일러・기계류", "1,056,396.6", "30,454,166", "8,116,748"],
    ["", "2026", "87", "철도용이나 궤도용 외의 차량", "2,024,174.3", "21,742,747", "16,911,657"],
    ["", "2026", "27", "광물성 연료", "17,057,240.5", "13,555,100", "-20,691,889"],
    ["", "2026", "39", "플라스틱과 그 제품", "4,356,068.5", "8,282,269", "5,055,639"],
    ["", "2026", "89", "선박과 수상 구조물", "2,137,972.5", "7,769,508", "7,098,731"],
    ["", "2026", "72", "철강", "6,806,248.4", "5,724,567", "2,906,039"],
    ["", "2026", "29", "유기화학품", "5,002,862.3", "5,131,993", "1,687,049"],
    ["", "2026", "90", "광학기기・측정기기", "44,055.8", "4,040,798", "-2,450,552"],
    ["", "2026", "71", "천연진주・양식진주", "2,153.1", "4,003,410", "-1,212,208"],
]


# ═══ 纯函数 parser 测试 ═══

class TestRowsToUnits:
    def test_filters_only_known_hs2(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        units = coll._rows_to_units(SAMPLE_ROWS)
        # 5 个可映射 HS2: 85, 84, 87, 89, 90
        assert len(units) == 5
        for u in units:
            assert u.source == "V3"
            assert u.source_credibility == "权威"
            assert u.category == "宏观"
            # v1.11: timestamp = snapshot UTC；period 在 content 里
            assert u.timestamp.endswith("+00:00")
            assert '"period": "2026"' in u.content

    def test_skips_header_rows(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        # 只传表头（<7 单元格 or 非数字 period）
        rows_headers_only = SAMPLE_ROWS[:2]
        units = coll._rows_to_units(rows_headers_only)
        assert units == []

    def test_hs85_maps_to_three_industries(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        units = coll._rows_to_units(SAMPLE_ROWS)
        u85 = [u for u in units if "HS85" in u.id or "85" in u.content.split(",")[0]]
        # 更直接: 找 hs2=85 的那条
        hs85 = next(
            (u for u in units if '"hs2": "85"' in u.content), None
        )
        assert hs85 is not None
        assert set(hs85.related_industries) == {"半导体设备", "AI算力", "HBM"}

    def test_hs84_maps_to_semi_equipment_only(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        units = coll._rows_to_units(SAMPLE_ROWS)
        hs84 = next(
            (u for u in units if '"hs2": "84"' in u.content), None
        )
        assert hs84 is not None
        assert hs84.related_industries == ["半导体设备"]

    def test_hs87_maps_to_korea_battery(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        units = coll._rows_to_units(SAMPLE_ROWS)
        hs87 = next(
            (u for u in units if '"hs2": "87"' in u.content), None
        )
        assert hs87 is not None
        assert hs87.related_industries == ["韩国电池"]

    def test_hs89_maps_to_shipbuilding(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        units = coll._rows_to_units(SAMPLE_ROWS)
        hs89 = next(
            (u for u in units if '"hs2": "89"' in u.content), None
        )
        assert hs89 is not None
        assert hs89.related_industries == ["造船海工"]

    def test_amount_parsing_with_commas(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        import json as _json
        units = coll._rows_to_units(SAMPLE_ROWS)
        hs85 = next(u for u in units if '"hs2": "85"' in u.content)
        payload = _json.loads(hs85.content)
        assert payload["export_usd_thousand"] == 88753776.0
        assert payload["weight_kg"] == 644315.7
        assert payload["trade_balance_thousand"] == 52752391.0

    def test_unknown_hs2_ignored(self, tmp_db):
        """HS2=27 (광물 연료) 不在映射表内，应被忽略"""
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        units = coll._rows_to_units(SAMPLE_ROWS)
        hs27 = [u for u in units if '"hs2": "27"' in u.content]
        assert hs27 == []

    def test_malformed_amount_row_skipped(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        bad_rows = [
            ["", "2026", "85", "破折号金额", "-", "--", ""],
        ]
        units = coll._rows_to_units(bad_rows)
        assert units == []

    def test_cell_count_wrong_skipped(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        bad_rows = [
            ["", "2026", "85", "少两列"],
        ]
        units = coll._rows_to_units(bad_rows)
        assert units == []

    def test_dedup_same_period_hs(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        dup_rows = SAMPLE_ROWS + [
            ["", "2026", "85", "duplicate", "1", "2", "3"],
        ]
        units = coll._rows_to_units(dup_rows)
        hs85_units = [u for u in units if '"hs2": "85"' in u.content]
        assert len(hs85_units) == 1

    def test_hs_filter_kwarg(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db, hs_filter=["85"])
        units = coll._rows_to_units(SAMPLE_ROWS)
        assert len(units) == 1
        assert '"hs2": "85"' in units[0].content

    def test_idempotent_id(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        u1 = coll._rows_to_units(SAMPLE_ROWS)
        u2 = coll._rows_to_units(SAMPLE_ROWS)
        ids1 = sorted(u.id for u in u1)
        ids2 = sorted(u.id for u in u2)
        assert ids1 == ids2
        # 且 id 互不相同
        assert len(set(ids1)) == len(ids1)

    def test_all_units_pass_pydantic(self, tmp_db):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        units = coll._rows_to_units(SAMPLE_ROWS)
        for u in units:
            # 重新 model_validate 一下，验证契约
            m = InfoUnitV1.model_validate(u.model_dump())
            assert m.source == "V3"


# ═══ HS2_INDUSTRY_MAP 静态检查 ═══

class TestIndustryMap:
    def test_all_values_are_lists(self):
        for hs2, industries in HS2_INDUSTRY_MAP.items():
            assert isinstance(industries, list)
            assert len(industries) >= 1
            for name in industries:
                assert isinstance(name, str)
                assert name  # 非空

    def test_hs85_has_ai_compute_and_hbm(self):
        assert "AI算力" in HS2_INDUSTRY_MAP["85"]
        assert "HBM" in HS2_INDUSTRY_MAP["85"]
        assert "半导体设备" in HS2_INDUSTRY_MAP["85"]

    def test_semiconductor_equipment_covered_by_multiple_hs(self):
        """半导体设备 应该被多个 HS2 映射（设备/材料/测试）"""
        hs_with_semi = [
            hs2 for hs2, ind in HS2_INDUSTRY_MAP.items() if "半导体设备" in ind
        ]
        assert len(hs_with_semi) >= 2


# ═══ collect_recent + DataMissing 路径 ═══

class TestCollectRecentErrors:
    def test_empty_rows_raises_data_missing(self, tmp_db, monkeypatch):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        monkeypatch.setattr(coll, "_fetch_table_rows", lambda: [])
        from agents.base import DataMissingError
        with pytest.raises(DataMissingError):
            coll.collect_recent()

    def test_no_matching_hs_raises_data_missing(self, tmp_db, monkeypatch):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        # 全是 HS2=71 (珠宝) 等未映射的
        rows_no_match = [
            ["", "2026", "71", "珠宝", "1", "2", "3"],
            ["", "2026", "72", "철강", "1", "2", "3"],
        ]
        monkeypatch.setattr(coll, "_fetch_table_rows", lambda: rows_no_match)
        from agents.base import DataMissingError
        with pytest.raises(DataMissingError):
            coll.collect_recent()

    def test_successful_collect_with_mock(self, tmp_db, monkeypatch):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        monkeypatch.setattr(coll, "_fetch_table_rows", lambda: SAMPLE_ROWS)
        units = coll.collect_recent()
        assert len(units) == 5


class TestRunIntegration:
    def test_run_persists_units(self, tmp_db, monkeypatch):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        monkeypatch.setattr(coll, "_fetch_table_rows", lambda: SAMPLE_ROWS)
        n = coll.run()
        assert n == 5
        row = tmp_db.query_one(
            "SELECT COUNT(*) AS c FROM info_units WHERE source='V3'"
        )
        assert row["c"] == 5

    def test_run_idempotent_second_call(self, tmp_db, monkeypatch):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        monkeypatch.setattr(coll, "_fetch_table_rows", lambda: SAMPLE_ROWS)
        n1 = coll.run()
        n2 = coll.run()
        assert n1 == 5
        assert n2 == 0  # same ids → INSERT OR IGNORE skips
        row = tmp_db.query_one(
            "SELECT COUNT(*) AS c FROM info_units WHERE source='V3'"
        )
        assert row["c"] == 5

    def test_run_with_data_missing_returns_0_via_base_agent(self, tmp_db, monkeypatch):
        """DataMissingError 应被 BaseAgent 捕捉记录，返回 None → run 转换为 0"""
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        monkeypatch.setattr(coll, "_fetch_table_rows", lambda: [])
        n = coll.run()
        assert n == 0
        # 应在 agent_errors 里有一条 DataMissing 记录
        row = tmp_db.query_one(
            "SELECT COUNT(*) AS c FROM agent_errors "
            "WHERE agent_name='korea_customs_v3' AND error_type='data'"
        )
        assert row["c"] >= 1


class TestRateLimit:
    def test_respects_min_interval(self, tmp_db, monkeypatch, sleeps_captured):
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)
        monkeypatch.setattr(coll, "_fetch_table_rows", lambda: SAMPLE_ROWS)
        # 第一次调用不 sleep（_last_call_time=0）
        coll._rate_limit()
        # 强制 _last_call_time 近似 now，模拟紧邻第二次
        coll._last_call_time = time.time() - 1.0
        coll._rate_limit()
        # 应 sleep 约 5 秒（MIN_INTERVAL - 1.0）
        # sleeps_captured 包含所有 time.sleep 的参数；最后一次应 >= 4
        rate_sleeps = [s for s in sleeps_captured if s >= 3]
        assert any(s >= 4.0 for s in rate_sleeps), (
            f"expected sleep >= 4s, got {sleeps_captured}"
        )


class TestNetworkErrorPath:
    def test_playwright_import_error(self, tmp_db, monkeypatch):
        """playwright 未装应转换成 NetworkError（被 BaseAgent 吃掉，run 返 0）"""
        coll = KoreaCustomsPlaywrightCollector(db=tmp_db)

        def _raise_import(*a, **kw):
            from agents.base import NetworkError as _NE
            raise _NE(
                "V3 Playwright: playwright 未安装。执行 "
                "`python -m pip install playwright && python -m playwright install chromium`"
            )

        monkeypatch.setattr(coll, "_fetch_table_rows", _raise_import)
        n = coll.run()
        assert n == 0
        # BaseAgent 会重试 MAX_RETRIES 次，最终 NetworkError 会留在 agent_errors
        row = tmp_db.query_one(
            "SELECT COUNT(*) AS c FROM agent_errors "
            "WHERE agent_name='korea_customs_v3' AND error_type='network'"
        )
        assert row["c"] >= 1
