"""Ledger I/O: loading, validating, and atomically saving plan.yml and status.yml.

The ledger is the single source of truth for the whole system (terminology
in design/README.md). This module only handles the file <-> dataclass
round trip; advanced rules such as dependency acyclicity are check.py's
job, not ours.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Full task status set from design doc §3.1; used as fallback when the file header omits statuses
DEFAULT_STATUSES = [
    "draft", "ready", "assigned", "in_progress", "evidence_wait",
    "stuck", "escalated", "gating", "merged", "done",
]

# Required task fields; files/size/research/depends_on may be omitted
_TASK_REQUIRED = ("id", "title", "status")
_LIST_FIELDS = ("files", "research", "depends_on")


class LedgerError(Exception):
    """Ledger file parsing or validation failed. Messages face the management session and spell out the location and cause."""


@dataclass
class Task:
    """One task entry in the dispatch plan."""

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
            raise LedgerError(f"Task #{index} is not a mapping: {raw!r}")
        missing = [k for k in _TASK_REQUIRED if not raw.get(k)]
        if missing:
            where = f"Task {raw.get('id', f'#{index}')}"
            raise LedgerError(f"{where} is missing required fields: {', '.join(missing)}")
        for k in _LIST_FIELDS:
            if k in raw and not isinstance(raw[k], list):
                raise LedgerError(f"Task {raw['id']}: field {k} should be a list, got {type(raw[k]).__name__}")
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
    """A dispatch plan (in-memory form of agents/<project>/plan.yml)."""

    project: str
    kind: str = ""
    version: str = ""
    statuses: list[str] = field(default_factory=lambda: list(DEFAULT_STATUSES))
    modules: list[dict] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    # Records which fields were ignored when reading a legacy bigdev ledger
    # (always empty on the load_plan happy path; never written by to_dict,
    # so native ledger round trips never see this field)
    compat_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "Plan":
        if not raw.get("project"):
            raise LedgerError("Plan is missing required field 'project'")
        statuses = list(DEFAULT_STATUSES)
        if "statuses" in raw:
            if not isinstance(raw["statuses"], list) or not raw["statuses"]:
                raise LedgerError("Plan header 'statuses' must be a non-empty list (the legal status enumeration)")
            statuses = [str(s) for s in raw["statuses"]]
        modules = raw.get("modules") or []
        if not isinstance(modules, list) or any(not isinstance(m, dict) for m in modules):
            raise LedgerError("Plan 'modules' should be a list of module mappings")
        if "tasks" not in raw or not isinstance(raw["tasks"], list):
            raise LedgerError("Plan is missing the task list 'tasks' (write 'tasks: []' even when there are no tasks)")
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
    """Read YAML and require a mapping at the top level; failures raise LedgerError uniformly."""
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise LedgerError(f"{what} not found: {path}") from None
    except yaml.YAMLError as e:
        raise LedgerError(f"{what} has a YAML syntax error: {path}\n{e}") from e
    if not isinstance(data, dict):
        raise LedgerError(f"{what} top level should be a mapping, parsed into {type(data).__name__}: {path}")
    return data


def load_plan(path: Path | str) -> Plan:
    """Parse plan.yml into a Plan. Missing fields or syntax errors raise LedgerError."""
    return Plan.from_dict(_read_yaml_mapping(path, "Plan file"))


def load_status(path: Path | str) -> dict:
    """Parse status.yml (per-module green points + supervision metrics).

    Returns {"project": str|None, "modules": [{repo, last_green, ...}], "metrics": [...]}.
    last_green is the sha of the module's most recent all-green commit,
    or None when no green point exists yet.
    """
    path = Path(path)
    data = _read_yaml_mapping(path, "Status file")
    raw_modules = data.get("modules") or []
    if not isinstance(raw_modules, list):
        raise LedgerError(f"Status file 'modules' should be a list: {path}")
    modules = []
    for i, m in enumerate(raw_modules, start=1):
        if not isinstance(m, dict) or not m.get("repo"):
            raise LedgerError(f"Status file module #{i} is missing the 'repo' field: {path}")
        entry = dict(m)
        entry.setdefault("last_green", None)
        modules.append(entry)
    metrics = data.get("metrics") or []
    if not isinstance(metrics, list) or any(not isinstance(x, dict) for x in metrics):
        raise LedgerError(f"Status file 'metrics' should be a list of metric mappings: {path}")
    return {"project": data.get("project"), "modules": modules, "metrics": metrics}


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


def save_plan(plan: Plan, path: Path | str) -> None:
    """Atomically write a Plan back to plan.yml (temp file + replace)."""
    path = Path(path)
    text = yaml.safe_dump(
        plan.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    _atomic_write(path, text)


def update_task_status(plan_path: Path | str, task_id: str, new_status: str) -> Plan:
    """Change one task's status and atomically write it back. Illegal status or unknown task raises LedgerError and leaves the file untouched."""
    plan = load_plan(plan_path)
    if new_status not in plan.statuses:
        raise LedgerError(
            f"Illegal status '{new_status}', legal values: {', '.join(plan.statuses)}"
        )
    for task in plan.tasks:
        if task.id == task_id:
            task.status = new_status
            save_plan(plan, plan_path)
            return plan
    raise LedgerError(f"Task '{task_id}' not found: {plan_path}")


