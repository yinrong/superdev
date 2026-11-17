"""Engine and regression protocols plus a git command wrapper: the two pluggable halves of the red-green loop.

Maps to detailed design §1.2 (discipline script): the engine implements one round,
RegressionRunner judges green (§1.1.2: green is judged by the script, never
self-reported by the worker), and GitOps wraps the git commands for "branch from
the green point / roll back on red". Real mode runs subprocess with a 30-second
timeout; dry_run mode only records the assembled commands, so tests never touch
real git.
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
    """A git command failed: non-zero exit, timeout, or a missing executable."""


class Engine(Protocol):
    """Engine protocol: adapters for ZCode/Codex etc. implement it to run one implementation round."""

    def run_round(self, task_desc: str, context: str) -> str:
        """Implement one round and return a summary of this round's output."""
        ...


@dataclass(frozen=True)
class RegressionResult:
    """Result of one full module regression run."""

    green: bool
    commit_sha: str = ""
    log_tail: str = ""


class RegressionRunner(Protocol):
    """Regression protocol: the sole basis for judging green (§1.1.2)."""

    def run(self, module: str) -> RegressionResult:
        """Run the full module regression, returning greenness and the failure log tail."""
        ...


class EchoEngine:
    """Fake engine for tests: plays a scripted performance, popping one output per round from the provided sequence."""

    def __init__(self, outputs: Sequence[str]):
        self._pending = list(outputs)
        self.calls: list[tuple[str, str]] = []

    @property
    def pending(self) -> list[str]:
        """Parts of the script not performed yet (copy)."""
        return list(self._pending)

    def run_round(self, task_desc: str, context: str) -> str:
        self.calls.append((task_desc, context))
        if not self._pending:
            raise RuntimeError(
                "EchoEngine script exhausted: run_round was called more times than the provided sequence"
            )
        return self._pending.pop(0)


class GitOps:
    """Wraps three commands: rev-parse / checkout -B / reset --hard.

    Real mode: runs subprocess with a 30-second timeout; any failure raises GitError.
    dry_run mode: executes nothing, only records the argument lists; rev-parse
    returns its argument unchanged, so the red-green loop can be tested offline
    against a fake green-point sha.
    """

    timeout_seconds = 30

    def __init__(self, cwd: str | None = None, dry_run: bool = False):
        self.cwd = cwd
        self.dry_run = dry_run
        self.commands: list[list[str]] = []  # commands executed (or assembled), in call order

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
            raise GitError(f"git timed out ({self.timeout_seconds}s): {' '.join(cmd)}") from e
        except OSError as e:
            raise GitError(f"git cannot be executed: {' '.join(cmd)}: {e}") from e
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise GitError(f"git failed (exit {proc.returncode}): {' '.join(cmd)}: {stderr}")
        return (proc.stdout or "").strip()

    def rev_parse(self, ref: str) -> str:
        """Resolve a ref to a commit sha; in dry_run mode return the argument unchanged."""
        out = self._git("rev-parse", ref)
        return ref if self.dry_run else out

    def checkout_b(self, branch: str, base: str) -> str:
        """Create (or reset) a branch on base. §1.1.1: every round branches from the green point."""
        return self._git("checkout", "-B", branch, base)

    def reset_hard(self, ref: str) -> str:
        """Roll everything back to the green point. §1.1.3: red means roll back."""
        return self._git("reset", "--hard", ref)
