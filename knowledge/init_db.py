"""
knowledge/init_db.py — 创建knowledge.db及其全部20张表+索引

对应Scout系统蓝图v1.61。所有时间字段存UTC ISO 8601（TEXT类型）。
v1.57前置10项技术决策已纳入（industry_id主键、global_companies、mode字段等）。

运行方式：
    python knowledge/init_db.py
"""
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "knowledge.db"


SCHEMA_SQL = """
-- 1. 信息单元（信号层）
CREATE TABLE IF NOT EXISTS info_units (
    id TEXT PRIMARY KEY,                    -- hash(source+标题+发布日期)
    source TEXT NOT NULL,                   -- D1/D4/V1/V3/S4等
    source_type TEXT DEFAULT 'system',      -- system/manual
    source_credibility TEXT,                -- 权威/可靠/参考/线索
    timestamp TEXT NOT NULL,                -- UTC ISO 8601
    category TEXT,                          -- 宏观/政策/行业/公司/科研/资产/汇率/风险
    content TEXT,
    raw_link TEXT,
    related_industries TEXT,                -- JSON数组
    priority TEXT,                          -- 高/中/低
    status TEXT DEFAULT 'pending',          -- pending/processing/done/failed
    verified INTEGER DEFAULT 0,
    layer_trace TEXT,
    schema_version INTEGER DEFAULT 1,
    analysis_fields TEXT,                   -- JSON
    markdown_path TEXT,
    policy_direction TEXT,                  -- supportive/restrictive/neutral/mixed/null
    event_chain_id TEXT,                    -- v1.59：关联事件串ID（Phase 1允许NULL）
    mixed_subtype TEXT,                     -- v1.60：mixed时必填 conflict/structural/stage_difference
    is_secondary_source INTEGER DEFAULT 0,
    independent_confirmation INTEGER DEFAULT 0,
    weight REAL DEFAULT 100,
    created_at TEXT NOT NULL,               -- UTC
    updated_at TEXT NOT NULL                -- UTC
);
CREATE INDEX IF NOT EXISTS idx_iu_source_time ON info_units(source, timestamp);
CREATE INDEX IF NOT EXISTS idx_iu_status ON info_units(status);
CREATE INDEX IF NOT EXISTS idx_iu_chain ON info_units(event_chain_id);


-- 2. 行业观察清单（v1.57 industry_id主键 + v1.58-v1.60新字段）
CREATE TABLE IF NOT EXISTS watchlist (
    industry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    industry_name TEXT NOT NULL UNIQUE,
    industry_aliases TEXT,                  -- JSON数组
    industry_name_en TEXT,
    industry_name_ko TEXT,
    zone TEXT DEFAULT 'active',             -- active/cold/cycle_bottom/observe_new_direction
    source_type TEXT DEFAULT 'system',
    early_signal INTEGER DEFAULT 0,
    dimensions INTEGER,
    priority_score REAL,
    verification_status TEXT,               -- positive/early_positive/neutral/negative/insufficient
    verification_date TEXT,                 -- UTC
    verification_changed_at TEXT,           -- UTC
    industry_stage TEXT,
    policy_risk TEXT DEFAULT 'none',
    -- 动机标签（v1.48+v1.54）
    motivation_levels TEXT,                 -- JSON数组
    motivation_duration TEXT,
    motivation_updated_at TEXT,             -- UTC
    motivation_detail TEXT,                 -- v1.54 JSON
    motivation_uncertainty TEXT,            -- low/medium/high
    motivation_last_drift_at TEXT,          -- UTC
    -- 缺口分析（v1.50+v1.55）
    gap_analysis TEXT,                      -- JSON
    gap_status TEXT DEFAULT 'active',       -- active/closing/closed/no_gap
    gap_reversal_signals TEXT,
    gap_fillability INTEGER,                -- 1-5
    gap_fillability_evidence TEXT,
    gap_fillability_updated_at TEXT,        -- UTC
    -- 结构性分化（v1.60）
    sub_market_signals TEXT,                -- JSON
    -- S曲线渗透率
    penetration_rate REAL,
    penetration_history TEXT,               -- JSON
    penetration_source TEXT,
    -- 投资论点
    thesis TEXT,
    key_metrics TEXT,                       -- JSON
    kill_conditions TEXT,                   -- JSON
    thesis_status TEXT DEFAULT 'intact',
    entered_at TEXT,                        -- UTC
    last_signal_at TEXT,                    -- UTC
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_wl_name ON watchlist(industry_name);
CREATE INDEX IF NOT EXISTS idx_wl_zone ON watchlist(zone);


-- 3. 行业字典（v1.58 sub_industries字段）
CREATE TABLE IF NOT EXISTS industry_dict (
    industry TEXT PRIMARY KEY,
    aliases TEXT,                           -- JSON数组
    data_source_mapping TEXT,               -- JSON对象
    cyclical INTEGER DEFAULT 0,
    benchmark_code TEXT,
    in_watchlist INTEGER DEFAULT 0,
    global_leaders TEXT,                    -- JSON
    historical_cycles TEXT,                 -- JSON
    historical_context TEXT,
    why_different_now TEXT,                 -- v1.47
    supply_chain_readiness INTEGER,         -- 1-5
    readiness_evidence TEXT,
    readiness_bottleneck TEXT,
    readiness_updated_at TEXT,              -- UTC
    scout_range TEXT DEFAULT 'active',      -- early_strict/early_qualified/active/mature/out_of_range
    sub_industries TEXT,                    -- v1.58 JSON数组
    version INTEGER DEFAULT 1,
    last_change_reason TEXT,
    last_change_by TEXT,
    confidence TEXT DEFAULT 'confirmed'
);


-- 4. 产业链关系图
CREATE TABLE IF NOT EXISTS industry_chain (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_industry TEXT,
    to_industry TEXT,
    relation_type TEXT,                     -- upstream/downstream/substitute/complement/coincidence
    confidence TEXT,
    evidence TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT,                        -- UTC
    updated_at TEXT                         -- UTC
);
CREATE INDEX IF NOT EXISTS idx_ic_from ON industry_chain(from_industry, status);


-- 5. 关联股票（v1.57 industry_id + v1.58-v1.59 sub_industry JSON数组）
CREATE TABLE IF NOT EXISTS related_stocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    industry_id INTEGER,                    -- v1.57 FK watchlist.industry_id
    industry TEXT,                          -- 保留镜像
    sub_industry TEXT,                      -- v1.59 JSON数组
    stock_code TEXT,
    stock_name TEXT,
    market TEXT,                            -- A/KR/US
    global_company_id TEXT,                 -- v1.57 跨市场关联
    discovery_source TEXT,
    discovery_detail TEXT,
    discovered_at TEXT,                     -- UTC
    confidence TEXT DEFAULT 'staging',
    last_analyzed_at TEXT,                  -- UTC
    analysis_result TEXT,
    gate_block_reason TEXT,
    consecutive_fail_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    dormant_reason TEXT,
    removed_reason TEXT,
    company_policy_risk TEXT DEFAULT 'none',
    updated_at TEXT,                        -- UTC
    UNIQUE(industry, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_rs_industry ON related_stocks(industry, status);
CREATE INDEX IF NOT EXISTS idx_rs_market ON related_stocks(market);
CREATE INDEX IF NOT EXISTS idx_rs_global ON related_stocks(global_company_id);


-- 6. 信息行业映射（多对多关联）
CREATE TABLE IF NOT EXISTS info_industry_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    info_unit_id TEXT,
    industry_name TEXT,
    relation_strength REAL,
    created_at TEXT,                        -- UTC
    FOREIGN KEY (info_unit_id) REFERENCES info_units(id)
);
CREATE INDEX IF NOT EXISTS idx_iim_info ON info_industry_map(info_unit_id);
CREATE INDEX IF NOT EXISTS idx_iim_industry ON info_industry_map(industry_name);


-- 7. 跟踪列表（Phase 2A启用，Phase 1预建）
CREATE TABLE IF NOT EXISTS track_list (
    stock TEXT PRIMARY KEY,
    global_company_id TEXT,                 -- v1.57 跨市场关联
    company_name TEXT,
    market TEXT,                            -- A/KR/US
    tier TEXT,
    industry_id INTEGER,                    -- v1.57
    industry TEXT,                          -- 镜像
    recommend_date TEXT,                    -- UTC
    recommend_price REAL,
    recommend_reason TEXT,
    actual_buy_price REAL,
    actual_buy_date TEXT,                   -- UTC
    actual_shares INTEGER,
    risk_flag INTEGER DEFAULT 0,
    risk_detail TEXT,
    risk_updated TEXT,                      -- UTC
    company_policy_risk TEXT DEFAULT 'none',
    composite_score REAL,
    target_price_auto REAL,
    updated_at TEXT                         -- UTC
);


-- 8. 股票财务（Phase 2A启用）
CREATE TABLE IF NOT EXISTS stock_financials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock TEXT,
    report_period TEXT,
    revenue REAL,
    net_profit REAL,
    roe REAL,
    roa REAL,
    f_score INTEGER,                        -- Piotroski F-Score
    z_score REAL,                           -- Altman Z''-1995 (emerging markets)
    m_score REAL,                           -- Beneish M-Score (Phase 3+)
    dupont_data TEXT,                       -- JSON
    historical_peak REAL,
    historical_peak_date TEXT,              -- UTC
    pe_ttm REAL,                            -- v1.01 Trailing 12-month P/E
    eps_cagr_3y REAL,                       -- v1.01 3-year EPS CAGR (decimal, 0.20 = 20%)
    peg_ratio REAL,                         -- v1.01 PE_TTM / (eps_cagr_3y * 100)
    updated_at TEXT                         -- UTC
);
CREATE INDEX IF NOT EXISTS idx_sf_stock ON stock_financials(stock);


-- 9. 规则统计
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name TEXT,
    rule_type TEXT,                         -- gate/scoring/filter/warning
    apply_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    reliability REAL DEFAULT 0,
    status TEXT DEFAULT 'active',           -- active/canary/retired
    created_at TEXT,                        -- UTC
    updated_at TEXT                         -- UTC
);


-- 10. 系统元数据
CREATE TABLE IF NOT EXISTS system_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT                         -- UTC
);


-- 11. 事件串（v1.48，Phase 2C完整启用，Phase 1预建）
CREATE TABLE IF NOT EXISTS event_chains (
    chain_id TEXT PRIMARY KEY,              -- #E-YYYYMMDD-TAG
    tag TEXT,
    title TEXT,
    status TEXT DEFAULT 'active',           -- active/archived/confirmed/falsified
    related_info_units TEXT,                -- JSON数组
    keywords TEXT,                          -- JSON数组
    created_at TEXT,                        -- UTC
    updated_at TEXT                         -- UTC
);
CREATE INDEX IF NOT EXISTS idx_ec_status ON event_chains(status);
CREATE INDEX IF NOT EXISTS idx_ec_tag ON event_chains(tag);


-- 12. 动机漂移日志（v1.54，Phase 2B启用，Phase 1预建）
CREATE TABLE IF NOT EXISTS motivation_drift_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    industry TEXT,
    old_dominant TEXT,                      -- JSON数组
    new_dominant TEXT,                      -- JSON数组
    old_model TEXT,
    new_model TEXT,
    change_reason TEXT,                     -- time_180d/policy_accumulation/manual/event_triggered
    drift_severity TEXT,                    -- low/medium/high
    created_at TEXT                         -- UTC
);
CREATE INDEX IF NOT EXISTS idx_mdl_industry ON motivation_drift_log(industry);


-- 13. 全球公司跨市场关联（v1.57）
CREATE TABLE IF NOT EXISTS global_companies (
    global_company_id TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    primary_listing TEXT,
    all_listings TEXT,                      -- JSON数组
    industry_id INTEGER,
    created_at TEXT,                        -- UTC
    updated_at TEXT                         -- UTC
);
CREATE INDEX IF NOT EXISTS idx_gc_name ON global_companies(company_name);


-- 14. LLM调用记录（v1.57）
CREATE TABLE IF NOT EXISTS llm_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT,
    prompt_version TEXT,                    -- direction_judge_v001
    model_name TEXT,                        -- claude-opus-4-7
    input_hash TEXT,
    output_summary TEXT,
    tokens_used INTEGER,
    cost_cents INTEGER,
    invoked_at TEXT                         -- UTC
);
CREATE INDEX IF NOT EXISTS idx_llm_agent_time ON llm_invocations(agent_name, invoked_at);


-- 15. Agent错误日志（v1.57）
CREATE TABLE IF NOT EXISTS agent_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT,
    error_type TEXT,                        -- network/parse/llm/rule/data/unknown
    error_message TEXT,
    context_data TEXT,                      -- JSON
    occurred_at TEXT,                       -- UTC
    resolved INTEGER DEFAULT 0,
    resolved_at TEXT                        -- UTC
);
CREATE INDEX IF NOT EXISTS idx_ae_type_time ON agent_errors(error_type, occurred_at);


-- 16. 推荐记录（v1.57 mode字段，Phase 2A启用，Phase 1预建）
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock TEXT,
    industry_id INTEGER,
    recommend_level TEXT,                   -- A/B/candidate/reject
    total_score REAL,
    dimensions_detail TEXT,                 -- JSON
    thesis_hash TEXT,
    mode TEXT DEFAULT 'cold_start',         -- cold_start/running/diagnosis/架构重审
    mode_since TEXT,                        -- UTC
    recommended_at TEXT,                    -- UTC
    UNIQUE(stock, thesis_hash, recommended_at)
);
CREATE INDEX IF NOT EXISTS idx_rec_stock ON recommendations(stock);
CREATE INDEX IF NOT EXISTS idx_rec_mode ON recommendations(mode);


-- 17. 用户决策记录（v1.61 推荐→复盘闭环）
CREATE TABLE IF NOT EXISTS user_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommend_id INTEGER,
    stock TEXT,
    decision TEXT,                          -- track/reject/auto_track
    decision_reason TEXT,
    decided_at TEXT,                        -- UTC
    FOREIGN KEY (recommend_id) REFERENCES recommendations(id)
);
CREATE INDEX IF NOT EXISTS idx_ud_recommend ON user_decisions(recommend_id);


-- 18. 价格追踪（v1.61，Phase 2A启用）
CREATE TABLE IF NOT EXISTS price_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommend_id INTEGER,
    stock TEXT,
    tracking_date TEXT,                     -- UTC
    close_price REAL,
    return_since_recommend REAL,
    benchmark_return REAL,
    excess_return REAL,
    FOREIGN KEY (recommend_id) REFERENCES recommendations(id)
);
CREATE INDEX IF NOT EXISTS idx_pt_recommend ON price_tracking(recommend_id);


-- 19. 复盘结果（v1.61，Phase 4启用）
CREATE TABLE IF NOT EXISTS review_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommend_id INTEGER,
    review_at_days INTEGER,                 -- 30/90/180/360
    return_rate REAL,
    hit_or_miss TEXT,                       -- hit/miss/ongoing
    attribution TEXT,                       -- JSON {"industry":0.4,"stock":0.3,"timing":0.3}
    reviewed_at TEXT,                       -- UTC
    FOREIGN KEY (recommend_id) REFERENCES recommendations(id)
);
CREATE INDEX IF NOT EXISTS idx_rr_recommend ON review_results(recommend_id);


-- 20. 否决追踪（v1.61）
CREATE TABLE IF NOT EXISTS rejected_stocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock TEXT,
    industry_id INTEGER,
    rejected_at TEXT,                       -- UTC
    reject_reason TEXT,
    return_at_360d REAL,
    missed_opportunity INTEGER DEFAULT 0,
    updated_at TEXT                         -- UTC
);
CREATE INDEX IF NOT EXISTS idx_rs_stock ON rejected_stocks(stock);
"""


