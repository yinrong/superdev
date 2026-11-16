"""superdev.events 的测试：JSONL 事件日志、防篡改哈希链、派发消费、并发追加。"""

import json
import threading
from datetime import datetime

import pytest

from superdev import events as ev
from superdev.events import EventLog


def _read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [line for line in f.read().splitlines() if line]


def _write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _tamper(path, event_id, new_payload):
    """改掉某条事件的 payload 后原样写回，模拟离线篡改。"""
    lines = _read_lines(path)
    rec = json.loads(lines[event_id - 1])
    rec["payload"] = new_payload
    lines[event_id - 1] = json.dumps(rec, ensure_ascii=False)
    _write_lines(path, lines)


def test_append_and_read(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    r1 = log.append(ev.TASK_CREATED, {"task_id": "T01"})
    log.append(ev.ROUND_GREEN, {"task_id": "T01", "attempt": 1})

    assert r1["event_id"] == 1
    assert r1["kind"] == ev.TASK_CREATED
    assert r1["payload"] == {"task_id": "T01"}
    assert len(log) == 2

    stored = [json.loads(line) for line in _read_lines(log.path)]
    assert [e["event_id"] for e in stored] == [1, 2]
    assert [e["kind"] for e in stored] == [ev.TASK_CREATED, ev.ROUND_GREEN]
    assert datetime.fromisoformat(r1["created_at"])  # created_at 是合法 ISO 时间


def test_kind_constants_match_design_table():
    assert (
        ev.TASK_CREATED,
        ev.WORKER_IDLE,
        ev.ROUND_GREEN,
        ev.ROUND_RED,
        ev.TASK_STUCK,
        ev.REPRO_SPLIT,
        ev.EXPERT_DECIDED,
        ev.ALL_GREEN,
        ev.METRICS_STALL,
    ) == (
        "TASK_CREATED",
        "WORKER_IDLE",
        "ROUND_GREEN",
        "ROUND_RED",
        "TASK_STUCK",
        "REPRO_SPLIT",
        "EXPERT_DECIDED",
        "ALL_GREEN",
        "METRICS_STALL",
    )


def test_chain_links_prev_hash(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(3):
        log.append(ev.WORKER_IDLE, {"i": i})

    stored = [json.loads(line) for line in _read_lines(log.path)]
    assert stored[0]["prev_hash"] == ev.GENESIS_HASH
    assert stored[1]["prev_hash"] == stored[0]["hash"]
    assert stored[2]["prev_hash"] == stored[1]["hash"]


def test_verify_ok_on_intact_chain(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(4):
        log.append(ev.METRICS_STALL, {"i": i})
    assert log.verify() == (True, 0)


def test_verify_empty_log(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    assert log.verify() == (True, 0)
    assert log.events() == []


def test_verify_catches_tampered_middle_line(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(5):
        log.append(ev.ROUND_RED, {"attempt": i})
    _tamper(log.path, 3, {"attempt": 999})
    assert log.verify() == (False, 3)


def test_verify_catches_tampered_first_line(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(3):
        log.append(ev.ALL_GREEN, {"i": i})
    _tamper(log.path, 1, {"i": "改了"})
    assert log.verify() == (False, 1)


def test_verify_catches_deleted_line(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(4):
        log.append(ev.TASK_STUCK, {"i": i})
    lines = _read_lines(log.path)
    del lines[1]  # 抽掉第 2 条，链在位置 2 断开
    _write_lines(log.path, lines)
    assert log.verify() == (False, 2)


def test_unfired_and_mark_fired(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(4):
        log.append(ev.TASK_CREATED, {"i": i})

    assert [e["event_id"] for e in log.unfired()] == [1, 2, 3, 4]
    assert log.mark_fired(2) is True
    assert [e["event_id"] for e in log.unfired()] == [1, 3, 4]
    assert [e["event_id"] for e in log.unfired(limit=2)] == [1, 3]
    assert log.mark_fired(2) is False  # 重复标记幂等
    with pytest.raises(KeyError):
        log.mark_fired(99)


def test_fired_state_survives_reopen(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append(ev.REPRO_SPLIT, {"case": "c1"})
    log.append(ev.EXPERT_DECIDED, {"choice": "换引擎"})
    log.mark_fired(1)

    reopened = EventLog(path)
    assert len(reopened) == 2
    assert [e["event_id"] for e in reopened.unfired()] == [2]
    assert reopened.verify() == (True, 0)


def test_concurrent_append_no_loss_no_dup(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    n_threads, per_thread = 4, 5
    barrier = threading.Barrier(n_threads)

    def worker(t):
        barrier.wait()
        for i in range(per_thread):
            log.append(ev.WORKER_IDLE, {"t": t, "i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(log) == n_threads * per_thread  # 20 条不丢
    ids = sorted(e["event_id"] for e in log.events())
    assert ids == list(range(1, n_threads * per_thread + 1))  # 自增不重
    assert len(log.unfired()) == n_threads * per_thread
    assert log.verify() == (True, 0)  # 并发写完链仍完整
