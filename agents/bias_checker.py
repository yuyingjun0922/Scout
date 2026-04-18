"""
agents/bias_checker.py — 认知偏误检查 Agent (v1.04 Phase 2A)

四个检查（参考 认知偏误检查模块设计文档）：
  - B01 确认偏误   : 方向判断时检查近 N 天同行业负面信号
  - B02 幸存者偏差 : 复盘时纳入已移除/已否决的失败案例
  - B03 基数效应   : 同比 >X% 且去年同期下降 >Y%
  - B04 行业集中度 : 跟踪列表同行业 ≥N 只

数据源：
  - B01: info_units.policy_direction='restrictive' OR content 命中负面关键词
         （info_units 无 sentiment 字段，用关键词 + 方向代理）
  - B02: rejected_stocks ∪ related_stocks (status='removed' OR removed_reason 非空)
         （recommendations 表无 status，用专用拒绝表 + related_stocks 移除标记）
  - B03: stock_financials 同股至少 3 个年度 revenue（current/prev/prev_prev）
  - B04: track_list 按 industry COUNT(*)

约定：
  - 单 check 缺数据返 None（区别于"已检查、清白" → []）
  - check(result, stage) 统一入口；按 stage 路由相关检查
  - 累计警告 ≥ downgrade_threshold → result['bias_warnings']['downgrade']=True
  - 纯 SQL + 规则；不调 LLM
  - 任何 unknown 异常走 BaseAgent re-raise（fail-loud）；
    但 check() 暴露 try/except 兜底（不能因偏误检查崩外层流程）
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


# ── stage → 触发的 check 列表 ──
STAGE_CHECKS: Dict[str, List[str]] = {
    "direction":    ["b01"],
    "verification": ["b01", "b03"],
    "decision":     ["b03", "b04"],
    "review":       ["b02"],
    "all":          ["b01", "b02", "b03", "b04"],
}

VALID_STAGES = set(STAGE_CHECKS.keys())


# ── 默认阈值（可由 config/bias_checker.yaml 覆盖）──
_DEFAULTS: Dict[str, Any] = {
    "b01_confirmation_bias": True,
    "b01_lookback_days": 90,
    "b01_negative_threshold": 3,
    "b01_negative_keywords": [
        "禁止", "限制", "整改", "罚款", "警告", "暴跌",
        "亏损", "失败", "争议", "诉讼", "调查",
        "退市", "取缔", "严禁",
    ],
    "b02_survivorship_bias": True,
    "b02_min_removed_for_warning": 1,
    "b03_base_effect": True,
    "base_effect_yoy_threshold": 30.0,   # 百分点
    "base_effect_prev_drop": 20.0,
    "b04_concentration_risk": True,
    "concentration_threshold": 5,
    "downgrade_threshold": 3,
}


@dataclass
class BiasReport:
    """单条 bias 警告（落到 result['bias_warnings']['warnings'] 里）。"""
    code: str               # b01/b02/b03/b04
    severity: str           # low/medium/high
    message: str            # 中文给用户看
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence,
        }


class BiasChecker(BaseAgent):
    """认知偏误检查器。check(result, stage) 统一入口。"""

    def __init__(
        self,
        db: Union[str, DatabaseManager],
        config: Optional[Dict[str, Any]] = None,
    ):
        if isinstance(db, str):
            self.db_path = db
            db_manager = DatabaseManager(db)
        else:
            self.db_path = db.db_path
            db_manager = db
        super().__init__(name="bias_checker", db=db_manager)
        self.cfg = dict(_DEFAULTS)
        if config:
            self.cfg.update(config)

    # ── 主入口 ──

    def run(self, result: Dict[str, Any], stage: str) -> Dict[str, Any]:
        return self.check(result, stage)

    def check(self, result: Dict[str, Any], stage: str) -> Dict[str, Any]:
        """对 result 跑该 stage 相关的检查；填充 result['bias_warnings'] 后返回。

        永不抛：内部错误兜底，把错误降级为 None 检查结果。

        result 期望字段（按 stage 不同）：
            direction:    {industry: str, direction: str?, ...}
            verification: {industry: str, stock: str?, ...}
            decision:     {stock: str, industry: str, ...}
            review:       {industry: str, ...}
        """
        if stage not in VALID_STAGES:
            stage = "all"

        result = dict(result)  # 不污染原 dict
        warnings_block: Dict[str, Any] = {
            "stage": stage,
            "warnings": [],
            "counts": {"b01": None, "b02": None, "b03": None, "b04": None},
            "downgrade": False,
        }

        check_funcs = {
            "b01": (self._check_b01_confirmation, "b01_confirmation_bias"),
            "b02": (self._check_b02_survivorship, "b02_survivorship_bias"),
            "b03": (self._check_b03_base_effect,  "b03_base_effect"),
            "b04": (self._check_b04_concentration, "b04_concentration_risk"),
        }
        for code in STAGE_CHECKS[stage]:
            func, enable_key = check_funcs[code]
            if not self.cfg.get(enable_key, True):
                continue
            try:
                items = func(result)
            except Exception as e:
                # 偏误检查失败不能崩外层；记录 unknown 但不 raise
                self._log_error(
                    "unknown",
                    f"{type(e).__name__}: {e}",
                    f"check.{code}",
                )
                items = None

            if items is None:
                warnings_block["counts"][code] = None  # 数据不足
            else:
                warnings_block["counts"][code] = len(items)
                warnings_block["warnings"].extend(w.to_dict() for w in items)

        if len(warnings_block["warnings"]) >= int(self.cfg["downgrade_threshold"]):
            warnings_block["downgrade"] = True

        result["bias_warnings"] = warnings_block
        return result

    # ════════════ B01 确认偏误 ════════════

    def _check_b01_confirmation(
        self, result: Dict[str, Any]
    ) -> Optional[List[BiasReport]]:
        """方向判断阶段 → 查近 N 天同行业负面信号。

        触发条件：
          - result.industry 必填（否则数据不足返 None）
          - result.direction 在 supportive/mixed 时才检查
            （restrictive/null 已经在表达谨慎，不需提醒）
        缺数据返 None；查到信号 < 阈值返 []；超阈返 [BiasReport]。
        """
        industry = (result.get("industry") or "").strip()
        if not industry:
            return None

        direction = (result.get("direction") or "").strip().lower()
        if direction and direction not in ("supportive", "mixed", ""):
            # 已经在表达保守 → 不需要提醒"忽略了反方向"
            return []

        days = int(self.cfg["b01_lookback_days"])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            rows = self.db.query(
                """
                SELECT id, source, timestamp, content,
                       policy_direction, related_industries
                FROM info_units
                WHERE timestamp >= ?
                  AND (related_industries LIKE ? OR content LIKE ?)
                ORDER BY timestamp DESC
                LIMIT 200
                """,
                (cutoff, f"%{industry}%", f"%{industry}%"),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"b01 DB error: {e}")
            return None

        keywords = self.cfg.get("b01_negative_keywords") or []
        negatives: List[Dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            content = row.get("content") or ""
            policy = (row.get("policy_direction") or "").lower()
            hit_keyword = next((k for k in keywords if k in content), None)
            if policy == "restrictive" or hit_keyword:
                negatives.append({
                    "id": row.get("id"),
                    "source": row.get("source"),
                    "timestamp": row.get("timestamp"),
                    "policy_direction": row.get("policy_direction"),
                    "matched_keyword": hit_keyword,
                })

        threshold = int(self.cfg["b01_negative_threshold"])
        if len(negatives) < threshold:
            return []

        # 单条警告，证据里附前 5 条 negative
        msg = (
            f"确认偏误：近 {days} 天 {industry} 行业有 {len(negatives)} 条负面信号 "
            f"（≥阈值 {threshold}），方向判断 {direction or 'supportive'} 可能忽略了反向证据。"
        )
        return [BiasReport(
            code="b01",
            severity="medium" if len(negatives) < threshold * 2 else "high",
            message=msg,
            evidence={
                "industry": industry,
                "direction": direction or None,
                "lookback_days": days,
                "negative_count": len(negatives),
                "samples": negatives[:5],
            },
        )]

    # ════════════ B02 幸存者偏差 ════════════

    def _check_b02_survivorship(
        self, result: Dict[str, Any]
    ) -> Optional[List[BiasReport]]:
        """复盘阶段 → 查同行业已移除/已否决的失败案例。

        合并源：
          - rejected_stocks（专用否决表）
          - related_stocks (status='removed' OR removed_reason IS NOT NULL)
        缺 industry 返 None；查不到返 []。
        """
        industry = (result.get("industry") or "").strip()
        if not industry:
            return None

        try:
            rejected = self.db.query(
                """
                SELECT stock, reject_reason, rejected_at
                FROM rejected_stocks
                WHERE industry_id IN (
                    SELECT industry_id FROM watchlist WHERE industry_name = ?
                )
                ORDER BY rejected_at DESC
                LIMIT 50
                """,
                (industry,),
            )
            removed = self.db.query(
                """
                SELECT stock_code AS stock, removed_reason, dormant_reason,
                       status, updated_at
                FROM related_stocks
                WHERE industry = ?
                  AND (status IN ('removed', 'dormant')
                       OR removed_reason IS NOT NULL)
                ORDER BY updated_at DESC
                LIMIT 50
                """,
                (industry,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"b02 DB error: {e}")
            return None

        cases: List[Dict[str, Any]] = []
        for r in rejected:
            cases.append({
                "stock": r["stock"],
                "kind": "rejected",
                "reason": r["reject_reason"],
                "at": r["rejected_at"],
            })
        for r in removed:
            cases.append({
                "stock": r["stock"],
                "kind": r["status"] or "removed",
                "reason": r["removed_reason"] or r["dormant_reason"],
                "at": r["updated_at"],
            })

        threshold = int(self.cfg["b02_min_removed_for_warning"])
        if len(cases) < threshold:
            return []

        msg = (
            f"幸存者偏差：{industry} 行业有 {len(cases)} 只已淘汰股票（rejected/removed/dormant），"
            f"复盘时若只看现存 active 名单，会高估该行业历史成功率。"
        )
        return [BiasReport(
            code="b02",
            severity="medium",
            message=msg,
            evidence={
                "industry": industry,
                "removed_count": len(cases),
                "samples": cases[:5],
            },
        )]

    # ════════════ B03 基数效应 ════════════

    def _check_b03_base_effect(
        self, result: Dict[str, Any]
    ) -> Optional[List[BiasReport]]:
        """同比 >X% 且去年同期下降 >Y% → 警告。

        需要 stock_financials 同股 ≥3 行（current/prev/prev_prev 年报）。
        缺 stock 或不足 3 行 → None；满足但未触发 → []；触发 → [BiasReport]。
        """
        stock = (result.get("stock") or result.get("symbol") or "").strip()
        if not stock:
            return None

        try:
            rows = self.db.query(
                """
                SELECT report_period, revenue
                FROM stock_financials
                WHERE stock = ? AND revenue IS NOT NULL
                ORDER BY report_period DESC
                LIMIT 3
                """,
                (stock,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"b03 DB error: {e}")
            return None

        if len(rows) < 3:
            return None

        cur = rows[0]["revenue"]
        prev = rows[1]["revenue"]
        prev_prev = rows[2]["revenue"]
        if any(v is None or v == 0 for v in (cur, prev, prev_prev)):
            return None

        try:
            yoy_now = (cur - prev) / abs(prev) * 100.0       # 当年增速 %
            yoy_prev = (prev - prev_prev) / abs(prev_prev) * 100.0  # 去年增速 %
        except (ZeroDivisionError, TypeError):
            return None

        thresh_now = float(self.cfg["base_effect_yoy_threshold"])      # eg 30
        thresh_prev_drop = float(self.cfg["base_effect_prev_drop"])    # eg 20

        if yoy_now > thresh_now and yoy_prev < -thresh_prev_drop:
            msg = (
                f"基数效应：{stock} 当年营收同比 +{yoy_now:.1f}%，但去年同期 "
                f"{yoy_prev:.1f}%（跌超 {thresh_prev_drop}%），高增长更多是低基数反弹，"
                f"非真实业务加速。"
            )
            return [BiasReport(
                code="b03",
                severity="medium",
                message=msg,
                evidence={
                    "stock": stock,
                    "yoy_now_pct": round(yoy_now, 2),
                    "yoy_prev_pct": round(yoy_prev, 2),
                    "periods": [r["report_period"] for r in rows],
                    "revenues": [r["revenue"] for r in rows],
                },
            )]
        return []

    # ════════════ B04 行业集中度 ════════════

    def _check_b04_concentration(
        self, result: Dict[str, Any]
    ) -> Optional[List[BiasReport]]:
        """决策阶段 → 检查 track_list 同行业是否已超阈。

        缺 industry 返 None；不超阈返 []；超阈返 [BiasReport]。
        """
        industry = (result.get("industry") or "").strip()
        if not industry:
            return None

        try:
            rows = self.db.query(
                """
                SELECT stock, company_name
                FROM track_list
                WHERE industry = ?
                """,
                (industry,),
            )
        except sqlite3.Error as e:
            self.logger.warning(f"b04 DB error: {e}")
            return None

        threshold = int(self.cfg["concentration_threshold"])
        if len(rows) < threshold:
            return []

        msg = (
            f"行业集中度：track_list 已有 {len(rows)} 只 {industry} 行业股票"
            f"（≥阈值 {threshold}），再加仓同行业可能过度集中、丧失分散保护。"
        )
        return [BiasReport(
            code="b04",
            severity="high" if len(rows) >= threshold * 2 else "medium",
            message=msg,
            evidence={
                "industry": industry,
                "track_list_count": len(rows),
                "threshold": threshold,
                "samples": [
                    {"stock": r["stock"], "name": r["company_name"]} for r in rows[:5]
                ],
            },
        )]


# ══════════════════ Config 加载 ══════════════════


def load_bias_config(
    path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """读 config/bias_checker.yaml；不存在 → 返 _DEFAULTS 副本。"""
    cfg = dict(_DEFAULTS)
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "bias_checker.yaml"
    p = Path(path)
    if not p.exists():
        return cfg
    try:
        import yaml
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            cfg.update(data)
    except ImportError:
        pass
    except Exception:
        pass
    return cfg


# ══════════════════ CLI ══════════════════


if __name__ == "__main__":
    import argparse

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="BiasChecker 单步演示")
    parser.add_argument("--db", default="data/knowledge.db")
    parser.add_argument("--stage", default="all", choices=sorted(VALID_STAGES))
    parser.add_argument(
        "--result-json",
        default='{"industry":"半导体","direction":"supportive","stock":"600519"}',
    )
    args = parser.parse_args()

    checker = BiasChecker(args.db, config=load_bias_config())
    result = json.loads(args.result_json)
    out = checker.check(result, args.stage)
    print(json.dumps(out, ensure_ascii=False, indent=2))
