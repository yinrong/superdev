"""superdev.engines 与 superdev.redgreen 的测试：假引擎按脚本演戏、dry_run 命令拼接、红绿循环纪律。

全文件不碰真 git：GitOps 用 dry_run，subprocess 的失败与超时用 monkeypatch 顶替。
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
    """假回归：按脚本每次弹出一个结果，并记录见过的模块名。"""

    results: list[RegressionResult] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)

    def run(self, module: str) -> RegressionResult:
        self.modules.append(module)
        if not self.results:
            raise RuntimeError("ScriptedRunner 脚本演完")
        return self.results.pop(0)


def _task(**overrides) -> TaskSpec:
    """默认任务 T06：研究结论关键词"锁竞争"，策略文本引用了它。"""
    defaults: dict = dict(
        id="T06",
        module="core",
        base=BASE,
        title="加回滚",
        research_notes="锁竞争：连接必须带超时",
        strategy="策略变更：改用锁竞争超时加重试",
        strategy_changes=1,
    )
    defaults.update(overrides)
    return TaskSpec(**defaults)


def _loop(tmp_path, outputs, results, knowledge_base=None):
    """装配一套 dry_run + 假引擎 + 假回归的红绿循环，返回全部部件供断言。"""
    engine = EchoEngine(list(outputs))
    runner = ScriptedRunner(list(results))
    git_ops = GitOps(dry_run=True)
    log = EventLog(tmp_path / "events.jsonl")
    loop = RedGreenLoop(engine, runner, git_ops, log, knowledge_base=knowledge_base)
    return loop, engine, runner, git_ops, log


# ---------- engines.py：EchoEngine 与 GitOps ----------


def test_echo_engine_performs_script_and_records_calls():
    engine = EchoEngine(["第一轮产物", "第二轮产物"])
    assert engine.run_round("T06 加回滚", "") == "第一轮产物"
    assert engine.run_round("T06 加回滚", "FAIL 日志") == "第二轮产物"
    assert engine.calls == [
        ("T06 加回滚", ""),
        ("T06 加回滚", "FAIL 日志"),
    ]
    assert engine.pending == []


def test_echo_engine_exhausted_script_raises():
    engine = EchoEngine(["只有一轮"])
    engine.run_round("t", "")
    with pytest.raises(RuntimeError):
        engine.run_round("t", "")


def test_gitops_dry_run_splices_commands_without_running():
    ops = GitOps(cwd="D:/repo", dry_run=True)
    assert ops.rev_parse(BASE) == BASE  # dry_run 下 rev-parse 原样返回参数
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


# ---------- redgreen.py：四条主路径 ----------


def test_round_green_on_first_attempt(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["搞定"], [_green()])
    result = loop.round(_task())

    assert result.green is True
    assert result.stuck is False
    assert result.attempts == 1
    assert result.reason == ""
    assert len(engine.calls) == 1
    assert engine.calls[0] == ("T06 加回滚", "")  # 第一轮没有任何回喂上下文
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
        tmp_path, ["初稿", "修复"], [_red(), _green()]
    )
    result = loop.round(_task())

    assert result.green is True
    assert result.attempts == 2
    assert git_ops.commands[-1] == ["git", "reset", "--hard", BASE]  # 第 1 红整体回滚绿点
    assert "AssertionError: boom" in engine.calls[1][1]  # 失败日志回喂给了第 2 轮
    assert [e["kind"] for e in log.events()] == [ROUND_RED, ROUND_GREEN]


def test_round_two_red_with_citation_continues(tmp_path):
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["方向一", "方向二"], [_red(), _red()]
    )
    result = loop.round(_task())

    assert result.green is False
    assert result.stuck is False  # 未卡住：派发器可按新方向另开回合
    assert result.reason == REASON_CITED
    assert result.attempts == 2
    assert [e["kind"] for e in log.events()] == [ROUND_RED, ROUND_RED]


def test_round_two_red_without_strategy_changes_stuck(tmp_path):
    task = _task(strategy_changes=0)
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["方向一", "方向二"], [_red(), _red()])
    result = loop.round(task)

    assert result.stuck is True
    assert result.reason == REASON_NO_CITATION
    assert result.attempts == 2
    assert [e["kind"] for e in log.events()] == [ROUND_RED, TASK_STUCK]
    assert log.events()[-1]["payload"]["reason"] == REASON_NO_CITATION


def test_round_two_red_strategy_not_citing_notes_stuck(tmp_path):
    # 有策略变更次数，但策略文本不含研究结论的任何关键词 → 纯猜测，程序拒绝
    task = _task(strategy="策略变更：整套换成别的数据库")
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["方向一", "方向二"], [_red(), _red()])
    result = loop.round(task)

    assert result.stuck is True
    assert result.reason == REASON_NO_CITATION
    assert [e["kind"] for e in log.events()] == [ROUND_RED, TASK_STUCK]


# ---------- redgreen.py：事件、命令序列、截断与知识库 ----------


def test_event_count_ids_and_chain_stay_intact(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["方向一", "方向二"], [_red(), _red()])
    loop.round(_task())

    assert len(log) == 2
    assert [e["event_id"] for e in log.events()] == [1, 2]
    assert log.verify() == (True, 0)  # 循环写完事件后哈希链仍完整


def test_loop_dry_run_command_sequence(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["初稿", "修复"], [_red(), _green()])
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
        tmp_path, ["初稿", "修复"], [_red(tail=big_tail), _green()]
    )
    loop.round(_task())

    fed = engine.calls[1][1]
    assert fed.splitlines() == lines[-200:]  # 只回喂最近 200 行
    assert len(fed.splitlines()) == 200


def test_log_tail_truncated_in_stuck_result(tmp_path):
    lines = [f"L{i:03d}" for i in range(1, 301)]
    task = _task(strategy_changes=0)  # 走卡住路径，看返回值里的日志截断
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["方向一", "方向二"], [_red(), _red(tail="\n".join(lines))]
    )
    result = loop.round(task)

    assert result.stuck is True
    assert result.log_tail.splitlines() == lines[-200:]


def test_knowledge_base_injection_counts_as_research(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb.yml")
    kb.record("踩坑", "锁竞争", "连接池打满导致回归红")
    # 任务自身研究结论为空，策略只引用知识库注入的坑 → 同样过关
    task = _task(research_notes="", strategy="策略变更：规避锁竞争，压连接池上限")
    loop, engine, runner, git_ops, log = _loop(
        tmp_path, ["方向一", "方向二"], [_red(), _red()], knowledge_base=kb
    )
    result = loop.round(task)

    assert result.stuck is False
    assert result.reason == REASON_CITED
    assert [e["kind"] for e in log.events()] == [ROUND_RED, ROUND_RED]


def test_runner_judges_module_each_attempt(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["初稿", "修复"], [_red(), _green()])
    loop.round(_task())
    assert runner.modules == ["core", "core"]


def test_round_requires_green_base(tmp_path):
    loop, engine, runner, git_ops, log = _loop(tmp_path, ["搞定"], [_green()])
    with pytest.raises(ValueError):
        loop.round(_task(base=""))


def test_round_result_defaults():
    r = RoundResult()
    assert (r.green, r.stuck, r.reason, r.attempts, r.log_tail) == (False, False, "", 0, "")
