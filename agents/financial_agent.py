"""
agents/financial_agent.py — 财务一体化 Agent（v1.01-financial-lite）

职责：
  1. 从 related_stocks 拉取宇宙（confidence != 'staging' 且 market = 'A'）
  2. 通过 FinancialsAdapter 抓取最近年报 + PE-TTM + EPS 历史
  3. 计算 Z''-1995（新兴市场版）+ PEG（PE-TTM / EPS 3年 CAGR）
  4. 落库到 stock_financials（按 stock + report_period upsert）

失败策略：单只失败不阻塞批次；错误落 agent_errors，stock_financials 跳过
（GateAgent.calculate_s4_score 找不到行就给 0，不扣分）。
"""
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

if __name__ == "__main__":
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

from agents.base import BaseAgent, DataMissingError
from infra.data_adapters.financials import FinancialsAdapter
from infra.db_manager import DatabaseManager


# ── Z''-1995 边界（新兴市场版）──
Z_DOUBLE_PRIME_SAFE = 2.60
Z_DOUBLE_PRIME_DISTRESS = 1.10


@dataclass
class FinancialSnapshot:
    """计算后的单只股票财务快照"""
    stock: str
    report_period: str
    revenue: Optional[float] = None
    net_profit: Optional[float] = None
    z_score: Optional[float] = None        # Z''-1995
    pe_ttm: Optional[float] = None
    eps_cagr_3y: Optional[float] = None    # decimal e.g. 0.20 = 20%
    peg_ratio: Optional[float] = None


