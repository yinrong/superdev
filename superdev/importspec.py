"""spec-kit 目录导入：plan.md / tasks.md 任务清单转成 派工计划.yml（设计文档 §8.1）。

spec-kit 管写规范，superdev 管执行落地，本模块只做两者的串联转换：
markdown 清单行（`- [ ] 标题` 或 `1. 标题`）逐条转台账任务，
粒度交给 check_plan 把关；超出导入上限的直接抛 ImportError 打回重拆。
"""
from __future__ import annotations

import re
from pathlib import Path

from superdev import check
from superdev.ledger import Plan, Task, save_plan

__all__ = ["SpecImportError", "import_spec"]

# 与 check.MAX_TASKS 一致的缺省上限；§8.1 的建议下限（少于它只是警告，不拦截）
MAX_TASKS = 50
MIN_RECOMMENDED = 10

# 两种清单行：未勾选的 checkbox（`- [ ]`，已勾选的 [x] 跳过），或行首编号（1. / 1、 / 1)）
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[\s\]\s*(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.、)]\s*(.+?)\s*$")

# spec-kit 约定的清单文件，plan.md 优先
_SPEC_FILES = ("plan.md", "tasks.md")


class SpecImportError(ImportError):
    """spec-kit 目录无法导入：找不到清单、清单里没有任务、或任务数超上限要先拆小。"""


def _size_of(title: str) -> str:
    """按标题长度粗分粒度：<20 字 trivial，<60 字 small，否则 medium。"""
    if len(title) < 20:
        return "trivial"
    if len(title) < 60:
        return "small"
    return "medium"


def _find_spec_file(spec_dir: Path) -> Path:
    for name in _SPEC_FILES:
        candidate = spec_dir / name
        if candidate.is_file():
            return candidate
    raise SpecImportError(
        f"spec-kit 目录里找不到 plan.md 或 tasks.md：{spec_dir}（先按 spec-kit 规范补任务清单再导入）"
    )


def _parse_titles(text: str) -> list[str]:
    """抽取任务标题：`- [ ] 标题` 与 `1. 标题` 各认一行；已勾选（[x]）与普通文字不算。"""
    titles: list[str] = []
    for line in text.splitlines():
        m = _CHECKBOX_RE.match(line)
        if m is None:
            m = _NUMBERED_RE.match(line)
        if m is not None:
            titles.append(m.group(1))
    return titles


def import_spec(
    spec_dir: str | Path,
    out_plan_path: str | Path,
    repo: str | Path = ".",
    max_tasks: int = MAX_TASKS,
) -> Plan:
    """读 spec-kit 风格目录，转成派工计划落盘，返回 Plan。

    - 任务 id 从 spec-001 起编号，status 一律 ready，depends_on 空；
      size 按标题长度粗分（trivial/small/medium）。
    - 任务数超过 max_tasks 抛 ImportError（先拆小再导入）；清单缺失或没有任务行同样抛。
    - 生成后立即 save_plan 落盘，并跑 check.check_plan；
      结果挂在返回值的 ``warnings`` 属性上（空列表 = 校验干净，警告不打断导入）。
    """
    spec_dir = Path(spec_dir)
    out_plan_path = Path(out_plan_path)

    spec_file = _find_spec_file(spec_dir)
    titles = _parse_titles(spec_file.read_text(encoding="utf-8"))
    if not titles:
        raise SpecImportError(
            f"任务清单是空的：{spec_file}（至少要有一行 `- [ ] 标题` 或 `1. 标题`）"
        )
    if len(titles) > max_tasks:
        raise SpecImportError(
            f"任务数量 {len(titles)} 超过导入上限 {max_tasks} 个：先拆小再导入（§8.1）"
        )

    tasks = [
        Task(
            id=f"spec-{i:03d}",
            title=title,
            status="ready",
            files=[],
            size=_size_of(title),
            research=[],
            depends_on=[],
        )
        for i, title in enumerate(titles, start=1)
    ]
    plan = Plan(
        project=spec_dir.name or "spec",
        kind="spec-kit",
        modules=[{"repo": str(repo)}],
        tasks=tasks,
    )

    out_plan_path.parent.mkdir(parents=True, exist_ok=True)
    save_plan(plan, out_plan_path)

    warnings = check.check_plan(plan, knowledge_dir=repo)
    if len(tasks) < MIN_RECOMMENDED:
        warnings.append(
            f"警告：任务数量 {len(tasks)} 少于 {MIN_RECOMMENDED} 个，粒度可能偏大，"
            "建议管理端进一步拆小（§8.1）"
        )
    plan.warnings = warnings
    return plan
