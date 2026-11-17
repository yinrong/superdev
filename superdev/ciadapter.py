"""CI adapter (detailed design §8.4): once a merge request is open, watch the remote pipeline and route the result through the same local feed-back path.

This module does no real HTTP polling: the watch callback is the injection point
and returns a three-state CIStatus. To hook up GitHub Actions / GitLab CI,
implement the concrete watch here:
  - GitHub Actions: poll the latest run's status/conclusion via
    `gh api repos/{owner}/{repo}/actions/runs?event=pull_request`
    (success/failure/timing-out map onto this module's three states); on failure,
    take step conclusions and tail output from `/actions/runs/{run_id}/jobs`;
  - GitLab CI: poll status via `GET /api/v4/projects/{id}/pipelines?ref={branch}`;
    on failure, take the trace tail of the failed job in `/pipelines/{pipeline_id}/jobs`.
Both implementations are thin "query status + assemble log tail" functions: drop
one into CIAdapter(watch=...) and poll_once's event recording and feed-back
decisions all carry over, so local regression and remote CI share one
failure-handling path.
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

# poll_once's plain verdict: lets the dispatcher pick the next step (green = mergeable, feedback = fix, wait = keep polling)
VERDICT_GREEN = "green"
VERDICT_FEEDBACK = "feedback"
VERDICT_WAIT = "wait"


@dataclass(frozen=True)
class CIStatus:
    """One remote pipeline query result: three states + failure log tail."""

    state: str
    log_tail: str = ""


class CIAdapter:
    """Merge request poller: remote CI red/green maps onto local events; the same state seen twice in a row is not recorded twice."""

    def __init__(self, watch: Callable[[], CIStatus]):
        self.watch = watch
        # mr_url -> state seen on the last poll; this only suppresses repeated
        # events for "the same mr staying in the same terminal state". Once a
        # pending shows up in between (a fresh CI round was pushed), the terminal
        # state landing again counts as a new result
        self._last_state: dict[str, str] = {}

    def poll_once(self, mr_url: str, event_log: EventLog) -> str:
        """Poll the remote state once and record an event: success -> ALL_GREEN; failure -> ROUND_RED (with log tail)."""
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


# ---------- real-API polling watches (detailed design §8.4; offline tests monkeypatch urlopen) ----------


def _get_json(url: str, headers: dict[str, str]) -> Any:
    """GET a JSON endpoint; HTTPError propagates to the caller as-is, to be normalized into failure."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


class GithubCIWatch:
    """GitHub Actions polling: take the latest workflow run's status/conclusion for the branch.

    completed+success -> success; completed+anything else -> failure (log_tail
    reads "run {id} conclusion={conclusion}"); not completed -> pending;
    HTTPError -> failure with the status code in log_tail. When a token is set,
    an Authorization: Bearer header goes along.
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
            return CIStatus(CI_PENDING, log_tail=f"branch {self.branch} has no workflow run")
        run = runs[0]
        if run.get("status") != "completed":
            return CIStatus(CI_PENDING, log_tail=f"run {run.get('id')} status={run.get('status')}")
        conclusion = run.get("conclusion")
        state = CI_SUCCESS if conclusion == "success" else CI_FAILURE
        return CIStatus(state, log_tail=f"run {run.get('id')} conclusion={conclusion}")


class GitlabCIWatch:
    """GitLab CI polling: take the latest pipeline's status for the ref.

    success -> success; failed/canceled -> failure; running/pending -> pending
    (any other unknown state is treated as pending, and log_tail records only the
    pipeline status, without pulling jobs); HTTPError (e.g. 404) -> failure with
    the status code in log_tail. The token travels via the PRIVATE-TOKEN header.
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
            return CIStatus(CI_PENDING, log_tail=f"ref {self.ref} has no pipeline")
        pipeline = pipelines[0]
        state = str(pipeline.get("status") or "")
        tail = f"pipeline {pipeline.get('id')} status={state}"
        if state == "success":
            return CIStatus(CI_SUCCESS, log_tail=tail)
        if state in self._FAILURE_STATES:
            return CIStatus(CI_FAILURE, log_tail=tail)
        return CIStatus(CI_PENDING, log_tail=tail)
