"""Tests for ledger.py: happy-path parsing, missing fields, duplicate ids, bad dependencies, bad statuses, atomic writes."""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from superdev.ledger import (
    DEFAULT_STATUSES,
    LedgerError,
    init_last_green,
    load_bigdev_plan,
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
    assert plan.kind == "single-repo software"
    assert plan.version == "v0.1.0"
    assert plan.statuses[0] == "draft" and "stuck" in plan.statuses
    assert plan.modules[0]["repo"] == r"D:\devs\dev4\demo"
    assert len(plan.tasks) == 3
    t1 = plan.tasks[0]
    assert (t1.id, t1.title, t1.status) == ("T01", "Foundation task", "draft")
    assert t1.files == ["demo/a.py", "tests/test_a.py"]
    assert t1.size == "small"
    assert t1.research == ["Read the current state of demo/a.py"]
    assert t1.depends_on == []
    assert plan.tasks[1].depends_on == ["T01"]


def test_load_plan_optional_fields_default_to_empty():
    t3 = load_plan(FIX / "plan_valid.yml").tasks[2]
    assert (t3.files, t3.size, t3.research, t3.depends_on) == ([], None, [], [])


def test_load_plan_file_not_found():
    with pytest.raises(LedgerError, match="not found"):
        load_plan(FIX / "no_such_file.yml")


def test_load_plan_yaml_syntax_error(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("tasks: [unclosed", encoding="utf-8")
    with pytest.raises(LedgerError, match="syntax error"):
        load_plan(bad)


def test_load_plan_missing_required_field():
    with pytest.raises(LedgerError) as exc:
        load_plan(FIX / "plan_missing_field.yml")
    assert "title" in str(exc.value)


def test_load_plan_statuses_header_defaults():
    # Fixture deliberately omits the statuses header; should fall back to DEFAULT_STATUSES
    plan = load_plan(FIX / "plan_dup_id.yml")
    assert plan.statuses == DEFAULT_STATUSES


# ---------- validate_basic ----------

def test_validate_valid_plan_passes():
    assert validate_basic(load_plan(FIX / "plan_valid.yml")) is None


def test_validate_duplicate_id():
    plan = load_plan(FIX / "plan_dup_id.yml")  # loading itself does not dedupe; that is the validator's job
    with pytest.raises(LedgerError) as exc:
        validate_basic(plan)
    assert "Duplicate" in str(exc.value) and "T01" in str(exc.value)


def test_validate_unknown_dependency():
    with pytest.raises(LedgerError) as exc:
        validate_basic(load_plan(FIX / "plan_bad_dep.yml"))
    assert "T99" in str(exc.value) and "depends_on" in str(exc.value)


def test_validate_bad_status():
    with pytest.raises(LedgerError) as exc:
        validate_basic(load_plan(FIX / "plan_bad_status.yml"))
    assert "todo" in str(exc.value) and "enumeration" in str(exc.value)


def test_validate_enum_comes_from_file_header():
    # ready is legal under the default enumeration, but this fixture pins the
    # header to [todo, doing, done], so it must be judged illegal
    plan = load_plan(FIX / "plan_custom_statuses.yml")
    assert plan.statuses == ["todo", "doing", "done"]
    with pytest.raises(LedgerError) as exc:
        validate_basic(plan)
    assert "A2" in str(exc.value) and "ready" in str(exc.value)
    plan.tasks[1].status = "doing"  # back inside the enumeration: passes again
    assert validate_basic(plan) is None


# ---------- save_plan / atomic write ----------

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
        raise OSError("simulated replace failure")

    monkeypatch.setattr("superdev.ledger.os.replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        save_plan(load_plan(target), target)

    assert target.read_bytes() == before          # original file untouched
    assert list(tmp_path.glob("*.tmp")) == []     # temp file cleaned up


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
    assert reloaded.tasks[0].status == "draft"     # other tasks unaffected
    assert reloaded.statuses[0] == "draft"          # statuses header preserved


def test_update_task_status_rejects_illegal_status(tmp_path):
    target = _copy_plan(tmp_path)
    before = target.read_bytes()
    with pytest.raises(LedgerError) as exc:
        update_task_status(target, "T01", "todo")
    assert "Illegal status" in str(exc.value)
    assert target.read_bytes() == before           # file untouched on rejection


def test_update_task_status_unknown_task(tmp_path):
    target = _copy_plan(tmp_path)
    before = target.read_bytes()
    with pytest.raises(LedgerError) as exc:
        update_task_status(target, "T99", "ready")
    assert "Task 'T99' not found" in str(exc.value)
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
    with pytest.raises(LedgerError, match="top level"):
        load_status(FIX / "status_not_dict.yml")


def test_load_status_file_not_found():
    with pytest.raises(LedgerError, match="not found"):
        load_status(FIX / "no_status.yml")


# ---------- load_bigdev_plan (legacy bigdev ledger compat reading, detailed design §7) ----------

BIGDEV_FIX = FIX / "bigdev"


def test_load_bigdev_plan_field_mapping():
    plan = load_bigdev_plan(BIGDEV_FIX / "full.yml")
    assert plan.project == "demo"                       # module -> project
    assert plan.version == "v2.0_2026-01-05"
    assert plan.statuses == DEFAULT_STATUSES            # status enumeration uses the superdev full set
    t = plan.tasks[0]
    assert (t.id, t.title, t.status) == (
        "demo-api-search",
        "Paginate the search results while keeping assertion semantics unchanged.",
        "done",
    )
    assert t.files == [] and t.size == "medium" and t.research == []  # defaults filled in
    assert plan.tasks[2].depends_on == ["demo-api-search"]            # depends_on preserved


def test_load_bigdev_plan_status_mapping():
    by_id = {t.id: t.status for t in load_bigdev_plan(BIGDEV_FIX / "full.yml").tasks}
    assert by_id["demo-api-search"] == "done"           # done -> done
    assert by_id["demo-ui-theme"] == "in_progress"      # 进行中 -> in_progress
    assert by_id["demo-api-export"] == "ready"          # 待办 -> ready
    assert by_id["demo-ui-legacy-cleanup"] == "draft"   # queued has no mapping -> draft


def test_load_bigdev_plan_compat_notes_record_ignored_fields():
    notes = load_bigdev_plan(BIGDEV_FIX / "full.yml").compat_notes
    text = "\n".join(notes)
    assert "workers" in text                # the bigdev worker table has no superdev counterpart; must be recorded
    assert "lifecycle" in text              # task-level fields recorded too
    assert any("draft" in n for n in notes)  # unmapped statuses falling to draft are recorded too


def test_load_bigdev_plan_sparse_fixture_no_crash():
    plan = load_bigdev_plan(BIGDEV_FIX / "sparse.yml")
    assert plan.project == "demo"
    assert [t.id for t in plan.tasks] == ["t-a", "t-b"]
    assert all(t.status in DEFAULT_STATUSES for t in plan.tasks)
    assert all(t.size == "medium" for t in plan.tasks)
    by_id = {t.id: t for t in plan.tasks}
    assert by_id["t-a"].status == "draft"               # missing status -> draft
    assert by_id["t-b"].status == "ready"               # 待办 -> ready
    assert by_id["t-a"].title == "first-thing"          # no objective: falls back to the todo file name
    assert by_id["t-b"].title == "t-b"                  # no todo either: falls back to the id
    assert validate_basic(plan) is None                 # the fallback result passes basic validation directly


def test_load_bigdev_plan_native_plan_keeps_compat_notes_empty():
    # the native ledger path is unaffected by compat fields
    assert load_plan(FIX / "plan_valid.yml").compat_notes == []


# ---------- init_last_green (first-run status.yml initialization, detailed design §7) ----------


def test_init_last_green_first_run_writes_git_head(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "superdev.ledger.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout="abc123def456\n", returncode=0),
    )
    assert init_last_green(tmp_path) == "abc123def456"
    status = load_status(tmp_path / "status.yml")
    assert status["project"] == tmp_path.name
    assert status["modules"][0]["last_green"] == "abc123def456"


def test_init_last_green_no_git_falls_back_unverified(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr("superdev.ledger.subprocess.run", boom)
    assert init_last_green(tmp_path) == "unverified"
    assert load_status(tmp_path / "status.yml")["modules"][0]["last_green"] == "unverified"


def test_init_last_green_second_run_does_not_overwrite(tmp_path, monkeypatch):
    calls = []

    def fake_git(*a, **k):
        calls.append(a)
        return SimpleNamespace(stdout="1" * 40 + "\n", returncode=0)

    monkeypatch.setattr("superdev.ledger.subprocess.run", fake_git)
    assert init_last_green(tmp_path) == "1" * 40

    monkeypatch.setattr(
        "superdev.ledger.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout="2" * 40 + "\n", returncode=0),
    )
    assert init_last_green(tmp_path) == "1" * 40          # returns the old value
    assert len(calls) == 1                                # second run never touches git
    assert load_status(tmp_path / "status.yml")["modules"][0]["last_green"] == "1" * 40
