#!/usr/bin/env python
"""
scripts/phase1_acceptance.py — Scout Phase 1 最终验收脚本（Step 14）

自动跑完 14 项验收矩阵：

    架构完整性（4）
        1. 20 张表 + 关键索引存在
        2. Pydantic 契约可用
        3. 5 个 Collector adapter 可实例化
        4. BaseAgent 错误传播矩阵完整

    功能完整性（6）
        5. SignalCollector 能处理真实 D1 政策（无 D1 数据 → SKIP）
        6. Dashboard 对 '半导体' 能生成
        7. DirectionJudge 能生成周报 + Gemma 离线降级
        8. MCP Server 10 个工具全可调用
        9. PushQueue 生产-订阅-送达
        10. main.py status 能运行

    冷启动数据（3）
        11. watchlist 含 15 行业（14 active + 1 observation）
        12. system_meta.user_principles 有 5 条
        13. system_meta.user_context 有 8 字段

    质量保证（1）
        14. pytest 全量通过（≥ 1031 tests, 0 failed）

用法：
    python scripts/phase1_acceptance.py                  # 默认读 data/knowledge.db
    python scripts/phase1_acceptance.py --db data/test_knowledge.db
    python scripts/phase1_acceptance.py --skip-pytest    # 跳过 check 14（省 25s）

退出码：0 全部通过 / 1 有 FAIL / 2 参数错误
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ══════════════════════ 常量 ══════════════════════

DEFAULT_DB = PROJECT_ROOT / "data" / "knowledge.db"
TEST_DB = PROJECT_ROOT / "data" / "test_knowledge.db"

EXPECTED_TABLES_20 = [
    "info_units", "watchlist", "industry_dict", "industry_chain",
    "related_stocks", "info_industry_map", "track_list", "stock_financials",
    "rules", "system_meta", "event_chains", "motivation_drift_log",
    "global_companies", "llm_invocations", "agent_errors", "recommendations",
    "user_decisions", "price_tracking", "review_results", "rejected_stocks",
]

KEY_INDEXES = [
    "idx_iu_source_time",
    "idx_iu_status",
    "idx_wl_name",
    "idx_wl_zone",
    "idx_llm_agent_time",
    "idx_ae_type_time",
]

EXPECTED_15_INDUSTRIES = [
    "半导体设备", "HBM", "医疗器械国产替代", "工业自动化",
    "AI算力", "AI应用软件", "数据中心配套",
    "核电", "特高压", "储能细分",
    "造船海工", "韩国电池",
    "军工", "创新药", "人形机器人",
]

EXPECTED_USER_CONTEXT_KEYS = {
    "investor_type", "capital_range", "holding_horizon", "markets",
    "tech_background", "tools_stack", "api_key_status", "phase",
}

EXPECTED_5_MCP_TOOLS = {
    "get_watchlist", "ask_industry", "get_system_status",
    "search_signals", "get_latest_weekly_report",
    "add_industry", "remove_industry",
    "get_industry_full_context", "get_decision_context",
    "get_policy_for_motivation_analysis",
}

MIN_PYTEST_TESTS = 1031


# ══════════════════════ 框架 ══════════════════════


@dataclass
class Check:
    id: int
    category: str
    name: str
    fn: Callable[["AcceptanceContext"], Tuple[str, str]]  # (status, details)


class AcceptanceContext:
    def __init__(self, db_path: Path, skip_pytest: bool = False):
        self.db_path = db_path
        self.skip_pytest = skip_pytest
        self.start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.start


class Result:
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ══════════════════════ 架构完整性 ══════════════════════


def check_1_schema(ctx: AcceptanceContext) -> Tuple[str, str]:
    """20 张表 + 关键索引存在"""
    if not ctx.db_path.exists():
        return Result.FAIL, f"db 不存在: {ctx.db_path}"
    conn = _connect(ctx.db_path)
    try:
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    finally:
        conn.close()

    missing_tables = [t for t in EXPECTED_TABLES_20 if t not in tables]
    if missing_tables:
        return Result.FAIL, f"missing tables: {missing_tables}"
    missing_indexes = [i for i in KEY_INDEXES if i not in indexes]
    if missing_indexes:
        return Result.FAIL, f"missing indexes: {missing_indexes}"
    return Result.PASS, f"{len(EXPECTED_TABLES_20)} tables, {len(KEY_INDEXES)} key indexes"


def check_2_contracts(ctx: AcceptanceContext) -> Tuple[str, str]:
    """Pydantic 契约可实例化"""
    from datetime import datetime, timezone
    from contracts.contracts import InfoUnitV1, WatchlistUpdateV1, AgentError
    from utils.hash_utils import info_unit_id

    now = datetime.now(tz=timezone.utc).isoformat()
    unit = InfoUnitV1(
        id=info_unit_id("D1", "测试政策", "2026-04-18"),
        source="D1", source_credibility="权威",
        timestamp=now, category="政策发布", content="{}",
    )
    wl = WatchlistUpdateV1(industry_id=1)
    err = AgentError(
        agent_name="x", error_type="parse", error_message="m", occurred_at=now,
    )
    assert unit.source == "D1" and wl.industry_id == 1 and err.error_type == "parse"
    return Result.PASS, "InfoUnitV1 / WatchlistUpdateV1 / AgentError 实例化成功"


def check_3_adapters(ctx: AcceptanceContext) -> Tuple[str, str]:
    """5 个 Collector adapter 可实例化"""
    from infra.db_manager import DatabaseManager
    db = DatabaseManager(ctx.db_path)
    names: List[str] = []
    try:
        from infra.data_adapters.akshare_wrapper import AkShareCollector
        names.append(f"S4={AkShareCollector(db=db).SOURCE_CODE}")
        from infra.data_adapters.arxiv_semantic import PaperCollector
        names.append(f"D4={PaperCollector(db=db).SOURCE_CODE}")
        from infra.data_adapters.nbs import NBSCollector
        names.append(f"V1={NBSCollector(db=db).SOURCE_CODE}")
        from infra.data_adapters.korea_customs import KoreaCustomsCollector
        names.append(f"V3={KoreaCustomsCollector(db=db).SOURCE_CODE}")
        from infra.data_adapters.gov_cn import GovCNCollector
        names.append(f"D1={GovCNCollector(db=db).SOURCE_CODE}")
    finally:
        db.close()
    return Result.PASS, "; ".join(names)


def check_4_baseagent(ctx: AcceptanceContext) -> Tuple[str, str]:
    """BaseAgent 错误传播矩阵完整（6 类错误类 + run_with_error_handling）"""
    from agents.base import (
        BaseAgent, ScoutError, NetworkError, ParseError, LLMError,
        RuleViolation, DataMissingError,
    )
    # 6 类错误
    classes = [NetworkError, ParseError, LLMError, RuleViolation, DataMissingError]
    for cls in classes:
        assert issubclass(cls, ScoutError), f"{cls.__name__} not ScoutError subclass"
    # run_with_error_handling 存在
    assert hasattr(BaseAgent, "run_with_error_handling")
    assert BaseAgent.MAX_RETRIES >= 1 and BaseAgent.RETRY_BACKOFF_BASE >= 1
    return Result.PASS, (
        f"5 Scout error classes + ScoutError base; "
        f"MAX_RETRIES={BaseAgent.MAX_RETRIES}"
    )


# ══════════════════════ 功能完整性 ══════════════════════


def check_5_signal_collector(ctx: AcceptanceContext) -> Tuple[str, str]:
    """SignalCollector 能处理真实 D1 政策（无 Ollama 或无 D1 数据 → SKIP）"""
    # 找 D1 数据
    source_db = ctx.db_path
    conn = _connect(source_db)
    try:
        row = conn.execute(
            """SELECT id, timestamp, content FROM info_units
               WHERE source='D1' LIMIT 1"""
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        # fallback 到 test_knowledge.db
        if TEST_DB.exists() and TEST_DB != source_db:
            conn = _connect(TEST_DB)
            try:
                row = conn.execute(
                    """SELECT id, timestamp, content FROM info_units
                       WHERE source='D1' LIMIT 1"""
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                source_db = TEST_DB

    if row is None:
        return Result.SKIP, (
            "无 D1 数据可测试；先跑 `python main.py collect --source D1 --days 7`"
        )

    # Ollama 可达性
    import httpx
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        if r.status_code != 200:
            return Result.SKIP, "Ollama 不可达（返回非 200）"
    except Exception as e:
        return Result.SKIP, f"Ollama 不可达（{type(e).__name__}: {e}）"

    try:
        content_obj = json.loads(row["content"])
        title = content_obj.get("title") or "D1 政策标题"
        raw_text = content_obj.get("summary") or content_obj.get("title") or "政策内容"
    except (ValueError, TypeError):
        title = "D1 政策"
        raw_text = row["content"][:500] if row["content"] else "政策"

    from agents.signal_collector import SignalCollectorAgent
    from infra.db_manager import DatabaseManager

    # 用隔离的 tmp DB，避免往 production 写 llm_invocations
    tmp = Path(tempfile.mkdtemp(prefix="scout_accept_check5_")) / "k.db"
    from knowledge.init_db import init_database
    init_database(tmp)
    db = DatabaseManager(tmp)
    try:
        agent = SignalCollectorAgent(db=db, timeout=60.0)
        unit = agent.process(
            raw_text=raw_text[:1000],
            source="D1",
            title=title[:100],
            published_date=(row["timestamp"] or "2026-04-18")[:10],
        )
    finally:
        db.close()

    if unit is None:
        return Result.FAIL, "SignalCollector.process 返 None（agent_errors 查看详情）"
    return Result.PASS, (
        f"processed D1 policy → direction={unit.policy_direction} "
        f"category={unit.category} industries={unit.related_industries}"
    )


def check_6_dashboard(ctx: AcceptanceContext) -> Tuple[str, str]:
    """Dashboard 对 '半导体' 能生成"""
    from infra.dashboard import build_industry_dashboard
    from infra.db_manager import DatabaseManager
    db = DatabaseManager(ctx.db_path)
    try:
        d = build_industry_dashboard("半导体", db, days=30)
    finally:
        db.close()
    required_keys = {
        "industry", "snapshot_at", "recent_signals_total",
        "recent_signals_by_source", "policy_direction_distribution",
        "mixed_subtype_breakdown", "source_credibility_weighted_count",
        "latest_signals", "data_freshness", "watchlist_status",
    }
    missing = required_keys - set(d.keys())
    if missing:
        return Result.FAIL, f"missing keys: {missing}"
    return Result.PASS, (
        f"industry=半导体 total={d['recent_signals_total']} "
        f"watchlist={'Y' if d['watchlist_status'] else 'N'}"
    )


def check_7_direction_judge(ctx: AcceptanceContext) -> Tuple[str, str]:
    """DirectionJudge 能生成周报 + Gemma 离线降级"""
    from agents.direction_judge import (
        DirectionJudgeAgent, GEMMA_OFFLINE_BANNER,
    )
    from infra.db_manager import DatabaseManager

    # 用 FakeOllama 模拟"Gemma 离线"，avoid 真连网络
    class _FakeOllamaDown:
        def chat(self, **kwargs):
            raise ConnectionError("simulated offline for acceptance check")

    # tmp reports 目录，防止污染 reports/
    tmp_reports = Path(tempfile.mkdtemp(prefix="scout_accept_check7_"))
    db = DatabaseManager(ctx.db_path)
    try:
        agent = DirectionJudgeAgent(
            db=db, ollama_client=_FakeOllamaDown(),
            reports_dir=tmp_reports,
        )
        # 指定一个行业避免遍历所有 active；半导体应该存在
        report, path = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=True, save=False,
        )
    finally:
        db.close()

    if not isinstance(report, str):
        return Result.FAIL, f"report 非 str: {type(report).__name__}"
    if GEMMA_OFFLINE_BANNER not in report:
        return Result.FAIL, "报告未出现 GEMMA_OFFLINE_BANNER（降级路径未触发）"
    if "半导体" not in report:
        return Result.FAIL, "报告未包含行业名"
    return Result.PASS, (
        f"weekly_industry_report ok，{len(report)} chars，"
        f"offline banner 正确显示"
    )


def check_8_mcp(ctx: AcceptanceContext) -> Tuple[str, str]:
    """MCP Server 10 个工具全可调用"""
    from infra.db_manager import DatabaseManager
    from infra.mcp_server import (
        SERVER_NAME, ScoutToolImpl, build_server,
    )

    db = DatabaseManager(ctx.db_path)
    try:
        impl = ScoutToolImpl(db=db, reports_dir=PROJECT_ROOT / "reports")
        app = build_server(impl)
        tools = asyncio.run(app.list_tools())
        tool_names = {t.name for t in tools}
    finally:
        db.close()

    if len(tools) != 10:
        return Result.FAIL, f"expected 10 tools, got {len(tools)}"
    missing = EXPECTED_5_MCP_TOOLS - tool_names
    if missing:
        return Result.FAIL, f"missing tools: {missing}"

    # 每个工具基础调用
    db = DatabaseManager(ctx.db_path)
    try:
        impl = ScoutToolImpl(db=db, reports_dir=PROJECT_ROOT / "reports")
        results: List[Tuple[str, bool, str]] = []

        results.append(("get_watchlist", impl.get_watchlist()["ok"], ""))
        results.append(("ask_industry", impl.ask_industry("半导体")["ok"], ""))
        results.append(("get_system_status", impl.get_system_status()["ok"], ""))
        results.append(("search_signals", impl.search_signals("通知", days=365)["ok"], ""))
        r_report = impl.get_latest_weekly_report("industry")
        # get_latest_weekly_report 可能 ok=false（无报告），但不应 raise
        results.append(("get_latest_weekly_report", True, f"ok={r_report['ok']}"))
        r_add = impl.add_industry("__acceptance_probe__", reason="phase1 check 8")
        results.append(("add_industry", r_add["ok"], ""))
        r_rem = impl.remove_industry("__acceptance_probe__", reason="cleanup")
        results.append(("remove_industry", r_rem["ok"], ""))
        r_ctx = impl.get_industry_full_context("半导体")
        results.append(("get_industry_full_context", r_ctx["ok"], ""))
        r_dec = impl.get_decision_context("__acceptance_probe__")
        results.append(("get_decision_context", r_dec["ok"], ""))
        # get_policy_for_motivation_analysis 需要真实 info_unit_id
        any_row = db.query_one("SELECT id FROM info_units LIMIT 1")
        if any_row is not None:
            r_pol = impl.get_policy_for_motivation_analysis(any_row["id"])
            results.append(("get_policy_for_motivation_analysis", r_pol["ok"], ""))
        else:
            results.append(("get_policy_for_motivation_analysis", True, "no info_unit skipped"))
    finally:
        db.close()

    failures = [name for name, ok, _ in results if not ok]
    if failures:
        return Result.FAIL, f"tool calls failed: {failures}"
    return Result.PASS, (
        f"server={SERVER_NAME} | 10 tools registered | "
        f"{len(results)} calls all ok"
    )


def check_9_push_queue(ctx: AcceptanceContext) -> Tuple[str, str]:
    """PushQueue 生产-订阅-送达-失败重试完整流程"""
    from infra.push_queue import PushQueue
    from infra.queue_manager import QueueManager
    from knowledge.init_queue_db import init_queue_db

    # 用 tmp queue.db 避免污染生产
    tmp = Path(tempfile.mkdtemp(prefix="scout_accept_check9_")) / "q.db"
    init_queue_db(tmp)
    qm = QueueManager(tmp)
    try:
        pq = PushQueue(qm)
        # 1. push 3 条不同优先级
        ev_r = pq.push_alert("data_source_down", {"source": "V3"})  # red
        ev_d = pq.push_daily_briefing({"highlights": ["test"]},
                                       target_date="20260418")        # blue
        ev_w = pq.push_weekly_report("industry", {"summary": "x"},
                                      target_date="20260418")          # blue

        # 2. 幂等：重推 alert → 返回原 event_id
        ev_r2 = pq.push_alert("data_source_down", {"source": "V3"})
        assert ev_r == ev_r2, "entity_key 幂等失效"

        # 3. poll：red 优先级在最前
        pending = pq.poll_pending(max=10)
        assert len(pending) == 3, f"expected 3 pending, got {len(pending)}"
        assert pending[0]["priority"] == "red", f"first not red: {pending[0]['priority']}"

        # 4. 送达 + 失败
        assert pq.mark_delivered(ev_r) is True
        assert pq.mark_failed(ev_w, "simulated") is True   # retries=1, 回 pending
        stats = pq.queue_stats()
        # ev_r done; ev_d pending; ev_w pending (retried)
    finally:
        qm.close()

    if stats["done"] != 1:
        return Result.FAIL, f"expected done=1, got stats={stats}"
    return Result.PASS, (
        f"push 3 items（1 red + 2 blue），幂等 ok，"
        f"poll 排序 ok，deliver+retry ok；final stats={stats}"
    )


def check_10_main_status(ctx: AcceptanceContext) -> Tuple[str, str]:
    """main.py status 能运行"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"),
         "--db", str(ctx.db_path), "status"],
        cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, timeout=30,
    )
    if r.returncode != 0:
        return Result.FAIL, f"rc={r.returncode}; stderr: {r.stderr.decode('utf-8','replace')[-200:]}"
    out = r.stdout.decode("utf-8", "replace")
    for expected in ("Scout 系统状态", "数据库", "健康度"):
        if expected not in out:
            return Result.FAIL, f"missing '{expected}' in output"
    return Result.PASS, f"rc=0 | {len(out)} chars | 关键字段完整"


