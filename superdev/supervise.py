"""Supervision (supervise): metric comparison, stall detection, the five-element escalation report, and automatic recovery.

Design source: design/03-detailed-design.md §3.3/§6 — metric comparison stays at once
every 30 minutes; the escalation report carries five elements (failed case, repro
command, directions tried, cost, options); recovery prefers metric rebound
(auto_resume), with expert sign-off driven by events.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from superdev.events import METRICS_STALL

REPRO_FALLBACK = "TBD: no repro microtask is linked to this task"
OPTIONS = ("Change strategy", "Split into microtasks", "Switch engine", "Abandon")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metrics_desc(projection: Any, project: str) -> list:
    """All metrics of the project, newest->oldest (ts primary; rowid breaks ties for same-second writes)."""
    return projection.conn.execute(
        "SELECT ts, done_count FROM metrics WHERE project = ?"
        " ORDER BY ts DESC, rowid DESC",
        (project,),
    ).fetchall()


def record_metric(
    projection: Any,
    project: str,
    done_count: int,
    worker_hours: float = 0.0,
    cost_by_model: str = "{}",
) -> None:
    """Write one supervision metric row (with a UTC timestamp)."""
    projection.conn.execute(
        "INSERT INTO metrics (project, ts, done_count, worker_hours, cost_by_model)"
        " VALUES (?, ?, ?, ?, ?)",
        (project, _now(), int(done_count), float(worker_hours), cost_by_model),
    )
    projection.conn.commit()


def stalled(projection: Any, project: str, window_minutes: int = 30) -> bool:
    """No done_count growth inside the window means stalled.

    Uses the newest metric as the baseline and looks back window_minutes: a
    smaller done_count inside the window means it grew, so it is not stalled;
    with no comparable point inside the window (fewer than two metrics, or the
    previous one already outside the window) no verdict is possible, which also
    returns False.
    """
    rows = _metrics_desc(projection, project)
    if len(rows) < 2:
        return False
    latest_ts = datetime.fromisoformat(rows[0]["ts"])
    latest_count = rows[0]["done_count"]
    cutoff = latest_ts - timedelta(minutes=window_minutes)
    saw_point = False
    for row in rows[1:]:  # iterate newest->oldest; stop at the first row outside the window
        if datetime.fromisoformat(row["ts"]) < cutoff:
            break
        saw_point = True
        if row["done_count"] < latest_count:
            return False
    return saw_point


def _recent_red(projection: Any, project: str, task_id: str | None = None) -> Any:
    """The project's (optionally task-scoped) latest red row, or None."""
    sql = (
        "SELECT r.task_id, r.log_tail FROM regression_events r"
        " JOIN tasks t ON r.task_id = t.task_id"
        " WHERE t.project = ? AND r.green = 0"
    )
    params: list[Any] = [project]
    if task_id is not None:
        sql += " AND r.task_id = ?"
        params.append(task_id)
    sql += " ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1"
    return projection.conn.execute(sql, params).fetchone()


def _failed_case_section(projection: Any, project: str) -> str:
    row = _recent_red(projection, project)
    if row is None:
        return "(no failed cases yet)"
    head = [ln for ln in row["log_tail"].splitlines() if ln.strip()][:5]
    if not head:
        return f"[{row['task_id']}] latest red: (empty log, no content in the first 5 lines)"
    lines = [f"[{row['task_id']}] latest red (first 5 log lines):"]
    lines += [f"  {ln}" for ln in head]
    return "\n".join(lines)


def _tried_directions_section(projection: Any, project: str) -> str:
    rows = projection.conn.execute(
        "SELECT r.task_id, COUNT(*) AS red_count"
        " FROM regression_events r JOIN tasks t ON r.task_id = t.task_id"
        " WHERE t.project = ? AND r.green = 0"
        " GROUP BY r.task_id ORDER BY red_count DESC, r.task_id",
        (project,),
    ).fetchall()
    if not rows:
        return "(no directions tried yet)"
    out = []
    for row in rows:
        latest = _recent_red(projection, project, row["task_id"])
        tail = latest["log_tail"].splitlines()[-3:] if latest is not None else []
        tail_text = " | ".join(tail) if tail else "(empty log)"
        out.append(
            f"- {row['task_id']}: red {row['red_count']} time(s), last 3 log lines: {tail_text}"
        )
    return "\n".join(out)