class FinancialAgent(BaseAgent):
    """A 股财务一体化批处理 Agent"""

    def __init__(
        self,
        db: Union[str, DatabaseManager],
        adapter: Optional[FinancialsAdapter] = None,
    ):
        if isinstance(db, str):
            self.db_path = db
            db_manager = DatabaseManager(db)
        else:
            self.db_path = db.db_path
            db_manager = db
        super().__init__(name="financial_agent", db=db_manager)
        self.adapter = adapter or FinancialsAdapter()

    # ── 主入口 ──

    def run(self) -> Optional[dict]:
        """周任务入口。返回 {processed, succeeded, failed, skipped} 或 None。"""
        return self.run_with_error_handling(self._run_batch)

    def _run_batch(self) -> dict:
        universe = self.load_universe()
        self.logger.info(f"FinancialAgent batch: {len(universe)} A-share stocks")

        succeeded = 0
        failed = 0
        for code in universe:
            try:
                snap = self._process_one(code)
                if snap is None:
                    failed += 1
                else:
                    self.upsert_snapshot(snap)
                    succeeded += 1
            except Exception as e:
                # 单只 unknown 错误不重新抛——批处理不能因为一只挂掉
                self._log_error('unknown', f"{type(e).__name__}: {e}", f"_process_one({code})")
                failed += 1

        # 释放 spot 缓存
        FinancialsAdapter.reset_spot_cache()

        result = {
            "processed": len(universe),
            "succeeded": succeeded,
            "failed": failed,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.logger.info(f"FinancialAgent done: {result}")
        return result

    # ── 宇宙加载 ──

    def load_universe(self) -> List[str]:
        """从 related_stocks 取 A 股、非 staging 的 stock_code 列表（去重）。"""
        try:
            rows = self.db.query(
                """
                SELECT DISTINCT stock_code
                FROM related_stocks
                WHERE market = 'A'
                  AND confidence != 'staging'
                  AND status = 'active'
                  AND stock_code IS NOT NULL
                  AND length(stock_code) = 6
                """
            )
            return [r["stock_code"] for r in rows]
        except sqlite3.Error as e:
            self.logger.error(f"load_universe DB error: {e}")
            return []

    # ── 单只处理 ──

    def _process_one(self, code: str) -> Optional[FinancialSnapshot]:
        """对一只股票拉数据 + 算 Z'' + PEG。

        失败（任何 BaseAgent 6 类）走 run_with_error_handling 路径返 None。
        """
        return self.run_with_error_handling(self._fetch_and_compute, code)

    def _fetch_and_compute(self, code: str) -> FinancialSnapshot:
        raw = self.adapter.fetch_snapshot(code)

        z_score = self.compute_z_double_prime(raw)
        eps_cagr = self.compute_eps_cagr_3y(raw.get("eps_history") or [])
        peg = self.compute_peg(raw.get("pe_ttm"), eps_cagr)

        return FinancialSnapshot(
            stock=code,
            report_period=raw["report_period"],
            revenue=raw.get("revenue"),
            net_profit=raw.get("net_profit"),
            z_score=z_score,
            pe_ttm=raw.get("pe_ttm"),
            eps_cagr_3y=eps_cagr,
            peg_ratio=peg,
        )

    # ── 计算公式 ──

    @staticmethod
    def compute_z_double_prime(raw: dict) -> Optional[float]:
        """Z''-1995 = 6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4

        X1 = (流动资产 - 流动负债) / 总资产
        X2 = 留存收益 / 总资产
        X3 = EBIT / 总资产
        X4 = 股东权益账面值 / 总负债账面值

        任一关键字段缺失或除零 → None（GateAgent 拿不到就给 S4=0）。
        """
        ta = raw.get("total_assets")
        tca = raw.get("total_current_assets")
        tcl = raw.get("total_current_liab")
        re_ = raw.get("retained_earnings")
        ebit = raw.get("ebit")
        eq = raw.get("total_equity")
        liab = raw.get("total_liabilities")

        # 必要字段
        if not ta or ta <= 0:
            return None
        if liab is None or liab <= 0:
            return None
        if any(v is None for v in (tca, tcl, re_, ebit, eq)):
            return None

        x1 = (tca - tcl) / ta
        x2 = re_ / ta
        x3 = ebit / ta
        x4 = eq / liab
        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
        return round(z, 4)

    @staticmethod
    def compute_eps_cagr_3y(eps_history: List[dict]) -> Optional[float]:
        """3 年 EPS 复合增长率。需要至少 4 个年报点（首尾）。

        CAGR = (EPS_T / EPS_0) ** (1/3) - 1
        EPS_0 必须为正（负数 / 0 时无法定义 CAGR）。
        eps_history 假定按 period DESC 排序。
        """
        if not eps_history or len(eps_history) < 4:
            return None
        latest = eps_history[0].get("eps")
        oldest = eps_history[3].get("eps")
        if latest is None or oldest is None:
            return None
        if oldest <= 0:
            return None
        if latest <= 0:
            return None
        try:
            cagr = (latest / oldest) ** (1.0 / 3.0) - 1.0
            return round(cagr, 4)
        except (ValueError, ZeroDivisionError, OverflowError):
            return None

    @staticmethod
    def compute_peg(pe_ttm: Optional[float], eps_cagr_3y: Optional[float]) -> Optional[float]:
        """PEG = PE-TTM / (CAGR * 100)

        负 PE / 负 CAGR / 0 增长 → None（含义不清，GateAgent 不给加分）。
        """
        if pe_ttm is None or pe_ttm <= 0:
            return None
        if eps_cagr_3y is None or eps_cagr_3y <= 0:
            return None
        growth_pct = eps_cagr_3y * 100.0
        try:
            return round(pe_ttm / growth_pct, 3)
        except ZeroDivisionError:
            return None

    # ── 持久化 ──

    def upsert_snapshot(self, snap: FinancialSnapshot) -> None:
        """按 (stock, report_period) upsert：先 DELETE 再 INSERT，单事务。

        stock_financials 没有 UNIQUE 约束，靠 SELECT-by-key 去重。
        """
        ts = datetime.now(timezone.utc).isoformat()
        try:
            self.db.write(
                "DELETE FROM stock_financials WHERE stock = ? AND report_period = ?",
                (snap.stock, snap.report_period),
            )
            self.db.write(
                """
                INSERT INTO stock_financials
                    (stock, report_period, revenue, net_profit,
                     z_score, pe_ttm, eps_cagr_3y, peg_ratio, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.stock,
                    snap.report_period,
                    snap.revenue,
                    snap.net_profit,
                    snap.z_score,
                    snap.pe_ttm,
                    snap.eps_cagr_3y,
                    snap.peg_ratio,
                    ts,
                ),
            )
        except sqlite3.Error as e:
            self.logger.error(f"upsert_snapshot DB error for {snap.stock}: {e}")
            raise


if __name__ == "__main__":
    db_path = "data/knowledge.db"
    agent = FinancialAgent(db_path)
    result = agent.run()
    print(json.dumps(result, ensure_ascii=False, indent=2) if result else "run returned None")
