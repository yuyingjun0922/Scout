"""Step 2 验证脚本：push_consumer 通过 QQ C2C API 投递测试消息。

流程：
  1. 加载 config.yaml（校验 qq_push 段 schema）
  2. 打开 queue.db / knowledge.db
  3. 插 2 条 producer='manual_test' 的 alert 消息
  4. 构建 QQPushChannel + PushConsumerAgent.deliver_pending(producer='manual_test')
  5. 打印投递结果 + 投递前后的 message_queue 状态

**重要：** 只处理 producer='manual_test' 的消息，存量 5 条真实业务消息（motivation_drift/
daily_briefing/weekly_report）保持 pending，不会被本脚本推送。

运行:  python scripts\test_push_consumer_qq.py
"""
from __future__ import annotations

import io
import os
import sys
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)  # config.yaml 里的 data/knowledge.db 是相对路径

# Windows console UTF-8
if sys.platform.startswith("win"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from agents.push_consumer_agent import PushConsumerAgent  # noqa: E402
from config.loader import load_config  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from infra.push_queue import PushQueue  # noqa: E402
from infra.qq_channel import build_qq_channel_from_config  # noqa: E402
from infra.queue_manager import QueueManager  # noqa: E402


MANUAL_PRODUCER = "manual_test"


def print_queue_stats(qm: QueueManager, label: str) -> None:
    conn = sqlite3.connect(qm.db_path)
    conn.row_factory = sqlite3.Row
    print(f"\n─── {label} ───")
    for r in conn.execute(
        """SELECT status, producer, COUNT(*) cnt
           FROM message_queue WHERE queue_name='push_outbox'
           GROUP BY status, producer ORDER BY status, producer"""
    ).fetchall():
        print(f"  status={r['status']:<10} producer={r['producer']:<25} count={r['cnt']}")
    conn.close()


def main() -> int:
    print("=" * 70)
    print("Scout push_consumer → QQ 推送验证")
    print("=" * 70)

    cfg = load_config()
    if cfg.qq_push is None or not cfg.qq_push.enabled:
        print("❌ config.yaml 里 qq_push 未配置或 enabled=false")
        return 1
    print(f"[config] qq_push enabled, user_openid={cfg.qq_push.user_openid[:12]}...")

    kdb = DatabaseManager(cfg.database.knowledge_db)
    qm = QueueManager(cfg.database.queue_db)
    try:
        push_queue = PushQueue(qm)
        agent = PushConsumerAgent(kdb=kdb, push_queue=push_queue)

        # ─ 1. 先把上次测试残留清掉（幂等 entity_key 会让新插入无效） ─
        qm._ensure_open()
        cleared = qm.conn.execute(
            """UPDATE message_queue SET status='failed',
               error_message='superseded_by_new_test'
               WHERE queue_name='push_outbox' AND producer=?
                 AND status='pending'""",
            (MANUAL_PRODUCER,),
        ).rowcount
        if cleared:
            print(f"[cleanup] 旧 manual_test pending 清理 {cleared} 条")

        # ─ 2. 插 2 条 alert 测试消息 ─
        # 用唯一 entity_key 避免幂等拦截
        import time
        ts = int(time.time())
        id1 = push_queue.push_alert(
            alert_type="motivation_drift",
            content={
                "industry": "测试行业",
                "state": "drifting",
                "note": f"[Scout测试] push_consumer测试1 @ {ts}",
            },
            priority="blue",
            producer=MANUAL_PRODUCER,
            entity_key=f"manual_test_1_{ts}",
        )
        id2 = push_queue.push_alert(
            alert_type="failure_level_change",
            content={
                "stock": "000000",
                "note": f"[Scout测试] push_consumer测试2 @ {ts}",
            },
            priority="yellow",
            producer=MANUAL_PRODUCER,
            entity_key=f"manual_test_2_{ts}",
        )
        print(f"\n[insert] event_id#1={id1}")
        print(f"[insert] event_id#2={id2}")

        print_queue_stats(qm, "投递前 queue 状态")

        # ─ 3. 构建 channel 并 deliver ─
        channel = build_qq_channel_from_config(cfg.qq_push)
        print("\n[channel] QQPushChannel 构造完成，开始 deliver_pending")

        result = agent.deliver_pending(
            channel=channel,
            max=10,
            producer=MANUAL_PRODUCER,
        )

        print("\n─── 投递结果 ───")
        for k, v in result.items():
            if k == "errors":
                if v:
                    print("  errors:")
                    for e in v:
                        print(f"    · {e}")
                else:
                    print("  errors: (none)")
            else:
                print(f"  {k}: {v}")

        print_queue_stats(qm, "投递后 queue 状态")

        ok = result.get("delivered", 0) == 2 and result.get("failed", 0) == 0
        print("\n" + ("✅ 两条测试消息均发送成功" if ok else "⚠️  未完全成功，见 errors"))
        return 0 if ok else 2
    finally:
        try:
            qm.conn.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            kdb.conn.close()  # type: ignore[union-attr]
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
