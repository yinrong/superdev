"""hooks.py 的测试：四个 hook 裁决逐个正反路径，stop_gate 用临时仓真跑 pytest。"""
import os
from pathlib import Path

import pytest

from superdev.events import TASK_CREATED, WORKER_IDLE, EventLog
from superdev.hooks import guard_command, guard_write, post_write_check, stop_gate

GOOD_PLAN = """\
project: hookdemo
tasks:
  - id: T01
    title: 演示任务
    status: ready
    files: [superdev/a.py]
    size: small
"""

BAD_PLAN = """\
project: hookdemo
tasks:
  - id: T01
    title: 演示任务
    status: ready
    files: [docs/design.md]
"""

PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING_TEST = "def test_bad():\n    assert False\n"


# ---------- guard_write：越界写 ----------

def test_guard_write_inside_root_allowed(tmp_path):
    root = tmp_path / "ledger"
    root.mkdir()
    assert guard_write(root / "派工计划.yml", root) == (True, "")


def test_guard_write_prefix_sibling_denied(tmp_path):
    # ledger2 只是名字前缀相似，不是 ledger 的子路径，必须拦
    root = tmp_path / "ledger"
    root.mkdir()
    (tmp_path / "ledger2").mkdir()
    ok, msg = guard_write(tmp_path / "ledger2" / "plan.yml", root)
    assert ok is False
    assert msg == "管理端只写台账"


def test_guard_write_other_dir_denied(tmp_path):
    root = tmp_path / "ledger"
    root.mkdir()
    ok, msg = guard_write(tmp_path / "elsewhere" / "x.yml", root)
    assert ok is False
    assert "管理端只写台账" in msg


def test_guard_write_relative_paths_use_abspath(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ok, _ = guard_write("ledger/a.yml", "ledger")
    assert ok is True


@pytest.mark.skipif(os.name != "nt", reason="跨盘符比较只在 Windows 上有意义")
def test_guard_write_different_drive_denied(tmp_path):
    other = "Q:\\" if tmp_path.drive != "Q:" else "X:\\"
    ok, msg = guard_write(f"{tmp_path.drive}\\somewhere\\a.yml", other)
    assert ok is False
    assert msg == "管理端只写台账"


# ---------- guard_command：commit/push 拦截 ----------

def test_guard_command_commit_string_denied():
    assert guard_command("git commit -m 台账更新") == (False, "管理端不代做交付")


def test_guard_command_push_list_denied():
    assert guard_command(["git", "push", "origin", "main"]) == (False, "管理端不代做交付")


def test_guard_command_git_with_option_denied():
    # git -C <路径> commit：选项和值跳过后仍要认出 commit
    assert guard_command("git -C ../repo commit -m x") == (False, "管理端不代做交付")


def test_guard_command_normal_allowed():
    assert guard_command(["python", "-m", "pytest", "-q"]) == (True, "")


def test_guard_command_readonly_git_allowed():
    assert guard_command("git status") == (True, "")
    assert guard_command(["git", "log", "--oneline"]) == (True, "")


# ---------- post_write_check：落盘后自动校验 ----------

def test_post_write_check_good_plan(tmp_path):
    plan = tmp_path / "派工计划.yml"
    plan.write_text(GOOD_PLAN, encoding="utf-8")
    assert post_write_check(plan) == (True, [])


def test_post_write_check_bad_plan(tmp_path):
    plan = tmp_path / "派工计划.yml"
    plan.write_text(BAD_PLAN, encoding="utf-8")
    ok, problems = post_write_check(plan)
    assert ok is False
    assert any("越界" in p for p in problems)
    assert any("size" in p for p in problems)


def test_post_write_check_broken_yaml(tmp_path):
    plan = tmp_path / "派工计划.yml"
    plan.write_text("project: [未闭合", encoding="utf-8")
    ok, problems = post_write_check(plan)
    assert ok is False
    assert any("台账解析失败" in p for p in problems)


# ---------- stop_gate：收口门（临时仓真跑 pytest） ----------

def _make_repo(tmp_path: Path, test_body: str) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(test_body, encoding="utf-8")
    return tmp_path


def test_stop_gate_green_repo(tmp_path):
    repo = _make_repo(tmp_path, PASSING_TEST)
    log = EventLog(repo / "events.jsonl")
    log.append(TASK_CREATED, {"task_id": "T01"})
    log.append(WORKER_IDLE, {"worker": "w1"})
    assert stop_gate(repo) == (True, [])


def test_stop_gate_red_repo(tmp_path):
    repo = _make_repo(tmp_path, FAILING_TEST)
    ok, problems = stop_gate(repo)
    assert ok is False
    assert any("全量测试未通过" in p for p in problems)


def test_stop_gate_tampered_event_log(tmp_path):
    repo = _make_repo(tmp_path, PASSING_TEST)
    log = EventLog(repo / "events.jsonl")
    log.append(TASK_CREATED, {"task_id": "T01"})
    # 篡改第一条事件的 payload，哈希链应立刻断（canonical 序列化无空格冒号）
    raw = (repo / "events.jsonl").read_text(encoding="utf-8")
    (repo / "events.jsonl").write_text(
        raw.replace('"task_id":"T01"', '"task_id":"T99"'), encoding="utf-8"
    )
    ok, problems = stop_gate(repo)
    assert ok is False
    assert any("事件日志校验失败" in p for p in problems)


def test_stop_gate_no_event_log_is_ok(tmp_path):
    # 没有事件日志时 verify 容忍通过，测试绿就能收尾
    repo = _make_repo(tmp_path, PASSING_TEST)
    assert stop_gate(repo) == (True, [])
