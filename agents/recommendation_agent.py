"""
agents/recommendation_agent.py — 推荐 Agent (v1.07 Phase 2A 决策层)

四阶段混合制评分（参考 Phase 1 施工文档 Step 9 + 系统蓝图 v1.50 Step 3）：

  阶段 1：硬底线淘汰 (任一触发即 reject)
    - policy_fatal     : 近 90 天该行业 restrictive 政策
    - z_score_distress : stock_financials 最新 Z'' < 1.81
    - gap_unfillable   : watchlist.gap_fillability < 2
    - risk_flag        : track_list.risk_flag = 2 OR 在 rejected_stocks

  阶段 2：6 维度评分 (满分按 A 股权重 → 100)
    d1 政策含金量       (15)  funded=100/mandatory=75/directive=25
    d2 动机持续性       (15)  stable=100/drifting=50/reversing=0; default 75
    d3 真实缺口存在     (15)  fillability 4-5=100/3=75/2=50/1=25/0=0; default 50
    d4 数据验证         (10)  V1/V3/D4 ≥5=100/2-4=50/<2=20
    d5 标的财务         (15)  Z''>2.60+PEG<1.5=100; OR=75; grey=50; distress=0
    d6 估值合理         (10)  PEG<1.5=100/1.5-2.5=50/>2.5=25; default 50
    总分 = (d1+d2+d3+d4+d5+d6) × 100/80 → normalized 0-100

  阶段 3：综合验证 (在归一化总分上加减)
    - GateAgent 同行业信号 ≥5 条 → +5
    - MasterAgent 5 位中 ≥3 位正面 (score≥60) → +5
    - BiasChecker 3+ 警告 → -10

  阶段 4：推荐级别
    ≥75: A | 60-74: B | 40-59: candidate (不推送) | <40: reject

降级策略：
  - stock_financials 缺失   → d5=50, 标 "数据不足"
  - policy_funding_type 缺  → d1 默认 directive=25
  - watchlist.gap_fillability 缺 → d3=50
  - watchlist.motivation_uncertainty 缺 → d2=75
  - 任何子项数据缺失不阻塞流程，标 reasons 字段
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

if __name__ == "__main__":
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)

from agents.base import BaseAgent, DataMissingError
from infra.db_manager import DatabaseManager


# ═══════════════ 阈值常量 ═══════════════

POLICY_LOOKBACK_DAYS = 90
Z_SCORE_DISTRESS = 1.81           # 用户 spec
Z_SCORE_SAFE = 2.60               # Z''-1995 安全区
GAP_FILLABILITY_FATAL = 2          # < 2 → fatal
PEG_CHEAP = 1.5
PEG_MID = 2.5

# 维度权重（A 股，单位 100 分制规范化前的子项满分）
WEIGHTS = {"d1": 15, "d2": 15, "d3": 15, "d4": 10, "d5": 15, "d6": 10}
WEIGHT_TOTAL = sum(WEIGHTS.values())  # 80

# Phase 3 验证阈值
VERIFY_GATE_SIGNAL_THRESHOLD = 5       # 同行业信号 ≥5 → +5
VERIFY_MASTER_POSITIVE_THRESHOLD = 60  # master.score ≥60 视为正面
VERIFY_MASTER_MIN_POSITIVES = 3        # 5 位中 ≥3 位正面 → +5
VERIFY_BIAS_WARN_THRESHOLD = 3         # 3+ bias 警告 → -10
VERIFY_BONUS_GATE = 5
VERIFY_BONUS_MASTER = 5
VERIFY_PENALTY_BIAS = -10

# Phase 4 级别阈值
LEVEL_A_MIN = 75
LEVEL_B_MIN = 60
LEVEL_CANDIDATE_MIN = 40

# 软标签
LEVEL_A = "A"
LEVEL_B = "B"
LEVEL_CANDIDATE = "candidate"
LEVEL_REJECT = "reject"


@dataclass
class DimensionScore:
    """单维度评分结果。"""
    code: str           # d1..d6
    score: int          # 0-100 子项内
    weight: int         # 权重
    weighted: float     # score * weight / 100
    note: str           # 中文说明
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "score": self.score,
            "weight": self.weight,
            "weighted": round(self.weighted, 2),
            "note": self.note,
            "evidence": self.evidence,
        }


@dataclass
class HardGateResult:
    """阶段 1 结果。"""
    passed: bool
    fail_reasons: List[str] = field(default_factory=list)
    checked: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationDelta:
    """阶段 3 综合验证调整。"""
    delta: int = 0
    notes: List[str] = field(default_factory=list)
    gate_signals_count: Optional[int] = None
    master_positive_count: Optional[int] = None
    bias_warnings_count: Optional[int] = None


@dataclass
class CounterCard:
    """反方卡片：列出 N 条潜在风险/反对意见。"""
    risks: List[str] = field(default_factory=list)
    data_gaps: List[str] = field(default_factory=list)
    contrary_signals: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "risks": self.risks,
            "data_gaps": self.data_gaps,
            "contrary_signals": self.contrary_signals,
        }


# ═══════════════════ Agent ═══════════════════


class RecommendationAgent(BaseAgent):
    """4 阶段混合制推荐 Agent（v1.07 MVP）。"""

    def __init__(self, db: Union[str, DatabaseManager], mode: str = "cold_start"):
        if isinstance(db, str):
            self.db_path = db
            db_manager = DatabaseManager(db)
        else:
            self.db_path = db.db_path
            db_manager = db
        super().__init__(name="recommendation_agent", db=db_manager)
        self.mode = mode

    # ─────────────── 主入口 ───────────────

    def run(self) -> Optional[Dict[str, Any]]:
        """全量跑：load_universe → analyze each → 写库 → 汇总。"""
        return self.run_with_error_handling(self._run_batch)

    def _run_batch(self) -> Dict[str, Any]:
        universe = self.load_universe()
        self.logger.info(f"RecommendationAgent batch: {len(universe)} A-share stocks")

        results: List[Dict[str, Any]] = []
        for code in universe:
            try:
                r = self._analyze_one(code)
                results.append(r)
            except Exception as e:
                self._log_error(
                    "unknown", f"{type(e).__name__}: {e}", f"_analyze_one({code})"
                )
                results.append({
                    "ok": False,
                    "stock": code,
                    "error": f"{type(e).__name__}: {e}",
                    "level": LEVEL_REJECT,
                })

        # 汇总
        levels = {LEVEL_A: 0, LEVEL_B: 0, LEVEL_CANDIDATE: 0, LEVEL_REJECT: 0}
        for r in results:
            lv = r.get("level") or LEVEL_REJECT
            levels[lv] = levels.get(lv, 0) + 1

        return {
            "processed": len(universe),
            "succeeded": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok")),
            "levels": levels,
            "ts_utc": _now_utc(),
            "results": results,
        }

    def analyze(self, symbol: str) -> Dict[str, Any]:
        """对外友好单股入口：永不抛，缺数据降级返结果。"""
        try:
            return self._analyze_one(symbol)
        except DataMissingError as e:
            return {
                "ok": False,
                "stock": symbol,
                "error": str(e),
                "level": LEVEL_REJECT,
                "report": f"# {symbol} 推荐分析失败\n\n{e}",
            }
        except Exception as e:
            self._log_error("unknown", f"{type(e).__name__}: {e}", "analyze")
            return {
                "ok": False,
                "stock": symbol,
                "error": f"{type(e).__name__}: {e}",
                "level": LEVEL_REJECT,
                "report": f"# {symbol} 推荐分析失败\n\n内部错误：{type(e).__name__}",
            }

    def generate_counter_card(self, symbol: str) -> Dict[str, Any]:
        """生成反方卡片：列风险 + 数据缺口 + 反向信号。"""
        try:
            full = self._analyze_one(symbol)
        except Exception as e:
            return {
                "ok": False,
                "stock": symbol,
                "error": f"{type(e).__name__}: {e}",
            }
        return {
            "ok": True,
            "stock": symbol,
            "level": full.get("level"),
            "total_score": full.get("total_score"),
            "counter_card": full.get("counter_card"),
        }

    # ─────────────── 核心 ───────────────

    def _analyze_one(self, symbol: str) -> Dict[str, Any]:
        if not symbol or not isinstance(symbol, str):
            raise DataMissingError("symbol 必填且必须是 str")

        meta = self._load_stock_meta(symbol)
        industry = meta.get("industry") if meta else None
        industry_id = meta.get("industry_id") if meta else None

        # 阶段 1：硬底线
        gate = self._phase1_hard_gates(symbol, industry)

        # 阶段 2：6 维度评分（即使 gate 失败也跑，便于看完整画像）
        dims: Dict[str, DimensionScore] = {
            "d1": self._d1_policy_funding(industry),
            "d2": self._d2_motivation_persistence(industry),
            "d3": self._d3_gap_fillability(industry),
            "d4": self._d4_data_verification(industry),
            "d5": self._d5_stock_financials(symbol),
            "d6": self._d6_valuation(symbol),
        }
        weighted_sum = sum(d.weighted for d in dims.values())
        normalized_score = round(weighted_sum * 100.0 / WEIGHT_TOTAL, 2)

        # 阶段 3：综合验证调整
        verify = self._phase3_verify(symbol, industry, normalized_score)
        adjusted_score = max(0.0, min(100.0, normalized_score + verify.delta))

        # 阶段 4：级别
        if not gate.passed:
            level = LEVEL_REJECT
        else:
            level = self._level_from_score(adjusted_score)

        # 反方卡片
        counter = self._build_counter_card(symbol, industry, dims, gate, verify)

        # 持久化（写 recommendations 表）
        ts = _now_utc()
        thesis_hash = self._thesis_hash(industry, dims, gate, verify)
        try:
            self._persist_recommendation(
                stock=symbol,
                industry_id=industry_id,
                level=level,
                total_score=adjusted_score,
                dims=dims,
                gate=gate,
                verify=verify,
                counter=counter,
                thesis_hash=thesis_hash,
                ts=ts,
            )
        except sqlite3.Error as e:
            self.logger.warning(f"persist recommendation failed for {symbol}: {e}")

        report = self._render_report(
            symbol, industry, dims, gate, verify,
            normalized_score, adjusted_score, level, counter,
        )

        return {
            "ok": True,
            "stock": symbol,
            "industry": industry,
            "industry_id": industry_id,
            "level": level,
            "raw_score": normalized_score,
            "total_score": adjusted_score,
            "phase1_passed": gate.passed,
            "phase1_fail_reasons": gate.fail_reasons,
            "phase1_checked": gate.checked,
            "dimensions": {k: v.to_dict() for k, v in dims.items()},
            "verification": {
                "delta": verify.delta,
                "notes": verify.notes,
                "gate_signals_count": verify.gate_signals_count,
                "master_positive_count": verify.master_positive_count,
                "bias_warnings_count": verify.bias_warnings_count,
            },
            "counter_card": counter.to_dict(),
            "thesis_hash": thesis_hash,
            "mode": self.mode,
            "report": report,
            "recommended_at": ts,
        }

    # ─────────────── 数据加载 ───────────────

    def _load_stock_meta(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 related_stocks 取 industry/industry_id（取最新一行）。"""
        try:
            row = self.db.query_one(
                """SELECT industry, industry_id, stock_name, market
                   FROM related_stocks
                   WHERE stock_code = ? AND status = 'active'
                   ORDER BY updated_at DESC
                   LIMIT 1""",
                (symbol,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_load_stock_meta DB error for {symbol}: {e}")
            return None
        return dict(row) if row else None

    def load_universe(self) -> List[str]:
        """A 股 active 全量。"""
        try:
            rows = self.db.query(
                """SELECT DISTINCT stock_code FROM related_stocks
                   WHERE market = 'A' AND status = 'active'
                     AND confidence != 'staging'
                     AND stock_code IS NOT NULL
                     AND length(stock_code) = 6"""
            )
            return [r["stock_code"] for r in rows]
        except sqlite3.Error as e:
            self.logger.error(f"load_universe DB error: {e}")
            return []

    # ─────────────── 阶段 1：硬底线 ───────────────

    def _phase1_hard_gates(
        self, symbol: str, industry: Optional[str]
    ) -> HardGateResult:
        reasons: List[str] = []
        checked: Dict[str, Any] = {}

        # (1) policy_fatal: 近 90 天 restrictive 政策
        if industry:
            pol = self._has_recent_restrictive_policy(industry)
            checked["policy_fatal_count"] = pol
            if pol > 0:
                reasons.append(
                    f"policy_fatal: 近 {POLICY_LOOKBACK_DAYS} 天 {industry} 行业有 "
                    f"{pol} 条 restrictive 政策"
                )
        else:
            checked["policy_fatal_count"] = None  # 无法检查

        # (2) z_score < 1.81
        z = self._latest_z_score(symbol)
        checked["z_score"] = z
        if z is not None and z < Z_SCORE_DISTRESS:
            reasons.append(f"z_score_distress: Z''={z:.2f} < {Z_SCORE_DISTRESS}")

        # (3) gap_fillability < 2
        gap = self._gap_fillability(industry) if industry else None
        checked["gap_fillability"] = gap
        if gap is not None and gap < GAP_FILLABILITY_FATAL:
            reasons.append(
                f"gap_unfillable: gap_fillability={gap} < {GAP_FILLABILITY_FATAL}"
            )

        # (4) risk_flag
        risk = self._risk_flag(symbol)
        checked["risk_flag"] = risk["flag"]
        checked["in_rejected_stocks"] = risk["in_rejected"]
        if risk["flag"] == 2:
            reasons.append(f"risk_flag: track_list.risk_flag=2 ({risk.get('detail') or ''})")
        if risk["in_rejected"]:
            reasons.append(f"risk_flag: 已在 rejected_stocks 中（{risk.get('reject_reason') or ''}）")

        return HardGateResult(passed=not reasons, fail_reasons=reasons, checked=checked)

    def _has_recent_restrictive_policy(self, industry: str) -> int:
        """近 N 天该行业 restrictive 政策条数。"""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=POLICY_LOOKBACK_DAYS)
        ).isoformat()
        try:
            row = self.db.query_one(
                """SELECT COUNT(*) AS n FROM info_units
                   WHERE timestamp >= ?
                     AND policy_direction = 'restrictive'
                     AND related_industries LIKE ?""",
                (cutoff, f"%{industry}%"),
            )
            return int(row["n"]) if row else 0
        except sqlite3.Error as e:
            self.logger.warning(f"_has_recent_restrictive_policy error: {e}")
            return 0

    def _latest_z_score(self, symbol: str) -> Optional[float]:
        try:
            row = self.db.query_one(
                """SELECT z_score FROM stock_financials WHERE stock = ?
                   ORDER BY report_period DESC, updated_at DESC LIMIT 1""",
                (symbol,),
            )
            return row["z_score"] if row and row["z_score"] is not None else None
        except sqlite3.Error as e:
            self.logger.warning(f"_latest_z_score error: {e}")
            return None

    def _gap_fillability(self, industry: str) -> Optional[int]:
        try:
            row = self.db.query_one(
                "SELECT gap_fillability FROM watchlist WHERE industry_name = ?",
                (industry,),
            )
            return row["gap_fillability"] if row and row["gap_fillability"] is not None else None
        except sqlite3.Error as e:
            self.logger.warning(f"_gap_fillability error: {e}")
            return None

    def _risk_flag(self, symbol: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "flag": None, "detail": None,
            "in_rejected": False, "reject_reason": None,
        }
        try:
            tr = self.db.query_one(
                "SELECT risk_flag, risk_detail FROM track_list WHERE stock = ?",
                (symbol,),
            )
            if tr:
                result["flag"] = tr["risk_flag"]
                result["detail"] = tr["risk_detail"]
        except sqlite3.Error as e:
            self.logger.warning(f"_risk_flag track_list error: {e}")
        try:
            rj = self.db.query_one(
                "SELECT reject_reason FROM rejected_stocks WHERE stock = ? LIMIT 1",
                (symbol,),
            )
            if rj:
                result["in_rejected"] = True
                result["reject_reason"] = rj["reject_reason"]
        except sqlite3.Error as e:
            self.logger.warning(f"_risk_flag rejected_stocks error: {e}")
        return result

    # ─────────────── 阶段 2：6 维度 ───────────────

    def _d1_policy_funding(self, industry: Optional[str]) -> DimensionScore:
        """d1 政策含金量。

        无 policy_funding_type 字段，用 policy_direction supportive 数 + 关键词
        启发式：
          - 'funded'  : content 含"专项资金/补贴/拨款" → 100
          - 'mandatory': content 含"必须/强制/规定/要求" → 75
          - 默认 directive (supportive 但无强制资金) → 25
          - 无任何 supportive 信号 → 0
        """
        w = WEIGHTS["d1"]
        if not industry:
            return _make_dim("d1", 25, w, "缺 industry，默认 directive=25", {})

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=POLICY_LOOKBACK_DAYS)
        ).isoformat()
        try:
            rows = self.db.query(
                """SELECT content, policy_direction FROM info_units
                   WHERE timestamp >= ?
                     AND related_industries LIKE ?
                     AND source IN ('D1', 'V1', 'V3')""",
                (cutoff, f"%{industry}%"),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_d1 query error: {e}")
            rows = []

        funded_kw = ["专项资金", "补贴", "拨款", "财政支持", "重大专项"]
        mandatory_kw = ["必须", "强制", "应当", "不得", "禁止外的合规要求"]
        funded = mandatory = supportive = 0
        for r in rows:
            content = r["content"] or ""
            direction = (r["policy_direction"] or "").lower()
            if direction != "supportive":
                continue
            supportive += 1
            if any(k in content for k in funded_kw):
                funded += 1
            elif any(k in content for k in mandatory_kw):
                mandatory += 1

        if funded > 0:
            score, level = 100, "funded"
        elif mandatory > 0:
            score, level = 75, "mandatory"
        elif supportive > 0:
            score, level = 25, "directive"
        else:
            score, level = 25, "default_directive"  # 数据缺失时默认 25

        note = f"政策含金量={level} ({score}/100)"
        evidence = {
            "supportive_count": supportive,
            "funded_count": funded,
            "mandatory_count": mandatory,
            "lookback_days": POLICY_LOOKBACK_DAYS,
            "level": level,
        }
        return _make_dim("d1", score, w, note, evidence)

    def _d2_motivation_persistence(
        self, industry: Optional[str]
    ) -> DimensionScore:
        """d2 动机持续性。industry_dict 无 motivation_drift 列，用 watchlist.motivation_uncertainty 代理。"""
        w = WEIGHTS["d2"]
        default_score = 75
        if not industry:
            return _make_dim("d2", default_score, w, "缺 industry，默认 stable=75", {})
        try:
            row = self.db.query_one(
                "SELECT motivation_uncertainty, motivation_levels FROM watchlist WHERE industry_name = ?",
                (industry,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_d2 query error: {e}")
            row = None
        if not row or row["motivation_uncertainty"] is None:
            return _make_dim(
                "d2", default_score, w,
                "watchlist.motivation_uncertainty 缺失，默认 stable=75", {},
            )
        unc = (row["motivation_uncertainty"] or "").lower()
        mapping = {"low": (100, "stable"), "medium": (50, "drifting"), "high": (0, "reversing")}
        score, label = mapping.get(unc, (default_score, "unknown"))
        evidence = {
            "motivation_uncertainty": unc,
            "motivation_levels": row["motivation_levels"],
            "label": label,
        }
        return _make_dim("d2", score, w, f"动机={label} (uncertainty={unc})", evidence)

    def _d3_gap_fillability(self, industry: Optional[str]) -> DimensionScore:
        """d3 真实缺口存在。watchlist.gap_fillability 1-5。"""
        w = WEIGHTS["d3"]
        if not industry:
            return _make_dim("d3", 50, w, "缺 industry，默认=50", {})
        gap = self._gap_fillability(industry)
        if gap is None:
            return _make_dim(
                "d3", 50, w, "watchlist.gap_fillability 缺失，默认=50", {},
            )
        mapping = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100, 5: 100}
        score = mapping.get(int(gap), 50)
        return _make_dim(
            "d3", score, w, f"gap_fillability={gap} → {score}/100",
            {"gap_fillability": gap},
        )

    def _d4_data_verification(self, industry: Optional[str]) -> DimensionScore:
        """d4 数据验证。近 90 天 V1/V3/D4 信号条数。"""
        w = WEIGHTS["d4"]
        if not industry:
            return _make_dim("d4", 20, w, "缺 industry，默认=20", {})
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=POLICY_LOOKBACK_DAYS)
        ).isoformat()
        try:
            row = self.db.query_one(
                """SELECT COUNT(*) AS n FROM info_units
                   WHERE timestamp >= ?
                     AND related_industries LIKE ?
                     AND source IN ('V1', 'V3', 'D4')""",
                (cutoff, f"%{industry}%"),
            )
            n = int(row["n"]) if row else 0
        except sqlite3.Error as e:
            self.logger.warning(f"_d4 query error: {e}")
            n = 0
        if n >= 5:
            score, level = 100, "充分"
        elif n >= 2:
            score, level = 50, "部分"
        else:
            score, level = 20, "不足"
        return _make_dim(
            "d4", score, w, f"V1/V3/D4 数据 {n} 条 → {level} ({score}/100)",
            {"verify_count_90d": n, "level": level},
        )

    def _d5_stock_financials(self, symbol: str) -> DimensionScore:
        """d5 标的财务。Z'' + PEG 矩阵，缺数据 = 50 标 '数据不足'。"""
        w = WEIGHTS["d5"]
        try:
            row = self.db.query_one(
                """SELECT z_score, peg_ratio FROM stock_financials
                   WHERE stock = ?
                   ORDER BY report_period DESC, updated_at DESC LIMIT 1""",
                (symbol,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_d5 query error for {symbol}: {e}")
            row = None
        if not row:
            return _make_dim(
                "d5", 50, w, "stock_financials 无数据，默认=50（数据不足）",
                {"data_missing": True},
            )
        z = row["z_score"]
        peg = row["peg_ratio"]
        if z is None:
            return _make_dim(
                "d5", 50, w, "z_score 缺失，默认=50（数据不足）",
                {"data_missing": True, "peg_ratio": peg},
            )
        z_safe = z >= Z_SCORE_SAFE
        z_distress = z < 1.10
        peg_cheap = peg is not None and peg > 0 and peg < PEG_CHEAP
        if z_safe and peg_cheap:
            score, label = 100, "Z''安全 + PEG 便宜"
        elif z_safe or peg_cheap:
            score, label = 75, ("Z''安全" if z_safe else "PEG 便宜")
        elif z_distress:
            score, label = 0, "Z''困境"
        else:
            score, label = 50, "Z''灰区"
        return _make_dim(
            "d5", score, w, f"财务={label} (Z''={z}, PEG={peg})",
            {"z_score": z, "peg_ratio": peg, "label": label},
        )

    def _d6_valuation(self, symbol: str) -> DimensionScore:
        """d6 估值合理。Phase 2A 用 PEG 启发式（无历史 PE 中位数）。"""
        w = WEIGHTS["d6"]
        try:
            row = self.db.query_one(
                """SELECT peg_ratio, pe_ttm FROM stock_financials WHERE stock = ?
                   ORDER BY report_period DESC, updated_at DESC LIMIT 1""",
                (symbol,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_d6 query error for {symbol}: {e}")
            row = None
        if not row or row["peg_ratio"] is None:
            return _make_dim(
                "d6", 50, w, "PEG 缺失，默认=50",
                {"data_missing": True},
            )
        peg = row["peg_ratio"]
        if peg <= 0:
            score, label = 50, "PEG 无意义（负 PE 或零增长）"
        elif peg < PEG_CHEAP:
            score, label = 100, "PEG 低估"
        elif peg <= PEG_MID:
            score, label = 50, "PEG 合理"
        else:
            score, label = 25, "PEG 高估"
        return _make_dim(
            "d6", score, w, f"估值={label} (PEG={peg})",
            {"peg_ratio": peg, "pe_ttm": row["pe_ttm"], "label": label},
        )

    # ─────────────── 阶段 3：综合验证 ───────────────

    def _phase3_verify(
        self, symbol: str, industry: Optional[str], current_score: float
    ) -> VerificationDelta:
        verify = VerificationDelta()

        # GateAgent: 同行业近 30 天信号条数（D1 + V1 + S4）
        if industry:
            try:
                row = self.db.query_one(
                    """SELECT COUNT(*) AS n FROM info_units
                       WHERE related_industries LIKE ?
                         AND timestamp >= ?""",
                    (
                        f"%{industry}%",
                        (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
                    ),
                )
                gate_count = int(row["n"]) if row else 0
                verify.gate_signals_count = gate_count
                if gate_count >= VERIFY_GATE_SIGNAL_THRESHOLD:
                    verify.delta += VERIFY_BONUS_GATE
                    verify.notes.append(
                        f"GateAgent: 同行业 {gate_count} 条信号 → +{VERIFY_BONUS_GATE}"
                    )
            except sqlite3.Error as e:
                self.logger.warning(f"_phase3 gate query error: {e}")

        # MasterAgent: 5 大师正面数（先查 master_analysis；无则跑一次）
        master_positive = self._master_positive_count(symbol)
        verify.master_positive_count = master_positive
        if master_positive is not None and master_positive >= VERIFY_MASTER_MIN_POSITIVES:
            verify.delta += VERIFY_BONUS_MASTER
            verify.notes.append(
                f"MasterAgent: {master_positive}/5 位大师正面 → +{VERIFY_BONUS_MASTER}"
            )

        # BiasChecker: decision stage 警告数
        bias_count = self._bias_warning_count(symbol, industry)
        verify.bias_warnings_count = bias_count
        if bias_count is not None and bias_count >= VERIFY_BIAS_WARN_THRESHOLD:
            verify.delta += VERIFY_PENALTY_BIAS
            verify.notes.append(
                f"BiasChecker: {bias_count} 条警告 → {VERIFY_PENALTY_BIAS}"
            )

        return verify

    def _master_positive_count(self, symbol: str) -> Optional[int]:
        """先查最近的 master_analysis；如无则惰性跑一次（容忍数据不足）。"""
        try:
            rows = self.db.query(
                """SELECT master_name, score FROM master_analysis
                   WHERE stock = ?
                     AND analyzed_at = (
                       SELECT MAX(analyzed_at) FROM master_analysis WHERE stock = ?
                     )""",
                (symbol, symbol),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"master_analysis query error: {e}")
            rows = []

        if not rows:
            # 惰性跑一次
            try:
                from agents.master_agent import MasterAgent
                ma = MasterAgent(self.db)
                out = ma.analyze_stock(symbol)
                if not out.get("ok"):
                    return None  # 数据不足
                results = out.get("results", [])
                return sum(
                    1 for r in results
                    if r.get("score") is not None
                    and r["score"] >= VERIFY_MASTER_POSITIVE_THRESHOLD
                )
            except Exception as e:
                self.logger.warning(f"master agent lazy invoke failed: {e}")
                return None

        return sum(
            1 for r in rows
            if r["score"] is not None and r["score"] >= VERIFY_MASTER_POSITIVE_THRESHOLD
        )

    def _bias_warning_count(
        self, symbol: str, industry: Optional[str]
    ) -> Optional[int]:
        try:
            from agents.bias_checker import BiasChecker, load_bias_config
            checker = BiasChecker(self.db, config=load_bias_config())
            payload = {"stock": symbol, "industry": industry or "", "direction": "supportive"}
            checked = checker.check(payload, "decision")
            warnings = checked.get("bias_warnings", {}).get("warnings") or []
            return len(warnings)
        except Exception as e:
            self.logger.warning(f"bias checker invoke failed: {e}")
            return None

    # ─────────────── 阶段 4：级别 ───────────────

    @staticmethod
    def _level_from_score(score: float) -> str:
        if score >= LEVEL_A_MIN:
            return LEVEL_A
        if score >= LEVEL_B_MIN:
            return LEVEL_B
        if score >= LEVEL_CANDIDATE_MIN:
            return LEVEL_CANDIDATE
        return LEVEL_REJECT

    # ─────────────── 反方卡片 ───────────────

    def _build_counter_card(
        self,
        symbol: str,
        industry: Optional[str],
        dims: Dict[str, DimensionScore],
        gate: HardGateResult,
        verify: VerificationDelta,
    ) -> CounterCard:
        card = CounterCard()

        # 硬底线作为 risk
        for r in gate.fail_reasons:
            card.risks.append(f"硬底线触发：{r}")

        # 低分维度作为 risk
        for code, d in dims.items():
            if d.score <= 25:
                card.risks.append(f"{code} 低分 {d.score}/100：{d.note}")
            if d.evidence.get("data_missing"):
                card.data_gaps.append(f"{code} 缺数据：{d.note}")

        # bias 警告
        if verify.bias_warnings_count and verify.bias_warnings_count > 0:
            card.risks.append(
                f"BiasChecker 提示 {verify.bias_warnings_count} 条认知偏误警告"
            )

        # 反向信号样本（取近 90 天 restrictive）
        if industry:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=POLICY_LOOKBACK_DAYS)
            ).isoformat()
            try:
                rows = self.db.query(
                    """SELECT id, source, timestamp, policy_direction
                       FROM info_units
                       WHERE timestamp >= ?
                         AND related_industries LIKE ?
                         AND policy_direction IN ('restrictive', 'mixed')
                       ORDER BY timestamp DESC LIMIT 5""",
                    (cutoff, f"%{industry}%"),
                )
                card.contrary_signals = [dict(r) for r in rows]
            except sqlite3.Error as e:
                self.logger.warning(f"counter card contrary query error: {e}")

        return card

    # ─────────────── 持久化 ───────────────

    def _thesis_hash(
        self,
        industry: Optional[str],
        dims: Dict[str, DimensionScore],
        gate: HardGateResult,
        verify: VerificationDelta,
    ) -> str:
        payload = {
            "industry": industry,
            "dim_scores": {k: v.score for k, v in dims.items()},
            "gate_passed": gate.passed,
            "verify_delta": verify.delta,
        }
        return hashlib.sha1(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

    def _persist_recommendation(
        self,
        *,
        stock: str,
        industry_id: Optional[int],
        level: str,
        total_score: float,
        dims: Dict[str, DimensionScore],
        gate: HardGateResult,
        verify: VerificationDelta,
        counter: CounterCard,
        thesis_hash: str,
        ts: str,
    ) -> None:
        details = {
            "dimensions": {k: v.to_dict() for k, v in dims.items()},
            "phase1": {
                "passed": gate.passed,
                "fail_reasons": gate.fail_reasons,
                "checked": gate.checked,
            },
            "phase3": {
                "delta": verify.delta,
                "notes": verify.notes,
                "gate_signals_count": verify.gate_signals_count,
                "master_positive_count": verify.master_positive_count,
                "bias_warnings_count": verify.bias_warnings_count,
            },
            "counter_card": counter.to_dict(),
        }
        # UNIQUE(stock, thesis_hash, recommended_at) — 同秒同 hash 视为重复，OR IGNORE
        self.db.write(
            """INSERT OR IGNORE INTO recommendations
               (stock, industry_id, recommend_level, total_score,
                dimensions_detail, thesis_hash, mode, mode_since, recommended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stock, industry_id, level, total_score,
                json.dumps(details, ensure_ascii=False),
                thesis_hash, self.mode, ts, ts,
            ),
        )

    # ─────────────── 报告渲染 ───────────────

    @staticmethod
    def _render_report(
        symbol: str,
        industry: Optional[str],
        dims: Dict[str, DimensionScore],
        gate: HardGateResult,
        verify: VerificationDelta,
        raw_score: float,
        adjusted_score: float,
        level: str,
        counter: CounterCard,
    ) -> str:
        lines: List[str] = [
            f"# {symbol} 推荐分析报告（v1.07）",
            "",
            f"- 行业: {industry or '(未知)'}",
            f"- 阶段 1 硬底线: {'✅ PASS' if gate.passed else '❌ FAIL'}",
        ]
        if not gate.passed:
            for r in gate.fail_reasons:
                lines.append(f"  - {r}")
        lines.extend([
            "",
            "## 阶段 2：6 维度评分",
            "",
            "| 维度 | 子分 | 权重 | 加权 | 说明 |",
            "|---|---|---|---|---|",
        ])
        for code in ("d1", "d2", "d3", "d4", "d5", "d6"):
            d = dims[code]
            lines.append(
                f"| {code} | {d.score}/100 | {d.weight} | {d.weighted:.2f} | {d.note} |"
            )
        lines.extend([
            "",
            f"加权原始分: **{raw_score:.2f} / 100**",
            "",
            "## 阶段 3：综合验证",
            "",
        ])
        if verify.notes:
            for n in verify.notes:
                lines.append(f"- {n}")
        else:
            lines.append("- 无调整")
        lines.extend([
            f"- 调整后总分: **{adjusted_score:.2f} / 100**",
            "",
            f"## 阶段 4：推荐级别 → **{level}**",
            "",
            "## 反方卡片（Counter Card）",
            "",
        ])
        if counter.risks:
            lines.append("### 风险点")
            for r in counter.risks:
                lines.append(f"- {r}")
            lines.append("")
        if counter.data_gaps:
            lines.append("### 数据缺口")
            for g in counter.data_gaps:
                lines.append(f"- {g}")
            lines.append("")
        if counter.contrary_signals:
            lines.append("### 反向信号样本（近 90 天 restrictive/mixed）")
            for s in counter.contrary_signals:
                lines.append(
                    f"- [{s.get('source')}] {s.get('timestamp')} "
                    f"direction={s.get('policy_direction')} (id={s.get('id')})"
                )
            lines.append("")
        lines.extend([
            "---",
            "> Scout 推荐 Agent v1.07 MVP。规则评分 + 数据降级；",
            "> 不构成投资建议。请结合行业判断与外部 LLM 深度复核。",
        ])
        return "\n".join(lines)


# ═══════════════════ 工具 ═══════════════════


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_dim(
    code: str, score: int, weight: int, note: str, evidence: Dict[str, Any]
) -> DimensionScore:
    weighted = score * weight / 100.0
    return DimensionScore(
        code=code, score=int(score), weight=int(weight),
        weighted=weighted, note=note, evidence=evidence,
    )


# ═══════════════════ CLI ═══════════════════


if __name__ == "__main__":
    import argparse

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="RecommendationAgent v1.07")
    parser.add_argument("--symbol", default=None,
                        help="单股分析 (6 位 A 股代码)；不传则跑全量")
    parser.add_argument("--db", default="data/knowledge.db")
    parser.add_argument("--counter", action="store_true",
                        help="只输出反方卡片")
    args = parser.parse_args()

    agent = RecommendationAgent(args.db)
    if args.symbol:
        if args.counter:
            out = agent.generate_counter_card(args.symbol)
        else:
            out = agent.analyze(args.symbol)
        if "report" in out:
            print(out["report"])
            print()
            print("--- JSON ---")
            print(json.dumps(
                {k: v for k, v in out.items() if k != "report"},
                ensure_ascii=False, indent=2,
            ))
        else:
            print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        result = agent.run()
        if result is None:
            print("[recommend] run() returned None")
            sys.exit(1)
        summary = {k: v for k, v in result.items() if k != "results"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print()
        print(f"=== Top 5 by total_score ===")
        sorted_results = sorted(
            result["results"], key=lambda r: r.get("total_score", 0), reverse=True
        )
        for r in sorted_results[:5]:
            print(
                f"  [{r.get('level','?')}] {r.get('stock')} "
                f"({r.get('industry','?')}) score={r.get('total_score','?')}"
            )
