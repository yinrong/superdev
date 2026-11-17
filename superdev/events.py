"""Append-only event log: JSONL storage with a tamper-evident hash chain.

Design source: design/03-detailed-design.md §8.3 — each event's hash covers the
previous event's hash, forming a chained signature that anyone can verify
offline to prove the decision chain "from task creation to merge" was
never altered. Event kind constants are named after the §3.2 transition
trigger table (round_red_1/2 merged into ROUND_RED; a two-failure stall
is a separate TASK_STUCK).
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

GENESIS_HASH = "0" * 64  # chain head: no predecessor before the first event


def _canonical(record: dict) -> str:
    """Stable serialization shared by hashing and disk writes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _compute_hash(record: dict) -> str:
    # Everything except the hash field goes into the hash (prev_hash is
    # naturally included, which is what makes it a chain)
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


class EventLog:
    """Thread-safe append-only event log (JSONL + hash chain + fired markers).

    The fired set is the dispatcher's consumption progress; it is not part
    of the chain and lives in a sidecar file <path>.fired so the main log
    stays strictly append-only.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._fired_path = Path(str(self._path) + ".fired")
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._fired: set[int] = set()
        self._load()

    def _load(self) -> None:
        """Restore in-memory state at startup. Lenient parsing; integrity is verify()'s job."""
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        break  # after a corrupt line, prev_hash is untrustworthy; stop loading
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
        """Append one event and return the full record (including hash)."""
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
        """Return copies of events in ascending event_id order."""
        with self._lock:
            items = [dict(e) for e in self._events]
        return items if limit is None else items[:limit]

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def verify(self) -> tuple[bool, int]:
        """Verify the whole hash chain offline by reading the file directly
        (guards against memory/disk divergence).

        Returns (intact, chain position of the first bad line). Bad lines
        are counted 1-based: unparseable line, non-contiguous event_id,
        broken prev_hash link, or hash mismatch — any of these counts.
        An empty log or a missing file returns (True, 0).
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
        """Return not-yet-fired events (ascending), at most limit of them. The dispatcher's consumption entry."""
        with self._lock:
            pending = [
                dict(e) for e in self._events if e["event_id"] not in self._fired
            ]
        return pending if limit is None else pending[:limit]

    def mark_fired(self, event_id: int) -> bool:
        """Mark an event as fired. Returns True on first mark, False on idempotent re-mark."""
        with self._lock:
            if not any(e["event_id"] == event_id for e in self._events):
                raise KeyError(f"Unknown event id: {event_id}")
            if event_id in self._fired:
                return False
            self._fired.add(event_id)
            with open(self._fired_path, "a", encoding="utf-8") as f:
                f.write(f"{event_id}\n")
                f.flush()
            return True