def _cost_section(projection: Any, project: str) -> str:
    rows = projection.conn.execute(
        "SELECT worker_hours, cost_by_model FROM metrics WHERE project = ?",
        (project,),
    ).fetchall()
    if not rows:
        return "(no metrics yet)"
    hours = sum(r["worker_hours"] for r in rows)
    by_model: dict[str, float] = {}
    for r in rows:
        try:
            data = json.loads(r["cost_by_model"] or "{}")
        except ValueError:
            data = {}
        for model, cost in data.items():
            by_model[model] = by_model.get(model, 0.0) + float(cost)
    parts = [f"{m}={v:.2f}" for m, v in sorted(by_model.items())]
    text = f"worker_hours total {hours:.2f}"
    if parts:
        text += "; by model: " + ", ".join(parts)
    return text


def _repro_command(tasks: list | None) -> str:
    """Find the repro microtask linked to a stuck task in the task list and assemble `python <script>`.

    Link rule (design §1.1 item 5): the stuck task is itself a repro:-prefixed
    microtask, or its depends_on references a repro: task. If found, take the
    repro task's files[0] to build the command; with no tasks passed, or nothing
    linked, return the fallback wording (distinct from the old placeholder).
    """
    if not tasks:
        return REPRO_FALLBACK
    by_id = {str(t.id): t for t in tasks if getattr(t, "id", None)}
    for task in tasks:
        if getattr(task, "status", None) != "stuck":
            continue
        repro = task if str(task.id).startswith("repro:") else None
        if repro is None:
            for dep in getattr(task, "depends_on", None) or []:
                if str(dep).startswith("repro:") and str(dep) in by_id:
                    repro = by_id[str(dep)]
                    break
        files = getattr(repro, "files", None) or []
        if repro is not None and files:
            return f"python {files[0]}"
    return REPRO_FALLBACK


def escalation_report(
    projection: Any, event_log: Any, project: str, tasks: list | None = None
) -> str:
    """Assemble the five-element escalation report (design §6): failed case, repro command,
    directions tried, cost, options. Shown as-is on the web decision panel's
    "awaiting your call".

    When tasks (the dispatch plan's task objects) is passed, the repro command is
    taken from the repro: microtask linked to a stuck task; without it the
    fallback wording stands in (backward compatible with old callers).
    """
    stalls = sum(
        1
        for e in event_log.events()
        if e["kind"] == METRICS_STALL and e["payload"].get("project") == project
    )
    sections = [
        f"Escalation report: project {project} (metrics stalled {stalls} time(s))",
        "1. Failed case\n" + _failed_case_section(projection, project),
        "2. Repro\n" + _repro_command(tasks),
        "3. Directions tried\n" + _tried_directions_section(projection, project),
        "4. Cost\n" + _cost_section(projection, project),
        "5. Options\n" + " / ".join(OPTIONS),
    ]
    return "\n".join(sections)


def maybe_escalate(
    projection: Any, event_log: Any, project: str, threshold: int = 2
) -> str | None:
    """Stuck-task count under the project reaching the threshold (escalate on two failures, §3.2) ->
    append a METRICS_STALL event and return the escalation report; below the
    threshold return None and leave the log untouched."""
    stuck = projection.conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE project = ? AND status = 'stuck'",
        (project,),
    ).fetchone()["c"]
    if stuck < threshold:
        return None
    event_log.append(METRICS_STALL, {"project": project, "stuck_tasks": stuck})
    return escalation_report(projection, event_log, project)


def auto_resume(projection: Any, project: str) -> bool:
    """The newest metric showing growth over the previous one -> True (auto-resume on metric rebound, §6).

    Fewer than two metrics, flat, or a drop all return False.
    """
    rows = _metrics_desc(projection, project)
    if len(rows) < 2:
        return False
    return rows[0]["done_count"] > rows[1]["done_count"]
