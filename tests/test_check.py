"""check.py 的测试：校验八项逐项一正一反，外加命令行入口。"""
from pathlib import Path

from superdev.check import check_plan, main
from superdev.ledger import Plan, Task, load_plan

REPO = Path(__file__).resolve().parents[1]      # 仓库根：复现脚本按它解析
FIX = Path(__file__).parent / "fixtures" / "check"


def _task(tid, *, status="draft", files=(), size="small", depends_on=(), **extra):
    """造一个默认合法的任务；group/strategy_changes 等台账附加字段经 extra 挂上。"""
    t = Task(
        id=tid,
        title=f"任务 {tid}",
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


# ---------- 1. 任务数量 ----------

def test_valid_fixture_passes_all_checks():
    plan = load_plan(FIX / "plan_check_ok.yml")
    assert check_plan(plan, knowledge_dir=REPO) == []


def test_zero_tasks_is_error():
    errors = check_plan(_plan())
    assert any("任务数量为 0" in e for e in errors)


def test_over_50_tasks_warns():
    tasks = [_task(f"T{i:02d}") for i in range(51)]
    errors = check_plan(_plan(*tasks))
    assert any(e.startswith("警告：任务数量 51") for e in errors)


# ---------- 2. 同组依赖无环 ----------

def test_cycle_within_group_reported():
    plan = _plan(
        _task("A", depends_on=["B"], group="支付"),
        _task("B", depends_on=["A"], group="支付"),
    )
    errors = check_plan(plan)
    assert any("依赖成环" in e and "组 支付" in e and "A" in e and "B" in e for e in errors)


def test_cross_group_dependency_is_not_a_cycle():
    plan = _plan(
        _task("A", depends_on=["B"], group="前端"),
        _task("B", group="后端"),
    )
    assert check_plan(plan) == []


def test_same_group_chain_is_ok():
    plan = _plan(
        _task("A", group="g"),
        _task("B", depends_on=["A"], group="g"),
        _task("C", depends_on=["B"], group="g"),
    )
    assert check_plan(plan) == []


# ---------- 3. depends_on 引用存在（委托 validate_basic） ----------

def test_missing_dependency_reported():
    errors = check_plan(_plan(_task("A", depends_on=["T99"])))
    assert any("T99" in e and "depends_on" in e for e in errors)


# ---------- 4. 并行就绪数 ----------

def _one_worker_module():
    return [{"repo": "r1", "max_workers": 1}]


def test_parallel_ready_over_limit_warns():
    plan = _plan(_task("A", status="ready"), _task("B", status="ready"),
                 modules=_one_worker_module())
    errors = check_plan(plan)
    assert any("警告" in e and "max_workers" in e for e in errors)


def test_parallel_ready_within_limit_ok():
    plan = _plan(_task("A", status="ready"), _task("B", status="ready"),
                 modules=[{"repo": "r1", "max_workers": 2}])
    assert check_plan(plan) == []


def test_blocked_ready_task_not_counted():
    # B 依赖的 A 还没 done/merged，就绪数只算 A 一个，不超过 max_workers=1
    plan = _plan(
        _task("A", status="ready"),
        _task("B", status="ready", depends_on=["A"]),
        modules=_one_worker_module(),
    )
    assert check_plan(plan) == []


# ---------- 5. 引擎/工人归属 ----------

def test_files_prefixes_allowed():
    plan = _plan(_task("A", files=["superdev/a.py", "tests/test_a.py",
                                  "commands/x.md", "agents/g.md", "README.md"]))
    assert check_plan(plan) == []


def test_files_out_of_bounds_error():
    errors = check_plan(_plan(_task("A", files=["docs/design.md"])))
    assert any("越界" in e and "docs/design.md" in e for e in errors)


# ---------- 6. 策略引用 ----------

def test_strategy_citing_research_ok():
    plan = _plan(_task(
        "A",
        strategy_changes="改用连接池规避锁竞争",
        research_notes="锁竞争：热点行被长事务持有",
    ))
    assert check_plan(plan) == []


def test_strategy_without_notes_error():
    plan = _plan(_task("A", strategy_changes="换个方向重写"))
    errors = check_plan(plan)
    assert any("research_notes" in e for e in errors)


def test_strategy_not_citing_notes_error():
    plan = _plan(_task(
        "A",
        strategy_changes="全部推倒重写",
        research_notes="锁竞争：热点行被长事务持有",
    ))
    errors = check_plan(plan)
    assert any("没有引用 research_notes" in e for e in errors)


# ---------- 7. 复现脚本 ----------

def test_repro_script_exists_ok(tmp_path):
    script = tmp_path / "tests" / "repro_case.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('boom')\n", encoding="utf-8")
    plan = _plan(_task("repro:case-1", files=["tests/repro_case.py"]))
    assert check_plan(plan, knowledge_dir=tmp_path) == []


def test_repro_script_file_missing_error(tmp_path):
    plan = _plan(_task("repro:case-1", files=["tests/repro_case.py"]))
    errors = check_plan(plan, knowledge_dir=tmp_path)
    assert any("不存在" in e and "tests/repro_case.py" in e for e in errors)


def test_repro_script_suffix_missing_error(tmp_path):
    notes = tmp_path / "tests" / "notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_text("x", encoding="utf-8")
    plan = _plan(_task("repro:case-2", files=["tests/notes.txt"]))
    errors = check_plan(plan, knowledge_dir=tmp_path)
    assert any("复现脚本" in e for e in errors)


# ---------- 8. 研究预算 ----------

def test_known_sizes_accepted():
    plan = _plan(*[_task(f"T{i}", size=s) for i, s in
                   enumerate(("trivial", "small", "medium", "large"), start=1)])
    assert check_plan(plan) == []


def test_unknown_size_error():
    errors = check_plan(_plan(_task("A", size="huge")))
    assert any("未知任务规模" in e and "huge" in e for e in errors)


def test_missing_size_error():
    errors = check_plan(_plan(_task("A", size=None)))
    assert any("未声明 size" in e for e in errors)


# ---------- 命令行 ----------

def test_cli_ok_exits_zero(capsys):
    code = main([str(FIX / "plan_check_ok.yml"), str(REPO)])
    out = capsys.readouterr().out
    assert code == 0
    assert "校验通过" in out


def test_cli_problems_exit_one(capsys):
    code = main([str(FIX / "plan_check_bad.yml")])
    out = capsys.readouterr().out
    assert code == 1
    assert "B99" in out and "huge" in out


def test_cli_load_failure_exit_one(capsys):
    code = main([str(FIX / "no_such_plan.yml")])
    err = capsys.readouterr().err
    assert code == 1
    assert "加载失败" in err
