"""
tests/test_main.py — main.py CLI + ScoutRunner 测试矩阵

覆盖：
    CLI parse / paths：
        - 子命令注册：serve/mcp/collect/process/report/status
        - --db / --queue-db / SCOUT_DB_PATH env 三级优先
        - status / collect / report 参数校验
    Bootstrap：
        - bootstrap_paths 创建目录 + 初始化缺失的 db 文件
        - check_ollama 不可达返 False（不 raise）
    Logging：
        - configure_logging 创建 logs/scout.log + 日志级别生效
        - 多次 configure_logging 不重复挂 handler
    Status 子命令：
        - 空 db 能跑（GREEN），有 error 降 YELLOW
    Collect 子命令：
        - mock 一个 Collector（Monkey-patch make_collector），跑通 collect + persist
    Health 计算：
        - GREEN / YELLOW / RED 三种场景
    ScoutRunner：
        - _handle_failure 累计 → 到阈值推 alert（Mock push_queue）
        - _clear_failure 清计数
        - schedule_all 注册 12 个 job
        - run(max_runtime_seconds=0.3) 快速启停
        - 信号处理器模拟（手动 set shutdown_event → run 应返回）
    Print utils：
        - _format_size 对 1KB / 1MB / missing 正确
        - _compute_health 各路径
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as scout_main
from main import (
    AGENT_FAILURE_THRESHOLD,
    DEFAULT_D1_KEYWORDS,
    SOURCES,
    ScoutRunner,
    _compute_health,
    _format_size,
    bootstrap_paths,
    build_arg_parser,
    check_ollama,
    cmd_collect,
    cmd_status,
    configure_logging,
    resolve_paths,
)


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_data(tmp_path):
    return {
        "kdb": tmp_path / "data" / "knowledge.db",
        "qdb": tmp_path / "data" / "queue.db",
        "logs": tmp_path / "logs",
        "reports": tmp_path / "reports",
    }


@pytest.fixture
def tmp_logger(tmp_data):
    return configure_logging(tmp_data["logs"], log_level="DEBUG", console=False)


@pytest.fixture
def bootstrapped(tmp_data, tmp_logger):
    """已完成 bootstrap（db 存在、dirs 存在）的 tmp 环境。"""
    bootstrap_paths(
        knowledge_db_path=tmp_data["kdb"],
        queue_db_path=tmp_data["qdb"],
        logs_dir=tmp_data["logs"],
        reports_dir=tmp_data["reports"],
        logger=tmp_logger,
    )
    return tmp_data


# ═══════════════════ CLI 解析 ═══════════════════


class TestCLIParse:
    def test_help_exits_zero(self):
        parser = build_arg_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--help"])
        assert exc.value.code == 0

    def test_missing_command_fails(self):
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    @pytest.mark.parametrize(
        "cmd",
        ["serve", "mcp", "status"],
    )
    def test_simple_subcommands_parse(self, cmd):
        parser = build_arg_parser()
        args = parser.parse_args([cmd])
        assert args.command == cmd

    def test_collect_requires_source(self):
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["collect"])

    @pytest.mark.parametrize("source", list(SOURCES))
    def test_collect_accepts_all_sources(self, source):
        parser = build_arg_parser()
        args = parser.parse_args(["collect", "--source", source])
        assert args.source == source
        assert args.days == 7  # default

    def test_collect_rejects_unknown_source(self):
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["collect", "--source", "D99"])

    def test_process_optional_max(self):
        parser = build_arg_parser()
        args = parser.parse_args(["process"])
        assert args.max_messages is None
        args2 = parser.parse_args(["process", "--max", "5"])
        assert args2.max_messages == 5

    @pytest.mark.parametrize("rtype", ["industry", "paper"])
    def test_report_accepts_both_types(self, rtype):
        parser = build_arg_parser()
        args = parser.parse_args(["report", "--type", rtype])
        assert args.report_type == rtype
        assert args.no_gemma is False

    def test_report_no_gemma_flag(self):
        parser = build_arg_parser()
        args = parser.parse_args(["report", "--type", "industry", "--no-gemma"])
        assert args.no_gemma is True

    def test_serve_optional_max_runtime(self):
        parser = build_arg_parser()
        args = parser.parse_args(["serve"])
        assert args.max_runtime_seconds is None
        args2 = parser.parse_args(["serve", "--max-runtime-seconds", "30"])
        assert args2.max_runtime_seconds == 30.0


# ═══════════════════ resolve_paths ═══════════════════


class TestResolvePaths:
    def test_defaults(self):
        ns = argparse.Namespace(db=None, queue_db=None)
        p = resolve_paths(ns, env={})
        assert p["kdb_path"].name == "knowledge.db"
        assert p["qdb_path"].name == "queue.db"

    def test_cli_override(self):
        ns = argparse.Namespace(db="/x/k.db", queue_db="/x/q.db")
        p = resolve_paths(ns, env={})
        assert str(p["kdb_path"]).endswith("k.db")
        assert str(p["qdb_path"]).endswith("q.db")

    def test_env_override(self):
        ns = argparse.Namespace(db=None, queue_db=None)
        p = resolve_paths(ns, env={
            "SCOUT_DB_PATH": "/env/k.db",
            "SCOUT_QUEUE_DB_PATH": "/env/q.db",
        })
        assert str(p["kdb_path"]).endswith("k.db")
        assert str(p["qdb_path"]).endswith("q.db")

    def test_cli_wins_over_env(self):
        ns = argparse.Namespace(db="/cli/k.db", queue_db=None)
        p = resolve_paths(ns, env={"SCOUT_DB_PATH": "/env/k.db"})
        assert str(p["kdb_path"]).endswith("cli/k.db") or "cli" in str(p["kdb_path"])


# ═══════════════════ Bootstrap ═══════════════════


class TestBootstrap:
    def test_creates_missing_dirs(self, tmp_data, tmp_logger):
        bootstrap_paths(
            knowledge_db_path=tmp_data["kdb"],
            queue_db_path=tmp_data["qdb"],
            logs_dir=tmp_data["logs"],
            reports_dir=tmp_data["reports"],
            logger=tmp_logger,
        )
        assert tmp_data["logs"].exists()
        assert tmp_data["reports"].exists()
        assert tmp_data["kdb"].parent.exists()

    def test_initializes_missing_knowledge_db(self, tmp_data, tmp_logger):
        bootstrap_paths(
            knowledge_db_path=tmp_data["kdb"],
            queue_db_path=tmp_data["qdb"],
            logs_dir=tmp_data["logs"],
            reports_dir=tmp_data["reports"],
            logger=tmp_logger,
        )
        assert tmp_data["kdb"].exists()
        # 验证表存在
        import sqlite3
        conn = sqlite3.connect(tmp_data["kdb"])
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='info_units'"
            ).fetchone()
        finally:
            conn.close()
        assert rows is not None

    def test_initializes_missing_queue_db(self, tmp_data, tmp_logger):
        bootstrap_paths(
            knowledge_db_path=tmp_data["kdb"],
            queue_db_path=tmp_data["qdb"],
            logs_dir=tmp_data["logs"],
            reports_dir=tmp_data["reports"],
            logger=tmp_logger,
        )
        assert tmp_data["qdb"].exists()

    def test_existing_db_not_overwritten(self, tmp_data, tmp_logger):
        bootstrap_paths(
            knowledge_db_path=tmp_data["kdb"],
            queue_db_path=tmp_data["qdb"],
            logs_dir=tmp_data["logs"],
            reports_dir=tmp_data["reports"],
            logger=tmp_logger,
        )
        # 写一条测试数据
        import sqlite3
        conn = sqlite3.connect(tmp_data["kdb"])
        try:
            conn.execute(
                """INSERT INTO info_units
                   (id, source, timestamp, created_at, updated_at)
                   VALUES ('test1', 'D1', '2026-04-18T00:00+00:00',
                           '2026-04-18T00:00+00:00', '2026-04-18T00:00+00:00')"""
            )
            conn.commit()
        finally:
            conn.close()
        # 再 bootstrap：不应清空
        bootstrap_paths(
            knowledge_db_path=tmp_data["kdb"],
            queue_db_path=tmp_data["qdb"],
            logs_dir=tmp_data["logs"],
            reports_dir=tmp_data["reports"],
            logger=tmp_logger,
        )
        conn = sqlite3.connect(tmp_data["kdb"])
        try:
            row = conn.execute(
                "SELECT id FROM info_units WHERE id='test1'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None  # 数据保留


# ═══════════════════ Ollama check ═══════════════════


class TestOllamaCheck:
    def test_unreachable_returns_false(self, tmp_logger):
        ok = check_ollama("http://127.0.0.1:9", tmp_logger)  # 闭合端口
        assert ok is False

    def test_bad_url_returns_false(self, tmp_logger):
        ok = check_ollama("not-a-url", tmp_logger)
        assert ok is False


# ═══════════════════ 日志配置 ═══════════════════


class TestLogging:
    def test_log_file_created(self, tmp_data):
        configure_logging(tmp_data["logs"], log_level="DEBUG", console=False)
        assert tmp_data["logs"].exists()
        # logger 写一条；文件应该出现
        logger = logging.getLogger("scout")
        logger.info("test msg")
        log_files = list(tmp_data["logs"].iterdir())
        assert len(log_files) >= 1
        assert any(f.name.startswith("scout.log") for f in log_files)

    def test_log_level_applied(self, tmp_data):
        logger = configure_logging(
            tmp_data["logs"], log_level="WARNING", console=False
        )
        assert logger.level == logging.WARNING

    def test_multiple_calls_no_duplicate_handlers(self, tmp_data):
        configure_logging(tmp_data["logs"], console=False)
        configure_logging(tmp_data["logs"], console=False)
        configure_logging(tmp_data["logs"], console=False)
        logger = logging.getLogger("scout")
        # 恰好一个 FileHandler（console=False）
        handlers = logger.handlers
        assert len(handlers) == 1


# ═══════════════════ Health 计算 ═══════════════════


class TestComputeHealth:
    def test_green(self):
        status = {
            "active_errors_7d": 0,
            "source_last_collected": {"D1": "2026-04-18T..."},
        }
        h = _compute_health(status, {"push_outbox": {"pending": 0, "processing": 0, "done": 0, "failed": 0}})
        assert h["level"] == "GREEN"
        assert h["badge"] == "🟢"

    def test_yellow_some_errors(self):
        status = {
            "active_errors_7d": 3,
            "source_last_collected": {"D1": "..."},
        }
        h = _compute_health(status, {"push_outbox": {"pending": 0, "processing": 0, "done": 0, "failed": 0}})
        assert h["level"] == "YELLOW"

    def test_red_many_errors(self):
        status = {
            "active_errors_7d": 25,
            "source_last_collected": {"D1": "..."},
        }
        h = _compute_health(status, {"push_outbox": {"pending": 0, "processing": 0, "done": 0, "failed": 0}})
        assert h["level"] == "RED"

    def test_red_no_sources_collected(self):
        status = {
            "active_errors_7d": 0,
            "source_last_collected": {s: None for s in SOURCES},
        }
        h = _compute_health(status, {})
        assert h["level"] == "RED"
        assert "any_source_collected=False" in h["reason"]

    def test_red_many_push_failed(self):
        status = {
            "active_errors_7d": 0,
            "source_last_collected": {"D1": "x"},
        }
        h = _compute_health(status, {"push_outbox": {"pending": 0, "processing": 0, "done": 0, "failed": 10}})
        assert h["level"] == "RED"


# ═══════════════════ _format_size ═══════════════════


class TestFormatSize:
    def test_missing(self, tmp_path):
        assert _format_size(tmp_path / "nonexistent") == "(missing)"

    def test_small_bytes(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("abc")
        assert "3 B" in _format_size(f)

    def test_kilo(self, tmp_path):
        f = tmp_path / "k.txt"
        f.write_bytes(b"a" * 2048)
        out = _format_size(f)
        assert "KB" in out

    def test_mega(self, tmp_path):
        f = tmp_path / "m.txt"
        f.write_bytes(b"a" * (2 * 1024 * 1024))
        out = _format_size(f)
        assert "MB" in out


# ═══════════════════ status 子命令 ═══════════════════


class TestStatusCmd:
    def test_status_on_empty_db(self, bootstrapped, tmp_logger, capsys):
        rc = cmd_status(
            kdb_path=bootstrapped["kdb"],
            qdb_path=bootstrapped["qdb"],
            reports_dir=bootstrapped["reports"],
            logger=tmp_logger,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Scout 系统状态" in out
        assert "GREEN" in out or "YELLOW" in out or "RED" in out
        assert "info_units total          : 0" in out

    def test_status_with_error_shows_yellow(self, bootstrapped, tmp_logger, capsys):
        # 先写一个 agent_error（最近 7 天内）
        import sqlite3
        conn = sqlite3.connect(bootstrapped["kdb"])
        try:
            conn.execute(
                """INSERT INTO agent_errors
                   (agent_name, error_type, error_message, occurred_at)
                   VALUES (?, ?, ?, ?)""",
                ("test_agent", "network", "test", datetime.now(tz=timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        # 同时至少一条 info_units（防止 RED 分支）
        conn = sqlite3.connect(bootstrapped["kdb"])
        try:
            conn.execute(
                """INSERT INTO info_units
                   (id, source, timestamp, created_at, updated_at)
                   VALUES ('x', 'D1', ?, ?, ?)""",
                (datetime.now(tz=timezone.utc).isoformat(),) * 3,
            )
            conn.commit()
        finally:
            conn.close()

        cmd_status(
            kdb_path=bootstrapped["kdb"],
            qdb_path=bootstrapped["qdb"],
            reports_dir=bootstrapped["reports"],
            logger=tmp_logger,
        )
        out = capsys.readouterr().out
        assert "YELLOW" in out
        assert "network: 1" in out


# ═══════════════════ collect 子命令（mock collector）═══════════════════


class TestCollectCmd:
    def test_unknown_source_returns_2(self, bootstrapped, tmp_logger):
        rc = cmd_collect(
            source="XX", days=7,
            kdb_path=bootstrapped["kdb"], logger=tmp_logger,
        )
        assert rc == 2

    def test_mock_collector_success(
        self, bootstrapped, tmp_logger, monkeypatch, capsys
    ):
        """替换 make_collector 返回一个伪 collector。"""
        class FakeCollector:
            def collect_recent(self, days):
                return ["unit1", "unit2"]

            def persist_batch(self, units):
                return len(units)

        monkeypatch.setattr(
            ScoutRunner, "make_collector",
            lambda self, src: FakeCollector(),
        )
        rc = cmd_collect(
            source="D1", days=7,
            kdb_path=bootstrapped["kdb"], logger=tmp_logger,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "D1: +2 new rows" in out

    def test_collector_crash_returns_1(
        self, bootstrapped, tmp_logger, monkeypatch
    ):
        class CrashingCollector:
            def collect_recent(self, days):
                raise RuntimeError("boom")

            def persist_batch(self, units):
                return 0

        monkeypatch.setattr(
            ScoutRunner, "make_collector",
            lambda self, src: CrashingCollector(),
        )
        rc = cmd_collect(
            source="D1", days=7,
            kdb_path=bootstrapped["kdb"], logger=tmp_logger,
        )
        assert rc == 1


# ═══════════════════ ScoutRunner 内部 ═══════════════════


class _MockPushQueue:
    """记录所有 push_alert 调用的 mock。"""

    def __init__(self):
        self.alerts: List[Dict[str, Any]] = []

    def push_alert(self, alert_type, content, **kw):
        self.alerts.append({
            "alert_type": alert_type,
            "content": content,
            "kwargs": kw,
        })
        return "fake-event-id"

    # For heartbeat
    def queue_stats(self, qname=None):
        return {}


class TestScoutRunnerFailureCounter:
    @pytest.fixture
    def runner(self, bootstrapped, tmp_logger):
        from infra.db_manager import DatabaseManager
        from infra.queue_manager import QueueManager
        kdb = DatabaseManager(bootstrapped["kdb"])
        qm = QueueManager(bootstrapped["qdb"])
        push = _MockPushQueue()
        r = ScoutRunner(
            kdb=kdb, qdb=qm, push_queue=push,
            logger=tmp_logger, reports_dir=bootstrapped["reports"],
        )
        yield r, push
        kdb.close()
        qm.close()

    def test_failure_count_accumulates(self, runner):
        r, push = runner
        r._handle_failure("collect_D1", RuntimeError("fail 1"))
        r._handle_failure("collect_D1", RuntimeError("fail 2"))
        assert r.failure_counters["collect_D1"] == 2
        assert len(push.alerts) == 0  # 还没到阈值 3

    def test_failure_reaches_threshold_pushes_alert(self, runner):
        r, push = runner
        for i in range(AGENT_FAILURE_THRESHOLD):
            r._handle_failure("collect_D1", RuntimeError(f"fail {i}"))
        assert len(push.alerts) == 1
        assert push.alerts[0]["alert_type"] == "data_source_down"
        assert push.alerts[0]["content"]["agent"] == "collect_D1"
        assert push.alerts[0]["content"]["failure_count"] == 3

    def test_alert_only_once_per_agent(self, runner):
        r, push = runner
        for i in range(AGENT_FAILURE_THRESHOLD + 5):
            r._handle_failure("collect_D1", RuntimeError(f"fail {i}"))
        assert len(push.alerts) == 1  # 不重复报警

    def test_non_collect_failure_uses_failure_level_change(self, runner):
        r, push = runner
        for i in range(AGENT_FAILURE_THRESHOLD):
            r._handle_failure("weekly_industry_report", RuntimeError(f"fail {i}"))
        assert push.alerts[0]["alert_type"] == "failure_level_change"

    def test_clear_failure_resets_counter(self, runner):
        r, push = runner
        r._handle_failure("collect_D1", RuntimeError("fail"))
        assert r.failure_counters.get("collect_D1") == 1
        r._clear_failure("collect_D1")
        assert "collect_D1" not in r.failure_counters

    def test_clear_allows_future_alerts(self, runner):
        r, push = runner
        # 触发第一次报警
        for i in range(AGENT_FAILURE_THRESHOLD):
            r._handle_failure("collect_D1", RuntimeError(f"fail {i}"))
        assert len(push.alerts) == 1

        # 恢复 → clear
        r._clear_failure("collect_D1")

        # 再次连续失败 → 又一次告警
        for i in range(AGENT_FAILURE_THRESHOLD):
            r._handle_failure("collect_D1", RuntimeError(f"fail2 {i}"))
        assert len(push.alerts) == 2

    def test_push_queue_none_no_crash(self, bootstrapped, tmp_logger):
        from infra.db_manager import DatabaseManager
        from infra.queue_manager import QueueManager
        kdb = DatabaseManager(bootstrapped["kdb"])
        qm = QueueManager(bootstrapped["qdb"])
        try:
            r = ScoutRunner(
                kdb=kdb, qdb=qm, push_queue=None,
                logger=tmp_logger, reports_dir=bootstrapped["reports"],
            )
            for i in range(AGENT_FAILURE_THRESHOLD + 2):
                r._handle_failure("collect_D1", RuntimeError("x"))
            # 不抛，只计数
            assert r.failure_counters["collect_D1"] == AGENT_FAILURE_THRESHOLD + 2
        finally:
            kdb.close()
            qm.close()


class TestScoutRunnerSchedule:
    def test_schedule_all_registers_jobs(self, bootstrapped, tmp_logger):
        """验证 15 个 job 全部被注册（v1.12 起 += push_consumer_scan/push_consumer_digest）"""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from infra.db_manager import DatabaseManager
        from infra.queue_manager import QueueManager

        kdb = DatabaseManager(bootstrapped["kdb"])
        qm = QueueManager(bootstrapped["qdb"])
        try:
            r = ScoutRunner(
                kdb=kdb, qdb=qm, push_queue=_MockPushQueue(),
                logger=tmp_logger, reports_dir=bootstrapped["reports"],
            )
            r.scheduler = AsyncIOScheduler()
            r.schedule_all()
            job_ids = {j.id for j in r.scheduler.get_jobs()}
            expected = {
                "collect_D1", "collect_D4", "collect_V1",
                "collect_V3", "collect_S4",
                "weekly_industry_report", "weekly_paper_report",
                "daily_briefing", "financial_refresh",
                "recommend_batch", "motivation_drift",
                "backfill_direction", "heartbeat",
                # v1.12 推送 Agent
                "push_consumer_scan", "push_consumer_digest",
                # v1.13 外发 + 健康监控
                "push_consumer_deliver",
                "health_check_errors", "health_heartbeat",
            }
            assert job_ids == expected
        finally:
            kdb.close()
            qm.close()


class TestScoutRunnerRun:
    def test_max_runtime_seconds_triggers_shutdown(
        self, bootstrapped, tmp_logger
    ):
        """run(max_runtime_seconds=0.5) 应在 0.5s 后自动退出返 0"""
        from infra.db_manager import DatabaseManager
        from infra.queue_manager import QueueManager

        kdb = DatabaseManager(bootstrapped["kdb"])
        qm = QueueManager(bootstrapped["qdb"])
        try:
            r = ScoutRunner(
                kdb=kdb, qdb=qm, push_queue=_MockPushQueue(),
                logger=tmp_logger, reports_dir=bootstrapped["reports"],
                gemma_available=False,  # 避免真连 Ollama
            )
            rc = asyncio.run(r.run(
                max_runtime_seconds=0.5,
                install_signal_handlers=False,  # 测试不动真 signal
            ))
            assert rc == 0
        finally:
            kdb.close()
            qm.close()

    def test_early_shutdown_event_set(self, bootstrapped, tmp_logger):
        """外部 set shutdown_event → run 立即返回"""
        from infra.db_manager import DatabaseManager
        from infra.queue_manager import QueueManager

        kdb = DatabaseManager(bootstrapped["kdb"])
        qm = QueueManager(bootstrapped["qdb"])
        try:
            r = ScoutRunner(
                kdb=kdb, qdb=qm, push_queue=_MockPushQueue(),
                logger=tmp_logger, reports_dir=bootstrapped["reports"],
                gemma_available=False,
            )

            async def _go():
                task = asyncio.create_task(r.run(install_signal_handlers=False))
                # 等调度器启好
                await asyncio.sleep(0.2)
                r.shutdown_event.set()
                return await asyncio.wait_for(task, timeout=5)

            rc = asyncio.run(_go())
            assert rc == 0
        finally:
            kdb.close()
            qm.close()


# ═══════════════════ main() 入口级（end-to-end CLI）═══════════════════


class TestMainEntry:
    def test_main_help_returns_zero(self):
        # argparse --help exits with 0
        with pytest.raises(SystemExit) as exc:
            scout_main.main(["--help"])
        assert exc.value.code == 0

    def test_main_status_runs(self, tmp_path, monkeypatch):
        # 用 tmp 目录隔离
        monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "k.db"))
        monkeypatch.setenv("SCOUT_QUEUE_DB_PATH", str(tmp_path / "q.db"))
        # status 需要 config.yaml —— 用项目 default
        rc = scout_main.main(["status"])
        assert rc == 0

    def test_main_serve_max_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "k.db"))
        monkeypatch.setenv("SCOUT_QUEUE_DB_PATH", str(tmp_path / "q.db"))
        # 0.3s 后自动关
        rc = scout_main.main(["serve", "--max-runtime-seconds", "0.3"])
        assert rc == 0

    def test_main_bad_config_returns_2(self, tmp_path):
        # 指向不存在的 config
        fake_config = tmp_path / "missing.yaml"
        rc = scout_main.main([
            "--config", str(fake_config),
            "status",
        ])
        assert rc == 2
