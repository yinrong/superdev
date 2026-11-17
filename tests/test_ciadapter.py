"""Tests for superdev.ciadapter: three-state polling, event payloads, no duplicate events for a repeated state.

All watches are injected as fake callbacks, so no real HTTP happens; the
GithubCIWatch/GitlabCIWatch cases likewise monkeypatch urllib.request.urlopen
with fake JSON, fully offline.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field

from superdev.ciadapter import (
    CI_FAILURE,
    CI_PENDING,
    CI_SUCCESS,
    CIAdapter,
    CIStatus,
    GithubCIWatch,
    GitlabCIWatch,
)
from superdev.events import ALL_GREEN, ROUND_RED, EventLog

MR_URL = "https://git.example.com/superdev/-/merge_requests/7"


@dataclass
class ScriptedWatch:
    """Fake remote query: pops states from a script and counts calls; falls back to pending when the script runs out."""

    states: list[str] = field(default_factory=list)
    log_tail: str = ""
    calls: int = 0

    def __call__(self) -> CIStatus:
        self.calls += 1
        state = self.states.pop(0) if self.states else CI_PENDING
        return CIStatus(state=state, log_tail=self.log_tail)


def _log(tmp_path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


# ---------- the three states ----------


def test_success_returns_green_and_records_all_green(tmp_path):
    log = _log(tmp_path)
    watch = ScriptedWatch([CI_SUCCESS])
    adapter = CIAdapter(watch)

    assert adapter.poll_once(MR_URL, log) == "green"

    events = log.events()
    assert [e["kind"] for e in events] == [ALL_GREEN]
    assert events[0]["payload"]["mr_url"] == MR_URL
    assert watch.calls == 1


def test_failure_returns_feed_back_and_records_round_red(tmp_path):
    log = _log(tmp_path)
    adapter = CIAdapter(ScriptedWatch([CI_FAILURE], log_tail="FAILED tests/test_a.py::test_x"))

    assert adapter.poll_once(MR_URL, log) == "feedback"

    events = log.events()
    assert [e["kind"] for e in events] == [ROUND_RED]
    payload = events[0]["payload"]
    assert payload["mr_url"] == MR_URL
    assert payload["log_tail"] == "FAILED tests/test_a.py::test_x"


def test_pending_returns_wait_without_events(tmp_path):
    log = _log(tmp_path)
    watch = ScriptedWatch([CI_PENDING, CI_PENDING])
    adapter = CIAdapter(watch)

    assert adapter.poll_once(MR_URL, log) == "wait"
    assert adapter.poll_once(MR_URL, log) == "wait"

    assert len(log) == 0  # no events recorded
    assert watch.calls == 2  # but the remote really was queried each time


# ---------- event payload and truncation ----------


def test_round_red_payload_truncates_log_tail_to_200_lines(tmp_path):
    lines = [f"L{i:03d}" for i in range(1, 301)]
    log = _log(tmp_path)
    adapter = CIAdapter(ScriptedWatch([CI_FAILURE], log_tail="\n".join(lines)))

    adapter.poll_once(MR_URL, log)

    payload = log.events()[0]["payload"]
    assert payload["log_tail"].splitlines() == lines[-200:]  # same 200-line discipline as local feed-back
    assert log.verify() == (True, 0)


# ---------- idempotence across repeated polls ----------


def test_same_terminal_state_repeat_records_event_once(tmp_path):
    log = _log(tmp_path)
    adapter = CIAdapter(ScriptedWatch([CI_SUCCESS, CI_SUCCESS, CI_SUCCESS]))

    assert adapter.poll_once(MR_URL, log) == "green"
    assert adapter.poll_once(MR_URL, log) == "green"
    assert adapter.poll_once(MR_URL, log) == "green"

    assert len(log) == 1  # the same mr staying green: ALL_GREEN recorded only once


def test_pending_gap_lets_next_terminal_state_record_again(tmp_path):
    log = _log(tmp_path)
    # failure -> failure (duplicate, not recorded) -> pending (a fresh CI round was
    # pushed) -> failure (a new result, must be recorded)
    adapter = CIAdapter(ScriptedWatch([CI_FAILURE, CI_FAILURE, CI_PENDING, CI_FAILURE]))

    verdicts = [adapter.poll_once(MR_URL, log) for _ in range(4)]

    assert verdicts == ["feedback", "feedback", "wait", "feedback"]
    kinds = [e["kind"] for e in log.events()]
    assert kinds == [ROUND_RED, ROUND_RED]


def test_different_mr_urls_record_independently(tmp_path):
    log = _log(tmp_path)
    other_url = "https://git.example.com/superdev/-/merge_requests/8"
    adapter = CIAdapter(ScriptedWatch([CI_SUCCESS, CI_SUCCESS]))

    adapter.poll_once(MR_URL, log)
    adapter.poll_once(other_url, log)

    events = log.events()
    assert [e["kind"] for e in events] == [ALL_GREEN, ALL_GREEN]
    assert [e["payload"]["mr_url"] for e in events] == [MR_URL, other_url]


# ---------- real-API watches (urlopen monkeypatched, fully offline) ----------


class _FakeResponse:
    """Fake HTTP response: implements only read + context management, as used by urlopen's result."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_urlopen(monkeypatch, payload=None, error=None, captured=None):
    def fake(req, timeout=None):
        if captured is not None:
            captured.append(req)
        if error is not None:
            raise error
        return _FakeResponse(payload)

    monkeypatch.setattr("superdev.ciadapter.urllib.request.urlopen", fake)


