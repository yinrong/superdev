"""Tests for hooks.py: each of the four hook rulings on both the pass and deny path; stop_gate really runs pytest in a temp repo."""
import os
from pathlib import Path

import pytest

from superdev.events import TASK_CREATED, WORKER_IDLE, EventLog
from superdev.hooks import guard_command, guard_write, post_write_check, stop_gate

GOOD_PLAN = """\
project: hookdemo
tasks:
  - id: T01
    title: demo task
    status: ready
    files: [superdev/a.py]
    size: small
"""

BAD_PLAN = """\
project: hookdemo
tasks:
  - id: T01
    title: demo task
    status: ready
    files: [docs/design.md]
"""

PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING_TEST = "def test_bad():\n    assert False\n"


# ---------- guard_write: out-of-bounds writes ----------

def test_guard_write_inside_root_allowed(tmp_path):
    root = tmp_path / "ledger"
    root.mkdir()
    assert guard_write(root / "plan.yml", root) == (True, "")


def test_guard_write_prefix_sibling_denied(tmp_path):
    # ledger2 merely shares a name prefix; it is not a subpath of ledger and must be blocked
    root = tmp_path / "ledger"
    root.mkdir()
    (tmp_path / "ledger2").mkdir()
    ok, msg = guard_write(tmp_path / "ledger2" / "plan.yml", root)
    assert ok is False
    assert msg == "managers may only write the ledger"


def test_guard_write_other_dir_denied(tmp_path):
    root = tmp_path / "ledger"
    root.mkdir()
    ok, msg = guard_write(tmp_path / "elsewhere" / "x.yml", root)
    assert ok is False
    assert "managers may only write the ledger" in msg


def test_guard_write_relative_paths_use_abspath(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ok, _ = guard_write("ledger/a.yml", "ledger")
    assert ok is True


@pytest.mark.skipif(os.name != "nt", reason="cross-drive comparison only makes sense on Windows")
def test_guard_write_different_drive_denied(tmp_path):
    other = "Q:\\" if tmp_path.drive != "Q:" else "X:\\"
    ok, msg = guard_write(f"{tmp_path.drive}\\somewhere\\a.yml", other)
    assert ok is False
    assert msg == "managers may only write the ledger"


# ---------- guard_command: commit/push interception ----------

def test_guard_command_commit_string_denied():
    assert guard_command("git commit -m update ledger") == (
        False,
        "managers do not deliver on workers' behalf",
    )


def test_guard_command_push_list_denied():
    assert guard_command(["git", "push", "origin", "main"]) == (
        False,
        "managers do not deliver on workers' behalf",
    )


def test_guard_command_git_with_option_denied():
    # git -C <path> commit: after skipping the option and its value, commit must still be recognized
    assert guard_command("git -C ../repo commit -m x") == (
        False,
        "managers do not deliver on workers' behalf",
    )


def test_guard_command_normal_allowed():
    assert guard_command(["python", "-m", "pytest", "-q"]) == (True, "")


def test_guard_command_readonly_git_allowed():
    assert guard_command("git status") == (True, "")
    assert guard_command(["git", "log", "--oneline"]) == (True, "")


# ---------- post_write_check: automatic check after writing ----------

def test_post_write_check_good_plan(tmp_path):
    plan = tmp_path / "plan.yml"
    plan.write_text(GOOD_PLAN, encoding="utf-8")
    assert post_write_check(plan) == (True, [])


def test_post_write_check_bad_plan(tmp_path):
    plan = tmp_path / "plan.yml"
    plan.write_text(BAD_PLAN, encoding="utf-8")
    ok, problems = post_write_check(plan)
    assert ok is False
    assert any("out of bounds" in p for p in problems)
    assert any("size" in p for p in problems)


def test_post_write_check_broken_yaml(tmp_path):
    plan = tmp_path / "plan.yml"
    plan.write_text("project: [unclosed", encoding="utf-8")
    ok, problems = post_write_check(plan)
    assert ok is False
    assert any("ledger parse failed" in p for p in problems)


# ---------- stop_gate: close-out gate (really runs pytest in a temp repo) ----------

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
    assert any("full test run failed" in p for p in problems)


def test_stop_gate_tampered_event_log(tmp_path):
    repo = _make_repo(tmp_path, PASSING_TEST)
    log = EventLog(repo / "events.jsonl")
    log.append(TASK_CREATED, {"task_id": "T01"})
    # tamper with the first event's payload; the hash chain must break at once
    # (canonical serialization has no space after the colon)
    raw = (repo / "events.jsonl").read_text(encoding="utf-8")
    (repo / "events.jsonl").write_text(
        raw.replace('"task_id":"T01"', '"task_id":"T99"'), encoding="utf-8"
    )
    ok, problems = stop_gate(repo)
    assert ok is False
    assert any("event log verification failed" in p for p in problems)


def test_stop_gate_no_event_log_is_ok(tmp_path):
    # with no event log, verify tolerates it: green tests are enough to close out
    repo = _make_repo(tmp_path, PASSING_TEST)
    assert stop_gate(repo) == (True, [])
