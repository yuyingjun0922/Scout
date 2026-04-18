"""
tests/test_queue_manager.py — v1.57 消息队列管理器测试

覆盖：
    - enqueue / dequeue / ack / nack 完整生命周期
    - event_id UNIQUE（INSERT OR IGNORE 语义）
    - nack 重试转 pending；达到 MAX_RETRIES 转 failed
    - dequeue 空队列返 None
    - 并发 dequeue 无 double-take（threading）
    - peek 不改状态，仅 pending
    - queue_stats 全/单队列
    - purge_old_done 只清 done
    - 白名单验证（未知 queue_name → RuleViolation）
    - payload JSON round-trip（中文/嵌套/Unicode）
    - 关闭后调用的防御
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agents.base import RuleViolation
from infra.queue_manager import QueueManager
from knowledge.init_queue_db import init_queue_db


# ═══ fixtures ═══

@pytest.fixture
def tmp_queue_path(tmp_path):
    db_path = tmp_path / "queue_test.db"
    init_queue_db(db_path)
    return db_path


@pytest.fixture
def qm(tmp_queue_path):
    q = QueueManager(tmp_queue_path)
    yield q
    q.close()


# ═══ enqueue 基础 ═══

class TestEnqueue:
    def test_returns_event_id(self, qm):
        event_id = qm.enqueue("collection_to_knowledge", {"x": 1}, producer="test")
        assert isinstance(event_id, str)
        # uuid 格式：带短横的 36 字符
        assert len(event_id) == 36
        assert event_id.count("-") == 4

    def test_persists_message(self, qm):
        event_id = qm.enqueue("push_outbox", {"msg": "hello"}, producer="producer1")
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["pending"] == 1

    def test_multiple_enqueues_unique_ids(self, qm):
        ids = set()
        for i in range(5):
            ids.add(qm.enqueue("push_outbox", {"i": i}, producer="p"))
        assert len(ids) == 5  # 都不同

    def test_entity_key_optional(self, qm):
        qm.enqueue("push_outbox", {"x": 1}, producer="p", entity_key="ent-001")
        peeked = qm.peek("push_outbox")
        assert peeked[0]["entity_key"] == "ent-001"

    def test_entity_key_none_ok(self, qm):
        qm.enqueue("push_outbox", {"x": 1}, producer="p")
        peeked = qm.peek("push_outbox")
        assert peeked[0]["entity_key"] is None


class TestEnqueueValidation:
    def test_unknown_queue_name_raises_rule(self, qm):
        with pytest.raises(RuleViolation, match="unknown queue_name"):
            qm.enqueue("bogus_queue", {}, producer="p")

    @pytest.mark.parametrize("bad", [None, "", 123, [], {}])
    def test_non_str_or_empty_queue_name_raises(self, qm, bad):
        with pytest.raises(RuleViolation):
            qm.enqueue(bad, {}, producer="p")

    @pytest.mark.parametrize("bad", [None, "string", 42, [1, 2]])
    def test_non_dict_payload_raises(self, qm, bad):
        with pytest.raises(RuleViolation, match="payload must be dict"):
            qm.enqueue("push_outbox", bad, producer="p")

    @pytest.mark.parametrize("bad", [None, "", 0])
    def test_invalid_producer_raises(self, qm, bad):
        with pytest.raises(RuleViolation, match="producer"):
            qm.enqueue("push_outbox", {}, producer=bad)


# ═══ event_id UNIQUE（INSERT OR IGNORE） ═══

class TestEventIdUnique:
    def test_sqlite_unique_constraint_on_event_id(self, qm):
        qm.conn.execute(
            """INSERT INTO message_queue
               (event_id, queue_name, producer, payload, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("dup-id", "push_outbox", "p", "{}", "2026-01-01T00:00:00+00:00"),
        )
        qm.conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            qm.conn.execute(
                """INSERT INTO message_queue
                   (event_id, queue_name, producer, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("dup-id", "push_outbox", "p", "{}", "2026-01-01T00:00:00+00:00"),
            )

    def test_insert_or_ignore_silent_on_duplicate(self, qm):
        sql = """INSERT OR IGNORE INTO message_queue
                 (event_id, queue_name, producer, payload, created_at)
                 VALUES (?, ?, ?, ?, ?)"""
        params = ("dup-id-2", "push_outbox", "p", "{}", "2026-01-01T00:00:00+00:00")
        qm.conn.execute(sql, params)
        qm.conn.execute(sql, params)  # 第二次被忽略
        qm.conn.commit()
        n = qm.conn.execute(
            "SELECT COUNT(*) FROM message_queue WHERE event_id=?", ("dup-id-2",),
        ).fetchone()[0]
        assert n == 1


# ═══ dequeue ═══

class TestDequeue:
    def test_empty_queue_returns_none(self, qm):
        assert qm.dequeue("push_outbox", consumer="c1") is None

    def test_enqueue_dequeue_roundtrip(self, qm):
        eid = qm.enqueue("push_outbox", {"key": "val", "n": 42}, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        assert msg is not None
        assert msg["event_id"] == eid
        assert msg["payload"] == {"key": "val", "n": 42}
        assert msg["status"] == "processing"
        assert msg["queue_name"] == "push_outbox"
        assert msg["producer"] == "p"
        assert msg["processed_at"] is not None

    def test_dequeue_fifo_order(self, qm):
        ids = [qm.enqueue("push_outbox", {"i": i}, producer="p") for i in range(3)]
        dequeued_ids = []
        for _ in range(3):
            msg = qm.dequeue("push_outbox", consumer="c")
            assert msg is not None
            dequeued_ids.append(msg["event_id"])
        assert dequeued_ids == ids  # FIFO

    def test_dequeue_does_not_cross_queues(self, qm):
        qm.enqueue("collection_to_knowledge", {"x": 1}, producer="p")
        assert qm.dequeue("push_outbox", consumer="c") is None

    def test_dequeue_moves_status_to_processing(self, qm):
        qm.enqueue("push_outbox", {"x": 1}, producer="p")
        qm.dequeue("push_outbox", consumer="c")
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["pending"] == 0
        assert stats["push_outbox"]["processing"] == 1


class TestDequeueValidation:
    def test_unknown_queue_name_raises(self, qm):
        with pytest.raises(RuleViolation):
            qm.dequeue("nope", consumer="c")

    @pytest.mark.parametrize("bad", [None, "", 42])
    def test_invalid_consumer_raises(self, qm, bad):
        with pytest.raises(RuleViolation, match="consumer"):
            qm.dequeue("push_outbox", consumer=bad)


# ═══ ack ═══

class TestAck:
    def test_ack_marks_done(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        assert qm.ack(msg["event_id"]) is True
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["done"] == 1
        assert stats["push_outbox"]["processing"] == 0

    def test_ack_unknown_event_id_returns_false(self, qm):
        assert qm.ack("does-not-exist") is False

    def test_ack_pending_message_returns_false(self, qm):
        """只 processing 能 ack；pending 状态的消息不能"""
        eid = qm.enqueue("push_outbox", {}, producer="p")
        assert qm.ack(eid) is False
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["pending"] == 1
        assert stats["push_outbox"]["done"] == 0

    def test_ack_already_done_returns_false(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        qm.ack(msg["event_id"])
        assert qm.ack(msg["event_id"]) is False

    @pytest.mark.parametrize("bad", [None, "", 0])
    def test_invalid_event_id_raises(self, qm, bad):
        with pytest.raises(RuleViolation):
            qm.ack(bad)


# ═══ nack（重试/失败） ═══

class TestNack:
    def test_nack_first_time_returns_to_pending(self, qm):
        qm.enqueue("push_outbox", {"x": 1}, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        assert qm.nack(msg["event_id"], "first failure") is True
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["pending"] == 1
        assert stats["push_outbox"]["processing"] == 0

    def test_nack_increments_retries(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        qm.nack(msg["event_id"], "err1")

        # 重新 dequeue 同一条，check retries
        msg2 = qm.dequeue("push_outbox", consumer="c")
        assert msg2["event_id"] == msg["event_id"]
        assert msg2["retries"] == 1

    def test_nack_until_failed(self, qm):
        """retries 达到 MAX_RETRIES 时转 failed"""
        qm.enqueue("push_outbox", {}, producer="p")
        eid = None
        for i in range(QueueManager.MAX_RETRIES):
            msg = qm.dequeue("push_outbox", consumer="c")
            assert msg is not None, f"iteration {i}"
            eid = msg["event_id"]
            qm.nack(eid, f"attempt {i + 1}")

        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["failed"] == 1
        assert stats["push_outbox"]["pending"] == 0
        # 已 failed，不再可 dequeue
        assert qm.dequeue("push_outbox", consumer="c") is None

    def test_failed_message_retains_error(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        for i in range(QueueManager.MAX_RETRIES):
            msg = qm.dequeue("push_outbox", consumer="c")
            qm.nack(msg["event_id"], f"attempt-{i}")

        row = qm.conn.execute(
            "SELECT status, retries, error_message FROM message_queue WHERE event_id=?",
            (msg["event_id"],),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["retries"] == QueueManager.MAX_RETRIES
        assert "attempt-" in (row["error_message"] or "")

    def test_nack_pending_returns_false(self, qm):
        """只 processing 能 nack"""
        eid = qm.enqueue("push_outbox", {}, producer="p")
        assert qm.nack(eid, "bogus") is False

    def test_nack_unknown_event_id_returns_false(self, qm):
        assert qm.nack("does-not-exist", "whatever") is False

    @pytest.mark.parametrize("bad", [None, ""])
    def test_invalid_event_id_raises(self, qm, bad):
        with pytest.raises(RuleViolation):
            qm.nack(bad, "x")


# ═══ peek ═══

class TestPeek:
    def test_peek_returns_pending_only(self, qm):
        qm.enqueue("push_outbox", {"a": 1}, producer="p")
        qm.enqueue("push_outbox", {"b": 2}, producer="p")
        e3 = qm.enqueue("push_outbox", {"c": 3}, producer="p")
        # dequeue 一条（变 processing）
        qm.dequeue("push_outbox", consumer="c")

        peeked = qm.peek("push_outbox")
        assert len(peeked) == 2  # 处于 processing 的不算
        assert all(m["status"] == "pending" for m in peeked)

    def test_peek_does_not_change_status(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        qm.peek("push_outbox")
        qm.peek("push_outbox")
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["pending"] == 1

    def test_peek_limit_works(self, qm):
        for i in range(5):
            qm.enqueue("push_outbox", {"i": i}, producer="p")
        peeked = qm.peek("push_outbox", limit=3)
        assert len(peeked) == 3

    def test_peek_empty_queue_returns_empty_list(self, qm):
        assert qm.peek("push_outbox") == []

    def test_peek_invalid_queue_name_raises(self, qm):
        with pytest.raises(RuleViolation):
            qm.peek("not-a-queue")

    @pytest.mark.parametrize("bad", [0, -1, "x", None])
    def test_peek_invalid_limit_raises(self, qm, bad):
        with pytest.raises(RuleViolation):
            qm.peek("push_outbox", limit=bad)


# ═══ queue_stats ═══

class TestQueueStats:
    def test_all_whitelist_queues_present_even_when_empty(self, qm):
        stats = qm.queue_stats()
        for q in QueueManager.QUEUE_NAMES:
            assert q in stats
            assert stats[q] == {"pending": 0, "processing": 0, "done": 0, "failed": 0}

    def test_stats_reflect_mixed_states(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        qm.enqueue("push_outbox", {}, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        qm.ack(msg["event_id"])
        # 一个 done + 一个 pending
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["done"] == 1
        assert stats["push_outbox"]["pending"] == 1

    def test_stats_single_queue_does_not_cross(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        qm.enqueue("collection_to_knowledge", {}, producer="p")
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["pending"] == 1
        assert "collection_to_knowledge" not in stats

    def test_stats_unknown_queue_name_raises(self, qm):
        with pytest.raises(RuleViolation):
            qm.queue_stats("bogus")


# ═══ purge_old_done ═══

class TestPurgeOldDone:
    def test_purges_only_done(self, qm):
        # pending / processing / failed 各一
        e1 = qm.enqueue("push_outbox", {}, producer="p")  # pending
        e2 = qm.enqueue("push_outbox", {}, producer="p")
        qm.dequeue("push_outbox", consumer="c")  # processing
        e3 = qm.enqueue("push_outbox", {}, producer="p")
        # 先 dequeue 再 nack 3 次达 failed
        for _ in range(3):
            m = qm.dequeue("push_outbox", consumer="c")
            if m and m["event_id"] == e3:
                qm.nack(m["event_id"], "x")
            elif m:
                # 别的消息先 dequeue 了，还回去（也 nack）
                qm.nack(m["event_id"], "bounce")
                # 再 dequeue e3
                m2 = qm.dequeue("push_outbox", consumer="c")
                if m2 and m2["event_id"] == e3:
                    qm.nack(m2["event_id"], "x")
        # 简化：就按当前状态计数，关键是 done=0
        # 人工设一条很老的 done
        qm.conn.execute(
            """INSERT INTO message_queue
               (event_id, queue_name, producer, payload, status, created_at, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("old-done", "push_outbox", "p", "{}", "done",
             "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )
        qm.conn.commit()

        removed = qm.purge_old_done(days=30)
        assert removed == 1

        rows = qm.conn.execute(
            "SELECT COUNT(*) FROM message_queue WHERE event_id=?", ("old-done",),
        ).fetchone()[0]
        assert rows == 0

    def test_does_not_purge_recent_done(self, qm):
        qm.enqueue("push_outbox", {}, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        qm.ack(msg["event_id"])

        removed = qm.purge_old_done(days=30)
        assert removed == 0
        stats = qm.queue_stats("push_outbox")
        assert stats["push_outbox"]["done"] == 1

    def test_days_zero_purges_all_done(self, qm):
        """days=0 表示"所有已完成都算旧"—— 测试边界"""
        qm.enqueue("push_outbox", {}, producer="p")
        m = qm.dequeue("push_outbox", consumer="c")
        qm.ack(m["event_id"])
        time.sleep(0.01)  # 确保 processed_at < now
        removed = qm.purge_old_done(days=0)
        assert removed == 1

    def test_invalid_days_raises(self, qm):
        with pytest.raises(RuleViolation):
            qm.purge_old_done(days=-1)


# ═══ 并发 dequeue ═══

class TestConcurrency:
    def test_concurrent_dequeue_no_double_take(self, tmp_queue_path):
        """两个线程同时 dequeue 同一条消息，只能有一个拿到。"""
        # 先入队一条
        qm_seed = QueueManager(tmp_queue_path)
        qm_seed.enqueue("push_outbox", {"race": "test"}, producer="p")
        qm_seed.close()

        results: list = [None, None]
        barrier = threading.Barrier(2)

        def worker(idx: int) -> None:
            local = QueueManager(tmp_queue_path)
            try:
                barrier.wait(timeout=5)
                results[idx] = local.dequeue("push_outbox", consumer=f"t{idx}")
            finally:
                local.close()

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        got = [r for r in results if r is not None]
        nones = [r for r in results if r is None]
        assert len(got) == 1, f"expected exactly 1 winner, got {results}"
        assert len(nones) == 1
        assert got[0]["payload"] == {"race": "test"}

    def test_many_workers_many_messages(self, tmp_queue_path):
        """多 worker 多消息：每条消息恰好被一个 worker 拿到"""
        N_MSGS = 10
        N_WORKERS = 4

        qm_seed = QueueManager(tmp_queue_path)
        for i in range(N_MSGS):
            qm_seed.enqueue("push_outbox", {"i": i}, producer="p")
        qm_seed.close()

        taken: list = []
        lock = threading.Lock()

        def worker() -> None:
            local = QueueManager(tmp_queue_path)
            try:
                while True:
                    msg = local.dequeue("push_outbox", consumer="w")
                    if msg is None:
                        break
                    with lock:
                        taken.append(msg["payload"]["i"])
                    local.ack(msg["event_id"])
            finally:
                local.close()

        threads = [threading.Thread(target=worker) for _ in range(N_WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 每条消息正好被拿一次
        assert sorted(taken) == list(range(N_MSGS))


# ═══ Payload JSON round-trip ═══

class TestPayloadSerialization:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"a": 1},
            {"nested": {"x": [1, 2, 3], "y": {"z": True}}},
            {"chinese": "中文内容"},
            {"emoji": "🚀", "mix": ["abc", 123, None]},
            {"big_str": "x" * 5000},
            {"numbers": [1.5, -2.3, 0, 1e10]},
        ],
    )
    def test_roundtrip(self, qm, payload):
        qm.enqueue("push_outbox", payload, producer="p")
        msg = qm.dequeue("push_outbox", consumer="c")
        assert msg["payload"] == payload


# ═══ 生命周期 ═══

class TestLifecycle:
    def test_close_then_call_raises(self, qm):
        qm.close()
        with pytest.raises(RuleViolation, match="closed"):
            qm.enqueue("push_outbox", {}, producer="p")

    def test_context_manager_closes(self, tmp_queue_path):
        with QueueManager(tmp_queue_path) as qm:
            qm.enqueue("push_outbox", {}, producer="p")
            assert qm.conn is not None
        # 退出 with 后 conn 被置 None
        assert qm.conn is None

    def test_whitelist_queues_exposed(self):
        assert QueueManager.QUEUE_NAMES == frozenset({
            "collection_to_knowledge",
            "knowledge_to_analysis",
            "push_outbox",
        })
