"""
agents/base.py — Scout Agent 基类（v1.57 决策5 错误传播矩阵）

所有 Agent 继承 BaseAgent。错误分 6 类：
    - network : 网络类错，指数退避重试 MAX_RETRIES 次（默认 3，间隔 1/2/4s）
    - parse   : 数据解析错，保留 raw 在 error_message 中；不重试
    - llm     : LLM 调用错，记录，返 None（调用方可降级规则）
    - rule    : 业务规则违反，记录，返 None
    - data    : 必要数据缺失，记录，返 None（Agent 层标 insufficient）
    - unknown : 非预期错，记录 + 告警 + re-raise（fail-loud / 异常可见性）

所有 6 类统一写入 agent_errors 表。日志写入失败不遮盖原错误。
"""
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from contracts.contracts import AgentError
from infra.db_manager import DatabaseManager
from utils.time_utils import now_utc


# ═══ Scout 异常类 ═══

class ScoutError(Exception):
    """Scout agent 错误基类"""


class NetworkError(ScoutError):
    """网络错（timeout / connection refused / DNS / 5xx / 流中断）"""


class ParseError(ScoutError):
    """数据解析错（malformed JSON/HTML/XML/RSS）。raw 应放 error_message 中保留。"""


class LLMError(ScoutError):
    """LLM 调用错（rate limit / bad response / context overflow / JSON 解码失败）"""


class RuleViolation(ScoutError):
    """业务规则违反（硬约束被违反）"""


class DataMissingError(ScoutError):
    """必要数据缺失（下游标 insufficient）"""


# ═══ BaseAgent ═══

class BaseAgent(ABC):
    """Scout Agent 基类。

    子类必须：
        - 实现 run() 方法（主入口）
        - 通过 run_with_error_handling() 包装所有可能抛错的调用

    Attributes:
        MAX_RETRIES:         network 错误的重试次数（默认 3）
        RETRY_BACKOFF_BASE:  指数退避底数，第 i 次重试等 BASE^i 秒
    """

    MAX_RETRIES: int = 3
    RETRY_BACKOFF_BASE: int = 2

    def __init__(self, name: str, db: DatabaseManager):
        if not name:
            raise ValueError("Agent name must be non-empty")
        if db is None:
            raise ValueError("Agent db (DatabaseManager) is required for error logging")
        self.name = name
        self.db = db
        self.logger = logging.getLogger(f"scout.agent.{name}")

    @abstractmethod
    def run(self, *args, **kwargs):
        """Agent 主入口。子类必须实现。"""
        raise NotImplementedError

    # ── 统一错误处理入口 ──

    def run_with_error_handling(
        self,
        func: Callable[..., Any],
        *args,
        **kwargs,
    ) -> Optional[Any]:
        """统一错误处理入口（v1.57 决策5 错误传播矩阵）。

        Args:
            func: 要执行的可调用对象
            *args, **kwargs: 透传给 func

        Returns:
            func 返回值 / 已处理错误返 None / unknown 错 re-raise
        """
        try:
            return func(*args, **kwargs)
        except NetworkError as e:
            return self._handle_network_error(e, func, args, kwargs)
        except (ConnectionError, TimeoutError) as e:
            # 标准库网络错归入 network 类别
            return self._handle_network_error(
                NetworkError(f"{type(e).__name__}: {e}"), func, args, kwargs
            )
        except ParseError as e:
            return self._handle_handled('parse', e, func.__name__)
        except LLMError as e:
            return self._handle_handled('llm', e, func.__name__)
        except RuleViolation as e:
            return self._handle_handled('rule', e, func.__name__)
        except DataMissingError as e:
            return self._handle_handled('data', e, func.__name__)
        except Exception as e:
            # 未知错：记录 + 告警 + re-raise
            self._log_error('unknown', f"{type(e).__name__}: {e}", func.__name__)
            self._send_alert(func.__name__, e)
            raise

    # ── 分类处理 ──

    def _handle_network_error(
        self,
        e: Exception,
        func: Callable,
        args: tuple,
        kwargs: dict,
    ) -> Optional[Any]:
        """网络错：指数退避重试 MAX_RETRIES 次"""
        last_error: Exception = e
        for i in range(self.MAX_RETRIES):
            wait = self.RETRY_BACKOFF_BASE ** i  # 1, 2, 4 (默认)
            self.logger.warning(
                f"{self.name}.{func.__name__} network error, "
                f"retry {i + 1}/{self.MAX_RETRIES} after {wait}s: {last_error}"
            )
            time.sleep(wait)
            try:
                return func(*args, **kwargs)
            except (NetworkError, ConnectionError, TimeoutError) as retry_err:
                last_error = retry_err
                continue
            # 重试时若抛出其它类别错误 → 按原分支处理
            except ParseError as pe:
                return self._handle_handled('parse', pe, func.__name__)
            except LLMError as le:
                return self._handle_handled('llm', le, func.__name__)
            except RuleViolation as rv:
                return self._handle_handled('rule', rv, func.__name__)
            except DataMissingError as de:
                return self._handle_handled('data', de, func.__name__)
        # MAX_RETRIES 用尽
        self._log_error('network', str(last_error), func.__name__)
        return None

    def _handle_handled(self, error_type: str, e: Exception, context: str) -> None:
        """parse / llm / rule / data 共享的处理路径：记录后返 None"""
        self.logger.warning(
            f"{self.name}.{context} {error_type} error: {e}"
        )
        self._log_error(error_type, str(e), context)
        return None

    # ── 落库 + 告警 ──

    def _log_error(self, error_type: str, message: str, context: str) -> None:
        """写入 agent_errors 表。先走 AgentError 契约校验再持久化。"""
        try:
            err = AgentError(
                agent_name=self.name,
                error_type=error_type,
                error_message=message or f"<empty-{error_type}>",
                context_data={'func': context},
                occurred_at=now_utc(),
            )
            self.db.write(
                """INSERT INTO agent_errors
                   (agent_name, error_type, error_message, context_data, occurred_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    err.agent_name,
                    err.error_type,
                    err.error_message,
                    json.dumps(err.context_data, ensure_ascii=False),
                    err.occurred_at,
                ),
            )
        except Exception as log_err:
            # 日志错不能遮盖原错：本地 logger 记一下就算
            self.logger.error(
                f"Failed to persist agent_errors row: {log_err}"
            )

    def _send_alert(self, context: str, error: Exception) -> None:
        """告警通道。Phase 1 = logger.error；Phase 2A 对接推送队列。"""
        self.logger.error(
            f"[ALERT] {self.name}.{context} failed: "
            f"{type(error).__name__}: {error}"
        )
