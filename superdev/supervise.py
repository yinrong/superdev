"""督办（supervise）：指标比对、停滞判定、五要素升级报告、自动恢复。

设计出处：design/03-详细设计.md §3.3/§6——指标比对保留 30 分钟一次；
升级报告五要素（失败用例、复现命令、已试方向、已耗成本、建议选项）；
恢复优先走指标回升（auto_resume），专家拍板走事件触发。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from superdev.events import METRICS_STALL

REPRO_FALLBACK = "待补：该任务无关联复现微任务"
OPTIONS = ("换策略", "拆微任务", "换引擎", "放弃")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metrics_desc(projection: Any, project: str) -> list:
    """该项目全部指标，新→旧（ts 为主序，rowid 兜底同秒写入）。"""
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
    """写一条督办指标（带 UTC 时间戳）。"""
    projection.conn.execute(
        "INSERT INTO metrics (project, ts, done_count, worker_hours, cost_by_model)"
        " VALUES (?, ?, ?, ?, ?)",
        (project, _now(), int(done_count), float(worker_hours), cost_by_model),
    )
    projection.conn.commit()


def stalled(projection: Any, project: str, window_minutes: int = 30) -> bool:
    """窗口内 done_count 无增长即停滞。

    以最新一条指标为基准往前看 window_minutes：窗口内存在更小的
    done_count 说明涨过，不算停滞；窗口内没有可比点（指标不足两条，
    或前一条已出窗）无法判定，同样返回 False。
    """
    rows = _metrics_desc(projection, project)
    if len(rows) < 2:
        return False
    latest_ts = datetime.fromisoformat(rows[0]["ts"])
    latest_count = rows[0]["done_count"]
    cutoff = latest_ts - timedelta(minutes=window_minutes)
    saw_point = False
    for row in rows[1:]:  # 新→旧遍历，第一条出窗即可停
        if datetime.fromisoformat(row["ts"]) < cutoff:
            break
        saw_point = True
        if row["done_count"] < latest_count:
            return False
    return saw_point


def _recent_red(projection: Any, project: str, task_id: str | None = None) -> Any:
    """该项目（可限定任务）最近一条红，没有返回 None。"""
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
        return "（暂无失败记录）"
    head = [ln for ln in row["log_tail"].splitlines() if ln.strip()][:5]
    if not head:
        return f"[{row['task_id']}] 最近一次红：（空日志，前 5 行无内容）"
    lines = [f"[{row['task_id']}] 最近一次红（日志前 5 行）："]
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
        return "（暂无已试方向）"
    out = []
    for row in rows:
        latest = _recent_red(projection, project, row["task_id"])
        tail = latest["log_tail"].splitlines()[-3:] if latest is not None else []
        tail_text = " | ".join(tail) if tail else "（空日志）"
        out.append(
            f"- {row['task_id']}：red {row['red_count']} 次，日志尾 3 行：{tail_text}"
        )
    return "\n".join(out)


def _cost_section(projection: Any, project: str) -> str:
    rows = projection.conn.execute(
        "SELECT worker_hours, cost_by_model FROM metrics WHERE project = ?",
        (project,),
    ).fetchall()
    if not rows:
        return "（暂无指标）"
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
    text = f"worker_hours 合计 {hours:.2f}"
    if parts:
        text += "；按模型：" + "，".join(parts)
    return text


def _repro_command(tasks: list | None) -> str:
    """从任务列表里找 stuck 任务关联的复现微任务，拼 `python <脚本>`。

    关联规则（design §1.1 第 5 条）：stuck 任务自身就是 repro: 前缀的微任务，
    或它的 depends_on 里引用了 repro: 任务。找到取 repro 任务 files[0] 拼命令；
    没传 tasks、或找不到关联，返回降级文案（区别于旧的"待填"占位）。
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
    """拼五要素升级报告（design §6）：失败用例、复现命令、已试方向、
    已耗成本、建议选项。给网页决策面"等你拍板"直接展示。

    tasks 传派工计划的任务对象列表时，复现命令从 stuck 任务关联的
    repro: 微任务实取；不传则降级文案占位（旧调用兼容）。
    """
    stalls = sum(
        1
        for e in event_log.events()
        if e["kind"] == METRICS_STALL and e["payload"].get("project") == project
    )
    sections = [
        f"【升级报告】项目 {project}（指标停滞 {stalls} 次）",
        "一、失败用例\n" + _failed_case_section(projection, project),
        "二、复现命令\n" + _repro_command(tasks),
        "三、已试方向\n" + _tried_directions_section(projection, project),
        "四、已耗成本\n" + _cost_section(projection, project),
        "五、建议选项\n" + " / ".join(OPTIONS),
    ]
    return "\n".join(sections)


def maybe_escalate(
    projection: Any, event_log: Any, project: str, threshold: int = 2
) -> str | None:
    """项目名下 stuck 任务数达阈值（两败即升，§3.2）→ 记一条
    METRICS_STALL 事件并返回升级报告；未达阈值返回 None，不动日志。"""
    stuck = projection.conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE project = ? AND status = 'stuck'",
        (project,),
    ).fetchone()["c"]
    if stuck < threshold:
        return None
    event_log.append(METRICS_STALL, {"project": project, "stuck_tasks": stuck})
    return escalation_report(projection, event_log, project)


def auto_resume(projection: Any, project: str) -> bool:
    """最新一条指标比上一条有增长 → True（指标回升自动恢复，§6）。

    指标不足两条、持平或回落都返回 False。
    """
    rows = _metrics_desc(projection, project)
    if len(rows) < 2:
        return False
    return rows[0]["done_count"] > rows[1]["done_count"]
