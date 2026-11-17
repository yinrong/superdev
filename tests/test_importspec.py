"""Tests for importspec.py: both checklist formats, over-cap bounce, empty directory, round-trip on disk, clean check."""
from pathlib import Path

import pytest

from superdev.check import check_plan
from superdev.importspec import SpecImportError, import_spec
from superdev.ledger import load_plan

# sample titles for the three granularity tiers: short (<20 chars), mid (20-59), long (>=60)
_SHORT = "Fix the README typo"  # 19 chars -> trivial
_MID = "Add a module filter arg to the ledger loader, with tests"  # 56 chars -> small
# 190 chars -> medium
_LONG = (
    "Refactor the whole event log module: turn the in-memory hash chain into an"
    " on-disk structure with incremental verification, keep every existing caller"
    " compatible, and add boundary tests for concurrent writes, checkpoint"
    " recovery, and historical data migration"
)


def _make_spec(tmp_path: Path, name: str, filename: str, content: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / filename).write_text(content, encoding="utf-8")
    return d


# ---------- the two checklist formats ----------

def test_checkbox_format(tmp_path):
    spec = _make_spec(tmp_path, "feat-a", "plan.md", (
        "# Plan\n"
        "Background prose, not a task.\n"
        f"  - [ ] {_SHORT}\n"
        f"- [ ] {_MID}\n"
        "- [x] old task already done\n"
    ))
    out = tmp_path / "feat-a.yml"
    plan = import_spec(spec, out)
    assert [t.title for t in plan.tasks] == [_SHORT, _MID]
    assert [t.id for t in plan.tasks] == ["spec-001", "spec-002"]
    assert all(t.status == "ready" for t in plan.tasks)
    assert all(t.depends_on == [] for t in plan.tasks)
    assert plan.project == "feat-a"


def test_numbered_format(tmp_path):
    spec = _make_spec(tmp_path, "feat-b", "tasks.md", (
        f"1. {_SHORT}\n"
        f"2、{_MID}\n"  # the Chinese enumeration comma also counts as numbering
        "plain paragraph, no number\n"
    ))
    plan = import_spec(spec, tmp_path / "feat-b.yml")
    assert [t.title for t in plan.tasks] == [_SHORT, _MID]


def test_plan_md_wins_over_tasks_md(tmp_path):
    spec = tmp_path / "feat-c"
    spec.mkdir()
    (spec / "plan.md").write_text(f"- [ ] {_SHORT}\n", encoding="utf-8")
    (spec / "tasks.md").write_text("1. a task that must not be read\n", encoding="utf-8")
    plan = import_spec(spec, tmp_path / "c.yml")
    assert [t.title for t in plan.tasks] == [_SHORT]


# ---------- granularity tiers ----------

def test_size_tiers_by_title_length(tmp_path):
    spec = _make_spec(tmp_path, "feat-d", "plan.md", (
        f"- [ ] {_SHORT}\n- [ ] {_MID}\n- [ ] {_LONG}\n"
    ))
    plan = import_spec(spec, tmp_path / "d.yml")
    assert [t.size for t in plan.tasks] == ["trivial", "small", "medium"]


# ---------- bounces: over cap / empty directory / blank checklist ----------

def test_over_limit_raises(tmp_path):
    spec = _make_spec(tmp_path, "feat-e", "plan.md", "".join(
        f"- [ ] task {i}\n" for i in range(1, 5)
    ))
    with pytest.raises(ImportError, match="split into smaller tasks before importing"):
        import_spec(spec, tmp_path / "e.yml", max_tasks=3)


def test_empty_dir_raises(tmp_path):
    spec = tmp_path / "empty"
    spec.mkdir()
    with pytest.raises(SpecImportError, match="no plan.md"):
        import_spec(spec, tmp_path / "e.yml")


def test_blank_checklist_raises(tmp_path):
    spec = _make_spec(tmp_path, "feat-f", "plan.md", "# only a heading, no tasks\n")
    with pytest.raises(SpecImportError, match="empty"):
        import_spec(spec, tmp_path / "f.yml")


# ---------- on-disk output ----------

def test_output_roundtrips_with_load_plan(tmp_path):
    spec = _make_spec(tmp_path, "feat-g", "plan.md", (
        f"1. {_SHORT}\n2. {_MID}\n"
    ))
    out = tmp_path / "sub" / "g.yml"
    plan = import_spec(spec, out, repo=r"D:\devs\dev4\demo")
    assert out.is_file()
    loaded = load_plan(out)
    assert loaded.project == "feat-g"
    assert loaded.kind == "spec-kit"
    assert loaded.modules == [{"repo": r"D:\devs\dev4\demo"}]
    assert [(t.id, t.title, t.size) for t in loaded.tasks] == [
        ("spec-001", _SHORT, "trivial"),
        ("spec-002", _MID, "small"),
    ]


def test_check_passes_and_warnings_come_back(tmp_path):
    spec = _make_spec(tmp_path, "feat-h", "plan.md", "".join(
        f"- [ ] task item {i}: finish the small change and add matching unit tests\n" for i in range(1, 12)
    ))
    out = tmp_path / "h.yml"
    plan = import_spec(spec, out)
    # 11 tasks: all eight checks pass; the granularity sits in the 10-50 band,
    # so not even the "too coarse" warning shows up
    assert check_plan(load_plan(out)) == []
    assert plan.warnings == []


def test_few_tasks_get_granularity_warning(tmp_path):
    spec = _make_spec(tmp_path, "feat-i", "plan.md", f"- [ ] {_SHORT}\n")
    plan = import_spec(spec, tmp_path / "i.yml")
    assert any("fewer than" in w for w in plan.warnings)
    # the warning never interrupts: check itself raises nothing for a small task count
    assert check_plan(load_plan(tmp_path / "i.yml")) == []
