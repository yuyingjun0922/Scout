"""
agents/motivation_drift_agent.py — 动机漂移检测 Agent (v1.08 Phase 2A 决策层支撑)

职责：
  对 watchlist.zone='active' 的行业，扫描 info_units + stock_financials，识别
  3 种动机状态并写回 watchlist.motivation_drift：

      stable     — 推动逻辑未变
      drifting   — 出现削弱信号，但未反转
      reversing  — 出现反转信号（硬否定）

四类信号（任意触发即纳入聚合）：

  A) 政策方向 (policy_direction)
     - 7d 内出现 restrictive + funded keywords          → reversing
     - 30d 内 restrictive 条数 ≥ 2                       → drifting
     - 30d 内 restrictive 条数 = 1                       → drifting（保守）

  B) 关键词情绪 (keyword sentiment)
     - 7d 内 content 含严重负面关键词（暴跌/亏损/破产/清算/腰斩） → reversing
     - 30d 内 content 中等负面关键词命中 ≥ 3 次          → drifting

  C) 财务趋势 (financial trends)
     - 行业内任一 related_stock Z'' < 1.81               → reversing
     - 行业内 ≥30% 的 related_stocks Z'' < 2.60          → drifting

  D) 信号冲突 (cross-signal contradiction)
     - 7d 内同时存在 supportive ≥ 1 AND restrictive ≥ 1 → drifting

聚合规则：
  - 任一 reversing       → state=reversing
  - drifting 计数 ≥ 2    → state=drifting
  - drifting 计数 = 1    → state=drifting（保守）
  - 全部 stable          → state=stable

降级：
  - info_units / stock_financials 缺数据 → 该信号 stable，不阻塞
  - 整个行业无任何数据                    → state=stable

写回 watchlist：
  motivation_drift          = state
  motivation_last_drift_at  = now（仅 state ∈ {drifting, reversing} 时更新）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

if __name__ == "__main__":
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)

from agents.base import BaseAgent
from infra.db_manager import DatabaseManager


# ═══════════════════ 常量 ═══════════════════

LOOKBACK_7D = 7
LOOKBACK_30D = 30

# 政策含金量关键词（与 RecommendationAgent 一致）
FUNDED_KEYWORDS = ["专项资金", "补贴", "拨款", "财政支持", "重大专项"]

# 严重负面关键词 — 出现即触发 reversing
SEVERE_NEGATIVE_KEYWORDS = [
    "暴跌", "腰斩", "亏损", "破产", "清算", "崩盘", "退市",
]

# 中等负面关键词 — 30d 内 ≥3 次触发 drifting
MODERATE_NEGATIVE_KEYWORDS = [
    "过剩", "产能", "降价", "减产", "去库存", "退出",
    "价格战", "萎缩", "下行", "回调", "走弱",
]

MODERATE_NEG_THRESHOLD = 3
RESTRICTIVE_DRIFT_THRESHOLD_30D = 2

# 财务阈值（与 recommendation_agent / financial_agent 一致）
Z_DISTRESS = 1.81
Z_SAFE = 2.60
FINANCIAL_DRIFT_RATIO = 0.30          # ≥30% 的股票 Z<safe → drifting

STATE_STABLE = "stable"
STATE_DRIFTING = "drifting"
STATE_REVERSING = "reversing"

# 聚合优先级
_PRIORITY = {STATE_REVERSING: 2, STATE_DRIFTING: 1, STATE_STABLE: 0}


# ═══════════════════ dataclass ═══════════════════

@dataclass
class SignalResult:
    """单个信号的检测结果。"""
    name: str                              # 'policy' / 'keyword' / 'financial' / 'cross'
    state: str                             # stable / drifting / reversing
    note: str                              # 中文说明
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "note": self.note,
            "evidence": self.evidence,
        }


@dataclass
class DriftDetection:
    """单行业的完整检测结果。"""
    industry: str
    state: str
    signals: List[SignalResult] = field(default_factory=list)
    detected_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "industry": self.industry,
            "state": self.state,
            "signals": [s.to_dict() for s in self.signals],
            "triggered": [s.name for s in self.signals if s.state != STATE_STABLE],
            "detected_at": self.detected_at,
        }


# ═══════════════════ Agent ═══════════════════

class MotivationDriftAgent(BaseAgent):
    """v1.08 动机漂移检测 Agent。"""

    def __init__(self, db: Union[str, DatabaseManager]):
        if isinstance(db, str):
            self.db_path = db
            db_manager = DatabaseManager(db)
        else:
            self.db_path = db.db_path
            db_manager = db
        super().__init__(name="motivation_drift_agent", db=db_manager)

    # ─────────────── 主入口 ───────────────

    def run(self) -> Optional[Dict[str, Any]]:
        """全量跑：watchlist active → 每行业检测 → 写回。"""
        return self.run_with_error_handling(self._run_batch)

    def _run_batch(self) -> Dict[str, Any]:
        universe = self.load_universe()
        self.logger.info(
            f"MotivationDriftAgent batch: {len(universe)} active industries"
        )

        results: List[Dict[str, Any]] = []
        counts = {STATE_STABLE: 0, STATE_DRIFTING: 0, STATE_REVERSING: 0}
        for industry in universe:
            try:
                detection = self._detect_one(industry)
                self._persist(detection)
                results.append(detection.to_dict())
                counts[detection.state] = counts.get(detection.state, 0) + 1
            except Exception as e:
                self._log_error(
                    "unknown", f"{type(e).__name__}: {e}",
                    f"_detect_one({industry})",
                )
                results.append({
                    "industry": industry,
                    "state": STATE_STABLE,
                    "error": f"{type(e).__name__}: {e}",
                    "signals": [],
                })
                counts[STATE_STABLE] += 1

        return {
            "processed": len(universe),
            "stable": counts[STATE_STABLE],
            "drifting": counts[STATE_DRIFTING],
            "reversing": counts[STATE_REVERSING],
            "ts_utc": _now_utc(),
            "results": results,
        }

    def detect(self, industry: str) -> Dict[str, Any]:
        """对外友好单行业入口；永不抛。"""
        try:
            d = self._detect_one(industry)
            return d.to_dict()
        except Exception as e:
            self._log_error(
                "unknown", f"{type(e).__name__}: {e}", f"detect({industry})",
            )
            return {
                "industry": industry,
                "state": STATE_STABLE,
                "error": f"{type(e).__name__}: {e}",
                "signals": [],
            }

    # ─────────────── 宇宙 ───────────────

    def load_universe(self) -> List[str]:
        """watchlist.zone='active' 的行业名（去重排序）。"""
        try:
            rows = self.db.query(
                """SELECT DISTINCT industry_name FROM watchlist
                   WHERE zone = 'active' AND industry_name IS NOT NULL
                   ORDER BY industry_name"""
            )
            return [r["industry_name"] for r in rows]
        except sqlite3.Error as e:
            self.logger.error(f"load_universe DB error: {e}")
            return []

    # ─────────────── 单行业核心 ───────────────

    def _detect_one(self, industry: str) -> DriftDetection:
        if not industry:
            raise ValueError("industry must be non-empty")

        signals: List[SignalResult] = [
            self._signal_policy(industry),
            self._signal_keyword(industry),
            self._signal_financial(industry),
            self._signal_cross(industry),
        ]
        state = self._aggregate(signals)
        return DriftDetection(
            industry=industry,
            state=state,
            signals=signals,
            detected_at=_now_utc(),
        )

    # ─────────────── 信号 A: 政策方向 ───────────────

    def _signal_policy(self, industry: str) -> SignalResult:
        cutoff_7d = _utc_iso_offset(-LOOKBACK_7D)
        cutoff_30d = _utc_iso_offset(-LOOKBACK_30D)
        try:
            rows_30d = self.db.query(
                """SELECT timestamp, content, policy_direction FROM info_units
                   WHERE timestamp >= ?
                     AND policy_direction = 'restrictive'
                     AND related_industries LIKE ?""",
                (cutoff_30d, f"%{industry}%"),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_signal_policy query error ({industry}): {e}")
            rows_30d = []

        restrictive_30d = len(rows_30d)
        restrictive_funded_7d = 0
        for r in rows_30d:
            ts = r["timestamp"] or ""
            if ts < cutoff_7d:
                continue
            content = r["content"] or ""
            if any(k in content for k in FUNDED_KEYWORDS):
                restrictive_funded_7d += 1

        evidence = {
            "restrictive_30d": restrictive_30d,
            "restrictive_funded_7d": restrictive_funded_7d,
        }

        if restrictive_funded_7d > 0:
            return SignalResult(
                name="policy",
                state=STATE_REVERSING,
                note=f"7d 内 restrictive 政策伴随专项资金/补贴 {restrictive_funded_7d} 条 → reversing",
                evidence=evidence,
            )
        if restrictive_30d >= RESTRICTIVE_DRIFT_THRESHOLD_30D:
            return SignalResult(
                name="policy",
                state=STATE_DRIFTING,
                note=f"30d 内 restrictive 政策 {restrictive_30d} 条 (≥{RESTRICTIVE_DRIFT_THRESHOLD_30D}) → drifting",
                evidence=evidence,
            )
        if restrictive_30d == 1:
            return SignalResult(
                name="policy",
                state=STATE_DRIFTING,
                note="30d 内 restrictive 政策 1 条 → drifting (保守)",
                evidence=evidence,
            )
        return SignalResult(
            name="policy",
            state=STATE_STABLE,
            note=f"30d 内 restrictive 政策 {restrictive_30d} 条 → stable",
            evidence=evidence,
        )

    # ─────────────── 信号 B: 关键词情绪 ───────────────

    def _signal_keyword(self, industry: str) -> SignalResult:
        cutoff_7d = _utc_iso_offset(-LOOKBACK_7D)
        cutoff_30d = _utc_iso_offset(-LOOKBACK_30D)
        try:
            rows_30d = self.db.query(
                """SELECT timestamp, content FROM info_units
                   WHERE timestamp >= ? AND related_industries LIKE ?""",
                (cutoff_30d, f"%{industry}%"),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_signal_keyword query error ({industry}): {e}")
            rows_30d = []

        severe_hits_7d: List[str] = []
        moderate_hits_30d = 0
        for r in rows_30d:
            ts = r["timestamp"] or ""
            content = r["content"] or ""
            if ts >= cutoff_7d:
                for k in SEVERE_NEGATIVE_KEYWORDS:
                    if k in content:
                        severe_hits_7d.append(k)
                        break  # 一条只算一次 severe
            for k in MODERATE_NEGATIVE_KEYWORDS:
                if k in content:
                    moderate_hits_30d += 1

        evidence = {
            "severe_keywords_7d": severe_hits_7d,
            "moderate_keyword_hits_30d": moderate_hits_30d,
        }

        if severe_hits_7d:
            return SignalResult(
                name="keyword",
                state=STATE_REVERSING,
                note=f"7d 内严重负面关键词 {severe_hits_7d} → reversing",
                evidence=evidence,
            )
        if moderate_hits_30d >= MODERATE_NEG_THRESHOLD:
            return SignalResult(
                name="keyword",
                state=STATE_DRIFTING,
                note=f"30d 内中等负面关键词命中 {moderate_hits_30d} 次 (≥{MODERATE_NEG_THRESHOLD}) → drifting",
                evidence=evidence,
            )
        return SignalResult(
            name="keyword",
            state=STATE_STABLE,
            note=f"30d 内中等负面命中 {moderate_hits_30d} 次 → stable",
            evidence=evidence,
        )

    # ─────────────── 信号 C: 财务趋势 ───────────────

    def _signal_financial(self, industry: str) -> SignalResult:
        try:
            rows = self.db.query(
                """SELECT sf.stock, sf.z_score
                   FROM stock_financials sf
                   JOIN related_stocks rs ON rs.stock_code = sf.stock
                   WHERE rs.industry = ?
                     AND rs.status = 'active'
                     AND sf.z_score IS NOT NULL
                     AND sf.report_period = (
                       SELECT MAX(report_period) FROM stock_financials
                       WHERE stock = sf.stock
                     )""",
                (industry,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_signal_financial query error ({industry}): {e}")
            rows = []

        total = len(rows)
        if total == 0:
            return SignalResult(
                name="financial",
                state=STATE_STABLE,
                note="无 stock_financials 数据 → stable",
                evidence={"sample_size": 0},
            )

        distress_count = sum(1 for r in rows if r["z_score"] < Z_DISTRESS)
        weak_count = sum(1 for r in rows if r["z_score"] < Z_SAFE)
        weak_ratio = weak_count / total
        evidence = {
            "sample_size": total,
            "distress_count": distress_count,
            "weak_count": weak_count,
            "weak_ratio": round(weak_ratio, 3),
        }

        if distress_count > 0:
            return SignalResult(
                name="financial",
                state=STATE_REVERSING,
                note=f"行业内 {distress_count}/{total} 只股票 Z'' < {Z_DISTRESS} → reversing",
                evidence=evidence,
            )
        if weak_ratio >= FINANCIAL_DRIFT_RATIO:
            return SignalResult(
                name="financial",
                state=STATE_DRIFTING,
                note=f"行业内 {weak_count}/{total} 只 ({weak_ratio*100:.0f}%) Z'' < {Z_SAFE} → drifting",
                evidence=evidence,
            )
        return SignalResult(
            name="financial",
            state=STATE_STABLE,
            note=f"{total} 只样本财务健康 → stable",
            evidence=evidence,
        )

    # ─────────────── 信号 D: 信号冲突 ───────────────

    def _signal_cross(self, industry: str) -> SignalResult:
        cutoff_7d = _utc_iso_offset(-LOOKBACK_7D)
        try:
            rows = self.db.query(
                """SELECT policy_direction FROM info_units
                   WHERE timestamp >= ? AND related_industries LIKE ?
                     AND policy_direction IN ('supportive', 'restrictive')""",
                (cutoff_7d, f"%{industry}%"),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"_signal_cross query error ({industry}): {e}")
            rows = []

        sup = sum(1 for r in rows if r["policy_direction"] == "supportive")
        res = sum(1 for r in rows if r["policy_direction"] == "restrictive")
        evidence = {"supportive_7d": sup, "restrictive_7d": res}

        if sup >= 1 and res >= 1:
            return SignalResult(
                name="cross",
                state=STATE_DRIFTING,
                note=f"7d 内 supportive={sup} & restrictive={res} 同时存在 → drifting",
                evidence=evidence,
            )
        return SignalResult(
            name="cross",
            state=STATE_STABLE,
            note=f"7d 内方向一致 (sup={sup}/res={res}) → stable",
            evidence=evidence,
        )

    # ─────────────── 聚合 ───────────────

    @staticmethod
    def _aggregate(signals: List[SignalResult]) -> str:
        """聚合规则：任一 reversing → reversing；≥1 drifting → drifting；其余 stable。"""
        if any(s.state == STATE_REVERSING for s in signals):
            return STATE_REVERSING
        drifting_count = sum(1 for s in signals if s.state == STATE_DRIFTING)
        if drifting_count >= 1:
            return STATE_DRIFTING
        return STATE_STABLE

    # ─────────────── 持久化 ───────────────

    def _persist(self, detection: DriftDetection) -> None:
        """更新 watchlist.motivation_drift（+ motivation_last_drift_at）。"""
        ts = detection.detected_at or _now_utc()
        try:
            if detection.state == STATE_STABLE:
                self.db.write(
                    "UPDATE watchlist SET motivation_drift = ? WHERE industry_name = ?",
                    (detection.state, detection.industry),
                )
            else:
                self.db.write(
                    """UPDATE watchlist
                       SET motivation_drift = ?, motivation_last_drift_at = ?
                       WHERE industry_name = ?""",
                    (detection.state, ts, detection.industry),
                )
        except sqlite3.Error as e:
            self.logger.warning(
                f"_persist DB error for {detection.industry}: {e}"
            )

    # ─────────────── MCP / CLI 辅助 ───────────────

    def get_status(self, industry: str) -> Dict[str, Any]:
        """读取当前漂移状态（不重新检测，从 watchlist 读）。"""
        try:
            row = self.db.query_one(
                """SELECT industry_name, motivation_drift,
                          motivation_last_drift_at, motivation_uncertainty
                   FROM watchlist WHERE industry_name = ?""",
                (industry,),
            )
        except sqlite3.Error as e:
            return {
                "ok": False,
                "industry": industry,
                "error": f"{type(e).__name__}: {e}",
            }
        if not row:
            return {
                "ok": False,
                "industry": industry,
                "error": "industry not in watchlist",
            }
        return {
            "ok": True,
            "industry": industry,
            "state": row["motivation_drift"] or STATE_STABLE,
            "last_drift_at": row["motivation_last_drift_at"],
            "motivation_uncertainty": row["motivation_uncertainty"],
        }


# ═══════════════════ 工具 ═══════════════════

def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _utc_iso_offset(offset_days: int) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(days=offset_days)).isoformat()


# ═══════════════════ CLI ═══════════════════

if __name__ == "__main__":
    db_path = "data/knowledge.db"
    agent = MotivationDriftAgent(db_path)
    result = agent.run()
    print(json.dumps(result, ensure_ascii=False, indent=2) if result else "run returned None")