def _header(req, name) -> str | None:
    # urllib capitalizes header keys; assertions look them up case-insensitively
    for k, v in req.headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _gh_runs(run_id, status, conclusion=None):
    return {"workflow_runs": [{"id": run_id, "status": status, "conclusion": conclusion}]}


def _gl_pipeline(pipeline_id, status):
    return [{"id": pipeline_id, "status": status}]


# ---------- GithubCIWatch ----------


def test_github_completed_success(monkeypatch):
    _install_urlopen(monkeypatch, payload=_gh_runs(101, "completed", "success"))
    result = GithubCIWatch("owner/repo", "main").check()
    assert result.state == CI_SUCCESS
    assert "conclusion=success" in result.log_tail


def test_github_completed_other_conclusion_is_failure(monkeypatch):
    _install_urlopen(monkeypatch, payload=_gh_runs(4242, "completed", "failure"))
    result = GithubCIWatch("owner/repo", "main").check()
    assert result.state == CI_FAILURE
    assert result.log_tail == "run 4242 conclusion=failure"


def test_github_not_completed_is_pending(monkeypatch):
    _install_urlopen(monkeypatch, payload=_gh_runs(102, "in_progress"))
    result = GithubCIWatch("owner/repo", "main", api_base="https://gh.example.com/api/v3").check()
    assert result.state == CI_PENDING
    assert "in_progress" in result.log_tail


def test_github_token_sends_bearer_header_and_full_url(monkeypatch):
    captured = []
    _install_urlopen(monkeypatch, payload=_gh_runs(103, "completed", "success"), captured=captured)
    GithubCIWatch("owner/repo", "feat/x", token="tok123").check()
    (req,) = captured
    assert _header(req, "Authorization") == "Bearer tok123"
    assert req.full_url == (
        "https://api.github.com/repos/owner/repo/actions/runs?branch=feat%2Fx&per_page=1"
    )


def test_github_without_token_sends_no_auth_header(monkeypatch):
    captured = []
    _install_urlopen(monkeypatch, payload=_gh_runs(104, "completed", "success"), captured=captured)
    GithubCIWatch("owner/repo", "main").check()
    assert _header(captured[0], "Authorization") is None


def test_github_http_error_is_failure_with_status_code(monkeypatch):
    err = urllib.error.HTTPError("https://api.github.com/x", 500, "boom", None, None)
    _install_urlopen(monkeypatch, error=err)
    result = GithubCIWatch("owner/repo", "main").check()
    assert result.state == CI_FAILURE
    assert "500" in result.log_tail


# ---------- GitlabCIWatch ----------


def test_gitlab_success(monkeypatch):
    _install_urlopen(monkeypatch, payload=_gl_pipeline(9, "success"))
    result = GitlabCIWatch(123, "main").check()
    assert result.state == CI_SUCCESS
    assert result.log_tail == "pipeline 9 status=success"


def test_gitlab_failed_states_are_failure(monkeypatch):
    for state in ("failed", "canceled"):
        _install_urlopen(monkeypatch, payload=_gl_pipeline(10, state))
        result = GitlabCIWatch(123, "main").check()
        assert result.state == CI_FAILURE, state
        assert f"status={state}" in result.log_tail


def test_gitlab_running_and_pending_are_pending(monkeypatch):
    for state in ("running", "pending"):
        _install_urlopen(monkeypatch, payload=_gl_pipeline(11, state))
        result = GitlabCIWatch(123, "main").check()
        assert result.state == CI_PENDING, state


def test_gitlab_404_is_failure(monkeypatch):
    err = urllib.error.HTTPError("https://gitlab.example.com/api/v4/x", 404, "missing", None, None)
    _install_urlopen(monkeypatch, error=err)
    result = GitlabCIWatch(
        "group/proj", "main", base_url="https://gitlab.example.com/api/v4", token="gltok"
    ).check()
    assert result.state == CI_FAILURE
    assert "404" in result.log_tail


def test_gitlab_token_sends_private_token_header(monkeypatch):
    captured = []
    _install_urlopen(monkeypatch, payload=_gl_pipeline(12, "success"), captured=captured)
    GitlabCIWatch(123, "main", token="gltok").check()
    (req,) = captured
    assert _header(req, "PRIVATE-TOKEN") == "gltok"
    assert req.full_url == "https://gitlab.com/api/v4/projects/123/pipelines?ref=main&per_page=1"


# ---------- both watches wired into CIAdapter.poll_once end to end ----------


def test_adapter_with_github_watch_success_records_all_green(monkeypatch, tmp_path):
    _install_urlopen(monkeypatch, payload=_gh_runs(201, "completed", "success"))
    log = _log(tmp_path)
    adapter = CIAdapter(GithubCIWatch("owner/repo", "main").check)

    assert adapter.poll_once(MR_URL, log) == "green"

    events = log.events()
    assert [e["kind"] for e in events] == [ALL_GREEN]
    assert events[0]["payload"]["mr_url"] == MR_URL


def test_adapter_with_gitlab_watch_failure_records_round_red(monkeypatch, tmp_path):
    _install_urlopen(monkeypatch, payload=_gl_pipeline(301, "failed"))
    log = _log(tmp_path)
    adapter = CIAdapter(GitlabCIWatch(123, "main").check)

    assert adapter.poll_once(MR_URL, log) == "feedback"

    events = log.events()
    assert [e["kind"] for e in events] == [ROUND_RED]
    assert "pipeline 301 status=failed" in events[0]["payload"]["log_tail"]
