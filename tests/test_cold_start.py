"""
tests/test_cold_start.py — 冷启动脚本测试

覆盖：
    - YAML 加载：合法 / 缺失字段（default 空 list）/ 不存在 / 非 mapping
    - 校验：industries/holdings/principles 各类非法输入
    - 预览：preview 输出不崩（smoke）
    - seed_industries：
        * 插入 15 条 → watchlist 正确
        * 重跑：同 name → UPDATE（非重复插入）
        * zone=observation 保留
        * sub_industries/motivation_tags → JSON
        * primary_market 并入 notes
        * dry_run 不写 DB
    - seed_holdings：插入 / 重跑 UPDATE / market 合法 / 字段映射正确
    - seed_principles：非空时 INSERT OR REPLACE；空 list 时跳过不覆盖
    - run_cold_start：完整流程
    - 交互：prompt_holdings / prompt_principles 从注入的 stdin 读
    - CLI main：--dry-run 不写；--yes 跳过确认
"""
from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import pytest
import yaml

from infra.db_manager import DatabaseManager
from knowledge.init_db import init_database
from scripts.cold_start import (
    MAX_PRINCIPLES,
    SYSTEM_META_PRINCIPLES_KEY,
    SYSTEM_META_USER_CONTEXT_KEY,
    VALID_MARKETS,
    VALID_ZONES,
    load_config,
    main as cold_start_main,
    print_preview,
    print_result,
    prompt_holdings,
    prompt_principles,
    run_cold_start,
    seed_holdings,
    seed_industries,
    seed_principles,
    seed_user_context,
    validate_holdings,
    validate_industries,
    validate_principles,
    validate_user_context,
)


# ═══════════════════ fixtures ═══════════════════


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "knowledge.db"
    init_database(db_path)
    db = DatabaseManager(db_path)
    yield db, db_path
    db.close()


@pytest.fixture
def minimal_yaml(tmp_path):
    content = textwrap.dedent(
        """\
        industries:
          - name: "半导体设备"
            sub_industries: ["光刻", "刻蚀"]
            zone: "active"
            motivation_tags: ["1_国家安全"]
            primary_market: "CN+KR+US"
            notes: "test"
          - name: "人形机器人"
            sub_industries: ["整机"]
            zone: "observation"
            motivation_tags: ["新兴"]
            primary_market: "CN+US"
            notes: "观察"

        holdings: []

        principles: []
        """
    )
    path = tmp_path / "conf.yaml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def full_yaml(tmp_path):
    """完整 fixture：2 行业 + 1 持仓 + 2 经验"""
    content = textwrap.dedent(
        """\
        industries:
          - name: "半导体设备"
            sub_industries: ["光刻"]
            zone: "active"
            motivation_tags: ["1_国家安全"]
            primary_market: "CN"
            notes: "A"

          - name: "AI算力"
            zone: "active"
            motivation_tags: ["窗口博弈"]
            primary_market: "US"

        holdings:
          - stock: "600519"
            market: "A"
            company_name: "贵州茅台"
            industry: "白酒"
            shares: 100
            buy_price: 1500.0
            buy_date: "2024-05-15"

        principles:
          - "永远不在连续上涨的第二天买入"
          - "仓位 > 70% 时单只新增不超 5%"
        """
    )
    path = tmp_path / "full.yaml"
    path.write_text(content, encoding="utf-8")
    return path


# ═══════════════════ load_config ═══════════════════


class TestLoadConfig:
    def test_basic(self, minimal_yaml):
        cfg = load_config(minimal_yaml)
        assert len(cfg["industries"]) == 2
        assert cfg["holdings"] == []
        assert cfg["principles"] == []

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.yaml")

    def test_non_mapping_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_config(path)

    def test_missing_sections_default_empty(self, tmp_path):
        path = tmp_path / "partial.yaml"
        path.write_text("industries: []\n", encoding="utf-8")
        cfg = load_config(path)
        assert cfg["holdings"] == []
        assert cfg["principles"] == []

    def test_real_config_loads(self):
        cfg = load_config(
            Path(__file__).resolve().parent.parent
            / "scripts" / "cold_start_config.yaml"
        )
        assert len(cfg["industries"]) == 15


# ═══════════════════ validate_* ═══════════════════


