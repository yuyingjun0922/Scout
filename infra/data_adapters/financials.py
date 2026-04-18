"""
infra/data_adapters/financials.py — A 股财务数据适配器（v1.01-financial-lite）

为 FinancialAgent 提供：
    - 资产负债表（最近年报）
    - 利润表（最近年报）
    - PE-TTM（动态市盈率，spot 表查询）
    - EPS 历史（用于 3 年 CAGR 计算）

仅支持 A 股；KR/US 暂不实现（OpenDART deferred per Phase 2A 决议）。
错误映射：网络/超时 → NetworkError，其它 → ParseError。空数据 → DataMissingError。
"""
import time
from typing import Dict, List, Optional

from agents.base import DataMissingError, NetworkError, ParseError


# A 股代码 → AkShare 资产负债表 / 利润表 期望的 "SH600519" / "SZ000858" 形式
def _to_em_symbol(code: str) -> str:
    """6 位 A 股代码加交易所前缀。沪市 6 开头 → SH，其它 → SZ。"""
    code = str(code).strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"非法 A 股代码: {code!r}")
    return ("SH" if code.startswith("6") else "SZ") + code


def _to_float(val, default: Optional[float] = None) -> Optional[float]:
    """安全转 float。NaN/None/空串 → default。"""
    if val is None:
        return default
    try:
        f = float(val)
        if f != f:  # NaN 自比较为 False
            return default
        return f
    except (TypeError, ValueError):
        return default


