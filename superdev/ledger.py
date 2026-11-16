"""台账读写：派工计划.yml 与 status.yml 的加载、校验、原子保存。

台账是全系统唯一事实源（术语见 design/README.md），本模块只负责
文件 <-> 数据类的往返；不校验依赖无环等高级规则（那是 check.py 的职责）。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 设计文档 §3.1 的任务状态全集；文件头 statuses 缺省时用它兜底
DEFAULT_STATUSES = [
    "draft", "ready", "assigned", "in_progress", "evidence_wait",
    "stuck", "escalated", "gating", "merged", "done",
]

# 任务必填字段；files/size/research/depends_on 允许缺省
_TASK_REQUIRED = ("id", "title", "status")
_LIST_FIELDS = ("files", "research", "depends_on")


class LedgerError(Exception):
    """台账文件解析或校验失败。消息面向管理端会话，用中文写明位置和原因。"""


@dataclass
class Task:
    """派工计划里的一个任务条目。"""

    id: str
    title: str
    status: str
    files: list[str] = field(default_factory=list)
    size: str | None = None
    research: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "Task":
        if not isinstance(raw, dict):
            raise LedgerError(f"第 {index} 个任务不是键值结构：{raw!r}")
        missing = [k for k in _TASK_REQUIRED if not raw.get(k)]
        if missing:
            where = f"任务 {raw.get('id', f'第 {index} 个')}"
            raise LedgerError(f"{where} 缺少必填字段：{'、'.join(missing)}")
        for k in _LIST_FIELDS:
            if k in raw and not isinstance(raw[k], list):
                raise LedgerError(f"任务 {raw['id']} 的 {k} 应为列表，现在是 {type(raw[k]).__name__}")
        return cls(
            id=str(raw["id"]),
            title=str(raw["title"]),
            status=str(raw["status"]),
            files=list(raw.get("files") or []),
            size=raw.get("size"),
            research=list(raw.get("research") or []),
            depends_on=list(raw.get("depends_on") or []),
        )

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "title": self.title,
            "status": self.status,
        }
        if self.size is not None:
            d["size"] = self.size
        d["files"] = list(self.files)
        d["research"] = list(self.research)
        d["depends_on"] = list(self.depends_on)
        return d


@dataclass
class Plan:
    """一份派工计划（agents/<项目>/派工计划.yml 的内存形态）。"""

    project: str
    kind: str = ""
    version: str = ""
    statuses: list[str] = field(default_factory=lambda: list(DEFAULT_STATUSES))
    modules: list[dict] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    # bigdev 存量台账兼容读入时记录忽略了哪些字段（load_plan 正常路径恒为空，
    # 不参与 to_dict 落盘，所以原生台账的往返不吃这个字段）
    compat_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "Plan":
        if not raw.get("project"):
            raise LedgerError("派工计划缺少必填字段 project")
        statuses = list(DEFAULT_STATUSES)
        if "statuses" in raw:
            if not isinstance(raw["statuses"], list) or not raw["statuses"]:
                raise LedgerError("派工计划头部的 statuses 必须是非空列表（合法状态枚举）")
            statuses = [str(s) for s in raw["statuses"]]
        modules = raw.get("modules") or []
        if not isinstance(modules, list) or any(not isinstance(m, dict) for m in modules):
            raise LedgerError("派工计划的 modules 应为模块字典列表")
        if "tasks" not in raw or not isinstance(raw["tasks"], list):
            raise LedgerError("派工计划缺少任务列表 tasks（没有任务也请写成 tasks: []）")
        tasks = [Task.from_dict(t, i + 1) for i, t in enumerate(raw["tasks"])]
        return cls(
            project=str(raw["project"]),
            kind=str(raw.get("kind") or ""),
            version=str(raw.get("version") or ""),
            statuses=statuses,
            modules=list(modules),
            tasks=tasks,
        )

    def to_dict(self) -> dict:
        d = {"project": self.project}
        if self.kind:
            d["kind"] = self.kind
        if self.version:
            d["version"] = self.version
        d["statuses"] = list(self.statuses)
        d["modules"] = list(self.modules)
        d["tasks"] = [t.to_dict() for t in self.tasks]
        return d


def _read_yaml_mapping(path: Path | str, what: str) -> dict:
    """读 YAML 并要求顶层是映射，失败统一抛 LedgerError。"""
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise LedgerError(f"{what}不存在：{path}") from None
    except yaml.YAMLError as e:
        raise LedgerError(f"{what}YAML 语法错误：{path}\n{e}") from e
    if not isinstance(data, dict):
        raise LedgerError(f"{what}顶层应为键值结构，现在解析结果是 {type(data).__name__}：{path}")
    return data


def load_plan(path: Path | str) -> Plan:
    """解析派工计划.yml，返回 Plan。字段缺失或语法错误抛 LedgerError。"""
    return Plan.from_dict(_read_yaml_mapping(path, "派工计划文件"))


def load_status(path: Path | str) -> dict:
    """解析 status.yml（每模块绿点 + 督办指标）。

    返回 {"project": str|None, "modules": [{repo, last_green, ...}], "metrics": [...]}。
    last_green 是该模块最近全绿提交的 sha，尚未有绿点时为 None。
    """
    path = Path(path)
    data = _read_yaml_mapping(path, "状态文件")
    raw_modules = data.get("modules") or []
    if not isinstance(raw_modules, list):
        raise LedgerError(f"状态文件的 modules 应为列表：{path}")
    modules = []
    for i, m in enumerate(raw_modules, start=1):
        if not isinstance(m, dict) or not m.get("repo"):
            raise LedgerError(f"状态文件第 {i} 个模块缺少 'repo' 字段：{path}")
        entry = dict(m)
        entry.setdefault("last_green", None)
        modules.append(entry)
    metrics = data.get("metrics") or []
    if not isinstance(metrics, list) or any(not isinstance(x, dict) for x in metrics):
        raise LedgerError(f"状态文件的 metrics 应为指标字典列表：{path}")
    return {"project": data.get("project"), "modules": modules, "metrics": metrics}


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


def save_plan(plan: Plan, path: Path | str) -> None:
    """把 Plan 原子写回派工计划.yml（临时文件 + 替换）。"""
    path = Path(path)
    text = yaml.safe_dump(
        plan.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    _atomic_write(path, text)


def update_task_status(plan_path: Path | str, task_id: str, new_status: str) -> Plan:
    """改单个任务状态并原子写回。非法状态或找不到任务时抛 LedgerError，文件不动。"""
    plan = load_plan(plan_path)
    if new_status not in plan.statuses:
        raise LedgerError(
            f"非法状态 '{new_status}'，合法枚举为：{'、'.join(plan.statuses)}"
        )
    for task in plan.tasks:
        if task.id == task_id:
            task.status = new_status
            save_plan(plan, plan_path)
            return plan
    raise LedgerError(f"找不到任务 '{task_id}'：{plan_path}")


def validate_basic(plan: Plan) -> None:
    """基础校验：id 唯一、depends_on 引用存在、status 在合法枚举内。

    枚举以文件头 statuses 为准（缺省回退 DEFAULT_STATUSES）。
    通过返回 None；有问题抛 LedgerError，一次性列出全部问题。
    """
    problems: list[str] = []
    if not plan.statuses:
        problems.append("状态枚举为空，无法校验任务状态")
    seen: set[str] = set()
    for task in plan.tasks:
        if task.id in seen:
            problems.append(f"任务 id 重复：{task.id}")
        seen.add(task.id)
    ids = {t.id for t in plan.tasks}
    for task in plan.tasks:
        for dep in task.depends_on:
            if dep not in ids:
                problems.append(f"任务 {task.id} 的 depends_on 引用了不存在的任务：{dep}")
        if plan.statuses and task.status not in plan.statuses:
            problems.append(
                f"任务 {task.id} 的状态 '{task.status}' 不在合法枚举内（{len(plan.statuses)} 个合法值）"
            )
    if problems:
        raise LedgerError(
            "台账基础校验失败，共 "
            f"{len(problems)} 处问题：\n" + "\n".join(f"- {p}" for p in problems)
        )


# ---------- bigdev 存量台账兼容读入（详细设计 §7 迁移兼容） ----------

# bigdev 老状态枚举 → superdev 最接近的状态；不在表内的落 draft 并记 compat note
_BIGDEV_STATUS_MAP = {
    "done": "done",
    "进行中": "in_progress",
    "待办": "ready",
}
_BIGDEV_DEFAULT_SIZE = "medium"
# bigdev 顶层里 superdev 有对应物（或直接透传）的键；其余整键忽略并记账
_BIGDEV_TOP_KNOWN = {"version", "module", "kind", "statuses", "modules", "tasks"}
# bigdev 任务里 superdev 有对应物的键（objective/todo 用作 title 兜底）；
# group/client/worker/lifecycle/evidence/attempts/failure_class 等均无对应，记账后丢弃
_BIGDEV_TASK_KNOWN = {"id", "objective", "todo", "status", "depends_on", "files", "size", "research"}


def load_bigdev_plan(path: Path | str) -> Plan:
    """把 bigdev 老格式派工计划读成 superdev Plan（只读兼容，不改原文件）。

    老格式按真实台账字段（module/workers/tasks：id、todo、objective、status…）
    映射：缺 files/research → 空、缺 size → medium；老状态枚举映射
    done→done、进行中→in_progress、待办→ready，其余（如 queued/blocked/缺省）→draft。
    认不出的字段忽略，逐类记进 Plan.compat_notes，迁移时管理端可查丢了什么。
    """
    raw = _read_yaml_mapping(path, "bigdev 派工计划文件")
    notes: list[str] = []

    project = str(raw.get("module") or "").strip()
    if not project:
        project = Path(path).stem
        notes.append(f"缺少 module 字段，project 用文件名兜底：{project}")

    raw_tasks = raw.get("tasks")
    if not isinstance(raw_tasks, list):
        raise LedgerError(f"bigdev 派工计划缺少任务列表 tasks：{path}")

    tasks: list[Task] = []
    ignored_task_fields: set[str] = set()
    defaulted_tasks = 0
    unmapped_statuses: list[str] = []
    for i, t in enumerate(raw_tasks, start=1):
        if not isinstance(t, dict) or not t.get("id"):
            raise LedgerError(f"bigdev 派工计划第 {i} 个任务缺少必填字段 id：{path}")
        raw_status = str(t.get("status") or "").strip()
        status = _BIGDEV_STATUS_MAP.get(raw_status)
        if status is None:
            status = "draft"
            unmapped_statuses.append(f"{t['id']}:'{raw_status or '缺省'}'")
        objective = str(t.get("objective") or "").strip()
        todo = str(t.get("todo") or "").strip()
        title = objective or (Path(todo).stem if todo else "") or str(t["id"])
        depends = t.get("depends_on")
        if not isinstance(depends, list):
            depends = []
        files = t.get("files") if isinstance(t.get("files"), list) else []
        research = t.get("research") if isinstance(t.get("research"), list) else []
        size = str(t.get("size") or "").strip() or _BIGDEV_DEFAULT_SIZE
        if not (isinstance(t.get("files"), list) and t.get("size") and isinstance(t.get("research"), list)):
            defaulted_tasks += 1
        tasks.append(
            Task(
                id=str(t["id"]),
                title=title,
                status=status,
                files=list(files or []),
                size=size,
                research=list(research or []),
                depends_on=[str(d) for d in depends],
            )
        )
        ignored_task_fields.update(str(k) for k in t if k not in _BIGDEV_TASK_KNOWN)

    ignored_top = sorted(str(k) for k in raw if k not in _BIGDEV_TOP_KNOWN)
    if ignored_top:
        notes.append(f"忽略顶层字段：{'、'.join(ignored_top)}")
    if ignored_task_fields:
        notes.append(f"忽略任务字段：{'、'.join(sorted(ignored_task_fields))}")
    if defaulted_tasks:
        notes.append(
            f"{defaulted_tasks} 个任务缺 files/size/research，按 superdev 缺省补齐（size=medium）"
        )
    if unmapped_statuses:
        notes.append(f"任务状态无直接对应，落为 draft：{'、'.join(unmapped_statuses)}")

    modules = [m for m in (raw.get("modules") or []) if isinstance(m, dict)]
    plan = Plan(
        project=project,
        kind=str(raw.get("kind") or ""),
        version=str(raw.get("version") or ""),
        statuses=list(DEFAULT_STATUSES),
        modules=modules,
        tasks=tasks,
    )
    plan.compat_notes = notes
    return plan


def _git_head(project_dir: Path) -> str | None:
    """取项目目录当前 git HEAD sha；目录不是 git 仓或 git 不可用时返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def init_last_green(project_dir: Path | str) -> str:
    """首跑初始化（详细设计 §7）：项目目录还没有 status.yml 时写入初始绿点。

    last_green 取项目目录当前 git HEAD，拿不到写 "unverified"；
    已有 status.yml 一律不动（二跑不覆盖），返回文件里第一个模块的 last_green。
    """
    project_dir = Path(project_dir)
    status_path = project_dir / "status.yml"
    if status_path.exists():
        data = load_status(status_path)
        modules = data.get("modules") or []
        first = modules[0].get("last_green") if modules else None
        return str(first) if first else "unverified"
    last_green = _git_head(project_dir) or "unverified"
    payload = {
        "project": project_dir.name,
        "modules": [{"repo": str(project_dir), "last_green": last_green}],
        "metrics": [],
    }
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    _atomic_write(status_path, text)
    return last_green
