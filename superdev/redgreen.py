"""Red-green loop discipline script (detailed design §1.1-§1.3): start from a green point, roll back on red, and after two reds research first, then switch direction.

Engine and regression are injected per the engines protocols; all git goes through
GitOps (tests use dry_run plus a fake engine, never touching real git). Events are
written to EventLog; the green point is assembled into TaskSpec by the caller
(the dispatcher) from the ledger.
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

# At most 2 attempts within one round; a 2nd red must go to the research check, never a 3rd try (§1.1.4)
MAX_ATTEMPTS = 2
# Failure log feedback keeps only the last 200 lines (§1.1.3)
LOG_TAIL_LINES = 200

REASON_NO_CITATION = "Strategy change cites no research"
REASON_CITED = "Strategy change cites research"


@dataclass
class TaskSpec:
    """Everything a task needs to enter the red-green loop (assembled by the dispatcher from the ledger + status.yml)."""

    id: str
    module: str
    base: str  # module last_green (sha or resolvable ref); empty refuses to start
    title: str = ""
    size: str = "medium"  # research budget tier, used for knowledge base injection
    research_notes: str = ""  # task research conclusions (§1.4)
    strategy: str = ""  # strategy change text for this round
    strategy_changes: int = 0  # cumulative strategy change count; 0 means never switched direction


@dataclass
class RoundResult:
    """Outcome of one round() call.

    green and stuck both False means: the new direction cites research, so the
    dispatcher should open another round to retry (no third attempt within this
    round).
    """

    green: bool = False
    stuck: bool = False
    reason: str = ""
    attempts: int = 0
    log_tail: str = ""


def tail_lines(text: str, limit: int = LOG_TAIL_LINES) -> str:
    """Keep only the last limit lines: the tail of a failure log is the freshest, most useful part."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[-limit:])


class RedGreenLoop:
    """One round of the discipline script: branch at green -> implement -> judge green -> red rolls back and feeds the tail back / two reds trigger the research check."""

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
            raise ValueError(f"Task {task.id} is missing its green base; refusing to start")

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
                # First red: record the event, roll back to the green point, and feed the
                # failure log (last 200 lines) into the next attempt
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

            # Second red: inject the module knowledge base first, then check whether
            # the strategy change cites research
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

            # Citation passed: record the red event and hand back to the dispatcher to
            # open another round with the new direction (no third try in this round)
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

        raise AssertionError("Both attempts must return; reaching here means the logic is broken")
