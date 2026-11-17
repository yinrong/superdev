"""Quality-gate pipeline (detailed design §8.2): per-module configurable multi-step gating, used directly as a RegressionRunner.

The "green" in "all green before merging to main" no longer rests on a single
regression run: a module's gate.yml declares steps such as lint / typecheck /
test / build, executed in order. Any red step equals a red regression and goes
through the same rollback-and-feed-back flow (RedGreenLoop consumes this class
as-is, no adapter needed). A module without gate.yml defaults to a single step:
python -m pytest -q.

subprocess is referenced through the module attribute so tests can monkeypatch gate.subprocess.run.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from superdev.engines import RegressionResult
from superdev.redgreen import LOG_TAIL_LINES, tail_lines

__all__ = [
    "DEFAULT_TEST_COMMAND",
    "DEFAULT_STEP_TIMEOUT",
    "GATE_CONFIG_FILENAME",
    "GateStep",
    "GatePipeline",
]

# default gate when there is no gate.yml: a single full-regression step
DEFAULT_TEST_COMMAND = ["python", "-m", "pytest", "-q"]
DEFAULT_STEP_TIMEOUT = 120
GATE_CONFIG_FILENAME = "gate.yml"


@dataclass(frozen=True)
class GateStep:
    """One quality-check step: name, command line, timeout in seconds."""

    name: str
    command: list[str]
    timeout: int = DEFAULT_STEP_TIMEOUT


def _text(value: object) -> str:
    """The partial output inside a timeout exception may be str / bytes / None; keep only str."""
    return value if isinstance(value, str) else ""


class GatePipeline:
    """A regressor that runs quality-check steps in order: implements the RegressionRunner protocol, so green is never self-reported by the worker."""

    def __init__(self, steps: list[GateStep]):
        self.steps = list(steps)

    def run(self, module_dir: str) -> RegressionResult:
        """Run the whole pipeline: any red step short-circuits to red; all steps pass returns green (commit_sha fixed to "gate")."""
        for step in self.steps:
            ok, output = self._execute(step, module_dir)
            if not ok:
                return RegressionResult(green=False, log_tail=tail_lines(output))
        return RegressionResult(green=True, commit_sha="gate")

    def _execute(self, step: GateStep, module_dir: str) -> tuple[bool, str]:
        """Execute one step, returning (passed, diagnostic text on failure). The diagnostic header carries the step name so feed-back can locate it."""
        header = f"[gate:{step.name}] "
        try:
            proc = subprocess.run(
                list(step.command),
                cwd=module_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=step.timeout,
            )
        except subprocess.TimeoutExpired as e:
            partial = "\n".join(p for p in (_text(e.stdout), _text(e.stderr)) if p)
            msg = header + f"timed out ({step.timeout}s): {' '.join(step.command)}"
            return False, f"{msg}\n{partial}" if partial else msg
        except OSError as e:
            # missing executable and the like: the pipeline goes red instead of
            # letting the exception blow up the red-green loop
            return False, header + f"cannot execute: {' '.join(step.command)}: {e}"
        if proc.returncode != 0:
            combined = "\n".join(
                p for p in ((proc.stdout or ""), (proc.stderr or "")) if p
            )
            msg = header + f"exit {proc.returncode}"
            return False, f"{msg}\n{combined}" if combined else msg
        return True, ""

    @classmethod
    def from_config(cls, module_dir: str | Path) -> "GatePipeline":
        """Read the module directory's gate.yml (steps: [{name, command, timeout?}]).

        Without the file, default to a single test step: python -m pytest -q;
        if the file exists but is malformed, raise ValueError right away — better
        to fail at start-up than to judge green while broken.
        """
        path = Path(module_dir) / GATE_CONFIG_FILENAME
        if not path.exists():
            return cls([GateStep(name="test", command=list(DEFAULT_TEST_COMMAND))])
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(cls._parse_steps(data, path))

    @staticmethod
    def _parse_steps(data: object, path: Path) -> list[GateStep]:
        if not isinstance(data, dict):
            raise ValueError(f"{GATE_CONFIG_FILENAME} top level must be a mapping: {path}")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError(f"{GATE_CONFIG_FILENAME} needs a non-empty steps list: {path}")
        steps: list[GateStep] = []
        for i, item in enumerate(raw_steps, 1):
            if not isinstance(item, dict) or "name" not in item or "command" not in item:
                raise ValueError(f"{GATE_CONFIG_FILENAME} step {i} is missing name or command: {path}")
            command = item["command"]
            if isinstance(command, str):
                command = shlex.split(command)
            if not isinstance(command, list) or not all(
                isinstance(c, str) for c in command
            ):
                raise ValueError(
                    f"{GATE_CONFIG_FILENAME} step {i} command must be a list of strings: {path}"
                )
            steps.append(
                GateStep(
                    name=str(item["name"]),
                    command=list(command),
                    timeout=int(item.get("timeout", DEFAULT_STEP_TIMEOUT)),
                )
            )
        return steps
