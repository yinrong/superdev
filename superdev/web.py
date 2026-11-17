"""Web decision surface: kanban + one-click deciding, a minimal HTTP service (pure standard library).

Design source: design/03-detailed-design.md §5 (decision surface: "decisions awaiting
you" pinned at the top, one-click deciding, localhost-only by default) and §8.5
(five kanban columns: Todo / In progress / Awaiting evidence / Stuck / Merged).
There is exactly one write path: POST /decide -> projection.decide ->
event_log.append(EXPERT_DECIDED); writing back to the ledger is done by the
dispatcher consuming the event — this module never edits the ledger directly.
"""
from __future__ import annotations

import hmac
import html
import json
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from superdev.events import EXPERT_DECIDED, EventLog
from superdev.projection import Projection

DEFAULT_PORT = 6010
HOST = "127.0.0.1"  # §5 point 3: localhost-only by default
CONTEXT_PREVIEW = 200  # truncation length for context_report in /decisions

# §8.5 five kanban columns: task status -> column grouping.
# paused is a "back to the original status" pause state, grouped into Todo;
# gating is quality-gate checking and still counts as Awaiting evidence;
# escalated waits for an expert call, which from the worker's viewpoint is Stuck;
# done is the terminal state after merged.
KANBAN_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Todo", ("draft", "ready", "assigned", "paused")),
    ("In progress", ("in_progress",)),
    ("Awaiting evidence", ("evidence_wait", "gating")),
    ("Stuck", ("stuck", "escalated")),
    ("Merged", ("merged", "done")),
)
DONE_STATUSES = ("merged", "done")  # what the progress bar counts as "done"

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
    """One kanban card. All dynamic text goes through html.escape to block injection."""
    red = (
        f'<span class="red">red {task["red_count"]}</span>'
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
    """Wraps Projection + EventLog into a localhost HTTP decision surface."""

    def __init__(
        self,
        projection: Projection,
        event_log: EventLog,
        ledger_plan_path: str | Path,
        auth_token: str | None = None,
    ):
        self.projection = projection
        self.event_log = event_log
        # the ledger path is kept for the dispatcher's write-back
        # (expert_decided -> new strategy written back to the ledger); here we only hold it
        self.ledger_plan_path = Path(ledger_plan_path)
        # §5 point 3: remote access adds a token; None (the default) = local mode, behavior fully unchanged
        self.auth_token = auth_token
        self._db_lock = threading.Lock()
        self._swap_to_threadsafe_conn()
        self._server: ThreadingHTTPServer | None = None

    def _swap_to_threadsafe_conn(self) -> None:
        """ThreadingHTTPServer uses one thread per request, while a sqlite3 connection binds to its creating thread by default.

        Projection's self-built connection carries check_same_thread=True and
        would raise ProgrammingError if used on a request thread. Swap it for a
        check_same_thread=False connection (the schema is already in place in
        the db file) and serialize all access with _db_lock.
        """
        conn = sqlite3.connect(self.projection.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self.projection.conn.close()
        self.projection.conn = conn

    # ---- data reads -----------------------------------------------------

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
            # unknown statuses fall back to Todo so a task never vanishes from the kanban
            cards = "".join(
                _card(t) for t in tasks if column_of.get(t["status"], "Todo") == name
            )
            columns.append(f'<div class="col"><h3>{name}</h3>{cards}</div>')
        decisions_html = "".join(
            f'<div class="decision"><b>{html.escape(str(d["decision_id"]))}</b>'
            f'<p>{html.escape(str(d["question"]))}</p>'
            f"<pre>{html.escape((d['context_report'] or '')[:CONTEXT_PREVIEW])}</pre></div>"
            for d in pending
        )
        # in token mode, hide the token on the home page so the frontend can build
        # ?token= links (passing auth is all that matters)
        token_input = (
            f'<input type="hidden" id="auth-token" value="{html.escape(self.auth_token)}">'
            if self.auth_token
            else ""
        )
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>superdev decision console</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>superdev decision console</h1>
<div class="banner">{len(pending)} decisions awaiting you</div>
<p>Project progress: {done}/{total} ({pct}%)</p>
<div class="bar"><div class="fill" style="width:{pct}%"></div></div>
<div class="kanban">{''.join(columns)}</div>
<h2>Pending decisions</h2>
{token_input}
{decisions_html or "<p>No decisions waiting.</p>"}
</body>
</html>"""

    # ---- HTTP assembly ----------------------------------------------------

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        web = self

        class Handler(BaseHTTPRequestHandler):
            # neither tests nor the long-running process need access logs; silence them
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _authorized(self) -> bool:
                """In token mode every route checks credentials first (§5 point 3); in local mode everything passes.

                Credentials come in one of two forms: an Authorization: Bearer
                <token> header, or a ?token=<token> query parameter.
                compare_digest guards against timing side channels. A mismatch
                returns 401 + an error message.
                """
                if web.auth_token is None:
                    return True
                candidates: list[str] = []
                auth = self.headers.get("Authorization") or ""
                if auth.startswith("Bearer "):
                    candidates.append(auth[len("Bearer "):].strip())
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                candidates.extend(query.get("token", []))
                for token in candidates:
                    if token and hmac.compare_digest(token, web.auth_token):
                        return True
                self._send_json(
                    401,
                    {
                        "ok": False,
                        "error": "authentication failed: token missing or incorrect"
                        " (Authorization: Bearer <token> or ?token=<token>)",
                    },
                )
                return False

            def do_GET(self) -> None:
                if not self._authorized():
                    return
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._send(200, "text/html; charset=utf-8", web._render_home())
                elif path == "/decisions":
                    self._send_json(200, web._decisions_payload())
                elif path == "/tasks":
                    self._send_json(200, web._tasks_payload())
                else:
                    self._send_json(404, {"error": f"unknown path: {path}"})

            def do_POST(self) -> None:
                if not self._authorized():
                    return
                path = self.path.split("?", 1)[0]
                if path != "/decide":
                    self._send_json(404, {"error": f"unknown path: {path}"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    body: Any = json.loads(raw.decode("utf-8")) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "error": "request body is not valid JSON"})
                    return
                if not isinstance(body, dict):
                    self._send_json(400, {"ok": False, "error": "request body must be a JSON object"})
                    return
                decision_id = body.get("decision_id")
                choice = body.get("choice")
                note = str(body.get("note") or "")
                if not decision_id or not choice:
                    self._send_json(
                        400, {"ok": False, "error": "missing decision_id or choice"}
                    )
                    return
                try:
                    with web._db_lock:
                        result = web.projection.decide(
                            str(decision_id), str(choice), note
                        )
                except (LookupError, ValueError) as exc:
                    # already decided / nonexistent: both return 400 + the reason
                    # (projection's message is passed through as-is)
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
        """Start the HTTP service and return the server object.

        block=False (the default) runs it on a background thread; call
        server.shutdown() + server.server_close() to stop it. block=True blocks
        the current thread until shutdown. port=0 lets the system pick a free
        port; the actual port is server.server_address[1].
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