class TestValidateIndustries:
    def test_valid_minimal(self):
        assert validate_industries([{"name": "A"}]) == []

    def test_missing_name(self):
        errs = validate_industries([{"zone": "active"}])
        assert any("name" in e for e in errs)

    def test_empty_name(self):
        errs = validate_industries([{"name": "  "}])
        assert any("name" in e for e in errs)

    def test_invalid_zone(self):
        errs = validate_industries([{"name": "A", "zone": "weird"}])
        assert any("zone" in e for e in errs)

    @pytest.mark.parametrize("zone", sorted(VALID_ZONES))
    def test_valid_zones(self, zone):
        errs = validate_industries([{"name": "A", "zone": zone}])
        assert errs == []

    def test_sub_industries_must_be_list(self):
        errs = validate_industries([{"name": "A", "sub_industries": "not-list"}])
        assert any("sub_industries" in e for e in errs)

    def test_motivation_tags_must_be_list(self):
        errs = validate_industries([{"name": "A", "motivation_tags": "not-list"}])
        assert any("motivation_tags" in e for e in errs)


class TestValidateHoldings:
    def test_valid_minimal(self):
        errs = validate_holdings([{
            "stock": "600519", "market": "A",
            "shares": 100, "buy_price": 1500.0,
        }])
        assert errs == []

    def test_missing_stock(self):
        errs = validate_holdings([{"market": "A"}])
        assert any("stock" in e for e in errs)

    def test_invalid_market(self):
        errs = validate_holdings([{"stock": "X", "market": "JP"}])
        assert any("market" in e for e in errs)

    def test_shares_must_be_int(self):
        errs = validate_holdings([{"stock": "X", "market": "A", "shares": "100"}])
        assert any("shares" in e for e in errs)

    def test_price_must_be_number(self):
        errs = validate_holdings([{"stock": "X", "market": "A", "buy_price": "500"}])
        assert any("buy_price" in e for e in errs)


class TestValidatePrinciples:
    def test_empty_ok(self):
        assert validate_principles([]) == []

    def test_max_limit(self):
        errs = validate_principles([f"p{i}" for i in range(MAX_PRINCIPLES + 1)])
        assert any("max" in e.lower() for e in errs)

    def test_empty_string_rejected(self):
        errs = validate_principles(["ok", ""])
        assert any("principle[1]" in e for e in errs)

    def test_accepts_dict_form(self):
        errs = validate_principles([
            {"id": "P1", "title": "T1", "core": "c"},
            {"title": "T2"},
        ])
        assert errs == []

    def test_dict_without_title_rejected(self):
        errs = validate_principles([{"id": "P1", "core": "missing title"}])
        assert any("title" in e for e in errs)

    def test_dict_with_empty_title_rejected(self):
        errs = validate_principles([{"title": "  "}])
        assert any("title" in e for e in errs)

    def test_mixed_str_and_dict(self):
        errs = validate_principles([
            "简单经验",
            {"title": "结构化原则", "core": "c"},
        ])
        assert errs == []

    def test_non_str_non_dict_rejected(self):
        errs = validate_principles([42])
        assert any("must be str or dict" in e for e in errs)

    def test_non_list_rejected(self):
        errs = validate_principles("not a list")
        assert any("must be list" in e for e in errs)


class TestValidateUserContext:
    def test_none_ok(self):
        assert validate_user_context(None) == []

    def test_empty_dict_ok(self):
        assert validate_user_context({}) == []

    def test_full_dict_ok(self):
        ctx = {
            "investor_type": "长期价值投资者",
            "markets": ["A", "KR", "US"],
            "capital_range": "₩1-3亿",
        }
        assert validate_user_context(ctx) == []

    def test_non_mapping_rejected(self):
        errs = validate_user_context("not a mapping")
        assert any("mapping" in e for e in errs)

    def test_non_json_serializable_rejected(self):
        class _X:
            pass
        errs = validate_user_context({"bad": _X()})
        assert any("JSON" in e or "serial" in e for e in errs)


# ═══════════════════ seed_industries ═══════════════════


