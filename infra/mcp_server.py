"""
infra/mcp_server.py — v1.66 Phase 1 Step 10 MCP Server

对外接口层，通过 MCP (Model Context Protocol) 让 OpenClaw / Claude Desktop
等外部 LLM 客户端读写 Scout 数据。

传输：stdio（子进程管道，无需网络端口；`scout` 作为 server 名注册）
SDK：`mcp` (pip install mcp) — FastMCP 装饰器驱动

Phase 1 工具（10 个）：

    查询类（只读）：
        (a) get_watchlist                 列出 active 行业
        (b) ask_industry                  查单行业 dashboard（复用 Step 8）
        (c) get_system_status             信源/数据库/成本/错误四维快照
        (d) search_signals                按关键词 + 源 + 时间搜索 info_units
        (e) get_latest_weekly_report      读 reports/ 最新 .md（复用 Step 9 产物）

    操作类（写入）：
        (f) add_industry                  加入 watchlist（active）
        (g) remove_industry               标记 zone='cold'（不真删）

    LLM 深度分析专用（v1.62）：
        (h) get_industry_full_context     给 LLM 用的行业全景
        (i) get_decision_context          给 LLM 用的个股决策上下文（Phase 1 简化）
        (j) get_policy_for_motivation_analysis  政策原文 + 历史类似政策

设计：
    ScoutToolImpl        → 工具的"纯"实现（直接单测，不走 MCP）
    build_server(impl)   → 把实现装饰成 FastMCP app（tool schema 从 signature 推断）
    main()               → CLI 入口（加载 config / env / 起 stdio）

入参校验失败 / 资源不存在 / DB 错都降级为 `{"ok": False, "error": "..."}` 返回，
不 raise，保证 MCP 会话不断。所有调用记 logs/mcp_access.log（JSON 行）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# 作为脚本运行（`python infra/mcp_server.py`）时，sys.path[0] 是 infra/，
# 项目根没有在 path 中。作为 package 导入（tests / CLI entry）不触发。
if __name__ == "__main__":
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

from infra.dashboard import build_industry_dashboard
from infra.db_manager import DatabaseManager
from utils.time_utils import now_utc


# ══════════════════════ 全局常量 ══════════════════════

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_LOGS_DIR = PROJECT_ROOT / "logs"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

SERVER_NAME = "scout"
SERVER_INSTRUCTIONS = (
    "Scout 金融信号系统对外接口。提供 10 个工具：查询 watchlist / 行业 "
    "dashboard / 系统状态 / 信号搜索 / 周报，以及增删行业、读取 LLM 深度分析"
    "所需的完整上下文。读多写少；所有错误以 {'ok': False, 'error': str} 返回。"
)

PHASE1_SOURCES = ("D1", "D4", "V1", "V3", "S4")
VALID_REPORT_TYPES = frozenset({"industry", "paper"})
SIGNAL_SEARCH_DEFAULT_DAYS = 30
SIGNAL_SEARCH_DEFAULT_LIMIT = 20
SIGNAL_SEARCH_MAX_LIMIT = 200
FULL_CONTEXT_INFO_UNITS_LIMIT = 200
FULL_CONTEXT_WINDOW_DAYS = 180


# ══════════════════════ 日志 ══════════════════════


def build_access_logger(logs_dir: Path) -> logging.Logger:
    """挂 file handler 的 access logger（JSON 行）。可被测试传 tmp_path 覆盖。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "mcp_access.log"
    logger = logging.getLogger(f"scout.mcp.access.{log_path}")
    logger.setLevel(logging.INFO)
    # 测试重复 build 时不重复挂 handler
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


# ══════════════════════ 工具实现 ══════════════════════


