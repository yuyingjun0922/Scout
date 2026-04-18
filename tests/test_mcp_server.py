"""
tests/test_mcp_server.py — MCP Server 测试矩阵

两层覆盖：
    Layer A: ScoutToolImpl 直接调用（快，覆盖所有分支）
    Layer B: FastMCP.call_tool（JSON-RPC 语义验证 + 工具注册）

覆盖：
    - 10 个工具各自的基础调用
    - 入参校验：空字符串 / 非法 source / days<1 / limit 超限 / 非法 type
    - 错误场景：不存在的行业 / 不存在的 info_unit_id / 不存在的 stock
    - DB 错误降级
    - add_industry 幂等（INSERT OR IGNORE）
    - remove_industry 软删（zone='cold'）+ 已 cold 的二次调用
    - search_signals source 过滤 / days 过滤 / limit 截断
    - content_preview 从 JSON / 纯文本两种来源抽取
    - logs/mcp_access.log 写入
    - FastMCP：list_tools 返 10 个工具 / call_tool 正确返结构
    - 并发 50 次 ScoutToolImpl 调用无异常（Phase 1 level 并发）
    - resolve_db_path：env / config / 显式参数 三优先级
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from infra.db_manager import DatabaseManager
from infra.mcp_server import (
    PHASE1_SOURCES,
    SIGNAL_SEARCH_DEFAULT_DAYS,
    SIGNAL_SEARCH_DEFAULT_LIMIT,
    SIGNAL_SEARCH_MAX_LIMIT,
    SERVER_NAME,
    ScoutToolImpl,
    _content_preview,
    _extract_title_seed,
    build_access_logger,
    build_server,
    resolve_db_path,
)
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "mcp_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db, db_path
    db.close()


@pytest.fixture
def tmp_reports(tmp_path):
    d = tmp_path / "reports"
    return d


@pytest.fixture
def tmp_logs(tmp_path):
    d = tmp_path / "logs"
    return d


@pytest.fixture
def impl(tmp_db, tmp_reports, tmp_logs):
    db, _ = tmp_db
    logger = build_access_logger(tmp_logs)
    return ScoutToolImpl(db=db, reports_dir=tmp_reports, access_logger=logger)


# ── seed helpers ──


def _days_ago(n: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).isoformat()


def _insert_info_unit(
    db, *, unit_id="u1", source="D1", source_credibility="权威",
    timestamp=None, category="政策发布", content="{}",
    related_industries=None, policy_direction=None, mixed_subtype=None,
    created_at=None,
):
    if timestamp is None:
        timestamp = _days_ago(1)
    if related_industries is None:
        related_industries = ["半导体"]
    if created_at is None:
        created_at = timestamp
    db.write(
        """INSERT INTO info_units
           (id, source, source_credibility, timestamp, category, content,
            related_industries, policy_direction, mixed_subtype,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            unit_id, source, source_credibility, timestamp, category, content,
            json.dumps(related_industries, ensure_ascii=False),
            policy_direction, mixed_subtype, created_at, created_at,
        ),
    )


def _insert_watchlist(db, industry_name, **kw):
    return db.write(
        """INSERT INTO watchlist
           (industry_name, zone, dimensions, verification_status,
            gap_status, entered_at, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            industry_name,
            kw.get("zone", "active"),
            kw.get("dimensions"),
            kw.get("verification_status"),
            kw.get("gap_status", "active"),
            kw.get("entered_at", _days_ago(5)),
            kw.get("notes", ""),
        ),
    )


def _insert_related_stock(db, stock_code, industry="半导体", **kw):
    return db.write(
        """INSERT INTO related_stocks
           (stock_code, stock_name, industry, market, confidence,
            discovery_source, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            stock_code,
            kw.get("stock_name", "示例股"),
            industry,
            kw.get("market", "A"),
            kw.get("confidence", "staging"),
            kw.get("discovery_source", "seed"),
            kw.get("updated_at", _days_ago(1)),
        ),
    )