class TestSeedIndustries:
    def test_insert_all(self, tmp_db):
        db, _ = tmp_db
        result = seed_industries(db, [
            {"name": "半导体设备", "zone": "active",
             "sub_industries": ["光刻", "刻蚀"],
             "motivation_tags": ["1_国家安全"],
             "primary_market": "CN+KR+US",
             "notes": "test-A"},
            {"name": "人形机器人", "zone": "observation",
             "sub_industries": ["整机"],
             "motivation_tags": ["新兴"],
             "primary_market": "CN+US"},
        ])
        assert result == {"inserted": 2, "updated": 0}

        rows = db.query("SELECT * FROM watchlist ORDER BY industry_name")
        assert len(rows) == 2

        # zone 保留
        zones = {r["industry_name"]: r["zone"] for r in rows}
        assert zones["半导体设备"] == "active"
        assert zones["人形机器人"] == "observation"

    def test_rerun_updates_not_duplicates(self, tmp_db):
        db, _ = tmp_db
        payload = [{
            "name": "半导体设备", "zone": "active",
            "motivation_tags": ["1_国家安全"],
        }]
        r1 = seed_industries(db, payload)
        r2 = seed_industries(db, payload)
        assert r1["inserted"] == 1
        assert r2["updated"] == 1
        # 数据库只有 1 行
        n = db.query_one("SELECT COUNT(*) AS n FROM watchlist")["n"]
        assert n == 1

    def test_json_fields_stored_correctly(self, tmp_db):
        db, _ = tmp_db
        seed_industries(db, [{
            "name": "半导体设备",
            "sub_industries": ["光刻", "刻蚀", "沉积"],
            "motivation_tags": ["1_国家安全", "2_技术主权"],
        }])
        row = db.query_one(
            "SELECT industry_aliases, motivation_levels FROM watchlist"
        )
        subs = json.loads(row["industry_aliases"])
        assert subs == ["光刻", "刻蚀", "沉积"]
        tags = json.loads(row["motivation_levels"])
        assert tags == ["1_国家安全", "2_技术主权"]

    def test_primary_market_in_notes(self, tmp_db):
        db, _ = tmp_db
        seed_industries(db, [{
            "name": "A", "primary_market": "CN+KR+US", "notes": "原始备注",
        }])
        row = db.query_one("SELECT notes FROM watchlist")
        assert "CN+KR+US" in row["notes"]
        assert "原始备注" in row["notes"]

    def test_dry_run_no_writes(self, tmp_db):
        db, _ = tmp_db
        result = seed_industries(db, [{"name": "A"}], dry_run=True)
        assert result["inserted"] == 1  # 统计 +1
        n = db.query_one("SELECT COUNT(*) AS n FROM watchlist")["n"]
        assert n == 0  # 但 DB 没写

    def test_default_zone_active(self, tmp_db):
        db, _ = tmp_db
        seed_industries(db, [{"name": "A"}])  # zone 缺省
        row = db.query_one("SELECT zone FROM watchlist WHERE industry_name='A'")
        assert row["zone"] == "active"


# ═══════════════════ seed_holdings ═══════════════════


class TestSeedHoldings:
    def test_insert_single(self, tmp_db):
        db, _ = tmp_db
        result = seed_holdings(db, [{
            "stock": "600519", "market": "A",
            "company_name": "贵州茅台", "industry": "白酒",
            "shares": 100, "buy_price": 1500.0, "buy_date": "2024-05-15",
        }])
        assert result == {"inserted": 1, "updated": 0}
        row = db.query_one("SELECT * FROM track_list WHERE stock='600519'")
        assert row["company_name"] == "贵州茅台"
        assert row["market"] == "A"
        assert row["actual_shares"] == 100
        assert float(row["actual_buy_price"]) == 1500.0
        assert row["actual_buy_date"] == "2024-05-15"

    def test_rerun_updates(self, tmp_db):
        db, _ = tmp_db
        h1 = {"stock": "600519", "market": "A", "shares": 100, "buy_price": 1500.0}
        seed_holdings(db, [h1])
        h2 = {"stock": "600519", "market": "A", "shares": 200, "buy_price": 1400.0}
        r = seed_holdings(db, [h2])
        assert r["updated"] == 1
        row = db.query_one("SELECT actual_shares, actual_buy_price FROM track_list")
        assert row["actual_shares"] == 200
        assert float(row["actual_buy_price"]) == 1400.0

    def test_dry_run(self, tmp_db):
        db, _ = tmp_db
        r = seed_holdings(db, [{"stock": "X", "market": "A", "shares": 1, "buy_price": 1.0}], dry_run=True)
        assert r["inserted"] == 1
        n = db.query_one("SELECT COUNT(*) AS n FROM track_list")["n"]
        assert n == 0

    def test_multiple_stocks(self, tmp_db):
        db, _ = tmp_db
        holdings = [
            {"stock": "600519", "market": "A", "shares": 100, "buy_price": 1500.0},
            {"stock": "005930", "market": "KR", "shares": 50, "buy_price": 70000.0},
            {"stock": "NVDA", "market": "US", "shares": 10, "buy_price": 500.0},
        ]
        r = seed_holdings(db, holdings)
        assert r["inserted"] == 3
        n = db.query_one("SELECT COUNT(*) AS n FROM track_list")["n"]
        assert n == 3


