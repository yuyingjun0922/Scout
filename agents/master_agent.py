"""
agents/master_agent.py — 大师评分 Agent（v1.03 MVP）

5 位大师，纯规则评分（不调 LLM）：
  - 巴菲特 buffett   :护城河 + 安全边际 + 管理层 (0-100)
  - 芒格   munger    :排除清单（pass/fail，不打分）
  - 段永平 duan      :商业模式 + 自由现金流 (0-100)
  - 林奇   lynch     :PEG + 成长性 (0-100)
  - 费雪   fisher    :研发强度 + 长期成长质量 (0-100)

数据源：完全依赖 stock_financials（v1.01-financial-lite 填充的）
缺数据策略：
  - 单子项缺 → 该子项 0 分 + verdict 标"数据不足"或"需你主观判断"
  - 全表无该股记录 → analyze_stock 返 "数据不足，请先运行 financial_agent"

不硬凑分数：缺关键字段时不强行假数。
每次分析都写入 master_analysis（history 用）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

if __name__ == "__main__":
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)

from agents.base import BaseAgent, DataMissingError
from infra.db_manager import DatabaseManager


MASTER_NAMES: List[str] = ["buffett", "munger", "duan", "lynch", "fisher"]
MASTER_LABELS: Dict[str, str] = {
    "buffett": "巴菲特",
    "munger": "芒格",
    "duan": "段永平",
    "lynch": "彼得·林奇",
    "fisher": "费雪",
}


@dataclass
class MasterResult:
    master: str                        # buffett/munger/...
    label: str                         # 巴菲特/芒格/...
    score: Optional[int]               # 0-100；芒格 None
    verdict: str                       # 中文评价
    details: Dict[str, Any] = field(default_factory=dict)

    def to_db_row(self, stock: str, ts: str) -> tuple:
        return (
            stock,
            self.master,
            self.score,
            self.verdict,
            json.dumps(self.details, ensure_ascii=False),
            ts,
        )


class MasterAgent(BaseAgent):
    """大师评分 Agent。CLI/MCP 单股入口 + 批处理（可选）。"""

    def __init__(self, db: Union[str, DatabaseManager]):
        if isinstance(db, str):
            self.db_path = db
            db_manager = DatabaseManager(db)
        else:
            self.db_path = db.db_path
            db_manager = db
        super().__init__(name="master_agent", db=db_manager)

    # ── 主入口 ──

    def run(self, symbol: str) -> Optional[Dict[str, Any]]:
        """单股分析入口。包错误成 None 不抛。"""
        return self.run_with_error_handling(self._analyze_one, symbol)

    def analyze_stock(self, symbol: str) -> Dict[str, Any]:
        """对外友好入口：永不抛，缺数据返 ok=False + reason。

        返回 dict:
            {
              "ok": bool,
              "stock": str,
              "report": str,                # 中文文字报告
              "results": [ MasterResult.dict, ... ],   # 5 项
              "report_period": str | None,
              "analyzed_at": str (UTC ISO),
              "error": str (only when ok=False)
            }
        """
        try:
            return self._analyze_one(symbol)
        except DataMissingError as e:
            return {
                "ok": False,
                "stock": symbol,
                "error": str(e),
                "report": f"# {symbol} 大师分析失败\n\n{e}",
                "results": [],
                "report_period": None,
                "analyzed_at": _now_utc(),
            }
        except Exception as e:
            self._log_error("unknown", f"{type(e).__name__}: {e}", "analyze_stock")
            return {
                "ok": False,
                "stock": symbol,
                "error": f"{type(e).__name__}: {e}",
                "report": f"# {symbol} 大师分析失败\n\n内部错误：{type(e).__name__}",
                "results": [],
                "report_period": None,
                "analyzed_at": _now_utc(),
            }

    # ── 核心 ──

    def _analyze_one(self, symbol: str) -> Dict[str, Any]:
        if not symbol or not isinstance(symbol, str):
            raise DataMissingError("symbol 必填且必须是 str")

        fin = self._load_latest_financials(symbol)
        if fin is None:
            raise DataMissingError(
                f"stock_financials 没有 {symbol} 的数据，请先运行 financial_agent"
            )

        results: List[MasterResult] = [
            self._score_buffett(fin),
            self._score_munger(fin),
            self._score_duan(fin),
            self._score_lynch(fin),
            self._score_fisher(fin),
        ]

        ts = _now_utc()
        self._persist_results(symbol, results, ts)

        report = self._render_report(symbol, fin, results)
        return {
            "ok": True,
            "stock": symbol,
            "report_period": fin.get("report_period"),
            "results": [
                {
                    "master": r.master,
                    "label": r.label,
                    "score": r.score,
                    "verdict": r.verdict,
                    "details": r.details,
                }
                for r in results
            ],
            "report": report,
            "analyzed_at": ts,
        }

    # ── 数据加载 ──

    def _load_latest_financials(self, symbol: str) -> Optional[Dict[str, Any]]:
        """取该股 stock_financials 最新一行（按 report_period DESC, updated_at DESC）。"""
        try:
            rows = self.db.query(
                """
                SELECT stock, report_period, revenue, net_profit,
                       z_score, pe_ttm, eps_cagr_3y, peg_ratio, updated_at
                FROM stock_financials
                WHERE stock = ?
                ORDER BY report_period DESC, updated_at DESC
                LIMIT 1
                """,
                (symbol,),
            )
        except sqlite3.Error as e:
            self.logger.error(f"_load_latest_financials DB error: {e}")
            return None
        if not rows:
            return None
        return dict(rows[0])

    def load_universe(self) -> List[str]:
        """与 FinancialAgent 同口径：A 股、非 staging、active。"""
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

    # ── 5 位大师评分 ──

    @staticmethod
    def _score_buffett(fin: Dict[str, Any]) -> MasterResult:
        """巴菲特：护城河 + 安全边际 + 管理层 (0-100)。

        子项分配：
          - 安全边际 (40 pts) = PE 估值
          - 管理层 (30 pts)   = EPS 3 年 CAGR
          - 盈利能力 (30 pts) = 净利率（净利润/营收）
        护城河无量化数据 → 标 "需你主观判断"。
        """
        pe = fin.get("pe_ttm")
        cagr = fin.get("eps_cagr_3y")
        revenue = fin.get("revenue")
        net_profit = fin.get("net_profit")

        # 安全边际
        safety_score, safety_note = _band(
            pe,
            [
                ((None, 15), 40, "PE<15 显著低估"),
                ((15, 25), 30, "PE 15-25 合理"),
                ((25, 40), 15, "PE 25-40 偏贵"),
                ((40, None), 0, "PE>40 估值过高"),
            ],
            none_score=0,
            none_note="PE 数据不足",
        )

        # 管理层（用 EPS CAGR 代理）
        mgmt_score, mgmt_note = _band(
            cagr,
            [
                ((None, 0), 0, "EPS 在衰退"),
                ((0, 0.05), 10, "EPS 增长 0-5% 平庸"),
                ((0.05, 0.15), 20, "EPS 增长 5-15% 健康"),
                ((0.15, None), 30, "EPS 增长 >15% 优秀"),
            ],
            none_score=0,
            none_note="EPS CAGR 数据不足",
        )

        # 盈利能力
        net_margin = _safe_ratio(net_profit, revenue)
        prof_score, prof_note = _band(
            net_margin,
            [
                ((None, 0), 0, "净利率为负"),
                ((0, 0.05), 5, "净利率 0-5% 偏薄"),
                ((0.05, 0.10), 10, "净利率 5-10% 一般"),
                ((0.10, 0.20), 20, "净利率 10-20% 良好"),
                ((0.20, None), 30, "净利率 >20% 优秀"),
            ],
            none_score=0,
            none_note="净利率数据不足",
        )

        total = safety_score + mgmt_score + prof_score
        verdict = _bucket_label(total) + f"（{safety_note}；{mgmt_note}；{prof_note}）"
        return MasterResult(
            master="buffett",
            label=MASTER_LABELS["buffett"],
            score=total,
            verdict=verdict,
            details={
                "safety_margin": {"score": safety_score, "pe_ttm": pe, "note": safety_note},
                "management": {"score": mgmt_score, "eps_cagr_3y": cagr, "note": mgmt_note},
                "profitability": {
                    "score": prof_score,
                    "net_margin": net_margin,
                    "note": prof_note,
                },
                "moat": {"score": None, "note": "护城河需你主观判断（品牌/网络/转换成本/规模）"},
            },
        )

    @staticmethod
    def _score_munger(fin: Dict[str, Any]) -> MasterResult:
        """芒格：排除清单（pass/fail，不打分）。

        触发任一条 → 未通过：
          1. Z''-1995 < 1.10 → 财务困境
          2. EPS CAGR < -10% → 持续严重衰退
          3. 净利率 < 0     → 当期亏损
          4. 缺 z_score 又缺 cagr → 信息不足无法评估
        """
        z = fin.get("z_score")
        cagr = fin.get("eps_cagr_3y")
        revenue = fin.get("revenue")
        net_profit = fin.get("net_profit")
        net_margin = _safe_ratio(net_profit, revenue)

        reasons: List[str] = []
        if z is not None and z < 1.10:
            reasons.append(f"Z''-1995={z:.2f} 进入财务困境区（<1.10）")
        if cagr is not None and cagr < -0.10:
            reasons.append(f"EPS 3 年 CAGR={cagr*100:.1f}% 持续严重衰退")
        if net_margin is not None and net_margin < 0:
            reasons.append(f"净利率={net_margin*100:.1f}% 当期亏损")
        if z is None and cagr is None:
            reasons.append("Z'' 与 EPS CAGR 均缺失，信息不足无法判断")

        if reasons:
            verdict = "未通过：" + "；".join(reasons)
            details = {"passed": False, "fail_reasons": reasons}
        else:
            verdict = "通过芒格清单（无明显排除项；建议补主观判断：能力圈、商业模式、管理层诚信）"
            details = {"passed": True, "fail_reasons": []}

        return MasterResult(
            master="munger",
            label=MASTER_LABELS["munger"],
            score=None,
            verdict=verdict,
            details=details,
        )

    @staticmethod
    def _score_duan(fin: Dict[str, Any]) -> MasterResult:
        """段永平：商业模式 + 自由现金流 (0-100)。

        子项分配：
          - 商业模式 (50 pts) = 净利率（高利润 = 强商业模式）
          - 成长质量 (30 pts) = EPS CAGR
          - 财务稳健 (20 pts) = Z''-1995
        FCF 无数据 → 标 "数据不足，需补 cash_flow_statement"。
        """
        revenue = fin.get("revenue")
        net_profit = fin.get("net_profit")
        cagr = fin.get("eps_cagr_3y")
        z = fin.get("z_score")

        net_margin = _safe_ratio(net_profit, revenue)
        biz_score, biz_note = _band(
            net_margin,
            [
                ((None, 0.08), 0, "净利率<8% 商业模式偏弱"),
                ((0.08, 0.15), 20, "净利率 8-15% 一般"),
                ((0.15, 0.25), 35, "净利率 15-25% 优秀"),
                ((0.25, None), 50, "净利率>25% 卓越护城河"),
            ],
            none_score=0,
            none_note="净利率数据不足",
        )

        growth_score, growth_note = _band(
            cagr,
            [
                ((None, 0), 0, "EPS 衰退"),
                ((0, 0.05), 10, "增速 0-5% 平庸"),
                ((0.05, 0.15), 20, "增速 5-15% 健康"),
                ((0.15, None), 30, "增速 >15% 高质量"),
            ],
            none_score=0,
            none_note="EPS CAGR 数据不足",
        )

        health_score, health_note = _band(
            z,
            [
                ((None, 1.10), 0, "Z'' 进入困境区"),
                ((1.10, 2.60), 10, "Z'' 灰区"),
                ((2.60, None), 20, "Z'' 安全区"),
            ],
            none_score=0,
            none_note="Z 分数缺失",
        )

        total = biz_score + growth_score + health_score
        verdict = _bucket_label(total) + f"（{biz_note}；{growth_note}；{health_note}）"
        return MasterResult(
            master="duan",
            label=MASTER_LABELS["duan"],
            score=total,
            verdict=verdict,
            details={
                "business_model": {
                    "score": biz_score, "net_margin": net_margin, "note": biz_note
                },
                "growth_quality": {
                    "score": growth_score, "eps_cagr_3y": cagr, "note": growth_note
                },
                "financial_health": {
                    "score": health_score, "z_score": z, "note": health_note
                },
                "free_cash_flow": {
                    "score": None,
                    "note": "FCF 数据不足，需在 financial_agent 补 cash_flow_statement",
                },
            },
        )

    @staticmethod
    def _score_lynch(fin: Dict[str, Any]) -> MasterResult:
        """彼得·林奇：PEG + 成长性 (0-100)。

        子项分配：
          - PEG (60 pts) — 林奇核心指标
          - 成长性 (40 pts) = EPS CAGR 分类
        """
        peg = fin.get("peg_ratio")
        cagr = fin.get("eps_cagr_3y")

        peg_score, peg_note = _band(
            peg,
            [
                ((None, 0.5), 60, "PEG<0.5 严重低估"),
                ((0.5, 1.0), 50, "PEG 0.5-1 经典低估"),
                ((1.0, 1.5), 30, "PEG 1-1.5 合理"),
                ((1.5, 2.0), 15, "PEG 1.5-2 偏贵"),
                ((2.0, None), 0, "PEG>2 高估"),
            ],
            none_score=0,
            none_note="PEG 数据不足（PE 或增速缺失/为负）",
        )

        growth_score, growth_note = _band(
            cagr,
            [
                ((None, 0), 0, "增速为负，林奇排除"),
                ((0, 0.08), 10, "Slow Grower（<8%）"),
                ((0.08, 0.15), 20, "Stalwart 稳健（8-15%）"),
                ((0.15, 0.25), 30, "好成长（15-25%）"),
                ((0.25, None), 40, "Fast Grower 快速成长（>25%）"),
            ],
            none_score=0,
            none_note="EPS CAGR 数据不足",
        )

        total = peg_score + growth_score
        verdict = _bucket_label(total) + f"（{peg_note}；{growth_note}）"
        return MasterResult(
            master="lynch",
            label=MASTER_LABELS["lynch"],
            score=total,
            verdict=verdict,
            details={
                "peg": {"score": peg_score, "peg_ratio": peg, "note": peg_note},
                "growth": {"score": growth_score, "eps_cagr_3y": cagr, "note": growth_note},
            },
        )

    @staticmethod
    def _score_fisher(fin: Dict[str, Any]) -> MasterResult:
        """费雪：研发强度 + 长期成长质量 (0-100)。

        子项分配：
          - 长期成长 (60 pts) = EPS CAGR（费雪强调持续高增长）
          - 盈利质量 (40 pts) = 净利率（管理层质量代理）
        研发强度无数据 → 标 "需你主观判断"。
        """
        cagr = fin.get("eps_cagr_3y")
        revenue = fin.get("revenue")
        net_profit = fin.get("net_profit")
        net_margin = _safe_ratio(net_profit, revenue)

        growth_score, growth_note = _band(
            cagr,
            [
                ((None, 0.05), 0, "增速<5% 不符合费雪长期高成长标准"),
                ((0.05, 0.10), 20, "增速 5-10% 一般"),
                ((0.10, 0.20), 40, "增速 10-20% 优秀"),
                ((0.20, None), 60, "增速 >20% 卓越长期成长"),
            ],
            none_score=0,
            none_note="EPS CAGR 数据不足",
        )

        prof_score, prof_note = _band(
            net_margin,
            [
                ((None, 0.10), 0, "净利率<10% 盈利质量不足"),
                ((0.10, 0.20), 25, "净利率 10-20% 良好"),
                ((0.20, None), 40, "净利率>20% 卓越管理层质量"),
            ],
            none_score=0,
            none_note="净利率数据不足",
        )

        total = growth_score + prof_score
        verdict = _bucket_label(total) + f"（{growth_note}；{prof_note}）"
        return MasterResult(
            master="fisher",
            label=MASTER_LABELS["fisher"],
            score=total,
            verdict=verdict,
            details={
                "long_term_growth": {
                    "score": growth_score, "eps_cagr_3y": cagr, "note": growth_note
                },
                "profit_quality": {
                    "score": prof_score, "net_margin": net_margin, "note": prof_note
                },
                "rd_intensity": {
                    "score": None,
                    "note": "研发强度需你主观判断（财务表未含 R&D 字段，Phase 2B 待补）",
                },
            },
        )

    # ── 持久化 ──

    def _persist_results(
        self, stock: str, results: List[MasterResult], ts: str
    ) -> None:
        try:
            self.db.write_many(
                """
                INSERT INTO master_analysis
                    (stock, master_name, score, verdict, details, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [r.to_db_row(stock, ts) for r in results],
            )
        except sqlite3.Error as e:
            self.logger.error(f"_persist_results DB error for {stock}: {e}")
            raise

    # ── 报告渲染 ──

    @staticmethod
    def _render_report(
        symbol: str, fin: Dict[str, Any], results: List[MasterResult]
    ) -> str:
        period = fin.get("report_period") or "(未知期)"
        revenue = fin.get("revenue")
        net_profit = fin.get("net_profit")
        z = fin.get("z_score")
        pe = fin.get("pe_ttm")
        cagr = fin.get("eps_cagr_3y")
        peg = fin.get("peg_ratio")

        lines: List[str] = []
        lines.append(f"# {symbol} 大师评分报告")
        lines.append("")
        lines.append(f"- 报告期: {period}")
        lines.append(f"- 营收: {_fmt_money(revenue)} ; 净利润: {_fmt_money(net_profit)}")
        lines.append(
            f"- PE-TTM: {_fmt_num(pe)} ; EPS 3 年 CAGR: {_fmt_pct(cagr)} ; "
            f"PEG: {_fmt_num(peg, dec=3)} ; Z''-1995: {_fmt_num(z)}"
        )
        lines.append("")

        for r in results:
            score_str = "—" if r.score is None else f"{r.score}/100"
            lines.append(f"## {r.label}  {score_str}")
            lines.append(r.verdict)
            lines.append("")

        lines.append("---")
        lines.append("> Scout 大师模块 MVP（v1.03）。规则评分，不构成投资建议；")
        lines.append("> 请结合行业判断与主观验证（护城河 / 能力圈 / 管理层诚信 / 研发强度）。")
        return "\n".join(lines)


