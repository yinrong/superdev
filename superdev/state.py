"""Task state machine: transition table, legality checks, and event-to-transition mapping.

Design source: design/03-detailed-design.md §3 — the transition table is owned by
the program; the AI can only propose. Every status change must first pass
the can_transition/illegal gate. "paused" is not one of the ledger's
statuses: it is a runtime overlay — pausing records the original status
and resuming transitions back to it, so paused may go to any base status.
Event kinds reuse the events.py constants (round_red_1/2 merged into
ROUND_RED; a two-failure stall is a separate TASK_STUCK, see the events.py
module docstring).
"""
from __future__ import annotations

from superdev import events as ev

# Base statuses = the ten ledger status values; paused is a runtime overlay, see module docstring
BASE_STATUSES = [
    "draft", "ready", "assigned", "in_progress", "evidence_wait",
    "stuck", "escalated", "gating", "merged", "done",
]
ALL_STATUSES = BASE_STATUSES + ["paused"]

# Complete transition table of the §3.1 state diagram; the paused row means "resume to the original status"
ALLOWED: dict[str, set[str]] = {
    "draft": {"ready", "paused"},
    "ready": {"assigned", "paused"},
    "assigned": {"in_progress", "paused"},
    "in_progress": {"evidence_wait", "stuck", "paused"},
    "evidence_wait": {"gating", "stuck", "paused"},
    "stuck": {"ready", "escalated", "paused"},
    "escalated": {"ready", "paused"},
    "gating": {"merged", "paused"},
    "merged": {"done", "paused"},
    "done": {"paused"},
    "paused": set(BASE_STATUSES),
}


def can_transition(cur: str, nxt: str) -> bool:
    """Whether cur may transition directly to nxt. Unknown statuses are never allowed."""
    return nxt in ALLOWED.get(cur, set())


def illegal(cur: str, nxt: str) -> str | None:
    """Return the reason the transition is illegal, or None if it is legal."""
    if cur not in ALLOWED:
        return f"Unknown status '{cur}', valid statuses: {', '.join(ALL_STATUSES)}"
    if nxt not in ALLOWED:
        return f"Unknown status '{nxt}', valid statuses: {', '.join(ALL_STATUSES)}"
    if nxt not in ALLOWED[cur]:
        targets = ", ".join(sorted(ALLOWED[cur]))
        return f"Transition from {cur} to {nxt} is not allowed (valid targets from {cur}: {targets})"
    return None


# §3.2 transition trigger table: kind -> (required current status, target status, trigger action)
# ROUND_RED is special: first failure / rollback does not transition (from == to);
# a two-failure stall goes to stuck via TASK_STUCK
EVENT_RULES: dict[str, tuple[str, str, str]] = {
    ev.TASK_CREATED: ("draft", "ready", "refresh projection after checks pass"),
    ev.WORKER_IDLE: ("ready", "assigned", "dispatcher assigns the task"),
    ev.ROUND_GREEN: ("in_progress", "evidence_wait", "collect evidence, notify the gate"),
    ev.ROUND_RED: ("in_progress", "in_progress", "roll back and re-feed"),
    ev.TASK_STUCK: ("in_progress", "stuck", "trigger post-mortem research"),
    ev.REPRO_SPLIT: ("stuck", "ready", "generate repro-script contract"),
    ev.EXPERT_DECIDED: ("escalated", "ready", "write new strategy back to the ledger"),
    ev.ALL_GREEN: ("gating", "merged", "merge to main, update the green point"),
}


def handle_event(kind: str, task_status: str) -> tuple[str | None, str]:
    """Map an event to (new status, action name).

    On success returns (new status, action). When the event does not match
    the status, or the kind is unknown, returns (None, error reason) with
    the same wording as illegal(). METRICS_STALL is a project-level event
    (last row of §3.2): it does not transition any task status, only
    bumps a project counter; two triggers pause dispatch and escalate.
    """
    if kind == ev.METRICS_STALL:
        if task_status not in ALLOWED:
            return None, illegal(task_status, task_status) or ""
        return task_status, "project-level count; two triggers pause dispatch and escalate"
    if kind not in EVENT_RULES:
        kinds = ", ".join(list(EVENT_RULES) + [ev.METRICS_STALL])
        return None, f"Unknown event kind '{kind}', valid kinds: {kinds}"
    want, to, action = EVENT_RULES[kind]
    if task_status != want:
        return None, f"Event {kind} requires the task to be in status {want}, got {task_status}"
    return to, action