# ═══════════════════ seed_principles ═══════════════════


class TestSeedPrinciples:
    def test_insert_basic(self, tmp_db):
        db, _ = tmp_db
        r = seed_principles(db, ["p1", "p2", "p3"])
        assert r["written"] == 3
        assert r["action"] == "inserted"
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_PRINCIPLES_KEY,),
        )
        assert row is not None
        parsed = json.loads(row["value"])
        assert parsed == ["p1", "p2", "p3"]

    def test_insert_dict_form(self, tmp_db):
        """dict principles 结构化对象也能写入并读回。"""
        db, _ = tmp_db
        principles = [
            {"id": "P1", "title": "异常可见性优先", "core": "c1",
             "application": ["a1", "a2"], "warnings": ["w1"]},
            {"id": "P2", "title": "结构>智能", "core": "c2",
             "application": ["a3"], "warnings": ["w2"]},
        ]
        r = seed_principles(db, principles)
        assert r["written"] == 2
        assert r["action"] == "inserted"
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_PRINCIPLES_KEY,),
        )
        parsed = json.loads(row["value"])
        assert len(parsed) == 2
        assert parsed[0]["id"] == "P1"
        assert parsed[0]["title"] == "异常可见性优先"
        assert parsed[0]["application"] == ["a1", "a2"]

    def test_rerun_action_updated(self, tmp_db):
        db, _ = tmp_db
        r1 = seed_principles(db, ["p1"])
        r2 = seed_principles(db, ["p2", "p3"])
        assert r1["action"] == "inserted"
        assert r2["action"] == "updated"
        # 最终值是第二次的
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_PRINCIPLES_KEY,),
        )
        parsed = json.loads(row["value"])
        assert parsed == ["p2", "p3"]

    def test_empty_skips(self, tmp_db):
        db, _ = tmp_db
        r = seed_principles(db, [])
        assert r["skipped_empty"] is True
        assert r["action"] == "skipped"
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_PRINCIPLES_KEY,),
        )
        assert row is None

    def test_empty_preserves_existing(self, tmp_db):
        """用户先 seed 3 条经验；后续 run 若 principles=[] 不应清空。"""
        db, _ = tmp_db
        seed_principles(db, ["p1", "p2"])
        seed_principles(db, [])  # 空
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_PRINCIPLES_KEY,),
        )
        assert row is not None
        parsed = json.loads(row["value"])
        assert parsed == ["p1", "p2"]

    def test_replace_semantic(self, tmp_db):
        db, _ = tmp_db
        seed_principles(db, ["old1", "old2"])
        seed_principles(db, ["new1"])
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_PRINCIPLES_KEY,),
        )
        parsed = json.loads(row["value"])
        assert parsed == ["new1"]

    def test_dry_run(self, tmp_db):
        db, _ = tmp_db
        seed_principles(db, ["x"], dry_run=True)
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_PRINCIPLES_KEY,),
        )
        assert row is None


