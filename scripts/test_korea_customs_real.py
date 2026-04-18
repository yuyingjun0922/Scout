#!/usr/bin/env python
"""
scripts/test_korea_customs_real.py — V3 韩国关税厅真网络烟囱测试

⚠️ Phase 1 已知限制：
    UniPass / tradedata.go.kr 整站 JS 驱动，直接 HTTP 只能拿到骨架错误页。
    本脚本**预计会失败**并在 agent_errors 表留下 parse 错误（~1.5KB 错误页检测）。
    详见 infra/data_adapters/korea_customs.py 的模块 docstring 顶部。

Phase 2A 升级路径：
    (A) 引入 playwright，headless 渲染 JS 后抓 DOM
    (B) 申请 data.go.kr API（需韩国本地手机认证）

手工跑：
    python scripts/test_korea_customs_real.py
    python scripts/test_korea_customs_real.py --hs 8542
    python scripts/test_korea_customs_real.py --prod-db
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.data_adapters.korea_customs import KoreaCustomsCollector  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Korea Customs (V3) real-network smoke test")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument(
        "--hs",
        default=None,
        choices=list(KoreaCustomsCollector.HS_CODES_PHASE1.keys()),
        help="单个 HS code；默认采全部 3 个 Phase 1 HS",
    )
    parser.add_argument(
        "--prod-db",
        action="store_true",
        help="写 data/knowledge.db 主库（默认写 data/test_knowledge.db）",
    )
    args = parser.parse_args()

    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name
    if not db_path.exists():
        print(f"[init] creating {db_path}")
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        hs_codes = [args.hs] if args.hs else None
        collector = KoreaCustomsCollector(db=db, hs_codes=hs_codes)

        print(f"[V3 Korea Customs] months={args.months} hs_codes={collector.hs_codes} db={db_name}")
        print("[fetch] UniPass → tradedata.go.kr（预计失败，详见脚本 docstring）")
        print(f"[fetch] attempting HTTP GET→POST flow on {collector.BASE_URL}")

        units = collector.collect_recent(months=args.months)

        if not units:
            print()
            print("[RESULT] ⚠ 未拿到数据（符合 Phase 1 预期）")
            print("  UniPass 整站 JS-driven，纯 HTTP 仅得骨架错误页。")
            print(f"  检查 agent_errors：")
            print(
                f"  sqlite3 {db_path} "
                "\"SELECT error_type, substr(error_message, 1, 200) FROM agent_errors "
                "WHERE agent_name='korea_customs_v3' ORDER BY occurred_at DESC LIMIT 5\""
            )
            print()
            print("  Phase 2A 升级：")
            print("    (A) playwright headless 渲染")
            print("    (B) data.go.kr REST API (需韩国手机认证)")

            # 打印前几条错误给用户看
            errors = db.query(
                "SELECT error_type, substr(error_message, 1, 200) AS msg "
                "FROM agent_errors WHERE agent_name='korea_customs_v3' "
                "ORDER BY occurred_at DESC LIMIT 5"
            )
            if errors:
                print()
                print("  最近错误：")
                for r in errors:
                    print(f"    [{r['error_type']}] {r['msg']}")
            return 1

        print(f"\n[ok] got {len(units)} rows (unexpected success — worth a closer look):")
        for u in units[:5]:
            c = json.loads(u.content)
            print(f"  {c['hs_code']} {c['period']}: export=${c['export_usd']:,.0f} "
                  f"import=${c['import_usd']:,.0f} yoy={c['yoy_export_pct']}%")

        added = collector.persist_batch(units)
        print(f"[db] persisted {added} new rows")

        total = db.query_one(
            "SELECT COUNT(*) AS n FROM info_units WHERE source='V3'"
        )["n"]
        print(f"[db] total V3 rows in {db_name}: {total}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
