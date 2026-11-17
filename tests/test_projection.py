"""Tests for superdev.projection: five-table schema, ledger refresh,
regression records, the decision loop, and fire_pending's firing order and idempotence."""

from pathlib import Path

import pytest
import yaml

from superdev import events as ev
from superdev import ledger
from superdev.events import EventLog
from superdev.projection import TABLES, Projection

FIXTURES = Path(__file__).parent / "fixtures" / "ledger"
FIXTURE_PLAN = FIXTURES / "plan_valid.yml"  # 3 tasks: T01 draft, T02/T03 ready


@pytest.fixture()
def proj(tmp_path):
    """A per-case projection database + status.yml path; closes the connection afterwards."""
    p = Projection(
        tmp_path / "proj.db",
        status_path=tmp_path / "status.yml",
    )
    yield p
    p.close()


def _write_status(path, modules):
    data = {"project": "demo", "modules": modules, "metrics": []}
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)
    return data


class TestSchema:
    def test_creates_five_tables(self, tmp_path):
        with Projection(tmp_path / "p.db") as p:
            names = {
                r["name"]
                for r in p.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert set(TABLES) <= names

    def test_reopen_is_idempotent(self, tmp_path):
        # Reopening the same database hits IF NOT EXISTS: no error, no rebuild
        db = tmp_path / "p.db"
        with Projection(db) as p:
            p.add_decision("D1", "demo", "Pause or not?")
        with Projection(db) as p:
            assert len(p.pending_decisions()) == 1


class TestRefreshFromLedger:
    def test_upserts_all_tasks(self, proj):
        n = proj.refresh_from_ledger(FIXTURE_PLAN)
        assert n == 3
        rows = proj.conn.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()
        assert [r["task_id"] for r in rows] == ["T01", "T02", "T03"]
        assert [r["status"] for r in rows] == ["draft", "ready", "ready"]
        assert all(r["project"] == "demo" for r in rows)

    def test_depends_on_stored_as_json(self, proj):
        proj.refresh_from_ledger(FIXTURE_PLAN)
        t02 = proj.get_task("T02")
        assert t02["depends_on"] == '["T01"]'
        assert proj.get_task("T03")["depends_on"] == "[]"  # defaulted deps land too

    def test_rerun_is_upsert_not_duplicate(self, proj):
        assert proj.refresh_from_ledger(FIXTURE_PLAN) == 3
        assert proj.refresh_from_ledger(FIXTURE_PLAN) == 3
        assert proj.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3

    def test_status_change_propagates_and_runtime_fields_survive(self, proj, tmp_path):
        # Refresh after a ledger status change: status follows; red_count,
        # maintained outside refresh, is not wiped
        plan_copy = tmp_path / "plan.yml"
        plan = ledger.load_plan(FIXTURE_PLAN)
        ledger.save_plan(plan, plan_copy)
        proj.refresh_from_ledger(plan_copy)
        proj.conn.execute("UPDATE tasks SET red_count = 2 WHERE task_id = 'T01'")
        proj.conn.commit()

        ledger.update_task_status(plan_copy, "T01", "ready")
        proj.refresh_from_ledger(plan_copy)

        t01 = proj.get_task("T01")
        assert t01["status"] == "ready"
        assert t01["red_count"] == 2

    def test_bad_ledger_raises(self, proj, tmp_path):
        bad = tmp_path / "bad.yml"
        bad.write_text("tasks: []\n", encoding="utf-8")  # missing project
        with pytest.raises(ledger.LedgerError):
            proj.refresh_from_ledger(bad)


class TestRegression:
    def test_green_then_red_rows(self, proj):
        proj.refresh_from_ledger(FIXTURE_PLAN)
        proj.record_regression("T01", attempt=1, green=False, commit_sha="s1", log_tail="E")
        proj.record_regression("T01", attempt=2, green=True, commit_sha="s2", log_tail="ok")
        rows = proj.conn.execute(
            "SELECT * FROM regression_events ORDER BY rowid"
        ).fetchall()
        assert [(r["attempt"], r["green"]) for r in rows] == [(1, 0), (2, 1)]
        assert proj.get_task("T01")["red_count"] == 1  # only red rounds count

    def test_unknown_task_still_recorded(self, proj):
        rid = proj.record_regression("T99", attempt=1, green=False, commit_sha="s", log_tail="")
        assert rid is not None
        n = proj.conn.execute(
            "SELECT COUNT(*) FROM regression_events WHERE task_id='T99'"
        ).fetchone()[0]
        assert n == 1  # recorded even before the ledger refresh; only the counter is skipped


class TestSetLastGreen:
    def test_updates_existing_module(self, proj, tmp_path):
        _write_status(
            proj.status_path,
            [{"repo": "D:/demo", "last_green": None, "max_workers": 2}],
        )
        proj.set_last_green("D:/demo", "abc123")
        data = ledger.load_status(proj.status_path)
        assert data["modules"][0]["last_green"] == "abc123"
        assert data["modules"][0]["max_workers"] == 2  # sibling fields untouched
        assert data["project"] == "demo"

    def test_creates_file_and_entry_when_missing(self, proj):
        assert not proj.status_path.exists()
        proj.set_last_green("D:/demo", "sha1")
        data = ledger.load_status(proj.status_path)
        assert data["modules"] == [{"repo": "D:/demo", "last_green": "sha1"}]

    def test_without_status_path_raises(self, tmp_path):
        with Projection(tmp_path / "p.db") as p:
            with pytest.raises(ValueError):
                p.set_last_green("D:/demo", "sha")


class TestDecisions:
    def test_full_loop_waiting_to_decided(self, proj):
        proj.add_decision("D1", "demo", "Stuck: escalate or split?", context_report="tried A/B")
        proj.add_decision("D2", "demo", "Which commit to roll back?")
        pendings = proj.pending_decisions()
        assert [d["decision_id"] for d in pendings] == ["D1", "D2"]
        assert all(d["status"] == "waiting" for d in pendings)

        result = proj.decide("D1", choice="escalate", note="both directions tried")
        assert result == {"decision_id": "D1", "action": "escalate", "note": "both directions tried"}
        assert [d["decision_id"] for d in proj.pending_decisions()] == ["D2"]

        row = proj.conn.execute(
            "SELECT * FROM decisions WHERE decision_id='D1'"
        ).fetchone()
        assert row["status"] == "decided"
        assert row["decided_by"] == "escalate"
        assert row["decided_at"] is not None

    def test_decide_twice_raises(self, proj):
        proj.add_decision("D1", "demo", "q")
        proj.decide("D1", choice="escalate")
        with pytest.raises(ValueError, match="decided"):
            proj.decide("D1", choice="roll back")

    def test_decide_unknown_raises(self, proj):
        with pytest.raises(LookupError):
            proj.decide("NOPE", choice="escalate")


class TestFirePending:
    def _log_with(self, tmp_path, kinds):
        log = EventLog(tmp_path / "events.jsonl")
        for k in kinds:
            log.append(k, {"task_id": "T01"})
        return log

    def test_fires_in_event_order(self, proj, tmp_path):
        log = self._log_with(tmp_path, [ev.ROUND_GREEN, ev.ALL_GREEN, ev.WORKER_IDLE])
        seen = []
        handlers = {k: (lambda e, k=k: seen.append(k)) for k in
                    [ev.ROUND_GREEN, ev.ALL_GREEN, ev.WORKER_IDLE]}
        fired = proj.fire_pending(log, handlers)
        assert fired == [1, 2, 3]
        assert seen == [ev.ROUND_GREEN, ev.ALL_GREEN, ev.WORKER_IDLE]  # strict event_id order
        assert log.unfired() == []

    def test_idempotent_second_call_fires_nothing(self, proj, tmp_path):
        log = self._log_with(tmp_path, [ev.ROUND_GREEN, ev.ALL_GREEN])
        calls = []
        handlers = {k: (lambda e, k=k: calls.append(k)) for k in
                    [ev.ROUND_GREEN, ev.ALL_GREEN]}
        assert proj.fire_pending(log, handlers) == [1, 2]
        assert proj.fire_pending(log, handlers) == []  # idempotent: no replay
        assert len(calls) == 2

    def test_missing_handler_leaves_event_pending(self, proj, tmp_path):
        log = self._log_with(tmp_path, [ev.ROUND_GREEN, ev.TASK_STUCK])
        fired = proj.fire_pending(log, {ev.ROUND_GREEN: lambda e: None})
        assert fired == [1]
        assert [e["kind"] for e in log.unfired()] == [ev.TASK_STUCK]  # waits for a handler
        # Consuming again after the handler is supplied still fires in the remaining order
        assert proj.fire_pending(log, {ev.TASK_STUCK: lambda e: None}) == [2]

    def test_handler_failure_keeps_event_unfired(self, proj, tmp_path):
        log = self._log_with(tmp_path, [ev.ROUND_GREEN, ev.ALL_GREEN])

        def boom(_e):
            raise RuntimeError("engine down")

        with pytest.raises(RuntimeError):
            proj.fire_pending(log, {ev.ROUND_GREEN: boom, ev.ALL_GREEN: boom})
        assert log.unfired()[0]["event_id"] == 1  # at-least-once: failures can retry
