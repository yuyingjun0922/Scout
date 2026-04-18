"""
infra/collector.py — Scout 采集模块基类

所有 5 个信源 adapter (D1/D4/V1/V3/S4) 继承 Collector。
提供幂等写入 info_units（PK=hash(source+标题+日期)，靠 INSERT OR IGNORE 去重）和
统一的 batch 持久化逻辑。

典型用法：
    class D1Collector(Collector):
        SOURCE_CODE = "D1"
        CREDIBILITY = "权威"

        def collect_recent(self, days=7):
            # 抓取 + 解析 → InfoUnitV1 列表（不写 DB）
            ...

    collector = D1Collector(db)
    units = collector.collect_recent(7)
    added = collector.persist_batch(units)
"""
import json
from abc import ABC, abstractmethod
from typing import List

from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from utils.hash_utils import info_unit_id
from utils.time_utils import now_utc


class Collector(ABC):
    """采集模块基类（抽象）。

    子类必须：
        1) 设置类属性 SOURCE_CODE（D1/D4/V1/V3/S4）
        2) 设置类属性 CREDIBILITY（权威/可靠/参考/线索）
        3) 实现 collect_recent(days) -> List[InfoUnitV1]
    """

    # 子类必须覆盖
    SOURCE_CODE: str = ""
    CREDIBILITY: str = ""

    def __init__(self, db: DatabaseManager):
        if not self.SOURCE_CODE:
            raise ValueError(
                f"{type(self).__name__} must set SOURCE_CODE class attr "
                "(e.g. 'D1', 'D4', 'V1', 'V3', 'S4')"
            )
        if not self.CREDIBILITY:
            raise ValueError(
                f"{type(self).__name__} must set CREDIBILITY class attr "
                "(one of: 权威/可靠/参考/线索)"
            )
        if db is None:
            raise ValueError("Collector requires a DatabaseManager")
        self.db = db

    # ── 抽象方法 ──

    @abstractmethod
    def collect_recent(self, days: int = 7) -> List[InfoUnitV1]:
        """抓取最近 N 天的数据，返回 InfoUnitV1 列表。

        实现要点：
            - 网络抓取用 try/except 包成 NetworkError 抛出
            - 解析错用 ParseError 抛出（raw 片段放 error_message 保留）
            - 返回已通过 Pydantic 验证的 InfoUnitV1 列表
            - 不做 DB 写入（交给 persist_batch）
        """
        raise NotImplementedError

    # ── 幂等持久化 ──

    def persist_batch(self, info_units: List[InfoUnitV1]) -> int:
        """幂等写入 info_units 表。

        Returns:
            实际新增行数（重复 id 被 INSERT OR IGNORE 跳过）。
        """
        if not info_units:
            return 0

        # 只数本 collector 的 source，避免受其它 source 已有记录干扰
        before = self.db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source=?",
            (self.SOURCE_CODE,),
        )["n"]

        sql = (
            "INSERT OR IGNORE INTO info_units "
            "(id, source, source_credibility, timestamp, category, content, "
            "related_industries, policy_direction, mixed_subtype, "
            "event_chain_id, schema_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        ts = now_utc()
        for u in info_units:
            self.db.write(
                sql,
                (
                    u.id,
                    u.source,
                    u.source_credibility,
                    u.timestamp,
                    u.category,
                    u.content,
                    json.dumps(u.related_industries, ensure_ascii=False),
                    u.policy_direction,
                    u.mixed_subtype,
                    u.event_chain_id,
                    u.schema_version,
                    ts,
                    ts,
                ),
            )

        after = self.db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source=?",
            (self.SOURCE_CODE,),
        )["n"]
        return after - before

    # ── 幂等 ID ──

    def make_info_unit_id(self, title: str, published_date: str) -> str:
        """对齐 utils.hash_utils.info_unit_id，规范此 collector 的 ID 生成。"""
        return info_unit_id(self.SOURCE_CODE, title, published_date)