class TestSeedUserContext:
    def test_insert_basic(self, tmp_db):
        db, _ = tmp_db
        ctx = {
            "investor_type": "长期价值投资者",
            "capital_range": "₩1-3亿",
            "holding_horizon": "3-5年",
            "markets": ["A", "KR", "US"],
        }
        r = seed_user_context(db, ctx)
        assert r["written"] == 1
        assert r["action"] == "inserted"
        assert "markets" in r["keys"]
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_USER_CONTEXT_KEY,),
        )
        parsed = json.loads(row["value"])
        assert parsed["investor_type"] == "长期价值投资者"
        assert parsed["markets"] == ["A", "KR", "US"]

    def test_rerun_action_updated(self, tmp_db):
        db, _ = tmp_db
        r1 = seed_user_context(db, {"investor_type": "A"})
        r2 = seed_user_context(db, {"investor_type": "B", "phase": "cold_start"})
        assert r1["action"] == "inserted"
        assert r2["action"] == "updated"
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_USER_CONTEXT_KEY,),
        )
        parsed = json.loads(row["value"])
        assert parsed["investor_type"] == "B"
        assert parsed["phase"] == "cold_start"

    def test_empty_skips(self, tmp_db):
        db, _ = tmp_db
        r = seed_user_context(db, {})
        assert r["skipped_empty"] is True
        assert r["action"] == "skipped"
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_USER_CONTEXT_KEY,),
        )
        assert row is None

    def test_none_skips(self, tmp_db):
        db, _ = tmp_db
        r = seed_user_context(db, None)
        assert r["skipped_empty"] is True

    def test_empty_preserves_existing(self, tmp_db):
        db, _ = tmp_db
        seed_user_context(db, {"investor_type": "A"})
        seed_user_context(db, {})
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_USER_CONTEXT_KEY,),
        )
        assert row is not None
        parsed = json.loads(row["value"])
        assert parsed["investor_type"] == "A"

    def test_dry_run(self, tmp_db):
        db, _ = tmp_db
        seed_user_context(db, {"k": "v"}, dry_run=True)
        row = db.query_one(
            "SELECT value FROM system_meta WHERE key=?",
            (SYSTEM_META_USER_CONTEXT_KEY,),
        )
        assert row is None


# ═══════════════════ run_cold_start（完整流程）═══════════════════


class TestRunColdStart:
    def test_run_minimal(self, tmp_db, minimal_yaml):
        _, db_path = tmp_db
        result = run_cold_start(
            db_path=db_path, config_path=minimal_yaml,
            interactive=False, dry_run=False,
        )
        assert result["industries"]["inserted"] == 2
        assert result["holdings"]["inserted"] == 0
        assert result["principles"]["skipped_empty"] is True
        # user_context 缺省也应 skip
        assert result["user_context"]["skipped_empty"] is True

    def test_run_full(self, tmp_db, full_yaml):
        _, db_path = tmp_db
        result = run_cold_start(
            db_path=db_path, config_path=full_yaml,
            interactive=False, dry_run=False,
        )
        assert result["industries"]["inserted"] == 2
        assert result["holdings"]["inserted"] == 1
        assert result["principles"]["written"] == 2

        # 验证 DB 实际内容
        db = DatabaseManager(db_path)
        try:
            n_wl = db.query_one("SELECT COUNT(*) AS n FROM watchlist")["n"]
            n_tl = db.query_one("SELECT COUNT(*) AS n FROM track_list")["n"]
            row = db.query_one(
                "SELECT value FROM system_meta WHERE key=?",
                (SYSTEM_META_PRINCIPLES_KEY,),
            )
        finally:
            db.close()
        assert n_wl == 2
        assert n_tl == 1
        assert row is not None
        assert len(json.loads(row["value"])) == 2

    def test_run_with_user_context(self, tmp_db, tmp_path):
        _, db_path = tmp_db
        yaml_path = tmp_path / "with_ctx.yaml"
        yaml_path.write_text(textwrap.dedent("""\
            industries:
              - name: "半导体"
                zone: "active"
            holdings: []
            principles:
              - id: "P1"
                title: "T1"
                core: "core text"
            user_context:
              investor_type: "长期价值投资者"
              markets: ["A", "KR", "US"]
              phase: "cold_start"
        """), encoding="utf-8")

        result = run_cold_start(
            db_path=db_path, config_path=yaml_path,
            interactive=False, dry_run=False,
        )
        assert result["industries"]["inserted"] == 1
        assert result["principles"]["written"] == 1
        assert result["principles"]["action"] == "inserted"
        assert result["user_context"]["action"] == "inserted"

        # 重跑：action 应为 updated
        result2 = run_cold_start(
            db_path=db_path, config_path=yaml_path,
            interactive=False, dry_run=False,
        )
        assert result2["industries"]["updated"] == 1
        assert result2["principles"]["action"] == "updated"
        assert result2["user_context"]["action"] == "updated"

        # DB 内容正确
        db = DatabaseManager(db_path)
        try:
            row = db.query_one(
                "SELECT value FROM system_meta WHERE key=?",
                (SYSTEM_META_USER_CONTEXT_KEY,),
            )
        finally:
            db.close()
        assert row is not None
        parsed = json.loads(row["value"])
        assert parsed["investor_type"] == "长期价值投资者"
        assert parsed["markets"] == ["A", "KR", "US"]

    def test_invalid_yaml_raises(self, tmp_path, tmp_db):
        _, db_path = tmp_db
        bad = tmp_path / "bad.yaml"
        bad.write_text("industries:\n  - {}\n", encoding="utf-8")  # missing name
        with pytest.raises(ValueError, match="validation failed"):
            run_cold_start(
                db_path=db_path, config_path=bad,
                interactive=False, dry_run=False,
            )

    def test_auto_init_missing_db(self, tmp_path, minimal_yaml):
        # db_path 不存在 → 自动 init
        db_path = tmp_path / "fresh.db"
        assert not db_path.exists()
        result = run_cold_start(
            db_path=db_path, config_path=minimal_yaml,
            interactive=False, dry_run=False,
        )
        assert db_path.exists()
        assert result["industries"]["inserted"] == 2