class ScoutToolImpl:
    """MCP 工具的纯实现。

    独立于 MCP 框架以便单测：每个方法接受基础参数、返回 dict。
    """

    def __init__(
        self,
        db: DatabaseManager,
        reports_dir: Path,
        access_logger: Optional[logging.Logger] = None,
    ):
        self.db = db
        self.reports_dir = Path(reports_dir)
        self.access_logger = access_logger

    # ── 访问日志 ──

    def _log_access(
        self,
        tool: str,
        params: Dict[str, Any],
        ok: bool,
        rows: int,
        duration_ms: int,
        error: Optional[str] = None,
    ) -> None:
        if self.access_logger is None:
            return
        try:
            payload = {
                "tool": tool,
                "params": params,
                "ok": ok,
                "rows": rows,
                "ms": duration_ms,
            }
            if error:
                payload["error"] = error[:300]
            self.access_logger.info(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass  # 日志永不打断主流程

    def _call(
        self,
        tool_name: str,
        params: Dict[str, Any],
        fn: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """统一入口：计时 + 错误兜底 + access log。"""
        start = time.monotonic()
        try:
            result = fn()
            ok = bool(result.get("ok", True))
            rows = self._count_rows(result)
            self._log_access(
                tool_name, params, ok=ok, rows=rows,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=result.get("error") if not ok else None,
            )
            return result
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            self._log_access(
                tool_name, params, ok=False, rows=0,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=err_msg,
            )
            return {"ok": False, "error": err_msg}

    @staticmethod
    def _count_rows(result: Dict[str, Any]) -> int:
        """从返回 dict 估算 rows 数（仅用于日志观测，无精确语义）"""
        for key in (
            "industries", "signals", "related_stocks_entries",
            "recent_info_units_180d", "similar_policies",
        ):
            val = result.get(key)
            if isinstance(val, list):
                return len(val)
        return 1 if result.get("ok", True) else 0

    # ════════════ (a) get_watchlist ════════════

    def get_watchlist(self) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            rows = self.db.query(
                """SELECT industry_id, industry_name, zone, dimensions,
                          verification_status, gap_status, entered_at,
                          last_signal_at
                   FROM watchlist
                   WHERE zone = 'active'
                   ORDER BY industry_name"""
            )
            industries = [
                {
                    "industry_id": r["industry_id"],
                    "industry_name": r["industry_name"],
                    "zone": r["zone"],
                    "dimensions": r["dimensions"],
                    "verification_status": r["verification_status"],
                    "gap_status": r["gap_status"],
                    "entered_at": r["entered_at"],
                    "last_signal_at": r["last_signal_at"],
                }
                for r in rows
            ]
            return {
                "ok": True,
                "count": len(industries),
                "industries": industries,
            }
        return self._call("get_watchlist", {}, _impl)

    # ════════════ (b) ask_industry ════════════

    def ask_industry(self, industry: str, days: int = 30) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if not isinstance(industry, str) or not industry.strip():
                return {"ok": False, "error": "industry must be non-empty str"}
            if not isinstance(days, int) or days < 1:
                return {"ok": False, "error": "days must be int >= 1"}
            try:
                dashboard = build_industry_dashboard(
                    industry.strip(), self.db, days=days
                )
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            return {"ok": True, "dashboard": dashboard}
        return self._call("ask_industry", {"industry": industry, "days": days}, _impl)

    # ════════════ (c) get_system_status ════════════

    def get_system_status(self) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            snapshot_at = now_utc()
            today_cutoff = (
                datetime.now(tz=timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            ).isoformat()
            seven_days_ago = (
                datetime.now(tz=timezone.utc) - timedelta(days=7)
            ).isoformat()

            # 数据库统计
            info_total = self.db.query_one(
                "SELECT COUNT(*) AS n FROM info_units"
            )["n"]
            wl_active = self.db.query_one(
                "SELECT COUNT(*) AS n FROM watchlist WHERE zone='active'"
            )["n"]
            wl_total = self.db.query_one(
                "SELECT COUNT(*) AS n FROM watchlist"
            )["n"]
            err_total = self.db.query_one(
                "SELECT COUNT(*) AS n FROM agent_errors"
            )["n"]
            llm_total = self.db.query_one(
                "SELECT COUNT(*) AS n FROM llm_invocations"
            )["n"]

            # 每 source 最后采集时间（MAX(created_at)）
            source_rows = self.db.query(
                """SELECT source, MAX(created_at) AS last_at
                   FROM info_units GROUP BY source"""
            )
            source_map = {r["source"]: r["last_at"] for r in source_rows}
            last_collected = {s: source_map.get(s) for s in PHASE1_SOURCES}

            # 今日成本（llm_invocations 累计 tokens / cost_cents）
            today_cost_row = self.db.query_one(
                """SELECT COALESCE(SUM(tokens_used), 0) AS tokens,
                          COALESCE(SUM(cost_cents), 0) AS cents,
                          COUNT(*) AS n
                   FROM llm_invocations WHERE invoked_at >= ?""",
                (today_cutoff,),
            )

            # 最近 7 天的错误（按 type）
            err_7d_rows = self.db.query(
                """SELECT error_type, COUNT(*) AS n FROM agent_errors
                   WHERE occurred_at >= ?
                   GROUP BY error_type""",
                (seven_days_ago,),
            )
            errors_7d = {r["error_type"]: int(r["n"]) for r in err_7d_rows}

            return {
                "ok": True,
                "snapshot_at": snapshot_at,
                "database": {
                    "info_units_total": int(info_total),
                    "watchlist_active": int(wl_active),
                    "watchlist_total": int(wl_total),
                    "agent_errors_total": int(err_total),
                    "llm_invocations_total": int(llm_total),
                },
                "source_last_collected": last_collected,
                "today_cost": {
                    "total_tokens": int(today_cost_row["tokens"] or 0),
                    "total_cost_cents": int(today_cost_row["cents"] or 0),
                    "invocation_count": int(today_cost_row["n"] or 0),
                    "since_utc": today_cutoff,
                },
                "active_errors_7d": sum(errors_7d.values()),
                "errors_by_type_7d": errors_7d,
            }
        return self._call("get_system_status", {}, _impl)

    # ════════════ (d) search_signals ════════════

    def search_signals(
        self,
        query: str,
        source: Optional[str] = None,
        days: int = SIGNAL_SEARCH_DEFAULT_DAYS,
        limit: int = SIGNAL_SEARCH_DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if not isinstance(query, str) or not query.strip():
                return {"ok": False, "error": "query must be non-empty str"}
            if source is not None and source not in PHASE1_SOURCES:
                return {
                    "ok": False,
                    "error": f"source must be one of {PHASE1_SOURCES} or null, got {source!r}",
                }
            if not isinstance(days, int) or days < 1:
                return {"ok": False, "error": "days must be int >= 1"}
            if not isinstance(limit, int) or limit < 1 or limit > SIGNAL_SEARCH_MAX_LIMIT:
                return {
                    "ok": False,
                    "error": f"limit must be int in [1, {SIGNAL_SEARCH_MAX_LIMIT}]",
                }
            cutoff = (
                datetime.now(tz=timezone.utc) - timedelta(days=days)
            ).isoformat()
            q = query.strip()
            pattern = f"%{q}%"

            if source:
                sql = (
                    """SELECT id, source, source_credibility, timestamp,
                              category, content, related_industries,
                              policy_direction, mixed_subtype
                       FROM info_units
                       WHERE created_at >= ?
                         AND source = ?
                         AND (content LIKE ? OR related_industries LIKE ?
                              OR category LIKE ?)
                       ORDER BY timestamp DESC
                       LIMIT ?"""
                )
                params: tuple = (cutoff, source, pattern, pattern, pattern, limit)
            else:
                sql = (
                    """SELECT id, source, source_credibility, timestamp,
                              category, content, related_industries,
                              policy_direction, mixed_subtype
                       FROM info_units
                       WHERE created_at >= ?
                         AND (content LIKE ? OR related_industries LIKE ?
                              OR category LIKE ?)
                       ORDER BY timestamp DESC
                       LIMIT ?"""
                )
                params = (cutoff, pattern, pattern, pattern, limit)

            rows = self.db.query(sql, params)

            signals: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    industries = json.loads(r["related_industries"] or "[]")
                except (ValueError, TypeError):
                    industries = []
                signals.append(
                    {
                        "id": r["id"],
                        "source": r["source"],
                        "source_credibility": r["source_credibility"],
                        "timestamp": r["timestamp"],
                        "category": r["category"],
                        "policy_direction": r["policy_direction"],
                        "mixed_subtype": r["mixed_subtype"],
                        "related_industries": industries if isinstance(industries, list) else [],
                        "content_preview": _content_preview(r["content"]),
                    }
                )
            return {
                "ok": True,
                "query": q,
                "source": source,
                "days": days,
                "limit": limit,
                "total_matched": len(signals),
                "signals": signals,
            }
        return self._call(
            "search_signals",
            {"query": query, "source": source, "days": days, "limit": limit},
            _impl,
        )

    # ════════════ (e) get_latest_weekly_report ════════════

    def get_latest_weekly_report(self, type: str) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if type not in VALID_REPORT_TYPES:
                return {
                    "ok": False,
                    "error": f"type must be one of {sorted(VALID_REPORT_TYPES)}, got {type!r}",
                }
            if not self.reports_dir.exists():
                return {
                    "ok": False,
                    "error": f"reports directory not found: {self.reports_dir}",
                }
            pattern = f"weekly_{type}_*.md"
            files = sorted(self.reports_dir.glob(pattern))
            if not files:
                return {
                    "ok": False,
                    "error": f"no {type} weekly report found in {self.reports_dir}",
                }
            latest = files[-1]
            try:
                content = latest.read_text(encoding="utf-8")
            except OSError as e:
                return {"ok": False, "error": f"read error: {e}"}
            mtime = datetime.fromtimestamp(
                latest.stat().st_mtime, tz=timezone.utc
            ).isoformat()
            return {
                "ok": True,
                "type": type,
                "filename": latest.name,
                "path": str(latest),
                "modified_at": mtime,
                "size_bytes": len(content.encode("utf-8")),
                "content": content,
            }
        return self._call("get_latest_weekly_report", {"type": type}, _impl)

    # ════════════ (f) add_industry ════════════

    def add_industry(self, industry: str, reason: str = "") -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if not isinstance(industry, str) or not industry.strip():
                return {"ok": False, "error": "industry must be non-empty str"}
            name = industry.strip()
            existing = self.db.query_one(
                "SELECT industry_id, zone FROM watchlist WHERE industry_name=?",
                (name,),
            )
            if existing is not None:
                return {
                    "ok": True,
                    "action": "already_exists",
                    "industry": name,
                    "industry_id": existing["industry_id"],
                    "zone": existing["zone"],
                }
            entered_at = now_utc()
            notes = reason.strip() if isinstance(reason, str) else ""
            new_id = self.db.write(
                """INSERT INTO watchlist
                   (industry_name, zone, entered_at, notes)
                   VALUES (?, 'active', ?, ?)""",
                (name, entered_at, notes),
            )
            return {
                "ok": True,
                "action": "inserted",
                "industry": name,
                "industry_id": new_id,
                "zone": "active",
                "entered_at": entered_at,
                "reason": notes,
            }
        return self._call(
            "add_industry",
            {"industry": industry, "reason": reason},
            _impl,
        )

    # ════════════ (g) remove_industry ════════════

    def remove_industry(self, industry: str, reason: str) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if not isinstance(industry, str) or not industry.strip():
                return {"ok": False, "error": "industry must be non-empty str"}
            if not isinstance(reason, str) or not reason.strip():
                return {"ok": False, "error": "reason must be non-empty str"}
            name = industry.strip()
            row = self.db.query_one(
                "SELECT industry_id, zone, notes FROM watchlist WHERE industry_name=?",
                (name,),
            )
            if row is None:
                return {
                    "ok": False,
                    "error": f"industry {name!r} not found in watchlist",
                }
            if row["zone"] == "cold":
                return {
                    "ok": True,
                    "action": "already_cold",
                    "industry": name,
                    "industry_id": row["industry_id"],
                }
            notes_combined = (
                f"{row['notes']}; removed({now_utc()}): {reason.strip()}"
                if row["notes"]
                else f"removed({now_utc()}): {reason.strip()}"
            )
            self.db.write(
                """UPDATE watchlist SET zone='cold', notes=?
                   WHERE industry_id=?""",
                (notes_combined, row["industry_id"]),
            )
            return {
                "ok": True,
                "action": "marked_cold",
                "industry": name,
                "industry_id": row["industry_id"],
                "reason": reason.strip(),
            }
        return self._call(
            "remove_industry",
            {"industry": industry, "reason": reason},
            _impl,
        )

    # ════════════ (h) get_industry_full_context ════════════

    def get_industry_full_context(self, industry: str) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if not isinstance(industry, str) or not industry.strip():
                return {"ok": False, "error": "industry must be non-empty str"}
            name = industry.strip()

            # watchlist
            wl_row = self.db.query_one(
                "SELECT * FROM watchlist WHERE industry_name=?", (name,)
            )
            watchlist_entry = dict(wl_row) if wl_row is not None else None

            # 近 180 天 info_units（限数防爆）
            cutoff = (
                datetime.now(tz=timezone.utc)
                - timedelta(days=FULL_CONTEXT_WINDOW_DAYS)
            ).isoformat()
            info_rows = self.db.query(
                """SELECT id, source, source_credibility, timestamp, category,
                          content, related_industries, policy_direction,
                          mixed_subtype, created_at
                   FROM info_units
                   WHERE created_at >= ? AND related_industries LIKE ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (cutoff, f"%{name}%", FULL_CONTEXT_INFO_UNITS_LIMIT),
            )
            recent_info_units = [
                _info_row_to_full_dict(r) for r in info_rows
            ]

            return {
                "ok": True,
                "industry": name,
                "snapshot_at": now_utc(),
                "window_days": FULL_CONTEXT_WINDOW_DAYS,
                "info_units_truncated_to": FULL_CONTEXT_INFO_UNITS_LIMIT,
                "watchlist_entry": watchlist_entry,
                "recent_info_units_180d": recent_info_units,
                # Phase 1 占位，Phase 2A 填充
                "related_stocks_with_financials": [],
                "industry_chain": [],
                "scout_uncovered_dimensions": [],
                "phase_notes": (
                    "Phase 1 简化：related_stocks/industry_chain/"
                    "scout_uncovered_dimensions 留空，Phase 2A 启用。"
                ),
            }
        return self._call(
            "get_industry_full_context", {"industry": industry}, _impl
        )

    # ════════════ (i) get_decision_context ════════════

    def master_analysis(self, stock_code: str) -> Dict[str, Any]:
        """5 大师评分（v1.03）。委托给 MasterAgent，缺数据降级返 ok=False。"""
        def _impl() -> Dict[str, Any]:
            if not isinstance(stock_code, str) or not stock_code.strip():
                return {"ok": False, "error": "stock_code must be non-empty str"}
            code = stock_code.strip()
            if len(code) != 6 or not code.isdigit():
                return {
                    "ok": False,
                    "error": f"stock_code 必须是 6 位数字，got {code!r}",
                }
            from agents.master_agent import MasterAgent
            agent = MasterAgent(self.db)
            return agent.analyze_stock(code)
        return self._call(
            "master_analysis", {"stock_code": stock_code}, _impl
        )

    # ════════════ (i) get_decision_context ════════════

    def get_decision_context(self, stock: str) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if not isinstance(stock, str) or not stock.strip():
                return {"ok": False, "error": "stock must be non-empty str"}
            code = stock.strip()
            rows = self.db.query(
                """SELECT id, industry_id, industry, sub_industry,
                          stock_code, stock_name, market, global_company_id,
                          discovery_source, confidence, status, updated_at
                   FROM related_stocks
                   WHERE stock_code = ?
                   ORDER BY updated_at DESC""",
                (code,),
            )
            entries = [dict(r) for r in rows]
            return {
                "ok": True,
                "stock": code,
                "snapshot_at": now_utc(),
                "related_stocks_entries": entries,
                "phase_notes": (
                    "Phase 1 简化：只返 related_stocks 表基础数据；"
                    "Phase 2A 加财务/持仓/行业论点。"
                ),
            }
        return self._call(
            "get_decision_context", {"stock": stock}, _impl
        )

    # ════════════ (j) get_policy_for_motivation_analysis ════════════

    def get_policy_for_motivation_analysis(
        self, info_unit_id: str
    ) -> Dict[str, Any]:
        def _impl() -> Dict[str, Any]:
            if not isinstance(info_unit_id, str) or not info_unit_id.strip():
                return {"ok": False, "error": "info_unit_id must be non-empty str"}
            uid = info_unit_id.strip()
            row = self.db.query_one(
                "SELECT * FROM info_units WHERE id = ?", (uid,)
            )
            if row is None:
                return {
                    "ok": False,
                    "error": f"info_unit_id {uid!r} not found",
                }
            info_unit = _info_row_to_full_dict(row)

            # 历史类似政策（Phase 1 基础版）：
            # 同 source + 同 category + 标题关键词近似。
            # 用 content JSON 里的 title 做子串 LIKE（取前 10 字作为种子）
            title_seed = _extract_title_seed(info_unit.get("content"))
            similar: List[Dict[str, Any]] = []
            if title_seed:
                # 最多 20 条最近 1 年的同源同类别的带"种子子串"的记录
                one_year_ago = (
                    datetime.now(tz=timezone.utc) - timedelta(days=365)
                ).isoformat()
                sim_rows = self.db.query(
                    """SELECT id, source, timestamp, category, content,
                              policy_direction, mixed_subtype
                       FROM info_units
                       WHERE source = ? AND category = ? AND id != ?
                         AND timestamp >= ? AND content LIKE ?
                       ORDER BY timestamp DESC
                       LIMIT 20""",
                    (
                        row["source"],
                        row["category"],
                        uid,
                        one_year_ago,
                        f"%{title_seed}%",
                    ),
                )
                for sr in sim_rows:
                    similar.append(
                        {
                            "id": sr["id"],
                            "source": sr["source"],
                            "timestamp": sr["timestamp"],
                            "category": sr["category"],
                            "policy_direction": sr["policy_direction"],
                            "mixed_subtype": sr["mixed_subtype"],
                            "content_preview": _content_preview(sr["content"]),
                        }
                    )

            return {
                "ok": True,
                "info_unit": info_unit,
                "similar_policies_count": len(similar),
                "similar_policies": similar,
                "title_seed_used": title_seed or "",
                "phase_notes": (
                    "Phase 1 基础版：同源 + 同类别 + 标题子串匹配；"
                    "Phase 2B 升级到语义检索 + 事件串关联。"
                ),
            }
        return self._call(
            "get_policy_for_motivation_analysis",
            {"info_unit_id": info_unit_id},
            _impl,
        )


# ══════════════════════ 辅助函数 ══════════════════════


def _content_preview(content_str: Optional[str], max_len: int = 160) -> str:
    """与 infra/dashboard._extract_content_preview 行为一致（去重抽取）。"""
    if not content_str:
        return ""
    try:
        obj = json.loads(content_str)
    except (ValueError, TypeError):
        return (
            content_str[:max_len]
            + ("..." if len(content_str) > max_len else "")
        )
    if isinstance(obj, dict):
        title = obj.get("title")
        summary = obj.get("summary") or obj.get("description")
        parts = []
        if title:
            parts.append(str(title))
        if summary:
            parts.append(str(summary))
        preview = " — ".join(parts) if parts else json.dumps(obj, ensure_ascii=False)
    elif isinstance(obj, list):
        preview = json.dumps(obj, ensure_ascii=False)
    else:
        preview = str(obj)
    return preview[:max_len] + ("..." if len(preview) > max_len else "")


def _info_row_to_full_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """info_units 行 → 完整 dict。related_industries / content 解析为 Python 对象。"""
    d: Dict[str, Any] = dict(row)
    ri = d.get("related_industries")
    if isinstance(ri, str):
        try:
            d["related_industries"] = json.loads(ri or "[]")
        except (ValueError, TypeError):
            d["related_industries"] = []
    content = d.get("content")
    if isinstance(content, str):
        try:
            d["content_parsed"] = json.loads(content)
        except (ValueError, TypeError):
            d["content_parsed"] = None
    return d


def _extract_title_seed(content: Any, max_len: int = 10) -> str:
    """从 content 里挖标题种子（前 N 字符），用于类似政策检索。"""
    if isinstance(content, dict):
        title = content.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:max_len]
    if isinstance(content, str):
        try:
            obj = json.loads(content)
        except (ValueError, TypeError):
            return content.strip()[:max_len]
        return _extract_title_seed(obj, max_len)
    return ""


# ══════════════════════ FastMCP 装配 ══════════════════════


def build_server(impl: "ScoutToolImpl") -> Any:
    """把 ScoutToolImpl 装饰到 FastMCP app。"""
    from mcp.server.fastmcp import FastMCP

    app = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @app.tool(description=(
        "列出 Scout watchlist 中 zone='active' 的行业。返回 "
        "{ok, count, industries[{industry_id, industry_name, zone, dimensions, "
        "verification_status, gap_status, entered_at, last_signal_at}]}。"
    ))
    def get_watchlist() -> dict:
        return impl.get_watchlist()

    @app.tool(description=(
        "查询单行业近 days 天的 dashboard（复用 Step 8 build_industry_dashboard）。"
        "参数：industry (必选, str)；days (可选, int, 默认 30)。"
        "返回 {ok, dashboard:{...}} 或 {ok:False, error}。"
    ))
    def ask_industry(industry: str, days: int = 30) -> dict:
        return impl.ask_industry(industry=industry, days=days)

    @app.tool(description=(
        "系统健康快照：数据库行数、各信源最后采集时间、今日 LLM 成本 "
        "(tokens/cents/count)、最近 7 日错误按类型计数。无参。"
    ))
    def get_system_status() -> dict:
        return impl.get_system_status()

    @app.tool(description=(
        "搜索 info_units：按子串匹配 content / related_industries / category。"
        "参数：query (必选, str)；source (可选, str，D1/D4/V1/V3/S4)；"
        "days (可选, int, 默认 30)；limit (可选, int, 默认 20, 上限 200)。"
        "返回 {ok, total_matched, signals[...]}。"
    ))
    def search_signals(
        query: str,
        source: Optional[str] = None,
        days: int = SIGNAL_SEARCH_DEFAULT_DAYS,
        limit: int = SIGNAL_SEARCH_DEFAULT_LIMIT,
    ) -> dict:
        return impl.search_signals(
            query=query, source=source, days=days, limit=limit
        )

    @app.tool(description=(
        "读最新的周报（Step 9 产出）。参数：type (必选, 'industry' 或 'paper')。"
        "返回 {ok, filename, modified_at, size_bytes, content} 或错误。"
    ))
    def get_latest_weekly_report(type: str) -> dict:
        return impl.get_latest_weekly_report(type=type)

    @app.tool(description=(
        "将行业加入 watchlist（zone='active'）。已存在 → action='already_exists'；"
        "否则 action='inserted'，返回新 industry_id。"
        "参数：industry (必选, str)；reason (可选, str)。"
    ))
    def add_industry(industry: str, reason: str = "") -> dict:
        return impl.add_industry(industry=industry, reason=reason)

    @app.tool(description=(
        "软删除行业：zone 改 'cold'，notes 记录时间+原因。真删留给 Phase 2A。"
        "参数：industry (必选, str)；reason (必选, str)。"
    ))
    def remove_industry(industry: str, reason: str) -> dict:
        return impl.remove_industry(industry=industry, reason=reason)

    @app.tool(description=(
        "给外部 LLM 的行业完整上下文：watchlist_entry + 近 180 天 info_units "
        "(最多 200 条) + related_stocks / industry_chain / "
        "scout_uncovered_dimensions (Phase 1 留空，Phase 2A 填充)。"
        "参数：industry (必选, str)。"
    ))
    def get_industry_full_context(industry: str) -> dict:
        return impl.get_industry_full_context(industry=industry)

    @app.tool(description=(
        "给外部 LLM 的个股决策上下文。Phase 1 只返 related_stocks 基础数据。"
        "参数：stock (必选, str)；返回 {ok, related_stocks_entries[...]}。"
    ))
    def get_decision_context(stock: str) -> dict:
        return impl.get_decision_context(stock=stock)

    @app.tool(description=(
        "政策动机分析上下文：指定 info_unit_id 的政策原文 + 近 1 年同源同类别 "
        "标题相似的历史政策（基础版语法匹配）。参数：info_unit_id (必选, str)。"
    ))
    def get_policy_for_motivation_analysis(info_unit_id: str) -> dict:
        return impl.get_policy_for_motivation_analysis(info_unit_id=info_unit_id)

    @app.tool(description=(
        "对单股运行 5 大师评分（巴菲特/芒格/段永平/林奇/费雪），"
        "数据来自 stock_financials。参数：stock_code (必选, A 股 6 位代码)。"
        "返回 {ok, stock, report (中文报告), results[{master, label, score, verdict, details}], "
        "report_period, analyzed_at}。stock_financials 无该股数据 → ok=False + 提示先跑 financial_agent。"
    ))
    def master_analysis(stock_code: str) -> dict:
        return impl.master_analysis(stock_code=stock_code)

    return app


# ══════════════════════ CLI 入口 ══════════════════════


def resolve_db_path(
    override: Optional[str] = None,
    config_path: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Phase 1 顺序：显式参数 > SCOUT_DB_PATH env > config.yaml database.knowledge_db"""
    if override:
        return override
    env_dict = env if env is not None else os.environ
    env_path = env_dict.get("SCOUT_DB_PATH")
    if env_path:
        return env_path
    from config.loader import load_config
    cfg = load_config(
        config_path=config_path or DEFAULT_CONFIG_PATH,
        env=env_dict,
    )
    return str(PROJECT_ROOT / cfg.database.knowledge_db)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口：启动 MCP stdio 服务。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Scout MCP Server (stdio transport)"
    )
    parser.add_argument(
        "--db",
        default=None,
        help="info_units DB 路径（默认读 SCOUT_DB_PATH env 或 config.yaml）",
    )
    parser.add_argument(
        "--reports-dir",
        default=str(DEFAULT_REPORTS_DIR),
        help="周报目录（默认 <root>/reports）",
    )
    parser.add_argument(
        "--logs-dir",
        default=str(DEFAULT_LOGS_DIR),
        help="MCP access log 目录（默认 <root>/logs）",
    )
    args = parser.parse_args(argv)

    db_path = resolve_db_path(override=args.db)
    reports_dir = Path(args.reports_dir)
    logs_dir = Path(args.logs_dir)

    # 启动前先打到 stderr（stdout 归 MCP 用）
    print(
        f"[scout-mcp] db={db_path} reports={reports_dir} logs={logs_dir}",
        file=sys.stderr,
        flush=True,
    )

    db = DatabaseManager(db_path)
    access_logger = build_access_logger(logs_dir)
    try:
        impl = ScoutToolImpl(
            db=db, reports_dir=reports_dir, access_logger=access_logger
        )
        app = build_server(impl)
        # FastMCP.run 是阻塞的，直到 stdin 关闭
        app.run(transport="stdio")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
