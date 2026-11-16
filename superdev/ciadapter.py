"""CI 适配器（详细设计 §8.4）：合并请求开出后盯远端流水线，结果走本地同一条回喂路。

本模块不做真实 HTTP 轮询：watch 回调是注入点，返回 CIStatus 三态。
接 GitHub Actions / GitLab CI 时在这里实现具体 watch：
  - GitHub Actions：用 `gh api repos/{owner}/{repo}/actions/runs?event=pull_request`
    轮询最新 run 的 status/conclusion（success/failure/timing-out 映射本模块三态），
    失败日志取 `/actions/runs/{run_id}/jobs` 的 step 结论与末段输出；
  - GitLab CI：`GET /api/v4/projects/{id}/pipelines?ref={branch}` 轮询 status，
    失败日志取 `/pipelines/{pipeline_id}/jobs` 里 failed job 的 trace 末段。
两个实现都只是"查状态 + 拼日志尾"的薄函数，装进 CIAdapter(watch=...) 即可，
poll_once 的事件记录与回喂决策全部复用，本地回归和远端 CI 用同一条失败处理路径。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from superdev.events import ALL_GREEN, ROUND_RED, EventLog
from superdev.redgreen import tail_lines

__all__ = [
    "CI_PENDING",
    "CI_SUCCESS",
    "CI_FAILURE",
    "CIStatus",
    "CIAdapter",
]

CI_PENDING = "pending"
CI_SUCCESS = "success"
CI_FAILURE = "failure"

# poll_once 的口头结论：给派发器决定下一步（绿=可合，回喂=修，等待=继续轮询）
VERDICT_GREEN = "绿"
VERDICT_FEEDBACK = "回喂"
VERDICT_WAIT = "等待"


@dataclass(frozen=True)
class CIStatus:
    """一次远端流水线查询结果：三态 + 失败日志尾。"""

    state: str
    log_tail: str = ""


class CIAdapter:
    """合并请求轮询器：远端 CI 红/绿映射为本地事件，同状态连续出现不重复记。"""

    def __init__(self, watch: Callable[[], CIStatus]):
        self.watch = watch
        # mr_url -> 上次轮询到的状态；只抑制"同 mr 连续同终态"的重复事件，
        # 中间出现过 pending（新推了一轮 CI）后，终态再次落盘会被视为新结果
        self._last_state: dict[str, str] = {}

    def poll_once(self, mr_url: str, event_log: EventLog) -> str:
        """查一次远端状态并落事件：success → ALL_GREEN；failure → ROUND_RED（带日志尾）。"""
        status = self.watch()
        state = status.state
        if state in (CI_SUCCESS, CI_FAILURE) and self._last_state.get(mr_url) != state:
            if state == CI_SUCCESS:
                event_log.append(ALL_GREEN, {"mr_url": mr_url})
            else:
                event_log.append(
                    ROUND_RED,
                    {"mr_url": mr_url, "log_tail": tail_lines(status.log_tail)},
                )
        self._last_state[mr_url] = state
        if state == CI_SUCCESS:
            return VERDICT_GREEN
        if state == CI_FAILURE:
            return VERDICT_FEEDBACK
        return VERDICT_WAIT
