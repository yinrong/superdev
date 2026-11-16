"""superdev_cli.py 的测试：各子命令经 subprocess 真跑，验证输出与退出码。"""
import os
import subprocess
import sys
from pathlib import Path

from superdev.events import TASK_CREATED, WORKER_IDLE, EventLog

REPO = Path(__file__).resolve().parents[1]

GOOD_PLAN = """\
project: clidemo
tasks:
  - id: T01
    title: 任务一
    status: ready
    files: [superdev/a.py]
    size: small
  - id: T02
    title: 任务二
    status: in_progress
    files: [tests/test_a.py]
    size: medium
    depends_on: [T01]
"""

BAD_PLAN = """\
project: clidemo
tasks:
  - id: T01
    title: 任务一
    status: ready
    files: [docs/design.md]
"""


def run_cli(*args, cwd=None) -> subprocess.CompletedProcess:
    """用当前解释器真跑 superdev_cli.py，退出码和输出原样带回。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(REPO / "superdev_cli.py"), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or REPO),
        env=env,
        timeout=120,
    )


def _project(tmp_path: Path, plan_text: str = GOOD_PLAN) -> Path:
    proj = tmp_path / "clidemo"
    proj.mkdir()
    (proj / "派工计划.yml").write_text(plan_text, encoding="utf-8")
    return proj


# ---------- check ----------

def test_check_good_plan_exits_zero(tmp_path):
    proj = _project(tmp_path)
    r = run_cli("check", proj / "派工计划.yml")
    assert r.returncode == 0
    assert "校验通过" in r.stdout


def test_check_bad_plan_exits_one(tmp_path):
    proj = _project(tmp_path, BAD_PLAN)
    r = run_cli("check", proj / "派工计划.yml")
    assert r.returncode == 1
    assert "越界" in r.stdout


def test_check_missing_plan_exits_one(tmp_path):
    r = run_cli("check", tmp_path / "nope.yml")
    assert r.returncode == 1
    assert "加载失败" in r.stderr


# ---------- status ----------

def test_status_without_projection_hints_round(tmp_path):
    proj = _project(tmp_path)
    r = run_cli("status", proj)
    assert r.returncode == 0
    assert "round" in r.stdout


def test_status_prints_task_lines_after_round(tmp_path):
    proj = _project(tmp_path)
    assert run_cli("round", proj).returncode == 0
    r = run_cli("status", proj)
    assert r.returncode == 0
    assert "T01" in r.stdout and "ready" in r.stdout
    assert "红0次" in r.stdout


# ---------- round ----------

def test_round_refreshes_projection_and_counts_pending(tmp_path):
    proj = _project(tmp_path)
    log = EventLog(proj / "events.jsonl")
    log.append(TASK_CREATED, {"task_id": "T01"})
    log.append(WORKER_IDLE, {"worker": "w1"})
    r = run_cli("round", proj)
    assert r.returncode == 0
    assert "投影已刷新：2 个任务" in r.stdout
    assert "待办事件：2 条" in r.stdout
    assert (proj / "projection.db").exists()


def test_round_missing_plan_exits_one(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    r = run_cli("round", proj)
    assert r.returncode == 1
    assert "不存在" in r.stderr


# ---------- verify ----------

def test_verify_intact_log_exits_zero(tmp_path):
    log_path = tmp_path / "events.jsonl"
    EventLog(log_path).append(TASK_CREATED, {"task_id": "T01"})
    r = run_cli("verify", log_path)
    assert r.returncode == 0
    assert "校验通过" in r.stdout


def test_verify_tampered_log_exits_one(tmp_path):
    log_path = tmp_path / "events.jsonl"
    EventLog(log_path).append(TASK_CREATED, {"task_id": "T01"})
    raw = log_path.read_text(encoding="utf-8")
    # canonical 序列化冒号后无空格，按无空格替换才改得中
    log_path.write_text(
        raw.replace('"task_id":"T01"', '"task_id":"T99"'), encoding="utf-8"
    )
    r = run_cli("verify", log_path)
    assert r.returncode == 1
    assert "校验失败" in r.stdout


# ---------- import-spec ----------

def _spec_dir(tmp_path: Path, plan_text: str) -> Path:
    spec = tmp_path / "feat"
    spec.mkdir()
    (spec / "plan.md").write_text(plan_text, encoding="utf-8")
    return spec


def test_import_spec_good_path_writes_plan_and_prints_warnings(tmp_path):
    spec = _spec_dir(
        tmp_path,
        "- [ ] 改 README 错字\n"
        "- [ ] 给台账加载器补一个按模块过滤任务列表的参数并写测试\n",
    )
    out = tmp_path / "feat-plan.yml"
    r = run_cli("import-spec", spec, out)
    assert r.returncode == 0
    assert "导入完成：2 个任务" in r.stdout
    # 2 个任务少于建议下限 10，check 警告要照打但退出码仍是 0
    assert "check 警告 1 处" in r.stdout
    assert "少于" in r.stdout
    assert out.exists()


def test_import_spec_over_limit_exits_one(tmp_path):
    spec = _spec_dir(tmp_path, "".join(f"- [ ] 任务 {i}\n" for i in range(1, 5)))
    r = run_cli("import-spec", spec, tmp_path / "plan.yml", "--max-tasks", "3")
    assert r.returncode == 1
    assert "先拆小再导入" in r.stderr
    assert not (tmp_path / "plan.yml").exists()


def test_import_spec_missing_list_exits_one(tmp_path):
    empty = tmp_path / "empty-spec"
    empty.mkdir()
    r = run_cli("import-spec", empty, tmp_path / "plan.yml")
    assert r.returncode == 1
    assert "找不到 plan.md 或 tasks.md" in r.stderr


# ---------- 入口 ----------

def test_no_subcommand_prints_usage_exit_two():
    r = run_cli()
    assert r.returncode == 2
    assert "usage" in r.stderr.lower()
