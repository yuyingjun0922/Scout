#!/usr/bin/env python
"""
main.py — Scout Phase 1 统一启动入口（Step 12）

串联前 11 步所有组件：
    采集 (Step 5) → 队列 (Step 6) → 信号 Agent (Step 7) → dashboard (Step 8)
    → 方向判断 (Step 9) → MCP (Step 10) → 推送 (Step 11)

CLI：
    python main.py serve                     # 调度器 + 信号消费循环（长跑）
    python main.py mcp                        # MCP stdio server
    python main.py collect --source D1        # 手动采集
    python main.py process                    # 手动处理 collection_to_knowledge 队列
    python main.py report --type industry     # 手动生成周报
    python main.py status                     # 系统状态快照
    python main.py --help

serve 调度表：
    D1 每 6h | D4 每 24h | V1 每日午夜 KST | V3 每 24h（Phase 1 无数据）| S4 每 12h
    周报 industry：周一 07:00 KST
    周报 paper   ：周日 09:00 KST
    每日简报     ：每天 07:30 KST
    心跳         ：每小时
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


# ══════════════════════ 常量 ══════════════════════

DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_LOGS_DIR = PROJECT_ROOT / "logs"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

LOG_FILE_NAME = "scout.log"
HEARTBEAT_INTERVAL_SECONDS = 3600
AGENT_FAILURE_THRESHOLD = 3
GRACEFUL_SHUTDOWN_TIMEOUT_SEC = 60
SIGNAL_CONSUMER_POLL_INTERVAL_SEC = 5
OLLAMA_PING_TIMEOUT_SEC = 3

KST = ZoneInfo("Asia/Seoul")

SOURCES = ("D1", "D4", "V1", "V3", "S4")

# Phase 1 D1 关键词（承接 gov_cn adapter 的默认）
DEFAULT_D1_KEYWORDS = [
    "半导体", "新能源", "芯片", "人工智能", "光伏", "电池",
    "机器人", "稀土", "5G", "生物医药", "军工",
]


# ══════════════════════ 日志 ══════════════════════


def configure_logging(
    logs_dir: Path,
    log_level: str = "INFO",
    console: bool = True,
) -> logging.Logger:
    """配置 scout root logger；按天滚动到 logs/scout.log。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("scout")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # 清掉已有 handler（多次调用安全）
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_h = TimedRotatingFileHandler(
        logs_dir / LOG_FILE_NAME,
        when="midnight",
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    file_h.setFormatter(fmt)
    logger.addHandler(file_h)

    if console:
        stream_h = logging.StreamHandler(sys.stderr)
        stream_h.setFormatter(fmt)
        logger.addHandler(stream_h)

    logger.propagate = False
    return logger


# ══════════════════════ 环境引导 ══════════════════════


def bootstrap_paths(
    knowledge_db_path: Path,
    queue_db_path: Path,
    logs_dir: Path,
    reports_dir: Path,
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
    """确保 data/ logs/ reports/ 目录存在；knowledge.db / queue.db 缺失则 init。"""
    for d in (knowledge_db_path.parent, queue_db_path.parent, logs_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    if not knowledge_db_path.exists():
        from knowledge.init_db import init_database
        init_database(knowledge_db_path)
        if logger:
            logger.info(f"[bootstrap] created knowledge.db: {knowledge_db_path}")

    if not queue_db_path.exists():
        from knowledge.init_queue_db import init_queue_db
        init_queue_db(queue_db_path)
        if logger:
            logger.info(f"[bootstrap] created queue.db: {queue_db_path}")


def check_ollama(ollama_host: str, logger: logging.Logger) -> bool:
    """Ollama 可达性检查。不可达返 False + 打 warning，不 raise。"""
    try:
        import httpx
        r = httpx.get(
            f"{ollama_host.rstrip('/')}/api/tags", timeout=OLLAMA_PING_TIMEOUT_SEC
        )
        if r.status_code == 200:
            return True
    except Exception as e:
        logger.warning(
            f"[bootstrap] Ollama check failed ({type(e).__name__}): {e}"
        )
        return False
    logger.warning(f"[bootstrap] Ollama at {ollama_host} returned non-200")
    return False


# ══════════════════════ ScoutRunner（serve 模式核心）══════════════════════


class ScoutRunner:
    """serve 模式运行时：调度器 + 消费循环 + 故障计数 + 优雅关闭。"""

    def __init__(
        self,
        *,
        kdb: Any,
        qdb: Any,
        push_queue: Optional[Any],
        logger: logging.Logger,
        reports_dir: Path,
        ollama_host: str = "http://localhost:11434",
        ollama_model: str = "gemma4:e4b",
        d1_keywords: Optional[List[str]] = None,
        d4_industries_keywords: Optional[Dict[str, List[str]]] = None,
        gemma_available: bool = True,
    ):
        self.kdb = kdb
        self.qdb = qdb
        self.push_queue = push_queue
        self.logger = logger
        self.reports_dir = Path(reports_dir)
        self.ollama_host = ollama_host
        self.ollama_model = ollama_model
        self.d1_keywords = d1_keywords or DEFAULT_D1_KEYWORDS
        self.d4_industries_keywords = d4_industries_keywords
        self.gemma_available = gemma_available

        self.shutdown_event: Optional[asyncio.Event] = None
        self.scheduler: Optional[Any] = None
        self.failure_counters: Dict[str, int] = {}
        self.alerted_agents: set = set()
        self.signal_agent: Optional[Any] = None
        self.direction_agent: Optional[Any] = None

    # ── Collector factory ──

    def make_collector(self, source: str) -> Any:
        if source == "D1":
            from infra.data_adapters.gov_cn import GovCNCollector
            return GovCNCollector(db=self.kdb, keywords=self.d1_keywords)
        if source == "D4":
            from infra.data_adapters.arxiv_semantic import PaperCollector
            return PaperCollector(
                db=self.kdb,
                industries_keywords=self.d4_industries_keywords,
            )
        if source == "V1":
            from infra.data_adapters.nbs import NBSCollector
            return NBSCollector(db=self.kdb)
        if source == "V3":
            from infra.data_adapters.korea_customs import KoreaCustomsCollector
            return KoreaCustomsCollector(db=self.kdb)
        if source == "S4":
            from infra.data_adapters.akshare_wrapper import AkShareCollector
            return AkShareCollector(db=self.kdb)
        raise ValueError(f"unknown source: {source!r}")

    # ── 任务封装（所有 scheduled jobs 走这里；单 job 崩溃不影响其他）──

    async def _run_in_thread(self, fn: Callable, *args, **kwargs) -> Any:
        """在默认线程池跑同步阻塞调用，避免堵 asyncio 事件循环。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def job_collect(self, source: str, days: int = 7) -> int:
        """采集任务。返回新增行数。"""
        self.logger.info(f"[collect:{source}] starting (days={days})")
        try:
            collector = self.make_collector(source)
            units = await self._run_in_thread(collector.collect_recent, days)
            added = await self._run_in_thread(collector.persist_batch, units)
            self.logger.info(
                f"[collect:{source}] done: +{added} new rows "
                f"(fetched {len(units) if hasattr(units, '__len__') else '?'})"
            )
            self._clear_failure(f"collect_{source}")
            return int(added or 0)
        except Exception as e:
            self._handle_failure(f"collect_{source}", e)
            return 0

    async def job_weekly_industry_report(self) -> None:
        self.logger.info("[report:weekly_industry] starting")
        try:
            agent = self._build_direction_agent()
            report, path = await self._run_in_thread(
                agent.weekly_industry_report,
                None,                 # industry_name=None → 所有 active
                self.gemma_available, # use_gemma
                True,                 # save
            )
            self.logger.info(
                f"[report:weekly_industry] saved: {path} ({len(report)} chars)"
            )
            self._clear_failure("weekly_industry_report")
        except Exception as e:
            self._handle_failure("weekly_industry_report", e)

    async def job_weekly_paper_report(self) -> None:
        self.logger.info("[report:weekly_paper] starting")
        try:
            agent = self._build_direction_agent()
            report, path = await self._run_in_thread(
                agent.weekly_paper_report,
                self.gemma_available,  # use_gemma
                True,                  # save
            )
            self.logger.info(
                f"[report:weekly_paper] saved: {path} ({len(report)} chars)"
            )
            self._clear_failure("weekly_paper_report")
        except Exception as e:
            self._handle_failure("weekly_paper_report", e)

    async def job_daily_briefing(self) -> None:
        """Phase 1 基础版：汇总当日新增信号 → 推送 daily_briefing。"""
        self.logger.info("[briefing:daily] starting")
        try:
            if self.push_queue is None:
                self.logger.info("[briefing:daily] skipped (no push_queue)")
                return
            from infra.dashboard import get_all_active_industries_dashboards
            dashboards = await self._run_in_thread(
                get_all_active_industries_dashboards, self.kdb, 1
            )
            highlights: List[str] = []
            for d in dashboards:
                total = d["recent_signals_total"]
                if total > 0:
                    sup = d["policy_direction_distribution"].get("supportive", 0)
                    res = d["policy_direction_distribution"].get("restrictive", 0)
                    highlights.append(
                        f"{d['industry']}: {total} 条新信号"
                        f"（supportive={sup}, restrictive={res}）"
                    )
            content = {
                "highlights": highlights or ["今日无新增信号"],
                "industries_covered": len(dashboards),
                "generated_by": "main.py daily_briefing",
            }
            event_id = await self._run_in_thread(
                self.push_queue.push_daily_briefing, content
            )
            self.logger.info(
                f"[briefing:daily] pushed event_id={event_id[:8]}... "
                f"({len(highlights)} highlights)"
            )
            self._clear_failure("daily_briefing")
        except Exception as e:
            self._handle_failure("daily_briefing", e)

    async def job_financial_refresh(self) -> None:
        """周任务：刷新 A 股 stock_financials（Z'' + PEG）。失败计入 agent_errors。"""
        self.logger.info("[financial:refresh] starting")
        try:
            from agents.financial_agent import FinancialAgent
            agent = FinancialAgent(self.kdb)
            result = await self._run_in_thread(agent.run)
            if result is None:
                self.logger.warning("[financial:refresh] agent returned None")
                self._handle_failure("financial_refresh", RuntimeError("returned None"))
                return
            self.logger.info(
                f"[financial:refresh] done: processed={result['processed']} "
                f"succeeded={result['succeeded']} failed={result['failed']}"
            )
            self._clear_failure("financial_refresh")
        except Exception as e:
            self._handle_failure("financial_refresh", e)

    async def job_heartbeat(self) -> None:
        try:
            stats = await self._run_in_thread(
                self.qdb.queue_stats, "push_outbox"
            )
            pending = stats.get("push_outbox", {}).get("pending", 0)
            self.logger.info(
                f"[alive] scheduler heartbeat | push_outbox pending={pending} | "
                f"failures={dict(self.failure_counters)}"
            )
        except Exception as e:
            self.logger.warning(f"[alive] heartbeat failed: {e}")

    # ── 消费循环（collection_to_knowledge → SignalCollectorAgent）──

    async def signal_consumer_loop(self) -> None:
        """消费 collection_to_knowledge 队列。Phase 1 通常空（采集器直写 info_units）。

        若有消息：调 SignalCollectorAgent.process → 成功则 ack + 持久化；
        否则 nack（走 QueueManager 重试逻辑）。
        """
        self.logger.info("[consumer] signal processor loop started")
        try:
            while not self.shutdown_event.is_set():
                msg = await self._run_in_thread(
                    self.qdb.dequeue,
                    "collection_to_knowledge",
                    "scout_main_consumer",
                )
                if msg is None:
                    try:
                        await asyncio.wait_for(
                            self.shutdown_event.wait(),
                            timeout=SIGNAL_CONSUMER_POLL_INTERVAL_SEC,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._process_signal_message(msg)
        except asyncio.CancelledError:
            self.logger.info("[consumer] signal processor loop cancelled")
            raise
        except Exception as e:
            self.logger.error(f"[consumer] fatal: {type(e).__name__}: {e}")
        finally:
            self.logger.info("[consumer] signal processor loop stopped")

    async def _process_signal_message(self, msg: Dict[str, Any]) -> None:
        event_id = msg["event_id"]
        try:
            payload = msg["payload"]
            if self.signal_agent is None:
                self.signal_agent = self._build_signal_agent()
            unit = await self._run_in_thread(
                self.signal_agent.process,
                payload.get("raw_text", ""),
                payload.get("source", ""),
                payload.get("title", ""),
                payload.get("published_date", ""),
                payload.get("raw_metadata"),
            )
            if unit is None:
                # SignalCollectorAgent 内部已处理错误（llm/rule/data/parse）；nack 让它重试
                await self._run_in_thread(
                    self.qdb.nack, event_id, "signal agent returned None"
                )
                self.logger.warning(
                    f"[consumer] unit=None → nack event_id={event_id[:8]}..."
                )
                return
            await self._run_in_thread(self._persist_info_unit, unit)
            await self._run_in_thread(self.qdb.ack, event_id)
            self.logger.info(
                f"[consumer] processed event_id={event_id[:8]}... "
                f"unit={unit.id} direction={unit.policy_direction}"
            )
        except Exception as e:
            try:
                await self._run_in_thread(
                    self.qdb.nack,
                    event_id,
                    f"{type(e).__name__}: {str(e)[:200]}",
                )
            except Exception:
                pass
            self.logger.error(
                f"[consumer] error for event_id={event_id[:8]}...: "
                f"{type(e).__name__}: {e}"
            )

    def _persist_info_unit(self, unit: Any) -> None:
        """把 SignalCollectorAgent 输出的 InfoUnitV1 写入 info_units。"""
        now = _now_utc()
        self.kdb.write(
            """INSERT OR IGNORE INTO info_units
               (id, source, source_credibility, timestamp, category, content,
                related_industries, policy_direction, mixed_subtype, schema_version,
                event_chain_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                unit.id,
                unit.source,
                unit.source_credibility,
                unit.timestamp,
                unit.category,
                unit.content,
                json.dumps(unit.related_industries, ensure_ascii=False),
                unit.policy_direction,
                unit.mixed_subtype,
                unit.schema_version,
                unit.event_chain_id,
                now,
                now,
            ),
        )

    # ── Agent 惰性构造 ──

    def _build_signal_agent(self) -> Any:
        from agents.signal_collector import SignalCollectorAgent
        return SignalCollectorAgent(
            db=self.kdb,
            ollama_host=self.ollama_host,
            model=self.ollama_model,
        )

    def _build_direction_agent(self) -> Any:
        if self.direction_agent is None:
            from agents.direction_judge import DirectionJudgeAgent
            self.direction_agent = DirectionJudgeAgent(
                db=self.kdb,
                ollama_host=self.ollama_host,
                model=self.ollama_model,
                reports_dir=self.reports_dir,
                push_queue=self.push_queue,
            )
        return self.direction_agent

    # ── 失败计数与告警 ──

    def _handle_failure(self, agent_id: str, exc: Exception) -> None:
        count = self.failure_counters.get(agent_id, 0) + 1
        self.failure_counters[agent_id] = count
        self.logger.error(
            f"[{agent_id}] failure #{count}/{AGENT_FAILURE_THRESHOLD}: "
            f"{type(exc).__name__}: {exc}"
        )
        if (
            count >= AGENT_FAILURE_THRESHOLD
            and self.push_queue is not None
            and agent_id not in self.alerted_agents
        ):
            try:
                alert_type = (
                    "data_source_down"
                    if agent_id.startswith("collect_")
                    else "failure_level_change"
                )
                self.push_queue.push_alert(
                    alert_type,
                    {
                        "agent": agent_id,
                        "failure_count": count,
                        "last_error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    },
                    entity_key=f"alert_{agent_id}_sustained_{_today_kst_date_str()}",
                )
                self.alerted_agents.add(agent_id)
                self.logger.warning(
                    f"[{agent_id}] red alert pushed "
                    f"({count} consecutive failures)"
                )
            except Exception as push_err:
                self.logger.error(
                    f"[{agent_id}] push alert itself failed: {push_err}"
                )

    def _clear_failure(self, agent_id: str) -> None:
        if agent_id in self.failure_counters:
            del self.failure_counters[agent_id]
        self.alerted_agents.discard(agent_id)

    # ── 调度 ──

    def schedule_all(self) -> None:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        assert self.scheduler is not None, "scheduler must be built first"

        now_utc = datetime.now(tz=timezone.utc)
        stagger = [30, 60, 90, 120, 150]  # 避免启动时全部同时开跑

        schedule_matrix = [
            ("D1", IntervalTrigger(hours=6), stagger[0]),
            ("D4", IntervalTrigger(hours=24), stagger[1]),
            ("V3", IntervalTrigger(hours=24), stagger[2]),  # Phase 1 将持续失败，预期
            ("S4", IntervalTrigger(hours=12), stagger[3]),
        ]
        for source, trigger, first_delay in schedule_matrix:
            self.scheduler.add_job(
                self.job_collect, trigger=trigger,
                args=[source], id=f"collect_{source}",
                next_run_time=now_utc + timedelta(seconds=first_delay),
                replace_existing=True,
                max_instances=1, coalesce=True, misfire_grace_time=3600,
            )

        # V1 每天午夜 KST（不 stagger，因为用 cron）
        self.scheduler.add_job(
            self.job_collect,
            CronTrigger(hour=0, minute=0, timezone=KST),
            args=["V1"], id="collect_V1",
            replace_existing=True,
            max_instances=1, coalesce=True,
        )

        # 周报
        self.scheduler.add_job(
            self.job_weekly_industry_report,
            CronTrigger(day_of_week="mon", hour=7, minute=0, timezone=KST),
            id="weekly_industry_report",
            replace_existing=True, max_instances=1,
        )
        self.scheduler.add_job(
            self.job_weekly_paper_report,
            CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=KST),
            id="weekly_paper_report",
            replace_existing=True, max_instances=1,
        )

        # 每日简报
        self.scheduler.add_job(
            self.job_daily_briefing,
            CronTrigger(hour=7, minute=30, timezone=KST),
            id="daily_briefing",
            replace_existing=True, max_instances=1,
        )

        # v1.01：A 股财务周刷新（周六 02:00 KST，避开开盘和周报时间）
        self.scheduler.add_job(
            self.job_financial_refresh,
            CronTrigger(day_of_week="sat", hour=2, minute=0, timezone=KST),
            id="financial_refresh",
            replace_existing=True, max_instances=1,
            misfire_grace_time=3600,
        )

        # 心跳
        self.scheduler.add_job(
            self.job_heartbeat,
            IntervalTrigger(seconds=HEARTBEAT_INTERVAL_SECONDS),
            id="heartbeat",
            replace_existing=True, max_instances=1,
        )

        if not self.gemma_available:
            self.logger.warning(
                "[schedule] Ollama offline — 周报 AI 分析与信号处理将降级"
            )
        self.logger.info(
            f"[schedule] {len(self.scheduler.get_jobs())} jobs registered"
        )

    # ── 运行入口 ──

    async def run(
        self,
        *,
        max_runtime_seconds: Optional[float] = None,
        install_signal_handlers: bool = True,
    ) -> int:
        """启动 serve 循环。max_runtime_seconds 用于测试：N 秒后自动发 shutdown。"""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self.shutdown_event = asyncio.Event()
        self.scheduler = AsyncIOScheduler(timezone=KST)

        if install_signal_handlers:
            self._install_signal_handlers()

        self.schedule_all()
        self.scheduler.start()

        consumer_task = asyncio.create_task(self.signal_consumer_loop())

        self.logger.info("[serve] ready — waiting for shutdown signal")

        try:
            if max_runtime_seconds is not None:
                try:
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=max_runtime_seconds,
                    )
                except asyncio.TimeoutError:
                    self.logger.info(
                        f"[serve] max_runtime_seconds={max_runtime_seconds} "
                        "reached, shutting down"
                    )
                    self.shutdown_event.set()
            else:
                await self.shutdown_event.wait()
        finally:
            self.logger.info("[serve] shutting down scheduler + consumer")
            try:
                self.scheduler.shutdown(wait=False)
            except Exception as e:
                self.logger.error(f"[serve] scheduler shutdown error: {e}")
            consumer_task.cancel()
            try:
                await asyncio.wait_for(
                    consumer_task,
                    timeout=GRACEFUL_SHUTDOWN_TIMEOUT_SEC,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self.logger.info("[serve] shutdown complete")
        return 0

    def _install_signal_handlers(self) -> None:
        def _handler(sig_num, frame):
            if self.shutdown_event is not None and not self.shutdown_event.is_set():
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_soon_threadsafe(self.shutdown_event.set)
                except RuntimeError:
                    self.shutdown_event.set()
                self.logger.info(f"[signal] received {sig_num}, initiating shutdown")

        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, _handler)
            except (AttributeError, ValueError, OSError):
                # Windows 对 SIGTERM 支持有限；静默跳过
                pass


# ══════════════════════ 子命令实现 ══════════════════════


def cmd_status(
    *,
    kdb_path: Path,
    qdb_path: Path,
    reports_dir: Path,
    logger: logging.Logger,
) -> int:
    """status 子命令：打印系统状态 + 健康度。复用 MCP 的 get_system_status。"""
    from infra.db_manager import DatabaseManager
    from infra.mcp_server import ScoutToolImpl

    kdb = DatabaseManager(kdb_path)
    try:
        impl = ScoutToolImpl(db=kdb, reports_dir=reports_dir)
        status = impl.get_system_status()
    finally:
        kdb.close()

    # 队列状态
    qstats: Dict[str, Dict[str, int]] = {}
    try:
        from infra.queue_manager import QueueManager
        qm = QueueManager(qdb_path)
        try:
            qstats = qm.queue_stats()
        finally:
            qm.close()
    except Exception as e:
        logger.warning(f"queue stats unavailable: {e}")

    _print_status(status, qstats, kdb_path, qdb_path)
    return 0


def _print_status(
    status: Dict[str, Any],
    qstats: Dict[str, Dict[str, int]],
    kdb_path: Path,
    qdb_path: Path,
) -> None:
    sep = "=" * 64
    print(sep)
    print(f"Scout 系统状态  |  snapshot_at={status.get('snapshot_at')}")
    print(sep)

    print("\n[信源最后采集时间（created_at）]")
    for src in SOURCES:
        last = status["source_last_collected"].get(src)
        print(f"  {src}: {last or '(未采集)'}")

    print("\n[数据库]")
    db = status["database"]
    print(f"  info_units total          : {db['info_units_total']}")
    print(
        f"  watchlist active/total    : "
        f"{db['watchlist_active']}/{db['watchlist_total']}"
    )
    print(f"  agent_errors total        : {db['agent_errors_total']}")
    print(f"  llm_invocations total     : {db['llm_invocations_total']}")

    print("\n[今日成本（since_utc=当日 0 点 UTC）]")
    tc = status["today_cost"]
    print(f"  tokens               : {tc['total_tokens']}")
    print(f"  cost_cents           : {tc['total_cost_cents']}")
    print(f"  invocation_count     : {tc['invocation_count']}")

    print("\n[最近 7 日错误]")
    errs = status["errors_by_type_7d"]
    if not errs:
        print("  （零错误）")
    else:
        for k, v in errs.items():
            print(f"  {k}: {v}")

    print("\n[队列]")
    if not qstats:
        print("  （不可用）")
    else:
        for qname, bucket in qstats.items():
            print(
                f"  {qname:<28}  pending={bucket['pending']} "
                f"processing={bucket['processing']} "
                f"done={bucket['done']} failed={bucket['failed']}"
            )

    print("\n[磁盘]")
    print(f"  knowledge.db : {_format_size(kdb_path)}")
    print(f"  queue.db     : {_format_size(qdb_path)}")

    health = _compute_health(status, qstats)
    print("\n[健康度]")
    print(f"  {health['badge']} {health['level']}  — {health['reason']}")


def _compute_health(
    status: Dict[str, Any], qstats: Dict[str, Dict[str, int]]
) -> Dict[str, str]:
    errs_7d = status.get("active_errors_7d", 0)
    push_failed = (
        qstats.get("push_outbox", {}).get("failed", 0) if qstats else 0
    )
    any_source_collected = any(
        v is not None for v in status.get("source_last_collected", {}).values()
    )
    if errs_7d >= 20 or push_failed >= 5 or not any_source_collected:
        return {
            "badge": "🔴",
            "level": "RED",
            "reason": (
                f"errors_7d={errs_7d} push_failed={push_failed} "
                f"any_source_collected={any_source_collected}"
            ),
        }
    if errs_7d > 0 or push_failed > 0:
        return {
            "badge": "🟡",
            "level": "YELLOW",
            "reason": f"errors_7d={errs_7d} push_failed={push_failed}",
        }
    return {
        "badge": "🟢",
        "level": "GREEN",
        "reason": "no errors in last 7 days; all core queues clean",
    }


def _format_size(p: Path) -> str:
    if not p.exists():
        return "(missing)"
    size = p.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size/1024:.1f} KB"
    return f"{size/1024/1024:.2f} MB"