# ═══════════════════ 交互 prompt ═══════════════════


class TestPromptHoldings:
    def test_empty_input_returns_empty(self):
        stdin = io.StringIO("\n")  # 直接空行
        stdout = io.StringIO()
        result = prompt_holdings([], stdin=stdin, stdout=stdout)
        assert result == []

    def test_quit_shortcut(self):
        stdin = io.StringIO("q\n")
        stdout = io.StringIO()
        result = prompt_holdings([], stdin=stdin, stdout=stdout)
        assert result == []

    def test_one_holding(self):
        stdin = io.StringIO(
            "600519\n"      # stock
            "A\n"           # market
            "100\n"         # shares
            "1500.0\n"      # buy_price
            "贵州茅台\n"     # company
            "白酒\n"         # industry
            "2024-05-15\n"  # buy_date
            "\n"            # 空行结束
        )
        stdout = io.StringIO()
        result = prompt_holdings([], stdin=stdin, stdout=stdout)
        assert len(result) == 1
        assert result[0]["stock"] == "600519"
        assert result[0]["market"] == "A"
        assert result[0]["shares"] == 100
        assert result[0]["buy_price"] == 1500.0
        assert result[0]["company_name"] == "贵州茅台"
        assert result[0]["industry"] == "白酒"
        assert result[0]["buy_date"] == "2024-05-15"

    def test_invalid_market_skipped(self):
        stdin = io.StringIO(
            "X\n"      # stock
            "JP\n"     # invalid market
            "\n"       # 空行结束
        )
        stdout = io.StringIO()
        result = prompt_holdings([], stdin=stdin, stdout=stdout)
        assert result == []
        assert "market 必须是" in stdout.getvalue()

    def test_existing_preserved(self):
        existing = [{"stock": "600519", "market": "A", "shares": 100, "buy_price": 1500.0}]
        stdin = io.StringIO("\n")
        stdout = io.StringIO()
        result = prompt_holdings(existing, stdin=stdin, stdout=stdout)
        assert result == existing


