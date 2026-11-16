"""网页决策面：看板 + 一键拍板的极简 HTTP 服务（纯标准库）。

设计出处：design/03-详细设计.md §5（决策面：等你拍板常驻顶部、一键拍板、
默认只监听本机）与 §8.5（看板五列：待做/在做/等证据/卡住/已合并）。
写路径只有一条：POST /decide → projection.decide → event_log.append(EXPERT_DECIDED)，
台账写回由派发器消费事件完成，本模块不直接改台账。
"""
from __future__ import annotations

import html
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from superdev.events import EXPERT_DECIDED, EventLog
from superdev.projection import Projection

DEFAULT_PORT = 6010
HOST = "127.0.0.1"  # §5 第 3 点：默认只监听本机
CONTEXT_PREVIEW = 200  # /decisions 里 context_report 的截断长度

# §8.5 看板五列：任务状态 → 列的归组。
# paused 是"回原状态"的暂停态，归待做；gating 是门禁质检，仍算等证据；
# escalated 等专家拍板，从工人视角看就是卡住；done 是 merged 之后的终态。
KANBAN_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("待做", ("draft", "ready", "assigned", "paused")),
    ("在做", ("in_progress",)),
    ("等证据", ("evidence_wait", "gating")),
    ("卡住", ("stuck", "escalated")),
    ("已合并", ("merged", "done")),
)
DONE_STATUSES = ("merged", "done")  # 进度条的"done"口径

_STYLE = """\
body{font-family:sans-serif;max-width:1100px;margin:16px auto;padding:0 12px;color:#222}
.banner{background:#b45309;color:#fff;padding:10px 14px;border-radius:8px;font-size:18px}
.bar{background:#e5e7eb;border-radius:6px;height:14px;margin:6px 0 14px}
.fill{background:#16a34a;height:14px;border-radius:6px}
.kanban{display:flex;gap:10px}
.col{flex:1;background:#f3f4f6;border-radius:8px;padding:8px;min-width:0}
.col h3{margin:2px 0 8px;font-size:15px;border-bottom:2px solid #d1d5db;padding-bottom:4px}
.card{background:#fff;border:1px solid #d1d5db;border-radius:6px;padding:6px;margin-bottom:6px;
font-size:13px;word-break:break-all}
.card .status{color:#6b7280;margin-left:4px}
.red{color:#dc2626;margin-left:4px}
.decision{border:1px solid #d1d5db;border-radius:8px;padding:10px;margin:8px 0}
.decision pre{white-space:pre-wrap;background:#f9fafb;padding:6px;border-radius:6px}
"""


def _card(task: dict) -> str:
    """一张看板卡片。所有动态文本都过 html.escape，防注入。"""
    red = (
        f'<span class="red">红{task["red_count"]}</span>'
        if (task["red_count"] or 0) > 0
        else ""
    )
    engine = (
        f'<span class="engine">@{html.escape(str(task["engine"]))}</span>'
        if task["engine"]
        else ""
    )
    return (
        '<div class="card">'
        f'<b>{html.escape(str(task["task_id"]))}</b>{engine}'
        f'<span class="status">{html.escape(str(task["status"]))}</span>{red}'
        "</div>"
    )


