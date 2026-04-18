"""v1.05 — AkShare 交叉验证已加载的 related_stocks（A 股部分）

Step 1: 拉每行业关联的 AkShare 板块（概念/行业）成分股
Step 2: 比对当前 related_stocks 中的 A 股，分类为 ✓passed / ✗failed
Step 3: 对 ✗failed 行业从板块取 Top 龙头补充（按市值排序）
Step 4: 输出验证报告（不写库）

Usage:
  python scripts/validate_v105_related_stocks.py [--apply]

不带 --apply 只产报告；带 --apply 会：
  - 删除 A 股中 validate=fail 的行
  - 插入新补充的 akshare 板块龙头（discovery_source='akshare_sector_validation'）
  - 对所有保留的原候选行更新 discovery_source='manual_v105_cold_start'（覆盖旧的 v102）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import akshare as ak

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge.db"

# Industry → (concept_boards, industry_boards)
# 留空 list 表示该侧无可用板块
# 扩展原则：包含上下游/同等价值的相邻板块（如电力设备 ⊂ 数据中心配套）
BOARD_MAP: dict[str, tuple[list[str], list[str]]] = {
    "AI应用软件":        (["算力概念"], ["横向通用软件", "软件开发", "垂直应用软件"]),
    "AI算力":            (["算力概念"], ["半导体"]),
    "HBM":               ([], []),                              # KR/US only
    "低空经济":          (["低空经济", "无人机"], ["航空装备Ⅱ", "航天装备Ⅱ"]),
    "储能细分":          (["储能概念"], ["蓄电池及其他电池"]),
    "创新药":            (["创新药"], ["化学制药"]),
    "医疗器械国产替代":  (["医疗器械概念"], ["医疗器械", "医疗设备"]),
    "半导体材料":        (["第三代半导体"], ["半导体材料"]),
    "半导体设备":        (["第三代半导体"], ["半导体设备"]),
    "工业自动化":        (["机器人概念"], ["机器人"]),
    "数据中心配套":      (["数据中心", "液冷概念", "特高压", "算力概念"],
                          ["综合电力设备商", "电力"]),
    "新材料":            (["新材料"], ["金属新材料", "其他金属新材料",
                                        "锂电池", "电池化学品", "膜材料"]),
    "特高压":            (["特高压"], ["综合电力设备商"]),
    "生物制造":          (["合成生物"], ["化学制药"]),
    "韩国电池":          ([], []),                              # KR only
    "人形机器人":        (["人形机器人", "减速器", "机器人执行器"], ["机器人"]),
    "消费升级/IP经济":   ([], ["旅游及景区"]),
}

# Whitelist: 已知龙头股，即使 board 校验失败也保留
# 覆盖原因：1) 公司被并入/重组导致板块迁移  2) 板块映射覆盖不全
SAFE_LIST: dict[tuple[str, str], str] = {
    ("低空经济", "002025"): "航天电器(电连接器, 低空整机供应链)",
    ("低空经济", "002013"): "中航机电(已合并入中航电子, 体外仍代表低空概念)",
    ("数据中心配套", "300820"): "英杰电气(数据中心专用电源, 算力供应链)",
    ("生物制造", "002007"): "华兰生物(血液制品/疫苗, 生物制品代表)",
}


def fetch_concept(name: str) -> set[str]:
    try:
        df = ak.stock_board_concept_cons_em(symbol=name)
        if df is None or df.empty:
            return set()
        return set(df["代码"].astype(str))
    except Exception as e:
        print(f"   [warn] concept '{name}' failed: {e}", file=sys.stderr)
        return set()


def fetch_industry(name: str) -> set[str]:
    try:
        df = ak.stock_board_industry_cons_em(symbol=name)
        if df is None or df.empty:
            return set()
        return set(df["代码"].astype(str))
    except Exception as e:
        print(f"   [warn] industry '{name}' failed: {e}", file=sys.stderr)
        return set()


def fetch_concept_full(name: str):
    """Return DataFrame with 代码/名称."""
    try:
        df = ak.stock_board_concept_cons_em(symbol=name)
        if df is None or df.empty:
            return None
        df = df.copy()
        df["代码"] = df["代码"].astype(str)
        return df
    except Exception:
        return None


def fetch_industry_full(name: str):
    try:
        df = ak.stock_board_industry_cons_em(symbol=name)
        if df is None or df.empty:
            return None
        df = df.copy()
        df["代码"] = df["代码"].astype(str)
        return df
    except Exception:
        return None


_SPOT_CACHE: dict[str, tuple[str, float]] | None = None


def get_spot_universe() -> dict[str, tuple[str, float]]:
    """Fetch全 A 股 spot 一次, 返回 code -> (name, 总市值)."""
    global _SPOT_CACHE
    if _SPOT_CACHE is not None:
        return _SPOT_CACHE
    print("   [info] fetching A-share spot data for mkt_cap sort...")
    try:
        df = ak.stock_zh_a_spot_em()
        out: dict[str, tuple[str, float]] = {}
        for _, row in df.iterrows():
            code = str(row["代码"])
            name = str(row.get("名称", ""))
            mc = row.get("总市值", 0)
            try:
                mc = float(mc) if mc is not None else 0.0
            except Exception:
                mc = 0.0
            out[code] = (name, mc)
        _SPOT_CACHE = out
        print(f"   [info] spot universe: {len(out)} stocks")
        return out
    except Exception as e:
        print(f"   [warn] spot fetch failed: {e}")
        _SPOT_CACHE = {}
        return _SPOT_CACHE


def gather_universe(industry: str) -> tuple[set[str], list]:
    """Return (set of valid A股 codes, list of (code,name,mkt_cap) leaders sorted desc by mkt_cap)."""
    concepts, industries = BOARD_MAP.get(industry, ([], []))
    valid: set[str] = set()

    for c in concepts:
        df = fetch_concept_full(c)
        if df is not None:
            valid.update(df["代码"].tolist())
    for ind in industries:
        df = fetch_industry_full(ind)
        if df is not None:
            valid.update(df["代码"].tolist())

    spot = get_spot_universe()
    leaders_sorted = sorted(
        [(code, spot.get(code, ("?", 0))[0], spot.get(code, ("?", 0))[1])
         for code in valid if code in spot],
        key=lambda t: -t[2],
    )
    return valid, leaders_sorted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Apply DB updates (delete failed + insert replacements + rename source)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--target-per-industry", type=int, default=5,
                        help="Target stocks per active industry (default 5)")
    parser.add_argument("--target-observation", type=int, default=2,
                        help="Target stocks per observation industry (default 2)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # Load current A股 rows + zone
    zones = {r["industry_name"]: r["zone"]
             for r in con.execute("SELECT industry_name, zone FROM watchlist")}
    industry_id_map = {r["industry_name"]: r["industry_id"]
                       for r in con.execute("SELECT industry_id, industry_name FROM watchlist")}

    rows = con.execute("""SELECT id, industry, stock_code, stock_name, market, sub_industry, discovery_source
                          FROM related_stocks
                          ORDER BY industry, market, stock_code""").fetchall()

    by_industry: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_industry.setdefault(r["industry"], []).append(r)

    print(f"=== v1.05 AkShare validation ({len(rows)} rows, {len(by_industry)} industries) ===")
    print(f"Mode: {'APPLY' if args.apply else 'REPORT-ONLY'}")
    print()

    report: list[dict] = []
    new_inserts: list[tuple] = []  # (industry, code, name, market, sub, source)
    delete_ids: list[int] = []

    for industry in sorted(by_industry.keys()):
        zone = zones.get(industry, "?")
        target = args.target_per_industry if zone == "active" else args.target_observation
        a_rows = [r for r in by_industry[industry] if r["market"] == "A"]
        non_a_rows = [r for r in by_industry[industry] if r["market"] != "A"]

        print(f"--- {industry} (zone={zone}, target={target}) ---")
        boards_concept, boards_industry = BOARD_MAP.get(industry, ([], []))
        if not boards_concept and not boards_industry:
            print(f"   skip A-validation (no boards mapped — non-CN industry)")
            for r in by_industry[industry]:
                report.append({"industry": industry, "code": r["stock_code"], "name": r["stock_name"],
                               "market": r["market"], "source": "候选清单",
                               "validated": "n/a (KR/US only industry)"})
            print()
            continue

        valid_codes, leaders = gather_universe(industry)
        print(f"   board union size: {len(valid_codes)}")

        # Validate existing A-rows
        passed_a, failed_a, safe_a = [], [], []
        for r in a_rows:
            if r["stock_code"] in valid_codes:
                passed_a.append(r)
            elif (industry, r["stock_code"]) in SAFE_LIST:
                safe_a.append(r)
            else:
                failed_a.append(r)

        for r in passed_a:
            report.append({"industry": industry, "code": r["stock_code"], "name": r["stock_name"],
                           "market": "A", "source": "候选清单", "validated": "✓AkShare"})
            print(f"   ✓ {r['stock_code']} {r['stock_name']}")
        for r in safe_a:
            note = SAFE_LIST[(industry, r["stock_code"])]
            report.append({"industry": industry, "code": r["stock_code"], "name": r["stock_name"],
                           "market": "A", "source": "候选清单", "validated": f"✓SAFE_LIST ({note})"})
            print(f"   ✓ {r['stock_code']} {r['stock_name']}  [SAFE_LIST: {note}]")
        for r in failed_a:
            report.append({"industry": industry, "code": r["stock_code"], "name": r["stock_name"],
                           "market": "A", "source": "候选清单", "validated": "✗not in board"})
            print(f"   ✗ {r['stock_code']} {r['stock_name']} (not in any mapped board)")
            delete_ids.append(r["id"])
        for r in non_a_rows:
            report.append({"industry": industry, "code": r["stock_code"], "name": r["stock_name"],
                           "market": r["market"], "source": "候选清单", "validated": "skip (KR/US)"})

        # Decide if we need to top up
        existing_codes = ({r["stock_code"] for r in passed_a}
                          | {r["stock_code"] for r in safe_a}
                          | {r["stock_code"] for r in non_a_rows})
        kept_count = len(passed_a) + len(safe_a) + len(non_a_rows)
        needed = max(0, target - kept_count)
        if needed > 0 and leaders:
            picks = []
            for code, name, mc in leaders:
                if code in existing_codes:
                    continue
                picks.append((code, name, mc))
                if len(picks) >= needed:
                    break
            for code, name, mc in picks:
                new_inserts.append((industry, code, name, "A", None, "akshare_sector_validation"))
                report.append({"industry": industry, "code": code, "name": name,
                               "market": "A", "source": "akshare板块龙头",
                               "validated": "✓AkShare新增"})
                print(f"   + {code} {name} (mkt_cap={mc/1e8:.0f}亿) [new]")
        elif needed > 0:
            print(f"   !! need {needed} more but no leaders pool")
        print()

        # Be polite to the API
        time.sleep(0.5)

    # Apply changes
    if args.apply:
        with con:
            con.execute("BEGIN IMMEDIATE")
            # Rename existing source v102 → v105
            con.execute(
                "UPDATE related_stocks SET discovery_source='manual_v105_cold_start', updated_at=? "
                "WHERE discovery_source='manual_v102_cold_start'", (now,))
            # Delete failed
            for did in delete_ids:
                con.execute("DELETE FROM related_stocks WHERE id=?", (did,))
            # Insert replacements (idempotent)
            for industry, code, name, market, sub, src in new_inserts:
                exists = con.execute(
                    "SELECT 1 FROM related_stocks WHERE industry=? AND stock_code=?",
                    (industry, code)).fetchone()
                if exists:
                    continue
                ind_id = industry_id_map.get(industry)
                con.execute(
                    """INSERT INTO related_stocks
                       (industry_id, industry, sub_industry, stock_code, stock_name, market,
                        discovery_source, discovered_at, confidence, status, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staging', 'active', ?)""",
                    (ind_id, industry, sub, code, name, market, src, now, now),
                )

    # Final summary
    print("=== Summary ===")
    print(f"  Existing A-stocks kept:    {sum(1 for r in report if r['validated']=='✓AkShare')}")
    print(f"  Existing A-stocks failed:  {len(delete_ids)}")
    print(f"  KR/US untouched:           {sum(1 for r in report if r['validated'] in ('skip (KR/US)', 'n/a (KR/US only industry)'))}")
    print(f"  New AkShare leaders added: {len(new_inserts)}")
    print()

    final_total = con.execute("SELECT COUNT(*) FROM related_stocks").fetchone()[0]
    print(f"  Final related_stocks rows: {final_total}")
    print()

    print("=== Per-industry counts (after apply) ===")
    rows = con.execute("""
        SELECT industry, COUNT(*) AS n,
               GROUP_CONCAT(DISTINCT market) AS mkts
        FROM related_stocks GROUP BY industry ORDER BY industry
    """).fetchall()
    for r in rows:
        print(f"  [{r['n']}] {r['industry']:20s} markets={r['mkts']}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
