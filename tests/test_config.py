"""
tests/test_config.py — Step 4 config.yaml + loader 测试

覆盖：
    - config.yaml 能加载
    - Pydantic schema 验证（正反两面）
    - 环境变量覆盖
    - 缺失必填字段 fail-fast
    - Phase 1 phase1_mode / api_key_required / 5个信源齐全
"""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config.loader import (
    DEFAULT_CONFIG_PATH,
    PHASE1_REQUIRED_SOURCES,
    ConcurrencyConfig,
    DatabaseConfig,
    ErrorHandlingConfig,
    LLMConfig,
    ScoutConfig,
    SourceConfig,
    TestingConfig,
    load_config,
)


def _base_dict() -> dict:
    """生成最小合法 config dict（测试变异用）"""
    return {
        'llm': {
            'local_model': 'gemma4:e4b',
            'cloud_model': 'claude-sonnet-4-6',
            'phase1_mode': 'gemma_only',
            'api_key_required': False,
            'daily_llm_cost_usd_running': 1,
            'monthly_llm_cost_usd': 50,
            'emergency_stop_single_call_usd': 10,
            'emergency_stop_daily_usd': 20,
        },
        'sources': {
            'D1': {'name': '国务院', 'frequency_hours': 6, 'credibility': '权威'},
            'D4': {'name': 'Semantic Scholar', 'frequency_hours': 24, 'credibility': '参考'},
            'V1': {'name': '国家统计局', 'frequency_hours': 24, 'credibility': '权威'},
            'V3': {'name': '韩国关税厅', 'frequency_hours': 24, 'credibility': '权威'},
            'S4': {'name': 'AkShare', 'frequency_hours': 12, 'credibility': '权威'},
        },
        'database': {
            'knowledge_db': 'data/knowledge.db',
            'queue_db': 'data/queue.db',
        },
        'concurrency': {
            'max_workers': 4,
            'db_lock_timeout': 30,
            'transaction_isolation': 'IMMEDIATE',
        },
        'error_handling': {
            'max_retries': 3,
            'retry_backoff_base': 2,
            'alert_webhook': None,
        },
        'timezone': 'Asia/Seoul',
        'testing': {
            'coverage_target': 60,
            'fixture_dir': 'tests/fixtures',
        },
        'mode': 'cold_start',
    }


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "test_config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    return p


# ═══ 加载 config.yaml ═══

class TestConfigYamlLoad:
    def test_config_yaml_file_exists(self):
        assert DEFAULT_CONFIG_PATH.exists(), f"config.yaml missing: {DEFAULT_CONFIG_PATH}"

    def test_default_config_loads(self):
        cfg = load_config(env={})
        assert isinstance(cfg, ScoutConfig)

    def test_returns_typed_sub_sections(self):
        cfg = load_config(env={})
        assert isinstance(cfg.llm, LLMConfig)
        assert isinstance(cfg.database, DatabaseConfig)
        assert isinstance(cfg.concurrency, ConcurrencyConfig)
        assert isinstance(cfg.error_handling, ErrorHandlingConfig)
        assert isinstance(cfg.testing, TestingConfig)
        for src in cfg.sources.values():
            assert isinstance(src, SourceConfig)


# ═══ Phase 1 关键字段 ═══

class TestPhase1Fields:
    @pytest.mark.parametrize("src", sorted(PHASE1_REQUIRED_SOURCES))
    def test_all_5_core_sources_present(self, src):
        cfg = load_config(env={})
        assert src in cfg.sources

    def test_phase1_mode_is_gemma_only(self):
        cfg = load_config(env={})
        assert cfg.llm.phase1_mode == 'gemma_only'

    def test_api_key_not_required_in_phase1(self):
        cfg = load_config(env={})
        assert cfg.llm.api_key_required is False

    def test_mode_is_cold_start(self):
        cfg = load_config(env={})
        assert cfg.mode == 'cold_start'

    def test_timezone_is_asia_seoul(self):
        cfg = load_config(env={})
        assert cfg.timezone == 'Asia/Seoul'

    def test_v166_usd_budget_fields(self):
        cfg = load_config(env={})
        assert cfg.llm.daily_llm_cost_usd_running == 1
        assert cfg.llm.monthly_llm_cost_usd == 50
        assert cfg.llm.emergency_stop_single_call_usd == 10
        assert cfg.llm.emergency_stop_daily_usd == 20

    def test_krw_legacy_fields_preserved(self):
        cfg = load_config(env={})
        assert cfg.llm.daily_cost_limit_krw == 4500
        assert cfg.llm.monthly_cost_limit_krw == 60000

    def test_cloud_model_value(self):
        cfg = load_config(env={})
        assert cfg.llm.cloud_model == 'claude-sonnet-4-6'

    def test_local_model_is_gemma(self):
        cfg = load_config(env={})
        assert cfg.llm.local_model == 'gemma4:e4b'


# ═══ 环境变量覆盖 ═══