class DecisionWeb:
    """把 Projection + EventLog 包成一个本机 HTTP 决策面。"""

    def __init__(
        self,
        projection: Projection,
        event_log: EventLog,
        ledger_plan_path: str | Path,
    ):
        self.projection = projection
        self.event_log = event_log
        # 台账路径留给派发器写回用（expert_decided → 新策略写回台账），这里只持有
        self.ledger_plan_path = Path(ledger_plan_path)
        self._db_lock = threading.Lock()
        self._swap_to_threadsafe_conn()
        self._server: ThreadingHTTPServer | None = None

    def _swap_to_threadsafe_conn(self) -> None:
        """ThreadingHTTPServer 每请求一线程，而 sqlite3 连接默认绑定创建线程。

        Projection 自建的连接带 check_same_thread=True，在请求线程里用会抛
        ProgrammingError。这里换成 check_same_thread=False 的连接（库文件里
        schema 已就绪），配合 _db_lock 串行化所有访问。
        """
        conn = sqlite3.connect(self.projection.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self.projection.conn.close()
        self.projection.conn = conn

    # ---- 数据读取 -----------------------------------------------------

    def _tasks(self) -> list[dict]:
        with self._db_lock:
            rows = self.projection.conn.execute(
                "SELECT task_id, status, red_count, engine FROM tasks ORDER BY task_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def _waiting_decisions(self) -> list[dict]:
        with self._db_lock:
            return self.projection.pending_decisions()

    def _tasks_payload(self) -> list[dict]:
        return [
            {
                "id": t["task_id"],
                "status": t["status"],
                "red_count": t["red_count"],
                "engine": t["engine"],
            }
            for t in self._tasks()
        ]

    def _decisions_payload(self) -> list[dict]:
        return [
            {
                "id": d["decision_id"],
                "question": d["question"],
                "context_report": (d["context_report"] or "")[:CONTEXT_PREVIEW],
            }
            for d in self._waiting_decisions()
        ]

    def _render_home(self) -> str:
        pending = self._waiting_decisions()
        tasks = self._tasks()
        total = len(tasks)
        done = sum(1 for t in tasks if t["status"] in DONE_STATUSES)
        pct = round(done * 100 / total) if total else 0
        column_of = {s: name for name, statuses in KANBAN_COLUMNS for s in statuses}
        columns = []
        for name, _ in KANBAN_COLUMNS:
            # 未知状态兜底归"待做"，任务不在看板上消失
            cards = "".join(
                _card(t) for t in tasks if column_of.get(t["status"], "待做") == name
            )
            columns.append(f'<div class="col"><h3>{name}</h3>{cards}</div>')
        decisions_html = "".join(
            f'<div class="decision"><b>{html.escape(str(d["decision_id"]))}</b>'
            f'<p>{html.escape(str(d["question"]))}</p>'
            f"<pre>{html.escape((d['context_report'] or '')[:CONTEXT_PREVIEW])}</pre></div>"
            for d in pending
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>superdev 决策面</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>superdev 决策面</h1>
<div class="banner">等你拍板 {len(pending)} 条</div>
<p>项目进度：{done}/{total}（{pct}%）</p>
<div class="bar"><div class="fill" style="width:{pct}%"></div></div>
<div class="kanban">{''.join(columns)}</div>
<h2>待拍板决策</h2>
{decisions_html or "<p>没有等待中的决策。</p>"}
</body>
</html>"""

    # ---- HTTP 装配 ----------------------------------------------------

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        web = self

        class Handler(BaseHTTPRequestHandler):
            # 测试和常驻进程都不需要访问日志，静默掉
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._send(200, "text/html; charset=utf-8", web._render_home())
                elif path == "/decisions":
                    self._send_json(200, web._decisions_payload())
                elif path == "/tasks":
                    self._send_json(200, web._tasks_payload())
                else:
                    self._send_json(404, {"error": f"未知路径：{path}"})

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                if path != "/decide":
                    self._send_json(404, {"error": f"未知路径：{path}"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    body: Any = json.loads(raw.decode("utf-8")) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON"})
                    return
                if not isinstance(body, dict):
                    self._send_json(400, {"ok": False, "error": "请求体应为 JSON 对象"})
                    return
                decision_id = body.get("decision_id")
                choice = body.get("choice")
                note = str(body.get("note") or "")
                if not decision_id or not choice:
                    self._send_json(
                        400, {"ok": False, "error": "缺少 decision_id 或 choice"}
                    )
                    return
                try:
                    with web._db_lock:
                        result = web.projection.decide(
                            str(decision_id), str(choice), note
                        )
                except (LookupError, ValueError) as exc:
                    # 已拍板/不存在，都是 400 + 中文原因（projection 抛的就是中文）
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                web.event_log.append(
                    EXPERT_DECIDED,
                    {
                        "decision_id": result["decision_id"],
                        "choice": result["action"],
                        "note": note,
                    },
                )
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "action": result["action"],
                        "decision_id": result["decision_id"],
                        "note": note,
                    },
                )

            def _send(self, status: int, ctype: str, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_json(self, status: int, payload: Any) -> None:
                self._send(
                    status,
                    "application/json; charset=utf-8",
                    json.dumps(payload, ensure_ascii=False),
                )

        return Handler

    def serve(self, port: int = DEFAULT_PORT, block: bool = False) -> ThreadingHTTPServer:
        """起 HTTP 服务，返回 server 对象。

        block=False（默认）在后台线程跑，调 server.shutdown() +
        server.server_close() 即停；block=True 阻塞当前线程直到 shutdown。
        port=0 让系统挑空闲端口，实际端口看 server.server_address[1]。
        """
        server = ThreadingHTTPServer((HOST, port), self._build_handler())
        server.daemon_threads = True
        self._server = server
        if block:
            try:
                server.serve_forever()
            finally:
                server.server_close()
        else:
            threading.Thread(
                target=server.serve_forever, name="decision-web", daemon=True
            ).start()
        return server
