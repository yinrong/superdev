"""Dispatch plan validator: the "eight checks" of design doc §2.

CLI: python -m superdev.check <plan.yml> [repo root]
check_plan returns a problem list (warnings included); an empty list means
the plan passes. The three basic checks (unique ids, depends_on references
exist, status enumeration) are delegated to ledger.validate_basic, split
into lines, deduped, and merged into the list; this module supplies the
eight rules beyond the ledger format itself.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from superdev import research
from superdev.ledger import LedgerError, Plan, Task, load_plan, validate_basic

__all__ = ["check_plan", "main"]

# Split window of §8.1: 1–50 tasks; 0 tasks simply cannot be dispatched
MIN_TASKS = 1
MAX_TASKS = 50

# §2 engine/worker ownership: workers may only touch these spots in the
# module repo; the manager writes only the ledger
_OWNED_PREFIXES = ("superdev/", "tests/", "commands/", "agents/")
_OWNED_FILES = ("README.md", "superdev_cli.py", "pytest.ini")  # repo-root entry points and config, added for acceptance

# Statuses whose dependencies are satisfied: downstream ready tasks can be dispatched in parallel at once
_DONE_STATUSES = frozenset({"done", "merged"})

# §1.1 item 5: a repro micro-task must carry a script that can be rerun with a single command
_REPRO_ID_PREFIX = "repro:"
_REPRO_SUFFIXES = (".sh", ".py", ".ps1")

# Sink function: the same message enters the list only once (dedupes
# validate_basic output against local findings)
_Add = Callable[[str], None]


def check_plan(plan: Plan, *, knowledge_dir: str | Path | None = None) -> list[str]:
    """Run every check on the dispatch plan and return the problem list; empty list = pass.

    knowledge_dir is the repo root; repro scripts resolve relative paths
    against it. Defaults to the current working directory.
    """
    errors: list[str] = []
    seen: set[str] = set()

    def add(msg: str) -> None:
        if msg not in seen:
            seen.add(msg)
            errors.append(msg)

    _check_task_count(plan, add)
    _check_basic_and_cycles(plan, add)
    _check_parallel_readiness(plan, add)
    _check_files_owned(plan, add)
    _check_strategy_cites(plan, add)
    root = Path(knowledge_dir) if knowledge_dir is not None else Path.cwd()
    _check_repro_scripts(plan, root, add)
    _check_budgets(plan, add)
    return errors


def _check_task_count(plan: Plan, add: _Add) -> None:
    n = len(plan.tasks)
    if n == 0:
        add(f"Task count is 0: a plan needs at least {MIN_TASKS} task(s); an empty plan cannot be dispatched")
    elif n > MAX_TASKS:
        add(f"Warning: task count {n} exceeds {MAX_TASKS}; split granularity is off, the manager should re-split (§8.1)")


def _check_basic_and_cycles(plan: Plan, add: _Add) -> None:
    """Basic checks delegate to ledger.validate_basic; cycle detection lives only in this module."""
    try:
        validate_basic(plan)
    except LedgerError as e:
        for line in str(e).splitlines():
            line = line.strip()
            if line.startswith("- "):
                add(line[2:])
    _check_cycles(plan.tasks, add)


def _check_cycles(tasks: list[Task], add: _Add) -> None:
    """Topological cycle detection within each task group; tasks without a group field are each their own group (group of one)."""
    groups: dict[tuple, list[Task]] = {}
    for task in tasks:
        group = getattr(task, "group", None)
        key = (1, str(group)) if group else (0, task.id)
        groups.setdefault(key, []).append(task)
    for members in groups.values():
        label = getattr(members[0], "group", None)
        by_id = {t.id: t for t in members}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(by_id, WHITE)
        stack: list[str] = []

        def visit(tid: str) -> None:
            color[tid] = GRAY
            stack.append(tid)
            for dep in by_id[tid].depends_on:
                if dep not in by_id:
                    continue  # cross-group dependency or dangling reference: cycles are searched within the group only
                if color[dep] == GRAY:
                    cycle = stack[stack.index(dep):] + [dep]
                    where = f" (group {label})" if label else ""
                    add(f"Dependency cycle{where}: {' -> '.join(cycle)}")
                elif color[dep] == WHITE:
                    visit(dep)
            stack.pop()
            color[tid] = BLACK

        for tid in by_id:
            if color[tid] == WHITE:
                visit(tid)


def _check_parallel_readiness(plan: Plan, add: _Add) -> None:
    limit = sum(int(m.get("max_workers") or 0) for m in plan.modules)
    if limit <= 0:
        return  # no concurrency limit in the ledger; nothing to compare against
    status_of = {t.id: t.status for t in plan.tasks}
    ready = [
        t.id
        for t in plan.tasks
        if t.status == "ready"
        and all(status_of.get(d) in _DONE_STATUSES for d in t.depends_on)
    ]
    if len(ready) > limit:
        add(
            f"Warning: {len(ready)} tasks ready in parallel ({', '.join(ready)}), "
            f"exceeding the sum of all modules' max_workers ({limit}); close some out or raise the limit before dispatching"
        )


def _check_files_owned(plan: Plan, add: _Add) -> None:
    for task in plan.tasks:
        for path in task.files:
            owned = str(path) in _OWNED_FILES or any(
                str(path).startswith(p) for p in _OWNED_PREFIXES
            )
            if not owned:
                add(
                    f"Task {task.id} file out of bounds: {path}"
                    " (only superdev/, tests/, commands/, agents/ and repo-root README.md, superdev_cli.py, pytest.ini are allowed)"
                )


def _check_strategy_cites(plan: Plan, add: _Add) -> None:
    # Task does not store these two fields; probe via attributes. Absent
    # from the plan means no strategy change.
    for task in plan.tasks:
        changes = getattr(task, "strategy_changes", None)
        if not changes:
            continue
        notes = getattr(task, "research_notes", None)
        if not notes:
            add(f"Task {task.id} has strategy changes but no research_notes; the dispatcher rejects it outright (§1.4)")
        elif not research.strategy_cites(str(changes), str(notes)):
            add(f"Task {task.id} strategy changes do not cite any research_notes keywords")


def _check_repro_scripts(plan: Plan, root: Path, add: _Add) -> None:
    for task in plan.tasks:
        if not task.id.startswith(_REPRO_ID_PREFIX):
            continue
        scripts = [
            str(f) for f in task.files if str(f).lower().endswith(_REPRO_SUFFIXES)
        ]
        if not scripts:
            add(f"Task {task.id} is a repro task; its files must include a .sh/.py/.ps1 repro script")
        elif not any((root / f).is_file() for f in scripts):
            add(f"Task {task.id}'s repro script does not exist under the repo root {root}: {', '.join(scripts)}")


def _check_budgets(plan: Plan, add: _Add) -> None:
    for task in plan.tasks:
        if task.size is None:
            add(f"Task {task.id} has no size declared; the research budget cannot be tiered per §1.4")
            continue
        try:
            research.budget(task.size)
        except research.ResearchError as e:
            add(f"Task {task.id}: {e}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or len(argv) > 2:
        print("Usage: python -m superdev.check <plan.yml> [repo root]", file=sys.stderr)
        return 2
    try:
        plan = load_plan(argv[0])
    except LedgerError as e:
        print(f"Failed to load plan: {e}", file=sys.stderr)
        return 1
    root = Path(argv[1]) if len(argv) == 2 else None
    problems = check_plan(plan, knowledge_dir=root)
    if problems:
        print(f"Validation found {len(problems)} problem(s):")
        for p in problems:
            print(f"- {p}")
        return 1
    print("Validation passed: 0 problems")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
