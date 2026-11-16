"""引擎与回归协议 + git 命令包装：红绿循环里可插拔的两半。

对应详细设计 §1.2 纪律脚本：引擎负责实现一个回合，
RegressionRunner 负责判绿（§1.1.2：绿由脚本判定，工人不得自报），
GitOps 包装"从绿点开新分支 / 红即回滚"所需的 git 命令。
真实模式 subprocess 30 秒超时；dry_run 模式只记录拼接的命令，
测试全程不碰真 git。
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "GitError",
    "Engine",
    "RegressionResult",
    "RegressionRunner",
    "EchoEngine",
    "GitOps",
]


class GitError(Exception):
    """git 命令失败：非零退出、超时，或可执行文件缺失。"""


class Engine(Protocol):
    """引擎协议：ZCode/Codex 等适配器实现它，做一轮实现。"""

    def run_round(self, task_desc: str, context: str) -> str:
        """实现一个回合，返回本轮产物摘要。"""
        ...


@dataclass(frozen=True)
class RegressionResult:
    """一次模块全量回归的结果。"""

    green: bool
    commit_sha: str = ""
    log_tail: str = ""


class RegressionRunner(Protocol):
    """回归协议：判绿的唯一依据（§1.1.2）。"""

    def run(self, module: str) -> RegressionResult:
        """跑模块全量回归，返回绿否与失败日志尾部。"""
        ...


class EchoEngine:
    """测试用假引擎：按脚本表演戏，每轮从传入的结果序列弹出一个。"""

    def __init__(self, outputs: Sequence[str]):
        self._pending = list(outputs)
        self.calls: list[tuple[str, str]] = []

    @property
    def pending(self) -> list[str]:
        """脚本里还没演到的部分（拷贝）。"""
        return list(self._pending)

    def run_round(self, task_desc: str, context: str) -> str:
        self.calls.append((task_desc, context))
        if not self._pending:
            raise RuntimeError("EchoEngine 脚本演完：run_round 调用次数超过传入序列")
        return self._pending.pop(0)


class GitOps:
    """包装 rev-parse / checkout -B / reset --hard 三个命令。

    真实模式：subprocess 执行，30 秒超时，任何失败抛 GitError；
    dry_run 模式：不执行，只记录参数列表，rev-parse 返回参数本身，
    这样红绿循环可以拿假绿点 sha 离线测。
    """

    timeout_seconds = 30

    def __init__(self, cwd: str | None = None, dry_run: bool = False):
        self.cwd = cwd
        self.dry_run = dry_run
        self.commands: list[list[str]] = []  # 执行（或拼接）过的命令，按调用顺序

    def _git(self, *args: str) -> str:
        cmd = ["git", *args]
        self.commands.append(list(cmd))
        if self.dry_run:
            return ""
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git 超时（{self.timeout_seconds}s）：{' '.join(cmd)}") from e
        except OSError as e:
            raise GitError(f"git 无法执行：{' '.join(cmd)}：{e}") from e
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise GitError(f"git 失败（exit {proc.returncode}）：{' '.join(cmd)}：{stderr}")
        return (proc.stdout or "").strip()

    def rev_parse(self, ref: str) -> str:
        """把 ref 解析成 commit sha；dry_run 模式返回参数本身。"""
        out = self._git("rev-parse", ref)
        return ref if self.dry_run else out

    def checkout_b(self, branch: str, base: str) -> str:
        """在 base 上建（或重置）分支。§1.1.1：每回合从绿点开新分支。"""
        return self._git("checkout", "-B", branch, base)

    def reset_hard(self, ref: str) -> str:
        """整体回滚到绿点。§1.1.3：红即回滚。"""
        return self._git("reset", "--hard", ref)
