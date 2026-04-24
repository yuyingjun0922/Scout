"""
agents/direction_backfill_agent.py — v1.10 政策方向回填 Agent

修复架构断层：
    gov_cn / 其它 D 类信源直写 info_units 时 policy_direction=None
    （绕过了 collection_to_knowledge → SignalCollectorAgent 的方向判断 pipeline），
    导致 RecommendationAgent.d1 只能走关键词兜底。

本 Agent 作为定时清扫器：
    1. 扫描 source IN ('D1','V1','V3') 且 policy_direction IS NULL 的记录
    2. 逐条调 Gemma JSON-mode 做 supportive / restrictive / neutral 三分类
    3. UPDATE info_units SET policy_direction=... WHERE id=?
    4. 记 llm_invocations + agent_errors（按需）

降级策略（写时极简）：
    - Ollama 离线 → DataMissingError → 整批降级，未处理的留 NULL（下次重试）
    - Gemma 返非 supportive/restrictive/neutral → 当条 skipped_invalid，留 NULL
    - 单条其它失败 → 走 BaseAgent 错误矩阵，不阻塞批次
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __name__ == "__main__":
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

from agents.base import (
    BaseAgent,
    DataMissingError,
    LLMError,
    ParseError,
)
from infra.db_manager import DatabaseManager
from utils.llm_client import LLMClient, LLMResponse, OllamaClient
from utils.time_utils import now_utc


VALID_DIRECTIONS = {"supportive", "restrictive", "neutral"}
DEFAULT_BATCH_LIMIT = 20
SOURCES_TO_BACKFILL = ("D1", "V1", "V3")
PROMPT_VERSION = "v001"
CONTENT_TRUNCATE = 500   # 给 Gemma 的内容上限（字符）

SYSTEM_PROMPT = """你是政策方向分析助手。给定一条政策/行业文件信息，判断它对所述行业的方向。

只返回 JSON：{"direction": "supportive" | "restrictive" | "neutral"}

判定标准：
- supportive: 鼓励/支持/培育/推动/财政补贴/专项资金 该行业发展
- restrictive: 限制/禁止/清退/整治/严控/淘汰 该行业活动
- neutral: 程序性/管理规范/无明显倾向 / 与行业无强相关

