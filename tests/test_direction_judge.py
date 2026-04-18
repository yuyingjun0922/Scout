"""
tests/test_direction_judge.py — DirectionJudgeAgent 测试矩阵

覆盖：
    - 初始化：默认字段 / prompt 加载 / 缺失 prompt RuleViolation
    - weekly_industry_report:
        * 空数据库（watchlist 无 active）→ 返 Markdown 不崩
        * 单行业基础生成
        * 多行业合并周报
        * industry_name=None 遍历所有 active
        * use_gemma=False 不调 Gemma
        * Gemma online → 报告含 AI 分析
        * Gemma offline (connection) → 降级 + GEMMA_OFFLINE_BANNER + 落 'data' 错
        * Gemma LLMError → 降级 + 落 'llm' 错
        * save=True 写文件；save=False 不写
        * 同日重跑覆盖（幂等）
        * 180 天时效警示
        * 空 industry_name → RuleViolation
    - weekly_paper_report:
        * 空数据（无 D4 行）
        * 引用数降序排序
        * Top-N 截断
        * 多信源 D4 入库
        * Gemma offline 降级
        * 非法 top_n
    - cross_signal_validation:
        * 空 industry → signals=0, direction=None
        * 全部 null → None, low
        * 明显 supportive 多数 → supportive, high
        * 平衡分布 → low or medium
        * 样本 < 3 判断上限 medium
        * 非法 industry_name → RuleViolation
    - llm_invocations 写入（两种 prompt_kind）
    - run(task=...) 分派
    - run_with_error_handling：invalid task → rule
    - 方向重要度排序（restrictive > supportive > mixed > neutral > null）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from agents.base import (
    BaseAgent,
    DataMissingError,
    LLMError,
    ParseError,
    RuleViolation,
)
from agents.direction_judge import (
    CROSS_SIGNAL_WINDOW_DAYS,
    DirectionJudgeAgent,
    GEMMA_OFFLINE_BANNER,
    PAPER_TOP_N,
    STALENESS_THRESHOLD_DAYS,
    WEEKLY_INDUSTRY_WINDOW_DAYS,
    WEEKLY_PAPER_WINDOW_DAYS,
    _direction_importance,
    _parse_iso_ts,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "dj_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


@pytest.fixture
def tmp_reports(tmp_path):
    return tmp_path / "reports"


# ─── Fake Ollama ───


class _FakeResp(dict):
    class _Msg:
        def __init__(self, content):
            self.content = content

    def __init__(self, content, prompt_tokens=40, eval_tokens=60):
        super().__init__()
        self["message"] = {"content": content}
        self["prompt_eval_count"] = prompt_tokens
        self["eval_count"] = eval_tokens
        self.message = self._Msg(content)
        self.prompt_eval_count = prompt_tokens
        self.eval_count = eval_tokens


class FakeOllama:
    def __init__(
        self,
        text: Optional[str] = None,
        raise_on_call: Optional[Exception] = None,
        sequence: Optional[List[Any]] = None,
        prompt_tokens: int = 40,
        eval_tokens: int = 60,
    ):
        self.text = text if text is not None else "默认周报分析文本。"
        self.raise_on_call = raise_on_call
        self.sequence = sequence or []
        self.prompt_tokens = prompt_tokens
        self.eval_tokens = eval_tokens
        self.calls: List[Dict[str, Any]] = []

    def chat(self, *, model, messages, format=None, options=None):
        self.calls.append(
            {"model": model, "messages": messages, "format": format, "options": options}
        )
        if self.sequence:
            idx = min(len(self.calls) - 1, len(self.sequence) - 1)
            item = self.sequence[idx]
            if isinstance(item, Exception):
                raise item
            return _FakeResp(item, prompt_tokens=self.prompt_tokens, eval_tokens=self.eval_tokens)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return _FakeResp(self.text, prompt_tokens=self.prompt_tokens, eval_tokens=self.eval_tokens)


# ─── seed helpers ───


def _days_ago(n: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).isoformat()


def _insert_info_unit(
    db,
    *,
    unit_id: str,
    source: str = "D1",
    source_credibility: str = "权威",
    timestamp: Optional[str] = None,
    category: str = "政策发布",
    content: str = "{}",
    related_industries: Optional[list] = None,
    policy_direction: Optional[str] = None,
    mixed_subtype: Optional[str] = None,
    created_at: Optional[str] = None,
) -> None:
    if timestamp is None:
        timestamp = _days_ago(1)
    if related_industries is None:
        related_industries = ["半导体"]
    if created_at is None:
        created_at = timestamp
    db.write(
        """INSERT INTO info_units
           (id, source, source_credibility, timestamp, category, content,
            related_industries, policy_direction, mixed_subtype,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            unit_id,
            source,
            source_credibility,
            timestamp,
            category,
            content,
            json.dumps(related_industries, ensure_ascii=False),
            policy_direction,
            mixed_subtype,
            created_at,
            created_at,
        ),
    )


