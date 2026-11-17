"""Tests for check.py: each of the eight checks with a positive and a negative case, plus the CLI entry point."""
from pathlib import Path

from superdev.check import check_plan, main
from superdev.ledger import Plan, Task, load_plan

REPO = Path(__file__).resolve().parents[1]      # repo root: repro scripts resolve against it
FIX = Path(__file__).parent / "fixtures" / "check"


def _task(tid, *, status="draft", files=(), size="small", depends_on=(), **extra):
    """Build a default-legal task; extra ledger add-ons (group/strategy_changes etc.) attach via extra."""
    t = Task(
        id=tid,
        title=f"Task {tid}",
        status=status,
        files=list(files),
        size=size,
        depends_on=list(depends_on),
    )
    for key, value in extra.items():
        setattr(t, key, value)
    return t


def _plan(*tasks, modules=()):
    return Plan(project="check-tests", modules=list(modules), tasks=list(tasks))


# ---------- 1. task count ----------

def test_valid_fixture_passes_all_checks():
    plan = load_plan(FIX / "plan_check_ok.yml")
    assert check_plan(plan, knowledge_dir=REPO) == []


def test_zero_tasks_is_error():
    errors = check_plan(_plan())
    assert any("Task count is 0" in e for e in errors)


def test_over_50_tasks_warns():
    tasks = [_task(f"T{i:02d}") for i in range(51)]
    errors = check_plan(_plan(*tasks))
    assert any(e.startswith("Warning: task count 51") for e in errors)


# ---------- 2. no dependency cycles within a group ----------

def test_cycle_within_group_reported():
    plan = _plan(
        _task("A", depends_on=["B"], group="payments"),
        _task("B", depends_on=["A"], group="payments"),
    )
    errors = check_plan(plan)
    assert any("Dependency cycle" in e and "group payments" in e and "A" in e and "B" in e for e in errors)


def test_cross_group_dependency_is_not_a_cycle():
    plan = _plan(
        _task("A", depends_on=["B"], group="frontend"),
        _task("B", group="backend"),
    )
    assert check_plan(plan) == []


def test_same_group_chain_is_ok():
    plan = _plan(
        _task("A", group="g"),
        _task("B", depends_on=["A"], group="g"),
        _task("C", depends_on=["B"], group="g"),
    )
    assert check_plan(plan) == []


# ---------- 3. depends_on references exist (delegated to validate_basic) ----------

def test_missing_dependency_reported():
    errors = check_plan(_plan(_task("A", depends_on=["T99"])))
    assert any("T99" in e and "depends_on" in e for e in errors)


# ---------- 4. parallel readiness count ----------

def _one_worker_module():
    return [{"repo": "r1", "max_workers": 1}]


def test_parallel_ready_over_limit_warns():
    plan = _plan(_task("A", status="ready"), _task("B", status="ready"),
                 modules=_one_worker_module())
    errors = check_plan(plan)
    assert any("Warning" in e and "max_workers" in e for e in errors)


def test_parallel_ready_within_limit_ok():
    plan = _plan(_task("A", status="ready"), _task("B", status="ready"),
                 modules=[{"repo": "r1", "max_workers": 2}])
    assert check_plan(plan) == []


def test_blocked_ready_task_not_counted():
    # A, which B depends on, is not yet done/merged, so only A counts as
    # ready — within max_workers=1
    plan = _plan(
        _task("A", status="ready"),
        _task("B", status="ready", depends_on=["A"]),
        modules=_one_worker_module(),
    )
    assert check_plan(plan) == []


# ---------- 5. engine/worker ownership ----------

def test_files_prefixes_allowed():
    plan = _plan(_task("A", files=["superdev/a.py", "tests/test_a.py",
                                  "commands/x.md", "agents/g.md", "README.md"]))
    assert check_plan(plan) == []


def test_files_out_of_bounds_error():
    errors = check_plan(_plan(_task("A", files=["docs/design.md"])))
    assert any("out of bounds" in e and "docs/design.md" in e for e in errors)


# ---------- 6. strategy citations ----------

def test_strategy_citing_research_ok():
    plan = _plan(_task(
        "A",
        strategy_changes="switch to a connection pool to avoid the lock contention",
        research_notes="lock contention: hot rows held by long transactions",
    ))
    assert check_plan(plan) == []


def test_strategy_without_notes_error():
    plan = _plan(_task("A", strategy_changes="rewrite in a different direction"))
    errors = check_plan(plan)
    assert any("research_notes" in e for e in errors)


def test_strategy_not_citing_notes_error():
    plan = _plan(_task(
        "A",
        strategy_changes="rewrite everything from scratch",
        research_notes="lock contention: hot rows held by long transactions",
    ))
    errors = check_plan(plan)
    assert any("do not cite any research_notes" in e for e in errors)


# ---------- 7. repro scripts ----------

def test_repro_script_exists_ok(tmp_path):
    script = tmp_path / "tests" / "repro_case.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('boom')\n", encoding="utf-8")
    plan = _plan(_task("repro:case-1", files=["tests/repro_case.py"]))
    assert check_plan(plan, knowledge_dir=tmp_path) == []


def test_repro_script_file_missing_error(tmp_path):
    plan = _plan(_task("repro:case-1", files=["tests/repro_case.py"]))
    errors = check_plan(plan, knowledge_dir=tmp_path)
    assert any("does not exist" in e and "tests/repro_case.py" in e for e in errors)


def test_repro_script_suffix_missing_error(tmp_path):
    notes = tmp_path / "tests" / "notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_text("x", encoding="utf-8")
    plan = _plan(_task("repro:case-2", files=["tests/notes.txt"]))
    errors = check_plan(plan, knowledge_dir=tmp_path)
    assert any("repro script" in e for e in errors)


# ---------- 8. research budgets ----------

def test_known_sizes_accepted():
    plan = _plan(*[_task(f"T{i}", size=s) for i, s in
                   enumerate(("trivial", "small", "medium", "large"), start=1)])
    assert check_plan(plan) == []


def test_unknown_size_error():
    errors = check_plan(_plan(_task("A", size="huge")))
    assert any("Unknown task size" in e and "huge" in e for e in errors)


def test_missing_size_error():
    errors = check_plan(_plan(_task("A", size=None)))
    assert any("no size declared" in e for e in errors)


# ---------- CLI ----------

def test_cli_ok_exits_zero(capsys):
    code = main([str(FIX / "plan_check_ok.yml"), str(REPO)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Validation passed" in out


def test_cli_problems_exit_one(capsys):
    code = main([str(FIX / "plan_check_bad.yml")])
    out = capsys.readouterr().out
    assert code == 1
    assert "B99" in out and "huge" in out


def test_cli_load_failure_exit_one(capsys):
    code = main([str(FIX / "no_such_plan.yml")])
    err = capsys.readouterr().err
    assert code == 1
    assert "Failed to load plan" in err