EXPECTED_TABLES = [
    "info_units", "watchlist", "industry_dict", "industry_chain",
    "related_stocks", "info_industry_map", "track_list", "stock_financials",
    "rules", "system_meta", "event_chains", "motivation_drift_log",
    "global_companies", "llm_invocations", "agent_errors", "recommendations",
    "user_decisions", "price_tracking", "review_results", "rejected_stocks",
]


# v1.01：表迁移登记。{table_name: [(column, ddl_type), ...]}
# 仅 ALTER TABLE ADD COLUMN（SQLite 不支持 IF NOT EXISTS），靠 PRAGMA table_info 去重。
_COLUMN_MIGRATIONS = {
    "stock_financials": [
        ("pe_ttm", "REAL"),
        ("eps_cagr_3y", "REAL"),
        ("peg_ratio", "REAL"),
    ],
}


def _migrate_columns(conn):
    """对已存在表补齐缺失的列（幂等）。CREATE TABLE IF NOT EXISTS 不会改老表，只能 ALTER。"""
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            # 表还不存在，executescript 会建好，跳过
            continue
        for col, col_type in columns:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def init_database(db_path=DEFAULT_DB_PATH):
    """创建knowledge.db及其全部表+索引。幂等：CREATE TABLE IF NOT EXISTS + 迁移补列。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        _migrate_columns(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _verify(db_path):
    """读取sqlite_master，返回表和索引名列表"""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        indexes = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()
    return tables, indexes


if __name__ == "__main__":
    path = init_database()
    tables, indexes = _verify(path)
    print(f"[ok] knowledge.db created at: {path}")
    print(f"[ok] tables: {len(tables)} / expected 20")
    for t in tables:
        print(f"     - {t}")
    missing = [t for t in EXPECTED_TABLES if t not in tables]
    if missing:
        print(f"[ERROR] missing tables: {missing}")
        raise SystemExit(1)
    print(f"[ok] indexes: {len(indexes)}")
    for i in indexes:
        print(f"     - {i}")
