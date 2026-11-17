"""spec-kit directory import: turn plan.md / tasks.md checklists into plan.yml (design doc §8.1).

spec-kit owns writing the spec and superdev owns executing it; this module only
converts between the two: markdown checklist lines (`- [ ] title` or `1. title`)
become ledger tasks one by one, granularity is policed by check_plan, and
anything over the import cap raises ImportError to be bounced back for splitting.
"""
from __future__ import annotations

import re
from pathlib import Path

from superdev import check
from superdev.ledger import Plan, Task, save_plan

__all__ = ["SpecImportError", "import_spec"]

# default cap matching check.MAX_TASKS; §8.1's recommended minimum (below it is only a warning, never a block)
MAX_TASKS = 50
MIN_RECOMMENDED = 10

# two checklist line shapes: an unchecked checkbox (`- [ ]`; a checked [x] is skipped),
# or a leading number (1. / 1、 / 1))
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[\s\]\s*(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.、)]\s*(.+?)\s*$")

# the checklist files the spec-kit convention prescribes; plan.md wins
_SPEC_FILES = ("plan.md", "tasks.md")


class SpecImportError(ImportError):
    """A spec-kit directory cannot be imported: no checklist found, no tasks in the checklist, or the task count exceeds the cap and must be split first."""


def _size_of(title: str) -> str:
    """Rough granularity cut by title length: <20 chars trivial, <60 small, otherwise medium."""
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
        f"no plan.md or tasks.md found in spec-kit directory: {spec_dir}"
        " (add a task checklist per the spec-kit convention before importing)"
    )


def _parse_titles(text: str) -> list[str]:
    """Extract task titles: `- [ ] title` and `1. title` each match one line; checked items ([x]) and plain prose don't count."""
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
    """Read a spec-kit style directory, convert it into a dispatch plan on disk, and return the Plan.

    - task ids number from spec-001, status is always ready, depends_on is empty;
      size is a rough cut by title length (trivial/small/medium).
    - More than max_tasks tasks raises ImportError (split first, then import);
      a missing checklist or a checklist with no task lines raises too.
    - save_plan writes the file immediately, then check.check_plan runs;
      the results hang off the returned object's ``warnings`` attribute
      (an empty list = clean validation; warnings never interrupt the import).
    """
    spec_dir = Path(spec_dir)
    out_plan_path = Path(out_plan_path)

    spec_file = _find_spec_file(spec_dir)
    titles = _parse_titles(spec_file.read_text(encoding="utf-8"))
    if not titles:
        raise SpecImportError(
            f"task checklist is empty: {spec_file} (need at least one `- [ ] title` or `1. title` line)"
        )
    if len(titles) > max_tasks:
        raise SpecImportError(
            f"task count {len(titles)} exceeds the import cap of {max_tasks}: split into smaller tasks before importing (§8.1)"
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
            f"warning: task count {len(tasks)} is fewer than the recommended minimum"
            f" {MIN_RECOMMENDED}; granularity may be too coarse, the manager side"
            " should split further (§8.1)"
        )
    plan.warnings = warnings
    return plan
