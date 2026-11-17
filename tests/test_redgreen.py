"""Tests for superdev.engines and superdev.redgreen: fake engine playing a script, dry_run command assembly, red-green loop discipline.

Nothing here touches real git: GitOps runs in dry_run, and subprocess failures
and timeouts are monkeypatched in.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

import superdev.engines as engines_mod
from superdev.engines import EchoEngine, GitError, GitOps, RegressionResult
from superdev.events import ROUND_GREEN, ROUND_RED, TASK_STUCK, EventLog
from superdev.redgreen import (
    REASON_CITED,
    REASON_NO_CITATION,
    RedGreenLoop,
    RoundResult,
    TaskSpec,
)
from superdev.research import KnowledgeBase

BASE = "abc123"


def _green(sha: str = "sha-9") -> RegressionResult:
    return RegressionResult(green=True, commit_sha=sha, log_tail="3 passed in 0.01s")


def _red(sha: str = "sha-red", tail: str = "FAIL test_x\nAssertionError: boom") -> RegressionResult:
    return RegressionResult(green=False, commit_sha=sha, log_tail=tail)


@dataclass
class ScriptedRunner:
    """Fake regression runner: pops one scripted result per call and records the module names it saw."""

    results: list[RegressionResult] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)

    def run(self, module: str) -> RegressionResult:
        self.modules.append(module)
        if not self.results:
            raise RuntimeError("ScriptedRunner script exhausted")
        return self.results.pop(0)


def _task(**overrides) -> TaskSpec:
    """Default task T06: research notes keyword "lock", and a strategy text that cites it."""
    defaults: dict = dict(
        id="T06",
        module="core",
        base=BASE,
        title="add rollback",
        research_notes="lock contention: connections must carry a timeout",
        strategy="strategy change: switch to lock contention timeout plus retry",
        strategy_changes=1,
    )
    defaults.update(overrides)
    return TaskSpec(**defaults)


def _loop(tmp_path, outputs, results, knowledge_base=None):
    """Assemble a red-green loop with dry_run git, a fake engine, and a fake regression runner; return all parts for assertions."""
    engine = EchoEngine(list(outputs))
    runner = ScriptedRunner(list(results))
    git_ops = GitOps(dry_run=True)
    log = EventLog(tmp_path / "events.jsonl")
    loop = RedGreenLoop(engine, runner, git_ops, log, knowledge_base=knowledge_base)
    return loop, engine, runner, git_ops, log


# ---------- engines.py: EchoEngine and GitOps ----------


def test_echo_engine_performs_script_and_records_calls():
    engine = EchoEngine(["round 1 output", "round 2 output"])
    assert engine.run_round("T06 add rollback", "") == "round 1 output"
    assert engine.run_round("T06 add rollback", "FAIL log") == "round 2 output"
    assert engine.calls == [
        ("T06 add rollback", ""),
        ("T06 add rollback", "FAIL log"),
    ]
    assert engine.pending == []


def test_echo_engine_exhausted_script_raises():
    engine = EchoEngine(["one round only"])
    engine.run_round("t", "")
    with pytest.raises(RuntimeError):
        engine.run_round("t", "")


def test_gitops_dry_run_splices_commands_without_running():
    ops = GitOps(cwd="D:/repo", dry_run=True)
    assert ops.rev_parse(BASE) == BASE  # in dry_run, rev-parse returns the argument unchanged
    ops.checkout_b("superdev/T06", BASE)
    ops.reset_hard(BASE)
    assert ops.commands == [
        ["git", "rev-parse", BASE],
        ["git", "checkout", "-B", "superdev/T06", BASE],
        ["git", "reset", "--hard", BASE],
    ]


def test_gitops_nonzero_exit_raises_git_error(monkeypatch):
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, returncode=128, stderr="fatal: bad object")

    monkeypatch.setattr(engines_mod.subprocess, "run", fake_run)
    ops = GitOps()
    with pytest.raises(GitError) as ei:
        ops.rev_parse("bogus")
    assert "128" in str(ei.value)
    assert seen["timeout"] == 30
    assert ops.commands == [["git", "rev-parse", "bogus"]]


def test_gitops_timeout_raises_git_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=30)

    monkeypatch.setattr(engines_mod.subprocess, "run", fake_run)
    ops = GitOps()
    with pytest.raises(GitError):
        ops.reset_hard(BASE)


# ---------- redgreen.py: the four main paths ----------


def test_round_green_on_first_attempt(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["done"], [_green()])
    result = loop.round(_task())

    assert result.green is True
    assert result.stuck is False
    assert result.attempts == 1
    assert result.reason == ""
    assert len(engine.calls) == 1
    assert engine.calls[0] == ("T06 add rollback", "")  # first attempt has no fed-back context
    assert git_ops.commands == [
        ["git", "rev-parse", BASE],
        ["git", "checkout", "-B", "superdev/T06", BASE],
    ]
    assert [e["kind"] for e in log.events()] == [ROUND_GREEN]
    payload = log.events()[0]["payload"]
    assert payload["task_id"] == "T06"
    assert payload["attempt"] == 1
    assert payload["commit_sha"] == "sha-9"


def test_round_red_then_green_feeds_tail_back(tmp_path):
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["first draft", "fix"], [_red(), _green()]
    )
    result = loop.round(_task())

    assert result.green is True
    assert result.attempts == 2
    assert git_ops.commands[-1] == ["git", "reset", "--hard", BASE]  # first red rolls all the way back to the green point
    assert "AssertionError: boom" in engine.calls[1][1]  # the failure log was fed into attempt 2
    assert [e["kind"] for e in log.events()] == [ROUND_RED, ROUND_GREEN]


def test_round_two_red_with_citation_continues(tmp_path):
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["direction 1", "direction 2"], [_red(), _red()]
    )
    result = loop.round(_task())

    assert result.green is False
    assert result.stuck is False  # not stuck: the dispatcher may open another round with the new direction
    assert result.reason == REASON_CITED
    assert result.attempts == 2
    assert [e["kind"] for e in log.events()] == [ROUND_RED, ROUND_RED]


def test_round_two_red_without_strategy_changes_stuck(tmp_path):
    task = _task(strategy_changes=0)
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["direction 1", "direction 2"], [_red(), _red()])
    result = loop.round(task)

    assert result.stuck is True
    assert result.reason == REASON_NO_CITATION
    assert result.attempts == 2
    assert [e["kind"] for e in log.events()] == [ROUND_RED, TASK_STUCK]
    assert log.events()[-1]["payload"]["reason"] == REASON_NO_CITATION


def test_round_two_red_strategy_not_citing_notes_stuck(tmp_path):
    # strategy change count is nonzero, but the strategy text contains no research-notes keyword -> pure guesswork, refused by the program
    task = _task(strategy="strategy change: swap the whole thing to a different database")
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["direction 1", "direction 2"], [_red(), _red()])
    result = loop.round(task)

    assert result.stuck is True
    assert result.reason == REASON_NO_CITATION
    assert [e["kind"] for e in log.events()] == [ROUND_RED, TASK_STUCK]


# ---------- redgreen.py: events, command sequence, truncation, and knowledge base ----------


def test_event_count_ids_and_chain_stay_intact(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["direction 1", "direction 2"], [_red(), _red()])
    loop.round(_task())

    assert len(log) == 2
    assert [e["event_id"] for e in log.events()] == [1, 2]
    assert log.verify() == (True, 0)  # the hash chain is still intact after the loop wrote its events


def test_loop_dry_run_command_sequence(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["first draft", "fix"], [_red(), _green()])
    loop.round(_task())

    assert git_ops.commands == [
        ["git", "rev-parse", BASE],
        ["git", "checkout", "-B", "superdev/T06", BASE],
        ["git", "reset", "--hard", BASE],
    ]


def test_log_tail_truncated_to_last_200_lines_when_fed_back(tmp_path):
    lines = [f"L{i:03d}" for i in range(1, 351)]
    big_tail = "\n".join(lines)
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["first draft", "fix"], [_red(tail=big_tail), _green()]
    )
    loop.round(_task())

    fed = engine.calls[1][1]
    assert fed.splitlines() == lines[-200:]  # only the last 200 lines are fed back
    assert len(fed.splitlines()) == 200


def test_log_tail_truncated_in_stuck_result(tmp_path):
    lines = [f"L{i:03d}" for i in range(1, 301)]
    task = _task(strategy_changes=0)  # take the stuck path to check log truncation in the return value
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["direction 1", "direction 2"], [_red(), _red(tail="\n".join(lines))]
    )
    result = loop.round(task)

    assert result.stuck is True
    assert result.log_tail.splitlines() == lines[-200:]


def test_knowledge_base_injection_counts_as_research(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb.yml")
    kb.record("pitfall", "lock contention", "connection pool exhaustion turned the regression red")
    # the task's own research notes are empty and the strategy cites only the knowledge-base pitfall -> still passes
    task = _task(research_notes="", strategy="strategy change: avoid lock contention, cap the connection pool")
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["direction 1", "direction 2"], [_red(), _red()], knowledge_base=kb
    )
    result = loop.round(task)

    assert result.stuck is False
    assert result.reason == REASON_CITED
    assert [e["kind"] for e in log.events()] == [ROUND_RED, ROUND_RED]


def test_runner_judges_module_each_attempt(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["first draft", "fix"], [_red(), _green()])
    loop.round(_task())
    assert runner.modules == ["core", "core"]


def test_round_requires_green_base(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["done"], [_green()])
    with pytest.raises(ValueError):
        loop.round(_task(base=""))


def test_round_result_defaults():
    r = RoundResult()
    assert (r.green, r.stuck, r.reason, r.attempts, r.log_tail) == (False, False, "", 0, "")
