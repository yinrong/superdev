"""Offline fakes for end-to-end tests: a scripted regression runner + fake git; never touches real git.

ScriptedRunner implements the engines.RegressionRunner protocol (run(module)
judges green), producing results round by round from the green/red script passed
at construction; running past the script raises (same convention as EchoEngine).
FakeGit implements GitOps' three same-named methods: rev-parse returns a fixed
sha; checkout/reset only record the call sequence and return success.
"""
from __future__ import annotations

from collections.abc import Sequence

from superdev.engines import RegressionResult

__all__ = ["ScriptedRunner", "FakeGit"]


class ScriptedRunner:
    """A scripted regression runner that judges green.

    script is the per-round green(True)/red(False) sequence; log_tail is the
    failure log text attached to every round (shared by all rounds). calls
    records the module each run received; results keeps the actual
    RegressionResults returned, in call order, so tests can replay them into
    projection.record_regression afterwards (the dispatcher's job).
    """

    def __init__(self, script: Sequence[bool], log_tail: str = ""):
        self._script = list(script)
        self._log_tail = log_tail
        self.calls: list[str] = []
        self.results: list[RegressionResult] = []

    def run(self, module: str) -> RegressionResult:
        self.calls.append(module)
        if not self._script:
            raise RuntimeError(
                "ScriptedRunner script exhausted: run was called more times than the provided sequence"
            )
        green = self._script.pop(0)
        result = RegressionResult(
            green=green,
            commit_sha=f"fake-{len(self.results) + 1:03d}",
            log_tail=self._log_tail,
        )
        self.results.append(result)
        return result


class FakeGit:
    """An offline stand-in for GitOps: same-named methods, success return values, full call sequence.

    rev_parse always returns the sha given at construction (simulating the
    module's green point); checkout_b / reset_hard are recorded into calls and
    return an empty string (success). calls is a sequence of ("rev-parse", ref)
    / ("checkout", "-B", branch, base) / ("reset", "--hard", ref) tuples for
    tests to assert the discipline trail "branch from the green point, roll
    back on red".
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
