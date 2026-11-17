"""End-to-end lifecycle test: ledger -> projection -> event dispatch -> red-green -> stuck -> supervision -> decision close loop.

Maps to "data flow: one task's complete journey" in design/02-architecture.md. The
three test functions run one shared lifeline in file order, sharing a
module-scoped world fixture (a tmp directory):
  1. ledger intake + fire_pending event-driven dispatch, T-A pushed to in_progress
  2. T-A goes one red then one green to done (green point written back to
     status.yml); T-B stuck with two reds and no research citation
  3. supervision splits out repro microtask T-C, which also sticks to reach the
     threshold -> METRICS_STALL + five-element report -> decision panel POST
     /decide -> EXPERT_DECIDED -> final hash-chain verification
Note: the stages share upstream state; running a later stage alone fails for
lack of prerequisites, which is expected.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from superdev import ledger, state, supervise
from superdev.engines import EchoEngine
from superdev.events import (
    ALL_GREEN,
    EXPERT_DECIDED,
    METRICS_STALL,
    ROUND_GREEN,
    ROUND_RED,
    TASK_CREATED,
    TASK_STUCK,
    WORKER_IDLE,
    EventLog,
)
from superdev.projection import Projection
from superdev.redgreen import REASON_NO_CITATION, RedGreenLoop, TaskSpec
from superdev.web import DecisionWeb

from fake_engine import FakeGit, ScriptedRunner

PROJECT = "e2e-demo"
MODULE = "demo/repo"
FAKE_SHA = "f1" * 20  # the fake green point FakeGit resolves to

PLAN_YAML = f"""\
project: {PROJECT}
kind: e2e rehearsal
version: "1"
tasks:
  - id: T-A
    title: fix balance deduction
    status: draft
  - id: T-B
    title: wire up the statement
    status: draft
    depends_on: [T-A]
"""

# Failure log fed back each red regression round (3 lines: feeds the report's
# "Failed case" / "Directions tried")
RED_LOG = "\n".join([
    "FAILED tests/test_pay.py::test_deduct - AssertionError: balance deduction wrong",
    "AssertionError: assert 100 == 90",
    "===== 1 failed in 0.3s =====",
])


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The world one lifeline shares: a temp ledger + projection db + event log."""
    root = tmp_path_factory.mktemp("e2e-lifecycle")
    plan_path = root / "dispatch-plan.yml"
    plan_path.write_text(PLAN_YAML, encoding="utf-8")
    return {
        "root": root,
        "plan_path": plan_path,
        "status_path": root / "status.yml",
        "proj": Projection(root / "proj.db", status_path=root / "status.yml"),
        "log": EventLog(root / "events.jsonl"),
    }


# ---- Dispatcher simulation: the only write path = state machine gate -> ledger write -> projection refresh ----


def _apply_status(world, task_id, new_status, *, via_event=None):
    """Move the task to new_status: through the state machine first, then the ledger write (source of truth), then the projection refresh."""
    proj = world["proj"]
    cur = proj.get_task(task_id)["status"]
    if via_event:
        new_status, action = state.handle_event(via_event, cur)
        assert new_status, action  # surface the failure reason when handle_event rejects
    assert state.can_transition(cur, new_status), state.illegal(cur, new_status)
    ledger.update_task_status(world["plan_path"], task_id, new_status)
    assert proj.refresh_from_ledger(world["plan_path"]) >= 1
    assert proj.get_task(task_id)["status"] == new_status


def _on_task_created(world, e):
    _apply_status(world, e["payload"]["task_id"], "ready", via_event=TASK_CREATED)


def _on_worker_idle(world, e):
    task_id = e["payload"]["task_id"]
    _apply_status(world, task_id, "assigned", via_event=WORKER_IDLE)
    # the dispatcher hands the task to the engine: assigned->in_progress is allowed
    # by the transition table, but has no dedicated event
    _apply_status(world, task_id, "in_progress")


def _dispatch(world, task_id):
    """WORKER_IDLE enters the chain; after fire_pending consumes it the task is in_progress."""
    world["log"].append(WORKER_IDLE, {"task_id": task_id})
    fired = world["proj"].fire_pending(
        world["log"], {WORKER_IDLE: lambda e: _on_worker_idle(world, e)}
    )
    assert fired, "the WORKER_IDLE event should have been consumed by the dispatcher"


def _sync_regressions(world, task_id, runner):
    """Dispatcher duty: record each of the runner's round results into the projection (red rounds auto-accumulate red_count)."""
    return [
        world["proj"].record_regression(task_id, i, r.green, r.commit_sha, r.log_tail)
        for i, r in enumerate(runner.results, start=1)
    ]


def _round_events(world, task_id):
    """The task's red-green loop events (in chain order), excluding dispatch-type events."""
    kinds = (ROUND_RED, ROUND_GREEN, TASK_STUCK)
    return [
        e
        for e in world["log"].events()
        if e["kind"] in kinds and e["payload"].get("task_id") == task_id
    ]


# ---- Stage 1: ledger intake + event-driven dispatch ------------------------------------


