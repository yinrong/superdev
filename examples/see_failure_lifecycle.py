"""任务失败生命周期演示：两红换方向转绿、两红卡住升级、决策链完整性。

零依赖离线演示（对应详细设计 §1.1 红绿循环、§3 状态机、§8.3 事件链）：
不调 AI、不联网、不碰真 git，一切发生在 tempfile 临时目录里，秒级跑完。

运行：python examples/see_failure_lifecycle.py（退出码 0）

输出标签全部英文（[RED] [ROLLBACK] [RESEARCH] [VERIFY] 等），
旁白也是英文，纯英文读者零障碍；state.handle_event 返回的中文动作
文案用本地对照表 ACTION_EN 翻译，不修改 superdev 包本身。

三个假件是 tests/fake_engine.py 的自带轻量副本（避免 examples 去 import
tests 包）：DemoEngine 对应 EchoEngine、DemoRunner 对应 ScriptedRunner、
DemoGit 对应 FakeGit，语义相同，只多了每步一句英文旁白。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 脚本住在 examples/ 里，把仓库根目录挂上 sys.path 才能拿到本仓库的 superdev 包
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from superdev import state  # noqa: E402
from superdev.engines import RegressionResult  # noqa: E402
from superdev.events import (  # noqa: E402
    ALL_GREEN,
    EXPERT_DECIDED,
    ROUND_GREEN,
    TASK_CREATED,
    TASK_STUCK,
    WORKER_IDLE,
    EventLog,
)
from superdev.ledger import Plan, Task, save_plan, update_task_status  # noqa: E402
from superdev.redgreen import REASON_CITED, RedGreenLoop, TaskSpec  # noqa: E402
from superdev.research import KnowledgeBase, strategy_cites  # noqa: E402

# 两个模块各自的"上一次全绿"提交（假 sha，演示用）
GREEN_LOGIN = "1a2b3c4d" + "0" * 32
GREEN_API = "5e6f7a8b" + "0" * 32

# state.handle_event 返回的动作是中文（管理端口径），演示打印用这份英文对照
ACTION_EN = {
    TASK_CREATED: "checks passed, refresh projection",
    WORKER_IDLE: "dispatcher assigns",
    ROUND_GREEN: "collect evidence, notify gate",
    ALL_GREEN: "merge to main, refresh green base",
    TASK_STUCK: "trigger post-mortem research",
    EXPERT_DECIDED: "new strategy written back to ledger",
}


def say(tag: str, msg: str) -> None:
    """演示旁白：每步一行 [TAG] description。"""
    print(f"[{tag}] {msg}")


class DemoEngine:
    """EchoEngine 副本：按脚本出每轮产物摘要，顺带念一句旁白。"""

    def __init__(self, outputs: list[str]):
        self._pending = list(outputs)
        self.attempt = 0

    def run_round(self, task_desc: str, context: str) -> str:
        self.attempt += 1
        summary = self._pending.pop(0)
        say("ENGINE", f"Attempt {self.attempt}: {summary}")
        return summary


class DemoRunner:
    """ScriptedRunner 副本：按脚本判绿。绿红只由回归脚本说了算，工人不自报。"""

    def __init__(self, script: list[bool], log_tail: str):
        self._script = list(script)
        self._log_tail = log_tail
        self.n = 0

    def run(self, module: str) -> RegressionResult:
        self.n += 1
        green = self._script.pop(0)
        tag = "GREEN" if green else "RED"
        note = f"commit fake-{self.n:03d}" if green else "failure log tail fed back"
        say(tag, f"{module} regression verdict: {tag} ({note})")
        return RegressionResult(
            green=green, commit_sha=f"fake-{self.n:03d}", log_tail=self._log_tail
        )


class DemoGit:
    """FakeGit 副本：同名三方法，记录调用序列并念出纪律动作。"""

    def __init__(self, sha: str):
        self.sha = sha
        self.calls: list[tuple[str, ...]] = []

    def rev_parse(self, ref: str) -> str:
        self.calls.append(("rev-parse", ref))
        return self.sha

    def checkout_b(self, branch: str, base: str) -> str:
        self.calls.append(("checkout", "-B", branch, base))
        say("BRANCH", f"Branch {branch} cut from green base {base[:8]} (every round starts from green)")
        return ""

    def reset_hard(self, ref: str) -> str:
        self.calls.append(("reset", "--hard", ref))
        say("ROLLBACK", f"Red means rollback: hard reset to green base {ref[:8]}, failure log fed to next attempt")
        return ""


def dispatch_to_in_progress(log: EventLog, task_id: str, status: dict[str, str]) -> None:
    """派工小跑：WORKER_IDLE 派工 → assigned，再程序迁移进 in_progress。"""
    to, _ = state.handle_event(WORKER_IDLE, status[task_id])
    log.append(WORKER_IDLE, {"task_id": task_id})
    status[task_id] = to
    say(WORKER_IDLE, f"{task_id}: ready -> {to} ({ACTION_EN[WORKER_IDLE]})")
    if state.can_transition(to, "in_progress"):
        status[task_id] = "in_progress"
        say("START", f"{task_id}: {to} -> in_progress (program transition), entering the red-green loop with research notes")


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # 任何控制台编码都不许炸
    except AttributeError:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="superdev-demo-"))
    say("SETUP", f"Offline demo starts in temp dir {tmp} (no AI calls, no network, no real git)")

    log = EventLog(tmp / "events.jsonl")
    plan = Plan(
        project="demo-shop",
        kind="demo",
        version="t24-demo",
        tasks=[
            Task(id="fix-login-bug", title="fix duplicated login submits", status="draft", size="medium"),
            Task(id="refactor-api", title="refactor API error handling", status="draft", size="medium"),
        ],
    )
    plan_path = tmp / "plan.yml"
    save_plan(plan, plan_path)
    say("LEDGER", "plan.yml written with 2 tasks: " + ", ".join(t.id for t in plan.tasks))

    kb = KnowledgeBase(tmp / "kb.yml")
    kb.record("踩坑", "duplicate-submit", "duplicate submits under concurrent login; do the idempotency key before the lock")
    say("KB", "Module knowledge base records 1 pitfall: duplicate-submit -> idempotency key before the lock")

    status: dict[str, str] = {t.id: "draft" for t in plan.tasks}
    for t in plan.tasks:
        to, _ = state.handle_event(TASK_CREATED, status[t.id])
        log.append(TASK_CREATED, {"task_id": t.id, "title": t.title})
        status[t.id] = to
        say(TASK_CREATED, f"{t.id}: draft -> {to} ({ACTION_EN[TASK_CREATED]})")

    # ---- 任务一：两红后换方向（策略引用研究）→ 新回合转绿 -------------------
    print()
    print("--- Task 1 fix-login-bug: two reds, new cited direction, then green ---")
    spec1 = TaskSpec(
        id="fix-login-bug",
        module="login",
        base=GREEN_LOGIN,
        title="fix duplicated login submits",
        size="medium",
        research_notes="lock-contention: concurrent logins step on each other\n"
                       "idempotency-key: the submit entry needs dedupe by idempotency key",
        strategy="New direction: follow the 'duplicate-submit' pitfall - dedupe the submit entry first, then touch the lock",
        strategy_changes=1,
    )
    print()
    dispatch_to_in_progress(log, "fix-login-bug", status)

    engine1 = DemoEngine(
        ["patch password validation directly",
         "add a retry, same old path",
         "new direction: dedupe the submit entry first, then the lock"]
    )
    runner1 = DemoRunner(
        [False, False, True],
        log_tail="E   AssertionError: duplicate submit under concurrent login\n"
                 "E   assert response.status_code == 200",
    )
    git1 = DemoGit(GREEN_LOGIN)
    loop1 = RedGreenLoop(engine1, runner1, git1, log, knowledge_base=kb)

    print()
    say("ROUND 1", "Starting (max 2 attempts per round; the 2nd red forces a research verdict, never a 3rd try)")
    r1 = loop1.round(spec1)
    if r1.reason != REASON_CITED:
        raise SystemExit(f"Demo went off-script: round 1 expected {REASON_CITED!r}, got {r1.reason!r}")
    cited = strategy_cites(spec1.strategy, f"{spec1.research_notes}\n{kb.inject(spec1.size)}")
    say("RESEARCH", "2nd red triggers research: module-KB pitfalls injected; a direction change must cite research")
    say(
        "VERDICT",
        f"Strategy cites research: {'PASS' if cited else 'FAIL'}"
        " (keyword 'duplicate-submit' came from KB injection)"
        " -> citation accepted, dispatcher opens a new round",
    )

    print()
    spec1.strategy_changes += 1
    say("ROUND 2", "New direction, new round - still starts from the green base")
    r2 = loop1.round(spec1)
    if not r2.green:
        raise SystemExit("Demo went off-script: round 2 should turn green")
    to, _ = state.handle_event(ROUND_GREEN, status["fix-login-bug"])
    status["fix-login-bug"] = to
    say(ROUND_GREEN, f"fix-login-bug: in_progress -> {to} ({ACTION_EN[ROUND_GREEN]})")
    if state.can_transition(status["fix-login-bug"], "gating"):
        status["fix-login-bug"] = "gating"
        say("GATE", "evidence_wait -> gating (program transition): evidence collected, entering the gate")
    to, _ = state.handle_event(ALL_GREEN, status["fix-login-bug"])
    log.append(ALL_GREEN, {"task_id": "fix-login-bug", "commit_sha": "fake-003"})
    status["fix-login-bug"] = to
    say(ALL_GREEN, f"fix-login-bug: gating -> {to} ({ACTION_EN[ALL_GREEN]}) - even green must pass the gate to merge")

    # ---- 任务二：两红且不引用研究 → 卡住、升级、专家拍板 --------------------
    print()
    print("--- Task 2 refactor-api: two reds, no citation, stuck and escalated ---")
    spec2 = TaskSpec(
        id="refactor-api",
        module="api",
        base=GREEN_API,
        title="refactor API error handling",
        size="medium",
        research_notes="error-code: the error-code table is scattered across three modules",
        strategy="",  # 没有换方向，自然没有策略变更
        strategy_changes=0,
    )
    print()
    dispatch_to_in_progress(log, "refactor-api", status)

    engine2 = DemoEngine(["remap error codes by intuition", "try another file, direction unchanged"])
    runner2 = DemoRunner(
        [False, False], log_tail="E   AssertionError: error-code tables inconsistent in 3 places"
    )
    git2 = DemoGit(GREEN_API)
    loop2 = RedGreenLoop(engine2, runner2, git2, log, knowledge_base=kb)

    print()
    say("ROUND 1", "Starting")
    r3 = loop2.round(spec2)
    if not r3.stuck:
        raise SystemExit("Demo went off-script: task 2 should get stuck")
    say("RESEARCH", "2nd red research verdict: no strategy change this round (strategy_changes=0), nothing to cite")
    say("VERDICT", "Strategy change does not cite research -> no 3rd blind retry, stuck stops spinning")
    to, _ = state.handle_event(TASK_STUCK, status["refactor-api"])
    status["refactor-api"] = to
    say(TASK_STUCK, f"refactor-api: in_progress -> {to} ({ACTION_EN[TASK_STUCK]})")

    say("STATE MACHINE", "Stuck task jumps straight to the expert verdict? Refused: EXPERT_DECIDED requires state 'escalated', current state is 'stuck'")
    if state.can_transition(status["refactor-api"], "escalated"):
        status["refactor-api"] = "escalated"
        say("ESCALATE", "stuck -> escalated (can_transition says legal): human escalation, handed to the expert")
    to, _ = state.handle_event(EXPERT_DECIDED, status["refactor-api"])
    log.append(EXPERT_DECIDED, {"task_id": "refactor-api", "decision": "add a contract test that unifies the error-code table first"})
    status["refactor-api"] = to
    say(EXPERT_DECIDED, f"refactor-api: escalated -> {to} ({ACTION_EN[EXPERT_DECIDED]}): expert decides, task re-queued")

    update_task_status(plan_path, "refactor-api", status["refactor-api"])
    update_task_status(plan_path, "fix-login-bug", status["fix-login-bug"])
    say("LEDGER", "Statuses written back to plan.yml: fix-login-bug=merged, refactor-api=ready")

    # ---- 收尾：决策链完整性 --------------------------------------------------
    print()
    print("--- Wrap-up: decision-chain integrity ---")
    ok, first_bad = log.verify()
    chain = "intact - nobody can tamper with this decision chain" if ok else f"broken at event {first_bad}"
    say("VERIFY", f"EventLog.verify(): {len(log)} events, hash chain {chain} (ok={ok})")
    all_calls = git1.calls + git2.calls
    n_checkout = sum(1 for c in all_calls if c[0] == "checkout")
    n_reset = sum(1 for c in all_calls if c[0] == "reset")
    say("TRAIL", f"Git discipline trail: {n_checkout} branches cut from green, {n_reset} red rollbacks, no exceptions")

    print()
    print("Demo complete: green never merges without gating, stuck never spins idle (绿的不合入不存在，卡的不空转)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