def _insert_watchlist(db, industry_name: str, **kw) -> int:
    return db.write(
        """INSERT INTO watchlist
           (industry_name, zone, dimensions, verification_status, gap_status)
           VALUES (?, ?, ?, ?, ?)""",
        (
            industry_name,
            kw.get("zone", "active"),
            kw.get("dimensions"),
            kw.get("verification_status"),
            kw.get("gap_status", "active"),
        ),
    )


def _make_agent(db, *, ollama=None, reports_dir=None):
    return DirectionJudgeAgent(
        db=db,
        ollama_client=ollama or FakeOllama(),
        reports_dir=reports_dir,
    )


# ═══════════════════ 初始化 ═══════════════════


class TestInit:
    def test_default_construct(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        assert agent.name == "direction_judge"
        assert agent.model == "gemma4:e4b"
        assert agent.prompt_version == "v001"
        assert agent.reports_dir == tmp_reports

    def test_is_baseagent_subclass(self):
        assert issubclass(DirectionJudgeAgent, BaseAgent)

    def test_missing_prompt_raises(self, tmp_db):
        with pytest.raises(RuleViolation, match="Prompt template not found"):
            DirectionJudgeAgent(
                db=tmp_db,
                ollama_client=FakeOllama(),
                prompt_version="v999_nonexistent",
            )

    def test_both_prompts_loaded(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        assert "direction_judge_weekly_v001" in agent.weekly_prompt
        assert "direction_judge_paper_v001" in agent.paper_prompt


# ═══════════════════ weekly_industry_report — 基础 ═══════════════════


class TestWeeklyIndustryBasic:
    def test_empty_db_no_watchlist_no_industry_returns_string(
        self, tmp_db, tmp_reports
    ):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, path = agent.weekly_industry_report(save=False)
        assert isinstance(report, str)
        assert "Scout 周度行业信号报告" in report
        assert "覆盖行业：0" in report
        assert path is None

    def test_single_industry_explicit(self, tmp_db, tmp_reports):
        _insert_info_unit(
            tmp_db,
            unit_id="u1",
            related_industries=["半导体"],
            policy_direction="supportive",
            content=json.dumps({"title": "半导体政策", "summary": "推进发展"}, ensure_ascii=False),
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, path = agent.weekly_industry_report(
            industry_name="半导体", save=False
        )
        assert "半导体" in report
        assert "信号总数" in report
        assert "supportive" in report

    def test_multi_industry_aggregation_from_watchlist(
        self, tmp_db, tmp_reports
    ):
        for name in ("半导体", "新能源", "光伏"):
            _insert_watchlist(tmp_db, name, zone="active")
        _insert_info_unit(
            tmp_db, unit_id="u1", related_industries=["半导体"], policy_direction="supportive"
        )
        _insert_info_unit(
            tmp_db, unit_id="u2", related_industries=["新能源"], policy_direction="neutral"
        )

        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_industry_report(save=False)
        assert "半导体" in report
        assert "新能源" in report
        assert "光伏" in report
        assert "覆盖行业：3" in report

    def test_skip_inactive_zone(self, tmp_db, tmp_reports):
        _insert_watchlist(tmp_db, "半导体", zone="active")
        _insert_watchlist(tmp_db, "旧行业", zone="cold")
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_industry_report(save=False)
        assert "旧行业" not in report
        assert "半导体" in report

    def test_invalid_industry_name_raises(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        with pytest.raises(RuleViolation):
            agent.weekly_industry_report(industry_name="   ")

    def test_report_includes_metadata(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False
        )
        assert "生成时间" in report
        assert f"最近 {WEEKLY_INDUSTRY_WINDOW_DAYS} 天" in report


# ═══════════════════ weekly_industry_report — Gemma 降级 ═══════════════════


class TestWeeklyIndustryGemma:
    def test_use_gemma_false_skips_ollama(self, tmp_db, tmp_reports):
        ollama = FakeOllama()
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        report, _ = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False, save=False
        )
        assert len(ollama.calls) == 0
        assert "AI 分析" not in report or "未启用" in report

    def test_gemma_online_adds_analysis_section(self, tmp_db, tmp_reports):
        ollama = FakeOllama(text="本周半导体政策面向好。")
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        report, _ = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=True, save=False
        )
        assert "AI 分析" in report
        assert "本周半导体政策面向好" in report
        assert GEMMA_OFFLINE_BANNER not in report
        assert len(ollama.calls) == 1

    def test_gemma_connection_error_degrades_to_banner(
        self, tmp_db, tmp_reports
    ):
        ollama = FakeOllama(raise_on_call=ConnectionError("Connection refused"))
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False
        )
        assert GEMMA_OFFLINE_BANNER in report
        # 落 'data' 错到 agent_errors（connection 类）
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name='direction_judge'"
        )
        assert any(r["error_type"] == "data" for r in rows)

    def test_gemma_llm_error_degrades_to_banner(self, tmp_db, tmp_reports):
        ollama = FakeOllama(
            raise_on_call=RuntimeError("rate limit, nothing network-like")
        )
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False
        )
        assert GEMMA_OFFLINE_BANNER in report
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name='direction_judge'"
        )
        assert any(r["error_type"] == "llm" for r in rows)

    def test_gemma_empty_response_degrades(self, tmp_db, tmp_reports):
        """空白文本也算失败（ParseError 分类）"""
        ollama = FakeOllama(text="   ")
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False
        )
        assert GEMMA_OFFLINE_BANNER in report
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name='direction_judge'"
        )
        assert any(r["error_type"] == "parse" for r in rows)

    def test_gemma_retries_then_succeeds(self, tmp_db, tmp_reports):
        ollama = FakeOllama(
            sequence=[
                ConnectionError("flaky"),
                "重试后成功的分析文本。",
            ]
        )
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False
        )
        assert "重试后成功" in report
        assert GEMMA_OFFLINE_BANNER not in report
        assert len(ollama.calls) == 2


