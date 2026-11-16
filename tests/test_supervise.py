"""superdev.supervise 的测试：指标记录、停滞判定、五要素升级报告、
阈值升级（两败即升）与自动恢复。设计出处 design/03-详细设计.md §3.3/§6。"""

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
    """带指定时间戳的指标（record_metric 只写当下，测试要能回拨时钟）。"""
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
        datetime.fromisoformat(row["ts"])  # 时间戳可解析，不抛即过

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
        # 两条相隔 50 分钟：窗口内没有可比点，判不了停滞
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
        """两任务、多次红、两笔指标的项目数据。"""
        _task(proj, "T01")
        _task(proj, "T02")
        _red(proj, "T02", "E1\nE2\nE3", attempt=1)  # 先记旧的
        _red(proj, "T01", "F1\nF2\nF3\nF4\nF5\nF6\nF7\nF8", attempt=1)  # 最近的红
        _metric(
            proj, "2026-11-16T10:00:00+00:00", 5, 1.5, '{"glm": 1.5}'
        )
        _metric(
            proj, "2026-11-16T10:30:00+00:00", 7, 2.5, '{"glm": 2.5, "sonnet": 1.0}'
        )
        return proj

    def test_five_elements_and_options(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        for kw in ("失败用例", "复现命令", "已试方向", "已耗成本", "建议选项"):
            assert kw in report
        for opt in supervise.OPTIONS:  # 换策略/拆微任务/换引擎/放弃
            assert opt in report

    def test_failed_case_is_head_five_lines(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        section = report.split("二、复现命令")[0]  # 只看第一要素那一段
        assert "F1" in section and "F5" in section
        assert "F6" not in section and "F8" not in section

    def test_repro_command_placeholder(self, rich, log):
        # 不传 tasks（旧调用方式）：复现命令给降级文案，不再"待填"
        report = supervise.escalation_report(rich, log, PROJECT)
        assert "复现命令" in report
        assert supervise.REPRO_FALLBACK in report
        assert "待填" not in report

    def test_tried_directions_with_red_counts(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        assert "T01" in report and "red 1 次" in report
        assert "T02" in report and "E3" in report  # 日志尾 3 行入报告

    def test_cost_aggregated_by_model(self, rich, log):
        report = supervise.escalation_report(rich, log, PROJECT)
        assert "worker_hours 合计 4.00" in report
        assert "glm=4.00" in report
        assert "sonnet=1.00" in report

    def test_empty_project_still_five_elements(self, proj, log):
        report = supervise.escalation_report(proj, log, PROJECT)
        for kw in ("失败用例", "复现命令", "已试方向", "已耗成本", "建议选项"):
            assert kw in report
        assert "暂无" in report


def _ltask(tid, status="in_progress", files=None, depends_on=None):
    """测试用的台账 Task 对象。"""
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
        # stuck 任务的 depends_on 里挂着 repro: 微任务 → 取它的 files[0] 拼命令
        tasks = [
            _ltask("T01", status="stuck", depends_on=["repro:case-9"]),
            _ltask("repro:case-9", files=["scripts/repro_case9.py"]),
        ]
        report = supervise.escalation_report(stuck_proj, log, PROJECT, tasks=tasks)
        assert "python scripts/repro_case9.py" in report
        assert "待补" not in report

    def test_stuck_task_itself_is_repro(self, stuck_proj, log):
        # stuck 任务自身就是 repro: 微任务 → 直接取自己的 files[0]
        tasks = [_ltask("repro:case-1", status="stuck", files=["repro_case1.py"])]
        report = supervise.escalation_report(stuck_proj, log, PROJECT, tasks=tasks)
        assert "python repro_case1.py" in report

    def test_no_repro_falls_back(self, stuck_proj, log):
        # stuck 任务没有关联复现微任务 → 降级文案（与旧"待填"区分）
        # 非 stuck 任务挂着 repro 依赖不算数
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
        assert len(log) == 0  # 未达阈值不动事件日志

    def test_at_threshold_records_event_and_reports(self, proj, log):
        _task(proj, "T01", status="stuck")
        _task(proj, "T02", status="stuck")
        result = supervise.maybe_escalate(proj, log, PROJECT)
        assert isinstance(result, str) and "建议选项" in result
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
