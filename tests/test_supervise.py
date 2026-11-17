"""Tests for superdev.supervise: metric recording, stall detection, the five-element
escalation report, threshold escalation (escalate on two failures), and automatic
recovery. Design source: design/03-detailed-design.md §3.3/§6."""

from datetime import datetime

import pytest

from superdev import supervise
from superdev.events import METRICS_STALL, EventLog
from superdev.ledger import Task
from superdev.projection import Projection

PROJECT = "demo"


@pytest.fixture()
def proj(tmp_path):
    p = Projection(tmp_path / "proj.db")
    yield p
    p.close()


@pytest.fixture()
def log(tmp_path):
    return EventLog(tmp_path / "events.jsonl")


def _metric(p, ts, done_count, worker_hours=0.0, cost_by_model="{}", project=PROJECT):
    """A metric row with an explicit timestamp (record_metric only writes now; tests need to wind the clock back)."""
    p.conn.execute(
        "INSERT INTO metrics (project, ts, done_count, worker_hours, cost_by_model)"
        " VALUES (?, ?, ?, ?, ?)",
        (project, ts, done_count, worker_hours, cost_by_model),
    )
    p.conn.commit()


def _task(p, task_id, status="in_progress", project=PROJECT):
    p.conn.execute(
        "INSERT INTO tasks (task_id, project, status) VALUES (?, ?, ?)",
        (task_id, project, status),
    )
    p.conn.commit()


def _red(p, task_id, log_tail, attempt=1):
    p.record_regression(
        task_id, attempt, green=False, commit_sha="deadbeef", log_tail=log_tail
    )


class TestRecordMetric:
    def test_writes_row_with_timestamp(self, proj):
        supervise.record_metric(proj, PROJECT, 5, 1.5, '{"glm": 2.0}')
        row = proj.conn.execute("SELECT * FROM metrics").fetchone()
        assert row["project"] == PROJECT
        assert row["done_count"] == 5
        assert row["worker_hours"] == 1.5
        assert row["cost_by_model"] == '{"glm": 2.0}'
        datetime.fromisoformat(row["ts"])  # timestamp parseable; passing means no raise

    def test_defaults(self, proj):
        supervise.record_metric(proj, PROJECT, 3)
        row = proj.conn.execute("SELECT * FROM metrics").fetchone()
        assert row["worker_hours"] == 0.0
        assert row["cost_by_model"] == "{}"

    def test_multiple_rows(self, proj):
        supervise.record_metric(proj, PROJECT, 1)
        supervise.record_metric(proj, PROJECT, 2)
        n = proj.conn.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()["c"]
        assert n == 2