def cmd_collect(
    source: str,
    days: int,
    *,
    kdb_path: Path,
    logger: logging.Logger,
) -> int:
    from infra.db_manager import DatabaseManager

    if source not in SOURCES:
        logger.error(f"unknown source: {source}")
        return 2
    logger.info(f"[cli:collect] source={source} days={days}")

    kdb = DatabaseManager(kdb_path)
    try:
        runner = ScoutRunner(
            kdb=kdb, qdb=None, push_queue=None,
            logger=logger, reports_dir=DEFAULT_REPORTS_DIR,
        )
        collector = runner.make_collector(source)
        units = collector.collect_recent(days=days)
        added = collector.persist_batch(units)
        logger.info(
            f"[cli:collect] {source}: fetched "
            f"{len(units) if hasattr(units, '__len__') else '?'}, "
            f"persisted {added}"
        )
        print(f"{source}: +{added} new rows")
        return 0
    except Exception as e:
        logger.error(f"[cli:collect] failed: {type(e).__name__}: {e}")
        return 1
    finally:
        kdb.close()


def cmd_process(
    *,
    kdb_path: Path,
    qdb_path: Path,
    ollama_host: str,
    ollama_model: str,
    logger: logging.Logger,
    max_messages: Optional[int] = None,
) -> int:
    """手动消费 collection_to_knowledge 队列 N 条（or 至空）。"""
    from infra.db_manager import DatabaseManager
    from infra.queue_manager import QueueManager

    kdb = DatabaseManager(kdb_path)
    qm = QueueManager(qdb_path)
    processed = 0
    try:
        runner = ScoutRunner(
            kdb=kdb, qdb=qm, push_queue=None,
            logger=logger, reports_dir=DEFAULT_REPORTS_DIR,
            ollama_host=ollama_host, ollama_model=ollama_model,
        )
        while True:
            if max_messages is not None and processed >= max_messages:
                break
            msg = qm.dequeue("collection_to_knowledge", "scout_cli_process")
            if msg is None:
                break
            asyncio.run(runner._process_signal_message(msg))
            processed += 1
        logger.info(f"[cli:process] processed {processed} messages")
        print(f"processed: {processed}")
        return 0
    finally:
        qm.close()
        kdb.close()