JSON 之外不要写任何文字。"""


class DirectionBackfillAgent(BaseAgent):
    """v1.10 用 Gemma 回填 info_units.policy_direction 的 NULL 值。"""

    def __init__(
        self,
        db: DatabaseManager,
        ollama_host: str = "http://localhost:11434",
        model: str = "gemma4:e4b",
        timeout: float = 30.0,
        max_gemma_retries: int = 1,
        retry_wait_seconds: float = 1.0,
        ollama_client: Any = None,
        llm_client: Optional[LLMClient] = None,
        prompt_version: str = PROMPT_VERSION,
    ):
        super().__init__(name="direction_backfill", db=db)
        self.ollama_host = ollama_host
        self.model = model
        self.timeout = timeout
        self.max_gemma_retries = max_gemma_retries
        self.retry_wait_seconds = retry_wait_seconds
        self.prompt_version = prompt_version

        # v1.48 LLM 抽象层：3 路注入（见 direction_judge.__init__ 注释）
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

    # ── 主入口 ──

    def run(self, limit: int = DEFAULT_BATCH_LIMIT) -> Optional[dict]:
        """批处理入口。返回 {scanned, succeeded, failed, skipped_invalid, per_direction}。"""
        return self.run_with_error_handling(self._run_batch, limit)

    def _run_batch(self, limit: int) -> dict:
        rows = self._load_pending(limit)
        self.logger.info(
            f"DirectionBackfillAgent: {len(rows)} NULL rows to process "
            f"(limit={limit})"
        )

        succeeded = 0
        failed = 0
        skipped_invalid = 0
        per_direction = {"supportive": 0, "restrictive": 0, "neutral": 0}

        for r in rows:
            outcome = self.run_with_error_handling(
                self._process_one, r["id"], r["content"]
            )
            if outcome is None:
                # BaseAgent 已记 agent_errors；本条留 NULL，下次再试
                failed += 1
                continue
            direction, valid = outcome
            if not valid:
                skipped_invalid += 1
                continue
            try:
                self._update_direction(r["id"], direction)
                succeeded += 1
                per_direction[direction] = per_direction.get(direction, 0) + 1
            except sqlite3.Error as e:
                self.logger.warning(
                    f"_update_direction DB error for {r['id']}: {e}"
                )
                self._log_error(
                    "data", f"sqlite3.Error: {e}", "_update_direction"
                )
                failed += 1

        result = {
            "scanned": len(rows),
            "succeeded": succeeded,
            "failed": failed,
            "skipped_invalid": skipped_invalid,
            "per_direction": per_direction,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.logger.info(f"DirectionBackfillAgent done: {result}")
        return result

    # ── 加载待回填 ──

    def _load_pending(self, limit: int) -> List[Any]:
        try:
            placeholders = ",".join("?" * len(SOURCES_TO_BACKFILL))
            rows = self.db.query(
                f"""SELECT id, content
                    FROM info_units
                    WHERE policy_direction IS NULL
                      AND source IN ({placeholders})
                    ORDER BY timestamp DESC
                    LIMIT ?""",
                (*SOURCES_TO_BACKFILL, int(limit)),
            )
            return rows
        except sqlite3.Error as e:
            self.logger.error(f"_load_pending DB error: {e}")
            return []

    # ── 单条处理 ──

    def _process_one(
        self, unit_id: str, content: str
    ) -> Tuple[str, bool]:
        """返回 (direction, valid)。
        valid=False → Gemma 给了非法值，调用方跳过更新。
        """
        title, body = self._extract_title_body(content)
        user_prompt = self._build_user_prompt(title, body)

        resp = self._call_llm_json(user_prompt)
        try:
            parsed = json.loads(resp.text)
        except json.JSONDecodeError as je:
            raise ParseError(
                f"Gemma returned non-JSON: {resp.text[:300]!r}"
            ) from je
        if not isinstance(parsed, dict):
            raise ParseError(
                f"Gemma JSON must be object, got {type(parsed).__name__}: "
                f"{resp.text[:300]!r}"
            )

        direction = (parsed.get("direction") or "").strip().lower()

        self._log_llm_invocation(
            input_text=user_prompt,
            output=direction or "<empty>",
            resp=resp,
        )

        if direction not in VALID_DIRECTIONS:
            self.logger.warning(
                f"Gemma returned invalid direction {direction!r} for {unit_id}"
            )
            return ("", False)
        return (direction, True)

    @staticmethod
    def _extract_title_body(content: str) -> Tuple[str, str]:
        """gov_cn 写入的 content 是 JSON 字符串，含 title + summary 字段。
        其它 source 直接当文本处理。"""
        if not content:
            return ("", "")
        try:
            obj = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return ("", content[:CONTENT_TRUNCATE])
        if not isinstance(obj, dict):
            return ("", content[:CONTENT_TRUNCATE])
        title = str(obj.get("title") or "")
        summary = str(
            obj.get("summary") or obj.get("content") or obj.get("body") or ""
        )
        return (title, summary[:CONTENT_TRUNCATE])

    @staticmethod
    def _build_user_prompt(title: str, body: str) -> str:
        return (
            f"标题：{title}\n\n"
            f"内容：{body}\n\n"
            "请判断方向。"
        )

    # ── LLM 调用（v1.48 抽象层） ──

    def _call_llm_json(self, user_content: str) -> LLMResponse:
        """薄代理 —— 委托给 self.llm.chat（JSON 模式）。
        raises DataMissingError / LLMError / ParseError（与旧 _call_gemma 完全对齐）。
        """
        return self.llm.chat(
            SYSTEM_PROMPT,
            user_content,
            temperature=0.1,
            response_format="json",
            max_retries=self.max_gemma_retries,
            retry_wait_seconds=self.retry_wait_seconds,
        )

    # ── 持久化 ──

    def _update_direction(self, unit_id: str, direction: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self.db.write(
            """UPDATE info_units
               SET policy_direction = ?, updated_at = ?
               WHERE id = ?""",
            (direction, ts, unit_id),
        )

    def _log_llm_invocation(
        self,
        input_text: str,
        output: str,
        resp: LLMResponse,
    ) -> None:
        """v1.48 —— 同时填旧字段与新字段（见 direction_judge._log_llm_invocation）。"""
        try:
            input_hash = hashlib.sha256(
                input_text.encode("utf-8")
            ).hexdigest()[:32]
            self.db.write(
                """INSERT INTO llm_invocations
                   (agent_name, prompt_version, model_name, input_hash,
                    output_summary, tokens_used, cost_cents, invoked_at,
                    input_tokens, output_tokens, cost_usd_cents,
                    latency_ms, provider, fallback_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.name,
                    self.prompt_version,
                    resp.model_name,
                    input_hash,
                    output[:200],
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
        except sqlite3.Error as e:
            self.logger.warning(f"_log_llm_invocation DB error: {e}")


if __name__ == "__main__":
    db_path = "data/knowledge.db"
    agent = DirectionBackfillAgent(DatabaseManager(db_path))
    result = agent.run(limit=DEFAULT_BATCH_LIMIT)
    print(json.dumps(result, ensure_ascii=False, indent=2) if result else "run returned None")
