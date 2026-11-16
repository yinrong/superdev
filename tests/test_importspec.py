"""importspec.py 的测试：两种清单格式、超限打回、空目录、落盘读回、check 通过。"""
from pathlib import Path

import pytest

from superdev.check import check_plan
from superdev.importspec import SpecImportError, import_spec
from superdev.ledger import load_plan

# 三档粒度的样例标题：短（<20 字）、中（20–59 字）、长（≥60 字）
_SHORT = "改 README 错字"  # 9 字 → trivial
_MID = "给台账加载器补一个按模块过滤任务列表的参数并写测试"  # 25 字 → small
# 65 字 → medium
_LONG = "重构整个事件日志模块，把内存哈希链改成支持增量校验的落盘结构，兼容既有全部调用方，并补齐并发写入、断点恢复与历史数据迁移的边界测试"


def _make_spec(tmp_path: Path, name: str, filename: str, content: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / filename).write_text(content, encoding="utf-8")
    return d


# ---------- 两种清单格式 ----------

def test_checkbox_format(tmp_path):
    spec = _make_spec(tmp_path, "feat-a", "plan.md", (
        "# 计划\n"
        "背景说明，不是任务。\n"
        "  - [ ] 改 README 错字\n"
        "- [ ] 给台账加载器补一个按模块过滤任务列表的参数并写测试\n"
        "- [x] 已完成的旧任务\n"
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
        "1. 改 README 错字\n"
        "2、给台账加载器补一个按模块过滤任务列表的参数并写测试\n"
        "普通段落，没有编号\n"
    ))
    plan = import_spec(spec, tmp_path / "feat-b.yml")
    assert [t.title for t in plan.tasks] == [_SHORT, _MID]


def test_plan_md_wins_over_tasks_md(tmp_path):
    spec = tmp_path / "feat-c"
    spec.mkdir()
    (spec / "plan.md").write_text("- [ ] 改 README 错字\n", encoding="utf-8")
    (spec / "tasks.md").write_text("1. 不该被读到的任务\n", encoding="utf-8")
    plan = import_spec(spec, tmp_path / "c.yml")
    assert [t.title for t in plan.tasks] == [_SHORT]


# ---------- 粒度分档 ----------

def test_size_tiers_by_title_length(tmp_path):
    spec = _make_spec(tmp_path, "feat-d", "plan.md", (
        f"- [ ] {_SHORT}\n- [ ] {_MID}\n- [ ] {_LONG}\n"
    ))
    plan = import_spec(spec, tmp_path / "d.yml")
    assert [t.size for t in plan.tasks] == ["trivial", "small", "medium"]


# ---------- 打回：超限 / 空目录 / 空清单 ----------

def test_over_limit_raises(tmp_path):
    spec = _make_spec(tmp_path, "feat-e", "plan.md", "".join(
        f"- [ ] 任务 {i}\n" for i in range(1, 5)
    ))
    with pytest.raises(ImportError, match="先拆小再导入"):
        import_spec(spec, tmp_path / "e.yml", max_tasks=3)


def test_empty_dir_raises(tmp_path):
    spec = tmp_path / "empty"
    spec.mkdir()
    with pytest.raises(SpecImportError, match="找不到 plan.md"):
        import_spec(spec, tmp_path / "e.yml")


def test_blank_checklist_raises(tmp_path):
    spec = _make_spec(tmp_path, "feat-f", "plan.md", "# 只有标题没有任务\n")
    with pytest.raises(SpecImportError, match="空的"):
        import_spec(spec, tmp_path / "f.yml")


# ---------- 落盘产物 ----------

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
        f"- [ ] 任务条目 {i}：把某某小改动做完并补上对应单元测试用例\n" for i in range(1, 12)
    ))
    out = tmp_path / "h.yml"
    plan = import_spec(spec, out)
    # 11 个任务：check 八项全过；粒度在 10–50 区间，连"偏大"警告也没有
    assert check_plan(load_plan(out)) == []
    assert plan.warnings == []


def test_few_tasks_get_granularity_warning(tmp_path):
    spec = _make_spec(tmp_path, "feat-i", "plan.md", "- [ ] 改 README 错字\n")
    plan = import_spec(spec, tmp_path / "i.yml")
    assert any("少于 10" in w for w in plan.warnings)
    # 警告不打断：check 本身对少量任务不报错
    assert check_plan(load_plan(tmp_path / "i.yml")) == []