def cmd_masters(
    *,
    symbol: str,
    kdb_path: Path,
    logger: logging.Logger,
) -> int:
    """对单股运行 5 大师评分（v1.03 MasterAgent）。"""
    import json as _json

    from agents.master_agent import MasterAgent
    from infra.db_manager import DatabaseManager

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if not symbol or len(symbol) != 6 or not symbol.isdigit():
        logger.error(f"[cli:masters] invalid symbol: {symbol!r} (要 6 位数字)")
        return 2

    kdb = DatabaseManager(kdb_path)
    try:
        agent = MasterAgent(kdb)
        out = agent.analyze_stock(symbol)
        print(out["report"])
        print()
        print("--- JSON ---")
        print(_json.dumps(
            {k: v for k, v in out.items() if k != "report"},
            ensure_ascii=False, indent=2,
        ))
        if not out.get("ok"):
            logger.warning(f"[cli:masters] {symbol} ok=False: {out.get('error')}")
            return 0  # 数据不足不算异常退出
        scores = {
            r["master"]: r["score"] for r in out.get("results", [])
        }
        logger.info(
            f"[cli:masters] {symbol} period={out.get('report_period')} scores={scores}"
        )
        return 0
    except Exception as e:
        logger.error(f"[cli:masters] failed: {type(e).__name__}: {e}")
        return 1
    finally:
        kdb.close()


