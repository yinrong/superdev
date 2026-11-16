"""superdev.state 的测试：迁移表、非法迁移、paused 往返、事件到迁移映射。"""

import pytest

from superdev import events as ev
from superdev import state
from superdev.state import ALLOWED, can_transition, handle_event, illegal

# §3.1 主干：一条从 draft 走到 done 的完整路径
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
            assert can_transition(cur, nxt), f"{cur}->{nxt} 应合法"

    def test_in_progress_branches(self):
        # 两败卡住与暂停都从 in_progress 出发，但不许跳过门禁直接合并
        assert can_transition("in_progress", "stuck")
        assert can_transition("in_progress", "paused")
        assert not can_transition("in_progress", "merged")
        assert not can_transition("in_progress", "ready")

    def test_evidence_wait_branches(self):
        # 证据合格进门禁，不合格退回 stuck，都不许直接回 ready
        assert can_transition("evidence_wait", "gating")
        assert can_transition("evidence_wait", "stuck")
        assert not can_transition("evidence_wait", "ready")

    def test_stuck_and_escalated_paths(self):
        assert can_transition("stuck", "ready")        # 经 repro_split 回炉
        assert can_transition("stuck", "escalated")    # 管理端判断超出能力
        assert can_transition("escalated", "ready")    # 专家拍板带新策略回炉
        assert not can_transition("stuck", "in_progress")
        assert not can_transition("escalated", "stuck")

    @pytest.mark.parametrize("cur,nxt", [
        ("draft", "assigned"),        # 没经过 ready 就想派工
        ("ready", "in_progress"),     # 没经过 assigned 就想开工
        ("gating", "done"),           # 必须先 merged
        ("merged", "ready"),          # 合并后不许回炉
        ("done", "draft"),            # 终态只能暂停，不能重来
        ("paused", "paused"),         # 恢复必须回一个真实原状态
        ("draft", "draft"),           # 自迁移无意义
    ])
    def test_illegal_samples(self, cur, nxt):
        assert not can_transition(cur, nxt)
        assert illegal(cur, nxt) is not None

    def test_unknown_status_rejected(self):
        assert not can_transition("bogus", "ready")
        assert not can_transition("ready", "bogus")
        assert "未知状态" in illegal("bogus", "ready")

    def test_illegal_message_names_both_ends_and_targets(self):
        msg = illegal("draft", "merged")
        assert "draft" in msg and "merged" in msg
        # 错误信息要给出合法目标，管理端才不用翻文档
        assert "ready" in msg


class TestPaused:
    def test_every_base_status_can_pause(self):
        for s in state.BASE_STATUSES:
            assert can_transition(s, "paused"), f"{s} 应能暂停"

    def test_paused_returns_to_every_base_status(self):
        for s in state.BASE_STATUSES:
            assert can_transition("paused", s), f"恢复应能回 {s}"

    def test_pause_roundtrip_via_illegal(self):
        assert illegal("in_progress", "paused") is None
        assert illegal("paused", "in_progress") is None

    def test_paused_not_in_ledger_enumeration(self):
        # paused 是运行期叠加态，不进台账 statuses（ledger.DEFAULT_STATUSES）
        assert "paused" not in ALLOWED["paused"]
        assert set(ALLOWED) == set(state.ALL_STATUSES)


class TestHandleEvent:
    def test_all_rules_match_status_and_action(self):
        for kind, (want, to, action) in state.EVENT_RULES.items():
            new_status, act = handle_event(kind, want)
            assert new_status == to
            assert act == action

    def test_every_rule_transition_is_in_allowed_table(self):
        # 事件映射不得绕过 ALLOWED（ROUND_RED 是"不迁移"，from==to 不查表）
        for kind, (want, to, _action) in state.EVENT_RULES.items():
            if want != to:
                assert can_transition(want, to), f"{kind} 的 {want}->{to} 不在迁移表"

    def test_round_red_stays_in_progress(self):
        # round_red_1 口径：只回滚回喂，不迁移（两败另发 TASK_STUCK）
        assert handle_event(ev.ROUND_RED, "in_progress") == ("in_progress", "回滚回喂")

    def test_metrics_stall_is_project_level(self):
        for s in ("in_progress", "gating", "paused"):
            new_status, action = handle_event(ev.METRICS_STALL, s)
            assert new_status == s  # 项目级事件，任务状态不动
            assert "项目级" in action

    def test_wrong_status_returns_error(self):
        new_status, err = handle_event(ev.WORKER_IDLE, "draft")
        assert new_status is None
        assert "ready" in err  # 错误要说出事件要求的状态

    def test_unknown_kind_returns_error(self):
        new_status, err = handle_event("NOT_A_EVENT", "draft")
        assert new_status is None
        assert "未知事件类型" in err
