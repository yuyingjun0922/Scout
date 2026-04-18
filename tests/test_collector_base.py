"""
tests/test_collector_base.py — Collector 采集基类测试

覆盖：
    - 抽象约束（Collector 不可实例化；子类必须 collect_recent）
    - SOURCE_CODE / CREDIBILITY 必须配置
    - persist_batch 正常路径：空/单/多/字段完整
    - 幂等：同批重放 0 插入；部分重叠仅插入新的
    - per-source 计数隔离（其它 source 不影响）
    - make_info_unit_id 幂等 + 16-hex
"""
import json

import pytest

from contracts.contracts import InfoUnitV1
from infra.collector import Collector
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database
from utils.time_utils import now_utc


# ═══ fixtures ═══

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "collector_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


class _MockCollector(Collector):
    SOURCE_CODE = "D1"
    CREDIBILITY = "权威"

    def collect_recent(self, days=7):
        return []


def _unit(idx: int, source: str = "D1") -> InfoUnitV1:
    return InfoUnitV1(
        id=f"{idx:016x}",
        source=source,
        source_credibility="权威",
        timestamp="2026-04-17T00:00:00+00:00",
        category="政策",
        content=f"条目{idx}",
        related_industries=["半导体"],
    )


# ═══ 抽象约束 ═══

