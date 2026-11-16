"""决策面 HTTP 服务测试：真起服务器、urllib 真发请求。

覆盖 design/03-详细设计.md §5（等你拍板/一键拍板）和 §8.5（看板五列）。
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
    title: 登录页
    status: draft
    files: [a.py]
  - id: T02
    title: 接口层
    status: in_progress
    files: [b.py]
  - id: T03
    title: 收证据
    status: evidence_wait
    files: [c.py]
  - id: T04
    title: 卡住了
    status: stuck
    files: [d.py]
  - id: T05
    title: 已合并
    status: merged
    files: [e.py]
"""


def _get(url: str) -> tuple[int, str, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def _post(url: str, payload: dict | str) -> tuple[int, dict]:
    data = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture()
def site(tmp_path):
    """一份五任务台账（覆盖看板五列）+ 空事件日志 + 起在随机端口的服务。"""
    plan_path = tmp_path / "派工计划.yml"
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


# ---- 主页 ---------------------------------------------------------------


def test_homepage_ok_with_five_kanban_columns(site):
    status, body, headers = _get(site.base + "/")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    for col in ("待做", "在做", "等证据", "卡住", "已合并"):
        assert col in body
    # 五个任务各归各列：id 都在看板上
    for tid in ("T01", "T02", "T03", "T04", "T05"):
        assert tid in body


def test_homepage_pending_banner_counts(site):
    _, body, _ = _get(site.base + "/")
    assert "等你拍板 0 条" in body
    site.proj.add_decision("D1", "demo", "接口层两败，换策略还是拆微任务？", "试过 A、B")
    site.proj.add_decision("D2", "demo", "登录页用哪个库？", "试过 C")
    _, body, _ = _get(site.base + "/")
    assert "等你拍板 2 条" in body


def test_homepage_progress_and_badges(site):
    # 5 个任务里 1 个 merged → 1/5，20%
    _, body, _ = _get(site.base + "/")
    assert "1/5" in body
    assert "width:20%" in body
    # T02 记过一次红回归，卡片上要有红计数；T01 有引擎标记
    assert "红1" in body
    assert "@codex" in body


def test_homepage_escapes_unsafe_text(site):
    site.proj.add_decision("DX", "demo", "<script>alert(1)</script>", "ctx<x>")
    _, body, _ = _get(site.base + "/")
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "ctx&lt;x&gt;" in body


# ---- GET /decisions -----------------------------------------------------


def test_decisions_json_shape_and_truncation(site):
    site.proj.add_decision("D1", "demo", "换哪个引擎？", "试" * 500)
    status, body, headers = _get(site.base + "/decisions")
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert isinstance(data, list) and len(data) == 1
    item = data[0]
    assert item["id"] == "D1"
    assert item["question"] == "换哪个引擎？"
    # context_report 截断到前 200 字
    assert item["context_report"] == "试" * 200


def test_decisions_only_waiting(site):
    site.proj.add_decision("D1", "demo", "问一", "c1")
    site.proj.add_decision("D2", "demo", "问二", "c2")
    site.proj.decide("D1", "换策略")
    _, body, _ = _get(site.base + "/decisions")
    data = json.loads(body)
    assert [d["id"] for d in data] == ["D2"]


# ---- POST /decide -------------------------------------------------------


def test_decide_ok_writes_db_and_event(site):
    site.proj.add_decision("D1", "demo", "接口层两败怎么办？", "试过 A、B")
    status, body = _post(
        site.base + "/decide",
        {"decision_id": "D1", "choice": "换引擎", "note": "尽快"},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["action"] == "换引擎"
    # 落库：waiting → decided，choice 记进 decided_by
    row = site.proj.conn.execute(
        "SELECT * FROM decisions WHERE decision_id = 'D1'"
    ).fetchone()
    assert row["status"] == "decided"
    assert row["decided_by"] == "换引擎"
    assert site.proj.pending_decisions() == []
    # 事件：EXPERT_DECIDED 带决策 id、选项、备注
    assert len(site.log) == 1
    event = site.log.events()[-1]
    assert event["kind"] == EXPERT_DECIDED
    assert event["payload"]["decision_id"] == "D1"
    assert event["payload"]["choice"] == "换引擎"
    assert event["payload"]["note"] == "尽快"
    # 事件日志哈希链保持完整
    assert site.log.verify() == (True, 0)


def test_decide_note_defaults_to_empty(site):
    site.proj.add_decision("D1", "demo", "问", "c")
    status, body = _post(
        site.base + "/decide", {"decision_id": "D1", "choice": "放弃"}
    )
    assert status == 200
    assert body["note"] == ""
    assert site.log.events()[-1]["payload"]["note"] == ""


def test_decide_twice_returns_400(site):
    site.proj.add_decision("D1", "demo", "问", "c")
    status, _ = _post(site.base + "/decide", {"decision_id": "D1", "choice": "换策略"})
    assert status == 200
    status, body = _post(site.base + "/decide", {"decision_id": "D1", "choice": "放弃"})
    assert status == 400
    assert body["ok"] is False
    assert "不能重复拍板" in body["error"]


def test_decide_unknown_id_returns_400(site):
    status, body = _post(
        site.base + "/decide", {"decision_id": "NOPE", "choice": "换策略"}
    )
    assert status == 400
    assert "找不到" in body["error"]


def test_decide_missing_choice_returns_400(site):
    status, body = _post(site.base + "/decide", {"decision_id": "D1"})
    assert status == 400
    assert "缺少" in body["error"]


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


# ---- 其他路径与停服 -----------------------------------------------------


def test_unknown_path_returns_404(site):
    status, body, _ = _get(site.base + "/nope")
    assert status == 404
    assert "未知路径" in body


def test_shutdown_releases_port(tmp_path):
    plan_path = tmp_path / "派工计划.yml"
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
    # 端口已释放：能再次绑定同一个端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))