class TestEnvOverride:
    def test_anthropic_api_key_from_env_injected(self):
        cfg = load_config(env={'ANTHROPIC_API_KEY': 'sk-ant-test-12345'})
        assert cfg.llm.api_key == 'sk-ant-test-12345'

    def test_no_api_key_when_env_absent(self):
        cfg = load_config(env={})
        assert cfg.llm.api_key is None

    def test_empty_api_key_env_ignored(self):
        cfg = load_config(env={'ANTHROPIC_API_KEY': ''})
        assert cfg.llm.api_key is None

    def test_scout_mode_env_override(self):
        cfg = load_config(env={'SCOUT_MODE': 'diagnosis'})
        assert cfg.mode == 'diagnosis'

    @pytest.mark.parametrize("m", ['cold_start', 'running', 'diagnosis'])
    def test_all_scout_modes_overridable(self, m):
        cfg = load_config(env={'SCOUT_MODE': m})
        assert cfg.mode == m

    def test_invalid_scout_mode_env_raises(self):
        with pytest.raises(ValidationError):
            load_config(env={'SCOUT_MODE': 'invalid_mode'})

    def test_env_none_falls_back_to_os_environ(self, monkeypatch):
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-from-os-env')
        # 清掉可能干扰的其他 env
        monkeypatch.delenv('SCOUT_MODE', raising=False)
        cfg = load_config(env=None)
        assert cfg.llm.api_key == 'sk-from-os-env'


# ═══ Schema 验证（fail-fast） ═══

class TestSchemaValidation:
    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(config_path=tmp_path / "no_such.yaml", env={})

    def test_empty_yaml_raises(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding='utf-8')
        with pytest.raises((ValueError, ValidationError)):
            load_config(config_path=p, env={})

    def test_non_dict_yaml_raises(self, tmp_path):
        p = tmp_path / "list.yaml"
        p.write_text("- just\n- a\n- list\n", encoding='utf-8')
        with pytest.raises((ValueError, ValidationError)):
            load_config(config_path=p, env={})

    def test_missing_llm_section_raises(self, tmp_path):
        d = _base_dict()
        del d['llm']
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_missing_mode_raises(self, tmp_path):
        d = _base_dict()
        del d['mode']
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_missing_phase1_source_D1_raises(self, tmp_path):
        d = _base_dict()
        del d['sources']['D1']
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError) as exc:
            load_config(config_path=p, env={})
        assert "D1" in str(exc.value)

    def test_missing_phase1_source_S4_raises(self, tmp_path):
        d = _base_dict()
        del d['sources']['S4']
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError) as exc:
            load_config(config_path=p, env={})
        assert "S4" in str(exc.value)

    def test_invalid_mode_raises(self, tmp_path):
        d = _base_dict()
        d['mode'] = 'rogue'
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_invalid_phase1_mode_raises(self, tmp_path):
        d = _base_dict()
        d['llm']['phase1_mode'] = 'chaos'
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_invalid_credibility_raises(self, tmp_path):
        d = _base_dict()
        d['sources']['D1']['credibility'] = '不靠谱'
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_invalid_transaction_isolation_raises(self, tmp_path):
        d = _base_dict()
        d['concurrency']['transaction_isolation'] = 'READ_COMMITTED'
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_zero_max_workers_raises(self, tmp_path):
        d = _base_dict()
        d['concurrency']['max_workers'] = 0
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_negative_usd_budget_raises(self, tmp_path):
        d = _base_dict()
        d['llm']['monthly_llm_cost_usd'] = -10
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_coverage_target_over_100_raises(self, tmp_path):
        d = _base_dict()
        d['testing']['coverage_target'] = 150
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_zero_frequency_hours_raises(self, tmp_path):
        d = _base_dict()
        d['sources']['D1']['frequency_hours'] = 0
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_extra_top_level_field_raises(self, tmp_path):
        d = _base_dict()
        d['mystery_field'] = 'leaked'
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_extra_llm_field_raises(self, tmp_path):
        d = _base_dict()
        d['llm']['rogue_budget'] = 999
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})

    def test_empty_timezone_raises(self, tmp_path):
        d = _base_dict()
        d['timezone'] = ''
        p = _write_yaml(tmp_path, d)
        with pytest.raises(ValidationError):
            load_config(config_path=p, env={})


# ═══ Source 细节 ═══

class TestSourceConfigDetails:
    def test_D1_has_url(self):
        cfg = load_config(env={})
        assert cfg.sources['D1'].url == 'https://www.gov.cn/'

    def test_D4_has_api_url(self):
        cfg = load_config(env={})
        assert cfg.sources['D4'].api_url == 'https://api.semanticscholar.org/'

    @pytest.mark.parametrize(
        "src,expected",
        [('D1', '权威'), ('D4', '参考'), ('V1', '权威'), ('V3', '权威'), ('S4', '权威')],
    )
    def test_source_credibility_correct(self, src, expected):
        cfg = load_config(env={})
        assert cfg.sources[src].credibility == expected

    def test_D1_frequency_6h(self):
        cfg = load_config(env={})
        assert cfg.sources['D1'].frequency_hours == 6

    def test_S4_frequency_12h(self):
        cfg = load_config(env={})
        assert cfg.sources['S4'].frequency_hours == 12
