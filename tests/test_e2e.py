"""端到端生命周期测试：台账 → 投影 → 事件派发 → 红绿 → 卡住 → 督办 → 拍板闭环。

对应 design/02-架构设计.md"数据流：一个任务的完整旅程"。三个测试函数按
文件顺序分段跑同一条生命线，共用 module 级 world fixture（tmp 目录）：
  1. 台账入库 + fire_pending 事件驱动派发，T-A 推到 in_progress
  2. T-A 一红一绿走到 done（绿点写回 status.yml）；T-B 两红无研究引用卡住
  3. 督办拆出复现微任务 T-C 也卡住凑满阈值 → METRICS_STALL + 五要素报告
     → 决策面 POST /decide 拍板 → EXPERT_DECIDED → 哈希链最终校验
注意：分段共享前置状态，单独跑后一段会因缺前置而失败，属预期。
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
FAKE_SHA = "f1" * 20  # FakeGit 解析出的假绿点

PLAN_YAML = f"""\
project: {PROJECT}
kind: 端到端演练
version: "1"
tasks:
  - id: T-A
    title: 修余额扣减
    status: draft
  - id: T-B
    title: 接入对账单
    status: draft
    depends_on: [T-A]
"""

# 每轮红回归回喂的失败日志（3 行：进升级报告的"失败用例/已试方向"）
RED_LOG = "\n".join([
    "FAILED tests/test_pay.py::test_deduct - AssertionError: 余额扣减错误",
    "AssertionError: assert 100 == 90",
    "===== 1 failed in 0.3s =====",
])


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """一条生命线共用的世界：临时台账 + 投影库 + 事件日志。"""
    root = tmp_path_factory.mktemp("e2e-lifecycle")
    plan_path = root / "派工计划.yml"
    plan_path.write_text(PLAN_YAML, encoding="utf-8")
    return {
        "root": root,
        "plan_path": plan_path,
        "status_path": root / "status.yml",
        "proj": Projection(root / "proj.db", status_path=root / "status.yml"),
        "log": EventLog(root / "events.jsonl"),
    }


# ---- 派发器模拟：唯一写路径 = 状态机把关 → 改台账 → 刷新投影 -----------------


def _apply_status(world, task_id, new_status, *, via_event=None):
    """把任务迁到 new_status：先过状态机，再写台账（事实源），最后刷新投影。"""
    proj = world["proj"]
    cur = proj.get_task(task_id)["status"]
    if via_event:
        new_status, action = state.handle_event(via_event, cur)
        assert new_status, action  # handle_event 失败时把中文原因带出来
    assert state.can_transition(cur, new_status), state.illegal(cur, new_status)
    ledger.update_task_status(world["plan_path"], task_id, new_status)
    assert proj.refresh_from_ledger(world["plan_path"]) >= 1
    assert proj.get_task(task_id)["status"] == new_status


def _on_task_created(world, e):
    _apply_status(world, e["payload"]["task_id"], "ready", via_event=TASK_CREATED)


def _on_worker_idle(world, e):
    task_id = e["payload"]["task_id"]
    _apply_status(world, task_id, "assigned", via_event=WORKER_IDLE)
    # 派发器把任务交给引擎：assigned→in_progress 迁移表允许，但没有对应事件
    _apply_status(world, task_id, "in_progress")


def _dispatch(world, task_id):
    """WORKER_IDLE 入链，fire_pending 消费后任务进入 in_progress。"""
    world["log"].append(WORKER_IDLE, {"task_id": task_id})
    fired = world["proj"].fire_pending(
        world["log"], {WORKER_IDLE: lambda e: _on_worker_idle(world, e)}
    )
    assert fired, "WORKER_IDLE 事件应被派发器消费"


def _sync_regressions(world, task_id, runner):
    """派发器职责：把回归器每轮结果记进投影（红轮自动累计 red_count）。"""
    return [
        world["proj"].record_regression(task_id, i, r.green, r.commit_sha, r.log_tail)
        for i, r in enumerate(runner.results, start=1)
    ]


def _round_events(world, task_id):
    """该任务名下的红绿循环事件（按链上顺序），不含派发类事件。"""
    kinds = (ROUND_RED, ROUND_GREEN, TASK_STUCK)
    return [
        e
        for e in world["log"].events()
        if e["kind"] in kinds and e["payload"].get("task_id") == task_id
    ]


# ---- 第一段：台账入库 + 事件驱动派发 ----------------------------------------


def test_stage1_ledger_projection_and_dispatch(world):
    # 台账合法：两个任务、B 依赖 A，基础校验（id 唯一/依赖引用/状态枚举）通过
    plan = ledger.load_plan(world["plan_path"])
    ledger.validate_basic(plan)
    assert [t.id for t in plan.tasks] == ["T-A", "T-B"]
    assert plan.tasks[1].depends_on == ["T-A"]

    # 刷新投影：两任务入库，依赖以 JSON 副本落库
    assert world["proj"].refresh_from_ledger(world["plan_path"]) == 2
    assert world["proj"].get_task("T-B")["depends_on"] == '["T-A"]'

    # TASK_CREATED 双双入链，fire_pending 消费后 draft→ready
    world["log"].append(TASK_CREATED, {"task_id": "T-A", "title": "修余额扣减"})
    world["log"].append(TASK_CREATED, {"task_id": "T-B", "title": "接入对账单"})
    fired = world["proj"].fire_pending(
        world["log"], {TASK_CREATED: lambda e: _on_task_created(world, e)}
    )
    assert fired == [1, 2]
    assert world["proj"].get_task("T-A")["status"] == "ready"
    assert world["proj"].get_task("T-B")["status"] == "ready"

    # 派发只给 T-A：ready→assigned→in_progress；T-B 依赖未完成，留在 ready
    _dispatch(world, "T-A")
    assert world["proj"].get_task("T-A")["status"] == "in_progress"
    assert world["proj"].get_task("T-B")["status"] == "ready"


# ---- 第二段：T-A 一红一绿到 done；T-B 两败卡住 -------------------------------


def test_stage2_redgreen_green_for_a_and_stuck_for_b(world):
    log, proj = world["log"], world["proj"]

    # T-A：第 1 轮红（回滚+失败日志回喂），第 2 轮带策略引用后绿
    git_a = FakeGit(FAKE_SHA)
    runner_a = ScriptedRunner([False, True], log_tail=RED_LOG)
    engine_a = EchoEngine(["直接实现扣减", "引用研究 R-1 改整数分后修复"])
    result = RedGreenLoop(engine_a, runner_a, git_a, log).round(
        TaskSpec(
            id="T-A",
            module=MODULE,
            base="main",
            title="修余额扣减",
            research_notes="研究 R-1：金额必须用整数分参与运算",
            strategy="按研究 R-1 改为整数分比较",
            strategy_changes=1,
        )
    )
    assert result.green is True and result.stuck is False
    assert result.attempts == 2

    # 事件序列 ROUND_RED → ROUND_GREEN，绿事件带回归器给的 commit
    events_a = _round_events(world, "T-A")
    assert [e["kind"] for e in events_a] == [ROUND_RED, ROUND_GREEN]
    assert events_a[0]["payload"]["attempt"] == 1
    assert events_a[1]["payload"]["attempt"] == 2
    assert events_a[1]["payload"]["commit_sha"] == runner_a.results[1].commit_sha

    # 纪律轨迹：绿点解析 → 从绿点开分支 → 第 1 轮红整体回滚，全程假 git
    assert git_a.calls == [
        ("rev-parse", "main"),
        ("checkout", "-B", "superdev/T-A", FAKE_SHA),
        ("reset", "--hard", FAKE_SHA),
    ]

    # 回归结果落投影：一红一绿，red_count 只累计一次
    _sync_regressions(world, "T-A", runner_a)
    assert proj.get_task("T-A")["red_count"] == 1
    rows = proj.conn.execute(
        "SELECT green FROM regression_events WHERE task_id = 'T-A' ORDER BY rowid"
    ).fetchall()
    assert [r["green"] for r in rows] == [0, 1]

    # 绿点写回 status.yml
    proj.set_last_green(MODULE, FAKE_SHA)
    status = ledger.load_status(world["status_path"])
    assert status["modules"][0] == {"repo": MODULE, "last_green": FAKE_SHA}

    # 收尾旅程：收证据 → 门禁 → 全绿合入 → done
    _apply_status(world, "T-A", "evidence_wait", via_event=ROUND_GREEN)
    _apply_status(world, "T-A", "gating")
    log.append(ALL_GREEN, {"task_id": "T-A", "commit_sha": FAKE_SHA})
    _apply_status(world, "T-A", "merged", via_event=ALL_GREEN)
    _apply_status(world, "T-A", "done")
    assert proj.get_task("T-A")["status"] == "done"

    # T-B：A 完成后派发，两红且策略从未引用研究 → 卡住
    _dispatch(world, "T-B")
    git_b = FakeGit(FAKE_SHA)
    runner_b = ScriptedRunner([False, False], log_tail=RED_LOG)
    stuck = RedGreenLoop(
        EchoEngine(["直接接入对账单", "照失败日志再试一次"]), runner_b, git_b, log
    ).round(TaskSpec(id="T-B", module=MODULE, base="main", title="接入对账单"))
    assert stuck.green is False and stuck.stuck is True
    assert stuck.reason == REASON_NO_CITATION and stuck.attempts == 2

    # 两败只发 TASK_STUCK，不发第二个 ROUND_RED
    events_b = _round_events(world, "T-B")
    assert [e["kind"] for e in events_b] == [ROUND_RED, TASK_STUCK]
    assert events_b[1]["payload"]["reason"] == REASON_NO_CITATION

    _sync_regressions(world, "T-B", runner_b)
    assert proj.get_task("T-B")["red_count"] == 2
    _apply_status(world, "T-B", "stuck", via_event=TASK_STUCK)
    assert proj.get_task("T-B")["status"] == "stuck"

    # 只有一个 stuck 未到阈值：不升级、不记事件
    before = len(log)
    assert supervise.maybe_escalate(proj, log, PROJECT, threshold=2) is None
    assert len(log) == before


# ---- 第三段：督办升级 → 决策面拍板闭环 → 哈希链校验 ---------------------------


def test_stage3_supervise_escalation_and_decision_close_loop(world):
    log, proj = world["log"], world["proj"]

    # 督办按设计"拆复现微任务"：T-C 追加进台账（事实源）并走完整派发
    plan = ledger.load_plan(world["plan_path"])
    plan.tasks.append(
        ledger.Task(id="T-C", title="复现 T-B 两红", status="draft", depends_on=["T-B"])
    )
    ledger.save_plan(plan, world["plan_path"])
    ledger.validate_basic(ledger.load_plan(world["plan_path"]))
    # 写路径按设计：程序改台账 → 刷新投影 → 插入事件 → 派发器消费
    assert proj.refresh_from_ledger(world["plan_path"]) == 3
    log.append(TASK_CREATED, {"task_id": "T-C", "title": "复现 T-B 两红"})
    proj.fire_pending(
        log, {TASK_CREATED: lambda e: _on_task_created(world, e)}
    )
    _dispatch(world, "T-C")

    runner_c = ScriptedRunner([False, False], log_tail=RED_LOG)
    stuck_c = RedGreenLoop(
        EchoEngine(["复现第一次", "复现第二次"]), runner_c, FakeGit(FAKE_SHA), log
    ).round(TaskSpec(id="T-C", module=MODULE, base="main", title="复现 T-B 两红"))
    assert stuck_c.stuck is True
    _sync_regressions(world, "T-C", runner_c)
    _apply_status(world, "T-C", "stuck", via_event=TASK_STUCK)

    # 记一条成本指标，让升级报告"已耗成本"有内容
    supervise.record_metric(
        proj, PROJECT, done_count=1, worker_hours=3.5, cost_by_model='{"glm": 12.5}'
    )

    # stuck 任务数达阈值 2：记 METRICS_STALL 并产出五要素升级报告
    report = supervise.maybe_escalate(proj, log, PROJECT, threshold=2)
    assert report
    stall = log.events()[-1]
    assert stall["kind"] == METRICS_STALL
    assert stall["payload"] == {"project": PROJECT, "stuck_tasks": 2}
    for kw in ("失败用例", "复现命令", "已试方向", "已耗成本", "建议选项", "拆微任务", "T-B"):
        assert kw in report
    assert "3.50" in report  # worker_hours 合计

    # 决策闭环：waiting 决策 → 起决策面（随机端口）→ POST /decide 拍板"拆微任务"
    proj.add_decision(
        "D-B", PROJECT, "T-B 两红卡住，下一步怎么走？", context_report=report
    )
    web = DecisionWeb(proj, log, world["plan_path"])
    server = web.serve(port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # 看板数据反映整条生命线：T-A 已 done 且红 1 次，T-B 卡住且红 2 次
        with urllib.request.urlopen(base + "/tasks", timeout=5) as resp:
            tasks = {t["id"]: t for t in json.loads(resp.read().decode("utf-8"))}
        assert tasks["T-A"]["status"] == "done" and tasks["T-A"]["red_count"] == 1
        assert tasks["T-B"]["status"] == "stuck" and tasks["T-B"]["red_count"] == 2

        req = urllib.request.Request(
            base + "/decide",
            data=json.dumps(
                {"decision_id": "D-B", "choice": "拆微任务", "note": "先复现再拆"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True and body["action"] == "拆微任务"
    finally:
        server.shutdown()
        server.server_close()

    # 拍板落库：decisions 翻 decided，choice 记进 decided_by，waiting 清空
    row = proj.conn.execute(
        "SELECT * FROM decisions WHERE decision_id = 'D-B'"
    ).fetchone()
    assert row["status"] == "decided" and row["decided_by"] == "拆微任务"
    assert proj.pending_decisions() == []

    # EXPERT_DECIDED 入链
    decided = log.events()[-1]
    assert decided["kind"] == EXPERT_DECIDED
    assert decided["payload"]["choice"] == "拆微任务"
    assert decided["payload"]["note"] == "先复现再拆"

    # 拍板生效回写生命线：T-B stuck→escalated，专家已定 → ready 重派
    _apply_status(world, "T-B", "escalated")
    _apply_status(world, "T-B", "ready", via_event=EXPERT_DECIDED)
    assert proj.get_task("T-B")["status"] == "ready"

    # 哈希链自始至终完整
    assert log.verify() == (True, 0)
