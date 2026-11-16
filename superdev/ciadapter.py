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

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from superdev.events import ALL_GREEN, ROUND_RED, EventLog
from superdev.redgreen import tail_lines

__all__ = [
    "CI_PENDING",
    "CI_SUCCESS",
    "CI_FAILURE",
    "CIStatus",
    "CIAdapter",
    "GithubCIWatch",
    "GitlabCIWatch",
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


# ---------- 真实 API 轮询 watch（详细设计 §8.4；离线测试 monkeypatch urlopen） ----------


def _get_json(url: str, headers: dict[str, str]) -> Any:
    """GET 一个 JSON 接口；HTTPError 原样抛给调用方归一为 failure。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


class GithubCIWatch:
    """GitHub Actions 轮询：取 branch 最新一条 workflow run 的 status/conclusion。

    completed+success → success；completed+其他 → failure（log_tail 写
    "run {id} conclusion={conclusion}"）；未 completed → pending；HTTPError →
    failure 且 log_tail 写状态码。有 token 时带 Authorization: Bearer 头。
    """

    def __init__(
        self,
        repo: str,
        branch: str,
        token: str | None = None,
        api_base: str = "https://api.github.com",
    ):
        self.repo = repo
        self.branch = branch
        self.token = token
        self.api_base = api_base.rstrip("/")

    def check(self) -> CIStatus:
        url = (
            f"{self.api_base}/repos/{self.repo}/actions/runs"
            f"?branch={urllib.parse.quote(self.branch, safe='')}&per_page=1"
        )
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            data = _get_json(url, headers)
        except urllib.error.HTTPError as e:
            return CIStatus(CI_FAILURE, log_tail=f"GitHub API HTTP {e.code}: {url}")
        runs = data.get("workflow_runs") or [] if isinstance(data, dict) else []
        if not runs:
            return CIStatus(CI_PENDING, log_tail=f"branch {self.branch} 没有 workflow run")
        run = runs[0]
        if run.get("status") != "completed":
            return CIStatus(CI_PENDING, log_tail=f"run {run.get('id')} status={run.get('status')}")
        conclusion = run.get("conclusion")
        state = CI_SUCCESS if conclusion == "success" else CI_FAILURE
        return CIStatus(state, log_tail=f"run {run.get('id')} conclusion={conclusion}")


class GitlabCIWatch:
    """GitLab CI 轮询：取 ref 最新一条 pipeline 的 status。

    success → success；failed/canceled → failure；running/pending → pending
    （其余未知状态按 pending 处理，log_tail 只写 pipeline 状态，不拉 jobs）；
    HTTPError（如 404）→ failure 且 log_tail 写状态码。token 走 PRIVATE-TOKEN 头。
    """

    _FAILURE_STATES = frozenset({"failed", "canceled", "cancelled"})

    def __init__(
        self,
        project_id: str | int,
        ref: str,
        base_url: str = "https://gitlab.com/api/v4",
        token: str | None = None,
    ):
        self.project_id = project_id
        self.ref = ref
        self.base_url = base_url.rstrip("/")
        self.token = token

    def check(self) -> CIStatus:
        url = (
            f"{self.base_url}/projects/{urllib.parse.quote(str(self.project_id), safe='')}"
            f"/pipelines?ref={urllib.parse.quote(self.ref, safe='')}&per_page=1"
        )
        headers: dict[str, str] = {}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        try:
            pipelines = _get_json(url, headers)
        except urllib.error.HTTPError as e:
            return CIStatus(CI_FAILURE, log_tail=f"GitLab API HTTP {e.code}: {url}")
        if not isinstance(pipelines, list) or not pipelines:
            return CIStatus(CI_PENDING, log_tail=f"ref {self.ref} 没有 pipeline")
        pipeline = pipelines[0]
        state = str(pipeline.get("status") or "")
        tail = f"pipeline {pipeline.get('id')} status={state}"
        if state == "success":
            return CIStatus(CI_SUCCESS, log_tail=tail)
        if state in self._FAILURE_STATES:
            return CIStatus(CI_FAILURE, log_tail=tail)
        return CIStatus(CI_PENDING, log_tail=tail)
