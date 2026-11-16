"""ledger.py 的测试：正常解析、缺字段、重复 id、坏依赖、坏状态、原子写。"""
import shutil
from pathlib import Path

import pytest

from superdev.ledger import (
    DEFAULT_STATUSES,
    LedgerError,
    load_plan,
    load_status,
    save_plan,
    update_task_status,
    validate_basic,
)

FIX = Path(__file__).parent / "fixtures" / "ledger"


# ---------- load_plan ----------

def test_load_plan_valid():
    plan = load_plan(FIX / "plan_valid.yml")
    assert plan.project == "demo"
    assert plan.kind == "单仓软件"
    assert plan.version == "v0.1.0"
    assert plan.statuses[0] == "draft" and "stuck" in plan.statuses
    assert plan.modules[0]["repo"] == r"D:\devs\dev4\demo"
    assert len(plan.tasks) == 3
    t1 = plan.tasks[0]
    assert (t1.id, t1.title, t1.status) == ("T01", "地基任务", "draft")
    assert t1.files == ["demo/a.py", "tests/test_a.py"]
    assert t1.size == "small"
    assert t1.research == ["读 demo/a.py 现状"]
    assert t1.depends_on == []
    assert plan.tasks[1].depends_on == ["T01"]


def test_load_plan_optional_fields_default_to_empty():
    t3 = load_plan(FIX / "plan_valid.yml").tasks[2]
    assert (t3.files, t3.size, t3.research, t3.depends_on) == ([], None, [], [])


def test_load_plan_file_not_found():
    with pytest.raises(LedgerError, match="不存在"):
        load_plan(FIX / "no_such_file.yml")


def test_load_plan_yaml_syntax_error(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("tasks: [未闭合", encoding="utf-8")
    with pytest.raises(LedgerError, match="语法错误"):
        load_plan(bad)


def test_load_plan_missing_required_field():
    with pytest.raises(LedgerError) as exc:
        load_plan(FIX / "plan_missing_field.yml")
    assert "title" in str(exc.value)


def test_load_plan_statuses_header_defaults():
    # 夹具故意不写 statuses 头，应回退到 DEFAULT_STATUSES
    plan = load_plan(FIX / "plan_dup_id.yml")
    assert plan.statuses == DEFAULT_STATUSES


# ---------- validate_basic ----------

def test_validate_valid_plan_passes():
    assert validate_basic(load_plan(FIX / "plan_valid.yml")) is None


def test_validate_duplicate_id():
    plan = load_plan(FIX / "plan_dup_id.yml")  # 加载本身不查重，交给校验器
    with pytest.raises(LedgerError) as exc:
        validate_basic(plan)
    assert "重复" in str(exc.value) and "T01" in str(exc.value)


def test_validate_unknown_dependency():
    with pytest.raises(LedgerError) as exc:
        validate_basic(load_plan(FIX / "plan_bad_dep.yml"))
    assert "T99" in str(exc.value) and "depends_on" in str(exc.value)


def test_validate_bad_status():
    with pytest.raises(LedgerError) as exc:
        validate_basic(load_plan(FIX / "plan_bad_status.yml"))
    assert "todo" in str(exc.value) and "枚举" in str(exc.value)


def test_validate_enum_comes_from_file_header():
    # ready 在默认枚举合法，但本夹具头部写死了 [todo, doing, done]，必须判非法
    plan = load_plan(FIX / "plan_custom_statuses.yml")
    assert plan.statuses == ["todo", "doing", "done"]
    with pytest.raises(LedgerError) as exc:
        validate_basic(plan)
    assert "A2" in str(exc.value) and "ready" in str(exc.value)
    plan.tasks[1].status = "doing"  # 改回枚举内即通过
    assert validate_basic(plan) is None


# ---------- save_plan / 原子写 ----------

def test_save_plan_roundtrip(tmp_path):
    plan = load_plan(FIX / "plan_valid.yml")
    saved = tmp_path / "saved.yml"
    save_plan(plan, saved)
    assert load_plan(saved) == plan


def test_save_plan_failure_keeps_original_and_cleans_tmp(monkeypatch, tmp_path):
    target = tmp_path / "plan.yml"
    shutil.copy(FIX / "plan_valid.yml", target)
    before = target.read_bytes()

    def boom(src, dst):
        raise OSError("模拟替换失败")

    monkeypatch.setattr("superdev.ledger.os.replace", boom)
    with pytest.raises(OSError, match="模拟替换失败"):
        save_plan(load_plan(target), target)

    assert target.read_bytes() == before          # 原文件分毫未动
    assert list(tmp_path.glob("*.tmp")) == []     # 临时文件已清理


# ---------- update_task_status ----------

def _copy_plan(tmp_path, name="plan.yml"):
    target = tmp_path / name
    shutil.copy(FIX / "plan_valid.yml", target)
    return target


def test_update_task_status(tmp_path):
    target = _copy_plan(tmp_path)
    update_task_status(target, "T02", "in_progress")
    reloaded = load_plan(target)
    assert reloaded.tasks[1].status == "in_progress"
    assert reloaded.tasks[0].status == "draft"     # 其他任务不受影响
    assert reloaded.statuses[0] == "draft"          # 枚举头保住了


def test_update_task_status_rejects_illegal_status(tmp_path):
    target = _copy_plan(tmp_path)
    before = target.read_bytes()
    with pytest.raises(LedgerError) as exc:
        update_task_status(target, "T01", "todo")
    assert "非法状态" in str(exc.value)
    assert target.read_bytes() == before           # 拒绝时文件不动


def test_update_task_status_unknown_task(tmp_path):
    target = _copy_plan(tmp_path)
    before = target.read_bytes()
    with pytest.raises(LedgerError) as exc:
        update_task_status(target, "T99", "ready")
    assert "找不到任务 'T99'" in str(exc.value)
    assert target.read_bytes() == before


# ---------- load_status ----------

def test_load_status_valid():
    status = load_status(FIX / "status_valid.yml")
    assert status["project"] == "demo"
    assert status["modules"][0]["repo"] == r"D:\devs\dev4\demo"
    assert status["modules"][0]["last_green"] == "3f2b1c0d5e6f7890abcdef1234567890abcdef12"
    assert status["metrics"][0]["done_count"] == 2
    assert status["metrics"][0]["worker_hours"] == 1.5


def test_load_status_last_green_defaults_to_none(tmp_path):
    p = tmp_path / "status.yml"
    p.write_text(
        "project: demo\nmodules:\n  - repo: r1\n", encoding="utf-8"
    )
    status = load_status(p)
    assert status["modules"][0]["last_green"] is None
    assert status["modules"] == [{"repo": "r1", "last_green": None}]


def test_load_status_module_missing_repo():
    with pytest.raises(LedgerError) as exc:
        load_status(FIX / "status_bad_module.yml")
    assert "repo" in str(exc.value)


def test_load_status_top_level_not_mapping():
    with pytest.raises(LedgerError, match="顶层"):
        load_status(FIX / "status_not_dict.yml")


def test_load_status_file_not_found():
    with pytest.raises(LedgerError, match="不存在"):
        load_status(FIX / "no_status.yml")