def cmd_report(
    report_type: str,
    *,
    kdb_path: Path,
    qdb_path: Path,
    ollama_host: str,
    ollama_model: str,
    reports_dir: Path,
    logger: logging.Logger,
    use_gemma: bool = True,
) -> int:
    from agents.direction_judge import DirectionJudgeAgent
    from infra.db_manager import DatabaseManager
    from infra.push_queue import PushQueue
    from infra.queue_manager import QueueManager

    if report_type not in ("industry", "paper"):
        logger.error(f"unknown report type: {report_type}")
        return 2

    kdb = DatabaseManager(kdb_path)
    qm = QueueManager(qdb_path)
    try:
        push_queue = PushQueue(qm)
        agent = DirectionJudgeAgent(
            db=kdb, ollama_host=ollama_host, model=ollama_model,
            reports_dir=reports_dir, push_queue=push_queue,
        )
        if report_type == "industry":
            report, path = agent.weekly_industry_report(
                use_gemma=use_gemma, save=True
            )
        else:
            report, path = agent.weekly_paper_report(
                use_gemma=use_gemma, save=True
            )
        logger.info(f"[cli:report] {report_type} saved: {path}")
        print(f"[ok] saved: {path}")
        print(f"     length: {len(report)} chars")
        return 0
    finally:
        qm.close()
        kdb.close()