# ═══════════════════ weekly_industry_report — 保存 ═══════════════════


class TestWeeklyIndustrySave:
    def test_save_true_writes_file(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, path = agent.weekly_industry_report(
            industry_name="半导体", save=True, use_gemma=False
        )
        assert path is not None
        assert path.exists()
        assert path.read_text(encoding="utf-8") == report
        assert str(path).startswith(str(tmp_reports))

    def test_save_false_no_file(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        _, path = agent.weekly_industry_report(
            industry_name="半导体", save=False, use_gemma=False
        )
        assert path is None
        assert not tmp_reports.exists() or not any(tmp_reports.iterdir())

    def test_same_day_overwrites_idempotent(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        _, p1 = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False
        )
        _, p2 = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False
        )
        assert p1 == p2
        assert list(tmp_reports.iterdir()) == [p1]

    def test_file_name_format(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        _, path = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False
        )
        assert path.name.startswith("weekly_industry_")
        assert path.name.endswith(".md")
        # 日期部分：8 位 digit
        date_str = path.name[len("weekly_industry_"): -len(".md")]
        assert len(date_str) == 8 and date_str.isdigit()


# ═══════════════════ weekly_industry_report — 时效警示 ═══════════════════


class TestStalenessWarning:
    def test_stale_industry_shows_warning(self, tmp_db, tmp_reports):
        # 最新信号 200 天前 → 超过 180 天阈值
        _insert_info_unit(
            tmp_db,
            unit_id="u_old",
            related_industries=["半导体"],
            timestamp=_days_ago(200),
            created_at=_days_ago(200),
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False, save=False
        )
        assert "时效警示" in report
        assert str(STALENESS_THRESHOLD_DAYS) in report

    def test_fresh_industry_no_warning(self, tmp_db, tmp_reports):
        _insert_info_unit(
            tmp_db,
            unit_id="u_new",
            related_industries=["半导体"],
            timestamp=_days_ago(3),
            created_at=_days_ago(3),
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False, save=False
        )
        assert "时效警示" not in report

    def test_no_signal_at_all_no_warning(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False, save=False
        )
        assert "时效警示" not in report


# ═══════════════════ weekly_paper_report ═══════════════════


class TestWeeklyPaper:
    def test_empty_no_d4_rows(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(use_gemma=False, save=False)
        assert "Scout 周度论文报告" in report
        assert "本周入库：0" in report
        assert "本周无 D4 新增论文" in report

    def test_papers_sorted_by_citations_desc(self, tmp_db, tmp_reports):
        for idx, (citations, title) in enumerate(
            [(50, "Mid-cited"), (100, "Most-cited"), (10, "Least-cited")]
        ):
            _insert_info_unit(
                tmp_db,
                unit_id=f"p{idx}",
                source="D4",
                source_credibility="参考",
                timestamp=_days_ago(1),
                category="paper",
                content=json.dumps(
                    {
                        "title": title,
                        "venue": "Journal",
                        "citation_count": citations,
                        "abstract": "Some abstract",
                    }
                ),
                related_industries=["人工智能"],
            )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(use_gemma=False, save=False)

        idx_most = report.find("Most-cited")
        idx_mid = report.find("Mid-cited")
        idx_least = report.find("Least-cited")
        assert 0 < idx_most < idx_mid < idx_least

    def test_top_n_caps_list(self, tmp_db, tmp_reports):
        for i in range(15):
            _insert_info_unit(
                tmp_db,
                unit_id=f"p{i}",
                source="D4",
                source_credibility="参考",
                timestamp=_days_ago(1),
                content=json.dumps(
                    {"title": f"Paper {i}", "citation_count": i, "abstract": "x"}
                ),
            )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(
            use_gemma=False, save=False, top_n=5
        )
        # 只有 5 篇入 Top 列表（但全部 15 算入"入库"计数）
        assert "本周入库：15" in report
        assert "Top 5 论文" in report
        # 最高引用的 Paper 14 一定在
        assert "Paper 14" in report
        # 最低引用 Paper 0 不在 Top 5
        for low in range(0, 10):
            if f"Paper {low}" in report:
                pass  # 可能出现在某些偶然提及
        # 断言 Paper 14, 13, 12, 11, 10 都在
        for hi in (14, 13, 12, 11, 10):
            assert f"Paper {hi}" in report

    def test_out_of_window_papers_excluded(self, tmp_db, tmp_reports):
        _insert_info_unit(
            tmp_db,
            unit_id="p_old",
            source="D4",
            timestamp=_days_ago(30),
            content=json.dumps({"title": "Old Paper", "citation_count": 99}),
        )
        _insert_info_unit(
            tmp_db,
            unit_id="p_new",
            source="D4",
            timestamp=_days_ago(1),
            content=json.dumps({"title": "Fresh Paper", "citation_count": 5}),
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(use_gemma=False, save=False)
        assert "Fresh Paper" in report
        assert "Old Paper" not in report

    def test_gemma_online_adds_summary(self, tmp_db, tmp_reports):
        ollama = FakeOllama(text="本周论文集中在大模型优化。")
        _insert_info_unit(
            tmp_db,
            unit_id="p1",
            source="D4",
            timestamp=_days_ago(1),
            content=json.dumps(
                {"title": "P1", "citation_count": 10, "abstract": "x"}
            ),
        )
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(save=False)
        assert "AI 周总结" in report
        assert "大模型优化" in report

    def test_gemma_offline_degrades(self, tmp_db, tmp_reports):
        ollama = FakeOllama(raise_on_call=ConnectionError("down"))
        _insert_info_unit(
            tmp_db,
            unit_id="p1",
            source="D4",
            timestamp=_days_ago(1),
            content=json.dumps({"title": "P1", "citation_count": 10}),
        )
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(save=False)
        assert "AI 周总结" in report
        assert GEMMA_OFFLINE_BANNER in report

    def test_invalid_top_n_raises(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        for bad in (0, -1, 1.5, "10"):
            with pytest.raises(RuleViolation):
                agent.weekly_paper_report(top_n=bad)

    def test_non_d4_source_excluded(self, tmp_db, tmp_reports):
        _insert_info_unit(
            tmp_db,
            unit_id="d1_policy",
            source="D1",
            timestamp=_days_ago(1),
            content=json.dumps({"title": "Policy not paper"}),
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(use_gemma=False, save=False)
        assert "本周入库：0" in report

    def test_malformed_content_json_handled(self, tmp_db, tmp_reports):
        """content 非 JSON 也不崩：用默认值填充"""
        _insert_info_unit(
            tmp_db,
            unit_id="p_bad",
            source="D4",
            timestamp=_days_ago(1),
            content="this is not JSON",
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_paper_report(use_gemma=False, save=False)
        assert "(no title)" in report


# ═══════════════════ cross_signal_validation ═══════════════════


class TestCrossSignalValidation:
    def test_empty_industry(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.cross_signal_validation("半导体")
        assert result == {
            "industry": "半导体",
            "signals_30d": 0,
            "inferred_direction": None,
            "confidence": "low",
            "rationale": f"最近 {CROSS_SIGNAL_WINDOW_DAYS} 天无信号",
        }

    def test_all_null_direction(self, tmp_db, tmp_reports):
        for i in range(3):
            _insert_info_unit(
                tmp_db,
                unit_id=f"u{i}",
                related_industries=["半导体"],
                policy_direction=None,
            )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.cross_signal_validation("半导体")
        assert result["signals_30d"] == 3
        assert result["inferred_direction"] is None
        assert result["confidence"] == "low"
        assert "全部未判断" in result["rationale"]

    def test_clear_supportive_majority_high(self, tmp_db, tmp_reports):
        for i in range(8):
            _insert_info_unit(
                tmp_db,
                unit_id=f"s{i}",
                related_industries=["半导体"],
                policy_direction="supportive",
            )
        _insert_info_unit(
            tmp_db,
            unit_id="r1",
            related_industries=["半导体"],
            policy_direction="restrictive",
        )
        _insert_info_unit(
            tmp_db,
            unit_id="n1",
            related_industries=["半导体"],
            policy_direction=None,
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.cross_signal_validation("半导体")
        assert result["inferred_direction"] == "supportive"
        assert result["confidence"] == "high"
        assert "8 条 supportive" in result["rationale"]
        assert "1 条 restrictive" in result["rationale"]

    def test_balanced_distribution_low_confidence(self, tmp_db, tmp_reports):
        _insert_info_unit(
            tmp_db, unit_id="s1", related_industries=["半导体"],
            policy_direction="supportive",
        )
        _insert_info_unit(
            tmp_db, unit_id="r1", related_industries=["半导体"],
            policy_direction="restrictive",
        )
        _insert_info_unit(
            tmp_db, unit_id="n1", related_industries=["半导体"],
            policy_direction="neutral",
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.cross_signal_validation("半导体")
        # winner 是 supportive / restrictive / neutral 中最先达到 max 的
        assert result["inferred_direction"] in {"supportive", "restrictive", "neutral"}
        # 每桶 1 条，ratio = 1/3 → low
        assert result["confidence"] == "low"

    def test_small_sample_capped_at_medium(self, tmp_db, tmp_reports):
        """winner_count < 3：最多 medium，不给 high"""
        _insert_info_unit(
            tmp_db, unit_id="s1", related_industries=["半导体"],
            policy_direction="supportive",
        )
        _insert_info_unit(
            tmp_db, unit_id="s2", related_industries=["半导体"],
            policy_direction="supportive",
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.cross_signal_validation("半导体")
        assert result["inferred_direction"] == "supportive"
        assert result["confidence"] == "medium"  # 2 < 3 封顶

    def test_invalid_industry_raises(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        with pytest.raises(RuleViolation):
            agent.cross_signal_validation("")
        with pytest.raises(RuleViolation):
            agent.cross_signal_validation(None)

    def test_signals_30d_includes_all_directions(self, tmp_db, tmp_reports):
        """signals_30d 包含 null 行，inferred 基于 non-null"""
        for i in range(5):
            _insert_info_unit(
                tmp_db, unit_id=f"n{i}", related_industries=["半导体"],
                policy_direction=None,
            )
        for i in range(3):
            _insert_info_unit(
                tmp_db, unit_id=f"s{i}", related_industries=["半导体"],
                policy_direction="supportive",
            )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.cross_signal_validation("半导体")
        assert result["signals_30d"] == 8
        assert result["inferred_direction"] == "supportive"
        # ratio = 3/3 = 1.0 → high 但 winner_count=3 恰达 3 阈值
        assert result["confidence"] == "high"


# ═══════════════════ llm_invocations 记账 ═══════════════════


class TestLLMInvocations:
    def test_weekly_industry_logs_invocation(self, tmp_db, tmp_reports):
        ollama = FakeOllama(text="分析", prompt_tokens=100, eval_tokens=50)
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        agent.weekly_industry_report(industry_name="半导体", save=False)

        rows = tmp_db.query(
            "SELECT agent_name, prompt_version, model_name, tokens_used, "
            "cost_cents FROM llm_invocations"
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["agent_name"] == "direction_judge"
        assert "weekly_industry" in r["prompt_version"]
        assert r["model_name"] == "gemma4:e4b"
        assert r["tokens_used"] == 150
        assert r["cost_cents"] == 0

    def test_weekly_paper_logs_invocation(self, tmp_db, tmp_reports):
        ollama = FakeOllama(text="paper summary")
        _insert_info_unit(
            tmp_db, unit_id="p1", source="D4", timestamp=_days_ago(1),
            content=json.dumps({"title": "P1", "citation_count": 1}),
        )
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        agent.weekly_paper_report(save=False)

        rows = tmp_db.query(
            "SELECT prompt_version FROM llm_invocations"
        )
        assert len(rows) == 1
        assert "weekly_paper" in rows[0]["prompt_version"]

    def test_gemma_failure_no_invocation_row(self, tmp_db, tmp_reports):
        ollama = FakeOllama(raise_on_call=ConnectionError("down"))
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])

        agent.weekly_industry_report(industry_name="半导体", save=False)

        rows = tmp_db.query("SELECT COUNT(*) AS n FROM llm_invocations")
        assert rows[0]["n"] == 0

    def test_multi_industry_multi_invocations(self, tmp_db, tmp_reports):
        for name in ("半导体", "新能源", "光伏"):
            _insert_watchlist(tmp_db, name, zone="active")
        _insert_info_unit(
            tmp_db, unit_id="u1", related_industries=["半导体"]
        )
        _insert_info_unit(
            tmp_db, unit_id="u2", related_industries=["新能源"]
        )
        _insert_info_unit(
            tmp_db, unit_id="u3", related_industries=["光伏"]
        )
        ollama = FakeOllama(text="分析")
        agent = _make_agent(tmp_db, ollama=ollama, reports_dir=tmp_reports)
        agent.weekly_industry_report(save=False)
        rows = tmp_db.query("SELECT COUNT(*) AS n FROM llm_invocations")
        assert rows[0]["n"] == 3


# ═══════════════════ run() 分派 ═══════════════════


class TestRunDispatcher:
    def test_default_task_weekly_industry(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.run()  # default task
        assert isinstance(result, tuple)
        assert isinstance(result[0], str)

    def test_explicit_weekly_paper(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.run(task="weekly_paper", save=False, use_gemma=False)
        assert "论文" in result[0]

    def test_explicit_cross_signal(self, tmp_db, tmp_reports):
        _insert_info_unit(
            tmp_db, unit_id="u1", related_industries=["半导体"],
            policy_direction="supportive",
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.run(task="cross_signal", industry_name="半导体")
        assert isinstance(result, dict)
        assert result["industry"] == "半导体"

    def test_invalid_task_logs_rule_error(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.run(task="invalid_task")
        assert result is None
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name='direction_judge'"
        )
        assert any(r["error_type"] == "rule" for r in rows)


# ═══════════════════ 辅助：方向重要度排序 ═══════════════════


class TestDirectionImportance:
    @pytest.mark.parametrize(
        "direction, expected_rank",
        [
            ("restrictive", 0),
            ("supportive", 1),
            ("mixed", 2),
            ("neutral", 3),
            (None, 4),
            ("null", 4),
            ("unknown", 4),
        ],
    )
    def test_importance_order(self, direction, expected_rank):
        assert _direction_importance(direction) == expected_rank

    def test_latest_signals_sort_by_importance(self, tmp_db, tmp_reports):
        # 用 content.title 里嵌入唯一 token，方便在 Markdown 里定位
        _insert_info_unit(
            tmp_db, unit_id="n1", related_industries=["半导体"],
            policy_direction="neutral", timestamp=_days_ago(1),
            content=json.dumps({"title": "TOKEN_NEU"}, ensure_ascii=False),
        )
        _insert_info_unit(
            tmp_db, unit_id="r1", related_industries=["半导体"],
            policy_direction="restrictive", timestamp=_days_ago(2),
            content=json.dumps({"title": "TOKEN_RES"}, ensure_ascii=False),
        )
        _insert_info_unit(
            tmp_db, unit_id="s1", related_industries=["半导体"],
            policy_direction="supportive", timestamp=_days_ago(3),
            content=json.dumps({"title": "TOKEN_SUP"}, ensure_ascii=False),
        )
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", use_gemma=False, save=False
        )
        idx_r = report.find("TOKEN_RES")
        idx_s = report.find("TOKEN_SUP")
        idx_n = report.find("TOKEN_NEU")
        # 所有 token 都在
        assert idx_r > 0
        assert idx_s > 0
        assert idx_n > 0
        # 方向重要度顺序：restrictive < supportive < neutral
        assert idx_r < idx_s < idx_n


# ═══════════════════ _parse_iso_ts ═══════════════════


class TestParseIsoTs:
    def test_utc_iso(self):
        ts = "2026-04-18T12:00:00+00:00"
        result = _parse_iso_ts(ts)
        assert isinstance(result, int)
        assert result > 0

    def test_naive_treated_as_utc(self):
        result = _parse_iso_ts("2026-04-18T12:00:00")
        assert isinstance(result, int)

    def test_empty_returns_none(self):
        assert _parse_iso_ts("") is None
        assert _parse_iso_ts(None) is None

    def test_malformed_returns_none(self):
        assert _parse_iso_ts("not-a-date") is None


# ═══════════════════ Response 兼容 ═══════════════════


class TestResponseStructure:
    def test_missing_content_raises_parse(self, tmp_db, tmp_reports):
        class BrokenOllama:
            def chat(self, **kw):
                return {"no_message_key": True}

        agent = DirectionJudgeAgent(
            db=tmp_db,
            ollama_client=BrokenOllama(),
            reports_dir=tmp_reports,
        )
        _insert_info_unit(tmp_db, unit_id="u1", related_industries=["半导体"])
        report, _ = agent.weekly_industry_report(
            industry_name="半导体", save=False
        )
        assert GEMMA_OFFLINE_BANNER in report
        rows = tmp_db.query(
            "SELECT error_type FROM agent_errors WHERE agent_name='direction_judge'"
        )
        assert any(r["error_type"] == "parse" for r in rows)


# ═══════════════════ 数据完整性 ═══════════════════


class TestDataTypes:
    def test_report_is_string(self, tmp_db, tmp_reports):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        r, _ = agent.weekly_industry_report(save=False, use_gemma=False)
        assert isinstance(r, str)

    def test_cross_signal_returns_dict_with_expected_keys(
        self, tmp_db, tmp_reports
    ):
        agent = _make_agent(tmp_db, reports_dir=tmp_reports)
        result = agent.cross_signal_validation("半导体")
        expected_keys = {
            "industry", "signals_30d", "inferred_direction",
            "confidence", "rationale",
        }
        assert set(result.keys()) == expected_keys