# ══════════════════════ 冷启动数据 ══════════════════════


def check_11_watchlist(ctx: AcceptanceContext) -> Tuple[str, str]:
    """watchlist 含 15 行业（14 active + 1 observation）"""
    conn = _connect(ctx.db_path)
    try:
        rows = conn.execute(
            "SELECT industry_name, zone FROM watchlist"
        ).fetchall()
    finally:
        conn.close()

    names = {r["industry_name"] for r in rows}
    zones_count: Dict[str, int] = {}
    for r in rows:
        zones_count[r["zone"]] = zones_count.get(r["zone"], 0) + 1

    missing = [n for n in EXPECTED_15_INDUSTRIES if n not in names]
    if missing:
        return Result.FAIL, f"missing industries: {missing}"

    active = zones_count.get("active", 0)
    observation = zones_count.get("observation", 0)
    if active != 14 or observation != 1:
        return Result.FAIL, (
            f"expected 14 active + 1 observation, got "
            f"active={active} observation={observation}; "
            f"total={len(rows)}"
        )
    return Result.PASS, f"15 industries seeded: {active} active + {observation} observation"


def check_12_principles(ctx: AcceptanceContext) -> Tuple[str, str]:
    """system_meta.user_principles 有 5 条（结构化 dict）"""
    conn = _connect(ctx.db_path)
    try:
        row = conn.execute(
            "SELECT value FROM system_meta WHERE key='user_principles'"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return Result.FAIL, "system_meta.user_principles 不存在"
    try:
        principles = json.loads(row["value"])
    except (ValueError, TypeError) as e:
        return Result.FAIL, f"JSON parse error: {e}"

    if not isinstance(principles, list) or len(principles) != 5:
        return Result.FAIL, f"expected 5 items, got {len(principles)}"

    ids = []
    for i, p in enumerate(principles):
        if not isinstance(p, dict):
            return Result.FAIL, f"principle[{i}] not dict"
        if not p.get("id") or not p.get("title") or not p.get("core"):
            return Result.FAIL, f"principle[{i}] missing id/title/core"
        ids.append(p["id"])
    return Result.PASS, f"5 structured principles: {ids}"


def check_13_user_context(ctx: AcceptanceContext) -> Tuple[str, str]:
    """system_meta.user_context 有 8 字段"""
    conn = _connect(ctx.db_path)
    try:
        row = conn.execute(
            "SELECT value FROM system_meta WHERE key='user_context'"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return Result.FAIL, "system_meta.user_context 不存在"
    try:
        ctx_data = json.loads(row["value"])
    except (ValueError, TypeError) as e:
        return Result.FAIL, f"JSON parse error: {e}"

    missing = EXPECTED_USER_CONTEXT_KEYS - set(ctx_data.keys())
    if missing:
        return Result.FAIL, f"missing keys: {sorted(missing)}"
    return Result.PASS, (
        f"{len(ctx_data)} keys: investor_type={ctx_data.get('investor_type')!r}, "
        f"phase={ctx_data.get('phase')!r}"
    )


# ══════════════════════ 质量保证 ══════════════════════


def check_14_pytest(ctx: AcceptanceContext) -> Tuple[str, str]:
    """pytest 全量通过（≥ 1031 tests, 0 failed）"""
    if ctx.skip_pytest:
        return Result.SKIP, "用户指定 --skip-pytest"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
        cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, timeout=120,
    )
    stdout = r.stdout.decode("utf-8", "replace")
    # 找最后一行 "N passed"
    import re
    m = re.search(r"(\d+)\s+passed", stdout)
    passed_count = int(m.group(1)) if m else 0
    f_match = re.search(r"(\d+)\s+failed", stdout)
    failed_count = int(f_match.group(1)) if f_match else 0

    if r.returncode != 0:
        return Result.FAIL, (
            f"pytest rc={r.returncode}, passed={passed_count}, "
            f"failed={failed_count}"
        )
    if passed_count < MIN_PYTEST_TESTS:
        return Result.FAIL, (
            f"expected ≥ {MIN_PYTEST_TESTS} tests, got {passed_count}"
        )
    return Result.PASS, f"{passed_count} tests passed, 0 failed"


# ══════════════════════ 矩阵组装 ══════════════════════


ALL_CHECKS: List[Check] = [
    # 架构完整性
    Check(1, "架构", "20张表全部存在+关键索引", check_1_schema),
    Check(2, "架构", "Pydantic契约可用", check_2_contracts),
    Check(3, "架构", "5个信源adapter可实例化", check_3_adapters),
    Check(4, "架构", "Agent基类错误传播矩阵完整", check_4_baseagent),
    # 功能完整性
    Check(5, "功能", "SignalCollector处理真实D1政策", check_5_signal_collector),
    Check(6, "功能", "Dashboard对半导体能生成", check_6_dashboard),
    Check(7, "功能", "DirectionJudge周报+Gemma离线降级", check_7_direction_judge),
    Check(8, "功能", "MCP Server 10工具全可调用", check_8_mcp),
    Check(9, "功能", "PushQueue生产-订阅-送达", check_9_push_queue),
    Check(10, "功能", "main.py status能输出", check_10_main_status),
    # 冷启动数据
    Check(11, "数据", "watchlist含15行业", check_11_watchlist),
    Check(12, "数据", "system_meta.user_principles有5条", check_12_principles),
    Check(13, "数据", "system_meta.user_context有8字段", check_13_user_context),
    # 质量
    Check(14, "质量", "pytest全量通过", check_14_pytest),
]


# ══════════════════════ 执行 & 报告 ══════════════════════


def run_all(ctx: AcceptanceContext) -> List[Tuple[Check, str, str, float]]:
    """返 (check, status, details, duration_sec)[]"""
    results: List[Tuple[Check, str, str, float]] = []
    for c in ALL_CHECKS:
        t0 = time.monotonic()
        try:
            status, details = c.fn(ctx)
        except Exception as e:
            status = Result.FAIL
            details = f"unhandled {type(e).__name__}: {e}"
        dt = time.monotonic() - t0
        results.append((c, status, details, dt))
        badge = {"PASS": "✓", "FAIL": "✗", "SKIP": "~"}[status]
        print(
            f"[{status:4s}] {badge} [{c.category}] "
            f"{c.id:>2}. {c.name}  ({dt:.1f}s)"
        )
        if details:
            print(f"        {details[:160]}")
    return results


def summarize(results: List[Tuple[Check, str, str, float]]) -> int:
    """打印总结，返回退出码。"""
    by_cat: Dict[str, Dict[str, int]] = {}
    for c, st, _, _ in results:
        d = by_cat.setdefault(c.category, {"PASS": 0, "FAIL": 0, "SKIP": 0, "TOTAL": 0})
        d[st] = d.get(st, 0) + 1
        d["TOTAL"] += 1

    print()
    print("=" * 64)
    print("Scout Phase 1 验收报告")
    print("=" * 64)

    total_pass = 0
    total_fail = 0
    total_skip = 0
    for cat in ("架构", "功能", "数据", "质量"):
        d = by_cat.get(cat, {"PASS": 0, "FAIL": 0, "SKIP": 0, "TOTAL": 0})
        badge = "✅" if d["FAIL"] == 0 and d["SKIP"] == 0 else (
            "⚠️ " if d["FAIL"] == 0 else "❌"
        )
        extra = ""
        if d["SKIP"] > 0:
            extra = f" (SKIP={d['SKIP']})"
        if d["FAIL"] > 0:
            extra = f" (FAIL={d['FAIL']})"
        print(f"{cat:<4}: {d['PASS']}/{d['TOTAL']} {badge}{extra}")
        total_pass += d["PASS"]
        total_fail += d["FAIL"]
        total_skip += d["SKIP"]

    total = len(results)
    print()
    print(f"总计: {total_pass}/{total} 通过  "
          f"(PASS={total_pass}, FAIL={total_fail}, SKIP={total_skip})")

    if total_fail > 0:
        print("\n❌ Phase 1 验收未通过 — 以下检查失败：")
        for c, st, details, _ in results:
            if st == Result.FAIL:
                print(f"  - #{c.id} {c.name}: {details[:120]}")
        return 1

    if total_skip > 0:
        print(f"\n⚠️  Phase 1 验收条件通过（{total_skip} 项 SKIP 需手动确认）")
        for c, st, details, _ in results:
            if st == Result.SKIP:
                print(f"  - #{c.id} {c.name}: {details[:120]}")
        return 0

    print("\n🎉 Phase 1 验收通过")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scout Phase 1 Acceptance")
    parser.add_argument(
        "--db", default=str(DEFAULT_DB),
        help=f"knowledge.db 路径（默认 {DEFAULT_DB.relative_to(PROJECT_ROOT)}）",
    )
    parser.add_argument(
        "--skip-pytest", action="store_true",
        help="跳过 check 14（pytest 全量）",
    )
    args = parser.parse_args(argv)

    ctx = AcceptanceContext(
        db_path=Path(args.db).resolve(),
        skip_pytest=args.skip_pytest,
    )

    print("=" * 64)
    print("Scout Phase 1 验收矩阵 — 14 项自动检查")
    print(f"db: {ctx.db_path}")
    print(f"skip_pytest: {ctx.skip_pytest}")
    print("=" * 64)
    print()

    results = run_all(ctx)
    print(f"\n[elapsed] {ctx.elapsed():.1f}s")
    return summarize(results)


if __name__ == "__main__":
    sys.exit(main())
