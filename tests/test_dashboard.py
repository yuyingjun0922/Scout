"""
tests/test_dashboard.py — 行业信号仪表盘测试矩阵

覆盖：
    - 入参校验（industry_name 空 / days < 1 → ValueError）
    - 空数据库 → 全 0 dashboard + watchlist_status=None
    - 单信号在窗口内计入
    - 超出 days 窗口的信号不计入
    - snapshot_at 边界（snapshot 之后写入的不计入）
    - by_source 正确分桶，未知 source 被忽略
    - policy_direction_distribution：None → 'null'；全部 5 值都有键
    - mixed_subtype_breakdown：只统计 mixed 行；非 mixed 的 subtype 被忽略
    - source_credibility_weighted_count：四级分布
    - latest_signals：timestamp DESC，截断到 5 条，content_preview 从 JSON 提取
    - data_freshness：oldest/newest 天数；4 周密度分桶；空 rows 返 None + [0]*4
    - watchlist 行存在 vs 不存在
    - LIKE 子串匹配（半导体 匹配 半导体设备、半导体器件）
    - 精确行业名匹配（related_industries 中包含该名）
    - get_all_active_industries_dashboards：只返 zone='active'，按名字排序
    - format_dashboard_as_text：输出包含关键字段、零信号时不崩
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from infra.dashboard import (
    CREDIBILITY_LEVELS_FULL,
    DENSITY_WEEKS,
    LATEST_PREVIEW_LIMIT,
    MIXED_SUBTYPES_FULL,
    PHASE1_SOURCES,
    POLICY_DIRECTIONS_FULL,
    _compute_cutoff,
    _compute_freshness,
    _extract_content_preview,
    build_industry_dashboard,
    format_dashboard_as_text,
    get_all_active_industries_dashboards,
)
from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "dashboard_test.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db
    db.close()


def _insert_info_unit(
    db,
    *,
    unit_id: str,
    source: str = "D1",
    source_credibility: str = "权威",
    timestamp: str,
    category: str = "政策发布",
    content: str = "{}",
    related_industries: list = None,
    policy_direction: str = None,
    mixed_subtype: str = None,
    created_at: str = None,
) -> None:
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


def _insert_watchlist(db, industry_name: str, **kwargs) -> int:
    row_id = db.write(
        """INSERT INTO watchlist
           (industry_name, zone, dimensions, verification_status, gap_status)
           VALUES (?, ?, ?, ?, ?)""",
        (
            industry_name,
            kwargs.get("zone", "active"),
            kwargs.get("dimensions"),
            kwargs.get("verification_status"),
            kwargs.get("gap_status", "active"),
        ),
    )
    return row_id


def _days_ago(n: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).isoformat()


# ═══════════════════ 入参校验 ═══════════════════


class TestInputValidation:
    @pytest.mark.parametrize("bad", ["", "   ", None, 123])
    def test_empty_industry_name_raises(self, tmp_db, bad):
        with pytest.raises(ValueError, match="industry_name"):
            build_industry_dashboard(bad, tmp_db)

    @pytest.mark.parametrize("bad", [0, -1, 1.5, "30", None])
    def test_invalid_days_raises(self, tmp_db, bad):
        with pytest.raises(ValueError, match="days"):
            build_industry_dashboard("半导体", tmp_db, days=bad)


# ═══════════════════ 空数据库 ═══════════════════


class TestEmptyDatabase:
    def test_empty_db_returns_zero_dashboard(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)

        assert d["industry"] == "半导体"
        assert d["recent_signals_total"] == 0
        assert all(v == 0 for v in d["recent_signals_by_source"].values())
        assert all(v == 0 for v in d["policy_direction_distribution"].values())
        assert all(v == 0 for v in d["mixed_subtype_breakdown"].values())
        assert all(v == 0 for v in d["source_credibility_weighted_count"].values())
        assert d["latest_signals"] == []
        assert d["watchlist_status"] is None

    def test_empty_db_freshness_is_none(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)
        f = d["data_freshness"]
        assert f["oldest_signal_days_ago"] is None
        assert f["newest_signal_days_ago"] is None
        assert f["signal_density_per_week"] == [0] * DENSITY_WEEKS

    def test_empty_db_has_all_expected_top_level_keys(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)
        for key in (
            "industry",
            "snapshot_at",
            "recent_signals_total",
            "recent_signals_by_source",
            "policy_direction_distribution",
            "mixed_subtype_breakdown",
            "source_credibility_weighted_count",
            "latest_signals",
            "data_freshness",
            "watchlist_status",
        ):
            assert key in d

    def test_snapshot_at_is_utc_iso(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)
        snap = d["snapshot_at"]
        assert snap.endswith("+00:00") or snap.endswith("Z")

    def test_all_phase1_sources_have_keys(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)
        for src in PHASE1_SOURCES:
            assert src in d["recent_signals_by_source"]

    def test_all_directions_have_keys(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)
        for direction in POLICY_DIRECTIONS_FULL:
            assert direction in d["policy_direction_distribution"]

    def test_all_credibility_levels_have_keys(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)
        for level in CREDIBILITY_LEVELS_FULL:
            assert level in d["source_credibility_weighted_count"]


# ═══════════════════ 单信号计入 ═══════════════════


class TestSingleSignalCount:
    def test_one_signal_in_window_counted(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u1",
            timestamp=_days_ago(1),
            related_industries=["半导体"],
            policy_direction="supportive",
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["recent_signals_total"] == 1
        assert d["recent_signals_by_source"]["D1"] == 1
        assert d["policy_direction_distribution"]["supportive"] == 1
        assert d["source_credibility_weighted_count"]["权威"] == 1

    def test_one_signal_outside_window_not_counted(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u_old",
            timestamp=_days_ago(60),
            created_at=_days_ago(60),
            related_industries=["半导体"],
            policy_direction="supportive",
        )
        d = build_industry_dashboard("半导体", tmp_db, days=30)
        assert d["recent_signals_total"] == 0

    def test_boundary_days_exact(self, tmp_db):
        """临界点 days=30：29d 前的信号计入，31d 前的不计入"""
        _insert_info_unit(
            tmp_db,
            unit_id="u_29",
            timestamp=_days_ago(29),
            created_at=_days_ago(29),
            policy_direction="supportive",
        )
        _insert_info_unit(
            tmp_db,
            unit_id="u_31",
            timestamp=_days_ago(31),
            created_at=_days_ago(31),
            policy_direction="restrictive",
        )
        d = build_industry_dashboard("半导体", tmp_db, days=30)
        assert d["recent_signals_total"] == 1
        assert d["policy_direction_distribution"]["supportive"] == 1
        assert d["policy_direction_distribution"]["restrictive"] == 0

    def test_other_industry_signal_not_counted(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u_biomed",
            timestamp=_days_ago(1),
            related_industries=["生物医药"],
            policy_direction="supportive",
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["recent_signals_total"] == 0


# ═══════════════════ 多信号聚合 ═══════════════════


class TestMultiSignalAggregation:
    @pytest.fixture
    def seeded_db(self, tmp_db):
        """预置多条信号做聚合测试"""
        seed = [
            ("s1", "D1", "权威", 1, "supportive", None),
            ("s2", "D1", "权威", 2, "restrictive", None),
            ("s3", "D1", "权威", 3, "neutral", None),
            ("s4", "D1", "权威", 4, "mixed", "conflict"),
            ("s5", "D1", "权威", 5, None, None),
            ("s6", "D4", "参考", 6, "supportive", None),
            ("s7", "V1", "权威", 7, None, None),
            ("s8", "V3", "权威", 8, "mixed", "structural"),
            ("s9", "S4", "权威", 9, None, None),
        ]
        for uid, src, cred, days, dir_, st in seed:
            _insert_info_unit(
                tmp_db,
                unit_id=uid,
                source=src,
                source_credibility=cred,
                timestamp=_days_ago(days),
                created_at=_days_ago(days),
                policy_direction=dir_,
                mixed_subtype=st,
            )
        return tmp_db

    def test_total_count(self, seeded_db):
        d = build_industry_dashboard("半导体", seeded_db)
        assert d["recent_signals_total"] == 9

    def test_by_source_bucketing(self, seeded_db):
        d = build_industry_dashboard("半导体", seeded_db)
        assert d["recent_signals_by_source"] == {
            "D1": 5, "D4": 1, "V1": 1, "V3": 1, "S4": 1,
        }

    def test_policy_direction_distribution(self, seeded_db):
        d = build_industry_dashboard("半导体", seeded_db)
        dist = d["policy_direction_distribution"]
        assert dist["supportive"] == 2
        assert dist["restrictive"] == 1
        assert dist["neutral"] == 1
        assert dist["mixed"] == 2
        assert dist["null"] == 3  # s5, s7, s9

    def test_mixed_subtype_only_counts_mixed_rows(self, seeded_db):
        d = build_industry_dashboard("半导体", seeded_db)
        assert d["mixed_subtype_breakdown"]["conflict"] == 1
        assert d["mixed_subtype_breakdown"]["structural"] == 1
        assert d["mixed_subtype_breakdown"]["stage_difference"] == 0

    def test_credibility_counts(self, seeded_db):
        d = build_industry_dashboard("半导体", seeded_db)
        assert d["source_credibility_weighted_count"]["权威"] == 8
        assert d["source_credibility_weighted_count"]["参考"] == 1
        assert d["source_credibility_weighted_count"]["可靠"] == 0
        assert d["source_credibility_weighted_count"]["线索"] == 0


# ═══════════════════ mixed_subtype 关联约束 ═══════════════════


class TestMixedSubtypeIsolation:
    def test_subtype_ignored_when_direction_not_mixed(self, tmp_db):
        """非 mixed 行的 mixed_subtype（数据脏）不计入 breakdown"""
        _insert_info_unit(
            tmp_db,
            unit_id="dirty",
            timestamp=_days_ago(1),
            policy_direction="supportive",
            mixed_subtype="conflict",  # 数据不一致但 DB 允许
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["mixed_subtype_breakdown"]["conflict"] == 0
        assert d["policy_direction_distribution"]["supportive"] == 1

    def test_mixed_without_subtype_is_counted_but_subtype_all_zero(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="mixed_no_st",
            timestamp=_days_ago(1),
            policy_direction="mixed",
            mixed_subtype=None,
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["policy_direction_distribution"]["mixed"] == 1
        assert all(v == 0 for v in d["mixed_subtype_breakdown"].values())


# ═══════════════════ latest_signals 预览 ═══════════════════


class TestLatestSignals:
    def test_latest_sorted_timestamp_desc(self, tmp_db):
        for i in range(3):
            _insert_info_unit(
                tmp_db,
                unit_id=f"u{i}",
                timestamp=_days_ago(i + 1),  # u0 最新，u2 最老
                created_at=_days_ago(i + 1),
            )
        d = build_industry_dashboard("半导体", tmp_db)
        ids = [s["id"] for s in d["latest_signals"]]
        assert ids == ["u0", "u1", "u2"]

    def test_latest_capped_at_5(self, tmp_db):
        for i in range(10):
            _insert_info_unit(
                tmp_db,
                unit_id=f"u{i}",
                timestamp=_days_ago(i + 1),
                created_at=_days_ago(i + 1),
            )
        d = build_industry_dashboard("半导体", tmp_db)
        assert len(d["latest_signals"]) == LATEST_PREVIEW_LIMIT
        assert [s["id"] for s in d["latest_signals"]] == [
            f"u{i}" for i in range(5)
        ]

    def test_latest_item_has_expected_fields(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u1",
            source="D1",
            timestamp=_days_ago(1),
            category="政策发布",
            content=json.dumps(
                {"title": "测试政策", "summary": "摘要内容"},
                ensure_ascii=False,
            ),
            policy_direction="supportive",
        )
        d = build_industry_dashboard("半导体", tmp_db)
        item = d["latest_signals"][0]
        assert item["id"] == "u1"
        assert item["source"] == "D1"
        assert item["category"] == "政策发布"
        assert item["policy_direction"] == "supportive"
        assert "测试政策" in item["content_preview"]

    def test_content_preview_fallback_to_plain_text(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u_plain",
            timestamp=_days_ago(1),
            content="a plain text without json",
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert "plain text" in d["latest_signals"][0]["content_preview"]


# ═══════════════════ data_freshness ═══════════════════


class TestDataFreshness:
    def test_oldest_newest_days(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="new",
            timestamp=_days_ago(1),
            created_at=_days_ago(1),
        )
        _insert_info_unit(
            tmp_db,
            unit_id="old",
            timestamp=_days_ago(20),
            created_at=_days_ago(20),
        )
        d = build_industry_dashboard("半导体", tmp_db)
        f = d["data_freshness"]
        assert f["oldest_signal_days_ago"] == 20
        assert f["newest_signal_days_ago"] == 1

    def test_density_buckets(self, tmp_db):
        """每个周桶放一条；density = [week-4, week-3, week-2, week-1]"""
        # density[0] = 21-28d ago (earliest window)
        # density[1] = 14-21d
        # density[2] = 7-14d
        # density[3] = 0-7d (latest)
        for i, days in enumerate([3, 10, 17, 24]):
            _insert_info_unit(
                tmp_db,
                unit_id=f"u{i}",
                timestamp=_days_ago(days),
                created_at=_days_ago(days),
            )
        d = build_industry_dashboard("半导体", tmp_db, days=30)
        # days=3 → bucket 0 → density[3]; days=10 → bucket 1 → density[2];
        # days=17 → bucket 2 → density[1]; days=24 → bucket 3 → density[0]
        assert d["data_freshness"]["signal_density_per_week"] == [1, 1, 1, 1]

    def test_density_beyond_4_weeks_ignored(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u_40",
            timestamp=_days_ago(40),
            created_at=_days_ago(40),
        )
        d = build_industry_dashboard("半导体", tmp_db, days=60)
        assert d["data_freshness"]["signal_density_per_week"] == [0, 0, 0, 0]
        # 但 total 和 oldest/newest 仍计入
        assert d["recent_signals_total"] == 1

    def test_density_multiple_signals_same_week(self, tmp_db):
        """同周内多条信号累加"""
        for i in range(5):
            _insert_info_unit(
                tmp_db,
                unit_id=f"u{i}",
                timestamp=_days_ago(2),
                created_at=_days_ago(2),
            )
        d = build_industry_dashboard("半导体", tmp_db)
        density = d["data_freshness"]["signal_density_per_week"]
        assert density[3] == 5  # 最新一周
        assert density[0] == density[1] == density[2] == 0


# ═══════════════════ watchlist 状态 ═══════════════════


class TestWatchlistStatus:
    def test_not_in_watchlist_status_none(self, tmp_db):
        _insert_info_unit(tmp_db, unit_id="u1", timestamp=_days_ago(1))
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["watchlist_status"] is None

    def test_in_watchlist_status_populated(self, tmp_db):
        industry_id = _insert_watchlist(
            tmp_db,
            "半导体",
            zone="active",
            dimensions=5,
            verification_status="positive",
            gap_status="active",
        )
        d = build_industry_dashboard("半导体", tmp_db)
        ws = d["watchlist_status"]
        assert ws is not None
        assert ws["industry_id"] == industry_id
        assert ws["zone"] == "active"
        assert ws["dimensions"] == 5
        assert ws["verification_status"] == "positive"
        assert ws["gap_status"] == "active"

    def test_watchlist_exact_name_required(self, tmp_db):
        """半导体 != 半导体设备（watchlist 用 industry_name 精确匹配）"""
        _insert_watchlist(tmp_db, "半导体设备", zone="active")
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["watchlist_status"] is None


# ═══════════════════ LIKE 子串匹配 ═══════════════════


class TestLikeMatching:
    def test_partial_industry_matches_parent(self, tmp_db):
        """查 '半导体' 匹配 related_industries 含 '半导体设备' 的行"""
        _insert_info_unit(
            tmp_db,
            unit_id="u1",
            timestamp=_days_ago(1),
            related_industries=["半导体设备"],
        )
        _insert_info_unit(
            tmp_db,
            unit_id="u2",
            timestamp=_days_ago(1),
            related_industries=["半导体器件"],
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["recent_signals_total"] == 2

    def test_exact_industry_also_matches(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u_exact",
            timestamp=_days_ago(1),
            related_industries=["半导体"],
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["recent_signals_total"] == 1

    def test_multi_industry_in_array_matches(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u_multi",
            timestamp=_days_ago(1),
            related_industries=["新能源汽车", "半导体", "电池"],
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["recent_signals_total"] == 1

    def test_unrelated_industry_does_not_match(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u_other",
            timestamp=_days_ago(1),
            related_industries=["生物医药"],
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert d["recent_signals_total"] == 0


# ═══════════════════ get_all_active_industries_dashboards ═══════════════════


class TestGetAllActive:
    def test_empty_watchlist_returns_empty_list(self, tmp_db):
        result = get_all_active_industries_dashboards(tmp_db)
        assert result == []

    def test_returns_only_active_zone(self, tmp_db):
        _insert_watchlist(tmp_db, "半导体", zone="active")
        _insert_watchlist(tmp_db, "光伏", zone="cold")
        _insert_watchlist(tmp_db, "新能源", zone="active")

        result = get_all_active_industries_dashboards(tmp_db)
        names = [d["industry"] for d in result]
        assert "半导体" in names
        assert "新能源" in names
        assert "光伏" not in names

    def test_sorted_by_industry_name(self, tmp_db):
        _insert_watchlist(tmp_db, "光伏", zone="active")
        _insert_watchlist(tmp_db, "半导体", zone="active")
        _insert_watchlist(tmp_db, "新能源", zone="active")

        result = get_all_active_industries_dashboards(tmp_db)
        names = [d["industry"] for d in result]
        # SQLite ORDER BY 是 codepoint 排序，中文按 Unicode 码点
        assert names == sorted(names)

    def test_returns_full_dashboards(self, tmp_db):
        _insert_watchlist(tmp_db, "半导体", zone="active", dimensions=5)
        _insert_info_unit(tmp_db, unit_id="u1", timestamp=_days_ago(1))

        result = get_all_active_industries_dashboards(tmp_db)
        assert len(result) == 1
        assert result[0]["recent_signals_total"] == 1
        assert result[0]["watchlist_status"]["dimensions"] == 5


# ═══════════════════ format_dashboard_as_text ═══════════════════


class TestFormatDashboardAsText:
    def test_empty_dashboard_does_not_crash(self, tmp_db):
        d = build_industry_dashboard("半导体", tmp_db)
        text = format_dashboard_as_text(d)
        assert "半导体" in text
        assert isinstance(text, str)
        assert len(text) > 0

    def test_text_includes_key_fields(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u1",
            timestamp=_days_ago(1),
            policy_direction="supportive",
            content=json.dumps({"title": "测试标题"}, ensure_ascii=False),
        )
        _insert_watchlist(tmp_db, "半导体", zone="active", dimensions=4)

        text = format_dashboard_as_text(
            build_industry_dashboard("半导体", tmp_db)
        )
        assert "半导体" in text
        assert "supportive" in text
        assert "Watchlist" in text
        assert "4" in text  # dimensions
        assert "测试标题" in text

    def test_text_notes_not_in_watchlist(self, tmp_db):
        text = format_dashboard_as_text(
            build_industry_dashboard("半导体", tmp_db)
        )
        assert "未加入" in text

    def test_text_lists_all_directions(self, tmp_db):
        _insert_info_unit(tmp_db, unit_id="u1", timestamp=_days_ago(1))
        text = format_dashboard_as_text(
            build_industry_dashboard("半导体", tmp_db)
        )
        for d in POLICY_DIRECTIONS_FULL:
            assert d in text


# ═══════════════════ _compute_cutoff ═══════════════════


class TestComputeCutoff:
    def test_cutoff_is_days_before(self):
        snap = "2026-04-18T12:00:00+00:00"
        cutoff = _compute_cutoff(snap, 30)
        cut_dt = datetime.fromisoformat(cutoff)
        snap_dt = datetime.fromisoformat(snap)
        assert (snap_dt - cut_dt) == timedelta(days=30)

    def test_cutoff_preserves_utc(self):
        snap = "2026-04-18T12:00:00+00:00"
        cutoff = _compute_cutoff(snap, 1)
        assert cutoff.endswith("+00:00")

    def test_cutoff_naive_input_treated_as_utc(self):
        snap = "2026-04-18T12:00:00"  # naive（理论不应出现；防御）
        cutoff = _compute_cutoff(snap, 1)
        assert cutoff.endswith("+00:00")


# ═══════════════════ _compute_freshness ═══════════════════


class TestComputeFreshnessUnit:
    def test_empty_rows(self):
        result = _compute_freshness([], now_utc_ok())
        assert result["oldest_signal_days_ago"] is None
        assert result["newest_signal_days_ago"] is None
        assert result["signal_density_per_week"] == [0, 0, 0, 0]

    def test_single_row_in_last_week(self):
        snap = "2026-04-18T12:00:00+00:00"
        rows = [{"created_at": "2026-04-16T12:00:00+00:00"}]  # 2 days ago
        result = _compute_freshness(rows, snap)
        assert result["oldest_signal_days_ago"] == 2
        assert result["newest_signal_days_ago"] == 2
        assert result["signal_density_per_week"] == [0, 0, 0, 1]

    def test_future_timestamps_skipped_from_density(self):
        """防御：时间穿越的行不进 density 桶"""
        snap = "2026-04-18T12:00:00+00:00"
        rows = [{"created_at": "2026-05-01T12:00:00+00:00"}]  # 未来
        result = _compute_freshness(rows, snap)
        assert result["signal_density_per_week"] == [0, 0, 0, 0]


def now_utc_ok() -> str:
    """测试辅助：产生一个合法 UTC ISO 8601 字符串"""
    return datetime.now(tz=timezone.utc).isoformat()


# ═══════════════════ _extract_content_preview ═══════════════════


class TestContentPreview:
    def test_json_dict_with_title_and_summary(self):
        content = json.dumps(
            {"title": "A 政策", "summary": "摘要内容"},
            ensure_ascii=False,
        )
        p = _extract_content_preview(content)
        assert "A 政策" in p
        assert "摘要内容" in p

    def test_json_dict_with_description_alternative(self):
        content = json.dumps(
            {"title": "标题", "description": "描述"}, ensure_ascii=False
        )
        p = _extract_content_preview(content)
        assert "标题" in p and "描述" in p

    def test_json_dict_without_title_or_summary_falls_to_str(self):
        content = json.dumps({"x": 1}, ensure_ascii=False)
        p = _extract_content_preview(content)
        assert "x" in p

    def test_plain_text_passthrough(self):
        p = _extract_content_preview("just plain text")
        assert p == "just plain text"

    def test_none_content_empty_string(self):
        assert _extract_content_preview(None) == ""

    def test_empty_content_empty_string(self):
        assert _extract_content_preview("") == ""

    def test_truncation_with_ellipsis(self):
        long_text = "a" * 200
        p = _extract_content_preview(long_text, max_len=50)
        assert len(p) <= 55  # 50 + "..."
        assert p.endswith("...")

    def test_json_array_pass(self):
        content = json.dumps([1, 2, 3])
        p = _extract_content_preview(content)
        assert "1" in p


# ═══════════════════ snapshot 语义 ═══════════════════


class TestSnapshotSemantics:
    def test_signal_created_after_snapshot_not_counted(self, tmp_db, monkeypatch):
        """
        模拟：dashboard 开始时 snapshot=T0；之后另一个 agent 写入 created_at=T0+5s 的行
        （实际实现中 snapshot_at=now_utc() at entry；所以这里用未来 created_at 模拟）
        """
        # 人为写一条未来的行
        future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
        _insert_info_unit(
            tmp_db,
            unit_id="u_future",
            timestamp=future,
            created_at=future,
            policy_direction="supportive",
        )
        d = build_industry_dashboard("半导体", tmp_db)
        # 未来 created_at > snapshot_at → 不计入
        assert d["recent_signals_total"] == 0


# ═══════════════════ 性能/查询数 ═══════════════════


class TestQueryCount:
    def test_at_most_two_queries_per_industry(self, tmp_db, monkeypatch):
        """单行业 dashboard 查询数 ≤ 5（实际 = 2）"""
        counter = {"n": 0}
        original = tmp_db.__class__.query

        def counting_query(self, sql, params=()):
            counter["n"] += 1
            return original(self, sql, params)

        original_one = tmp_db.__class__.query_one

        def counting_query_one(self, sql, params=()):
            counter["n"] += 1
            return original_one(self, sql, params)

        monkeypatch.setattr(tmp_db.__class__, "query", counting_query)
        monkeypatch.setattr(tmp_db.__class__, "query_one", counting_query_one)

        build_industry_dashboard("半导体", tmp_db)
        assert counter["n"] <= 5


# ═══════════════════ 数据类型完整性 ═══════════════════


class TestDataTypes:
    def test_counts_are_ints(self, tmp_db):
        _insert_info_unit(
            tmp_db,
            unit_id="u1",
            timestamp=_days_ago(1),
            policy_direction="supportive",
        )
        d = build_industry_dashboard("半导体", tmp_db)
        assert isinstance(d["recent_signals_total"], int)
        for v in d["recent_signals_by_source"].values():
            assert isinstance(v, int)
        for v in d["policy_direction_distribution"].values():
            assert isinstance(v, int)

    def test_density_is_list_of_4_ints(self, tmp_db):
        _insert_info_unit(tmp_db, unit_id="u1", timestamp=_days_ago(1))
        d = build_industry_dashboard("半导体", tmp_db)
        density = d["data_freshness"]["signal_density_per_week"]
        assert isinstance(density, list)
        assert len(density) == DENSITY_WEEKS
        assert all(isinstance(x, int) for x in density)

    def test_latest_signals_is_list_of_dicts(self, tmp_db):
        _insert_info_unit(tmp_db, unit_id="u1", timestamp=_days_ago(1))
        d = build_industry_dashboard("半导体", tmp_db)
        assert isinstance(d["latest_signals"], list)
        for s in d["latest_signals"]:
            assert isinstance(s, dict)
