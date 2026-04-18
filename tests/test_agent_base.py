"""
tests/test_agent_base.py — BaseAgent 错误传播矩阵测试

覆盖：
    - 抽象约束（BaseAgent 不可直接实例化；子类必须 run()；name 非空）
    - 成功路径（返 func 值；不写 agent_errors）
    - network：重试 MAX_RETRIES 次；成功中途返回；失败后落库一行
    - stdlib ConnectionError/TimeoutError 归入 network
    - parse/llm/rule/data：各自 error_type 落库，返 None
    - unknown：re-raise + 告警 + 落库
    - context_data 保留调用方函数名
    - raw 片段保留在 error_message（parse 错契约）
    - DB 写入失败不遮盖原错
"""
import json
import logging

import pytest

from agents.base import (
    BaseAgent,
    DataMissingError,
    LLMError,
    NetworkError,
    ParseError,
    RuleViolation,
    ScoutError,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══ fixtures ═══

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "agent_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """所有测试都跳过真 sleep，跑得飞快"""
    monkeypatch.setattr("time.sleep", lambda s: None)


class _MockAgent(BaseAgent):
    def run(self, *args, **kwargs):
        return "mock_ok"


# ═══ 抽象约束 ═══

class TestAbstractConstraints:
    def test_baseagent_cannot_instantiate_directly(self, tmp_db):
        with pytest.raises(TypeError):
            BaseAgent(name="x", db=tmp_db)

    def test_subclass_without_run_cannot_instantiate(self, tmp_db):
        class Incomplete(BaseAgent):
            pass
        with pytest.raises(TypeError):
            Incomplete(name="x", db=tmp_db)

    def test_empty_name_raises(self, tmp_db):
        with pytest.raises(ValueError, match="name"):
            _MockAgent(name="", db=tmp_db)

    def test_none_db_raises(self):
        with pytest.raises(ValueError, match="db"):
            _MockAgent(name="x", db=None)

    def test_concrete_subclass_instantiates(self, tmp_db):
        agent = _MockAgent(name="mock", db=tmp_db)
        assert agent.name == "mock"
        assert agent.db is tmp_db
        assert agent.run() == "mock_ok"


# ═══ 成功路径 ═══

class TestSuccessPath:
    def test_simple_success_returns_value(self, tmp_db):
        agent = _MockAgent(name="ok", db=tmp_db)
        assert agent.run_with_error_handling(lambda: 42) == 42

    def test_success_with_args_and_kwargs(self, tmp_db):
        agent = _MockAgent(name="ok", db=tmp_db)
        assert agent.run_with_error_handling(lambda a, b: a + b, 3, b=4) == 7

    def test_success_path_writes_nothing_to_agent_errors(self, tmp_db):
        agent = _MockAgent(name="ok", db=tmp_db)
        agent.run_with_error_handling(lambda: "ok")
        rows = tmp_db.query("SELECT COUNT(*) AS n FROM agent_errors")
        assert rows[0]["n"] == 0


# ═══ network 重试 ═══

class TestNetworkRetry:
    def test_total_attempts_is_1_plus_MAX_RETRIES(self, tmp_db):
        """原始调用 1 次 + 重试 MAX_RETRIES 次 = 默认 4 次"""
        calls = []
        def always_fail():
            calls.append(1)
            raise NetworkError("timeout")

        agent = _MockAgent(name="nf", db=tmp_db)
        result = agent.run_with_error_handling(always_fail)

        assert result is None
        assert len(calls) == 1 + agent.MAX_RETRIES  # 4

    def test_succeeds_on_second_retry(self, tmp_db):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise NetworkError("flaky")
            return "success"

        agent = _MockAgent(name="flk", db=tmp_db)
        assert agent.run_with_error_handling(flaky) == "success"
        assert len(calls) == 3

    def test_failure_logs_exactly_one_agent_errors_row(self, tmp_db):
        def always_fail():
            raise NetworkError("still down")

        agent = _MockAgent(name="net", db=tmp_db)
        agent.run_with_error_handling(always_fail)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("net",),
        )
        assert len(rows) == 1
        assert rows[0]["error_type"] == "network"

    @pytest.mark.parametrize("exc_cls", [ConnectionError, TimeoutError])
    def test_stdlib_network_errors_classified_as_network(self, tmp_db, exc_cls):
        def raise_it():
            raise exc_cls("stdlib")

        agent = _MockAgent(name=f"std_{exc_cls.__name__}", db=tmp_db)
        agent.run_with_error_handling(raise_it)

        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?",
            (f"std_{exc_cls.__name__}",),
        )
        assert rows[0]["error_type"] == "network"

    def test_non_network_error_during_retry_handled(self, tmp_db):
        """重试中抛出 ParseError → 按 parse 分支处理"""
        calls = []
        def net_then_parse():
            calls.append(1)
            if len(calls) == 1:
                raise NetworkError("first")
            raise ParseError("then parse")

        agent = _MockAgent(name="flip", db=tmp_db)
        result = agent.run_with_error_handling(net_then_parse)

        assert result is None
        # 最后落的是 parse（非 network）
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name=?", ("flip",),
        )
        # 我们期望只有 parse 一行（重试的 ParseError 直接走 _handle_handled）
        assert any(r["error_type"] == "parse" for r in rows)


# ═══ parse / llm / rule / data：参数化矩阵 ═══

@pytest.mark.parametrize(
    "exc_cls, expected_type",
    [
        (ParseError, "parse"),
        (LLMError, "llm"),
        (RuleViolation, "rule"),
        (DataMissingError, "data"),
    ],
)
def test_handled_error_types_matrix(tmp_db, exc_cls, expected_type):
    """四类非 network 错：各自 error_type 落库，返 None，不 re-raise"""
    def raise_it():
        raise exc_cls(f"test-{expected_type}")

    agent_name = f"h_{expected_type}"
    agent = _MockAgent(name=agent_name, db=tmp_db)
    result = agent.run_with_error_handling(raise_it)

    assert result is None
    rows = tmp_db.query(
        "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
        (agent_name,),
    )
    assert len(rows) == 1
    assert rows[0]["error_type"] == expected_type
    assert f"test-{expected_type}" in rows[0]["error_message"]


# ═══ 未知错误：re-raise ═══

class TestUnknownError:
    def test_unknown_logs_and_reraises(self, tmp_db):
        def boom():
            raise ValueError("weird")

        agent = _MockAgent(name="unk", db=tmp_db)
        with pytest.raises(ValueError):
            agent.run_with_error_handling(boom)

        rows = tmp_db.query(
            "SELECT error_type, error_message FROM agent_errors WHERE agent_name=?",
            ("unk",),
        )
        assert len(rows) == 1
        assert rows[0]["error_type"] == "unknown"
        assert "ValueError" in rows[0]["error_message"]

    def test_unknown_reraise_preserves_exception_class(self, tmp_db):
        class MyCustom(Exception):
            pass

        def raise_my():
            raise MyCustom("custom")

        agent = _MockAgent(name="my", db=tmp_db)
        with pytest.raises(MyCustom):
            agent.run_with_error_handling(raise_my)

    def test_unknown_alert_sent(self, tmp_db, caplog):
        def boom():
            raise RuntimeError("surprise")

        agent = _MockAgent(name="alert", db=tmp_db)
        with caplog.at_level(logging.ERROR, logger="scout.agent.alert"):
            with pytest.raises(RuntimeError):
                agent.run_with_error_handling(boom)
        assert any("[ALERT]" in r.message for r in caplog.records)


# ═══ context_data 写入 ═══

def test_context_data_preserves_func_name(tmp_db):
    def specific_func_name():
        raise ParseError("bad data")

    agent = _MockAgent(name="ctx", db=tmp_db)
    agent.run_with_error_handling(specific_func_name)

    rows = tmp_db.query(
        "SELECT context_data FROM agent_errors WHERE agent_name=?", ("ctx",),
    )
    ctx = json.loads(rows[0]["context_data"])
    assert ctx["func"] == "specific_func_name"


# ═══ raw 保留契约 ═══

def test_parse_error_message_preserves_raw_fragment(tmp_db):
    """ParseError 的 message 应能携带 raw 片段（由调用方放入）"""
    raw = "<!DOCTYPE html>malformed<body"
    def parse_bad():
        raise ParseError(f"bad HTML from: {raw}")

    agent = _MockAgent(name="raw", db=tmp_db)
    agent.run_with_error_handling(parse_bad)

    rows = tmp_db.query(
        "SELECT error_message FROM agent_errors WHERE agent_name=?", ("raw",),
    )
    assert "malformed" in rows[0]["error_message"]


# ═══ 日志写入失败不遮盖原错 ═══

def test_db_failure_during_logging_does_not_cascade(tmp_db, monkeypatch):
    """_log_error 自己抛错时，不应影响 run_with_error_handling 的返回"""
    def bad_write(sql, params):
        raise RuntimeError("db full")

    agent = _MockAgent(name="db_fail", db=tmp_db)
    # 先替换 db.write，再触发 parse
    monkeypatch.setattr(agent.db, "write", bad_write)

    def parse_bad():
        raise ParseError("something")

    # 即使日志失败，也应返 None 而不是抛 RuntimeError
    result = agent.run_with_error_handling(parse_bad)
    assert result is None
