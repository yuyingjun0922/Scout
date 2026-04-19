"""
config/loader.py — Scout 配置加载器（v1.57 决策4 的扩展）

职责：
    1) 读取 config.yaml
    2) Pydantic 严格 schema 验证（extra='forbid'，未知字段拒绝）
    3) 环境变量覆盖：敏感信息（ANTHROPIC_API_KEY）不入 YAML
    4) 启动时 fail-fast：缺失/格式错立即报错

环境变量覆盖：
    ANTHROPIC_API_KEY → llm.api_key
    SCOUT_MODE        → mode
"""
import os
from pathlib import Path
from typing import Dict, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_DOTENV_PATH = PROJECT_ROOT / ".env"

PHASE1_REQUIRED_SOURCES = frozenset({"D1", "D4", "V1", "V3", "S4"})


# ═══ Sub-schemas ═══

class LLMConfig(BaseModel):
    """LLM 模型与预算配置"""

    model_config = ConfigDict(extra='forbid')

    local_model: str = Field(..., min_length=1)
    cloud_model: str = Field(..., min_length=1)
    phase1_mode: Literal['gemma_only', 'hybrid', 'cloud_only'] = 'gemma_only'
    api_key_required: bool = False

    # v1.66 USD 预算
    daily_llm_cost_usd_running: float = Field(..., ge=0)
    monthly_llm_cost_usd: float = Field(..., ge=0)
    emergency_stop_single_call_usd: float = Field(..., ge=0)
    emergency_stop_daily_usd: float = Field(..., ge=0)

    # 原 doc KRW 字段（保留兼容，可选）
    daily_cost_limit_krw: Optional[int] = Field(None, ge=0)
    monthly_cost_limit_krw: Optional[int] = Field(None, ge=0)

    # 运行时由 env 注入（非 YAML 字段）
    api_key: Optional[str] = None


class SourceConfig(BaseModel):
    """单个信源配置"""

    model_config = ConfigDict(extra='forbid')

    name: str = Field(..., min_length=1)
    url: Optional[str] = None
    api_url: Optional[str] = None
    frequency_hours: int = Field(..., ge=1)
    credibility: Literal['权威', '可靠', '参考', '线索']


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    knowledge_db: str = Field(..., min_length=1)
    queue_db: str = Field(..., min_length=1)


class ConcurrencyConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    max_workers: int = Field(..., ge=1)
    db_lock_timeout: int = Field(..., ge=1)
    transaction_isolation: Literal['IMMEDIATE', 'DEFERRED', 'EXCLUSIVE'] = 'IMMEDIATE'


class ErrorHandlingConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    max_retries: int = Field(..., ge=0)
    retry_backoff_base: int = Field(..., ge=1)
    alert_webhook: Optional[str] = None


class TestingConfig(BaseModel):
    __test__ = False  # 告诉 pytest 这不是 test 类（类名以 Test 开头会被误收集）

    model_config = ConfigDict(extra='forbid')

    coverage_target: int = Field(..., ge=0, le=100)
    fixture_dir: str = Field(..., min_length=1)


class QQPushConfig(BaseModel):
    """v1.13 Phase 2A — QQ 开放平台 C2C 主动推送配置（可选）。

    所有 secrets 通过 .env 注入（loader.load_config 里执行 env 覆盖）；
    config.yaml 里只放空串占位符，明文不入 YAML/CI。
    运行期缺失 secrets 由 QQPushChannel.__init__ 的 ValueError 兜底，
    保持 YAML schema 宽松（test 可传 env={} 且仍过校验）。
    """

    model_config = ConfigDict(extra='forbid')

    enabled: bool = True
    user_openid: str = Field(default="")             # env: QQ_USER_OPENID
    app_id: str = Field(default="")                  # env: QQ_BOT_APP_ID
    client_secret: str = Field(default="")           # env: QQ_BOT_SECRET
    rate_limit_per_minute: int = Field(default=10, ge=1, le=60)
    max_content_length: int = Field(default=900, ge=10, le=4000)


# ═══ 根 schema ═══

class ScoutConfig(BaseModel):
    """Scout 根配置。Pydantic v2 严格契约。"""

    model_config = ConfigDict(extra='forbid')

    llm: LLMConfig
    sources: Dict[str, SourceConfig]
    database: DatabaseConfig
    concurrency: ConcurrencyConfig
    error_handling: ErrorHandlingConfig
    timezone: str = Field(..., min_length=1)
    testing: TestingConfig
    mode: Literal['cold_start', 'running', 'diagnosis']
    qq_push: Optional[QQPushConfig] = None

    @model_validator(mode='after')
    def _check_phase1_sources(self) -> 'ScoutConfig':
        """Phase 1 必须配齐 D1/D4/V1/V3/S4 这 5 个核心信源"""
        missing = PHASE1_REQUIRED_SOURCES - set(self.sources.keys())
        if missing:
            raise ValueError(
                f"Phase 1 requires sources {sorted(PHASE1_REQUIRED_SOURCES)}, "
                f"missing: {sorted(missing)}"
            )
        return self


# ═══ 加载函数 ═══

def load_config(
    config_path: Optional[Path] = None,
    env: Optional[dict] = None,
) -> ScoutConfig:
    """加载并验证 Scout config.yaml。

    Args:
        config_path: 默认 <project_root>/config.yaml
        env: 环境变量字典。None → 读 os.environ；{} → 不读任何 env（测试用）。

    Raises:
        FileNotFoundError: config_path 不存在
        ValueError:         YAML 为空或非字典
        pydantic.ValidationError: schema 校验失败
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    config_path = Path(config_path)

    # v1.13+ — 自动加载项目根 .env（显式 env={} 时跳过，保持测试确定性）
    if env is None and load_dotenv is not None and DEFAULT_DOTENV_PATH.exists():
        load_dotenv(DEFAULT_DOTENV_PATH, override=False)

    if not config_path.exists():
        raise FileNotFoundError(f"Scout config not found: {config_path}")

    with config_path.open('r', encoding='utf-8') as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Scout config is empty: {config_path}")
    if not isinstance(raw, dict):
        raise ValueError(
            f"Scout config root must be a mapping, got {type(raw).__name__}: {config_path}"
        )

    # 环境变量覆盖
    if env is None:
        env = dict(os.environ)

    api_key = env.get('ANTHROPIC_API_KEY')
    if api_key:
        llm_section = raw.setdefault('llm', {})
        if isinstance(llm_section, dict):
            llm_section['api_key'] = api_key

    scout_mode = env.get('SCOUT_MODE')
    if scout_mode:
        raw['mode'] = scout_mode

    # v1.13+ — QQ secrets 全部走 env 注入（不把明文入 YAML/CI）
    for yaml_key, env_key in (
        ('app_id', 'QQ_BOT_APP_ID'),
        ('client_secret', 'QQ_BOT_SECRET'),
        ('user_openid', 'QQ_USER_OPENID'),
    ):
        val = env.get(env_key)
        if val:
            qq_section = raw.get('qq_push')
            if isinstance(qq_section, dict):
                qq_section[yaml_key] = val

    return ScoutConfig(**raw)
