"""质检管线（详细设计 §8.2）：每模块可配置的多步门禁，直接当 RegressionRunner 用。

"全绿才合 main"的绿不再只看单一回归：模块目录 gate.yml 声明
lint / typecheck / test / build 等步骤，顺序执行，任一步红等同回归红，
走同一套回滚回喂流程（RedGreenLoop 原样消费本类，无须适配）。
无 gate.yml 的模块默认只跑一步 python -m pytest -q。

subprocess 通过模块属性引用调用，测试可 monkeypatch gate.subprocess.run。
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

# 无 gate.yml 时的默认门禁：只跑一步全量回归
DEFAULT_TEST_COMMAND = ["python", "-m", "pytest", "-q"]
DEFAULT_STEP_TIMEOUT = 120
GATE_CONFIG_FILENAME = "gate.yml"


@dataclass(frozen=True)
class GateStep:
    """一步质检：名字、命令行、超时秒数。"""

    name: str
    command: list[str]
    timeout: int = DEFAULT_STEP_TIMEOUT


def _text(value: object) -> str:
    """超时异常里的半截输出可能是 str / bytes / None，只留 str。"""
    return value if isinstance(value, str) else ""


class GatePipeline:
    """顺序执行质检步骤的回归器：实现 RegressionRunner 协议，判绿不靠工人自报。"""

    def __init__(self, steps: list[GateStep]):
        self.steps = list(steps)

    def run(self, module_dir: str) -> RegressionResult:
        """跑完整管线：任一步红立即短路返回红，全过返回绿（commit_sha 固定 "gate"）。"""
        for step in self.steps:
            ok, output = self._execute(step, module_dir)
            if not ok:
                return RegressionResult(green=False, log_tail=tail_lines(output))
        return RegressionResult(green=True, commit_sha="gate")

    def _execute(self, step: GateStep, module_dir: str) -> tuple[bool, str]:
        """执行一步，返回 (是否过, 失败时的诊断文本)。诊断头部带步骤名，回喂时可定位。"""
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
            msg = header + f"超时（{step.timeout}s）：{' '.join(step.command)}"
            return False, f"{msg}\n{partial}" if partial else msg
        except OSError as e:
            # 可执行文件缺失等：管线红，而不是把异常抛进红绿循环
            return False, header + f"无法执行：{' '.join(step.command)}：{e}"
        if proc.returncode != 0:
            combined = "\n".join(
                p for p in ((proc.stdout or ""), (proc.stderr or "")) if p
            )
            msg = header + f"exit {proc.returncode}"
            return False, f"{msg}\n{combined}" if combined else msg
        return True, ""

    @classmethod
    def from_config(cls, module_dir: str | Path) -> "GatePipeline":
        """读模块目录 gate.yml（steps: [{name, command, timeout?}]）。

        无文件时默认只有一步 test: python -m pytest -q；
        文件存在但格式不对直接 ValueError，宁可开工失败也不带病判绿。
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
            raise ValueError(f"{GATE_CONFIG_FILENAME} 顶层必须是映射：{path}")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError(f"{GATE_CONFIG_FILENAME} 需要非空 steps 列表：{path}")
        steps: list[GateStep] = []
        for i, item in enumerate(raw_steps, 1):
            if not isinstance(item, dict) or "name" not in item or "command" not in item:
                raise ValueError(f"{GATE_CONFIG_FILENAME} 第 {i} 步缺 name 或 command：{path}")
            command = item["command"]
            if isinstance(command, str):
                command = shlex.split(command)
            if not isinstance(command, list) or not all(
                isinstance(c, str) for c in command
            ):
                raise ValueError(
                    f"{GATE_CONFIG_FILENAME} 第 {i} 步 command 必须是字符串列表：{path}"
                )
            steps.append(
                GateStep(
                    name=str(item["name"]),
                    command=list(command),
                    timeout=int(item.get("timeout", DEFAULT_STEP_TIMEOUT)),
                )
            )
        return steps