# ══════════════════════ 辅助 ══════════════════════


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _today_kst_date_str() -> str:
    return datetime.now(tz=KST).strftime("%Y%m%d")


def _print_banner(
    logger: logging.Logger,
    mode: str,
    kdb_path: Path,
    qdb_path: Path,
    logs_dir: Path,
    cfg_mode: str,
    gemma_ok: bool,
) -> None:
    banner_lines = [
        "╔" + "═" * 62 + "╗",
        "║  Scout Phase 1                                               ║",
        f"║  mode={mode:<10}  scout_mode={cfg_mode:<12}  gemma={'on' if gemma_ok else 'off':<4}  ║",
        f"║  kdb = {str(kdb_path):<54}  ║"[:66],
        f"║  qdb = {str(qdb_path):<54}  ║"[:66],
        f"║  logs= {str(logs_dir):<54}  ║"[:66],
        "╚" + "═" * 62 + "╝",
    ]
    for line in banner_lines:
        logger.info(line)


# ══════════════════════ CLI ══════════════════════


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="Scout Phase 1 unified CLI",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help="config.yaml path (default: <root>/config.yaml)",
    )
    parser.add_argument(
        "--db", default=None,
        help="knowledge.db path override（也可用 SCOUT_DB_PATH env）",
    )
    parser.add_argument(
        "--queue-db", default=None,
        help="queue.db path override",
    )
    parser.add_argument(
        "--log-level", default=None,
        help="override log level (DEBUG/INFO/WARNING/ERROR)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="完整运行：调度 + 消费循环（长跑）").add_argument(
        "--max-runtime-seconds", type=float, default=None,
        help="N 秒后自动优雅关闭（用于冒烟/测试；默认无限）",
    )

    sub.add_parser("mcp", help="MCP stdio server（阻塞）")

    collect_p = sub.add_parser("collect", help="手动采集某信源")
    collect_p.add_argument("--source", required=True, choices=list(SOURCES))
    collect_p.add_argument("--days", type=int, default=7)

    process_p = sub.add_parser(
        "process",
        help="手动消费 collection_to_knowledge 队列至空",
    )
    process_p.add_argument(
        "--max", dest="max_messages", type=int, default=None,
        help="最多处理 N 条（默认无限，直到队列空）",
    )

    report_p = sub.add_parser("report", help="手动生成周报")
    report_p.add_argument("--type", dest="report_type",
                          required=True, choices=["industry", "paper"])
    report_p.add_argument("--no-gemma", action="store_true",
                          help="跳过 Gemma 分析，只出数据部分")

    sub.add_parser("status", help="系统状态快照")

    masters_p = sub.add_parser("masters", help="对单股运行 5 大师评分（v1.03）")
    masters_p.add_argument("--symbol", required=True, help="A 股 6 位代码")

    return parser


