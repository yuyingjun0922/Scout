"""
tests/test_contracts.py — Step 3 Pydantic契约测试

覆盖：
    - 正常：InfoUnitV1 / WatchlistUpdateV1 / AgentError 都能创建
    - 异常：mixed不带subtype / source不在5选 / id非16hex / timestamp格式错
    - schema_version=Literal[1] 拒绝非1
    - extra='forbid' 拒绝未知字段
"""
import pytest
from pydantic import ValidationError

from contracts.contracts import AgentError, InfoUnitV1, WatchlistUpdateV1


VALID_ID = "a1b2c3d4e5f60718"          # 16 hex chars
VALID_TS = "2026-04-17T00:00:00+00:00"  # UTC ISO 8601


def _info(**overrides) -> dict:
    """InfoUnitV1最小合法参数集，overrides覆盖单个字段。"""
    base = dict(
        id=VALID_ID,
        source="D1",
        source_credibility="权威",
        timestamp=VALID_TS,
        category="政策",
        content="国务院发布某项政策",
        related_industries=["半导体"],
    )
    base.update(overrides)
    return base


# ═══ InfoUnitV1：正常case ═══

class TestInfoUnitV1Valid:
    def test_minimal_fields(self):
        info = InfoUnitV1(**_info())
        assert info.id == VALID_ID
        assert info.source == "D1"
        assert info.policy_direction is None
        assert info.mixed_subtype is None
        assert info.event_chain_id is None
        assert info.schema_version == 1

    def test_all_five_phase1_sources(self):
        for s in ("D1", "D4", "V1", "V3", "S4"):
            info = InfoUnitV1(**_info(source=s))
            assert info.source == s

    @pytest.mark.parametrize(
        "direction", ["supportive", "restrictive", "neutral"]
    )
    def test_non_mixed_directions(self, direction):
        info = InfoUnitV1(**_info(policy_direction=direction))
        assert info.policy_direction == direction
        assert info.mixed_subtype is None

    @pytest.mark.parametrize(
        "subtype", ["conflict", "structural", "stage_difference"]
    )
    def test_mixed_with_all_subtypes(self, subtype):
        info = InfoUnitV1(**_info(policy_direction="mixed", mixed_subtype=subtype))
        assert info.policy_direction == "mixed"
        assert info.mixed_subtype == subtype

    def test_timestamp_z_suffix_ok(self):
        """Z后缀也算UTC"""
        info = InfoUnitV1(**_info(timestamp="2026-04-17T00:00:00Z"))
        assert info.timestamp == "2026-04-17T00:00:00Z"

    def test_timestamp_microseconds_ok(self):
        """now_utc()输出的微秒精度格式"""
        info = InfoUnitV1(**_info(timestamp="2026-04-17T12:34:56.789012+00:00"))
        assert "789012" in info.timestamp

    def test_uppercase_hex_id_normalized(self):
        info = InfoUnitV1(**_info(id="ABCDEF0123456789"))
        assert info.id == "abcdef0123456789"

    def test_event_chain_id_accepted(self):
        info = InfoUnitV1(**_info(event_chain_id="#E-20260417-HBM"))
        assert info.event_chain_id == "#E-20260417-HBM"

    def test_related_industries_default_empty_list(self):
        d = _info()
        del d["related_industries"]
        info = InfoUnitV1(**d)
        assert info.related_industries == []


# ═══ InfoUnitV1：异常case ═══

class TestInfoUnitV1Invalid:
    def test_mixed_without_subtype_raises(self):
        """v1.60核心约束：mixed必须带subtype"""
        with pytest.raises(ValidationError) as exc:
            InfoUnitV1(**_info(policy_direction="mixed"))
        assert "mixed_subtype is required" in str(exc.value)

    def test_subtype_without_mixed_raises(self):
        """防误填：非mixed不能有subtype"""
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(
                policy_direction="supportive",
                mixed_subtype="conflict",
            ))

    def test_subtype_without_direction_raises(self):
        """policy_direction=None + subtype有值 → 也应拒绝"""
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(mixed_subtype="conflict"))

    @pytest.mark.parametrize("bad_source", ["V2", "X1", "d1", "DATA", ""])
    def test_invalid_source_raises(self, bad_source):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(source=bad_source))

    def test_id_too_short_raises(self):
        with pytest.raises(ValidationError) as exc:
            InfoUnitV1(**_info(id="short"))
        msg = str(exc.value)
        assert "16 chars" in msg or "must be 16" in msg

    def test_id_too_long_raises(self):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(id="a" * 17))

    def test_id_non_hex_raises(self):
        with pytest.raises(ValidationError) as exc:
            InfoUnitV1(**_info(id="zzzzzzzzzzzzzzzz"))
        assert "hex" in str(exc.value)

    def test_timestamp_naive_raises(self):
        """无时区信息应拒绝"""
        with pytest.raises(ValidationError) as exc:
            InfoUnitV1(**_info(timestamp="2026-04-17T00:00:00"))
        msg = str(exc.value)
        assert "timezone" in msg or "UTC" in msg

    def test_timestamp_non_utc_offset_raises(self):
        """非零偏移（即使合法ISO）也应拒绝"""
        with pytest.raises(ValidationError) as exc:
            InfoUnitV1(**_info(timestamp="2026-04-17T09:00:00+09:00"))
        assert "UTC" in str(exc.value) or "offset" in str(exc.value)

    def test_timestamp_garbage_raises(self):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(timestamp="not a date"))

    def test_invalid_credibility_raises(self):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(source_credibility="未知"))

    def test_invalid_policy_direction_raises(self):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(policy_direction="bullish"))

    def test_invalid_mixed_subtype_raises(self):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(
                policy_direction="mixed",
                mixed_subtype="disagreement",
            ))

    def test_schema_version_must_be_1(self):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(schema_version=2))

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            InfoUnitV1(**_info(unknown_field="oops"))


