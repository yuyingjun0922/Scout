"""
tests/test_direction_backfill_agent.py — DirectionBackfillAgent 测试矩阵 (v1.10)

覆盖：
    - 加载待回填：source 过滤 (D1/V1/V3 only)、NULL 过滤、limit、ORDER BY timestamp DESC
    - 单条处理：Gemma 返合法 direction → UPDATE
    - 单条处理：Gemma 返非法 direction → skipped_invalid，留 NULL
    - 单条处理：Gemma 连接失败 → DataMissingError（不 raise，本批降级）
    - 单条处理：Gemma JSON 解析失败 → ParseError，本条 failed，其它继续
    - 单条处理：Gemma 通用异常 → LLMError，本条 failed
    - 内容是 JSON 字符串（gov_cn 风格）→ 提取 title + summary
    - 内容是纯文本（其它 source 风格）→ 直接当 body
    - 内容为空 → 仍能调（title='', body=''）
    - llm_invocations 落库（agent_name=direction_backfill / model / tokens）
    - 批处理：3 条记录，2 succeeded + 1 failed → counters 正确
    - per_direction 按 supportive/restrictive/neutral 分桶
    - S4 / D4 等不在 SOURCES_TO_BACKFILL 的源不会被处理
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from agents.base import BaseAgent, DataMissingError, LLMError, ParseError
from agents.direction_backfill_agent import (
    DEFAULT_BATCH_LIMIT,
    SOURCES_TO_BACKFILL,
    VALID_DIRECTIONS,
    DirectionBackfillAgent,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "backfill_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


# ═══════════════════ FakeOllama mock ═══════════════════


class _FakeResp(dict):
    class _Msg:
        def __init__(self, content: str):
            self.content = content

    def __init__(self, content: str, prompt_tokens: int = 40, eval_tokens: int = 60):
        super().__init__()
        self["message"] = {"content": content}
        self["prompt_eval_count"] = prompt_tokens
        self["eval_count"] = eval_tokens
        self.message = self._Msg(content)
        self.prompt_eval_count = prompt_tokens
        self.eval_count = eval_tokens


class FakeOllama:
    """可注入到 DirectionBackfillAgent(ollama_client=...) 的 mock。

    用法：
        FakeOllama(direction='supportive')
        FakeOllama(raise_on_call=ConnectionError(...))
        FakeOllama(sequence=[{'direction': 'supportive'}, ConnectionError(...), ...])
    """

    def __init__(
        self,
        direction: Optional[str] = None,
        raw_content: Optional[str] = None,
        raise_on_call: Optional[Exception] = None,
        sequence: Optional[List[Any]] = None,
        prompt_tokens: int = 40,
        eval_tokens: int = 60,
    ):
        self.direction = direction
        self.raw_content = raw_content
        self.raise_on_call = raise_on_call
        self.sequence = sequence or []
        self.prompt_tokens = prompt_tokens
        self.eval_tokens = eval_tokens
        self.calls: List[Dict[str, Any]] = []

    def chat(self, *, model, messages, format=None, options=None):
        self.calls.append({
            "model": model,
            "messages": messages,
            "format": format,
            "options": options,
        })
        if self.sequence:
            idx = min(len(self.calls) - 1, len(self.sequence) - 1)
            item = self.sequence[idx]
            if isinstance(item, Exception):
                raise item
            if isinstance(item, str):
                return _FakeResp(item, self.prompt_tokens, self.eval_tokens)
            if isinstance(item, dict):
                return _FakeResp(
                    json.dumps(item, ensure_ascii=False),
                    self.prompt_tokens, self.eval_tokens,
                )
            raise RuntimeError(f"Bad sequence item type: {type(item)}")
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if self.raw_content is not None:
            content = self.raw_content
        elif self.direction is not None:
            content = json.dumps({"direction": self.direction}, ensure_ascii=False)
        else:
            content = json.dumps({"direction": "neutral"}, ensure_ascii=False)
        return _FakeResp(content, self.prompt_tokens, self.eval_tokens)


# ═══════════════════ helpers ═══════════════════

def _utc(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat()


def _seed_unit(
    db: DatabaseManager,
    *,
    uid: str,
    source: str = "D1",
    content: str = "{}",
    direction: Optional[str] = None,
    days_ago: int = 1,
    industries: Optional[List[str]] = None,
):
    db.write(
        """INSERT INTO info_units
           (id, source, source_credibility, timestamp, category, content,
            related_industries, policy_direction, schema_version,
            created_at, updated_at)
           VALUES (?, ?, 'high', ?, 'policy', ?, ?, ?, '1.0', ?, ?)""",
        (
            uid, source, _utc(-days_ago), content,
            json.dumps(industries or [], ensure_ascii=False),
            direction, _utc(), _utc(),
        ),
    )


def _make_agent(db, ollama=None, **kwargs) -> DirectionBackfillAgent:
    return DirectionBackfillAgent(
        db=db,
        ollama_client=ollama if ollama is not None else FakeOllama(direction="neutral"),
        **kwargs,
    )


# ═══════════════════ 模块常量 ═══════════════════


class TestConstants:
    def test_valid_directions(self):
        assert VALID_DIRECTIONS == {"supportive", "restrictive", "neutral"}

    def test_sources_to_backfill(self):
        assert SOURCES_TO_BACKFILL == ("D1", "V1", "V3")

    def test_default_batch_limit_sane(self):
        assert isinstance(DEFAULT_BATCH_LIMIT, int)
        assert 1 <= DEFAULT_BATCH_LIMIT <= 200


# ═══════════════════ _load_pending ═══════════════════


class TestLoadPending:
    def test_only_null_direction(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None)
        _seed_unit(tmp_db, uid="b", direction="supportive")
        agent = _make_agent(tmp_db)
        rows = agent._load_pending(limit=10)
        assert [r["id"] for r in rows] == ["a"]

    def test_source_filter(self, tmp_db):
        for uid, src in [("d1", "D1"), ("v1", "V1"), ("v3", "V3"),
                         ("s4", "S4"), ("d4", "D4")]:
            _seed_unit(tmp_db, uid=uid, source=src, direction=None)
        agent = _make_agent(tmp_db)
        rows = agent._load_pending(limit=10)
        ids = sorted(r["id"] for r in rows)
        assert ids == ["d1", "v1", "v3"]   # S4 / D4 不在 backfill 范围

    def test_limit(self, tmp_db):
        for i in range(5):
            _seed_unit(tmp_db, uid=f"u{i}", direction=None, days_ago=i + 1)
        agent = _make_agent(tmp_db)
        rows = agent._load_pending(limit=3)
        assert len(rows) == 3

    def test_order_by_timestamp_desc(self, tmp_db):
        _seed_unit(tmp_db, uid="old", direction=None, days_ago=10)
        _seed_unit(tmp_db, uid="new", direction=None, days_ago=1)
        _seed_unit(tmp_db, uid="mid", direction=None, days_ago=5)
        agent = _make_agent(tmp_db)
        rows = agent._load_pending(limit=10)
        assert [r["id"] for r in rows] == ["new", "mid", "old"]


# ═══════════════════ _extract_title_body ═══════════════════


class TestExtractTitleBody:
    def test_json_with_title_and_summary(self):
        content = json.dumps({"title": "T", "summary": "S body"}, ensure_ascii=False)
        title, body = DirectionBackfillAgent._extract_title_body(content)
        assert title == "T"
        assert body == "S body"

    def test_json_with_content_field(self):
        content = json.dumps({"title": "T", "content": "main content"}, ensure_ascii=False)
        title, body = DirectionBackfillAgent._extract_title_body(content)
        assert title == "T"
        assert body == "main content"

    def test_plain_text(self):
        title, body = DirectionBackfillAgent._extract_title_body("just plain text")
        assert title == ""
        assert body == "just plain text"

    def test_empty_string(self):
        title, body = DirectionBackfillAgent._extract_title_body("")
        assert title == ""
        assert body == ""

    def test_truncates_long_body(self):
        long_summary = "x" * 1000
        content = json.dumps({"title": "T", "summary": long_summary}, ensure_ascii=False)
        title, body = DirectionBackfillAgent._extract_title_body(content)
        assert len(body) == 500

    def test_json_array_falls_back(self):
        title, body = DirectionBackfillAgent._extract_title_body("[1, 2, 3]")
        assert title == ""
        assert body == "[1, 2, 3]"


# ═══════════════════ _process_one ═══════════════════


class TestProcessOne:
    def test_supportive(self, tmp_db):
        ollama = FakeOllama(direction="supportive")
        agent = _make_agent(tmp_db, ollama=ollama)
        direction, valid = agent._process_one(
            "u1",
            json.dumps({"title": "支持新能源", "summary": "鼓励发展"}, ensure_ascii=False),
        )
        assert direction == "supportive"
        assert valid is True
        assert ollama.calls[0]["format"] == "json"
        assert ollama.calls[0]["options"]["temperature"] == 0.1

    def test_restrictive(self, tmp_db):
        ollama = FakeOllama(direction="restrictive")
        agent = _make_agent(tmp_db, ollama=ollama)
        direction, valid = agent._process_one("u1", "禁止煤炭新增产能")
        assert direction == "restrictive"
        assert valid is True

    def test_neutral(self, tmp_db):
        ollama = FakeOllama(direction="neutral")
        agent = _make_agent(tmp_db, ollama=ollama)
        direction, valid = agent._process_one("u1", "统计口径调整")
        assert direction == "neutral"
        assert valid is True

    def test_invalid_direction_returns_invalid(self, tmp_db):
        ollama = FakeOllama(direction="positive")  # 非法
        agent = _make_agent(tmp_db, ollama=ollama)
        direction, valid = agent._process_one("u1", "x")
        assert valid is False

    def test_uppercase_normalized(self, tmp_db):
        ollama = FakeOllama(direction="SUPPORTIVE")
        agent = _make_agent(tmp_db, ollama=ollama)
        direction, valid = agent._process_one("u1", "x")
        assert direction == "supportive"
        assert valid is True


# ═══════════════════ LLM 调用错误分类（v1.48 抽象层路径）═══════════════════
# v1.48 后 _call_gemma 被拆成 _call_llm_json（连接/LLM 错由 LLMClient 抛）+
# _process_one 里的 JSON 解析（ParseError 由 json.loads 抛）。


class TestCallGemmaErrors:
    def test_connection_error_data_missing(self, tmp_db):
        ollama = FakeOllama(raise_on_call=ConnectionError("Connection refused"))
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        with pytest.raises(DataMissingError):
            agent._call_llm_json("hi")

    def test_timeout_data_missing(self, tmp_db):
        ollama = FakeOllama(raise_on_call=TimeoutError("timed out"))
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        with pytest.raises(DataMissingError):
            agent._call_llm_json("hi")

    def test_generic_exception_llm_error(self, tmp_db):
        ollama = FakeOllama(raise_on_call=RuntimeError("rate limited"))
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        with pytest.raises(LLMError):
            agent._call_llm_json("hi")

    def test_malformed_json_parse_error(self, tmp_db):
        ollama = FakeOllama(raw_content="not json at all")
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        with pytest.raises(ParseError):
            agent._process_one("u1", "content")

    def test_non_object_json_parse_error(self, tmp_db):
        ollama = FakeOllama(raw_content="[1, 2, 3]")
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        with pytest.raises(ParseError):
            agent._process_one("u1", "content")

    def test_retry_then_success(self, tmp_db):
        ollama = FakeOllama(sequence=[
            ConnectionError("transient"),
            {"direction": "neutral"},
        ])
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=1)
        resp = agent._call_llm_json("hi")
        assert json.loads(resp.text) == {"direction": "neutral"}


# ═══════════════════ batch run ═══════════════════


class TestRunBatch:
    def test_three_records_all_supportive(self, tmp_db):
        for i in range(3):
            _seed_unit(
                tmp_db, uid=f"u{i}", direction=None,
                content=json.dumps({"title": f"T{i}", "summary": "x"}, ensure_ascii=False),
                days_ago=i + 1,
            )
        ollama = FakeOllama(direction="supportive")
        agent = _make_agent(tmp_db, ollama=ollama)
        result = agent.run(limit=10)
        assert result["scanned"] == 3
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert result["per_direction"] == {
            "supportive": 3, "restrictive": 0, "neutral": 0
        }
        # 实际写回？
        rows = tmp_db.query(
            "SELECT policy_direction FROM info_units WHERE id IN ('u0','u1','u2')"
        )
        dirs = sorted(r["policy_direction"] for r in rows)
        assert dirs == ["supportive", "supportive", "supportive"]

    def test_mixed_outcomes(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None, content="A", days_ago=1)
        _seed_unit(tmp_db, uid="b", direction=None, content="B", days_ago=2)
        _seed_unit(tmp_db, uid="c", direction=None, content="C", days_ago=3)
        ollama = FakeOllama(sequence=[
            {"direction": "supportive"},   # a
            ConnectionError("down"),       # b → DataMissing → failed (whole batch降级)
            {"direction": "restrictive"},  # c (不会到，因 DataMissing 已 raise 中断 _process_one；
                                           # 但 BaseAgent.run_with_error_handling 包装单条
                                           # 所以 c 正常处理)
        ])
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        result = agent.run(limit=10)
        assert result["scanned"] == 3
        # a 成功；b 失败（DataMissing → BaseAgent return None）；c 成功
        assert result["succeeded"] == 2
        assert result["failed"] == 1

    def test_invalid_direction_skipped_not_failed(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None, content="A", days_ago=1)
        _seed_unit(tmp_db, uid="b", direction=None, content="B", days_ago=2)
        ollama = FakeOllama(sequence=[
            {"direction": "positive"},   # invalid → skipped_invalid
            {"direction": "supportive"},
        ])
        agent = _make_agent(tmp_db, ollama=ollama)
        result = agent.run(limit=10)
        assert result["scanned"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 0
        assert result["skipped_invalid"] == 1
        # 'a' 留 NULL
        a_dir = tmp_db.query(
            "SELECT policy_direction FROM info_units WHERE id='a'"
        )[0]["policy_direction"]
        assert a_dir is None

    def test_empty_universe_no_call(self, tmp_db):
        ollama = FakeOllama(direction="neutral")
        agent = _make_agent(tmp_db, ollama=ollama)
        result = agent.run(limit=10)
        assert result["scanned"] == 0
        assert result["succeeded"] == 0
        assert ollama.calls == []

    def test_s4_records_ignored(self, tmp_db):
        _seed_unit(tmp_db, uid="d1a", source="D1", direction=None)
        _seed_unit(tmp_db, uid="s4a", source="S4", direction=None)
        ollama = FakeOllama(direction="supportive")
        agent = _make_agent(tmp_db, ollama=ollama)
        result = agent.run(limit=10)
        assert result["scanned"] == 1
        assert len(ollama.calls) == 1
        # S4 仍 NULL
        s4_dir = tmp_db.query(
            "SELECT policy_direction FROM info_units WHERE id='s4a'"
        )[0]["policy_direction"]
        assert s4_dir is None


# ═══════════════════ llm_invocations 落库 ═══════════════════


class TestLLMInvocationLogging:
    def test_logged_per_call(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None, content="政策内容", days_ago=1)
        ollama = FakeOllama(direction="supportive", prompt_tokens=80, eval_tokens=20)
        agent = _make_agent(tmp_db, ollama=ollama)
        agent.run(limit=10)
        rows = tmp_db.query("SELECT * FROM llm_invocations")
        assert len(rows) == 1
        r = rows[0]
        assert r["agent_name"] == "direction_backfill"
        assert r["model_name"] == "gemma4:e4b"
        assert r["tokens_used"] == 100
        assert "supportive" in r["output_summary"]

    def test_invalid_direction_still_logged(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None, content="X", days_ago=1)
        ollama = FakeOllama(direction="positive")
        agent = _make_agent(tmp_db, ollama=ollama)
        agent.run(limit=10)
        rows = tmp_db.query("SELECT * FROM llm_invocations")
        # 即使非法，仍记录 invocation（成本统计用）
        assert len(rows) == 1


# ═══════════════════ agent_errors 落库 ═══════════════════


class TestAgentErrorLogging:
    def test_data_missing_logged(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None, content="X", days_ago=1)
        ollama = FakeOllama(raise_on_call=ConnectionError("refused"))
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        agent.run(limit=10)
        rows = tmp_db.query(
            "SELECT * FROM agent_errors WHERE agent_name='direction_backfill'"
        )
        assert len(rows) >= 1
        assert any(r["error_type"] == "data" for r in rows)

    def test_parse_error_logged(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None, content="X", days_ago=1)
        ollama = FakeOllama(raw_content="not json")
        agent = _make_agent(tmp_db, ollama=ollama, max_gemma_retries=0)
        agent.run(limit=10)
        rows = tmp_db.query(
            "SELECT * FROM agent_errors WHERE agent_name='direction_backfill'"
        )
        assert any(r["error_type"] == "parse" for r in rows)


# ═══════════════════ run() 入口 ═══════════════════


class TestRunEntry:
    def test_default_limit_used(self, tmp_db):
        _seed_unit(tmp_db, uid="a", direction=None, content="X", days_ago=1)
        ollama = FakeOllama(direction="neutral")
        agent = _make_agent(tmp_db, ollama=ollama)
        result = agent.run()
        assert result is not None
        assert "ts_utc" in result

    def test_returns_none_on_unknown_exception(self, tmp_db, monkeypatch):
        agent = _make_agent(tmp_db)
        # 强制 _load_pending 抛非 ScoutError
        def boom(*a, **kw):
            raise RuntimeError("synthetic boom")
        monkeypatch.setattr(agent, "_load_pending", boom)
        # unknown 类应 re-raise（fail-loud）
        with pytest.raises(RuntimeError, match="synthetic boom"):
            agent.run(limit=5)