def test_stage1_ledger_projection_and_dispatch(world):
    # ledger is valid: two tasks, B depends on A; basic checks (id uniqueness /
    # dependency references / status enum) pass
    plan = ledger.load_plan(world["plan_path"])
    ledger.validate_basic(plan)
    assert [t.id for t in plan.tasks] == ["T-A", "T-B"]
    assert plan.tasks[1].depends_on == ["T-A"]

    # refresh the projection: both tasks land, dependencies stored as JSON copies
    assert world["proj"].refresh_from_ledger(world["plan_path"]) == 2
    assert world["proj"].get_task("T-B")["depends_on"] == '["T-A"]'

    # TASK_CREATED enters the chain for both; after fire_pending consumes them, draft->ready
    world["log"].append(TASK_CREATED, {"task_id": "T-A", "title": "fix balance deduction"})
    world["log"].append(TASK_CREATED, {"task_id": "T-B", "title": "wire up the statement"})
    fired = world["proj"].fire_pending(
        world["log"], {TASK_CREATED: lambda e: _on_task_created(world, e)}
    )
    assert fired == [1, 2]
    assert world["proj"].get_task("T-A")["status"] == "ready"
    assert world["proj"].get_task("T-B")["status"] == "ready"

    # dispatch goes to T-A only: ready->assigned->in_progress; T-B's dependency is
    # unfinished, so it stays ready
    _dispatch(world, "T-A")
    assert world["proj"].get_task("T-A")["status"] == "in_progress"
    assert world["proj"].get_task("T-B")["status"] == "ready"


# ---- Stage 2: T-A one red one green to done; T-B stuck on two reds -----------------------


def test_stage2_redgreen_green_for_a_and_stuck_for_b(world):
    log, proj = world["log"], world["proj"]

    # T-A: round 1 red (rollback + failure log fed back), round 2 green with a
    # research citation
    git_a = FakeGit(FAKE_SHA)
    runner_a = ScriptedRunner([False, True], log_tail=RED_LOG)
    engine_a = EchoEngine(["implement the deduction directly", "fix via integer cents citing research R-1"])
    result = RedGreenLoop(engine_a, runner_a, git_a, log).round(
        TaskSpec(
            id="T-A",
            module=MODULE,
            base="main",
            title="fix balance deduction",
            research_notes="research R-1: amounts must be computed in integer cents",
            strategy="switch to integer-cent comparison per research R-1",
            strategy_changes=1,
        )
    )
    assert result.green is True and result.stuck is False
    assert result.attempts == 2

    # event sequence ROUND_RED -> ROUND_GREEN; the green event carries the commit from the runner
    events_a = _round_events(world, "T-A")
    assert [e["kind"] for e in events_a] == [ROUND_RED, ROUND_GREEN]
    assert events_a[0]["payload"]["attempt"] == 1
    assert events_a[1]["payload"]["attempt"] == 2
    assert events_a[1]["payload"]["commit_sha"] == runner_a.results[1].commit_sha

    # discipline trail: green point resolved -> branch at the green point -> round
    # 1 red rolls all the way back; fake git throughout
    assert git_a.calls == [
        ("rev-parse", "main"),
        ("checkout", "-B", "superdev/T-A", FAKE_SHA),
        ("reset", "--hard", FAKE_SHA),
    ]

    # regression results land in the projection: one red one green, red_count
    # accumulated exactly once
    _sync_regressions(world, "T-A", runner_a)
    assert proj.get_task("T-A")["red_count"] == 1
    rows = proj.conn.execute(
        "SELECT green FROM regression_events WHERE task_id = 'T-A' ORDER BY rowid"
    ).fetchall()
    assert [r["green"] for r in rows] == [0, 1]

    # green point written back to status.yml
    proj.set_last_green(MODULE, FAKE_SHA)
    status = ledger.load_status(world["status_path"])
    assert status["modules"][0] == {"repo": MODULE, "last_green": FAKE_SHA}

    # closing the journey: gather evidence -> gate -> all-green merge -> done
    _apply_status(world, "T-A", "evidence_wait", via_event=ROUND_GREEN)
    _apply_status(world, "T-A", "gating")
    log.append(ALL_GREEN, {"task_id": "T-A", "commit_sha": FAKE_SHA})
    _apply_status(world, "T-A", "merged", via_event=ALL_GREEN)
    _apply_status(world, "T-A", "done")
    assert proj.get_task("T-A")["status"] == "done"

    # T-B: dispatched once A is done; two reds and the strategy never cites
    # research -> stuck
    _dispatch(world, "T-B")
    git_b = FakeGit(FAKE_SHA)
    runner_b = ScriptedRunner([False, False], log_tail=RED_LOG)
    stuck = RedGreenLoop(
        EchoEngine(["wire up the statement directly", "retry once more following the failure log"]), runner_b, git_b, log
    ).round(TaskSpec(id="T-B", module=MODULE, base="main", title="wire up the statement"))
    assert stuck.green is False and stuck.stuck is True
    assert stuck.reason == REASON_NO_CITATION and stuck.attempts == 2

    # two reds emit only TASK_STUCK, not a second ROUND_RED
    events_b = _round_events(world, "T-B")
    assert [e["kind"] for e in events_b] == [ROUND_RED, TASK_STUCK]
    assert events_b[1]["payload"]["reason"] == REASON_NO_CITATION

    _sync_regressions(world, "T-B", runner_b)
    assert proj.get_task("T-B")["red_count"] == 2
    _apply_status(world, "T-B", "stuck", via_event=TASK_STUCK)
    assert proj.get_task("T-B")["status"] == "stuck"

    # a single stuck task is below the threshold: no escalation, no event
    before = len(log)
    assert supervise.maybe_escalate(proj, log, PROJECT, threshold=2) is None
    assert len(log) == before


