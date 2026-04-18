"""
agents/signal_collector.py — v1.66 Signal Collector Agent (Phase 1 Step 7)

从原始文本（政策 / 论文 / 指标 / 海关 / 公告）抽取结构化信号 → InfoUnitV1。

核心流程（v1.59 / v1.60 规则）：

    Step 1 — 规则层预检（rules precheck）
        RESTRICTIVE_HARD      : 禁止/取缔/不得/限制/整改/严禁 → 硬约束
        SUPPORTIVE_CANDIDATES : 支持/鼓励/加快/推进/大力发展 → 参考
        MULTI_INTERPRETATION  : 进口/出口/价格/产能/库存       → 触发 null

    Step 2 — Gemma LLM 抽取
        本地 Ollama (http://localhost:11434)，模型 gemma4:e4b
        format='json' 强约束；timeout 30s；2 次重试

    Step 3 — 组合决策
        规则硬覆盖 → 低置信度置 null → 多解读置 null → mixed 兜底 subtype

    Step 4 — 生成 InfoUnitV1（pydantic 严格校验）

错误映射（BaseAgent 错误矩阵）：
    - Ollama 不可达（连不上 / 超时）    → DataMissingError
    - Gemma 返回非 JSON / 非对象        → ParseError
    - 其它 LLM 异常（速率限制 / 异常响应）→ LLMError
    - 入参非法（空文本 / source 未知）  → RuleViolation

llm_invocations 表记录每次 Gemma 调用（agent_name / prompt_version / model_name
/ input_hash / tokens_used / cost_cents=0）。记账失败不上抛，只 logger 记录。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.base import (
    BaseAgent,
    DataMissingError,
    LLMError,
    ParseError,
    RuleViolation,
)
from contracts.contracts import PHASE1_SOURCES, InfoUnitV1
from infra.db_manager import DatabaseManager
from utils.hash_utils import info_unit_id
from utils.time_utils import now_utc


PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Credibility 映射（对应 config.yaml 的 sources.*.credibility）
CREDIBILITY_MAP: Dict[str, str] = {
    "D1": "权威",   # 国务院
    "D4": "参考",   # Semantic Scholar / arXiv
    "V1": "权威",   # 国家统计局
    "V3": "权威",   # 韩国关税厅
    "S4": "权威",   # AkShare
}

# v1.59 规则关键词（中文字面量）
RESTRICTIVE_HARD: Tuple[str, ...] = (
    "禁止", "取缔", "不得", "限制", "整改", "严禁",
)
SUPPORTIVE_CANDIDATES: Tuple[str, ...] = (
    "支持", "鼓励", "加快", "推进", "大力发展",
)
MULTI_INTERPRETATION: Tuple[str, ...] = (
    "进口", "出口", "价格", "产能", "库存",
)

CONFIDENCE_NULL_THRESHOLD: float = 0.7
MULTI_INTERP_TRUST_THRESHOLD: float = 0.85

_VALID_DIRECTIONS = {"supportive", "restrictive", "neutral", "mixed"}
_VALID_MIXED_SUBTYPES = {"conflict", "structural", "stage_difference"}


class SignalCollectorAgent(BaseAgent):
    """信号采集 Agent — 规则优先 + Gemma 辅助的结构化抽取器。"""

    def __init__(
        self,
        db: DatabaseManager,
        ollama_host: str = "http://localhost:11434",
        model: str = "gemma4:e4b",
        prompt_version: str = "v001",
        timeout: float = 30.0,
        max_gemma_retries: int = 2,
        retry_wait_seconds: float = 1.0,
        ollama_client: Any = None,
    ):
        super().__init__(name="signal_collector", db=db)
        self.ollama_host = ollama_host
        self.model = model
        self.prompt_version = prompt_version
        self.timeout = timeout
        self.max_gemma_retries = max_gemma_retries
        self.retry_wait_seconds = retry_wait_seconds

        # 允许测试注入 mock client；否则 lazy import ollama
        if ollama_client is not None:
            self.client = ollama_client
        else:
            import ollama
            self.client = ollama.Client(host=ollama_host, timeout=timeout)

        # 启动时加载 prompt：缺失立即报错，不等到首次调用
        self.prompt_template = self._load_prompt()

    # ══════════════════════ 入口 ══════════════════════

    def run(
        self,
        raw_text: str,
        source: str,
        title: str,
        published_date: str,
        raw_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[InfoUnitV1]:
        """处理一条原始文本 → InfoUnitV1。已处理错误返 None。"""
        return self.run_with_error_handling(
            self._process,
            raw_text, source, title, published_date, raw_metadata,
        )

    def process(
        self,
        raw_text: str,
        source: str,
        title: str,
        published_date: str,
        raw_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[InfoUnitV1]:
        """公共别名，等价于 run。"""
        return self.run(raw_text, source, title, published_date, raw_metadata)

    def _process(
        self,
        raw_text: str,
        source: str,
        title: str,
        published_date: str,
        raw_metadata: Optional[Dict[str, Any]],
    ) -> InfoUnitV1:
        self._validate_inputs(raw_text, source, title, published_date)

        rule_signals = self._rules_precheck(raw_text)

        gemma_out, tokens = self._call_gemma(raw_text)

        self._log_llm_invocation(raw_text, gemma_out, tokens)

        final = self._combine(rule_signals, gemma_out)

        return self._build_info_unit(
            final, raw_text, source, title, published_date, raw_metadata
        )

    # ══════════════════════ Step 1: 规则预检 ══════════════════════

    @staticmethod
    def _rules_precheck(text: str) -> Dict[str, bool]:
        return {
            "has_restrictive_hard": any(k in text for k in RESTRICTIVE_HARD),
            "has_supportive_candidate": any(k in text for k in SUPPORTIVE_CANDIDATES),
            "has_multi_interpretation": any(k in text for k in MULTI_INTERPRETATION),
        }

    # ══════════════════════ Step 2: Gemma 调用 ══════════════════════

    def _call_gemma(self, raw_text: str) -> Tuple[Dict[str, Any], int]:
        """Ollama 推理 + 内部重试。返回 (parsed_dict, tokens_used)。"""
        attempts = self.max_gemma_retries + 1
        last_err: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                response = self.client.chat(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.prompt_template},
                        {"role": "user", "content": raw_text},
                    ],
                    format="json",
                    options={"temperature": 0.2},
                )
                content = self._extract_content(response)
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError as je:
                    raise ParseError(
                        f"Gemma returned non-JSON: {content[:300]!r}"
                    ) from je
                if not isinstance(parsed, dict):
                    raise ParseError(
                        f"Gemma JSON must be object, got "
                        f"{type(parsed).__name__}: {content[:300]!r}"
                    )
                tokens = self._extract_tokens(response)
                return parsed, tokens
            except ParseError:
                # JSON 层错不重试（模型行为问题，重试无助益）
                raise
            except Exception as e:
                last_err = e
                if attempt < attempts - 1:
                    self.logger.warning(
                        f"Gemma call attempt {attempt + 1}/{attempts} "
                        f"failed: {type(e).__name__}: {e}"
                    )
                    time.sleep(self.retry_wait_seconds)
                    continue
                break

        err_msg = f"{type(last_err).__name__}: {last_err}"
        if self._is_connection_error(last_err):
            raise DataMissingError(
                f"Gemma unreachable after {attempts} attempts: {err_msg}"
            )
        raise LLMError(
            f"Gemma call failed after {attempts} attempts: {err_msg}"
        )

    @staticmethod
    def _extract_content(response: Any) -> str:
        """兼容 ollama 库 dict / pydantic 两种 response 结构。"""
        if isinstance(response, dict):
            msg = response.get("message") or {}
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    return content
        msg = getattr(response, "message", None)
        if msg is not None:
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                return content
        raise ParseError(
            f"Ollama response missing .message.content: {str(response)[:300]!r}"
        )

    @staticmethod
    def _extract_tokens(response: Any) -> int:
        if isinstance(response, dict):
            p = int(response.get("prompt_eval_count") or 0)
            e = int(response.get("eval_count") or 0)
        else:
            p = int(getattr(response, "prompt_eval_count", 0) or 0)
            e = int(getattr(response, "eval_count", 0) or 0)
        return p + e

    @staticmethod
    def _is_connection_error(err: Optional[Exception]) -> bool:
        """连接/超时类错 → DataMissingError；其它 → LLMError。"""
        if err is None:
            return False
        if isinstance(err, (ConnectionError, TimeoutError)):
            return True
        low = str(err).lower()
        for kw in (
            "connect", "refused", "unreachable",
            "timed out", "timeout", "econnrefused",
        ):
            if kw in low:
                return True
        return False

    # ══════════════════════ Step 3: 组合决策 ══════════════════════

    def _combine(
        self,
        rule_signals: Dict[str, bool],
        gemma_out: Dict[str, Any],
    ) -> Dict[str, Any]:
        """v1.59 组合：规则硬覆盖 → 低置信 null → 多解读 null → mixed 补 subtype。"""

        direction = self._coerce_direction(gemma_out.get("policy_direction"))
        confidence = self._coerce_confidence(gemma_out.get("confidence"))
        mixed_subtype = self._coerce_mixed_subtype(gemma_out.get("mixed_subtype"))
        category = str(gemma_out.get("category") or "").strip() or "未分类"
        industries = self._coerce_industries(gemma_out.get("related_industries"))
        summary = str(gemma_out.get("summary") or "").strip()[:500]
        reasoning = str(gemma_out.get("reasoning") or "").strip()[:500]
        rules_override = False

        # ── 硬覆盖：RESTRICTIVE_HARD 命中 → restrictive ──
        if rule_signals["has_restrictive_hard"]:
            original = direction
            direction = "restrictive"
            mixed_subtype = None
            confidence = 1.0
            rules_override = True
            tag = "[rules:hard]"
            if original not in (None, "restrictive"):
                tag += f" (overrode gemma={original})"
            reasoning = f"{tag} {reasoning}".strip()

        # ── 低置信 → null ──
        elif direction is not None and confidence < CONFIDENCE_NULL_THRESHOLD:
            direction = None
            mixed_subtype = None

        # ── 多解读 + 非高置信 → null ──
        elif (
            rule_signals["has_multi_interpretation"]
            and direction not in (None, "mixed")
            and confidence < MULTI_INTERP_TRUST_THRESHOLD
        ):
            direction = None
            mixed_subtype = None

        # ── mixed 必须带 subtype（Phase 1 默认 conflict）──
        if direction == "mixed" and mixed_subtype is None:
            mixed_subtype = "conflict"

        # ── 非 mixed 时 subtype 强制 None（契约硬约束）──
        if direction != "mixed":
            mixed_subtype = None

        return {
            "policy_direction": direction,
            "mixed_subtype": mixed_subtype,
            "confidence": confidence,
            "category": category,
            "related_industries": industries,
            "summary": summary,
            "reasoning": reasoning,
            "rules_override": rules_override,
        }

    # ── 类型清洗 ──

    @staticmethod
    def _coerce_direction(v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            vs = v.strip().lower()
            if vs in ("", "null", "none"):
                return None
            if vs in _VALID_DIRECTIONS:
                return vs
        return None  # 非法值 → null（保守）

    @staticmethod
    def _coerce_confidence(v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if f != f:  # NaN
            return 0.0
        if f < 0.0:
            return 0.0
        if f > 1.0:
            return 1.0
        return f

    @staticmethod
    def _coerce_mixed_subtype(v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            vs = v.strip().lower()
            if vs in ("", "null", "none"):
                return None
            if vs in _VALID_MIXED_SUBTYPES:
                return vs
        return None

    @staticmethod
    def _coerce_industries(v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        out: List[str] = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out[:10]  # 防爆

    # ══════════════════════ Step 4: InfoUnitV1 ══════════════════════

    def _build_info_unit(
        self,
        final: Dict[str, Any],
        raw_text: str,
        source: str,
        title: str,
        published_date: str,
        raw_metadata: Optional[Dict[str, Any]],
    ) -> InfoUnitV1:
        content_blob: Dict[str, Any] = {
            "title": title,
            "raw_text_excerpt": raw_text[:500],
            "summary": final["summary"],
            "reasoning": final["reasoning"],
            "category_detail": final["category"],
            "rules_override": final["rules_override"],
            "raw_confidence": final["confidence"],
            "prompt_version": self.prompt_version,
            "model_name": self.model,
        }
        if raw_metadata:
            content_blob["raw_metadata"] = raw_metadata

        return InfoUnitV1(
            id=info_unit_id(source, title, published_date),
            source=source,
            source_credibility=CREDIBILITY_MAP[source],
            timestamp=now_utc(),
            category=final["category"],
            content=json.dumps(content_blob, ensure_ascii=False),
            related_industries=final["related_industries"],
            policy_direction=final["policy_direction"],
            mixed_subtype=final["mixed_subtype"],
            event_chain_id=None,
            schema_version=1,
        )

    # ══════════════════════ 辅助 ══════════════════════

    def _load_prompt(self) -> str:
        path = PROMPT_DIR / f"signal_collector_{self.prompt_version}.md"
        if not path.exists():
            raise RuleViolation(f"Prompt template not found: {path}")
        return path.read_text(encoding="utf-8")

    def _validate_inputs(
        self,
        raw_text: str,
        source: str,
        title: str,
        published_date: str,
    ) -> None:
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise RuleViolation("raw_text must be non-empty str")
        if source not in PHASE1_SOURCES:
            raise RuleViolation(
                f"source must be one of {PHASE1_SOURCES}, got {source!r}"
            )
        if not isinstance(title, str) or not title.strip():
            raise RuleViolation("title must be non-empty str")
        if not isinstance(published_date, str) or not published_date.strip():
            raise RuleViolation("published_date must be non-empty str")

    def _log_llm_invocation(
        self,
        raw_text: str,
        output: Dict[str, Any],
        tokens: int,
    ) -> None:
        """写 llm_invocations 表。失败不上抛（记账不应遮盖主流程）。"""
        try:
            input_hash = hashlib.sha256(
                raw_text[:500].encode("utf-8")
            ).hexdigest()[:16]
            output_summary = str(output.get("summary") or "")[:200]
            self.db.write(
                """INSERT INTO llm_invocations
                   (agent_name, prompt_version, model_name, input_hash,
                    output_summary, tokens_used, cost_cents, invoked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.name,
                    f"signal_collector_{self.prompt_version}",
                    self.model,
                    input_hash,
                    output_summary,
                    int(tokens),
                    0,  # Gemma local → free
                    now_utc(),
                ),
            )
        except Exception as log_err:
            self.logger.error(
                f"Failed to persist llm_invocations row: {log_err}"
            )