def resolve_paths(args, env: Optional[Dict[str, str]] = None) -> Dict[str, Path]:
    env_dict = env if env is not None else os.environ
    kdb_env = env_dict.get("SCOUT_DB_PATH")
    qdb_env = env_dict.get("SCOUT_QUEUE_DB_PATH")

    if args.db:
        kdb_path = Path(args.db)
    elif kdb_env:
        kdb_path = Path(kdb_env)
    else:
        kdb_path = DEFAULT_DATA_DIR / "knowledge.db"

    if args.queue_db:
        qdb_path = Path(args.queue_db)
    elif qdb_env:
        qdb_path = Path(qdb_env)
    else:
        qdb_path = DEFAULT_DATA_DIR / "queue.db"

    return {
        "kdb_path": kdb_path.resolve(),
        "qdb_path": qdb_path.resolve(),
        "logs_dir": DEFAULT_LOGS_DIR,
        "reports_dir": DEFAULT_REPORTS_DIR,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # mcp 子命令特殊：stdio 模式，stdout 归 MCP 协议用。
    # 不初始化 console logger（避免污染），直接委托。
    if args.command == "mcp":
        from infra.mcp_server import main as mcp_main
        return mcp_main([])

    paths = resolve_paths(args)

    # 加载 config（尽力；失败就用默认）
    cfg_mode = "unknown"
    ollama_host = "http://localhost:11434"
    ollama_model = "gemma4:e4b"
    log_level = args.log_level or "INFO"
    try:
        from config.loader import load_config
        cfg = load_config(config_path=Path(args.config))
        cfg_mode = cfg.mode
        ollama_model = cfg.llm.local_model
        # 可加 ollama_host 字段到 config；Phase 1 写死 localhost:11434
    except FileNotFoundError as e:
        print(f"[fatal] config not found: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[fatal] config validation failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2

    logger = configure_logging(paths["logs_dir"], log_level=log_level)
    bootstrap_paths(
        knowledge_db_path=paths["kdb_path"],
        queue_db_path=paths["qdb_path"],
        logs_dir=paths["logs_dir"],
        reports_dir=paths["reports_dir"],
        logger=logger,
    )

    if args.command == "status":
        return cmd_status(
            kdb_path=paths["kdb_path"],
            qdb_path=paths["qdb_path"],
            reports_dir=paths["reports_dir"],
            logger=logger,
        )

    if args.command == "collect":
        return cmd_collect(
            source=args.source, days=args.days,
            kdb_path=paths["kdb_path"], logger=logger,
        )

    if args.command == "process":
        return cmd_process(
            kdb_path=paths["kdb_path"],
            qdb_path=paths["qdb_path"],
            ollama_host=ollama_host, ollama_model=ollama_model,
            logger=logger,
            max_messages=args.max_messages,
        )

    if args.command == "masters":
        return cmd_masters(
            symbol=args.symbol,
            kdb_path=paths["kdb_path"],
            logger=logger,
        )

    if args.command == "report":
        return cmd_report(
            report_type=args.report_type,
            kdb_path=paths["kdb_path"],
            qdb_path=paths["qdb_path"],
            ollama_host=ollama_host, ollama_model=ollama_model,
            reports_dir=paths["reports_dir"],
            logger=logger,
            use_gemma=not args.no_gemma,
        )

    if args.command == "serve":
        gemma_ok = check_ollama(ollama_host, logger)
        _print_banner(
            logger, mode="serve",
            kdb_path=paths["kdb_path"], qdb_path=paths["qdb_path"],
            logs_dir=paths["logs_dir"],
            cfg_mode=cfg_mode, gemma_ok=gemma_ok,
        )

        from infra.db_manager import DatabaseManager
        from infra.push_queue import PushQueue
        from infra.queue_manager import QueueManager
        kdb = DatabaseManager(paths["kdb_path"])
        qm = QueueManager(paths["qdb_path"])
        try:
            push_queue = PushQueue(qm)
            runner = ScoutRunner(
                kdb=kdb, qdb=qm, push_queue=push_queue,
                logger=logger,
                reports_dir=paths["reports_dir"],
                ollama_host=ollama_host, ollama_model=ollama_model,
                gemma_available=gemma_ok,
            )
            return asyncio.run(
                runner.run(max_runtime_seconds=args.max_runtime_seconds)
            )
        finally:
            qm.close()
            kdb.close()

    # 理论不可达
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