def _insert_llm_invocation(db, tokens=100, invoked_at=None):
    if invoked_at is None:
        invoked_at = _days_ago(0)
    db.write(
        """INSERT INTO llm_invocations
           (agent_name, prompt_version, model_name, input_hash,
            output_summary, tokens_used, cost_cents, invoked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("signal_collector", "signal_collector_v001", "gemma4:e4b",
         "abc123", "summary", tokens, 0, invoked_at),
    )


def _insert_agent_error(db, error_type="network", occurred_at=None):
    if occurred_at is None:
        occurred_at = _days_ago(0)
    db.write(
        """INSERT INTO agent_errors
           (agent_name, error_type, error_message, occurred_at)
           VALUES (?, ?, ?, ?)""",
        ("some_agent", error_type, "test error", occurred_at),
    )


# ═══════════════════ 模块辅助函数 ═══════════════════


class TestContentPreview:
    def test_json_dict_with_title(self):
        c = json.dumps({"title": "政策 A", "summary": "推进"}, ensure_ascii=False)
        p = _content_preview(c)
        assert "政策 A" in p and "推进" in p

    def test_plain_text(self):
        assert _content_preview("raw text") == "raw text"

    def test_none_returns_empty(self):
        assert _content_preview(None) == ""

    def test_long_truncates(self):
        c = "a" * 500
        p = _content_preview(c, max_len=100)
        assert len(p) <= 105
        assert p.endswith("...")


class TestTitleSeed:
    def test_from_dict(self):
        c = {"title": "关于开展 2026 年城市更新"}
        assert _extract_title_seed(c, max_len=5) == "关于开展 "

    def test_from_json_string(self):
        c = json.dumps({"title": "某政策通知"}, ensure_ascii=False)
        seed = _extract_title_seed(c, max_len=3)
        assert seed == "某政策"

    def test_plain_string_fallback(self):
        assert _extract_title_seed("plain text", max_len=5) == "plain"

    def test_empty(self):
        assert _extract_title_seed(None) == ""
        assert _extract_title_seed({}) == ""


# ═══════════════════ Layer A: ScoutToolImpl 直接 ═══════════════════


class TestGetWatchlist:
    def test_empty(self, impl):
        r = impl.get_watchlist()
        assert r["ok"] is True
        assert r["count"] == 0
        assert r["industries"] == []

    def test_only_active(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_watchlist(db, "半导体", zone="active", dimensions=4)
        _insert_watchlist(db, "旧行业", zone="cold")
        _insert_watchlist(db, "新能源", zone="active", dimensions=3)

        r = impl.get_watchlist()
        assert r["count"] == 2
        names = [x["industry_name"] for x in r["industries"]]
        assert "半导体" in names
        assert "新能源" in names
        assert "旧行业" not in names

    def test_sorted_by_name(self, impl, tmp_db):
        db, _ = tmp_db
        for name in ("新能源", "半导体", "光伏"):
            _insert_watchlist(db, name, zone="active")
        names = [x["industry_name"] for x in impl.get_watchlist()["industries"]]
        assert names == sorted(names)


class TestAskIndustry:
    def test_basic(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(db, unit_id="u1", related_industries=["半导体"])
        r = impl.ask_industry("半导体")
        assert r["ok"] is True
        assert r["dashboard"]["industry"] == "半导体"
        assert r["dashboard"]["recent_signals_total"] == 1

    def test_empty_industry_name(self, impl):
        r = impl.ask_industry("   ")
        assert r["ok"] is False
        assert "non-empty" in r["error"]

    @pytest.mark.parametrize("bad", [0, -1, "30"])
    def test_invalid_days(self, impl, bad):
        r = impl.ask_industry("半导体", days=bad)
        assert r["ok"] is False

    def test_custom_days(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(
            db, unit_id="u1",
            timestamp=_days_ago(45), created_at=_days_ago(45),
            related_industries=["半导体"],
        )
        # days=30 → 不计入
        assert impl.ask_industry("半导体", days=30)["dashboard"]["recent_signals_total"] == 0
        # days=60 → 计入
        assert impl.ask_industry("半导体", days=60)["dashboard"]["recent_signals_total"] == 1


class TestGetSystemStatus:
    def test_empty_db(self, impl):
        r = impl.get_system_status()
        assert r["ok"] is True
        assert r["database"]["info_units_total"] == 0
        assert r["database"]["watchlist_active"] == 0
        assert all(
            r["source_last_collected"][s] is None for s in PHASE1_SOURCES
        )
        assert r["today_cost"]["total_tokens"] == 0

    def test_with_data(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(db, unit_id="d1_u1", source="D1")
        _insert_info_unit(db, unit_id="d4_u1", source="D4")
        _insert_watchlist(db, "半导体", zone="active")
        _insert_llm_invocation(db, tokens=500)
        _insert_agent_error(db, error_type="network")

        r = impl.get_system_status()
        assert r["database"]["info_units_total"] == 2
        assert r["database"]["watchlist_active"] == 1
        assert r["database"]["llm_invocations_total"] == 1
        assert r["source_last_collected"]["D1"] is not None
        assert r["source_last_collected"]["V1"] is None
        assert r["today_cost"]["total_tokens"] == 500
        assert r["today_cost"]["invocation_count"] == 1
        assert r["active_errors_7d"] == 1
        assert r["errors_by_type_7d"]["network"] == 1

    def test_only_today_cost_counts(self, impl, tmp_db):
        db, _ = tmp_db
        # 今天的 + 昨天的
        _insert_llm_invocation(db, tokens=100, invoked_at=_days_ago(0))
        _insert_llm_invocation(db, tokens=200, invoked_at=_days_ago(2))
        r = impl.get_system_status()
        # today_cost 从当日 0 点起算 — 只计今天的一条
        assert r["today_cost"]["total_tokens"] == 100
        assert r["today_cost"]["invocation_count"] == 1


class TestSearchSignals:
    def test_basic_hit(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(
            db, unit_id="u1",
            content=json.dumps(
                {"title": "半导体行业政策", "summary": "推进发展"},
                ensure_ascii=False,
            ),
            related_industries=["半导体"],
        )
        r = impl.search_signals("半导体")
        assert r["ok"] is True
        assert r["total_matched"] == 1
        assert r["signals"][0]["id"] == "u1"

    def test_source_filter(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(db, unit_id="d1", source="D1", related_industries=["半导体"])
        _insert_info_unit(db, unit_id="d4", source="D4", related_industries=["半导体"])
        r = impl.search_signals("半导体", source="D1")
        assert r["total_matched"] == 1
        assert r["signals"][0]["source"] == "D1"

    def test_invalid_source(self, impl):
        r = impl.search_signals("半导体", source="XX")
        assert r["ok"] is False
        assert "source" in r["error"]

    def test_days_filter(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(
            db, unit_id="old",
            timestamp=_days_ago(45), created_at=_days_ago(45),
            related_industries=["半导体"],
        )
        _insert_info_unit(
            db, unit_id="new",
            related_industries=["半导体"],
        )
        r = impl.search_signals("半导体", days=30)
        assert r["total_matched"] == 1
        assert r["signals"][0]["id"] == "new"

    def test_limit_caps(self, impl, tmp_db):
        db, _ = tmp_db
        for i in range(30):
            _insert_info_unit(db, unit_id=f"u{i}", related_industries=["半导体"])
        r = impl.search_signals("半导体", limit=5)
        assert r["total_matched"] == 5

    def test_empty_query(self, impl):
        r = impl.search_signals("")
        assert r["ok"] is False

    @pytest.mark.parametrize("bad", [0, -1, SIGNAL_SEARCH_MAX_LIMIT + 1])
    def test_invalid_limit(self, impl, bad):
        r = impl.search_signals("q", limit=bad)
        assert r["ok"] is False

    def test_search_matches_category(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(
            db, unit_id="u1", category="半导体政策",
            related_industries=["其它"],
        )
        r = impl.search_signals("半导体")
        assert r["total_matched"] == 1


class TestGetLatestWeeklyReport:
    def test_empty_dir(self, impl):
        r = impl.get_latest_weekly_report("industry")
        assert r["ok"] is False
        assert "not found" in r["error"] or "no" in r["error"]

    def test_invalid_type(self, impl):
        r = impl.get_latest_weekly_report("invalid")
        assert r["ok"] is False

    def test_reads_latest(self, impl, tmp_reports):
        tmp_reports.mkdir(parents=True)
        (tmp_reports / "weekly_industry_20260410.md").write_text(
            "OLD_REPORT", encoding="utf-8"
        )
        (tmp_reports / "weekly_industry_20260417.md").write_text(
            "NEW_REPORT", encoding="utf-8"
        )
        # 其它 type 不应干扰
        (tmp_reports / "weekly_paper_20260418.md").write_text(
            "PAPER_REPORT", encoding="utf-8"
        )
        r = impl.get_latest_weekly_report("industry")
        assert r["ok"] is True
        assert r["filename"] == "weekly_industry_20260417.md"
        assert r["content"] == "NEW_REPORT"
        assert r["size_bytes"] > 0

    def test_paper_type(self, impl, tmp_reports):
        tmp_reports.mkdir(parents=True)
        (tmp_reports / "weekly_paper_20260418.md").write_text(
            "PAPER", encoding="utf-8"
        )
        r = impl.get_latest_weekly_report("paper")
        assert r["ok"] is True
        assert r["content"] == "PAPER"


class TestAddIndustry:
    def test_insert_new(self, impl, tmp_db):
        r = impl.add_industry("半导体", reason="Step 10 测试")
        assert r["ok"] is True
        assert r["action"] == "inserted"
        assert r["zone"] == "active"
        assert r["industry_id"] >= 1

    def test_idempotent_existing(self, impl, tmp_db):
        db, _ = tmp_db
        first = impl.add_industry("半导体")
        second = impl.add_industry("半导体")
        assert second["action"] == "already_exists"
        assert second["industry_id"] == first["industry_id"]

    def test_empty_name(self, impl):
        r = impl.add_industry("")
        assert r["ok"] is False

    def test_reason_saved(self, impl, tmp_db):
        db, _ = tmp_db
        impl.add_industry("半导体", reason="用户请求")
        row = db.query_one(
            "SELECT notes FROM watchlist WHERE industry_name='半导体'"
        )
        assert "用户请求" in (row["notes"] or "")


class TestRemoveIndustry:
    def test_marks_cold(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_watchlist(db, "半导体", zone="active")
        r = impl.remove_industry("半导体", reason="不再关注")
        assert r["ok"] is True
        assert r["action"] == "marked_cold"
        row = db.query_one(
            "SELECT zone, notes FROM watchlist WHERE industry_name='半导体'"
        )
        assert row["zone"] == "cold"
        assert "不再关注" in row["notes"]

    def test_not_found(self, impl):
        r = impl.remove_industry("不存在的行业", reason="test")
        assert r["ok"] is False
        assert "not found" in r["error"]

    def test_already_cold_idempotent(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_watchlist(db, "半导体", zone="cold")
        r = impl.remove_industry("半导体", reason="再试一次")
        assert r["ok"] is True
        assert r["action"] == "already_cold"

    def test_empty_reason_rejected(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_watchlist(db, "半导体", zone="active")
        r = impl.remove_industry("半导体", reason="")
        assert r["ok"] is False


class TestGetIndustryFullContext:
    def test_nonexistent_industry(self, impl):
        r = impl.get_industry_full_context("不存在")
        assert r["ok"] is True
        assert r["watchlist_entry"] is None
        assert r["recent_info_units_180d"] == []

    def test_with_watchlist_and_signals(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_watchlist(db, "半导体", zone="active", dimensions=4)
        for i in range(3):
            _insert_info_unit(
                db, unit_id=f"u{i}", related_industries=["半导体"]
            )

        r = impl.get_industry_full_context("半导体")
        assert r["ok"] is True
        assert r["watchlist_entry"]["industry_name"] == "半导体"
        assert len(r["recent_info_units_180d"]) == 3
        assert r["related_stocks_with_financials"] == []  # Phase 1 占位
        assert r["industry_chain"] == []
        assert "window_days" in r

    def test_empty_industry_name(self, impl):
        r = impl.get_industry_full_context("")
        assert r["ok"] is False

    def test_out_of_window_excluded(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(
            db, unit_id="old",
            timestamp=_days_ago(200), created_at=_days_ago(200),
            related_industries=["半导体"],
        )
        _insert_info_unit(db, unit_id="new", related_industries=["半导体"])
        r = impl.get_industry_full_context("半导体")
        ids = [x["id"] for x in r["recent_info_units_180d"]]
        assert "new" in ids
        assert "old" not in ids


class TestGetDecisionContext:
    def test_empty(self, impl):
        r = impl.get_decision_context("600519")
        assert r["ok"] is True
        assert r["related_stocks_entries"] == []

    def test_basic(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_related_stock(db, "600519", stock_name="贵州茅台", industry="白酒")
        r = impl.get_decision_context("600519")
        assert r["ok"] is True
        assert len(r["related_stocks_entries"]) == 1
        assert r["related_stocks_entries"][0]["stock_name"] == "贵州茅台"

    def test_empty_stock(self, impl):
        r = impl.get_decision_context("")
        assert r["ok"] is False


class TestGetPolicyForMotivationAnalysis:
    def test_nonexistent(self, impl):
        r = impl.get_policy_for_motivation_analysis("nonexistent_id")
        assert r["ok"] is False
        assert "not found" in r["error"]

    def test_empty_id(self, impl):
        r = impl.get_policy_for_motivation_analysis("")
        assert r["ok"] is False

    def test_returns_policy_and_similar(self, impl, tmp_db):
        db, _ = tmp_db
        # 目标政策 — 标题短（5 字），seed 覆盖整个标题
        _insert_info_unit(
            db, unit_id="target",
            source="D1", category="政策发布",
            content=json.dumps(
                {"title": "半导体政策", "summary": "推进"},
                ensure_ascii=False,
            ),
            timestamp=_days_ago(10), created_at=_days_ago(10),
            related_industries=["半导体"],
        )
        # 类似政策（同 source / category / 标题包含 "半导体政策" 5 字 seed）
        _insert_info_unit(
            db, unit_id="similar1",
            source="D1", category="政策发布",
            content=json.dumps(
                {"title": "半导体政策 A 修订版", "summary": "..."},
                ensure_ascii=False,
            ),
            timestamp=_days_ago(30), created_at=_days_ago(30),
            related_industries=["半导体"],
        )
        # 非同类：不同 source
        _insert_info_unit(
            db, unit_id="unrelated",
            source="V3", category="政策发布",
            content=json.dumps({"title": "半导体政策 C"}, ensure_ascii=False),
            timestamp=_days_ago(5), created_at=_days_ago(5),
            related_industries=["半导体"],
        )

        r = impl.get_policy_for_motivation_analysis("target")
        assert r["ok"] is True
        assert r["info_unit"]["id"] == "target"
        similar_ids = [s["id"] for s in r["similar_policies"]]
        assert "similar1" in similar_ids
        assert "unrelated" not in similar_ids  # 不同 source 排除
        assert r["title_seed_used"]  # 非空


# ═══════════════════ access log ═══════════════════


class TestAccessLog:
    def test_log_file_created(self, impl, tmp_logs):
        impl.get_watchlist()
        log_path = tmp_logs / "mcp_access.log"
        assert log_path.exists()

    def test_log_contents_json_line(self, impl, tmp_logs):
        impl.get_watchlist()
        lines = (tmp_logs / "mcp_access.log").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        last = lines[-1]
        # 行尾是 JSON（日期前缀后）
        json_start = last.index("{")
        payload = json.loads(last[json_start:])
        assert payload["tool"] == "get_watchlist"
        assert payload["ok"] is True
        assert "ms" in payload

    def test_logs_error_path(self, impl, tmp_logs):
        impl.ask_industry("")  # 空名
        lines = (tmp_logs / "mcp_access.log").read_text(encoding="utf-8").strip().splitlines()
        payload = json.loads(lines[-1][lines[-1].index("{"):])
        assert payload["ok"] is False
        assert "error" in payload


class TestAccessLogExceptionSafety:
    def test_db_error_does_not_crash_returns_error_dict(self, tmp_db, tmp_reports, tmp_logs):
        """如果 DB 在工具执行中途抛异常，工具应返回 {ok:False, error} 不 raise。"""
        db, _ = tmp_db
        logger = build_access_logger(tmp_logs)
        impl = ScoutToolImpl(db=db, reports_dir=tmp_reports, access_logger=logger)
        db.close()  # 故意关闭

        r = impl.get_watchlist()
        assert r["ok"] is False
        assert "error" in r


# ═══════════════════ Layer B: FastMCP call_tool ═══════════════════


@pytest.fixture
def fastmcp_app(impl):
    return build_server(impl)


class TestFastMCPRegistration:
    def test_lists_exactly_10_tools(self, fastmcp_app):
        tools = asyncio.run(fastmcp_app.list_tools())
        assert len(tools) == 10

    def test_all_expected_tools_present(self, fastmcp_app):
        tools = asyncio.run(fastmcp_app.list_tools())
        names = {t.name for t in tools}
        expected = {
            "get_watchlist", "ask_industry", "get_system_status",
            "search_signals", "get_latest_weekly_report",
            "add_industry", "remove_industry",
            "get_industry_full_context", "get_decision_context",
            "get_policy_for_motivation_analysis",
        }
        assert names == expected

    def test_each_tool_has_description(self, fastmcp_app):
        tools = asyncio.run(fastmcp_app.list_tools())
        for t in tools:
            assert t.description and len(t.description) > 20

    def test_server_name_is_scout(self, fastmcp_app):
        assert fastmcp_app.name == SERVER_NAME


class TestFastMCPCallTool:
    def _call(self, app, name: str, args: dict):
        """同步调 app.call_tool，统一为 (blocks, {"result": parsed_value}).

        FastMCP 返回形态：
            - tool 返 str → (blocks, result_dict) tuple
            - tool 返 dict → 单独 blocks list，dict JSON 化塞在 blocks[0].text
        两种都归一到 (blocks, {"result": <value>})。
        """
        raw = asyncio.run(app.call_tool(name, args))
        if isinstance(raw, tuple) and len(raw) == 2:
            return raw  # (blocks, result_dict)
        # list[ContentBlock] → 解析第一个 text block
        blocks = list(raw) if not isinstance(raw, list) else raw
        for block in blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    return blocks, {"result": parsed}
                except json.JSONDecodeError:
                    pass
        return blocks, {"result": None}

    def test_get_watchlist(self, fastmcp_app):
        blocks, result = self._call(fastmcp_app, "get_watchlist", {})
        assert result["result"]["ok"] is True

    def test_ask_industry_with_args(self, fastmcp_app, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(db, unit_id="u1", related_industries=["半导体"])
        blocks, result = self._call(
            fastmcp_app, "ask_industry", {"industry": "半导体", "days": 7}
        )
        assert result["result"]["ok"] is True

    def test_ask_industry_error_propagates_as_result(self, fastmcp_app):
        # 空 industry — 不 raise，返回 ok:False
        blocks, result = self._call(
            fastmcp_app, "ask_industry", {"industry": ""}
        )
        assert result["result"]["ok"] is False

    def test_add_then_remove(self, fastmcp_app, tmp_db):
        db, _ = tmp_db
        _, r1 = self._call(
            fastmcp_app, "add_industry",
            {"industry": "新能源", "reason": "测试"},
        )
        assert r1["result"]["action"] == "inserted"

        _, r2 = self._call(
            fastmcp_app, "remove_industry",
            {"industry": "新能源", "reason": "测试完"},
        )
        assert r2["result"]["action"] == "marked_cold"

    def test_search_signals_roundtrip(self, fastmcp_app, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(
            db, unit_id="u1",
            content=json.dumps({"title": "半导体政策"}, ensure_ascii=False),
            related_industries=["半导体"],
        )
        _, r = self._call(
            fastmcp_app, "search_signals",
            {"query": "半导体", "limit": 5},
        )
        assert r["result"]["total_matched"] == 1


# ═══════════════════ 并发 ═══════════════════


class TestConcurrency:
    def test_serial_50_calls_do_not_crash(self, impl, tmp_db):
        """Phase 1 级别：50 次顺序调用 + 1 个连接，应全部成功。"""
        db, _ = tmp_db
        for i in range(20):
            _insert_info_unit(db, unit_id=f"u{i}", related_industries=["半导体"])

        for _ in range(50):
            r1 = impl.get_watchlist()
            r2 = impl.ask_industry("半导体")
            r3 = impl.get_system_status()
            assert r1["ok"] and r2["ok"] and r3["ok"]


# ═══════════════════ resolve_db_path ═══════════════════


class TestResolveDBPath:
    def test_override_wins(self):
        path = resolve_db_path(override="/custom/path.db", env={})
        assert path == "/custom/path.db"

    def test_env_second_priority(self, monkeypatch):
        path = resolve_db_path(
            override=None, env={"SCOUT_DB_PATH": "/env/path.db"}
        )
        assert path == "/env/path.db"

    def test_falls_back_to_config(self, tmp_path):
        """没有 override 也没 env，走 config.yaml"""
        path = resolve_db_path(env={})
        # 默认指向 data/knowledge.db
        assert path.endswith("knowledge.db")


# ═══════════════════ data 完整性 ═══════════════════


class TestDataTypes:
    def test_all_tool_returns_are_dicts(self, impl, tmp_db):
        db, _ = tmp_db
        _insert_info_unit(db, unit_id="u1", related_industries=["半导体"])
        _insert_watchlist(db, "半导体", zone="active")
        _insert_related_stock(db, "600519")

        calls = [
            impl.get_watchlist(),
            impl.ask_industry("半导体"),
            impl.get_system_status(),
            impl.search_signals("半导体"),
            impl.get_latest_weekly_report("industry"),
            impl.add_industry("光伏"),
            impl.remove_industry("光伏", reason="test"),
            impl.get_industry_full_context("半导体"),
            impl.get_decision_context("600519"),
            impl.get_policy_for_motivation_analysis("u1"),
        ]
        for result in calls:
            assert isinstance(result, dict)
            assert "ok" in result

    def test_json_serializable_returns(self, impl, tmp_db):
        """MCP 要求所有返回值 JSON 可序列化。抽几个测。"""
        db, _ = tmp_db
        _insert_info_unit(db, unit_id="u1", related_industries=["半导体"])
        for r in (
            impl.get_watchlist(),
            impl.ask_industry("半导体"),
            impl.get_system_status(),
            impl.search_signals("半导体"),
        ):
            json.dumps(r, ensure_ascii=False)  # 不应抛