# ═══ WatchlistUpdateV1 ═══

class TestWatchlistUpdateV1:
    def test_minimal(self):
        u = WatchlistUpdateV1(industry_id=1)
        assert u.industry_id == 1
        assert u.dimensions is None
        assert u.verification_status is None
        assert u.motivation_detail is None
        assert u.schema_version == 1

    def test_full_phase2b_fields(self):
        u = WatchlistUpdateV1(
            industry_id=5,
            dimensions=4,
            verification_status="positive",
            motivation_detail={"level_3": 0.6, "level_5": 0.4},
            motivation_uncertainty="low",
        )
        assert u.dimensions == 4
        assert u.verification_status == "positive"
        assert u.motivation_detail["level_3"] == 0.6
        assert u.motivation_uncertainty == "low"

    @pytest.mark.parametrize(
        "status",
        ["positive", "early_positive", "neutral", "negative", "insufficient"],
    )
    def test_all_verification_statuses(self, status):
        u = WatchlistUpdateV1(industry_id=1, verification_status=status)
        assert u.verification_status == status

    def test_invalid_verification_status_raises(self):
        with pytest.raises(ValidationError):
            WatchlistUpdateV1(industry_id=1, verification_status="great")

    def test_industry_id_must_be_positive(self):
        with pytest.raises(ValidationError):
            WatchlistUpdateV1(industry_id=0)

    def test_industry_id_negative_raises(self):
        with pytest.raises(ValidationError):
            WatchlistUpdateV1(industry_id=-1)

    def test_invalid_uncertainty_raises(self):
        with pytest.raises(ValidationError):
            WatchlistUpdateV1(industry_id=1, motivation_uncertainty="unknown")

    def test_schema_version_must_be_1(self):
        with pytest.raises(ValidationError):
            WatchlistUpdateV1(industry_id=1, schema_version=2)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            WatchlistUpdateV1(industry_id=1, unknown_field="oops")


# ═══ AgentError ═══

class TestAgentError:
    def test_minimal(self):
        err = AgentError(
            agent_name="signal_collector",
            error_type="network",
            error_message="timeout after 30s",
            occurred_at=VALID_TS,
        )
        assert err.agent_name == "signal_collector"
        assert err.error_type == "network"
        assert err.context_data is None

    def test_with_context_data(self):
        err = AgentError(
            agent_name="direction_judge",
            error_type="llm",
            error_message="rate limit",
            context_data={"retry_count": 3, "endpoint": "claude"},
            occurred_at=VALID_TS,
        )
        assert err.context_data["retry_count"] == 3

    @pytest.mark.parametrize(
        "et", ["network", "parse", "llm", "rule", "data", "unknown"]
    )
    def test_all_six_error_types(self, et):
        err = AgentError(
            agent_name="x",
            error_type=et,
            error_message="m",
            occurred_at=VALID_TS,
        )
        assert err.error_type == et

    def test_invalid_error_type_raises(self):
        with pytest.raises(ValidationError):
            AgentError(
                agent_name="x",
                error_type="system",  # 不在六选一
                error_message="m",
                occurred_at=VALID_TS,
            )

    def test_occurred_at_naive_raises(self):
        with pytest.raises(ValidationError):
            AgentError(
                agent_name="x",
                error_type="network",
                error_message="m",
                occurred_at="2026-04-17T00:00:00",
            )

    def test_occurred_at_non_utc_raises(self):
        with pytest.raises(ValidationError):
            AgentError(
                agent_name="x",
                error_type="network",
                error_message="m",
                occurred_at="2026-04-17T09:00:00+09:00",
            )

    def test_empty_agent_name_raises(self):
        with pytest.raises(ValidationError):
            AgentError(
                agent_name="",
                error_type="network",
                error_message="m",
                occurred_at=VALID_TS,
            )

    def test_empty_error_message_raises(self):
        with pytest.raises(ValidationError):
            AgentError(
                agent_name="x",
                error_type="network",
                error_message="",
                occurred_at=VALID_TS,
            )
