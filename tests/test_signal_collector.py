"""
tests/test_signal_collector.py — SignalCollectorAgent 测试矩阵

覆盖：
    - 入参校验（RuleViolation 矩阵：raw_text / source / title / published_date）
    - 规则预检矩阵（RESTRICTIVE_HARD / SUPPORTIVE_CANDIDATES / MULTI_INTERPRETATION）
    - 规则硬覆盖（override Gemma 的 supportive；confidence 强制 1.0；reasoning 标签）
    - 低置信度 → null（边界 0.69 / 0.70）
    - 多解读 → null（边界 0.84 / 0.85；mixed 豁免；null 本身豁免）
    - mixed 必须带 subtype（默认 conflict）
    - 非 mixed 时 subtype 强制 None（契约）
    - Gemma 连接失败 → DataMissingError（返 None + 落 'data'）
    - Gemma malformed JSON → ParseError（返 None + 落 'parse'）
    - Gemma generic 异常 → LLMError（返 None + 落 'llm'）
    - 重试：首次失败 + 次试成功 → 成功
    - 重试耗尽 → DataMissingError / LLMError
    - llm_invocations 表落库（字段 / 幂等）
    - InfoUnitV1 契约合规（id / credibility / UTC / content blob）
    - 幂等：同 (source, title, date) → 同 id
    - 类型清洗矩阵（direction / confidence / mixed_subtype / industries）
    - prompt 加载错误（RuleViolation）
    - token 统计（dict / 对象 response）
    - response 结构兼容（dict / pydantic-like）
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from agents.base import (
    BaseAgent,
    DataMissingError,
    LLMError,
    ParseError,
    RuleViolation,
)
from agents.signal_collector import (
    CONFIDENCE_NULL_THRESHOLD,
    CREDIBILITY_MAP,
    MULTI_INTERP_TRUST_THRESHOLD,
    MULTI_INTERPRETATION,
    RESTRICTIVE_HARD,
    SUPPORTIVE_CANDIDATES,
    SignalCollectorAgent,
)
from contracts.contracts import InfoUnitV1
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database
from utils.hash_utils import info_unit_id


# ═══════════════════ fixtures ═══════════════════

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "signal_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """跳过真 sleep（重试 backoff 快跑）"""
    monkeypatch.setattr("time.sleep", lambda s: None)


# ═══════════════════ Fake Ollama Client ═══════════════════


class _FakeOllamaResponse(dict):
    """支持 .message.content 对象式访问（模拟 ollama-python 新版 pydantic 响应）"""

    class _Msg:
        def __init__(self, content: str):
            self.content = content

    def __init__(self, content: str, prompt_tokens: int = 50, eval_tokens: int = 30):
        super().__init__()
        self["message"] = {"content": content}
        self["prompt_eval_count"] = prompt_tokens
        self["eval_count"] = eval_tokens
        self.message = self._Msg(content)
        self.prompt_eval_count = prompt_tokens
        self.eval_count = eval_tokens


class FakeOllamaClient:
    """可注入到 SignalCollectorAgent(ollama_client=...) 的 mock。

    用法：
        client = FakeOllamaClient(response={"policy_direction": "supportive", ...})
        client = FakeOllamaClient(raise_on_call=ConnectionError("..."))
        client = FakeOllamaClient(call_sequence=[ConnectionError(...), {...success}])
    """

    def __init__(
        self,
        response: Optional[Dict[str, Any]] = None,
        raise_on_call: Optional[Exception] = None,
        call_sequence: Optional[List[Any]] = None,
        use_object_response: bool = False,
        prompt_tokens: int = 50,
        eval_tokens: int = 30,
    ):
        self.response = response
        self.raise_on_call = raise_on_call
        self.call_sequence = call_sequence or []
        self.use_object_response = use_object_response
        self.prompt_tokens = prompt_tokens
        self.eval_tokens = eval_tokens
        self.calls: List[Dict[str, Any]] = []

    def chat(self, *, model, messages, format=None, options=None):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "format": format,
                "options": options,
            }
        )

        if self.call_sequence:
            idx = min(len(self.calls) - 1, len(self.call_sequence) - 1)
            item = self.call_sequence[idx]
            if isinstance(item, Exception):
                raise item
            return self._shape(item)

        if self.raise_on_call is not None:
            raise self.raise_on_call

        if self.response is None:
            return self._shape({
                "policy_direction": "neutral",
                "confidence": 0.8,
                "category": "默认",
                "related_industries": [],
                "summary": "default",
                "reasoning": "default",
            })
        return self._shape(self.response)

    def _shape(self, payload: Dict[str, Any]):
        """把一个 dict payload 包成 ollama.chat 的响应结构。

        payload 已经是 ollama 响应（含 message 键）→ 原样返回
        payload 是业务 JSON（gemma output）→ 塞进 message.content
        """
        if "message" in payload:
            # 已是完整响应
            resp = _FakeOllamaResponse(
                content=payload["message"]["content"],
                prompt_tokens=payload.get("prompt_eval_count", self.prompt_tokens),
                eval_tokens=payload.get("eval_count", self.eval_tokens),
            )
        else:
            # 业务 payload → 序列化到 content
            if isinstance(payload, str):
                content_str = payload
            else:
                content_str = json.dumps(payload, ensure_ascii=False)
            resp = _FakeOllamaResponse(
                content=content_str,
                prompt_tokens=self.prompt_tokens,
                eval_tokens=self.eval_tokens,
            )
        if self.use_object_response:
            # 返回"对象式" response（只有属性访问，没有 dict 访问）
            class _ObjResp:
                pass
            o = _ObjResp()
            o.message = resp.message
            o.prompt_eval_count = resp.prompt_eval_count
            o.eval_count = resp.eval_count
            return o
        return resp


# ── 常用 helper ──

def _make_agent(db, client=None, **kwargs):
    if client is None:
        client = FakeOllamaClient()
    return SignalCollectorAgent(
        db=db,
        ollama_client=client,
        **kwargs,
    )


def _typical_payload(**overrides) -> Dict[str, Any]:
    base = {
        "policy_direction": "supportive",
        "mixed_subtype": None,
        "confidence": 0.9,
        "category": "政策发布",
        "related_industries": ["新能源汽车"],
        "summary": "测试摘要",
        "reasoning": "测试推理",
    }
    base.update(overrides)
    return base


# ═══════════════════ 模块常量完整性 ═══════════════════


class TestConstants:
    def test_restrictive_hard_has_expected_keywords(self):
        for kw in ("禁止", "取缔", "不得", "限制", "整改", "严禁"):
            assert kw in RESTRICTIVE_HARD

    def test_supportive_candidates_has_expected_keywords(self):
        for kw in ("支持", "鼓励", "加快", "推进", "大力发展"):
            assert kw in SUPPORTIVE_CANDIDATES

    def test_multi_interpretation_has_expected_keywords(self):
        for kw in ("进口", "出口", "价格", "产能", "库存"):
            assert kw in MULTI_INTERPRETATION

    def test_credibility_map_covers_all_phase1_sources(self):
        assert set(CREDIBILITY_MAP.keys()) == {"D1", "D4", "V1", "V3", "S4"}

    def test_thresholds_sensible(self):
        assert 0.0 < CONFIDENCE_NULL_THRESHOLD < 1.0
        assert CONFIDENCE_NULL_THRESHOLD <= MULTI_INTERP_TRUST_THRESHOLD
        assert MULTI_INTERP_TRUST_THRESHOLD <= 1.0


# ═══════════════════ 初始化 ═══════════════════


class TestInit:
    def test_default_init_uses_real_ollama_but_we_inject(self, tmp_db):
        client = FakeOllamaClient()
        agent = SignalCollectorAgent(db=tmp_db, ollama_client=client)
        assert agent.name == "signal_collector"
        assert agent.db is tmp_db
        assert agent.model == "gemma4:e4b"
        assert agent.prompt_version == "v001"
        assert agent.client is client

    def test_custom_model_and_version(self, tmp_db):
        agent = SignalCollectorAgent(
            db=tmp_db,
            ollama_client=FakeOllamaClient(),
            model="custom-model",
            prompt_version="v001",  # v001 实际存在
        )
        assert agent.model == "custom-model"
        assert agent.prompt_version == "v001"

    def test_missing_prompt_file_raises_rule_violation(self, tmp_db):
        with pytest.raises(RuleViolation, match="Prompt template not found"):
            SignalCollectorAgent(
                db=tmp_db,
                ollama_client=FakeOllamaClient(),
                prompt_version="v999_nonexistent",
            )

    def test_prompt_loaded_at_init(self, tmp_db):
        agent = _make_agent(tmp_db)
        assert "signal_collector_v001" in agent.prompt_template
        assert "policy_direction" in agent.prompt_template

    def test_is_baseagent_subclass(self):
        assert issubclass(SignalCollectorAgent, BaseAgent)


# ═══════════════════ 入参校验（RuleViolation 矩阵）═══════════════════


class TestInputValidation:
    @pytest.mark.parametrize("bad_raw", ["", "   ", None, 123, []])
    def test_empty_raw_text_logs_rule(self, tmp_db, bad_raw):
        agent = _make_agent(tmp_db)
        result = agent.run(
            raw_text=bad_raw,
            source="D1",
            title="t",
            published_date="2026-04-18",
        )
        assert result is None
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name='signal_collector'"
        )
        assert any(r["error_type"] == "rule" for r in rows)

    @pytest.mark.parametrize("bad_source", ["", "D2", "XX", None, 123])
    def test_invalid_source_logs_rule(self, tmp_db, bad_source):
        agent = _make_agent(tmp_db)
        result = agent.run(
            raw_text="content",
            source=bad_source,
            title="t",
            published_date="2026-04-18",
        )
        assert result is None
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name='signal_collector'"
        )
        assert any(r["error_type"] == "rule" for r in rows)

    @pytest.mark.parametrize("bad_title", ["", "   ", None])
    def test_empty_title_logs_rule(self, tmp_db, bad_title):
        agent = _make_agent(tmp_db)
        result = agent.run(
            raw_text="content",
            source="D1",
            title=bad_title,
            published_date="2026-04-18",
        )
        assert result is None

    @pytest.mark.parametrize("bad_date", ["", "   ", None])
    def test_empty_published_date_logs_rule(self, tmp_db, bad_date):
        agent = _make_agent(tmp_db)
        result = agent.run(
            raw_text="content",
            source="D1",
            title="t",
            published_date=bad_date,
        )
        assert result is None


# ═══════════════════ Step 1: 规则预检 ═══════════════════


class TestRulesPrecheck:
    @pytest.mark.parametrize("kw", list(RESTRICTIVE_HARD))
    def test_each_restrictive_hard_keyword_detected(self, kw):
        signals = SignalCollectorAgent._rules_precheck(f"文本{kw}内容")
        assert signals["has_restrictive_hard"] is True

    @pytest.mark.parametrize("kw", list(SUPPORTIVE_CANDIDATES))
    def test_each_supportive_candidate_detected(self, kw):
        signals = SignalCollectorAgent._rules_precheck(f"文本{kw}内容")
        assert signals["has_supportive_candidate"] is True

    @pytest.mark.parametrize("kw", list(MULTI_INTERPRETATION))
    def test_each_multi_interpretation_detected(self, kw):
        signals = SignalCollectorAgent._rules_precheck(f"文本{kw}内容")
        assert signals["has_multi_interpretation"] is True

    def test_empty_text_no_signals(self):
        signals = SignalCollectorAgent._rules_precheck("")
        assert signals == {
            "has_restrictive_hard": False,
            "has_supportive_candidate": False,
            "has_multi_interpretation": False,
        }

    def test_non_keyword_text_no_signals(self):
        signals = SignalCollectorAgent._rules_precheck(
            "这是一段普通的中文文本"
        )
        assert all(v is False for v in signals.values())

    def test_mixed_signals_in_one_text(self):
        signals = SignalCollectorAgent._rules_precheck(
            "限制低端产能同时鼓励高端研发，进出口数据同比上升"
        )
        assert signals["has_restrictive_hard"] is True
        assert signals["has_supportive_candidate"] is True
        assert signals["has_multi_interpretation"] is True


# ═══════════════════ Step 3: 组合决策 — 规则硬覆盖 ═══════════════════


class TestHardOverride:
    def test_restrictive_hard_overrides_supportive_gemma(self, tmp_db):
        """Gemma 说 supportive 0.95，但文本含"禁止" → final=restrictive"""
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="supportive",
            confidence=0.95,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="禁止钢铁新增产能",
            source="D1",
            title="钢铁限产公告",
            published_date="2026-04-18",
        )

        assert unit is not None
        assert unit.policy_direction == "restrictive"
        assert unit.mixed_subtype is None
        # content 内应标记 rules_override=True
        content = json.loads(unit.content)
        assert content["rules_override"] is True
        assert "[rules:hard]" in content["reasoning"]

    def test_restrictive_hard_overrides_mixed_gemma(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="mixed",
            mixed_subtype="conflict",
            confidence=0.8,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="严禁低端产能扩张",
            source="D1",
            title="产能整顿",
            published_date="2026-04-18",
        )

        assert unit.policy_direction == "restrictive"
        assert unit.mixed_subtype is None  # 规则覆盖 → 非 mixed → 必须 None

    @pytest.mark.parametrize("kw", list(RESTRICTIVE_HARD))
    def test_each_restrictive_keyword_triggers_override(self, tmp_db, kw):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="supportive",
            confidence=0.95,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text=f"文本中含有{kw}关键词",
            source="D1",
            title=f"测试{kw}",
            published_date="2026-04-18",
        )

        assert unit.policy_direction == "restrictive"

    def test_override_tag_notes_original_direction(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="supportive",
            confidence=0.9,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="禁止类文本",
            source="D1",
            title="t",
            published_date="2026-04-18",
        )

        content = json.loads(unit.content)
        assert "overrode gemma=supportive" in content["reasoning"]


# ═══════════════════ Step 3: 组合决策 — 低置信度 ═══════════════════


class TestLowConfidenceNull:
    @pytest.mark.parametrize(
        "conf, expect_direction",
        [
            (0.0, None),
            (0.5, None),
            (0.69, None),
            (0.70, "supportive"),  # 边界：>= threshold
            (0.75, "supportive"),
            (0.95, "supportive"),
        ],
    )
    def test_confidence_threshold_boundary(self, tmp_db, conf, expect_direction):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="supportive",
            confidence=conf,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励新能源发展",
            source="D1",
            title=f"test_conf_{conf}",
            published_date="2026-04-18",
        )

        assert unit.policy_direction == expect_direction

    def test_low_confidence_clears_mixed_subtype(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="mixed",
            mixed_subtype="conflict",
            confidence=0.5,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="普通文本",
            source="D1",
            title="t_low_mixed",
            published_date="2026-04-18",
        )

        assert unit.policy_direction is None
        assert unit.mixed_subtype is None


# ═══════════════════ Step 3: 组合决策 — 多解读 ═══════════════════


class TestMultiInterpretationNull:
    @pytest.mark.parametrize(
        "conf, expect_null",
        [
            (0.70, True),   # 低于多解读阈值
            (0.80, True),
            (0.84, True),
            (0.85, False),  # 达到阈值，信任 Gemma
            (0.90, False),
        ],
    )
    def test_multi_interp_threshold_boundary(self, tmp_db, conf, expect_null):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="supportive",
            confidence=conf,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="半导体设备进口额同比上升",  # 含 "进口"
            source="V3",
            title=f"multi_{conf}",
            published_date="2026-04-18",
        )

        if expect_null:
            assert unit.policy_direction is None
        else:
            assert unit.policy_direction == "supportive"

    def test_multi_interp_does_not_affect_mixed(self, tmp_db):
        """mixed 豁免多解读检查（mixed 本身承认矛盾）"""
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="mixed",
            mixed_subtype="conflict",
            confidence=0.75,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="进口限制同时鼓励本土生产",
            source="D1",
            title="mixed_multi",
            published_date="2026-04-18",
        )

        # 但此文本也含"限制"（RESTRICTIVE_HARD），会被硬覆盖。换个文本
        # 用 SUPPORTIVE_CANDIDATES + 多解读
        client2 = FakeOllamaClient(response=_typical_payload(
            policy_direction="mixed",
            mixed_subtype="conflict",
            confidence=0.75,
        ))
        agent2 = _make_agent(tmp_db, client=client2)
        unit2 = agent2.run(
            raw_text="鼓励本土半导体研发，进口料大幅上升",
            source="D1",
            title="mixed_multi_2",
            published_date="2026-04-18",
        )
        assert unit2.policy_direction == "mixed"
        assert unit2.mixed_subtype == "conflict"

    def test_multi_interp_does_not_affect_null(self, tmp_db):
        """Gemma 已给 null，不改变"""
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction=None,
            confidence=0.5,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="进口数据",
            source="V3",
            title="null_case",
            published_date="2026-04-18",
        )

        assert unit.policy_direction is None


# ═══════════════════ Step 3: 组合决策 — mixed + subtype ═══════════════════


class TestMixedSubtype:
    def test_mixed_without_subtype_defaults_to_conflict(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="mixed",
            mixed_subtype=None,  # Gemma 漏填
            confidence=0.85,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励高端 + 整顿低端",
            source="D1",
            title="mixed_no_subtype",
            published_date="2026-04-18",
        )
        # "整顿"不在 RESTRICTIVE_HARD（"整改" 才是）
        assert unit.policy_direction == "mixed"
        assert unit.mixed_subtype == "conflict"

    def test_mixed_with_explicit_subtype_preserved(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction="mixed",
            mixed_subtype="structural",
            confidence=0.9,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励 A 领域",
            source="D1",
            title="mixed_struct",
            published_date="2026-04-18",
        )
        assert unit.policy_direction == "mixed"
        assert unit.mixed_subtype == "structural"

    @pytest.mark.parametrize("direction", ["supportive", "neutral"])
    def test_non_mixed_clears_subtype_even_if_gemma_sets_it(
        self, tmp_db, direction
    ):
        """契约硬约束：非 mixed 时 mixed_subtype 必须 None"""
        client = FakeOllamaClient(response=_typical_payload(
            policy_direction=direction,
            mixed_subtype="conflict",  # Gemma 误填
            confidence=0.9,
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励发展",
            source="D1",
            title=f"non_mixed_{direction}",
            published_date="2026-04-18",
        )
        assert unit.policy_direction == direction
        assert unit.mixed_subtype is None


# ═══════════════════ Step 2: Gemma 错误处理 ═══════════════════


class TestGemmaErrors:
    def test_connection_error_classified_as_data(self, tmp_db):
        client = FakeOllamaClient(
            raise_on_call=ConnectionError("Connection refused")
        )
        agent = _make_agent(tmp_db, client=client)

        result = agent.run(
            raw_text="content",
            source="D1",
            title="t",
            published_date="2026-04-18",
        )

        assert result is None
        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors "
            "WHERE agent_name='signal_collector' ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["error_type"] == "data"
        assert "unreachable" in rows[0]["error_message"].lower()

    def test_timeout_error_classified_as_data(self, tmp_db):
        client = FakeOllamaClient(
            raise_on_call=TimeoutError("read timed out")
        )
        agent = _make_agent(tmp_db, client=client)

        result = agent.run(
            raw_text="content",
            source="D1",
            title="t_timeout",
            published_date="2026-04-18",
        )

        assert result is None
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors "
            "WHERE agent_name='signal_collector' ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["error_type"] == "data"

    def test_malformed_json_classified_as_parse(self, tmp_db):
        """Gemma 返回非 JSON 串 → ParseError → 'parse' 类错误"""
        # FakeOllamaClient._shape 的 payload 是 str → 直接塞 content
        client = FakeOllamaClient(response="this is not json at all")
        agent = _make_agent(tmp_db, client=client)

        result = agent.run(
            raw_text="content",
            source="D1",
            title="t_bad_json",
            published_date="2026-04-18",
        )

        assert result is None
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors "
            "WHERE agent_name='signal_collector' ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["error_type"] == "parse"

    def test_json_array_not_object_is_parse_error(self, tmp_db):
        """Gemma 返回 JSON 数组而非对象 → ParseError"""
        client = FakeOllamaClient(response="[1, 2, 3]")
        agent = _make_agent(tmp_db, client=client)

        result = agent.run(
            raw_text="content",
            source="D1",
            title="t_arr",
            published_date="2026-04-18",
        )

        assert result is None
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors "
            "WHERE agent_name='signal_collector' ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["error_type"] == "parse"

    def test_generic_error_classified_as_llm(self, tmp_db):
        client = FakeOllamaClient(
            raise_on_call=RuntimeError("rate limited by some non-network reason")
        )
        agent = _make_agent(tmp_db, client=client)

        result = agent.run(
            raw_text="content",
            source="D1",
            title="t_rate",
            published_date="2026-04-18",
        )

        assert result is None
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors "
            "WHERE agent_name='signal_collector' ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["error_type"] == "llm"

    def test_response_missing_content_is_parse(self, tmp_db):
        client = FakeOllamaClient(response={"message": {"content": ""}})
        # 空字符串会被 json.loads 失败 → ParseError
        agent = _make_agent(tmp_db, client=client)

        result = agent.run(
            raw_text="content",
            source="D1",
            title="t_empty",
            published_date="2026-04-18",
        )

        assert result is None


# ═══════════════════ Gemma 重试 ═══════════════════


class TestGemmaRetry:
    def test_retry_succeeds_on_second_attempt(self, tmp_db):
        client = FakeOllamaClient(call_sequence=[
            ConnectionError("first fail"),
            _typical_payload(policy_direction="supportive", confidence=0.9),
        ])
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励新能源",
            source="D1",
            title="t_retry_ok",
            published_date="2026-04-18",
        )

        assert unit is not None
        assert unit.policy_direction == "supportive"
        assert len(client.calls) == 2

    def test_retry_exhausted_raises_data_missing(self, tmp_db):
        client = FakeOllamaClient(call_sequence=[
            ConnectionError("fail 1"),
            ConnectionError("fail 2"),
            ConnectionError("fail 3"),
        ])
        agent = _make_agent(tmp_db, client=client, max_gemma_retries=2)

        result = agent.run(
            raw_text="content",
            source="D1",
            title="t_retry_fail",
            published_date="2026-04-18",
        )

        assert result is None
        assert len(client.calls) == 3  # 1 + 2 retries
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors "
            "WHERE agent_name='signal_collector' ORDER BY id DESC LIMIT 1"
        )
        assert rows[0]["error_type"] == "data"

    def test_parse_error_does_not_trigger_retry(self, tmp_db):
        """JSON 错是模型问题，重试无意义"""
        client = FakeOllamaClient(response="not json")
        agent = _make_agent(tmp_db, client=client, max_gemma_retries=2)

        agent.run(
            raw_text="content",
            source="D1",
            title="t_no_retry",
            published_date="2026-04-18",
        )

        assert len(client.calls) == 1  # 不重试

    def test_custom_max_retries_respected(self, tmp_db):
        client = FakeOllamaClient(
            raise_on_call=ConnectionError("always fail")
        )
        agent = _make_agent(tmp_db, client=client, max_gemma_retries=0)

        agent.run(
            raw_text="content",
            source="D1",
            title="t_no_retry",
            published_date="2026-04-18",
        )
        assert len(client.calls) == 1


# ═══════════════════ llm_invocations 记账 ═══════════════════


class TestLLMInvocationsLog:
    def test_successful_call_writes_row(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload(
            summary="A 政策"
        ), prompt_tokens=123, eval_tokens=45)
        agent = _make_agent(tmp_db, client=client)

        agent.run(
            raw_text="鼓励 A",
            source="D1",
            title="t_log",
            published_date="2026-04-18",
        )

        rows = tmp_db.query(
            "SELECT agent_name, prompt_version, model_name, input_hash, "
            "output_summary, tokens_used, cost_cents FROM llm_invocations"
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["agent_name"] == "signal_collector"
        assert r["prompt_version"] == "signal_collector_v001"
        assert r["model_name"] == "gemma4:e4b"
        assert r["tokens_used"] == 123 + 45
        assert r["cost_cents"] == 0
        assert "A 政策" in r["output_summary"]
        # input_hash 是 16 位 hex
        assert len(r["input_hash"]) == 16
        int(r["input_hash"], 16)  # 是合法 hex

    def test_input_hash_is_deterministic(self, tmp_db):
        """相同 raw_text[:500] 应产生相同 input_hash"""
        client1 = FakeOllamaClient(response=_typical_payload())
        agent1 = _make_agent(tmp_db, client=client1)
        agent1.run(
            raw_text="同一内容",
            source="D1",
            title="t1",
            published_date="2026-04-18",
        )

        client2 = FakeOllamaClient(response=_typical_payload())
        agent2 = _make_agent(tmp_db, client=client2)
        agent2.run(
            raw_text="同一内容",
            source="D1",
            title="t2",
            published_date="2026-04-19",
        )

        rows = tmp_db.query("SELECT input_hash FROM llm_invocations ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["input_hash"] == rows[1]["input_hash"]

    def test_error_path_does_not_write_llm_invocations(self, tmp_db):
        """连接失败不应写记账（没发生成功调用）"""
        client = FakeOllamaClient(
            raise_on_call=ConnectionError("down")
        )
        agent = _make_agent(tmp_db, client=client)

        agent.run(
            raw_text="x",
            source="D1",
            title="t_err",
            published_date="2026-04-18",
        )

        rows = tmp_db.query("SELECT COUNT(*) AS n FROM llm_invocations")
        assert rows[0]["n"] == 0

    def test_logging_failure_does_not_break_main_flow(self, tmp_db, monkeypatch):
        """llm_invocations 写入失败不应影响 process 返回 InfoUnitV1"""
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)

        # 替换 db.write：调用 llm_invocations 时抛错；其它调用正常
        original_write = agent.db.write

        def flaky_write(sql, params):
            if "llm_invocations" in sql:
                raise RuntimeError("disk full")
            return original_write(sql, params)

        monkeypatch.setattr(agent.db, "write", flaky_write)

        unit = agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_log_fail",
            published_date="2026-04-18",
        )
        assert unit is not None  # 主流程不崩


# ═══════════════════ InfoUnitV1 契约合规 ═══════════════════


class TestInfoUnitContract:
    def test_id_is_info_unit_id_hash(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励新能源",
            source="D1",
            title="电动车新规",
            published_date="2026-04-18",
        )
        expected_id = info_unit_id("D1", "电动车新规", "2026-04-18")
        assert unit.id == expected_id

    def test_same_source_title_date_gives_same_id(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)

        u1 = agent.run(
            raw_text="内容 A",
            source="D1",
            title="同标题",
            published_date="2026-04-18",
        )
        u2 = agent.run(
            raw_text="内容 B 完全不同",  # 内容变了
            source="D1",
            title="同标题",
            published_date="2026-04-18",
        )
        assert u1.id == u2.id

    def test_source_credibility_matches_map(self, tmp_db):
        for source, cred in CREDIBILITY_MAP.items():
            client = FakeOllamaClient(response=_typical_payload())
            agent = _make_agent(tmp_db, client=client)
            unit = agent.run(
                raw_text="鼓励",
                source=source,
                title=f"t_{source}",
                published_date="2026-04-18",
            )
            assert unit.source_credibility == cred, f"source={source}"

    def test_timestamp_is_utc_iso(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)
        unit = agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_time",
            published_date="2026-04-18",
        )
        # 契约已验证：+00:00 或 Z 后缀
        assert unit.timestamp.endswith("+00:00") or unit.timestamp.endswith("Z")

    def test_content_is_valid_json_with_expected_fields(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload(
            summary="摘要文",
            reasoning="推理文",
        ))
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_content",
            published_date="2026-04-18",
        )

        parsed = json.loads(unit.content)
        assert parsed["title"] == "t_content"
        assert "raw_text_excerpt" in parsed
        assert parsed["summary"] == "摘要文"
        assert parsed["reasoning"] == "推理文"
        assert parsed["prompt_version"] == "v001"
        assert parsed["model_name"] == "gemma4:e4b"
        assert "rules_override" in parsed

    def test_content_includes_raw_metadata_if_provided(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_meta",
            published_date="2026-04-18",
            raw_metadata={"source_url": "http://ex.com/a", "doc_num": "国发2026-1"},
        )
        parsed = json.loads(unit.content)
        assert parsed["raw_metadata"]["source_url"] == "http://ex.com/a"

    def test_returned_object_is_infounit_v1(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)
        unit = agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_type",
            published_date="2026-04-18",
        )
        assert isinstance(unit, InfoUnitV1)


# ═══════════════════ 类型清洗矩阵 ═══════════════════


class TestTypeCoercion:
    @pytest.mark.parametrize(
        "inp, expected",
        [
            ("supportive", "supportive"),
            ("SUPPORTIVE", "supportive"),
            ("  supportive  ", "supportive"),
            ("restrictive", "restrictive"),
            ("neutral", "neutral"),
            ("mixed", "mixed"),
            (None, None),
            ("", None),
            ("null", None),
            ("none", None),
            ("invalid_value", None),
            ("positive", None),
            (123, None),
            ([], None),
        ],
    )
    def test_coerce_direction(self, inp, expected):
        assert SignalCollectorAgent._coerce_direction(inp) == expected

    @pytest.mark.parametrize(
        "inp, expected",
        [
            (0.5, 0.5),
            (0.0, 0.0),
            (1.0, 1.0),
            (1.5, 1.0),   # clamp
            (-0.5, 0.0),  # clamp
            ("0.8", 0.8),
            ("invalid", 0.0),
            (None, 0.0),
            (float("nan"), 0.0),
            (float("inf"), 1.0),  # inf > 1 → clamp
        ],
    )
    def test_coerce_confidence(self, inp, expected):
        result = SignalCollectorAgent._coerce_confidence(inp)
        assert result == pytest.approx(expected)

    @pytest.mark.parametrize(
        "inp, expected",
        [
            ("conflict", "conflict"),
            ("structural", "structural"),
            ("stage_difference", "stage_difference"),
            ("CONFLICT", "conflict"),
            (None, None),
            ("", None),
            ("null", None),
            ("invalid", None),
            (42, None),
        ],
    )
    def test_coerce_mixed_subtype(self, inp, expected):
        assert SignalCollectorAgent._coerce_mixed_subtype(inp) == expected

    @pytest.mark.parametrize(
        "inp, expected",
        [
            (["半导体", "新能源"], ["半导体", "新能源"]),
            ([" 半导体 ", "新能源"], ["半导体", "新能源"]),  # strip
            (None, []),
            ("not a list", []),
            ([], []),
            (["", "半导体"], ["半导体"]),  # 空串过滤
            ([1, 2, 3], []),  # 非字符串过滤
            (["半导体", 123, "新能源"], ["半导体", "新能源"]),
        ],
    )
    def test_coerce_industries(self, inp, expected):
        assert SignalCollectorAgent._coerce_industries(inp) == expected

    def test_coerce_industries_cap_limit(self):
        huge = [f"ind_{i}" for i in range(50)]
        result = SignalCollectorAgent._coerce_industries(huge)
        assert len(result) == 10


# ═══════════════════ Response 结构兼容 ═══════════════════


class TestResponseStructure:
    def test_dict_response_works(self, tmp_db):
        client = FakeOllamaClient(
            response=_typical_payload(),
            use_object_response=False,
        )
        agent = _make_agent(tmp_db, client=client)
        unit = agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_dict",
            published_date="2026-04-18",
        )
        assert unit is not None

    def test_object_response_works(self, tmp_db):
        """新版 ollama 库返 pydantic 对象（非 dict）"""
        client = FakeOllamaClient(
            response=_typical_payload(),
            use_object_response=True,
        )
        agent = _make_agent(tmp_db, client=client)
        unit = agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_obj",
            published_date="2026-04-18",
        )
        assert unit is not None

    def test_extract_tokens_from_dict(self):
        resp = {"prompt_eval_count": 100, "eval_count": 50}
        assert SignalCollectorAgent._extract_tokens(resp) == 150

    def test_extract_tokens_from_object(self):
        class R:
            prompt_eval_count = 100
            eval_count = 50
        assert SignalCollectorAgent._extract_tokens(R()) == 150

    def test_extract_tokens_missing_returns_zero(self):
        assert SignalCollectorAgent._extract_tokens({}) == 0

    def test_extract_tokens_none_values(self):
        assert SignalCollectorAgent._extract_tokens(
            {"prompt_eval_count": None, "eval_count": None}
        ) == 0


# ═══════════════════ 整合：完整流程烟囱 ═══════════════════


class TestEndToEnd:
    def test_happy_path_supportive(self, tmp_db):
        payload = _typical_payload(
            policy_direction="supportive",
            confidence=0.92,
            category="政策发布",
            related_industries=["新能源汽车", "电池"],
            summary="国务院推进智能网联汽车",
            reasoning="大力推进，方向明确",
        )
        client = FakeOllamaClient(response=payload)
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="大力推进智能网联汽车发展",
            source="D1",
            title="新能源汽车规划",
            published_date="2026-04-18",
        )

        assert unit.policy_direction == "supportive"
        assert unit.mixed_subtype is None
        assert "新能源汽车" in unit.related_industries
        assert len(client.calls) == 1
        # llm_invocations 写成功
        rows = tmp_db.query("SELECT COUNT(*) AS n FROM llm_invocations")
        assert rows[0]["n"] == 1

    def test_happy_path_restrictive_hard(self, tmp_db):
        payload = _typical_payload(
            policy_direction="supportive",  # 会被覆盖
            confidence=0.9,
            summary="政策发布",
        )
        client = FakeOllamaClient(response=payload)
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="严禁钢铁行业新增产能",
            source="D1",
            title="钢铁限产",
            published_date="2026-04-18",
        )

        assert unit.policy_direction == "restrictive"
        content = json.loads(unit.content)
        assert content["rules_override"] is True

    def test_happy_path_mixed(self, tmp_db):
        payload = _typical_payload(
            policy_direction="mixed",
            mixed_subtype="conflict",
            confidence=0.85,
        )
        client = FakeOllamaClient(response=payload)
        agent = _make_agent(tmp_db, client=client)

        unit = agent.run(
            raw_text="鼓励高端研发",  # 只含 SUPPORTIVE 关键字，非硬覆盖
            source="D1",
            title="产业政策",
            published_date="2026-04-18",
        )

        assert unit.policy_direction == "mixed"
        assert unit.mixed_subtype == "conflict"


# ═══════════════════ Ollama chat 调用参数 ═══════════════════


class TestOllamaCallArgs:
    def test_chat_called_with_model_and_format_json(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)
        agent.run(
            raw_text="鼓励",
            source="D1",
            title="t_args",
            published_date="2026-04-18",
        )

        call = client.calls[0]
        assert call["model"] == "gemma4:e4b"
        assert call["format"] == "json"
        assert call["options"]["temperature"] == 0.2

    def test_chat_messages_have_system_and_user(self, tmp_db):
        client = FakeOllamaClient(response=_typical_payload())
        agent = _make_agent(tmp_db, client=client)
        agent.run(
            raw_text="user content here",
            source="D1",
            title="t_msgs",
            published_date="2026-04-18",
        )
        msgs = client.calls[0]["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == agent.prompt_template
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "user content here"
