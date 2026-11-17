"""Tests for the decision-surface HTTP service: a real server starts and urllib sends real requests.

Covers design/03-detailed-design.md §5 (decisions-awaiting banner / one-click deciding)
and §8.5 (five kanban columns).
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from superdev.events import EXPERT_DECIDED, EventLog
from superdev.projection import Projection
from superdev.web import DecisionWeb

PLAN_YAML = """\
project: demo
tasks:
  - id: T01
    title: login page
    status: draft
    files: [a.py]
  - id: T02
    title: API layer
    status: in_progress
    files: [b.py]
  - id: T03
    title: collect evidence
    status: evidence_wait
    files: [c.py]
  - id: T04
    title: stuck one
    status: stuck
    files: [d.py]
  - id: T05
    title: merged one
    status: merged
    files: [e.py]
"""


def _get(url: str, headers: dict | None = None) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def _post(url: str, payload: dict | str, headers: dict | None = None) -> tuple[int, dict]:
    data = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture()
def site(tmp_path):
    """A five-task ledger (covering all five kanban columns) + empty event log + a server on a random port."""
    plan_path = tmp_path / "plan.yml"
    plan_path.write_text(PLAN_YAML, encoding="utf-8")
    proj = Projection(tmp_path / "proj.db")
    proj.refresh_from_ledger(plan_path)
    proj.conn.execute("UPDATE tasks SET engine = 'codex' WHERE task_id = 'T01'")
    proj.conn.commit()
    proj.record_regression("T02", attempt=1, green=False, commit_sha="sha1",
                           log_tail="boom")
    log = EventLog(tmp_path / "events.jsonl")
    web = DecisionWeb(proj, log, plan_path)
    server = web.serve(port=0, block=False)
    yield SimpleNamespace(
        base=f"http://127.0.0.1:{server.server_address[1]}",
        proj=proj,
        log=log,
        server=server,
    )
    server.shutdown()
    server.server_close()
    proj.close()


@pytest.fixture()
def secure_site(tmp_path):
    """Same as site, but DecisionWeb has auth_token set (remote mode, §5 point 3)."""
    plan_path = tmp_path / "plan.yml"
    plan_path.write_text(PLAN_YAML, encoding="utf-8")
    proj = Projection(tmp_path / "proj.db")
    proj.refresh_from_ledger(plan_path)
    log = EventLog(tmp_path / "events.jsonl")
    web = DecisionWeb(proj, log, plan_path, auth_token="s3cret-t0ken")
    server = web.serve(port=0, block=False)
    yield SimpleNamespace(
        base=f"http://127.0.0.1:{server.server_address[1]}",
        token="s3cret-t0ken",
        proj=proj,
        log=log,
        server=server,
    )
    server.shutdown()
    server.server_close()
    proj.close()


# ---- home page -----------------------------------------------------------


def test_homepage_ok_with_five_kanban_columns(site):
    status, body, headers = _get(site.base + "/")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    for col in ("Todo", "In progress", "Awaiting evidence", "Stuck", "Merged"):
        assert col in body
    # the five tasks each land in their own column: every id is on the kanban
    for tid in ("T01", "T02", "T03", "T04", "T05"):
        assert tid in body


def test_homepage_pending_banner_counts(site):
    _, body, _ = _get(site.base + "/")
    assert "0 decisions awaiting you" in body
    site.proj.add_decision("D1", "demo", "API layer failed twice: switch strategy or split into micro-tasks?", "tried A, B")
    site.proj.add_decision("D2", "demo", "Which library should the login page use?", "tried C")
    _, body, _ = _get(site.base + "/")
    assert "2 decisions awaiting you" in body


def test_homepage_progress_and_badges(site):
    # 1 of 5 tasks merged -> 1/5, 20%
    _, body, _ = _get(site.base + "/")
    assert "1/5" in body
    assert "width:20%" in body
    # T02 has one red regression on record, so its card carries the red count; T01 carries the engine marker
    assert "red 1" in body
    assert "@codex" in body


def test_homepage_escapes_unsafe_text(site):
    site.proj.add_decision("DX", "demo", "<script>alert(1)</script>", "ctx<x>")
    _, body, _ = _get(site.base + "/")
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "ctx&lt;x&gt;" in body


# ---- GET /decisions -----------------------------------------------------


def test_decisions_json_shape_and_truncation(site):
    site.proj.add_decision("D1", "demo", "Which engine to switch to?", "x" * 500)
    status, body, headers = _get(site.base + "/decisions")
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert isinstance(data, list) and len(data) == 1
    item = data[0]
    assert item["id"] == "D1"
    assert item["question"] == "Which engine to switch to?"
    # context_report truncated to the first 200 characters
    assert item["context_report"] == "x" * 200


def test_decisions_only_waiting(site):
    site.proj.add_decision("D1", "demo", "question one", "c1")
    site.proj.add_decision("D2", "demo", "question two", "c2")
    site.proj.decide("D1", "switch strategy")
    _, body, _ = _get(site.base + "/decisions")
    data = json.loads(body)
    assert [d["id"] for d in data] == ["D2"]


# ---- POST /decide -------------------------------------------------------


def test_decide_ok_writes_db_and_event(site):
    site.proj.add_decision("D1", "demo", "API layer failed twice, what now?", "tried A, B")
    status, body = _post(
        site.base + "/decide",
        {"decision_id": "D1", "choice": "switch engine", "note": "asap"},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["action"] == "switch engine"
    # persisted: waiting -> decided, choice recorded in decided_by
    row = site.proj.conn.execute(
        "SELECT * FROM decisions WHERE decision_id = 'D1'"
    ).fetchone()
    assert row["status"] == "decided"
    assert row["decided_by"] == "switch engine"
    assert site.proj.pending_decisions() == []
    # event: EXPERT_DECIDED carrying the decision id, the choice, the note
    assert len(site.log) == 1
    event = site.log.events()[-1]
    assert event["kind"] == EXPERT_DECIDED
    assert event["payload"]["decision_id"] == "D1"
    assert event["payload"]["choice"] == "switch engine"
    assert event["payload"]["note"] == "asap"
    # the event log hash chain stays intact
    assert site.log.verify() == (True, 0)


def test_decide_note_defaults_to_empty(site):
    site.proj.add_decision("D1", "demo", "q", "c")
    status, body = _post(
        site.base + "/decide", {"decision_id": "D1", "choice": "abandon"}
    )
    assert status == 200
    assert body["note"] == ""
    assert site.log.events()[-1]["payload"]["note"] == ""


def test_decide_twice_returns_400(site):
    site.proj.add_decision("D1", "demo", "q", "c")
    status, _ = _post(site.base + "/decide", {"decision_id": "D1", "choice": "switch strategy"})
    assert status == 200
    status, body = _post(site.base + "/decide", {"decision_id": "D1", "choice": "abandon"})
    assert status == 400
    assert body["ok"] is False
    assert "cannot decide twice" in body["error"]


def test_decide_unknown_id_returns_400(site):
    status, body = _post(
        site.base + "/decide", {"decision_id": "NOPE", "choice": "switch strategy"}
    )
    assert status == 400
    assert "not found" in body["error"]


def test_decide_missing_choice_returns_400(site):
    status, body = _post(site.base + "/decide", {"decision_id": "D1"})
    assert status == 400
    assert "missing" in body["error"]


def test_decide_invalid_json_returns_400(site):
    status, body = _post(site.base + "/decide", "{not-json")
    assert status == 400
    assert body["ok"] is False


# ---- GET /tasks ---------------------------------------------------------


def test_tasks_json(site):
    status, body, headers = _get(site.base + "/tasks")
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert [t["id"] for t in data] == ["T01", "T02", "T03", "T04", "T05"]
    by_id = {t["id"]: t for t in data}
    assert by_id["T01"]["engine"] == "codex"
    assert by_id["T02"]["status"] == "in_progress"
    assert by_id["T02"]["red_count"] == 1
    assert by_id["T03"]["red_count"] == 0


# ---- other paths and shutdown -------------------------------------------


def test_unknown_path_returns_404(site):
    status, body, _ = _get(site.base + "/nope")
    assert status == 404
    assert "unknown path" in body


def test_shutdown_releases_port(tmp_path):
    plan_path = tmp_path / "plan.yml"
    plan_path.write_text(PLAN_YAML, encoding="utf-8")
    proj = Projection(tmp_path / "proj.db")
    web = DecisionWeb(proj, EventLog(tmp_path / "events.jsonl"), plan_path)
    server = web.serve(port=0, block=False)
    port = server.server_address[1]
    status, _, _ = _get(f"http://127.0.0.1:{port}/tasks")
    assert status == 200
    server.shutdown()
    server.server_close()
    proj.close()
    # the port is released: the same port can be bound again.
    # set SO_REUSEADDR the way a real server would — a leftover TIME_WAIT socket
    # does not count as occupancy (Linux) — but if the server never really closed
    # (an active listener still hanging on), bind still fails and the regression
    # still gets caught.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))


# ---- token auth (§5 point 3: remote access) ------------------------------


def test_auth_missing_credentials_401(secure_site):
    # with a token set, bare requests to any route get 401 + an error message
    for path in ("/", "/tasks", "/decisions"):
        status, body, _ = _get(secure_site.base + path)
        assert status == 401, path
        assert "token" in body and "authentication failed" in body


def test_auth_wrong_token_401(secure_site):
    status, body, _ = _get(
        secure_site.base + "/tasks", headers={"Authorization": "Bearer wrong"}
    )
    assert status == 401
    assert "authentication failed" in body
    status, body, _ = _get(secure_site.base + "/tasks?token=also-wrong")
    assert status == 401
    assert "authentication failed" in body


def test_auth_bearer_header_200(secure_site):
    status, body, headers = _get(
        secure_site.base + "/tasks", headers={"Authorization": f"Bearer {secure_site.token}"}
    )
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    assert "T01" in body


def test_auth_query_param_200(secure_site):
    status, body, _ = _get(secure_site.base + f"/?token={secure_site.token}")
    assert status == 200
    assert "Todo" in body  # the home page renders the kanban normally
    # the hidden token input is on the page (frontend kept simple; carrying the
    # token out is all it needs)
    assert f'value="{secure_site.token}"' in body


def test_auth_post_decide_requires_token(secure_site):
    secure_site.proj.add_decision("D1", "demo", "q", "c")
    # a credential-less POST -> 401, nothing written to the db, no event recorded
    status, body = _post(secure_site.base + "/decide", {"decision_id": "D1", "choice": "switch strategy"})
    assert status == 401
    assert body["ok"] is False
    assert len(secure_site.log) == 0
    assert secure_site.proj.pending_decisions() != []
    # a POST with the right token decides normally
    status, body = _post(
        secure_site.base + "/decide",
        {"decision_id": "D1", "choice": "switch strategy", "note": "ok"},
        headers={"Authorization": f"Bearer {secure_site.token}"},
    )
    assert status == 200
    assert body["ok"] is True
    assert len(secure_site.log) == 1


def test_auth_token_in_html_is_escaped(tmp_path):
    # a token containing HTML-special characters must still be safely escaped into the hidden input
    plan_path = tmp_path / "plan.yml"
    plan_path.write_text(PLAN_YAML, encoding="utf-8")
    proj = Projection(tmp_path / "proj.db")
    proj.refresh_from_ledger(plan_path)
    web = DecisionWeb(
        proj, EventLog(tmp_path / "events.jsonl"), plan_path, auth_token='a"b'
    )
    rendered = web._render_home()
    assert 'value="a&quot;b"' in rendered


def test_no_token_local_mode_allows_plain_requests(site):
    # no auth_token set (the default): credential-less requests still get 200, local mode behavior unchanged
    status, _, _ = _get(site.base + "/tasks")
    assert status == 200