class TestPromptPrinciples:
    def test_empty_input(self):
        stdin = io.StringIO("\n")
        stdout = io.StringIO()
        result = prompt_principles([], stdin=stdin, stdout=stdout)
        assert result == []

    def test_three_principles(self):
        stdin = io.StringIO("经验1\n经验2\n经验3\n\n")
        stdout = io.StringIO()
        result = prompt_principles([], stdin=stdin, stdout=stdout)
        assert result == ["经验1", "经验2", "经验3"]

    def test_max_cap(self):
        # 给超过 MAX 的行：只收 MAX_PRINCIPLES 条
        stdin_text = "\n".join(f"p{i}" for i in range(MAX_PRINCIPLES + 3)) + "\n"
        stdin = io.StringIO(stdin_text)
        stdout = io.StringIO()
        result = prompt_principles([], stdin=stdin, stdout=stdout)
        assert len(result) == MAX_PRINCIPLES

    def test_respects_existing_count(self):
        existing = ["旧经验"]
        stdin = io.StringIO("新1\n新2\n\n")
        stdout = io.StringIO()
        result = prompt_principles(existing, stdin=stdin, stdout=stdout)
        assert result == ["旧经验", "新1", "新2"]


# ═══════════════════ print 辅助（smoke）═══════════════════


class TestPrintFns:
    def test_print_preview_smoke(self, capsys, minimal_yaml):
        cfg = load_config(minimal_yaml)
        print_preview(cfg)
        out = capsys.readouterr().out
        assert "2 条待录入" in out
        assert "半导体设备" in out

    def test_print_result_smoke(self, capsys):
        print_result({
            "industries": {"inserted": 5, "updated": 1},
            "holdings": {"inserted": 0, "updated": 0},
            "principles": {
                "written": 3, "skipped_empty": False,
                "action": "inserted", "count": 3,
            },
            "user_context": {
                "written": 1, "skipped_empty": False,
                "action": "inserted", "keys": ["k1", "k2"],
            },
            "dry_run": False,
        })
        out = capsys.readouterr().out
        assert "+5 inserted" in out
        assert "3 items" in out
        assert "action=inserted" in out
        assert "user_context" in out


# ═══════════════════ CLI main ═══════════════════


