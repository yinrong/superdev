"""派工计划校验器：设计文档 §2 的"校验八项"。

命令行：python -m superdev.check <派工计划.yml> [仓库根目录]
check_plan 返回中文问题清单（含警告），空列表即通过。
基础三项（id 唯一、depends_on 引用存在、状态枚举）委托 ledger.validate_basic，
拆行去重后并入清单；本模块补齐台账格式之外的八项规则。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from superdev import research
from superdev.ledger import LedgerError, Plan, Task, load_plan, validate_basic

__all__ = ["check_plan", "main"]

# §8.1 的拆分区间：任务数 1–50；0 个直接无法派发
MIN_TASKS = 1
MAX_TASKS = 50

# §2 引擎与工人归属：工人只准动模块仓的这几处，管理端只写台账
_OWNED_PREFIXES = ("superdev/", "tests/", "commands/", "agents/")
_OWNED_FILES = ("README.md", "superdev_cli.py", "pytest.ini")  # 仓库根的入口与配置，验收时补录

# 依赖已齐的状态：下游 ready 任务可立刻并行派发
_DONE_STATUSES = frozenset({"done", "merged"})

# §1.1 第 5 条：复现微任务必须带一条命令就能重跑的脚本
_REPRO_ID_PREFIX = "repro:"
_REPRO_SUFFIXES = (".sh", ".py", ".ps1")

# 收口函数：同一条消息只进清单一次（validate_basic 的结果与本地发现去重）
_Add = Callable[[str], None]


def check_plan(plan: Plan, *, knowledge_dir: str | Path | None = None) -> list[str]:
    """逐项校验派工计划，返回中文问题清单，空列表 = 通过。

    knowledge_dir 是仓库根目录，复现脚本按它解析相对路径；缺省用当前工作目录。
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
        add(f"任务数量为 0：派工计划至少要有 {MIN_TASKS} 个任务，空计划无法派发")
    elif n > MAX_TASKS:
        add(f"警告：任务数量 {n} 超过 {MAX_TASKS} 个，拆分粒度失衡，建议管理端重拆（§8.1）")


def _check_basic_and_cycles(plan: Plan, add: _Add) -> None:
    """基础校验委托 ledger.validate_basic，环检测只在本模块做。"""
    try:
        validate_basic(plan)
    except LedgerError as e:
        for line in str(e).splitlines():
            line = line.strip()
            if line.startswith("- "):
                add(line[2:])
    _check_cycles(plan.tasks, add)


def _check_cycles(tasks: list[Task], add: _Add) -> None:
    """同 group 任务内做拓扑环检测；无 group 字段视为各自独立（单人成组）。"""
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
                    continue  # 跨组依赖或悬空引用：环只在本组内找
                if color[dep] == GRAY:
                    cycle = stack[stack.index(dep):] + [dep]
                    where = f"（组 {label}）" if label else ""
                    add(f"依赖成环{where}：{' → '.join(cycle)}")
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
        return  # 台账没写并发上限，无从比对
    status_of = {t.id: t.status for t in plan.tasks}
    ready = [
        t.id
        for t in plan.tasks
        if t.status == "ready"
        and all(status_of.get(d) in _DONE_STATUSES for d in t.depends_on)
    ]
    if len(ready) > limit:
        add(
            f"警告：并行就绪任务 {len(ready)} 个（{'、'.join(ready)}），"
            f"超过全部模块 max_workers 总和 {limit}，派发前先收口或调大并发"
        )


def _check_files_owned(plan: Plan, add: _Add) -> None:
    for task in plan.tasks:
        for path in task.files:
            owned = str(path) in _OWNED_FILES or any(
                str(path).startswith(p) for p in _OWNED_PREFIXES
            )
            if not owned:
                add(
                    f"任务 {task.id} 的 files 越界：{path}"
                    "（只允许 superdev/、tests/、commands/、agents/ 及仓库根的 README.md、superdev_cli.py、pytest.ini）"
                )


def _check_strategy_cites(plan: Plan, add: _Add) -> None:
    # Task 不保存这两个字段，靠属性探测；计划里没写就视为无策略变更
    for task in plan.tasks:
        changes = getattr(task, "strategy_changes", None)
        if not changes:
            continue
        notes = getattr(task, "research_notes", None)
        if not notes:
            add(f"任务 {task.id} 有策略变更但缺 research_notes，派发器直接拒绝（§1.4）")
        elif not research.strategy_cites(str(changes), str(notes)):
            add(f"任务 {task.id} 的策略变更没有引用 research_notes 的任何关键词")


def _check_repro_scripts(plan: Plan, root: Path, add: _Add) -> None:
    for task in plan.tasks:
        if not task.id.startswith(_REPRO_ID_PREFIX):
            continue
        scripts = [
            str(f) for f in task.files if str(f).lower().endswith(_REPRO_SUFFIXES)
        ]
        if not scripts:
            add(f"任务 {task.id} 是复现任务，files 里必须带一个 .sh/.py/.ps1 复现脚本")
        elif not any((root / f).is_file() for f in scripts):
            add(f"任务 {task.id} 的复现脚本在仓库根 {root} 下不存在：{'、'.join(scripts)}")


def _check_budgets(plan: Plan, add: _Add) -> None:
    for task in plan.tasks:
        if task.size is None:
            add(f"任务 {task.id} 未声明 size，研究预算无法按 §1.4 分档核定")
            continue
        try:
            research.budget(task.size)
        except research.ResearchError as e:
            add(f"任务 {task.id}：{e}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or len(argv) > 2:
        print("用法：python -m superdev.check <派工计划.yml> [仓库根目录]", file=sys.stderr)
        return 2
    try:
        plan = load_plan(argv[0])
    except LedgerError as e:
        print(f"派工计划加载失败：{e}", file=sys.stderr)
        return 1
    root = Path(argv[1]) if len(argv) == 2 else None
    problems = check_plan(plan, knowledge_dir=root)
    if problems:
        print(f"校验发现 {len(problems)} 处问题：")
        for p in problems:
            print(f"- {p}")
        return 1
    print("校验通过：0 处问题")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