class TestStalled:
    def test_no_growth_in_window_stalled(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        _metric(proj, "2026-11-16T10:20:00+00:00", 5)
        assert supervise.stalled(proj, PROJECT) is True

    def test_growth_in_window_not_stalled(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        _metric(proj, "2026-11-16T10:20:00+00:00", 7)
        assert supervise.stalled(proj, PROJECT) is False

    def test_no_growth_outside_window_not_stalled(self, proj):
        # two rows 50 minutes apart: no comparable point inside the window, so no stall verdict
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        _metric(proj, "2026-11-16T10:50:00+00:00", 5)
        assert supervise.stalled(proj, PROJECT) is False

    def test_custom_window(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        _metric(proj, "2026-11-16T10:20:00+00:00", 5)
        assert supervise.stalled(proj, PROJECT, window_minutes=15) is False
        assert supervise.stalled(proj, PROJECT, window_minutes=30) is True

    def test_single_metric_not_stalled(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        assert supervise.stalled(proj, PROJECT) is False

    def test_other_project_ignored(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5, project="other")
        _metric(proj, "2026-11-16T10:20:00+00:00", 5, project="other")
        assert supervise.stalled(proj, PROJECT) is False


class TestEscalationReport:
    @pytest.fixture()
    def rich(self, proj):
        """Project data with two tasks, several reds, and two metric rows."""
        _task(proj, "T01")
        _task(proj, "T02")
        _red(proj, "T02", "E1\nE2\nE3", attempt=1)  # older red recorded first
        _red(proj, "T01", "F1\nF2\nF3\nF4\nF5\nF6\nF7\nF8", attempt=1)  # latest red
        _metric(
            proj, "2026-11-16T10:00:00+00:00", 5, 1.5, '{"glm": 1.5}'
        )
        _metric(
            proj, "2026-11-16T10:30:00+00:00", 7, 2.5, '{"glm": 2.5, "sonnet": 1.0}'
        )
        return proj

    def test_five_elements_and_options(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        for kw in ("Failed case", "Repro", "Directions tried", "Cost", "Options"):
            assert kw in report
        for opt in supervise.OPTIONS:  # Change strategy / Split into microtasks / Switch engine / Abandon
            assert opt in report

    def test_failed_case_is_head_five_lines(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        section = report.split("2. Repro")[0]  # only the first element's section
        assert "F1" in section and "F5" in section
        assert "F6" not in section and "F8" not in section

    def test_repro_command_placeholder(self, rich, log):
        # no tasks passed (legacy call style): the repro command falls back to the placeholder wording
        report = supervise.escalation_report(rich, log, PROJECT)
        assert "Repro" in report
        assert supervise.REPRO_FALLBACK in report

    def test_tried_directions_with_red_counts(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        assert "T01" in report and "red 1 time" in report
        assert "T02" in report and "E3" in report  # the last 3 log lines make it into the report

    def test_cost_aggregated_by_model(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        assert "worker_hours total 4.00" in report
        assert "glm=4.00" in report
        assert "sonnet=1.00" in report

    def test_empty_project_still_five_elements(self, proj, log):
        report = supervise.escalation_report(proj, log, PROJECT)
        for kw in ("Failed case", "Repro", "Directions tried", "Cost", "Options"):
            assert kw in report
        assert "(no " in report  # placeholder sections still rendered


def _ltask(tid, status="in_progress", files=None, depends_on=None):
    """A ledger Task object for tests."""
    return Task(
        id=tid,
        title=tid,
        status=status,
        files=list(files or []),
        depends_on=list(depends_on or []),
    )


class TestReproCommand:
    @pytest.fixture()
    def stuck_proj(self, proj):
        _task(proj, "T01", status="stuck")
        _red(proj, "T01", "boom")
        return proj

    def test_repro_task_linked_by_depends_on(self, stuck_proj, log):
        # the stuck task's depends_on carries a repro: microtask -> take its files[0] to build the command
        tasks = [
            _ltask("T01", status="stuck", depends_on=["repro:case-9"]),
            _ltask("repro:case-9", files=["scripts/repro_case9.py"]),
        ]
        report = supervise.escalation_report(stuck_proj, log, PROJECT, tasks=tasks)
        assert "python scripts/repro_case9.py" in report
        assert supervise.REPRO_FALLBACK not in report

    def test_stuck_task_itself_is_repro(self, stuck_proj, log):
        # the stuck task is itself a repro: microtask -> take its own files[0] directly
        tasks = [_ltask("repro:case-1", status="stuck", files=["repro_case1.py"])]
        report = supervise.escalation_report(stuck_proj, log, PROJECT, tasks=tasks)
        assert "python repro_case1.py" in report

    def test_no_repro_falls_back(self, stuck_proj, log):
        # the stuck task has no linked repro microtask -> fallback wording;
        # a repro dependency on a non-stuck task does not count
        tasks = [
            _ltask("T01", status="stuck"),
            _ltask("T02", depends_on=["repro:case-2"]),
            _ltask("repro:case-2", files=["r2.py"]),
        ]
        report = supervise.escalation_report(stuck_proj, log, PROJECT, tasks=tasks)
        assert supervise.REPRO_FALLBACK in report
        assert "python r2.py" not in report


class TestMaybeEscalate:
    def test_below_threshold_returns_none(self, proj, log):
        _task(proj, "T01", status="stuck")
        assert supervise.maybe_escalate(proj, log, PROJECT) is None
        assert len(log) == 0  # below threshold: the event log stays untouched

    def test_at_threshold_records_event_and_reports(self, proj, log):
        _task(proj, "T01", status="stuck")
        _task(proj, "T02", status="stuck")
        result = supervise.maybe_escalate(proj, log, PROJECT)
        assert isinstance(result, str) and "Options" in result
        events = log.events()
        assert len(events) == 1
        assert events[0]["kind"] == METRICS_STALL
        assert events[0]["payload"]["project"] == PROJECT
        assert events[0]["payload"]["stuck_tasks"] == 2

    def test_threshold_param(self, proj, log):
        _task(proj, "T01", status="stuck")
        assert supervise.maybe_escalate(proj, log, PROJECT, threshold=3) is None
        assert supervise.maybe_escalate(proj, log, PROJECT, threshold=1) is not None

    def test_stuck_of_other_project_not_counted(self, proj, log):
        _task(proj, "X1", status="stuck", project="other")
        _task(proj, "X2", status="stuck", project="other")
        assert supervise.maybe_escalate(proj, log, PROJECT) is None


class TestAutoResume:
    def test_growth_resumes(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        _metric(proj, "2026-11-16T10:30:00+00:00", 7)
        assert supervise.auto_resume(proj, PROJECT) is True

    def test_flat_not_resumes(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        _metric(proj, "2026-11-16T10:30:00+00:00", 5)
        assert supervise.auto_resume(proj, PROJECT) is False

    def test_drop_not_resumes(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 7)
        _metric(proj, "2026-11-16T10:30:00+00:00", 5)
        assert supervise.auto_resume(proj, PROJECT) is False

    def test_single_metric_not_resumes(self, proj):
        _metric(proj, "2026-11-16T10:00:00+00:00", 5)
        assert supervise.auto_resume(proj, PROJECT) is False
