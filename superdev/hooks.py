"""管理端 hook 裁决（设计文档 §2 Hook 清单）：四个纯函数，供会话装配调用。

- guard_write：AI 写文件前，目标不在台账目录就拦下（"管理端只写台账"）
- guard_command：AI 执行命令前，git 子命令是 commit/push 就拦下（"管理端不代做交付"）
- post_write_check：台账落盘后自动跑 check_plan，错误注回会话
- stop_gate：会话收口门，全量 pytest + 事件日志哈希链校验，任一坏阻止收尾
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from superdev.check import check_plan
from superdev.events import EventLog
from superdev.ledger import LedgerError, load_plan

__all__ = [
    "WRITE_DENIED",
    "COMMAND_DENIED",
    "PYTEST_TIMEOUT",
    "guard_write",
    "guard_command",
    "post_write_check",
    "stop_gate",
]

WRITE_DENIED = "管理端只写台账"
COMMAND_DENIED = "管理端不代做交付"
PYTEST_TIMEOUT = 300  # 秒：stop_gate 里全量 pytest 的时间上限

# git 顶层的"带值选项"：跳过选项本身后还要连值一起跳过，
# 否则 git -C ../repo commit 这种写法会漏判
_GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def guard_write(
    target_path: str | os.PathLike, allowed_root: str | os.PathLike
) -> tuple[bool, str]:
    """写文件前裁决：target_path 不在 allowed_root 目录下 → (False, 拒绝理由)。

    路径统一过 os.path.abspath 再比，相对路径、正反斜杠都不影响判定；
    前缀相似但不同目录（如 ledger 与 ledger2）不算在其下。
    """
    target = os.path.abspath(str(target_path))
    root = os.path.abspath(str(allowed_root))
    try:
        under = os.path.commonpath([target, root]) == root
    except ValueError:
        # 两个路径不在同一个盘符下，commonpath 直接抛错，必然不在其下
        under = False
    if not under:
        return False, WRITE_DENIED
    return True, ""


def guard_command(cmdline: str | list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """执行命令前裁决：命令里的 git 子命令是 commit 或 push → (False, 拒绝理由)。

    字符串命令按空白切词；git 后面允许带任意个选项（带值选项连值一起跳过），
    第一个非选项词是 commit/push 才拦，git status / git log 这类只读命令放行。
    """
    if isinstance(cmdline, str):
        tokens = cmdline.split()
    else:
        tokens = [str(t) for t in cmdline]
    lowered = [t.lower() for t in tokens]
    for i, tok in enumerate(lowered):
        if tok not in ("git", "git.exe"):
            continue
        j = i + 1
        while j < len(lowered):
            word = lowered[j]
            if word in _GIT_OPTS_WITH_VALUE:
                j += 2  # 选项和它的值都不算子命令
                continue
            if word.startswith("-"):
                j += 1
                continue
            if word in ("commit", "push"):
                return False, COMMAND_DENIED
            break  # 碰到别的子命令（status/log/…）就不是 commit/push 了
    return True, ""


def post_write_check(plan_path: str | os.PathLike) -> tuple[bool, list[str]]:
    """台账写完后的自动校验：check_plan 全过 → (True, [])，否则 (False, 中文问题清单)。

    复现脚本等相对路径按台账文件所在目录解析；台账本身解析失败也算不通过。
    """
    try:
        plan = load_plan(plan_path)
    except LedgerError as e:
        return False, [f"台账解析失败：{e}"]
    problems = check_plan(plan, knowledge_dir=Path(plan_path).resolve().parent)
    return not problems, list(problems)


def stop_gate(
    repo_root: str | os.PathLike, event_log_path: str | os.PathLike | None = None
) -> tuple[bool, list[str]]:
    """会话收口门：全量 pytest + 事件日志哈希链校验，任一坏 → (False, 问题清单)。

    pytest 用当前解释器在 repo_root 下跑，超时 PYTEST_TIMEOUT 秒；
    事件日志缺省取 repo_root/events.jsonl，文件不存在视为无需校验（verify 容忍）。
    """
    problems: list[str] = []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PYTEST_TIMEOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        problems.append(f"全量测试超时：{PYTEST_TIMEOUT} 秒内没有跑完，不能收尾")
    else:
        if proc.returncode != 0:
            tail = "\n".join((proc.stdout or "").splitlines()[-5:])
            problems.append(
                f"全量测试未通过（pytest 退出码 {proc.returncode}），最近输出：\n{tail}"
            )

    log_path = (
        Path(event_log_path)
        if event_log_path is not None
        else Path(repo_root) / "events.jsonl"
    )
    ok, pos = EventLog(log_path).verify()
    if not ok:
        problems.append(f"事件日志校验失败：第 {pos} 条事件的哈希链断裂，决策链不可信")
    return not problems, problems