class TestAbstractConstraints:
    def test_collector_base_cannot_instantiate(self, tmp_db):
        with pytest.raises(TypeError):
            Collector(db=tmp_db)

    def test_subclass_without_collect_recent_cannot_instantiate(self, tmp_db):
        class Incomplete(Collector):
            SOURCE_CODE = "D1"
            CREDIBILITY = "权威"
        with pytest.raises(TypeError):
            Incomplete(db=tmp_db)

    def test_missing_source_code_raises(self, tmp_db):
        class NoCode(Collector):
            CREDIBILITY = "权威"
            def collect_recent(self, days=7):
                return []
        with pytest.raises(ValueError, match="SOURCE_CODE"):
            NoCode(db=tmp_db)

    def test_missing_credibility_raises(self, tmp_db):
        class NoCred(Collector):
            SOURCE_CODE = "D1"
            def collect_recent(self, days=7):
                return []
        with pytest.raises(ValueError, match="CREDIBILITY"):
            NoCred(db=tmp_db)

    def test_none_db_raises(self):
        with pytest.raises(ValueError, match="DatabaseManager"):
            _MockCollector(db=None)

    def test_concrete_subclass_instantiates(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        assert c.SOURCE_CODE == "D1"
        assert c.CREDIBILITY == "权威"
        assert c.db is tmp_db


# ═══ persist_batch 正常路径 ═══

class TestPersistBatchHappyPath:
    def test_empty_list_returns_zero_no_writes(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        assert c.persist_batch([]) == 0
        assert tmp_db.query_one("SELECT COUNT(*) AS n FROM info_units")["n"] == 0

    def test_single_item_persisted(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        assert c.persist_batch([_unit(1)]) == 1
        row = tmp_db.query_one("SELECT * FROM info_units")
        assert row["id"] == f"{1:016x}"
        assert row["source"] == "D1"

    @pytest.mark.parametrize("n", [2, 5, 20])
    def test_multiple_items_count_correct(self, tmp_db, n):
        c = _MockCollector(db=tmp_db)
        units = [_unit(i) for i in range(1, n + 1)]
        assert c.persist_batch(units) == n

    def test_all_fields_persisted(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        unit = InfoUnitV1(
            id="abcdef0123456789",
            source="D1",
            source_credibility="权威",
            timestamp="2026-04-17T00:00:00+00:00",
            category="政策",
            content="国务院发文支持新能源",
            related_industries=["半导体", "光伏"],
            policy_direction="supportive",
            event_chain_id="#E-20260417-AI",
        )
        c.persist_batch([unit])

        row = tmp_db.query_one(
            "SELECT * FROM info_units WHERE id=?", ("abcdef0123456789",),
        )
        assert row["source"] == "D1"
        assert row["source_credibility"] == "权威"
        assert row["timestamp"] == "2026-04-17T00:00:00+00:00"
        assert row["category"] == "政策"
        assert row["content"] == "国务院发文支持新能源"
        assert row["policy_direction"] == "supportive"
        assert row["event_chain_id"] == "#E-20260417-AI"
        assert row["schema_version"] == 1
        assert row["mixed_subtype"] is None
        assert json.loads(row["related_industries"]) == ["半导体", "光伏"]

    def test_created_and_updated_timestamps_present(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        c.persist_batch([_unit(1)])
        row = tmp_db.query_one("SELECT created_at, updated_at FROM info_units")
        assert row["created_at"] is not None
        assert row["updated_at"] is not None
        # UTC-ish check: end with Z or contains +00:00 (our now_utc() uses isoformat)
        assert "+00:00" in row["created_at"] or row["created_at"].endswith("Z")


# ═══ 幂等性 ═══

class TestIdempotency:
    def test_same_batch_twice_only_inserts_once(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        units = [_unit(i) for i in range(1, 4)]

        first = c.persist_batch(units)
        second = c.persist_batch(units)

        assert first == 3
        assert second == 0
        assert tmp_db.query_one("SELECT COUNT(*) AS n FROM info_units")["n"] == 3

    def test_partial_overlap_only_inserts_new(self, tmp_db):
        c = _MockCollector(db=tmp_db)

        batch1 = [_unit(i) for i in range(1, 4)]   # ids 1,2,3
        batch2 = [_unit(i) for i in range(2, 6)]   # ids 2,3,4,5

        c.persist_batch(batch1)
        added = c.persist_batch(batch2)

        assert added == 2  # 4, 5 是新的
        assert tmp_db.query_one("SELECT COUNT(*) AS n FROM info_units")["n"] == 5


# ═══ per-source 计数隔离 ═══

def test_persist_batch_counts_only_own_source(tmp_db):
    """DB 里已有其它 source 的记录时，本 collector 的计数不应被污染"""
    # 手工预置一条 D4 记录
    tmp_db.write(
        """INSERT INTO info_units (id, source, timestamp, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("preexisting_d4_", "D4", "2026-04-17T00:00:00+00:00", now_utc(), now_utc()),
    )

    c = _MockCollector(db=tmp_db)  # SOURCE_CODE = D1
    count = c.persist_batch([_unit(1), _unit(2)])

    assert count == 2  # D1 新增 2 条；不受 D4 干扰
    total = tmp_db.query_one("SELECT COUNT(*) AS n FROM info_units")["n"]
    assert total == 3  # 1 D4 + 2 D1


# ═══ make_info_unit_id ═══

class TestMakeInfoUnitId:
    def test_deterministic_same_input_same_id(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        assert c.make_info_unit_id("标题", "2026-04-17") == c.make_info_unit_id("标题", "2026-04-17")

    def test_different_titles_different_ids(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        assert c.make_info_unit_id("A", "2026-04-17") != c.make_info_unit_id("B", "2026-04-17")

    def test_different_dates_different_ids(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        assert c.make_info_unit_id("A", "2026-04-17") != c.make_info_unit_id("A", "2026-04-18")

    def test_returns_16_hex(self, tmp_db):
        c = _MockCollector(db=tmp_db)
        generated = c.make_info_unit_id("标题", "2026-04-17")
        assert len(generated) == 16
        int(generated, 16)  # 不抛错即为合法 hex

    def test_id_includes_source_code(self, tmp_db):
        """D1 和 D4 对同标题同日期应产出不同 id"""
        class D4Collector(Collector):
            SOURCE_CODE = "D4"
            CREDIBILITY = "参考"
            def collect_recent(self, days=7):
                return []

        c1 = _MockCollector(db=tmp_db)
        c2 = D4Collector(db=tmp_db)
        assert c1.make_info_unit_id("论文", "2026-04-17") != c2.make_info_unit_id("论文", "2026-04-17")
