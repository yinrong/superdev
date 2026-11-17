"""Tests for superdev_cli.py: every subcommand really runs via subprocess, checking output and exit codes."""
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
    title: task one
    status: ready
    files: [superdev/a.py]
    size: small
  - id: T02
    title: task two
    status: in_progress
    files: [tests/test_a.py]
    size: medium
    depends_on: [T01]
"""

BAD_PLAN = """\
project: clidemo
tasks:
  - id: T01
    title: task one
    status: ready
    files: [docs/design.md]
"""


def run_cli(*args, cwd=None) -> subprocess.CompletedProcess:
    """Really run superdev_cli.py with the current interpreter; exit code and output come back as-is."""
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
    (proj / "plan.yml").write_text(plan_text, encoding="utf-8")
    return proj


# ---------- check ----------

def test_check_good_plan_exits_zero(tmp_path):
    proj = _project(tmp_path)
    r = run_cli("check", proj / "plan.yml")
    assert r.returncode == 0
    assert "validation passed" in r.stdout


def test_check_bad_plan_exits_one(tmp_path):
    proj = _project(tmp_path, BAD_PLAN)
    r = run_cli("check", proj / "plan.yml")
    assert r.returncode == 1
    assert "out of bounds" in r.stdout


def test_check_missing_plan_exits_one(tmp_path):
    r = run_cli("check", tmp_path / "nope.yml")
    assert r.returncode == 1
    assert "failed to load" in r.stderr


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
    assert "red 0" in r.stdout


# ---------- round ----------

def test_round_refreshes_projection_and_counts_pending(tmp_path):
    proj = _project(tmp_path)
    log = EventLog(proj / "events.jsonl")
    log.append(TASK_CREATED, {"task_id": "T01"})
    log.append(WORKER_IDLE, {"worker": "w1"})
    r = run_cli("round", proj)
    assert r.returncode == 0
    assert "projection refreshed: 2 tasks" in r.stdout
    assert "pending events: 2" in r.stdout
    assert (proj / "projection.db").exists()


def test_round_missing_plan_exits_one(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    r = run_cli("round", proj)
    assert r.returncode == 1
    assert "Plan file not found" in r.stderr


# ---------- verify ----------

def test_verify_intact_log_exits_zero(tmp_path):
    log_path = tmp_path / "events.jsonl"
    EventLog(log_path).append(TASK_CREATED, {"task_id": "T01"})
    r = run_cli("verify", log_path)
    assert r.returncode == 0
    assert "verification passed" in r.stdout


def test_verify_tampered_log_exits_one(tmp_path):
    log_path = tmp_path / "events.jsonl"
    EventLog(log_path).append(TASK_CREATED, {"task_id": "T01"})
    raw = log_path.read_text(encoding="utf-8")
    # canonical serialization has no space after the colon; replace without the
    # space or the tamper misses
    log_path.write_text(
        raw.replace('"task_id":"T01"', '"task_id":"T99"'), encoding="utf-8"
    )
    r = run_cli("verify", log_path)
    assert r.returncode == 1
    assert "verification failed" in r.stdout


# ---------- import-spec ----------

def _spec_dir(tmp_path: Path, plan_text: str) -> Path:
    spec = tmp_path / "feat"
    spec.mkdir()
    (spec / "plan.md").write_text(plan_text, encoding="utf-8")
    return spec


def test_import_spec_good_path_writes_plan_and_prints_warnings(tmp_path):
    spec = _spec_dir(
        tmp_path,
        "- [ ] Fix the README typo\n"
        "- [ ] Add a module filter arg to the ledger loader, with tests\n",
    )
    out = tmp_path / "feat-plan.yml"
    r = run_cli("import-spec", spec, out)
    assert r.returncode == 0
    assert "import complete: 2 tasks" in r.stdout
    # 2 tasks is below the recommended minimum of 10: the check warning must
    # still print while the exit code stays 0
    assert "check warnings: 1" in r.stdout
    assert "fewer than" in r.stdout
    assert out.exists()


def test_import_spec_over_limit_exits_one(tmp_path):
    spec = _spec_dir(tmp_path, "".join(f"- [ ] task {i}\n" for i in range(1, 5)))
    r = run_cli("import-spec", spec, tmp_path / "plan.yml", "--max-tasks", "3")
    assert r.returncode == 1
    assert "split into smaller tasks before importing" in r.stderr
    assert not (tmp_path / "plan.yml").exists()


def test_import_spec_missing_list_exits_one(tmp_path):
    empty = tmp_path / "empty-spec"
    empty.mkdir()
    r = run_cli("import-spec", empty, tmp_path / "plan.yml")
    assert r.returncode == 1
    assert "no plan.md or tasks.md" in r.stderr


# ---------- entry point ----------

def test_no_subcommand_prints_usage_exit_two():
    r = run_cli()
    assert r.returncode == 2
    assert "usage" in r.stderr.lower()
