"""Tests for superdev.state: transition table, illegal transitions, paused round-trips, event-to-transition mapping."""

import pytest

from superdev import events as ev
from superdev import state
from superdev.state import ALLOWED, can_transition, handle_event, illegal

# §3.1 mainline: one complete path from draft to done
HAPPY_PATH = [
    ("draft", "ready"),
    ("ready", "assigned"),
    ("assigned", "in_progress"),
    ("in_progress", "evidence_wait"),
    ("evidence_wait", "gating"),
    ("gating", "merged"),
    ("merged", "done"),
]


class TestAllowedTransitions:
    def test_happy_path(self):
        for cur, nxt in HAPPY_PATH:
            assert can_transition(cur, nxt), f"{cur}->{nxt} should be legal"

    def test_in_progress_branches(self):
        # Two-failure stall and pause both leave from in_progress, but skipping the gate to merge is forbidden
        assert can_transition("in_progress", "stuck")
        assert can_transition("in_progress", "paused")
        assert not can_transition("in_progress", "merged")
        assert not can_transition("in_progress", "ready")

    def test_evidence_wait_branches(self):
        # Good evidence enters the gate, bad evidence returns to stuck; neither goes straight back to ready
        assert can_transition("evidence_wait", "gating")
        assert can_transition("evidence_wait", "stuck")
        assert not can_transition("evidence_wait", "ready")

    def test_stuck_and_escalated_paths(self):
        assert can_transition("stuck", "ready")        # back to work via repro_split
        assert can_transition("stuck", "escalated")    # manager judges it beyond capability
        assert can_transition("escalated", "ready")    # expert decision returns it with a new strategy
        assert not can_transition("stuck", "in_progress")
        assert not can_transition("escalated", "stuck")

    @pytest.mark.parametrize("cur,nxt", [
        ("draft", "assigned"),        # wants dispatch without going through ready
        ("ready", "in_progress"),     # wants to start without going through assigned
        ("gating", "done"),           # must merge first
        ("merged", "ready"),          # no rework after merge
        ("done", "draft"),            # terminal status can only pause, not restart
        ("paused", "paused"),         # resume must return to a real original status
        ("draft", "draft"),           # self-transition is meaningless
    ])
    def test_illegal_samples(self, cur, nxt):
        assert not can_transition(cur, nxt)
        assert illegal(cur, nxt) is not None

    def test_unknown_status_rejected(self):
        assert not can_transition("bogus", "ready")
        assert not can_transition("ready", "bogus")
        assert "Unknown status" in illegal("bogus", "ready")

    def test_illegal_message_names_both_ends_and_targets(self):
        msg = illegal("draft", "merged")
        assert "draft" in msg and "merged" in msg
        # The error must list valid targets so the manager never has to check the docs
        assert "ready" in msg


class TestPaused:
    def test_every_base_status_can_pause(self):
        for s in state.BASE_STATUSES:
            assert can_transition(s, "paused"), f"{s} should be pausable"

    def test_paused_returns_to_every_base_status(self):
        for s in state.BASE_STATUSES:
            assert can_transition("paused", s), f"resume should reach {s}"

    def test_pause_roundtrip_via_illegal(self):
        assert illegal("in_progress", "paused") is None
        assert illegal("paused", "in_progress") is None

    def test_paused_not_in_ledger_enumeration(self):
        # paused is a runtime overlay, never listed in ledger statuses (ledger.DEFAULT_STATUSES)
        assert "paused" not in ALLOWED["paused"]
        assert set(ALLOWED) == set(state.ALL_STATUSES)


class TestHandleEvent:
    def test_all_rules_match_status_and_action(self):
        for kind, (want, to, action) in state.EVENT_RULES.items():
            new_status, act = handle_event(kind, want)
            assert new_status == to
            assert act == action

    def test_every_rule_transition_is_in_allowed_table(self):
        # Event mappings must not bypass ALLOWED (ROUND_RED is a "no transition", from==to, not table-checked)
        for kind, (want, to, _action) in state.EVENT_RULES.items():
            if want != to:
                assert can_transition(want, to), f"{kind}'s {want}->{to} is not in the transition table"

    def test_round_red_stays_in_progress(self):
        # round_red_1 semantics: roll back and re-feed only, no transition (two failures send TASK_STUCK separately)
        assert handle_event(ev.ROUND_RED, "in_progress") == ("in_progress", "roll back and re-feed")

    def test_metrics_stall_is_project_level(self):
        for s in ("in_progress", "gating", "paused"):
            new_status, action = handle_event(ev.METRICS_STALL, s)
            assert new_status == s  # project-level event; task status is untouched
            assert "project-level" in action

    def test_wrong_status_returns_error(self):
        new_status, err = handle_event(ev.WORKER_IDLE, "draft")
        assert new_status is None
        assert "ready" in err  # the error must name the status the event requires

    def test_unknown_kind_returns_error(self):
        new_status, err = handle_event("NOT_A_EVENT", "draft")
        assert new_status is None
        assert "Unknown event kind" in err
