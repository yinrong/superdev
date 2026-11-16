"""端到端测试用的离线假件：脚本化回归器 + 假 git，全程不碰真 git。

ScriptedRunner 实现 engines.RegressionRunner 协议（run(module) 判绿），
按构造时传入的绿/红脚本逐轮出结果，演完再跑就报错（与 EchoEngine 同款约定）。
FakeGit 实现 GitOps 的三个同名方法：rev-parse 返回固定 sha，
checkout/reset 只记录调用序列并返回成功。
"""
from __future__ import annotations

from collections.abc import Sequence

from superdev.engines import RegressionResult

__all__ = ["ScriptedRunner", "FakeGit"]


class ScriptedRunner:
    """按脚本判绿的假回归器。

    script 是每轮的绿(True)/红(False)序列；log_tail 是每轮附带的
    失败日志文本（所有轮共用）。calls 记录每次 run 收到的 module，
    results 按调用顺序保存真实返回过的 RegressionResult，
    供测试事后回放进 projection.record_regression（派发器的职责）。
    """

    def __init__(self, script: Sequence[bool], log_tail: str = ""):
        self._script = list(script)
        self._log_tail = log_tail
        self.calls: list[str] = []
        self.results: list[RegressionResult] = []

    def run(self, module: str) -> RegressionResult:
        self.calls.append(module)
        if not self._script:
            raise RuntimeError("ScriptedRunner 脚本演完：run 调用次数超过传入序列")
        green = self._script.pop(0)
        result = RegressionResult(
            green=green,
            commit_sha=f"fake-{len(self.results) + 1:03d}",
            log_tail=self._log_tail,
        )
        self.results.append(result)
        return result


class FakeGit:
    """GitOps 的离线替身：同名方法、成功返回值、完整调用序列。

    rev_parse 一律返回构造时给定的 sha（模拟模块绿点）；
    checkout_b / reset_hard 记入 calls 并返回空串（成功）。
    calls 是 ("rev-parse", ref) / ("checkout", "-B", branch, base) /
    ("reset", "--hard", ref) 的元组序列，供测试断言
    "从绿点开新分支、红即回滚"的纪律轨迹。
    """

    def __init__(self, sha: str = "0" * 40):
        self.sha = sha
        self.calls: list[tuple[str, ...]] = []

    def rev_parse(self, ref: str) -> str:
        self.calls.append(("rev-parse", ref))
        return self.sha

    def checkout_b(self, branch: str, base: str) -> str:
        self.calls.append(("checkout", "-B", branch, base))
        return ""

    def reset_hard(self, ref: str) -> str:
        self.calls.append(("reset", "--hard", ref))
        return ""
