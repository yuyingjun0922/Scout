"""Step 3 验证脚本：health_monitor 两类推送端到端测试。

流程：
  A. 🟢 心跳
     - 清理今天的 heartbeat pending（如果前一次测试留下）
     - monitor.run_daily_heartbeat() → push_alert(system_health, white)
     - consumer.deliver_pending(channel, producer='health_monitor')
     - 用户 QQ 应收到 🟢 [health · white] 一条

  B. 🔴 错误告警
     - 插入 1 条测试 agent_errors（agent_name='HealthMonitorTest', error_type='rule'）
     - monitor.run_check_errors(window_minutes=15) → push_alert(data_source_down, yellow)
     - consumer.deliver_pending(channel, producer='health_monitor')
     - 用户 QQ 应收到 🔴 [alert · yellow] 一条
     - 最后把测试错误行 mark resolved，不污染 24h stats

只处理 `producer='health_monitor'` 的 pending，其它业务队列不会被动到。

运行:  python scripts\\test_health_monitor.py [--only heartbeat|errors]
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import sqlite3
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

if sys.platform.startswith("win"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from agents.health_monitor_agent import HealthMonitorAgent  # noqa: E402
from agents.push_consumer_agent import PushConsumerAgent  # noqa: E402
from config.loader import load_config  # noqa: E402
from infra.db_manager import DatabaseManager  # noqa: E402
from infra.push_queue import PushQueue  # noqa: E402
from infra.qq_channel import build_qq_channel_from_config  # noqa: E402
from infra.queue_manager import QueueManager  # noqa: E402
from utils.time_utils import now_utc  # noqa: E402

PRODUCER = "health_monitor"


def clear_pending_producer(qm: QueueManager, producer: str) -> int:
    qm._ensure_open()
    cur = qm.conn.execute(
        """UPDATE message_queue SET status='failed',
           error_message='superseded_by_new_test'
           WHERE queue_name='push_outbox' AND producer=? AND status='pending'""",
        (producer,),
    )
    return cur.rowcount


def test_heartbeat(monitor, consumer, channel, qm) -> bool:
    print("\n─── 场景 A: 🟢 心跳 ───")
    cleared = clear_pending_producer(qm, PRODUCER)
    if cleared:
        print(f"[cleanup] 清理旧 health_monitor pending {cleared} 条")

    result = monitor.run_daily_heartbeat()
    print(f"[heartbeat] ok={result.get('ok')} event_id={result.get('event_id')}")
    if result.get("stats"):
        for k, v in result["stats"].items():
            print(f"           {k}: {v}")

    if not result.get("event_id"):
        print("❌ 心跳 push 失败")
        return False

    deliver = consumer.deliver_pending(channel, max=5, producer=PRODUCER)
    print(f"[deliver]  delivered={deliver['delivered']} failed={deliver['failed']} "
          f"errors={deliver['errors']}")
    return deliver["delivered"] >= 1 and deliver["failed"] == 0


def test_errors(kdb, monitor, consumer, channel, qm) -> bool:
    print("\n─── 场景 B: 🔴 错误告警 ───")
    cleared = clear_pending_producer(qm, PRODUCER)
    if cleared:
        print(f"[cleanup] 清理旧 health_monitor pending {cleared} 条")

    # 1. 插测试 agent_errors
    test_agent = "HealthMonitorTest"
    test_msg = f"[Scout测试] health_monitor check_errors @ {int(time.time())}"
    row_id = kdb.write(
        """INSERT INTO agent_errors (agent_name, error_type, error_message,
           context_data, occurred_at, resolved)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (test_agent, "rule", test_msg, "{}", now_utc()),
    )
    print(f"[insert]   agent_errors.id={row_id} agent_name={test_agent}")

    # 2. 触发扫描
    result = monitor.run_check_errors(window_minutes=15)
    print(f"[scan]     checked={result.get('checked_errors')} "
          f"agents={result.get('agents_with_errors')} "
          f"alerts={result.get('alerts_pushed')}")
    pushed = result.get("pushed") or []
    for p in pushed:
        print(f"           pushed: {p}")

    # 3. deliver
    deliver = consumer.deliver_pending(channel, max=5, producer=PRODUCER)
    print(f"[deliver]  delivered={deliver['delivered']} failed={deliver['failed']} "
          f"errors={deliver['errors']}")

    # 4. 清理：把刚插入的错误标 resolved，防止污染 24h stats
    kdb.write(
        "UPDATE agent_errors SET resolved=1, resolved_at=? WHERE id=?",
        (now_utc(), row_id),
    )
    print(f"[cleanup]  agent_errors.id={row_id} marked resolved")

    ok = False
    for p in pushed:
        if p.get("agent") == test_agent:
            ok = True
            break
    return ok and deliver["delivered"] >= 1 and deliver["failed"] == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("heartbeat", "errors"), default=None,
                    help="只跑一类；默认两类都跑")
    args = ap.parse_args()

    print("=" * 70)
    print("Scout health_monitor → QQ 推送验证")
    print("=" * 70)

    cfg = load_config()
    if cfg.qq_push is None or not cfg.qq_push.enabled:
        print("❌ qq_push 未启用")
        return 1

    kdb = DatabaseManager(cfg.database.knowledge_db)
    qm = QueueManager(cfg.database.queue_db)
    try:
        push_queue = PushQueue(qm)
        monitor = HealthMonitorAgent(kdb=kdb, push_queue=push_queue)
        consumer = PushConsumerAgent(kdb=kdb, push_queue=push_queue)
        channel = build_qq_channel_from_config(cfg.qq_push)

        scenarios = []
        if args.only in (None, "heartbeat"):
            scenarios.append(("heartbeat", lambda: test_heartbeat(monitor, consumer, channel, qm)))
        if args.only in (None, "errors"):
            scenarios.append(("errors", lambda: test_errors(kdb, monitor, consumer, channel, qm)))

        results = {}
        for name, fn in scenarios:
            try:
                results[name] = fn()
            except Exception as e:
                print(f"[{name}] 异常: {type(e).__name__}: {e}")
                results[name] = False

        print("\n─── 总结 ───")
        for k, v in results.items():
            print(f"  {k}: {'✅' if v else '❌'}")
        return 0 if all(results.values()) else 2
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
