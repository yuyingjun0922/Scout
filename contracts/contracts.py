"""
contracts/contracts.py — Agent间数据传递契约（v1.57决策4）

Pydantic v2严格契约。所有Agent输入输出走契约验证，防止字段漂移/
类型不一致导致下游崩溃。Schema演进时新增V2契约，不改V1。

契约列表：
    - InfoUnitV1     : 信号采集Agent输出（Gemma本地推理）
    - WatchlistUpdateV1: 方向判断Agent更新watchlist（Phase 1简化版）
    - AgentError     : 统一错误记录（对应agent_errors表）
"""
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ═══ 常量 ═══

PHASE1_SOURCES = ('D1', 'D4', 'V1', 'V3', 'S4')
"""Phase 1的5个核心信源：国务院/Semantic Scholar/国家统计局/韩国关税厅/AkShare"""


# ═══ 共享验证工具 ═══

def _validate_utc_iso8601(v: str, field_name: str) -> str:
    """字符串必须是UTC ISO 8601（offset=0 或 Z后缀）"""
    if not isinstance(v, str):
        raise ValueError(f"{field_name} must be str, got {type(v).__name__}")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError as e:
        raise ValueError(f"{field_name} must be ISO 8601, got {v!r}: {e}") from e
    if dt.tzinfo is None:
        raise ValueError(
            f"{field_name} must include timezone (UTC), got naive datetime: {v!r}"
        )
    if dt.utcoffset() != timedelta(0):
        raise ValueError(
            f"{field_name} must be UTC (offset=0), got offset={dt.utcoffset()}: {v!r}"
        )
    return v


def _validate_hex16(v: str, field_name: str) -> str:
    """字符串必须是16位hex（对应hash_utils.info_unit_id输出）"""
    if not isinstance(v, str):
        raise ValueError(f"{field_name} must be str, got {type(v).__name__}")
    if len(v) != 16:
        raise ValueError(f"{field_name} must be 16 chars, got {len(v)}: {v!r}")
    try:
        int(v, 16)
    except ValueError:
        raise ValueError(f"{field_name} must be hex [0-9a-fA-F], got {v!r}")
    return v.lower()


# ═══ InfoUnitV1: 信号采集Agent输出 ═══

class InfoUnitV1(BaseModel):
    """信号采集Agent输出 — schema版本1

    v1.59/v1.60 规则：
      - 单信号明确时才填policy_direction，多解读填None
      - policy_direction='mixed' 时mixed_subtype必填
      - 非mixed时mixed_subtype必须为None（防误填）
    """

    model_config = ConfigDict(
        extra='forbid',                 # 未知字段拒绝，让schema漂移可见
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    id: str = Field(..., description="hash(source+标题+发布日期)[:16] hex")
    source: Literal['D1', 'D4', 'V1', 'V3', 'S4'] = Field(
        ..., description="Phase 1的5个核心信源"
    )
    source_credibility: Literal['权威', '可靠', '参考', '线索']
    timestamp: str = Field(..., description="UTC ISO 8601")
    category: str
    content: str
    related_industries: List[str] = Field(default_factory=list)

    # v1.59
    policy_direction: Optional[
        Literal['supportive', 'restrictive', 'neutral', 'mixed']
    ] = None

    # v1.60 — mixed时必填
    mixed_subtype: Optional[
        Literal['conflict', 'structural', 'stage_difference']
    ] = None

    # v1.59 事件串关联（Phase 1允许None）
    event_chain_id: Optional[str] = None

    schema_version: Literal[1] = 1

    # ── 字段验证器 ──

    @field_validator('id')
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_hex16(v, 'id')

    @field_validator('timestamp')
    @classmethod
    def _check_timestamp(cls, v: str) -> str:
        return _validate_utc_iso8601(v, 'timestamp')

    # ── 跨字段验证器 ──

    @model_validator(mode='after')
    def _check_mixed_subtype(self) -> 'InfoUnitV1':
        """v1.60：mixed与mixed_subtype双向一致"""
        if self.policy_direction == 'mixed' and self.mixed_subtype is None:
            raise ValueError(
                "mixed_subtype is required when policy_direction='mixed' "
                "(v1.60: conflict/structural/stage_difference)"
            )
        if self.policy_direction != 'mixed' and self.mixed_subtype is not None:
            raise ValueError(
                f"mixed_subtype must be None when policy_direction="
                f"{self.policy_direction!r}, got {self.mixed_subtype!r}"
            )
        return self


# ═══ WatchlistUpdateV1: 方向判断Agent输出 ═══

class WatchlistUpdateV1(BaseModel):
    """方向判断Agent更新watchlist（Phase 1简化版）

    Phase 2B将启用motivation_detail/motivation_uncertainty完整评估。
    Phase 1只填dimensions和verification_status就够。
    """

    model_config = ConfigDict(
        extra='forbid',
        validate_assignment=True,
    )

    industry_id: int = Field(..., ge=1, description="watchlist.industry_id")
    dimensions: Optional[int] = Field(None, ge=0, le=6)
    verification_status: Optional[
        Literal['positive', 'early_positive', 'neutral', 'negative', 'insufficient']
    ] = None

    # Phase 2B才填
    motivation_detail: Optional[dict] = None
    motivation_uncertainty: Optional[Literal['low', 'medium', 'high']] = None

    schema_version: Literal[1] = 1


# ═══ AgentError: 统一错误记录 ═══

class AgentError(BaseModel):
    """Agent错误传播矩阵记录（v1.57决策5），对应agent_errors表。"""

    model_config = ConfigDict(
        extra='forbid',
        str_strip_whitespace=True,
    )

    agent_name: str = Field(..., min_length=1)
    error_type: Literal['network', 'parse', 'llm', 'rule', 'data', 'unknown']
    error_message: str = Field(..., min_length=1)
    context_data: Optional[dict] = None
    occurred_at: str = Field(..., description="UTC ISO 8601")

    @field_validator('occurred_at')
    @classmethod
    def _check_occurred_at(cls, v: str) -> str:
        return _validate_utc_iso8601(v, 'occurred_at')