def validate_basic(plan: Plan) -> None:
    """Basic validation: unique ids, depends_on references exist, statuses within the legal enumeration.

    The enumeration comes from the file header 'statuses' (falling back to
    DEFAULT_STATUSES). Returns None when clean; raises LedgerError listing
    every problem at once otherwise.
    """
    problems: list[str] = []
    if not plan.statuses:
        problems.append("Status enumeration is empty; cannot validate task statuses")
    seen: set[str] = set()
    for task in plan.tasks:
        if task.id in seen:
            problems.append(f"Duplicate task id: {task.id}")
        seen.add(task.id)
    ids = {t.id for t in plan.tasks}
    for task in plan.tasks:
        for dep in task.depends_on:
            if dep not in ids:
                problems.append(f"Task {task.id} depends_on references a nonexistent task: {dep}")
        if plan.statuses and task.status not in plan.statuses:
            problems.append(
                f"Task {task.id} status '{task.status}' is not in the legal enumeration ({len(plan.statuses)} legal values)"
            )
    if problems:
        raise LedgerError(
            f"Ledger basic validation failed with "
            f"{len(problems)} problem(s):\n" + "\n".join(f"- {p}" for p in problems)
        )


# ---------- legacy bigdev ledger compat reading (detailed design §7 migration compat) ----------

# old bigdev status enum -> closest superdev status; anything not in this
# table lands on draft with a compat note. (Chinese keys are the legacy
# system's literal status values and must stay verbatim.)
_BIGDEV_STATUS_MAP = {
    "done": "done",
    "进行中": "in_progress",
    "待办": "ready",
}
_BIGDEV_DEFAULT_SIZE = "medium"
# bigdev top-level keys that superdev has counterparts for (or passes
# through); all other keys are ignored wholesale and recorded
_BIGDEV_TOP_KNOWN = {"version", "module", "kind", "statuses", "modules", "tasks"}
# bigdev task keys that superdev has counterparts for (objective/todo are
# used as title fallbacks); group/client/worker/lifecycle/evidence/
# attempts/failure_class etc. have no counterpart and are dropped after
# being recorded
_BIGDEV_TASK_KNOWN = {"id", "objective", "todo", "status", "depends_on", "files", "size", "research"}


def load_bigdev_plan(path: Path | str) -> Plan:
    """Read an old-format bigdev dispatch plan into a superdev Plan (read-only compat; the original file is never modified).

    The old format follows the real legacy ledger fields (module/workers/
    tasks: id, todo, objective, status...) with this mapping: missing
    files/research -> empty, missing size -> medium; old status enum maps
    done->done, 进行中->in_progress, 待办->ready, anything else (queued,
    missing, ...) -> draft. Unrecognized fields are ignored and recorded
    per category in Plan.compat_notes so the manager can audit what was
    dropped during migration.
    """
    raw = _read_yaml_mapping(path, "bigdev plan file")
    notes: list[str] = []

    project = str(raw.get("module") or "").strip()
    if not project:
        project = Path(path).stem
        notes.append(f"Missing 'module' field; project falls back to the file name: {project}")

    raw_tasks = raw.get("tasks")
    if not isinstance(raw_tasks, list):
        raise LedgerError(f"bigdev plan is missing the task list 'tasks': {path}")

    tasks: list[Task] = []
    ignored_task_fields: set[str] = set()
    defaulted_tasks = 0
    unmapped_statuses: list[str] = []
    for i, t in enumerate(raw_tasks, start=1):
        if not isinstance(t, dict) or not t.get("id"):
            raise LedgerError(f"bigdev plan task #{i} is missing required field 'id': {path}")
        raw_status = str(t.get("status") or "").strip()
        status = _BIGDEV_STATUS_MAP.get(raw_status)
        if status is None:
            status = "draft"
            unmapped_statuses.append(f"{t['id']}:'{raw_status or 'missing'}'")
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
        notes.append(f"Ignored top-level fields: {', '.join(ignored_top)}")
    if ignored_task_fields:
        notes.append(f"Ignored task fields: {', '.join(sorted(ignored_task_fields))}")
    if defaulted_tasks:
        notes.append(
            f"{defaulted_tasks} task(s) missing files/size/research; filled with superdev defaults (size=medium)"
        )
    if unmapped_statuses:
        notes.append(f"Task statuses with no direct mapping fell back to draft: {', '.join(unmapped_statuses)}")

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
    """Get the project directory's current git HEAD sha; returns None when the directory is not a git repo or git is unavailable."""
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
    """First-run initialization (detailed design §7): when the project directory has no status.yml yet, write the initial green point.

    last_green takes the project directory's current git HEAD, or
    "unverified" when that cannot be obtained. An existing status.yml is
    never touched (the second run does not overwrite); returns the first
    module's last_green from the file.
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
