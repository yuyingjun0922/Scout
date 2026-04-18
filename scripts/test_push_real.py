#!/usr/bin/env python
"""
scripts/test_push_real.py — Step 11 PushQueue 真数据烟囱

验证生产+订阅完整流程：
    1. 推送一条 daily_briefing（blue）
    2. 推送一条 weekly_report industry（blue）
    3. 推送一条 alert data_source_down（red）
    4. 再推一条 daily_briefing 同日 → 幂等（返同 event_id）
    5. poll_pending(10) 看排序：red > blue
    6. mark_delivered 第 1 条 → status='done'
    7. mark_failed 第 2 条 → retries+=1，回 pending
    8. queue_stats 最终快照

默认写 data/queue.db（独立库）。加 --fresh 用 tmp queue.db。

用法：
    python scripts/test_push_real.py
    python scripts/test_push_real.py --fresh
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.push_queue import PushQueue  # noqa: E402
from infra.queue_manager import QueueManager  # noqa: E402
from knowledge.init_queue_db import init_queue_db  # noqa: E402


def _pp(title: str, obj) -> None:
    print(f"\n--- {title} ---")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def main() -> int:
    parser = argparse.ArgumentParser(description="PushQueue real smoke test")
    parser.add_argument("--fresh", action="store_true",
                        help="用临时 queue.db 跑（不影响 data/queue.db）")
    parser.add_argument("--queue-db", default=None,
                        help="显式指定 queue.db 路径")
    args = parser.parse_args()

    if args.fresh:
        tmp = Path(tempfile.mkdtemp(prefix="scout_push_"))
        qdb_path = tmp / "queue.db"
        print(f"[fresh] 使用临时 queue.db: {qdb_path}")
    elif args.queue_db:
        qdb_path = Path(args.queue_db)
    else:
        qdb_path = PROJECT_ROOT / "data" / "queue.db"

    if not qdb_path.exists():
        print(f"[init] creating {qdb_path}")
        init_queue_db(qdb_path)

    qm = QueueManager(qdb_path)
    try:
        pq = PushQueue(queue_manager=qm)

        # ── 0. 清理 Step 11 烟囱历史（只清 push_outbox 的 test entity）──
        cleared = qm.conn.execute(
            """DELETE FROM message_queue
               WHERE queue_name='push_outbox'
                 AND entity_key LIKE 'push_real_smoke_%'"""
        ).rowcount
        qm.conn.commit()
        if cleared:
            print(f"[cleanup] removed {cleared} prior smoke rows")

        print(f"\n{'=' * 60}")
        print(f"[phase 1] 生产（push）")
        print("=" * 60)

        # 用 smoke-specific entity_key 以便反复跑不撞天然同日的 entity_key
        ev_brief = pq.push_daily_briefing(
            {
                "highlights": [
                    "半导体：本周 D1 新增 2 条政策",
                    "论文：20 篇 D4 新增，TOP 集中在材料/量子传输",
                ],
                "active_industries": 1,
            },
            target_date="20260418",  # 明确给，避免 KST 边界
        )
        # 覆盖 entity_key 避免和 production 真实用户的 daily_briefing_20260418 冲突
        # （production 可能已经推过）→ 用另一条专属 smoke 的
        ev_brief_smoke = pq.push(
            "daily_briefing",
            {"smoke": True, "highlights": ["test"]},
            entity_key="push_real_smoke_daily_20260418",
        )
        _pp("daily_briefing 原始", {"event_id": ev_brief})
        _pp("daily_briefing smoke", {"event_id": ev_brief_smoke})

        ev_report = pq.push(
            "weekly_report",
            {"report_type": "industry", "summary": "半导体本周 supportive 主导"},
            priority="blue",
            producer="scripts/test_push_real.py",
            entity_key="push_real_smoke_weekly_industry_20260418",
        )
        _pp("weekly_report industry", {"event_id": ev_report})

        ev_alert = pq.push(
            "alert",
            {"alert_type": "data_source_down", "source": "V3", "detail": "UniPass blocked"},
            priority="red",
            producer="scripts/test_push_real.py",
            entity_key="push_real_smoke_alert_v3_20260418",
        )
        _pp("alert 🔴 (V3 down)", {"event_id": ev_alert})

        # 幂等验证：再推一次同 entity_key
        ev_brief_again = pq.push(
            "daily_briefing",
            {"smoke": True, "overwrite_attempt": True},
            entity_key="push_real_smoke_daily_20260418",
        )
        print(f"\n[idempotent] 重推同 entity_key → 返回 {ev_brief_again}")
        print(f"           同 event_id? {ev_brief_again == ev_brief_smoke}")
        assert ev_brief_again == ev_brief_smoke, "entity_key 幂等失效"

        # ── 2. poll ──
        print(f"\n{'=' * 60}")
        print(f"[phase 2] 订阅（poll_pending）")
        print("=" * 60)

        pending = pq.poll_pending(max=20)
        print(f"\n[poll] 收到 {len(pending)} 条 pending（按 priority 降序）：")
        for i, m in enumerate(pending, 1):
            print(
                f"  {i}. [{m['priority']}] event_id={m['event_id'][:8]}... "
                f"type={m['message_type']} "
                f"entity_key={m.get('entity_key')}"
            )

        # 验证排序：red 在最前
        smoke_pending = [m for m in pending if (m.get("entity_key") or "").startswith("push_real_smoke_")]
        if smoke_pending:
            print(f"\n[smoke subset] {len(smoke_pending)} 条本次 smoke:")
            priorities = [m["priority"] for m in smoke_pending]
            print(f"  priorities in order: {priorities}")
            # alert 必须在前（red）
            assert priorities[0] == "red", f"red 没在最前: {priorities}"
            print(f"  [ok] red 优先级正确排在最前")

        # ── 3. 订阅者模拟送达 ──
        print(f"\n{'=' * 60}")
        print(f"[phase 3] 订阅者处理（mark_delivered / mark_failed）")
        print("=" * 60)

        # 送达 alert
        ok1 = pq.mark_delivered(ev_alert)
        print(f"[delivered] alert({ev_alert[:8]}...): {ok1}")

        # 失败 weekly_report 一次 → 回 pending，retries=1
        ok2 = pq.mark_failed(ev_report, reason="smoke: simulated delivery failure")
        print(f"[failed-retry] weekly_report({ev_report[:8]}...): {ok2}")

        # 再 mark_failed 2 次触发 MAX_RETRIES → failed
        pq.mark_failed(ev_report, reason="smoke fail 2")
        pq.mark_failed(ev_report, reason="smoke fail 3")
        print(f"[failed-final] weekly_report 应已标 failed（retries=3=MAX）")

        # 送达 daily_briefing
        ok3 = pq.mark_delivered(ev_brief_smoke)
        print(f"[delivered] daily_briefing({ev_brief_smoke[:8]}...): {ok3}")

        # ── 4. 最终快照 ──
        print(f"\n{'=' * 60}")
        print(f"[phase 4] 最终 queue_stats")
        print("=" * 60)
        stats = pq.queue_stats()
        _pp("push_outbox stats", stats)

        # 查本次 smoke 写入的所有行
        rows = qm.conn.execute(
            """SELECT event_id, entity_key, status, retries, error_message
               FROM message_queue
               WHERE entity_key LIKE 'push_real_smoke_%'
               ORDER BY id"""
        ).fetchall()
        print(f"\n[smoke rows] {len(rows)} 条:")
        for r in rows:
            em = r["error_message"] or ""
            print(
                f"  {r['entity_key']:<50}  status={r['status']:<10} "
                f"retries={r['retries']}  err={em[:40]}"
            )

        # ── 验证 ──
        assert any(r["status"] == "done" for r in rows), "至少 1 条应 done"
        assert any(r["status"] == "failed" for r in rows), "weekly_report 应 failed"
        print("\n[ok] 所有 phase 验证通过")
        return 0
    finally:
        qm.close()


if __name__ == "__main__":
    sys.exit(main())
