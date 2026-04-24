"""
agents/direction_judge.py — v1.66 Phase 1 Step 9 方向判断 Agent（简化版）

Phase 1 职责：
    (a) 周度行业报告（weekly_industry_report）— 每周一跑
    (b) 周度论文报告（weekly_paper_report）     — 每周日跑
    (c) 跨信号交叉验证（cross_signal_validation）— v1.59 行业级 direction 汇总

Phase 2B 将升级到 Sonnet 完整版：
    - 事件串关联
    - 多解读信号的 LLM 补判
    - 动机层级分析

Gemma 调用性质（相对 Step 7）：
    - "总结性"任务，非结构化判断（输出自然语言，非 JSON）
    - 不做规则层覆盖
    - 温度略高（0.3），让周报不呆板

降级策略（用户明确要求）：
    - Ollama 连不上 → 周报只含 dashboard 数据，标注 GEMMA_OFFLINE_BANNER，不 raise
    - 周报方法"尽力返回字符串"，内部 catch ScoutError 并记 agent_errors

所有 Gemma 调用记 llm_invocations（agent_name=direction_judge，prompt_version 按 task 区分）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.base import (
    BaseAgent,
    DataMissingError,
    LLMError,
    ParseError,
    RuleViolation,
)
from infra.dashboard import (
    build_industry_dashboard,
    get_all_active_industries_dashboards,
)
from infra.db_manager import DatabaseManager
from utils.llm_client import LLMClient, LLMResponse, OllamaClient
from utils.time_utils import now_utc


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = PROJECT_ROOT / "prompts"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"

GEMMA_OFFLINE_BANNER = "⚠ AI 分析未启用（Gemma 离线或调用失败）"
STALENESS_THRESHOLD_DAYS = 180           # v1.56 局限2：信号 180 天不更新则警示
WEEKLY_INDUSTRY_WINDOW_DAYS = 7
WEEKLY_PAPER_WINDOW_DAYS = 7
CROSS_SIGNAL_WINDOW_DAYS = 30
PAPER_TOP_N = 10

_VALID_TASKS = {"weekly_industry", "weekly_paper", "cross_signal"}


class DirectionJudgeAgent(BaseAgent):
    """Phase 1 简化版方向判断 Agent — 报告生成 + 统计汇总。"""

    def __init__(
        self,
        db: DatabaseManager,
        ollama_host: str = "http://localhost:11434",
        model: str = "gemma4:e4b",
        prompt_version: str = "v001",
        timeout: float = 30.0,
        max_gemma_retries: int = 2,
        retry_wait_seconds: float = 1.0,
        reports_dir: Optional[Path] = None,
        ollama_client: Any = None,
        llm_client: Optional[LLMClient] = None,
        push_queue: Any = None,
    ):
        """
        Args:
            ollama_client: （兼容路径）旧的 Ollama SDK 客户端 mock；测试里仍可用。
            llm_client:    （v1.48 首选路径）直接传 LLMClient；None 则走 config.from_config()。
                           两者都不传 → LLMClient.from_config("gemma_local") 按 config.yaml 装配。
            push_queue: 可选 PushQueue 实例；传入后 weekly_* 方法成功后自动调用
                        push_weekly_report()。None → 不推送（向后兼容 Step 9）。
        """
        super().__init__(name="direction_judge", db=db)
        self.ollama_host = ollama_host
        self.model = model
        self.prompt_version = prompt_version
        self.timeout = timeout
        self.max_gemma_retries = max_gemma_retries
        self.retry_wait_seconds = retry_wait_seconds
        self.reports_dir = Path(reports_dir) if reports_dir else DEFAULT_REPORTS_DIR
        self.push_queue = push_queue

        # v1.48 LLM 抽象层：3 路注入
        #   1) llm_client 直接注入 → 测试主路径
        #   2) ollama_client 注入（旧测试接口）→ 包一个 OllamaClient
        #   3) 都不传 → LLMClient.from_config("gemma_local")
        if llm_client is not None:
            self.llm: LLMClient = llm_client
        elif ollama_client is not None:
            self.llm = OllamaClient(
                provider_name="gemma_local",
                model=self.model,
                endpoint=self.ollama_host,
                timeout=self.timeout,
                client_override=ollama_client,
            )
        else:
            self.llm = LLMClient.from_config("gemma_local")

        # 启动时加载两个 prompt（缺失即爆）
        self.weekly_prompt = self._load_prompt("direction_judge_weekly")
        self.paper_prompt = self._load_prompt("direction_judge_paper")

    # ══════════════════════ 入口 ══════════════════════

    def run(self, task: str = "weekly_industry", **kwargs: Any) -> Any:
        """任务分派入口，走 BaseAgent 错误矩阵包装。"""
        return self.run_with_error_handling(self._dispatch, task, **kwargs)

    def _dispatch(self, task: str, **kwargs: Any) -> Any:
        if task not in _VALID_TASKS:
            raise RuleViolation(
                f"unknown task {task!r}; allowed: {sorted(_VALID_TASKS)}"
            )
        if task == "weekly_industry":
            return self.weekly_industry_report(**kwargs)
        if task == "weekly_paper":
            return self.weekly_paper_report(**kwargs)
        return self.cross_signal_validation(**kwargs)

    # ══════════════════════ (a) 行业周报 ══════════════════════

    def weekly_industry_report(
        self,
        industry_name: Optional[str] = None,
        use_gemma: bool = True,
        save: bool = True,
    ) -> Tuple[str, Optional[Path]]:
        """生成周度行业报告 Markdown。

        Args:
            industry_name: None → 遍历 watchlist 所有 zone='active'
            use_gemma: False 则完全不调 Gemma（纯数据报告）
            save: True → 同时写 reports/weekly_industry_YYYYMMDD.md

        Returns:
            (markdown_text, saved_path_or_None)
        """
        report_date = datetime.now(tz=timezone.utc).strftime("%Y%m%d")

        if industry_name is None:
            dashboards = get_all_active_industries_dashboards(
                self.db, days=WEEKLY_INDUSTRY_WINDOW_DAYS
            )
        else:
            if not isinstance(industry_name, str) or not industry_name.strip():
                raise RuleViolation("industry_name must be non-empty str or None")
            dashboards = [
                build_industry_dashboard(
                    industry_name, self.db, days=WEEKLY_INDUSTRY_WINDOW_DAYS
                )
            ]

        lines: List[str] = []
        lines.append(f"# Scout 周度行业信号报告 — {report_date}")
        lines.append("")
        lines.append(f"- 生成时间：{now_utc()}")
        lines.append(f"- 覆盖行业：{len(dashboards)} 个")
        lines.append(
            f"- 窗口：最近 {WEEKLY_INDUSTRY_WINDOW_DAYS} 天"
        )
        lines.append(f"- Gemma 分析：{'启用' if use_gemma else '未启用（use_gemma=False）'}")
        lines.append("")

        if not dashboards:
            lines.append("---")
            lines.append("")
            lines.append("*watchlist 中无 `zone='active'` 行业，或传入行业在 DB 中无记录。*")
        else:
            for d in dashboards:
                lines.append("---")
                lines.append("")
                lines.extend(self._format_industry_section(d))

                # 时效警示（回查 180 天窗口）
                stale_days = self._latest_signal_days_ago(d["industry"])
                if stale_days is not None and stale_days > STALENESS_THRESHOLD_DAYS:
                    lines.append("")
                    lines.append(
                        f"> **⚠ 时效警示**：最新信号距今 {stale_days} 天，"
                        f"超过 {STALENESS_THRESHOLD_DAYS} 天阈值（v1.56 局限2）。"
                    )

                # AI 分析
                if use_gemma:
                    lines.append("")
                    lines.append("#### AI 分析")
                    lines.append("")
                    analysis = self._gemma_analyze_industry(d)
                    if analysis:
                        lines.append(analysis)
                    else:
                        lines.append(GEMMA_OFFLINE_BANNER)
                lines.append("")

        report = "\n".join(lines)

        saved_path: Optional[Path] = None
        if save:
            saved_path = self._save_report(
                report, f"weekly_industry_{report_date}.md"
            )

        # Step 11 集成：配置了 push_queue 就自动推送
        self._maybe_push_weekly_report(
            "industry",
            report_text=report,
            saved_path=saved_path,
            industries=[d["industry"] for d in dashboards],
            report_date=report_date,
        )

        return report, saved_path

    def _format_industry_section(self, d: Dict[str, Any]) -> List[str]:
        """单行业 Markdown 段落（不含 Gemma 分析）。"""
        lines: List[str] = []
        lines.append(f"## {d['industry']}")
        lines.append("")
        lines.append(f"- 信号总数（近 {WEEKLY_INDUSTRY_WINDOW_DAYS} 天）：{d['recent_signals_total']}")

        by_source = d["recent_signals_by_source"]
        src_parts = [f"{k}={v}" for k, v in by_source.items() if v > 0]
        lines.append(
            "- 按信源：" + (", ".join(src_parts) if src_parts else "（无）")
        )

        dist = d["policy_direction_distribution"]
        dir_parts = [f"{k}={v}" for k, v in dist.items() if v > 0]
        lines.append(
            "- 方向分布：" + (", ".join(dir_parts) if dir_parts else "（无）")
        )

        mixed = d["mixed_subtype_breakdown"]
        if any(mixed.values()):
            lines.append(
                "- mixed 子类型："
                + ", ".join(f"{k}={v}" for k, v in mixed.items() if v > 0)
            )

        cred = d["source_credibility_weighted_count"]
        cred_parts = [f"{k}={v}" for k, v in cred.items() if v > 0]
        lines.append(
            "- 可信度分布：" + (", ".join(cred_parts) if cred_parts else "（无）")
        )

        fresh = d["data_freshness"]
        if fresh["oldest_signal_days_ago"] is None:
            lines.append("- 数据新鲜度：窗口内无信号")
        else:
            lines.append(
                f"- 数据新鲜度：最老 {fresh['oldest_signal_days_ago']} 天 / "
                f"最新 {fresh['newest_signal_days_ago']} 天；"
                f"周密度 {fresh['signal_density_per_week']}"
            )

        wl = d["watchlist_status"]
        if wl:
            lines.append(
                f"- Watchlist：zone={wl['zone']} "
                f"dimensions={wl['dimensions']} "
                f"verification={wl['verification_status']} "
                f"gap={wl['gap_status']}"
            )
        else:
            lines.append("- Watchlist：未加入")

        if d["latest_signals"]:
            lines.append("")
            lines.append(f"#### 本周新增信号（最多 {len(d['latest_signals'])} 条）")
            lines.append("")
            # 按 supportive > restrictive > mixed > neutral > null 的"重要度"排序
            sorted_sigs = sorted(
                d["latest_signals"],
                key=lambda s: (
                    _direction_importance(s.get("policy_direction")),
                    # tie-breaker：timestamp DESC
                    -(_parse_iso_ts(s.get("timestamp", "")) or 0),
                ),
            )
            for s in sorted_sigs:
                direction = s.get("policy_direction") or "null"
                ts_short = (s.get("timestamp") or "")[:19]
                preview = (s.get("content_preview") or "")[:200]
                lines.append(
                    f"- **[{s.get('source')}]** `{ts_short}` "
                    f"*direction={direction}*"
                )
                if preview:
                    lines.append(f"  - {preview}")
        return lines

    def _gemma_analyze_industry(self, dashboard: Dict[str, Any]) -> Optional[str]:
        """调 Gemma 生成行业分析。失败返 None（降级）+ 落 agent_errors。"""
        payload = json.dumps(dashboard, ensure_ascii=False)
        try:
            resp = self._call_llm_text(self.weekly_prompt, payload, temperature=0.3)
        except DataMissingError as e:
            self._handle_gemma_failure("data", e, "weekly_industry_report")
            return None
        except LLMError as e:
            self._handle_gemma_failure("llm", e, "weekly_industry_report")
            return None
        except ParseError as e:
            self._handle_gemma_failure("parse", e, "weekly_industry_report")
            return None
        self._log_llm_invocation(
            prompt_kind="weekly_industry",
            input_text=payload,
            output_text=resp.text,
            resp=resp,
        )
        return resp.text.strip()

    # ══════════════════════ (b) 论文周报 ══════════════════════

    def weekly_paper_report(
        self,
        use_gemma: bool = True,
        save: bool = True,
        top_n: int = PAPER_TOP_N,
    ) -> Tuple[str, Optional[Path]]:
        """生成周度 D4 论文周报 Markdown。"""
        if not isinstance(top_n, int) or top_n < 1:
            raise RuleViolation("top_n must be int >= 1")

        report_date = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        cutoff = (
            datetime.now(tz=timezone.utc)
            - timedelta(days=WEEKLY_PAPER_WINDOW_DAYS)
        ).isoformat()

        rows = self.db.query(
            """SELECT id, timestamp, category, content, related_industries
               FROM info_units
               WHERE source='D4' AND timestamp >= ?
               ORDER BY timestamp DESC""",
            (cutoff,),
        )
        enriched = self._enrich_papers(rows)
        enriched.sort(
            key=lambda p: (-int(p["citations"] or 0), p["venue"] or "")
        )
        top = enriched[:top_n]

        lines: List[str] = []
        lines.append(f"# Scout 周度论文报告 — {report_date}")
        lines.append("")
        lines.append(f"- 生成时间：{now_utc()}")
        lines.append(
            f"- 窗口：最近 {WEEKLY_PAPER_WINDOW_DAYS} 天（D4：arXiv + Semantic Scholar）"
        )
        lines.append(f"- 本周入库：{len(enriched)} 篇")
        lines.append(f"- Gemma 分析：{'启用' if use_gemma else '未启用（use_gemma=False）'}")
        lines.append("")

        if not enriched:
            lines.append("---")
            lines.append("")
            lines.append("*本周无 D4 新增论文。可能是 Semantic Scholar 限流或论文采集未运行。*")
        else:
            lines.append("---")
            lines.append("")
            lines.append(f"## Top {len(top)} 论文（按引用数 + venue 排序）")
            lines.append("")
            for i, p in enumerate(top, 1):
                lines.extend(self._format_paper_entry(i, p))

            if use_gemma:
                lines.append("---")
                lines.append("")
                lines.append("## AI 周总结")
                lines.append("")
                summary = self._gemma_summarize_papers(top)
                if summary:
                    lines.append(summary)
                else:
                    lines.append(GEMMA_OFFLINE_BANNER)

        report = "\n".join(lines)

        saved_path: Optional[Path] = None
        if save:
            saved_path = self._save_report(
                report, f"weekly_paper_{report_date}.md"
            )

        # Step 11 集成：自动推送
        self._maybe_push_weekly_report(
            "paper",
            report_text=report,
            saved_path=saved_path,
            papers_count=len(enriched),
            top_n=len(top),
            report_date=report_date,
        )

        return report, saved_path

    @staticmethod
    def _enrich_papers(rows: List[Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in rows:
            c: Dict[str, Any] = {}
            try:
                c = json.loads(r["content"]) if r["content"] else {}
                if not isinstance(c, dict):
                    c = {}
            except (ValueError, TypeError):
                c = {}
            try:
                industries = json.loads(r["related_industries"] or "[]")
            except (ValueError, TypeError):
                industries = []
            out.append(
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "title": c.get("title") or "(no title)",
                    "venue": c.get("venue") or c.get("journal") or "arXiv",
                    "citations": int(c.get("citation_count") or c.get("citations") or 0),
                    "authors": c.get("authors") or [],
                    "abstract": (c.get("abstract") or c.get("summary") or "").strip(),
                    "doi": c.get("doi"),
                    "url": c.get("url") or c.get("link"),
                    "published_date": c.get("published_date"),
                    "related_industries": industries if isinstance(industries, list) else [],
                }
            )
        return out

    @staticmethod
    def _format_paper_entry(idx: int, p: Dict[str, Any]) -> List[str]:
        lines: List[str] = []
        lines.append(f"### {idx}. {p['title']}")
        lines.append("")
        meta_parts = [f"Venue: {p['venue']}", f"Citations: {p['citations']}"]
        if p.get("doi"):
            meta_parts.append(f"DOI: {p['doi']}")
        if p.get("published_date"):
            meta_parts.append(f"Published: {p['published_date']}")
        lines.append(f"- {' | '.join(meta_parts)}")
        if p.get("authors"):
            first = p["authors"][:3]
            if isinstance(first, list):
                authors_str = ", ".join(str(a) for a in first)
                if len(p["authors"]) > 3:
                    authors_str += f" (+{len(p['authors']) - 3})"
                lines.append(f"- Authors: {authors_str}")
        if p.get("related_industries"):
            lines.append(
                f"- Related industries: {', '.join(p['related_industries'])}"
            )
        if p.get("abstract"):
            preview = p["abstract"][:400].replace("\n", " ")
            lines.append("")
            lines.append(f"> {preview}{'...' if len(p['abstract']) > 400 else ''}")
        lines.append("")
        return lines

    def _gemma_summarize_papers(
        self, papers: List[Dict[str, Any]]
    ) -> Optional[str]:
        """调 Gemma 周总结论文。失败返 None（降级）。"""
        # 压缩输入：去掉 doi / url 等对总结无关字段；abstract 截断
        compact: List[Dict[str, Any]] = []
        for p in papers:
            compact.append(
                {
                    "title": p["title"],
                    "venue": p["venue"],
                    "citations": p["citations"],
                    "authors": (p["authors"][:3] if isinstance(p["authors"], list) else []),
                    "abstract": (p["abstract"] or "")[:500],
                    "related_industries": p.get("related_industries") or [],
                }
            )
        payload = json.dumps(compact, ensure_ascii=False)
        try:
            resp = self._call_llm_text(self.paper_prompt, payload, temperature=0.3)
        except DataMissingError as e:
            self._handle_gemma_failure("data", e, "weekly_paper_report")
            return None
        except LLMError as e:
            self._handle_gemma_failure("llm", e, "weekly_paper_report")
            return None
        except ParseError as e:
            self._handle_gemma_failure("parse", e, "weekly_paper_report")
            return None
        self._log_llm_invocation(
            prompt_kind="weekly_paper",
            input_text=payload,
            output_text=resp.text,
            resp=resp,
        )
        return resp.text.strip()

    # ══════════════════════ (c) 跨信号交叉验证 ══════════════════════

    def cross_signal_validation(self, industry_name: str) -> Dict[str, Any]:
        """v1.59 跨信号交叉验证（Phase 1 简化版 = 多数投票 + 置信度）。

        Returns:
            {industry, signals_30d, inferred_direction, confidence, rationale}
        """
        if not isinstance(industry_name, str) or not industry_name.strip():
            raise RuleViolation("industry_name must be non-empty str")

        industry_name = industry_name.strip()
        d = build_industry_dashboard(
            industry_name, self.db, days=CROSS_SIGNAL_WINDOW_DAYS
        )
        total = d["recent_signals_total"]
        dist = d["policy_direction_distribution"]

        if total == 0:
            return {
                "industry": industry_name,
                "signals_30d": 0,
                "inferred_direction": None,
                "confidence": "low",
                "rationale": f"最近 {CROSS_SIGNAL_WINDOW_DAYS} 天无信号",
            }

        # 多数投票：不算 null（未判断的）
        judged = {
            k: v for k, v in dist.items()
            if k not in ("null",) and v > 0
        }

        if not judged:
            return {
                "industry": industry_name,
                "signals_30d": total,
                "inferred_direction": None,
                "confidence": "low",
                "rationale": f"{total} 条信号全部未判断（null）",
            }

        winner, winner_count = max(judged.items(), key=lambda x: x[1])
        non_null_total = sum(judged.values())
        ratio = winner_count / non_null_total if non_null_total > 0 else 0.0

        # 置信度门槛：high ≥ 0.7；medium ≥ 0.5；low 其它
        # 额外约束：若已判断信号 < 3，最多 medium
        if winner_count < 3:
            confidence = "low" if ratio < 0.5 else "medium"
        elif ratio >= 0.7:
            confidence = "high"
        elif ratio >= 0.5:
            confidence = "medium"
        else:
            confidence = "low"

        # rationale：列出所有非零桶
        parts: List[str] = []
        for k in ("supportive", "restrictive", "mixed", "neutral", "null"):
            v = dist.get(k, 0)
            if v > 0:
                parts.append(f"{v} 条 {k}")
        rationale = " + ".join(parts)

        return {
            "industry": industry_name,
            "signals_30d": total,
            "inferred_direction": winner,
            "confidence": confidence,
            "rationale": rationale,
        }

    # ══════════════════════ LLM 调用（v1.48 抽象层） ══════════════════════

    def _call_llm_text(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.3,
    ) -> LLMResponse:
        """薄代理 —— 委托给 self.llm.chat（非 JSON 模式）。
        raises DataMissingError / LLMError / ParseError（与旧 _call_gemma_text 完全对齐）。
        """
        return self.llm.chat(
            system_prompt,
            user_content,
            temperature=temperature,
            response_format="text",
            max_retries=self.max_gemma_retries,
            retry_wait_seconds=self.retry_wait_seconds,
        )

    def _log_llm_invocation(
        self,
        prompt_kind: str,
        input_text: str,
        output_text: str,
        resp: LLMResponse,
    ) -> None:
        """写 llm_invocations，同时填旧字段（tokens_used/cost_cents）与
        v1.48 新字段（input_tokens / output_tokens / cost_usd_cents / latency_ms /
        provider / fallback_used）。失败不上抛。"""
        try:
            input_hash = hashlib.sha256(
                input_text[:500].encode("utf-8")
            ).hexdigest()[:16]
            output_summary = (output_text or "")[:200]
            self.db.write(
                """INSERT INTO llm_invocations
                   (agent_name, prompt_version, model_name, input_hash,
                    output_summary, tokens_used, cost_cents, invoked_at,
                    input_tokens, output_tokens, cost_usd_cents,
                    latency_ms, provider, fallback_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.name,
                    f"direction_judge_{prompt_kind}_{self.prompt_version}",
                    resp.model_name,
                    input_hash,
                    output_summary,
                    int(resp.tokens_used),
                    int(round(resp.cost_usd_cents)),
                    now_utc(),
                    int(resp.input_tokens),
                    int(resp.output_tokens),
                    float(resp.cost_usd_cents),
                    int(resp.latency_ms),
                    resp.provider,
                    resp.fallback_used,
                ),
            )
        except Exception as log_err:
            self.logger.error(
                f"Failed to persist llm_invocations row: {log_err}"
            )

    def _handle_gemma_failure(
        self,
        error_type: str,
        e: Exception,
        context: str,
    ) -> None:
        """降级时调用：log agent_errors + logger warning，不 raise。"""
        self.logger.warning(
            f"{self.name}.{context} gemma degrade ({error_type}): {e}"
        )
        self._log_error(error_type, str(e), context)

    # ══════════════════════ Step 11 推送集成 ══════════════════════

    def _maybe_push_weekly_report(
        self,
        report_type: str,
        *,
        report_text: str,
        saved_path: Optional[Path],
        report_date: str,
        **meta: Any,
    ) -> None:
        """若配置了 push_queue，推送周报到 push_outbox；否则 no-op。

        推送失败不影响主流程：catch 一切异常并 log。
        entity_key 由 PushQueue.push_weekly_report 内部组装（日期 KST），
        同日同 type 重复调用幂等。
        """
        if self.push_queue is None:
            return
        try:
            # 重点：只推 preview（前 2000 字），避免 push 消息过大；
            # 完整报告在 reports/ 目录里可读。
            preview = report_text if len(report_text) <= 2000 else (
                report_text[:2000] + "\n\n... (truncated, see full at saved_path)"
            )
            content: Dict[str, Any] = {
                "report_date": report_date,
                "report_preview": preview,
                "saved_path": str(saved_path) if saved_path else None,
                "producer_agent": self.name,
                "prompt_version": self.prompt_version,
                "model_name": self.model,
                **meta,
            }
            self.push_queue.push_weekly_report(
                report_type=report_type,
                content=content,
                producer=self.name,
            )
        except Exception as e:
            # 推送失败不打断报告流程：记 warning
            self.logger.warning(
                f"{self.name}.weekly_{report_type}_report push failed "
                f"(non-fatal): {type(e).__name__}: {e}"
            )

    # ══════════════════════ 辅助 ══════════════════════

    def _latest_signal_days_ago(self, industry_name: str) -> Optional[int]:
        """查该行业最新信号距今天数。无信号返 None。"""
        row = self.db.query_one(
            """SELECT MAX(timestamp) AS latest_ts
               FROM info_units
               WHERE related_industries LIKE ?""",
            (f"%{industry_name}%",),
        )
        if row is None or row["latest_ts"] is None:
            return None
        try:
            dt = datetime.fromisoformat(row["latest_ts"])
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - dt).days

    def _load_prompt(self, base_name: str) -> str:
        path = PROMPT_DIR / f"{base_name}_{self.prompt_version}.md"
        if not path.exists():
            raise RuleViolation(f"Prompt template not found: {path}")
        return path.read_text(encoding="utf-8")

    def _save_report(self, content: str, filename: str) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / filename
        path.write_text(content, encoding="utf-8")
        return path


# ═══ 模块级辅助 ═══

_DIRECTION_IMPORTANCE_ORDER = {
    "restrictive": 0,   # 政策风险最重要，最先
    "supportive": 1,
    "mixed": 2,
    "neutral": 3,
    None: 4,
    "null": 4,
}


def _direction_importance(direction: Optional[str]) -> int:
    return _DIRECTION_IMPORTANCE_ORDER.get(direction, 4)


def _parse_iso_ts(ts: str) -> Optional[int]:
    """ISO 8601 → unix epoch int（秒）。失败返 None。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None
