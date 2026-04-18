"""
infra/data_adapters/nbs.py — V1 国家统计局采集器

Phase 1 简化：只采 3 个核心宏观指标，通过 AkShare 封装：
    - PMI  (制造业采购经理指数)  →  ak.macro_china_pmi()
    - 社融 (社会融资规模)        →  ak.macro_china_shrzgm()
    - M2   (货币供应量 M2 同比)  →  ak.macro_china_m2_yearly()

不采 GDP/CPI/PPI（用户明确不看）。

AkShare 列名不稳定：每个指标配 period_patterns / value_patterns 列表，
精确匹配优先、子串回退。找不到就抛 ParseError 让异常可见。
"""
import json
import re
import time
from typing import Dict, List, Optional

from agents.base import (
    BaseAgent,
    DataMissingError,
    NetworkError,
    ParseError,
)
from contracts.contracts import InfoUnitV1
from infra.collector import Collector
from infra.db_manager import DatabaseManager
from utils.hash_utils import info_unit_id


class NBSCollector(Collector, BaseAgent):
    """V1 国家统计局（National Bureau of Statistics）采集器"""

    SOURCE_CODE = "V1"
    CREDIBILITY = "权威"

    # 每个指标的抓取 + 解析配置
    # - ak_func        : ak 模块上的函数名（延迟绑定，允许 monkeypatch）
    # - period_patterns: 周期列名候选（AkShare 列名历史不稳定）
    # - value_patterns : 主值列名候选
    # - value_kind     : 值的物理意义（index / amount_yi_yuan / yoy_pct），给下游阅读用
    INDICATORS: Dict[str, Dict] = {
        "PMI": {
            "ak_func": "macro_china_pmi",
            "period_patterns": ["月份", "日期"],
            "value_patterns": ["制造业-指数", "制造业PMI", "今值", "值"],
            "value_kind": "index",
        },
        "社融": {
            "ak_func": "macro_china_shrzgm",
            "period_patterns": ["月份", "日期"],
            "value_patterns": [
                "社会融资规模增量",
                "增量",
                "社会融资规模",
                "今值",
            ],
            "value_kind": "amount_yi_yuan",  # 亿元
        },
        "M2": {
            "ak_func": "macro_china_m2_yearly",
            "period_patterns": ["日期", "月份"],
            "value_patterns": ["今值", "M2同比", "值"],
            "value_kind": "yoy_pct",
        },
    }

    MIN_INTERVAL_SECONDS: float = 3.0

    def __init__(
        self,
        db: DatabaseManager,
        indicators: Optional[List[str]] = None,
        name: str = "nbs_v1",
    ):
        Collector.__init__(self, db=db)
        BaseAgent.__init__(self, name=name, db=db)

        requested = list(indicators) if indicators else list(self.INDICATORS.keys())
        unknown = [i for i in requested if i not in self.INDICATORS]
        if unknown:
            raise ValueError(
                f"Unknown indicators: {unknown}; "
                f"supported: {list(self.INDICATORS.keys())}"
            )
        self.indicators = requested
        self._last_call_time: float = 0.0

    # ── 主入口 ──

    def run(self, months: int = 6) -> int:
        units = self.collect_recent(months=months)
        return self.persist_batch(units) if units else 0

    def collect_recent(self, months: int = 6) -> List[InfoUnitV1]:
        """对每个配置指标独立采集并错误隔离：一项失败不影响另两项。"""
        result: List[InfoUnitV1] = []
        for indicator in self.indicators:
            units = self.run_with_error_handling(
                self._collect_one_indicator, indicator, months
            )
            if units:
                result.extend(units)
        return result

    # ── 单指标采集 ──

    def _collect_one_indicator(
        self, indicator: str, months: int
    ) -> List[InfoUnitV1]:
        """抓取一个指标、解析、切最近 months 个月 → InfoUnitV1 列表。

        可能抛出：
            NetworkError      - AkShare 网络错
            DataMissingError  - AkShare 返回空 / 无可用行
            ParseError        - 列缺失 / 数据格式不识别
        """
        self._rate_limit()

        df = self._fetch_indicator(indicator)
        if df is None or len(df) == 0:
            raise DataMissingError(f"V1 {indicator}: AkShare returned empty")

        return self._parse_indicator_df(indicator, df, months)

    def _fetch_indicator(self, indicator: str):
        """调 AkShare 对应函数（延迟导入以便 monkeypatch）"""
        import akshare as ak  # lazy

        conf = self.INDICATORS[indicator]
        func_name = conf["ak_func"]
        func = getattr(ak, func_name, None)
        if func is None:
            raise ParseError(
                f"V1 {indicator}: AkShare module has no function {func_name!r}"
                f" (akshare 版本不兼容?)"
            )

        try:
            return func()
        except (ConnectionError, TimeoutError, OSError) as e:
            raise NetworkError(
                f"V1 {indicator} network: {type(e).__name__}: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "resolve")):
                raise NetworkError(f"V1 {indicator} net-like: {e}") from e
            raise ParseError(
                f"V1 {indicator}: {type(e).__name__}: {e}"
            ) from e

    # ── 解析 ──

    def _parse_indicator_df(
        self, indicator: str, df, months: int
    ) -> List[InfoUnitV1]:
        conf = self.INDICATORS[indicator]
        period_col = self._find_column(df, conf["period_patterns"])
        value_col = self._find_column(df, conf["value_patterns"])

        if period_col is None:
            raise ParseError(
                f"V1 {indicator}: cannot find period column; "
                f"tried {conf['period_patterns']}; got columns={list(df.columns)}"
            )
        if value_col is None:
            raise ParseError(
                f"V1 {indicator}: cannot find value column; "
                f"tried {conf['value_patterns']}; got columns={list(df.columns)}"
            )

        rows: List[tuple] = []
        for _, row in df.iterrows():
            period = self._normalize_period(row[period_col])
            if not period:
                continue
            val_raw = row[value_col]
            try:
                value = float(val_raw)
            except (ValueError, TypeError):
                continue
            if value != value:  # NaN
                continue
            rows.append((period, value))

        if not rows:
            raise DataMissingError(
                f"V1 {indicator}: no usable rows in {len(df)} raw rows "
                f"(period_col={period_col!r}, value_col={value_col!r})"
            )

        # 升序排；取最近 months 条
        rows.sort(key=lambda x: x[0])
        rows = rows[-months:]

        units: List[InfoUnitV1] = []
        prev_val: Optional[float] = None
        for period, value in rows:
            mom_change = (value - prev_val) if prev_val is not None else None
            units.append(
                self._make_unit(indicator, period, value, mom_change, conf["value_kind"])
            )
            prev_val = value
        return units

    # ── 辅助 ──

    @staticmethod
    def _find_column(df, patterns: List[str]) -> Optional[str]:
        """按 patterns 顺序匹配列名：先精确，再子串回退。"""
        cols = list(df.columns)
        for p in patterns:
            if p in cols:
                return p
        for p in patterns:
            for c in cols:
                if p in str(c):
                    return c
        return None

    @staticmethod
    def _normalize_period(raw) -> Optional[str]:
        """把 'YYYY年MM月份' / 'YYYY-MM' / 'YYYY-MM-DD' / pd.Timestamp 统一成 'YYYY-MM'"""
        if hasattr(raw, "strftime"):
            try:
                return raw.strftime("%Y-%m")
            except Exception:  # noqa: BLE001
                pass
        s = str(raw).strip()
        m = re.match(r"(\d{4})[年\-\.\/]?\s*(\d{1,2})", s)
        if m:
            year, month = m.groups()
            month_i = int(month)
            if 1 <= month_i <= 12:
                return f"{year}-{month_i:02d}"
        return None

    def _make_unit(
        self,
        indicator: str,
        period: str,
        value: float,
        mom_change: Optional[float],
        value_kind: str,
    ) -> InfoUnitV1:
        content = json.dumps(
            {
                "indicator": indicator,
                "value": value,
                "value_kind": value_kind,
                "period": period,
                "mom_change": mom_change,
            },
            ensure_ascii=False,
        )
        # 月度数据用 period-01 作为 UTC 午夜
        timestamp = f"{period}-01T00:00:00+00:00"
        return InfoUnitV1(
            id=self._make_nbs_id(indicator, period),
            source=self.SOURCE_CODE,
            source_credibility=self.CREDIBILITY,
            timestamp=timestamp,
            category="宏观",
            content=content,
            related_industries=[],  # 宏观不关联具体行业；Phase 2A 升级
        )

    def _make_nbs_id(self, indicator: str, period: str) -> str:
        """id = hash(V1 + indicator + period)；同一 (indicator, period) 全局唯一"""
        return info_unit_id(self.SOURCE_CODE, indicator, period)

    # ── 限流 ──

    def _rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.MIN_INTERVAL_SECONDS:
            time.sleep(self.MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_time = time.time()