class FinancialsAdapter:
    """A 股财务/估值数据获取层。无状态（除节流时间戳和 spot 缓存）。

    用法：
        adapter = FinancialsAdapter()
        snapshot = adapter.fetch_snapshot("600519")
        # snapshot 含：report_period, balance_sheet 字段, income_statement 字段,
        #              pe_ttm, eps_history (List[(period, eps)])
    """

    MIN_INTERVAL_SECONDS: float = 0.5

    # spot 表抓全市场要 ~2min，单批次复用
    _spot_cache = None

    def __init__(self):
        self._last_call_time: float = 0.0

    # ── 公共入口：组装一个 snapshot ──

    def fetch_snapshot(self, code: str) -> Dict:
        """对一只股票拉齐计算 Z'' + PEG 所需的全部字段。

        Returns:
            dict with keys: stock, report_period, total_assets, total_current_assets,
                total_current_liab, total_liabilities, total_equity, retained_earnings,
                ebit, revenue, net_profit, eps_basic, pe_ttm, eps_history (list of dicts)

        Raises:
            DataMissingError: 关键字段缺失（无法算 Z''）
            NetworkError / ParseError: AkShare 调用失败
        """
        bs = self.fetch_balance_sheet(code)
        is_ = self.fetch_income_statement(code)
        pe = self.fetch_pe_ttm(code)
        eps_hist = self.fetch_eps_history(code, years=4)

        # 报表期对齐：取较新的 report_period（通常一致）
        period = bs.get("report_period") or is_.get("report_period")
        if not period:
            raise DataMissingError(f"{code}: report_period missing in both BS/IS")

        return {
            "stock": code,
            "report_period": period,
            # balance sheet
            "total_assets": bs.get("total_assets"),
            "total_current_assets": bs.get("total_current_assets"),
            "total_current_liab": bs.get("total_current_liab"),
            "total_liabilities": bs.get("total_liabilities"),
            "total_equity": bs.get("total_equity"),
            "retained_earnings": bs.get("retained_earnings"),
            # income statement
            "revenue": is_.get("revenue"),
            "net_profit": is_.get("net_profit"),
            "ebit": is_.get("ebit"),
            # market
            "pe_ttm": pe,
            # EPS history (sorted DESC by period)
            "eps_history": eps_hist,
        }

    # ── Balance Sheet ──

    def fetch_balance_sheet(self, code: str) -> Dict:
        """最近一期年报资产负债表关键字段。"""
        import akshare as ak

        self._rate_limit()
        em_symbol = _to_em_symbol(code)
        try:
            df = ak.stock_balance_sheet_by_yearly_em(symbol=em_symbol)
        except (ConnectionError, TimeoutError, OSError) as e:
            raise NetworkError(f"BS network err for {code}: {e}") from e
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "resolve")):
                raise NetworkError(f"BS net-like err for {code}: {e}") from e
            raise ParseError(f"BS call err for {code}: {type(e).__name__}: {e}") from e

        if df is None or len(df) == 0:
            raise DataMissingError(f"BS empty for {code}")

        # AkShare 的 yearly 接口按 REPORT_DATE DESC 返回
        row = df.iloc[0]
        period = self._period_str(row.get("REPORT_DATE"))
        return {
            "report_period": period,
            "total_assets": _to_float(row.get("TOTAL_ASSETS")),
            "total_current_assets": _to_float(row.get("TOTAL_CURRENT_ASSETS")),
            "total_current_liab": _to_float(row.get("TOTAL_CURRENT_LIAB")),
            "total_liabilities": _to_float(row.get("TOTAL_LIABILITIES")),
            "total_equity": _to_float(row.get("TOTAL_EQUITY")),
            # 留存收益用未分配利润 + 盈余公积近似
            "retained_earnings": (
                (_to_float(row.get("UNASSIGN_RPOFIT"), 0.0) or 0.0)
                + (_to_float(row.get("SURPLUS_RESERVE"), 0.0) or 0.0)
            ),
        }

    # ── Income Statement ──

    def fetch_income_statement(self, code: str) -> Dict:
        """最近一期年报利润表关键字段。EBIT = 利润总额 + 财务费用。"""
        import akshare as ak

        self._rate_limit()
        em_symbol = _to_em_symbol(code)
        try:
            df = ak.stock_profit_sheet_by_yearly_em(symbol=em_symbol)
        except (ConnectionError, TimeoutError, OSError) as e:
            raise NetworkError(f"IS network err for {code}: {e}") from e
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "resolve")):
                raise NetworkError(f"IS net-like err for {code}: {e}") from e
            raise ParseError(f"IS call err for {code}: {type(e).__name__}: {e}") from e

        if df is None or len(df) == 0:
            raise DataMissingError(f"IS empty for {code}")

        row = df.iloc[0]
        total_profit = _to_float(row.get("TOTAL_PROFIT"))
        finance_expense = _to_float(row.get("FINANCE_EXPENSE"), 0.0) or 0.0
        ebit = (total_profit + finance_expense) if total_profit is not None else None

        return {
            "report_period": self._period_str(row.get("REPORT_DATE")),
            "revenue": _to_float(row.get("TOTAL_OPERATE_INCOME"))
            or _to_float(row.get("OPERATE_INCOME")),
            "net_profit": _to_float(row.get("PARENT_NETPROFIT"))
            or _to_float(row.get("NETPROFIT")),
            "ebit": ebit,
        }

    # ── PE TTM ──

    def fetch_pe_ttm(self, code: str) -> Optional[float]:
        """从 spot 表取动态市盈率（缓存全市场快照，单次拉取 ~2min）。

        spot 表的 "市盈率-动态" ≈ PE-TTM (动态市盈率, EastMoney convention)。
        这里复用 _spot_cache 避免每只股票都拉一次全市场。
        """
        import akshare as ak

        if FinancialsAdapter._spot_cache is None:
            try:
                self._rate_limit()
                FinancialsAdapter._spot_cache = ak.stock_zh_a_spot_em()
            except (ConnectionError, TimeoutError, OSError) as e:
                raise NetworkError(f"spot_em network err: {e}") from e
            except Exception as e:
                raise ParseError(f"spot_em call err: {type(e).__name__}: {e}") from e

        df = FinancialsAdapter._spot_cache
        if df is None or len(df) == 0:
            return None

        try:
            hit = df[df["代码"] == str(code)]
        except KeyError:
            return None
        if hit.empty:
            return None

        return _to_float(hit.iloc[0].get("市盈率-动态"))

    @classmethod
    def reset_spot_cache(cls) -> None:
        """供测试 / 周任务结束后清缓存，避免下次跑用陈旧报价。"""
        cls._spot_cache = None

    # ── EPS history ──

    def fetch_eps_history(self, code: str, years: int = 4) -> List[Dict]:
        """近 N 年的年度 EPS（摊薄）。返回按报表期 DESC 排序的列表。

        计算 3 年 CAGR 需要 4 个年报点（取首尾两端）。
        """
        import akshare as ak

        self._rate_limit()
        # start_year 给保守一点：当前年-years-1
        from datetime import datetime, timezone

        start_year = str(datetime.now(timezone.utc).year - years - 1)
        try:
            df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
        except (ConnectionError, TimeoutError, OSError) as e:
            raise NetworkError(f"EPS history net err for {code}: {e}") from e
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "resolve")):
                raise NetworkError(f"EPS history net-like err for {code}: {e}") from e
            raise ParseError(f"EPS history call err for {code}: {e}") from e

        if df is None or len(df) == 0:
            return []

        # 仅保留年报（12-31 结尾）
        annual = df[df["日期"].astype(str).str.endswith("12-31")].copy()
        annual = annual.sort_values("日期", ascending=False)

        eps_col = "摊薄每股收益(元)"
        result: List[Dict] = []
        for _, row in annual.iterrows():
            period = str(row["日期"])
            eps = _to_float(row.get(eps_col))
            result.append({"period": period, "eps": eps})
        return result

    # ── 工具 ──

    def _rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.MIN_INTERVAL_SECONDS:
            time.sleep(self.MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_time = time.time()

    @staticmethod
    def _period_str(raw) -> Optional[str]:
        """REPORT_DATE 形如 '2025-12-31 00:00:00' → '2025-12-31'。"""
        if raw is None:
            return None
        s = str(raw)
        return s[:10] if len(s) >= 10 else s