# ---- Stage 3: supervision escalation -> decision-panel close loop -> hash-chain check ----


def test_stage3_supervise_escalation_and_decision_close_loop(world):
    log, proj = world["log"], world["proj"]

    # supervision per the design "split out a repro microtask": T-C is appended to
    # the ledger (source of truth) and goes through the full dispatch
    plan = ledger.load_plan(world["plan_path"])
    plan.tasks.append(
        ledger.Task(id="T-C", title="reproduce T-B's two reds", status="draft", depends_on=["T-B"])
    )
    ledger.save_plan(plan, world["plan_path"])
    ledger.validate_basic(ledger.load_plan(world["plan_path"]))
    # the write path per design: program edits ledger -> refresh projection ->
    # insert event -> dispatcher consumes
    assert proj.refresh_from_ledger(world["plan_path"]) == 3
    log.append(TASK_CREATED, {"task_id": "T-C", "title": "reproduce T-B's two reds"})
    proj.fire_pending(
        log, {TASK_CREATED: lambda e: _on_task_created(world, e)}
    )
    _dispatch(world, "T-C")

    runner_c = ScriptedRunner([False, False], log_tail=RED_LOG)
    stuck_c = RedGreenLoop(
        EchoEngine(["reproduce round 1", "reproduce round 2"]), runner_c, FakeGit(FAKE_SHA), log
    ).round(TaskSpec(id="T-C", module=MODULE, base="main", title="reproduce T-B's two reds"))
    assert stuck_c.stuck is True
    _sync_regressions(world, "T-C", runner_c)
    _apply_status(world, "T-C", "stuck", via_event=TASK_STUCK)

    # record a cost metric so the report's "Cost" element has content
    supervise.record_metric(
        proj, PROJECT, done_count=1, worker_hours=3.5, cost_by_model='{"glm": 12.5}'
    )

    # stuck-task count reaches the threshold 2: record METRICS_STALL and produce
    # the five-element escalation report
    report = supervise.maybe_escalate(proj, log, PROJECT, threshold=2)
    assert report
    stall = log.events()[-1]
    assert stall["kind"] == METRICS_STALL
    assert stall["payload"] == {"project": PROJECT, "stuck_tasks": 2}
    for kw in ("Failed case", "Repro", "Directions tried", "Cost", "Options", "Split into microtasks", "T-B"):
        assert kw in report
    assert "3.50" in report  # worker_hours total

    # decision close loop: waiting decision -> start the decision panel (random
    # port) -> POST /decide signing off on "Split into microtasks"
    proj.add_decision(
        "D-B", PROJECT, "T-B is stuck after two reds; what is the next move?", context_report=report
    )
    web = DecisionWeb(proj, log, world["plan_path"])
    server = web.serve(port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # the board reflects the whole lifeline: T-A done with 1 red, T-B stuck with 2 reds
        with urllib.request.urlopen(base + "/tasks", timeout=5) as resp:
            tasks = {t["id"]: t for t in json.loads(resp.read().decode("utf-8"))}
        assert tasks["T-A"]["status"] == "done" and tasks["T-A"]["red_count"] == 1
        assert tasks["T-B"]["status"] == "stuck" and tasks["T-B"]["red_count"] == 2

        req = urllib.request.Request(
            base + "/decide",
            data=json.dumps(
                {"decision_id": "D-B", "choice": "Split into microtasks", "note": "reproduce first, then split"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True and body["action"] == "Split into microtasks"
    finally:
        server.shutdown()
        server.server_close()

    # the decision lands in the db: decisions flips to decided, choice recorded
    # in decided_by, waiting emptied
    row = proj.conn.execute(
        "SELECT * FROM decisions WHERE decision_id = 'D-B'"
    ).fetchone()
    assert row["status"] == "decided" and row["decided_by"] == "Split into microtasks"
    assert proj.pending_decisions() == []

    # EXPERT_DECIDED enters the chain
    decided = log.events()[-1]
    assert decided["kind"] == EXPERT_DECIDED
    assert decided["payload"]["choice"] == "Split into microtasks"
    assert decided["payload"]["note"] == "reproduce first, then split"

    # the decision writes back into the lifeline: T-B stuck->escalated, expert
    # has decided -> ready for redispatch
    _apply_status(world, "T-B", "escalated")
    _apply_status(world, "T-B", "ready", via_event=EXPERT_DECIDED)
    assert proj.get_task("T-B")["status"] == "ready"

    # the hash chain is intact from start to finish
    assert log.verify() == (True, 0)
