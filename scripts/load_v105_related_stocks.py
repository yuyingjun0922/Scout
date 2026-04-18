"""v1.05 — related_stocks 冷启动填充（17 个行业，~76 只龙头股）

Idempotent loader. Safe to re-run. Does NOT mutate existing rows.

Scope:
  - 17 industries (excludes 军工 / 核电 / 造船海工 / 量子计算)
  - 15 active industries × 3-5 龙头股
  - 2 observation industries × 1-2 stocks
  - Cross-market coverage: each industry covers ≥2 of {A, KR, US} where applicable

Defaults applied per row:
  - discovery_source = 'manual_v102_cold_start'
  - discovered_at = now (UTC)
  - confidence = 'staging'  (待 review 后升级 'approved')
  - status = 'active'
  - updated_at = now (UTC)
  - industry_id = lookup from watchlist by industry_name (NULL if not found)

Idempotency:
  - UNIQUE(industry, stock_code) → INSERT OR IGNORE
  - Re-running prints "skipped (exists)" for existing rows

Usage:
  python scripts/load_v105_related_stocks.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge.db"

DISCOVERY_SOURCE = "manual_v102_cold_start"
CONFIDENCE = "staging"
STATUS = "active"

# Format: (industry, stock_code, stock_name, market, sub_industry|None)
CANDIDATES: list[tuple[str, str, str, str, str | None]] = [
    # 1. AI应用软件 (US-led, 1 个 CN 锚)
    ("AI应用软件", "PLTR", "Palantir Technologies", "US", "企业SaaS"),
    ("AI应用软件", "CRM", "Salesforce", "US", "企业SaaS"),
    ("AI应用软件", "NOW", "ServiceNow", "US", "企业SaaS"),
    ("AI应用软件", "MSFT", "Microsoft", "US", "AI平台"),
    ("AI应用软件", "ADBE", "Adobe", "US", "垂直AI"),
    ("AI应用软件", "002230", "科大讯飞", "A", "语音/智能体"),

    # 2. AI算力 (US-led, 1 个 CN 锚)
    ("AI算力", "NVDA", "NVIDIA", "US", "GPU"),
    ("AI算力", "AMD", "Advanced Micro Devices", "US", "GPU/CPU"),
    ("AI算力", "AVGO", "Broadcom", "US", "ASIC/互联"),
    ("AI算力", "MRVL", "Marvell Technology", "US", "互联ASIC"),
    ("AI算力", "688256", "寒武纪", "A", "AI芯片"),

    # 3. HBM (KR + US)
    ("HBM", "000660", "SK하이닉스", "KR", "HBM3/HBM4"),
    ("HBM", "005930", "삼성전자", "KR", "HBM3e/HBM4"),
    ("HBM", "MU", "Micron Technology", "US", "HBM3e"),

    # 4. 低空经济 (CN + US)
    ("低空经济", "002013", "中航机电", "A", "航空机电"),
    ("低空经济", "002179", "中航光电", "A", "高压连接器"),
    ("低空经济", "002025", "航天电器", "A", "电连接器"),
    ("低空经济", "EH", "EHang Holdings 亿航智能", "US", "eVTOL整机"),
    ("低空经济", "JOBY", "Joby Aviation", "US", "eVTOL整机"),

    # 5. 储能细分 (CN + US)
    ("储能细分", "300274", "阳光电源", "A", "PCS/逆变器"),
    ("储能细分", "688411", "海博思创", "A", "储能集成"),
    ("储能细分", "300693", "盛弘股份", "A", "PCS"),
    ("储能细分", "TSLA", "Tesla", "US", "Megapack"),
    ("储能细分", "ENPH", "Enphase Energy", "US", "户储"),

    # 6. 创新药 (CN + US)
    ("创新药", "600276", "恒瑞医药", "A", "创新biotech"),
    ("创新药", "688235", "百济神州", "A", "创新biotech"),
    ("创新药", "688180", "君实生物", "A", "创新biotech"),
    ("创新药", "LLY", "Eli Lilly", "US", "GLP-1/代谢"),
    ("创新药", "REGN", "Regeneron Pharmaceuticals", "US", "免疫/眼科"),

    # 7. 医疗器械国产替代 (CN + US benchmark)
    ("医疗器械国产替代", "300760", "迈瑞医疗", "A", "医疗设备"),
    ("医疗器械国产替代", "688271", "联影医疗", "A", "高端影像"),
    ("医疗器械国产替代", "300003", "乐普医疗", "A", "心血管植入"),
    ("医疗器械国产替代", "002901", "大博医疗", "A", "骨科植入"),
    ("医疗器械国产替代", "ISRG", "Intuitive Surgical", "US", "对标(手术机器人)"),

    # 8. 半导体材料 (CN + KR + US)
    ("半导体材料", "688126", "沪硅产业", "A", "硅片"),
    ("半导体材料", "688268", "华特气体", "A", "电子特气"),
    ("半导体材料", "014680", "한솔케미칼", "KR", "电子化学品"),
    ("半导体材料", "005290", "동진세미켐", "KR", "光刻胶"),
    ("半导体材料", "ENTG", "Entegris", "US", "纯水/CMP耗材"),

    # 9. 半导体设备 (CN + KR + US)
    ("半导体设备", "002371", "北方华创", "A", "刻蚀/沉积"),
    ("半导体设备", "688012", "中微公司", "A", "刻蚀"),
    ("半导体设备", "688082", "盛美上海", "A", "清洗"),
    ("半导体设备", "042700", "한미반도체", "KR", "HBM TC本더"),
    ("半导体设备", "ASML", "ASML Holding", "US", "光刻"),

    # 10. 工业自动化 (CN + US)
    ("工业自动化", "300124", "汇川技术", "A", "工业控制"),
    ("工业自动化", "002747", "埃斯顿", "A", "工业机器人"),
    ("工业自动化", "ROK", "Rockwell Automation", "US", "工业控制"),
    ("工业自动化", "EMR", "Emerson Electric", "US", "过程自动化"),
    ("工业自动化", "TER", "Teradyne", "US", "测试/UR协作机器人"),

    # 11. 数据中心配套 (CN + KR + US)
    ("数据中心配套", "300820", "英杰电气", "A", "DC电源"),
    ("数据中心配套", "002028", "思源电气", "A", "变压器"),
    ("数据中心配套", "267260", "HD현대일렉트릭", "KR", "变压器/开关柜"),
    ("数据中心配套", "VRT", "Vertiv Holdings", "US", "液冷+电源"),
    ("数据中心配套", "DLR", "Digital Realty Trust", "US", "DC REIT"),

    # 12. 新材料 (CN + KR + US)
    ("新材料", "300699", "光威复材", "A", "碳纤维"),
    ("新材料", "600884", "杉杉股份", "A", "负极材料"),
    ("新材料", "002709", "天赐材料", "A", "电解液"),
    ("新材料", "003670", "POSCO Future M", "KR", "正极/前驱体"),
    ("新材料", "ALB", "Albemarle", "US", "锂"),

    # 13. 特高压 (CN + US 类比)
    ("特高压", "600406", "国电南瑞", "A", "二次设备"),
    ("特高压", "600089", "特变电工", "A", "变压器"),
    ("特高压", "600268", "国电南自", "A", "二次设备"),
    ("特高压", "600312", "平高电气", "A", "GIS"),
    ("特高压", "PWR", "Quanta Services", "US", "对标(美电网建设)"),

    # 14. 生物制造 (CN + US)
    ("生物制造", "688065", "凯赛生物", "A", "生物基材料"),
    ("生物制造", "688363", "华熙生物", "A", "透明质酸"),
    ("生物制造", "603739", "蔚蓝生物", "A", "酶/微生物"),
    ("生物制造", "002007", "华兰生物", "A", "血液制品/疫苗"),
    ("生物制造", "DNA", "Ginkgo Bioworks", "US", "合成生物平台"),

    # 15. 韩国电池 (KR + US下游)
    ("韩国电池", "373220", "LG에너지솔루션", "KR", "动力电池"),
    ("韩国电池", "006400", "삼성SDI", "KR", "动力电池"),
    ("韩国电池", "096770", "SK이노베이션", "KR", "动力电池"),
    ("韩国电池", "247540", "EcoPro BM 에코프로비엠", "KR", "正极材料"),
    ("韩国电池", "TSLA", "Tesla", "US", "下游车厂客户"),

    # 16. 人形机器人 (observation, 1-2)
    ("人形机器人", "688017", "绿的谐波", "A", "谐波减速器(Tesla Optimus供应链)"),
    ("人形机器人", "TSLA", "Tesla", "US", "Optimus"),

    # 17. 消费升级/IP经济 (observation, 1-2)
    ("消费升级/IP经济", "300144", "宋城演艺", "A", "文化演艺/IP"),
    ("消费升级/IP经济", "DIS", "The Walt Disney Company", "US", "IP/媒体"),
]


def load_industry_id_map(con: sqlite3.Connection) -> dict[str, int]:
    """Map industry_name -> industry_id from watchlist."""
    return {
        r["industry_name"]: r["industry_id"]
        for r in con.execute("SELECT industry_id, industry_name FROM watchlist")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not write")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        id_map = load_industry_id_map(con)

        unknown_industries = sorted({c[0] for c in CANDIDATES if c[0] not in id_map})
        if unknown_industries:
            print("WARN: industries not in watchlist (industry_id will be NULL):")
            for ind in unknown_industries:
                print(f"  - {ind}")

        inserted = 0
        skipped = 0
        existing_before = con.execute("SELECT COUNT(*) FROM related_stocks").fetchone()[0]

        with con:
            con.execute("BEGIN IMMEDIATE")
            for industry, code, name, market, sub in CANDIDATES:
                ind_id = id_map.get(industry)
                # Idempotent: SELECT-before-INSERT (UNIQUE(industry, stock_code) anchors)
                exists = con.execute(
                    "SELECT 1 FROM related_stocks WHERE industry=? AND stock_code=?",
                    (industry, code),
                ).fetchone()
                if exists:
                    skipped += 1
                    continue
                if args.dry_run:
                    inserted += 1
                    continue
                con.execute(
                    """INSERT INTO related_stocks
                       (industry_id, industry, sub_industry, stock_code, stock_name, market,
                        discovery_source, discovered_at, confidence, status, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ind_id, industry, sub, code, name, market,
                     DISCOVERY_SOURCE, now, CONFIDENCE, STATUS, now),
                )
                inserted += 1

        existing_after = con.execute("SELECT COUNT(*) FROM related_stocks").fetchone()[0]

        print()
        print("=== v1.05 related_stocks load ===")
        print(f"  Mode:     {'DRY-RUN' if args.dry_run else 'WRITE'}")
        print(f"  Inserted: {inserted}")
        print(f"  Skipped:  {skipped} (already present)")
        print(f"  Total before: {existing_before}")
        print(f"  Total after:  {existing_after}")

        # Per-industry summary
        print()
        print("=== Per-industry counts ===")
        rows = con.execute("""
            SELECT industry, COUNT(*) AS n,
                   GROUP_CONCAT(market || ':' || stock_code, ', ') AS tickers
            FROM related_stocks
            GROUP BY industry
            ORDER BY industry
        """).fetchall()
        for r in rows:
            print(f"  [{r['n']}] {r['industry']}: {r['tickers']}")

    finally:
        con.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
