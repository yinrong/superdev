"""Tests for superdev.gate: the quality pipeline runs steps in order, any red step short-circuits, config parsing, and use as a RegressionRunner.

All subprocess calls in this file are replaced via monkeypatch (plus one small
real-python integration case), so nothing depends on lint / build tools being
installed, and no real git is touched.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

import superdev.gate as gate_mod
from superdev.engines import EchoEngine, GitOps
from superdev.events import ROUND_GREEN, EventLog
from superdev.gate import GatePipeline, GateStep
from superdev.redgreen import RedGreenLoop, TaskSpec


def _proc(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["cmd"], returncode=code, stdout=stdout, stderr=stderr)


@dataclass
class FakeRun:
    """Replays CompletedProcess objects or exceptions from a script, recording each command, cwd and timeout."""

    script: list = field(default_factory=list)
    calls: list[tuple[list[str], object, int]] = field(default_factory=list)

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("cwd"), kwargs.get("timeout")))
        item = self.script.pop(0) if self.script else _proc(0)
        if isinstance(item, Exception):
            raise item
        return item


def _pipeline(*steps: GateStep) -> GatePipeline:
    return GatePipeline(list(steps))


# ---------- run: in-order execution and short-circuit on red ----------


def test_all_steps_pass_returns_green_with_gate_sha(tmp_path, monkeypatch):
    fake = FakeRun([_proc(0), _proc(0)])
    monkeypatch.setattr(gate_mod.subprocess, "run", fake)
    pipeline = _pipeline(
        GateStep("lint", ["ruff", "check", "."]),
        GateStep("test", ["python", "-m", "pytest", "-q"], timeout=60),
    )

    result = pipeline.run(str(tmp_path))

    assert result.green is True
    assert result.commit_sha == "gate"
    assert result.log_tail == ""
    assert fake.calls == [
        (["ruff", "check", "."], str(tmp_path), 120),
        (["python", "-m", "pytest", "-q"], str(tmp_path), 60),
    ]


def test_first_step_red_short_circuits(tmp_path, monkeypatch):
    fake = FakeRun([_proc(2, stdout="E501 line too long\n")])
    monkeypatch.setattr(gate_mod.subprocess, "run", fake)
    pipeline = _pipeline(GateStep("lint", ["ruff"]), GateStep("test", ["pytest"]))

    result = pipeline.run(str(tmp_path))

    assert result.green is False
    assert result.commit_sha == ""
    assert len(fake.calls) == 1  # step 1 went red, step 2 never runs
    assert "[gate:lint]" in result.log_tail
    assert "E501 line too long" in result.log_tail


def test_middle_step_red_short_circuits(tmp_path, monkeypatch):
    fake = FakeRun([_proc(0), _proc(1, stderr="TypeError: boom\n")])
    monkeypatch.setattr(gate_mod.subprocess, "run", fake)
    pipeline = _pipeline(
        GateStep("lint", ["ruff"]),
        GateStep("typecheck", ["mypy", "."]),
        GateStep("test", ["pytest"]),
    )

    result = pipeline.run(str(tmp_path))

    assert result.green is False
    assert len(fake.calls) == 2  # step 2 went red, step 3 never runs
    assert "[gate:typecheck]" in result.log_tail  # the failing step is locatable
    assert "TypeError: boom" in result.log_tail


def test_step_timeout_is_red_and_names_the_step(tmp_path, monkeypatch):
    fake = FakeRun([subprocess.TimeoutExpired(["slow.cmd"], timeout=5)])
    monkeypatch.setattr(gate_mod.subprocess, "run", fake)
    pipeline = _pipeline(GateStep("build", ["python", "build.py"], timeout=5))

    result = pipeline.run(str(tmp_path))

    assert result.green is False
    assert "timed out" in result.log_tail
    assert "build" in result.log_tail  # step name and command both appear in the diagnostic


def test_step_not_executable_is_red_not_crash(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("[WinError 2] The system cannot find the file specified.")

    monkeypatch.setattr(gate_mod.subprocess, "run", fake_run)
    result = _pipeline(GateStep("lint", ["no-such-tool"])).run(str(tmp_path))

    assert result.green is False
    assert "cannot execute" in result.log_tail


def test_red_log_tail_keeps_last_200_lines(tmp_path, monkeypatch):
    lines = [f"L{i:03d}" for i in range(1, 321)]
    fake = FakeRun([_proc(1, stdout="\n".join(lines))])
    monkeypatch.setattr(gate_mod.subprocess, "run", fake)

    result = _pipeline(GateStep("test", ["pytest"])).run(str(tmp_path))

    assert result.green is False
    assert result.log_tail.splitlines() == lines[-200:]  # keep only the last 200 lines; head lines get squeezed out


# ---------- from_config: defaults and custom ----------


def test_from_config_missing_file_defaults_to_single_pytest(tmp_path):
    pipeline = GatePipeline.from_config(tmp_path)
    assert pipeline.steps == [GateStep(name="test", command=["python", "-m", "pytest", "-q"])]


def test_from_config_reads_custom_steps_and_string_command(tmp_path):
    (tmp_path / "gate.yml").write_text(
        "steps:\n"
        "  - name: lint\n"
        "    command: [ruff, check, .]\n"
        "  - name: build\n"
        "    command: python -m build\n"
        "    timeout: 60\n",
        encoding="utf-8",
    )

    pipeline = GatePipeline.from_config(tmp_path)

    assert pipeline.steps == [
        GateStep(name="lint", command=["ruff", "check", "."]),
        GateStep(name="build", command=["python", "-m", "build"], timeout=60),
    ]


def test_from_config_rejects_malformed(tmp_path):
    (tmp_path / "gate.yml").write_text("steps: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        GatePipeline.from_config(tmp_path)


# ---------- compatibility: fed straight into RedGreenLoop as the RegressionRunner ----------


def test_gate_pipeline_serves_as_runner_in_redgreen_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_mod.subprocess, "run", FakeRun([_proc(0)]))
    runner = GatePipeline.from_config(tmp_path)  # the default single pytest step
    engine = EchoEngine(["done"])
    git_ops = GitOps(dry_run=True)
    log = EventLog(tmp_path / "events.jsonl")
    loop = RedGreenLoop(engine, runner, git_ops, log)

    result = loop.round(TaskSpec(id="T14", module=str(tmp_path), base="abc123", title="pipeline"))

    assert result.green is True
    assert result.attempts == 1
    events = log.events()
    assert [e["kind"] for e in events] == [ROUND_GREEN]
    assert events[0]["payload"]["commit_sha"] == "gate"


def test_real_subprocess_green_then_red(tmp_path):
    # really run python once to prove the subprocess arguments work in a real
    # environment (Windows / UTF-8 output capture)
    green = _pipeline(GateStep("ok", ["python", "-c", "print('hi')"])).run(str(tmp_path))
    assert green.green is True
    assert green.commit_sha == "gate"

    red = _pipeline(
        GateStep("bad", ["python", "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"])
    ).run(str(tmp_path))
    assert red.green is False
    assert "[gate:bad]" in red.log_tail
    assert "boom" in red.log_tail