class TestCLIMain:
    def test_dry_run(self, tmp_db, minimal_yaml, capsys):
        _, db_path = tmp_db
        rc = cold_start_main([
            "--config", str(minimal_yaml),
            "--db", str(db_path),
            "--dry-run",
        ])
        assert rc == 0
        n = DatabaseManager(db_path)
        try:
            count = n.query_one("SELECT COUNT(*) AS n FROM watchlist")["n"]
        finally:
            n.close()
        assert count == 0  # dry_run 不写

    def test_yes_flag_writes(self, tmp_db, minimal_yaml, capsys):
        _, db_path = tmp_db
        rc = cold_start_main([
            "--config", str(minimal_yaml),
            "--db", str(db_path),
            "--yes",
        ])
        assert rc == 0
        db = DatabaseManager(db_path)
        try:
            count = db.query_one("SELECT COUNT(*) AS n FROM watchlist")["n"]
        finally:
            db.close()
        assert count == 2

    def test_missing_config_rc_2(self, tmp_path):
        rc = cold_start_main([
            "--config", str(tmp_path / "no.yaml"),
            "--db", str(tmp_path / "k.db"),
            "--yes",
        ])
        assert rc == 2

    def test_invalid_config_rc_2(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("industries:\n  - name: ''\n", encoding="utf-8")
        rc = cold_start_main([
            "--config", str(bad),
            "--db", str(tmp_path / "k.db"),
            "--yes",
        ])
        assert rc == 2


# ═══════════════════ 真 YAML（15 行业）端到端 ═══════════════════


class TestRealConfigE2E:
    @pytest.fixture
    def real_yaml_path(self):
        return (
            Path(__file__).resolve().parent.parent
            / "scripts" / "cold_start_config.yaml"
        )

    def test_15_industries_seed_successfully(self, tmp_db, real_yaml_path):
        _, db_path = tmp_db
        result = run_cold_start(
            db_path=db_path, config_path=real_yaml_path,
            interactive=False, dry_run=False,
        )
        assert result["industries"]["inserted"] == 15

        db = DatabaseManager(db_path)
        try:
            n_active = db.query_one(
                "SELECT COUNT(*) AS n FROM watchlist WHERE zone='active'"
            )["n"]
            n_obs = db.query_one(
                "SELECT COUNT(*) AS n FROM watchlist WHERE zone='observation'"
            )["n"]
            rows = db.query(
                "SELECT industry_name FROM watchlist ORDER BY industry_name"
            )
        finally:
            db.close()

        assert n_active == 14
        assert n_obs == 1
        names = {r["industry_name"] for r in rows}
        for expected in (
            "半导体设备", "HBM", "医疗器械国产替代", "工业自动化",
            "AI算力", "AI应用软件", "数据中心配套",
            "核电", "特高压", "储能细分",
            "造船海工", "韩国电池",
            "军工", "创新药", "人形机器人",
        ):
            assert expected in names, f"missing {expected}"

    def test_5_principles_and_user_context(self, tmp_db, real_yaml_path):
        _, db_path = tmp_db
        result = run_cold_start(
            db_path=db_path, config_path=real_yaml_path,
            interactive=False, dry_run=False,
        )
        assert result["principles"]["written"] == 5
        assert result["principles"]["action"] == "inserted"
        assert result["user_context"]["action"] == "inserted"

        db = DatabaseManager(db_path)
        try:
            rows = db.query(
                """SELECT key, value FROM system_meta
                   WHERE key IN (?, ?)""",
                (SYSTEM_META_PRINCIPLES_KEY, SYSTEM_META_USER_CONTEXT_KEY),
            )
        finally:
            db.close()

        by_key = {r["key"]: r["value"] for r in rows}
        assert SYSTEM_META_PRINCIPLES_KEY in by_key
        assert SYSTEM_META_USER_CONTEXT_KEY in by_key

        principles = json.loads(by_key[SYSTEM_META_PRINCIPLES_KEY])
        assert len(principles) == 5
        # 结构化原则：有 id / title / core / application / warnings / source
        for p in principles:
            assert isinstance(p, dict)
            assert "id" in p and "title" in p and "core" in p
            assert isinstance(p["application"], list)
            assert isinstance(p["warnings"], list)
        ids = [p["id"] for p in principles]
        assert ids == ["P1", "P2", "P3", "P4", "P5"]

        ctx = json.loads(by_key[SYSTEM_META_USER_CONTEXT_KEY])
        assert ctx["investor_type"] == "长期价值投资者"
        assert ctx["markets"] == ["A", "KR", "US"]
        assert ctx["phase"] == "cold_start"

    def test_rerun_updates_industry_fields(self, tmp_db, real_yaml_path):
        """先 seed 再 seed：行业数量不变，字段（subs/notes）UPDATE。"""
        _, db_path = tmp_db
        run_cold_start(
            db_path=db_path, config_path=real_yaml_path,
            interactive=False, dry_run=False,
        )
        r2 = run_cold_start(
            db_path=db_path, config_path=real_yaml_path,
            interactive=False, dry_run=False,
        )
        assert r2["industries"]["inserted"] == 0
        assert r2["industries"]["updated"] == 15

        # principles / user_context 变 updated
        assert r2["principles"]["action"] == "updated"
        assert r2["user_context"]["action"] == "updated"

    def test_storage_subindustries_json_contains_6_items(
        self, tmp_db, real_yaml_path
    ):
        """储能细分的 sub_industries 应 6 项：构网型/液流/PCS/钠电/温控/BMS"""
        _, db_path = tmp_db
        run_cold_start(
            db_path=db_path, config_path=real_yaml_path,
            interactive=False, dry_run=False,
        )
        db = DatabaseManager(db_path)
        try:
            row = db.query_one(
                "SELECT industry_aliases FROM watchlist WHERE industry_name=?",
                ("储能细分",),
            )
        finally:
            db.close()
        subs = json.loads(row["industry_aliases"])
        assert len(subs) == 6
        for expected in ("构网型", "液流", "PCS", "钠电", "温控", "BMS"):
            assert expected in subs

    def test_notes_contains_primary_market_and_description(
        self, tmp_db, real_yaml_path
    ):
        """半导体设备 notes 应包含 [market=CN+KR+US] 和原始备注"""
        _, db_path = tmp_db
        run_cold_start(
            db_path=db_path, config_path=real_yaml_path,
            interactive=False, dry_run=False,
        )
        db = DatabaseManager(db_path)
        try:
            row = db.query_one(
                "SELECT notes FROM watchlist WHERE industry_name=?",
                ("半导体设备",),
            )
        finally:
            db.close()
        assert row is not None
        assert "CN+KR+US" in row["notes"]
        assert "跨市场联动最强" in row["notes"]