# ══════════════ 工具函数 ══════════════


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    try:
        return numerator / denominator
    except (ZeroDivisionError, TypeError):
        return None


def _band(
    value: Optional[float],
    bands: List[tuple],
    *,
    none_score: int = 0,
    none_note: str = "数据不足",
) -> tuple:
    """根据数值落在哪个区间返回 (score, note)。

    bands: [((low, high), score, note), ...]
        - low/high 为 None 表示无下/上界
        - 区间为 [low, high)，落在该段返回对应 score+note
    value None 时返回 (none_score, none_note)。
    """
    if value is None:
        return none_score, none_note
    for (low, high), score, note in bands:
        in_low = low is None or value >= low
        in_high = high is None or value < high
        if in_low and in_high:
            return score, note
    return none_score, none_note


def _bucket_label(total: int) -> str:
    if total >= 80:
        return f"评分 {total}/100：强烈推荐"
    if total >= 60:
        return f"评分 {total}/100：值得关注"
    if total >= 40:
        return f"评分 {total}/100：有保留"
    return f"评分 {total}/100：不建议"


def _fmt_num(v: Optional[float], dec: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:.{dec}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{v*100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        if abs(v) >= 1e8:
            return f"{v/1e8:.2f} 亿"
        if abs(v) >= 1e4:
            return f"{v/1e4:.2f} 万"
        return f"{v:.0f}"
    except (TypeError, ValueError):
        return "—"


# ══════════════ CLI ══════════════


if __name__ == "__main__":
    import argparse

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="MasterAgent 单股分析")
    parser.add_argument("--symbol", required=True, help="A 股 6 位代码，如 600519")
    parser.add_argument("--db", default="data/knowledge.db")
    args = parser.parse_args()

    agent = MasterAgent(args.db)
    out = agent.analyze_stock(args.symbol)
    print(out["report"])
    print()
    print("--- JSON ---")
    print(json.dumps(
        {k: v for k, v in out.items() if k != "report"},
        ensure_ascii=False,
        indent=2,
    ))
