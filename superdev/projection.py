"""SQLite projection: five tables + ledger refresh + regression/decision records + event dispatch consumption.

Design source: design/03-detailed-design.md §4 — the projection is a read-only
copy of the ledger. There is exactly one write path: the program mutates
the ledger -> refreshes the projection -> appends events -> the
dispatcher consumes unfired events. fire_pending is the heart of the
event-driven dispatcher: it calls handlers in event_id order, marking
each event fired on success (at-least-once); events without a handler
stay pending for later.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from superdev import ledger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (            -- copy of the ledger plan.yml
  task_id TEXT PRIMARY KEY, project TEXT, module TEXT,
  status TEXT, engine TEXT, worker TEXT,
  red_count INTEGER DEFAULT 0, strategy_changes INTEGER DEFAULT 0,
  depends_on TEXT, mr_url TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS regression_events (-- written by discipline scripts
  task_id TEXT, attempt INTEGER, green INTEGER,
  commit_sha TEXT, log_tail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (        -- decisions table copy
  decision_id TEXT PRIMARY KEY, project TEXT, question TEXT,
  context_report TEXT,                        -- program-assembled "directions already tried"
  status TEXT,                                -- waiting/decided/escalated
  decided_by TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS events (           -- event bus (with fired markers)
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, payload TEXT, fired INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (          -- supervision metrics (definitions inherited from bigdev)
  project TEXT, ts TEXT, done_count INTEGER,
  worker_hours REAL, cost_by_model TEXT
);
"""

TABLES = ("tasks", "regression_events", "decisions", "events", "metrics")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    """Write a temp file in the same directory, then os.replace; on mid-flight failure the original file stays untouched."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Projection:
    """SQLite projection database. The engine-side fields of the tasks
    table (engine/worker/red_count etc.) are maintained by runtime
    actions; refresh_from_ledger only overwrites the fields the ledger
    itself carries.
    """

    def __init__(self, db_path: str | Path, status_path: str | Path | None = None):
        self.db_path = Path(db_path)
        # set_last_green writes status.yml; without a configured path we
        # refuse to write rather than write to the wrong place
        self.status_path = Path(status_path) if status_path is not None else None
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Projection":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- ledger refresh -------------------------------------------------

    def refresh_from_ledger(self, plan_path: str | Path) -> int:
        """Read plan.yml, upsert into the tasks table, return the number of tasks written this round.

        On conflict, only the ledger-owned fields are updated
        (project/status/depends_on/updated_at); runtime fields such as
        engine/worker/red_count keep their values.
        """
        plan = ledger.load_plan(plan_path)
        ts = _now()
        for t in plan.tasks:
            self.conn.execute(
                """
                INSERT INTO tasks (task_id, project, module, status, engine, worker,
                                   red_count, strategy_changes, depends_on, mr_url,
                                   updated_at)
                VALUES (?, ?, NULL, ?, NULL, NULL, 0, 0, ?, NULL, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    project = excluded.project,
                    status = excluded.status,
                    depends_on = excluded.depends_on,
                    updated_at = excluded.updated_at
                """,
                (
                    t.id,
                    plan.project,
                    t.status,
                    json.dumps(t.depends_on, ensure_ascii=False),
                    ts,
                ),
            )
        self.conn.commit()
        return len(plan.tasks)

    def get_task(self, task_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- regression and green points ------------------------------------------------

    def record_regression(
        self,
        task_id: str,
        attempt: int,
        green: bool,
        commit_sha: str,
        log_tail: str,
    ) -> int:
        """Record one regression result; a red round also increments tasks.red_count.

        Returns the inserted regression_events row id. A task missing from
        the tasks table is still recorded (the regression script may run
        before the ledger refresh); only the counter is skipped.
        """
        cur = self.conn.execute(
            """
            INSERT INTO regression_events
                (task_id, attempt, green, commit_sha, log_tail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, int(attempt), int(bool(green)), commit_sha, log_tail, _now()),
        )
        if not green:
            self.conn.execute(
                "UPDATE tasks SET red_count = red_count + 1, updated_at = ?"
                " WHERE task_id = ?",
                (_now(), task_id),
            )
        self.conn.commit()
        return cur.lastrowid

    def set_last_green(self, module: str, sha: str) -> dict:
        """Write the module's latest green point into status.yml and return the full status dict written back.

        module matches modules[].repo in status.yml; a missing file or
        entry is created. Raises ValueError when status_path was not
        configured. Prefers reusing ledger.save_status (that function
        does not exist in ledger yet, so fall back to a local atomic write).
        """
        if self.status_path is None:
            raise ValueError("status.yml path not configured (Projection(status_path=...)); cannot write the green point")
        path = self.status_path
        if path.exists():
            data = ledger.load_status(path)
        else:
            data = {"project": None, "modules": [], "metrics": []}
        for m in data["modules"]:
            if m.get("repo") == module:
                m["last_green"] = sha
                break
        else:
            data["modules"].append({"repo": module, "last_green": sha})
        save_status = getattr(ledger, "save_status", None)
        if callable(save_status):
            save_status(data, path)
        else:
            _atomic_write(
                path,
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            )
        return data

    # ---- decision loop ---------------------------------------------------

    def add_decision(
        self, decision_id: str, project: str, question: str, context_report: str = ""
    ) -> None:
        """Register one pending decision (status='waiting')."""
        self.conn.execute(
            """
            INSERT INTO decisions
                (decision_id, project, question, context_report, status)
            VALUES (?, ?, ?, ?, 'waiting')
            """,
            (decision_id, project, question, context_report),
        )
        self.conn.commit()

    def pending_decisions(self) -> list[dict]:
        """All waiting decisions, ascending by decision_id."""
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE status = 'waiting' ORDER BY decision_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def decide(self, decision_id: str, choice: str, note: str = "") -> dict:
        """Flip a waiting decision to decided and return the action for the dispatcher to fire.

        Returns {"decision_id", "action", "note"}: action is the chosen
        option itself; the dispatcher decides the next step from it (e.g.
        "escalate"). choice is recorded in decided_by; note is not stored,
        only carried in the return value. Raises when the decision is
        missing or not waiting.
        """
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Decision '{decision_id}' not found")
        if row["status"] != "waiting":
            raise ValueError(
                f"Decision '{decision_id}' is already {row['status']}; cannot decide twice"
            )
        self.conn.execute(
            """
            UPDATE decisions
            SET status = 'decided', decided_by = ?, decided_at = ?
            WHERE decision_id = ?
            """,
            (choice, _now(), decision_id),
        )
        self.conn.commit()
        return {"decision_id": decision_id, "action": choice, "note": note}

    # ---- event-driven dispatch ------------------------------------------------

    def fire_pending(
        self, event_log: Any, handlers: dict[str, Callable[[dict], Any]]
    ) -> list[int]:
        """Consume event_log.unfired(): call the handler per kind, then mark_fired on success.

        Events fire in strict ascending event_id order; an event is marked
        only after its handler succeeds — a failure (raised exception)
        propagates immediately and the event stays unfired for the next
        retry (at-least-once). Events without a matching handler are
        skipped and left unmarked until the dispatcher supplies one.
        Returns the ids of the events actually fired this round.
        """
        fired: list[int] = []
        for e in event_log.unfired():
            handler = handlers.get(e["kind"])
            if handler is None:
                continue
            handler(e)
            event_log.mark_fired(e["event_id"])
            fired.append(e["event_id"])
        return fired
