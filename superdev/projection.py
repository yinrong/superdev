"""SQLite 投影：五张表 + 台账刷新 + 回归/决策记录 + 事件派发消费。

设计出处：design/03-详细设计.md §4——投影只是台账的只读副本，
写路径只有一条：程序改台账 → 刷新投影 → 插入事件 → 派发器消费未触发的事件。
fire_pending 是事件驱动派发器的核心：按 event_id 顺序调 handler，
成功一条标记一条（at-least-once），没有 handler 的事件留待后续。
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
CREATE TABLE IF NOT EXISTS tasks (            -- 台账 派工计划.yml 的副本
  task_id TEXT PRIMARY KEY, project TEXT, module TEXT,
  status TEXT, engine TEXT, worker TEXT,
  red_count INTEGER DEFAULT 0, strategy_changes INTEGER DEFAULT 0,
  depends_on TEXT, mr_url TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS regression_events (-- 纪律脚本写入
  task_id TEXT, attempt INTEGER, green INTEGER,
  commit_sha TEXT, log_tail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (        -- 决策表副本
  decision_id TEXT PRIMARY KEY, project TEXT, question TEXT,
  context_report TEXT,                        -- 程序拼的"试过哪些方向"
  status TEXT,                                -- waiting/decided/escalated
  decided_by TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS events (           -- 事件总线（带已触发标记）
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, payload TEXT, fired INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (          -- 督办指标（口径继承 bigdev）
  project TEXT, ts TEXT, done_count INTEGER,
  worker_hours REAL, cost_by_model TEXT
);
"""

TABLES = ("tasks", "regression_events", "decisions", "events", "metrics")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    """先写同目录临时文件再 os.replace，中途失败保持原文件不动。"""
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
    """SQLite 投影库。tasks 表的引擎侧字段（engine/worker/red_count 等）
    由运行期动作维护，refresh_from_ledger 只覆盖台账自带的字段。"""

    def __init__(self, db_path: str | Path, status_path: str | Path | None = None):
        self.db_path = Path(db_path)
        # set_last_green 要写 status.yml；不配路径就拒绝写，避免写歪位置
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

    # ---- 台账刷新 -------------------------------------------------

    def refresh_from_ledger(self, plan_path: str | Path) -> int:
        """读派工计划.yml，upsert 进 tasks 表，返回本次写入的任务数。

        冲突时只更新台账拥有的字段（project/status/depends_on/updated_at），
        engine/worker/red_count 等运行期字段保持原值。
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

    # ---- 回归与绿点 ------------------------------------------------

    def record_regression(
        self,
        task_id: str,
        attempt: int,
        green: bool,
        commit_sha: str,
        log_tail: str,
    ) -> int:
        """记一条回归结果；红的一轮同时把 tasks.red_count 加一。

        返回插入的 regression_events 行 id。任务不在 tasks 表也照记
        （回归脚本可能先于台账刷新跑），只是不更新计数。
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
        """把模块最近绿点写进 status.yml，返回写回的完整状态字典。

        module 按 status.yml 里 modules[].repo 匹配；文件或条目不存在则补建。
        status_path 未配置时抛 ValueError。优先复用 ledger.save_status
        （当前 ledger 还没有这个函数，走本地原子写）。
        """
        if self.status_path is None:
            raise ValueError("未配置 status.yml 路径（Projection(status_path=...)），无法写绿点")
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

    # ---- 决策闭环 ---------------------------------------------------

    def add_decision(
        self, decision_id: str, project: str, question: str, context_report: str = ""
    ) -> None:
        """登记一条待拍板决策（status='waiting'）。"""
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
        """所有 waiting 状态的决策，按 decision_id 升序。"""
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE status = 'waiting' ORDER BY decision_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def decide(self, decision_id: str, choice: str, note: str = "") -> dict:
        """把 waiting 决策翻成 decided，返回派发器待触发的动作。

        返回 {"decision_id", "action", "note"}：action 就是拍板选项本身，
        派发器拿它决定下一步（比如"升级"）。choice 记入 decided_by，
        note 不落库、只随返回值带走。找不到或不在 waiting 抛错。
        """
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"找不到决策 '{decision_id}'")
        if row["status"] != "waiting":
            raise ValueError(
                f"决策 '{decision_id}' 已是 {row['status']}，不能重复拍板"
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

    # ---- 事件驱动派发 ------------------------------------------------

    def fire_pending(
        self, event_log: Any, handlers: dict[str, Callable[[dict], Any]]
    ) -> list[int]:
        """消费 event_log.unfired()：按 kind 调 handler，成功后 mark_fired。

        事件严格按 event_id 升序触发；handler 成功才标记，失败（抛异常）
        立即上抛且该事件保持未触发，下次重试（at-least-once）。
        没有对应 handler 的事件跳过、不标记，等派发器补齐 handler 再消费。
        返回本次实际触发的事件 id 列表。
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
