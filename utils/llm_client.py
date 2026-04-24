"""
utils/llm_client.py — Scout LLM 抽象层 v1（v1.48 Phase A）

目标：
    "能力需求绑定，不绑定模型" —— 每个 Agent 声明需要的 provider（配置名），
    实际的 LLMClient 子类由 LLMClient.from_config() 按 config.yaml 装配。
    Agent 代码不 import 任何具体的 provider 类。

核心对象：
    - LLMResponse : 统一响应（text / tokens / latency / cost / provider / fallback_used）
    - LLMClient   : 抽象基类，chat() 是唯一对外方法
    - OllamaClient: 本地 Gemma；Phase A 唯一真正实装的子类
    - FallbackClient: 装饰器，primary 的连接/LLM 错降级到 fallback

异常契约（与 agents.base 的错误矩阵对齐）：
    - ParseError       : LLM 返回无法解析（空/格式错）
    - DataMissingError : LLM 连接失败（unreachable / timeout）
    - LLMError         : 其它 LLM 调用错（用尽重试）
    这些正好是 BaseAgent.run_with_error_handling 的 6 类错误中的 parse/data/llm。

Phase A 只实装 ollama；deepseek/anthropic/openai 的骨架留给 Phase B/C。

示例用法：
    llm = LLMClient.from_config("gemma_local")        # 从 config.yaml 读
    resp = llm.chat(system_prompt, user_content,
                    temperature=0.3, response_format="text")
    print(resp.text, resp.input_tokens, resp.output_tokens, resp.latency_ms)
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from agents.base import DataMissingError, LLMError, ParseError


# ═══════════════════ 响应对象 ═══════════════════


@dataclass
class LLMResponse:
    """统一 LLM 响应。所有 provider 的调用返回这个形状。"""

    text: str
    input_tokens: int
    output_tokens: int
    model_name: str
    latency_ms: int
    cost_usd_cents: float
    provider: str                                  # config 里的 provider key
    fallback_used: Optional[str] = None            # 若发生降级，原 primary 名
    raw: Any = field(default=None, repr=False)     # 原始响应对象（调试用，不入库）

    @property
    def tokens_used(self) -> int:
        """向后兼容：等价于 input_tokens + output_tokens。"""
        return (self.input_tokens or 0) + (self.output_tokens or 0)


# ═══════════════════ 抽象基类 + 工厂 ═══════════════════


class LLMClient(ABC):
    """所有 LLM provider 的统一接口。

    子类需要实现 chat()。具体 provider 的 HTTP/SDK 细节封装在子类里，
    Agent 层只看得到 chat() + LLMResponse。
    """

    provider_name: str
    model: str

    @abstractmethod
    def chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        temperature: float = 0.1,
        response_format: str = "text",      # "text" | "json"
        max_retries: int = 2,
        retry_wait_seconds: float = 1.0,
    ) -> LLMResponse:
        """发一次 chat completion，返回统一响应对象。

        Raises:
            ParseError       : 响应 shape 不对 / 空 content / JSON 解析失败
            DataMissingError : 连接失败（经重试后）
            LLMError         : 其它错误（经重试后）
        """
        raise NotImplementedError

    # ─── 工厂 ───

    @classmethod
    def from_config(
        cls,
        provider_name: str,
        *,
        config: Any = None,
        client_override: Any = None,
    ) -> "LLMClient":
        """按 provider_name 从 config.yaml 装配对应客户端。

        Args:
            provider_name: config.yaml 里 llm_providers.* 的 key
                           （如 "gemma_local" / "deepseek_v3" / "claude_sonnet"）
            config: 可传 ScoutConfig 或 dict 用于测试/单元注入；None → 实时加载
            client_override: 底层 SDK 客户端（OllamaClient 的 ollama.Client 对象）的替换，
                             方便测试注入 FakeOllama 而不改 config

        Raises:
            ValueError: provider 不存在 / type 未知
            NotImplementedError: Phase A 以外的 provider type
        """
        providers_cfg = _resolve_providers_config(config)
        if provider_name not in providers_cfg:
            raise ValueError(
                f"Unknown LLM provider {provider_name!r}; "
                f"available: {sorted(providers_cfg.keys())}"
            )

        pcfg = providers_cfg[provider_name]
        ptype = pcfg.get("type")

        if ptype == "ollama":
            primary: LLMClient = OllamaClient(
                provider_name=provider_name,
                model=pcfg["model"],
                endpoint=pcfg.get("endpoint", "http://localhost:11434"),
                timeout=float(pcfg.get("timeout", 30.0)),
                max_tokens=int(pcfg.get("max_tokens", 2048)),
                client_override=client_override,
            )
        elif ptype == "deepseek":
            raise NotImplementedError(
                "DeepSeekClient 未实装（Phase B 引入）"
            )
        elif ptype == "anthropic":
            raise NotImplementedError(
                "AnthropicClient 未实装（骨架保留，未来启用）"
            )
        elif ptype == "openai":
            raise NotImplementedError(
                "OpenAIClient 未实装（骨架保留，未来启用）"
            )
        else:
            raise ValueError(f"Unknown LLM provider type: {ptype!r}")

        fallback_name = pcfg.get("fallback")
        if fallback_name:
            fallback = cls.from_config(fallback_name, config=config)
            return FallbackClient(primary=primary, fallback=fallback)

        return primary


# ═══════════════════ Ollama 实装 ═══════════════════


class OllamaClient(LLMClient):
    """本地 Ollama / Gemma 客户端。

    保持与 direction_judge / direction_backfill / signal_collector 之前
    直接调 ollama.Client 完全一致的行为：
      - messages 是 [system, user] 两条
      - format="json" 仅当 response_format="json"
      - options.temperature 透传
      - 响应从 response["message"]["content"] 取
      - tokens = prompt_eval_count + eval_count
      - 连接错 → DataMissingError；解析错 → ParseError；其它 → LLMError
      - 重试 max_retries 次，固定间隔 retry_wait_seconds
    """

    def __init__(
        self,
        provider_name: str,
        model: str,
        endpoint: str = "http://localhost:11434",
        timeout: float = 30.0,
        max_tokens: int = 2048,
        client_override: Any = None,
    ):
        self.provider_name = provider_name
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.logger = logging.getLogger(f"scout.llm.{provider_name}")

        if client_override is not None:
            self.client = client_override
        else:
            import ollama
            self.client = ollama.Client(host=endpoint, timeout=timeout)

    def chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        temperature: float = 0.1,
        response_format: str = "text",
        max_retries: int = 2,
        retry_wait_seconds: float = 1.0,
    ) -> LLMResponse:
        attempts = max_retries + 1
        last_err: Optional[Exception] = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "options": {"temperature": temperature},
                }
                if response_format == "json":
                    kwargs["format"] = "json"

                response = self.client.chat(**kwargs)
                latency_ms = int((time.monotonic() - started) * 1000)

                content = self._extract_content(response)
                if not isinstance(content, str) or not content.strip():
                    raise ParseError(
                        f"Ollama returned empty content: {content!r}"
                    )
                input_tokens, output_tokens = self._extract_tokens(response)

                return LLMResponse(
                    text=content,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model_name=self.model,
                    latency_ms=latency_ms,
                    cost_usd_cents=0.0,         # 本地 Gemma = 免费
                    provider=self.provider_name,
                    fallback_used=None,
                    raw=response,
                )
            except ParseError:
                raise  # 模型层问题不重试
            except Exception as e:
                last_err = e
                if attempt < attempts - 1:
                    self.logger.warning(
                        f"Ollama attempt {attempt + 1}/{attempts} failed: "
                        f"{type(e).__name__}: {e}"
                    )
                    time.sleep(retry_wait_seconds)
                    continue
                break

        err_msg = f"{type(last_err).__name__}: {last_err}"
        if self._is_connection_error(last_err):
            raise DataMissingError(
                f"Ollama unreachable after {attempts} attempts: {err_msg}"
            )
        raise LLMError(
            f"Ollama call failed after {attempts} attempts: {err_msg}"
        )

    @staticmethod
    def _extract_content(response: Any) -> str:
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
    def _extract_tokens(response: Any) -> Tuple[int, int]:
        if isinstance(response, dict):
            p = int(response.get("prompt_eval_count") or 0)
            e = int(response.get("eval_count") or 0)
        else:
            p = int(getattr(response, "prompt_eval_count", 0) or 0)
            e = int(getattr(response, "eval_count", 0) or 0)
        return p, e

    @staticmethod
    def _is_connection_error(err: Optional[Exception]) -> bool:
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


# ═══════════════════ 降级装饰器 ═══════════════════


class FallbackClient(LLMClient):
    """装饰 primary，失败时切到 fallback。

    触发降级的错误：DataMissingError（连接失败）+ LLMError（调用失败）。
    ParseError 不触发降级（模型层问题降级也无解）。
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient):
        self.primary = primary
        self.fallback = fallback
        self.provider_name = primary.provider_name
        self.model = primary.model
        self.logger = logging.getLogger(
            f"scout.llm.fallback.{primary.provider_name}"
        )

    def chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        temperature: float = 0.1,
        response_format: str = "text",
        max_retries: int = 2,
        retry_wait_seconds: float = 1.0,
    ) -> LLMResponse:
        try:
            return self.primary.chat(
                system_prompt, user_content,
                temperature=temperature,
                response_format=response_format,
                max_retries=max_retries,
                retry_wait_seconds=retry_wait_seconds,
            )
        except (DataMissingError, LLMError) as e:
            self.logger.warning(
                f"primary {self.primary.provider_name} failed, "
                f"falling back to {self.fallback.provider_name}: "
                f"{type(e).__name__}: {e}"
            )
            resp = self.fallback.chat(
                system_prompt, user_content,
                temperature=temperature,
                response_format=response_format,
                max_retries=max_retries,
                retry_wait_seconds=retry_wait_seconds,
            )
            resp.fallback_used = self.primary.provider_name
            return resp


# ═══════════════════ config 解析工具 ═══════════════════


def _resolve_providers_config(config: Any) -> Dict[str, Dict[str, Any]]:
    """把多种形状的 config 输入统一转成 llm_providers dict。

    支持：
      - None                  → 走 config.loader.load_config()
      - ScoutConfig (pydantic) → 读 .llm_providers （pydantic model）
      - dict                  → 假定已经是 llm_providers 或根 config
    """
    if config is None:
        from config.loader import load_config
        config = load_config()

    # Pydantic ScoutConfig
    providers_attr = getattr(config, "llm_providers", None)
    if providers_attr is not None:
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in providers_attr.items():
            out[k] = v.model_dump() if hasattr(v, "model_dump") else dict(v)
        return out

    if isinstance(config, dict):
        if "llm_providers" in config:
            return {k: dict(v) for k, v in config["llm_providers"].items()}
        # 直接把整个 dict 当 llm_providers（测试便利）
        return {k: dict(v) for k, v in config.items()}

    raise ValueError(
        f"Unable to resolve llm_providers from config: {type(config).__name__}"
    )
