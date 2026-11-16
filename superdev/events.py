"""append-only 事件日志：JSONL 存储，防篡改哈希链。

设计出处：design/03-详细设计.md §8.3——每条事件的哈希覆盖上一条事件的哈希，
形成链式签名，任何人可离线验证"从任务创建到合并"的决策链没被改过。
事件 kind 常量命名对齐 §3.2 迁移触发表（round_red_1/2 合并为 ROUND_RED，
两败卡住另发 TASK_STUCK）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

TASK_CREATED = "TASK_CREATED"
WORKER_IDLE = "WORKER_IDLE"
ROUND_GREEN = "ROUND_GREEN"
ROUND_RED = "ROUND_RED"
TASK_STUCK = "TASK_STUCK"
REPRO_SPLIT = "REPRO_SPLIT"
EXPERT_DECIDED = "EXPERT_DECIDED"
ALL_GREEN = "ALL_GREEN"
METRICS_STALL = "METRICS_STALL"

GENESIS_HASH = "0" * 64  # 链头：第一条事件之前没有上一条


def _canonical(record: dict) -> str:
    """哈希与落盘共用的稳定序列化：键排序，无空白，UTF-8。"""
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _compute_hash(record: dict) -> str:
    # 除 hash 字段外的整行内容进哈希（prev_hash 天然在内，链由此成立）
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


class EventLog:
    """线程安全的 append-only 事件日志（JSONL + 哈希链 + 已触发标记）。

    fired 状态是派发器的消费进度，不属于链的一部分，
    存在旁车文件 <path>.fired，保证主日志严格 append-only。
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._fired_path = Path(str(self._path) + ".fired")
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._fired: set[int] = set()
        self._load()

    def _load(self) -> None:
        """启动时恢复内存状态。宽容解析，完整性问题交给 verify() 判定。"""
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        break  # 坏行之后 prev_hash 已不可信，不再往下装
                    if isinstance(rec, dict) and isinstance(rec.get("event_id"), int):
                        self._events.append(rec)
        if self._fired_path.exists():
            with open(self._fired_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip().isdigit():
                        self._fired.add(int(line))

    @property
    def path(self) -> Path:
        return self._path

    def append(self, kind: str, payload: dict) -> dict:
        """追加一条事件，返回完整记录（含 hash）。"""
        with self._lock:
            prev = self._events[-1]["hash"] if self._events else GENESIS_HASH
            record = {
                "event_id": len(self._events) + 1,
                "kind": kind,
                "payload": payload,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "prev_hash": prev,
            }
            record["hash"] = _compute_hash(record)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(_canonical(record) + "\n")
                f.flush()
            self._events.append(record)
            return dict(record)

    def events(self, limit: int | None = None) -> list[dict]:
        """按 event_id 升序返回事件副本。"""
        with self._lock:
            items = [dict(e) for e in self._events]
        return items if limit is None else items[:limit]

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def verify(self) -> tuple[bool, int]:
        """离线校验整条哈希链，直接读文件（防内存与磁盘不一致）。

        返回 (是否完整, 第一处坏行的链上位置)。坏行按 1 起的位置计：
        行解析失败、event_id 不连续、prev_hash 断链、哈希对不上，任一即坏。
        空日志或文件不存在返回 (True, 0)。
        """
        with self._lock:
            if not self._path.exists():
                return True, 0
            prev_hash = GENESIS_HASH
            expected_id = 1
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        return False, expected_id
                    try:
                        rec = json.loads(line)
                        ok = (
                            isinstance(rec, dict)
                            and rec.get("event_id") == expected_id
                            and rec.get("prev_hash") == prev_hash
                            and _compute_hash(rec) == rec.get("hash")
                        )
                    except (ValueError, TypeError):
                        ok = False
                    if not ok:
                        return False, expected_id
                    prev_hash = rec["hash"]
                    expected_id += 1
            return True, 0

    def unfired(self, limit: int | None = None) -> list[dict]:
        """返回尚未触发的事件（升序），最多 limit 条。派发器消费入口。"""
        with self._lock:
            pending = [
                dict(e) for e in self._events if e["event_id"] not in self._fired
            ]
        return pending if limit is None else pending[:limit]

    def mark_fired(self, event_id: int) -> bool:
        """标记事件已触发。首次标记返回 True，重复标记幂等返回 False。"""
        with self._lock:
            if not any(e["event_id"] == event_id for e in self._events):
                raise KeyError(f"未知事件 id: {event_id}")
            if event_id in self._fired:
                return False
            self._fired.add(event_id)
            with open(self._fired_path, "a", encoding="utf-8") as f:
                f.write(f"{event_id}\n")
                f.flush()
            return True
