"""红绿循环纪律脚本（详细设计 §1.1–§1.3）：从绿点开工、红即回滚、两败先研究再换方向。

引擎与回归按 engines 的协议注入，git 全部走 GitOps（测试用 dry_run + 假引擎，
全程不碰真 git）。事件写入 EventLog，绿点由调用方（派发器）从台账装配进 TaskSpec。
"""

from __future__ import annotations

from dataclasses import dataclass

from superdev.engines import Engine, GitOps, RegressionRunner
from superdev.events import ROUND_GREEN, ROUND_RED, TASK_STUCK, EventLog
from superdev.research import KnowledgeBase, strategy_cites

__all__ = [
    "MAX_ATTEMPTS",
    "LOG_TAIL_LINES",
    "REASON_NO_CITATION",
    "REASON_CITED",
    "TaskSpec",
    "RoundResult",
    "tail_lines",
    "RedGreenLoop",
]

# 同一回合内最多尝试 2 次；第 2 次红必转研究判定，不再第三次（§1.1.4）
MAX_ATTEMPTS = 2
# 失败日志回喂只保留最近 200 行（§1.1.3）
LOG_TAIL_LINES = 200

REASON_NO_CITATION = "策略变更未引用研究"
REASON_CITED = "策略变更已引用研究"


@dataclass
class TaskSpec:
    """任务进入红绿循环所需的一切（派发器从台账 + status.yml 装配）。"""

    id: str
    module: str
    base: str  # 模块 last_green（sha 或可解析 ref），空值直接拒绝开工
    title: str = ""
    size: str = "medium"  # 研究预算档位，用于知识库注入
    research_notes: str = ""  # 任务研究结论（§1.4）
    strategy: str = ""  # 本次的策略变更文本
    strategy_changes: int = 0  # 累计策略变更次数，0 表示从未换方向


@dataclass
class RoundResult:
    """一次 round() 的结局。

    green 与 stuck 均为 False 表示：新方向已引用研究，交给派发器
    另开一个回合再试（本回合内没有第三次尝试）。
    """

    green: bool = False
    stuck: bool = False
    reason: str = ""
    attempts: int = 0
    log_tail: str = ""


def tail_lines(text: str, limit: int = LOG_TAIL_LINES) -> str:
    """只保留最后 limit 行：失败日志的尾部才是最新、最有用的部分。"""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[-limit:])


class RedGreenLoop:
    """纪律脚本的一回合：绿点开分支 → 实现 → 判绿 → 红回滚回喂 / 两败研究判定。"""

    def __init__(
        self,
        engine: Engine,
        runner: RegressionRunner,
        git_ops: GitOps,
        event_log: EventLog,
        knowledge_base: KnowledgeBase | None = None,
    ):
        self.engine = engine
        self.runner = runner
        self.git = git_ops
        self.log = event_log
        self.kb = knowledge_base

    def round(self, task: TaskSpec) -> RoundResult:
        if not task.base:
            raise ValueError(f"任务 {task.id} 缺少绿点（base），拒绝开工")

        base = self.git.rev_parse(task.base)
        self.git.checkout_b(f"superdev/{task.id}", base)

        context = ""
        task_desc = f"{task.id} {task.title}".strip()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            summary = self.engine.run_round(task_desc, context)
            result = self.runner.run(task.module)
            tail = tail_lines(result.log_tail)

            if result.green:
                self.log.append(
                    ROUND_GREEN,
                    {
                        "task_id": task.id,
                        "attempt": attempt,
                        "commit_sha": result.commit_sha,
                        "summary": summary,
                    },
                )
                return RoundResult(green=True, attempts=attempt, log_tail=tail)

            if attempt < MAX_ATTEMPTS:
                # 第 1 次红：记事件、回滚绿点、失败日志（末 200 行）回喂下一轮
                self.log.append(
                    ROUND_RED,
                    {
                        "task_id": task.id,
                        "attempt": attempt,
                        "commit_sha": result.commit_sha,
                        "log_tail_lines": len(tail.splitlines()),
                    },
                )
                self.git.reset_hard(base)
                context = f"{context}\n{tail}" if context else tail
                continue

            # 第 2 次红：先注入模块知识库，再判定策略变更是否引用研究
            research_notes = task.research_notes
            if self.kb is not None:
                injected = self.kb.inject(task.size)
                if injected:
                    research_notes = (
                        f"{research_notes}\n{injected}" if research_notes else injected
                    )
            if not task.strategy_changes or not strategy_cites(
                task.strategy, research_notes
            ):
                self.log.append(
                    TASK_STUCK,
                    {"task_id": task.id, "attempt": attempt, "reason": REASON_NO_CITATION},
                )
                return RoundResult(
                    stuck=True, reason=REASON_NO_CITATION, attempts=attempt, log_tail=tail
                )

            # 引用过关：记红事件，交回派发器按新方向另开回合（本回合不试第三次）
            self.log.append(
                ROUND_RED,
                {
                    "task_id": task.id,
                    "attempt": attempt,
                    "commit_sha": result.commit_sha,
                    "log_tail_lines": len(tail.splitlines()),
                },
            )
            return RoundResult(reason=REASON_CITED, attempts=attempt, log_tail=tail)

        raise AssertionError("两次尝试必然返回，走到这里是逻辑破损")
