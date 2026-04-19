"""
infra/data_adapters/akshare_wrapper.py — S4 AkShare 适配器

Phase 1 简化版：抓取一批 A 股种子股票最近 N 天日线数据，
按 (stock, trading_date) 粒度落 info_units。

继承 Collector（提供持久化+幂等 ID）+ BaseAgent（提供错误传播矩阵）。
"""
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from agents.base import (
    BaseAgent,
    DataMissingError,
    NetworkError,
    ParseError,
)
from contracts.contracts import InfoUnitV1
from infra.collector import Collector
from infra.db_manager import DatabaseManager


class AkShareCollector(Collector, BaseAgent):
    """S4 AkShare 采集器（A 股日线 + 粗粒度行业标签）

    子类必要属性：
        SOURCE_CODE = "S4"
        CREDIBILITY = "权威"

    用法：
        c = AkShareCollector(db, symbols=["600519", "300750"])
        inserted = c.run(days=7)
    """

    SOURCE_CODE = "S4"
    CREDIBILITY = "权威"

    # Phase 1 种子股票（用户可通过 __init__ 覆盖）
    DEFAULT_SYMBOLS = [
        "600519",  # 贵州茅台
        "300750",  # 宁德时代
        "000858",  # 五粮液
    ]

    # 极简行业关键词映射（Phase 1 简化，Phase 2A 走 industry_dict 查表）
    INDUSTRY_KEYWORDS = {
        "白酒": ["茅台", "五粮", "洋河", "汾酒"],
        "电池": ["宁德", "比亚迪", "国轩"],
        "半导体": ["中芯", "海光", "韦尔", "紫光", "北方华创"],
        "光伏": ["隆基", "通威", "晶澳", "天合"],
        "CXO": ["药明", "泰格", "康龙"],
    }

    # 股票代码→中文名兜底映射（Phase 1 硬编码，Phase 2A 走数据库）
    NAME_FALLBACK = {
        "600519": "贵州茅台",
        "300750": "宁德时代",
        "000858": "五粮液",
    }

    # AkShare 自身有限流，这里加一层保险：每次调用间隔 ≥ 0.5s
    MIN_INTERVAL_SECONDS: float = 0.5

    def __init__(
        self,
        db: DatabaseManager,
        symbols: Optional[List[str]] = None,
        name: str = "akshare_s4",
    ):
        # 两个基类都有自己的 __init__，显式调用避免 MRO 歧义
        Collector.__init__(self, db=db)
        BaseAgent.__init__(self, name=name, db=db)
        self.symbols = list(symbols) if symbols else list(self.DEFAULT_SYMBOLS)
        self._last_call_time: float = 0.0

    # ── 主入口：完整管道 ──

    def run(self, days: int = 7) -> int:
        """完整管道：采集 → 持久化。返回落库的净增行数。"""
        units = self.collect_recent(days=days)
        if not units:
            return 0
        return self.persist_batch(units)

    def collect_recent(self, days: int = 7) -> List[InfoUnitV1]:
        """对每个 symbol 做错误隔离：单只失败不影响其它。"""
        result: List[InfoUnitV1] = []
        for symbol in self.symbols:
            units = self.run_with_error_handling(
                self._collect_one_symbol, symbol, days
            )
            if units:
                result.extend(units)
        return result

    # ── 单只股票采集 ──

    def _collect_one_symbol(self, symbol: str, days: int) -> List[InfoUnitV1]:
        """抓一只股票近 days 个交易日的日线，转 InfoUnitV1 列表。

        可能抛出：
            NetworkError        - 网络/超时（走重试）
            DataMissingError    - AkShare 返回空（标 insufficient）
            ParseError          - 字段缺失/类型错（保留 raw 在 message）
        """
        self._rate_limit()

        df = self._fetch_daily(symbol, days)
        if df is None or len(df) == 0:
            raise DataMissingError(f"S4 {symbol}: AkShare returned empty")

        stock_name = self._resolve_name(symbol, df)
        industries = self._infer_industries(stock_name)

        tail = df.tail(days)
        units: List[InfoUnitV1] = []
        for _, row in tail.iterrows():
            try:
                units.append(self._row_to_unit(row, symbol, stock_name, industries))
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                # 保留 raw 片段到 error_message（ParseError 契约）
                raw_fragment = str(dict(row))[:500] if hasattr(row, "to_dict") else str(row)[:500]
                raise ParseError(
                    f"S4 {symbol} row parse err: {type(e).__name__}: {e}; raw={raw_fragment}"
                ) from e
        return units

    # ── AkShare 调用 ──

    def _fetch_daily(self, symbol: str, days: int):
        """调 AkShare 日线接口。返回 DataFrame 或 None。

        错误映射：
            网络/超时/OSError  → NetworkError
            其它异常           → ParseError

        2026-04-19 P0：对 3 只种子股票稳定 RemoteDisconnected，
        加 1 次 2 秒背退重试；两次都挂才归类 NetworkError。
        """
        import akshare as ak  # 延迟 import：未安装 akshare 时也能被 mock

        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=max(days * 2, 14))).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")

        last_exc: Optional[BaseException] = None
        for attempt in range(2):
            try:
                return ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust="qfq",
                )
            except (ConnectionError, TimeoutError, OSError) as e:
                last_exc = e
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise NetworkError(
                    f"AkShare network err for {symbol}: {type(e).__name__}: {e}"
                ) from e
            except Exception as e:
                # AkShare 内部异常多样：HTTPError/JSONDecodeError/ValueError 等
                msg = str(e).lower()
                net_like = any(k in msg for k in ("timeout", "connection", "network", "resolve", "disconnect"))
                if net_like and attempt == 0:
                    last_exc = e
                    time.sleep(2)
                    continue
                if net_like:
                    raise NetworkError(f"AkShare net-like err for {symbol}: {e}") from e
                raise ParseError(
                    f"AkShare call err for {symbol}: {type(e).__name__}: {e}"
                ) from e
        # 不可达，防护返回
        if last_exc is not None:
            raise NetworkError(f"AkShare network err for {symbol}: {last_exc}") from last_exc
        return None

    # ── 辅助 ──

    def _rate_limit(self) -> None:
        """强制两次 AkShare 调用之间间隔至少 MIN_INTERVAL_SECONDS 秒。"""
        now = time.time()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.MIN_INTERVAL_SECONDS:
            time.sleep(self.MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_time = time.time()

    def _resolve_name(self, symbol: str, df) -> str:
        """取股票中文名：DataFrame 里若有 '名称' 列用它，否则走兜底表。"""
        if "名称" in getattr(df, "columns", []):
            try:
                name_val = df.iloc[0]["名称"]
                if name_val:
                    return str(name_val)
            except Exception:
                pass
        return self.NAME_FALLBACK.get(symbol, symbol)

    def _infer_industries(self, stock_name: str) -> List[str]:
        """从股票名字关键词粗推行业（Phase 1 简化，Phase 2A 走 industry_dict）"""
        inferred: List[str] = []
        for industry, keywords in self.INDUSTRY_KEYWORDS.items():
            if any(kw in stock_name for kw in keywords):
                inferred.append(industry)
        return inferred

    def _row_to_unit(
        self,
        row,
        symbol: str,
        stock_name: str,
        industries: List[str],
    ) -> InfoUnitV1:
        """一行 DataFrame → InfoUnitV1。"""
        trading_date = self._normalize_date(row["日期"])
        close_price = float(row["收盘"])

        content = json.dumps(
            {
                "symbol": symbol,
                "name": stock_name,
                "trading_date": trading_date,
                "open": float(row["开盘"]),
                "close": close_price,
                "high": float(row["最高"]),
                "low": float(row["最低"]),
                "volume": float(row["成交量"]),
                "pct_change": float(row["涨跌幅"]),
            },
            ensure_ascii=False,
        )

        # A 股 15:00 CST 收盘 = 07:00 UTC；timestamp 取收盘时刻
        timestamp = f"{trading_date}T07:00:00+00:00"
        title = f"{stock_name}({symbol})_{trading_date}"

        return InfoUnitV1(
            id=self.make_info_unit_id(title, trading_date),
            source=self.SOURCE_CODE,
            source_credibility=self.CREDIBILITY,
            timestamp=timestamp,
            category="公司",
            content=content,
            related_industries=industries,
        )

    @staticmethod
    def _normalize_date(raw) -> str:
        """把 pandas.Timestamp / datetime / date / str 统一成 'YYYY-MM-DD'"""
        if isinstance(raw, (date, datetime)):
            return raw.strftime("%Y-%m-%d")
        s = str(raw)
        return s[:10]  # 'YYYY-MM-DD'
