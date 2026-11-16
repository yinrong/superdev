"""superdev.ciadapter 的测试：三态轮询、事件 payload、同状态不重复记事件。

watch 全部用假回调注入，不做任何真实 HTTP。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from superdev.ciadapter import (
    CI_FAILURE,
    CI_PENDING,
    CI_SUCCESS,
    CIAdapter,
    CIStatus,
)
from superdev.events import ALL_GREEN, ROUND_RED, EventLog

MR_URL = "https://git.example.com/superdev/-/merge_requests/7"


@dataclass
class ScriptedWatch:
    """假远端查询：按脚本弹出状态，记录调用次数；脚本演完回落 pending。"""

    states: list[str] = field(default_factory=list)
    log_tail: str = ""
    calls: int = 0

    def __call__(self) -> CIStatus:
        self.calls += 1
        state = self.states.pop(0) if self.states else CI_PENDING
        return CIStatus(state=state, log_tail=self.log_tail)


def _log(tmp_path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


# ---------- 三态 ----------


def test_success_returns_green_and_records_all_green(tmp_path):
    log = _log(tmp_path)
    watch = ScriptedWatch([CI_SUCCESS])
    adapter = CIAdapter(watch)

    assert adapter.poll_once(MR_URL, log) == "绿"

    events = log.events()
    assert [e["kind"] for e in events] == [ALL_GREEN]
    assert events[0]["payload"]["mr_url"] == MR_URL
    assert watch.calls == 1


def test_failure_returns_feed_back_and_records_round_red(tmp_path):
    log = _log(tmp_path)
    adapter = CIAdapter(ScriptedWatch([CI_FAILURE], log_tail="FAILED tests/test_a.py::test_x"))

    assert adapter.poll_once(MR_URL, log) == "回喂"

    events = log.events()
    assert [e["kind"] for e in events] == [ROUND_RED]
    payload = events[0]["payload"]
    assert payload["mr_url"] == MR_URL
    assert payload["log_tail"] == "FAILED tests/test_a.py::test_x"


def test_pending_returns_wait_without_events(tmp_path):
    log = _log(tmp_path)
    watch = ScriptedWatch([CI_PENDING, CI_PENDING])
    adapter = CIAdapter(watch)

    assert adapter.poll_once(MR_URL, log) == "等待"
    assert adapter.poll_once(MR_URL, log) == "等待"

    assert len(log) == 0  # 没落任何事件
    assert watch.calls == 2  # 但每次都真的查了远端


# ---------- 事件 payload 与截断 ----------


def test_round_red_payload_truncates_log_tail_to_200_lines(tmp_path):
    lines = [f"L{i:03d}" for i in range(1, 301)]
    log = _log(tmp_path)
    adapter = CIAdapter(ScriptedWatch([CI_FAILURE], log_tail="\n".join(lines)))

    adapter.poll_once(MR_URL, log)

    payload = log.events()[0]["payload"]
    assert payload["log_tail"].splitlines() == lines[-200:]  # 与本地回喂同一条 200 行纪律
    assert log.verify() == (True, 0)


# ---------- 连续 poll 幂等性 ----------


def test_same_terminal_state_repeat_records_event_once(tmp_path):
    log = _log(tmp_path)
    adapter = CIAdapter(ScriptedWatch([CI_SUCCESS, CI_SUCCESS, CI_SUCCESS]))

    assert adapter.poll_once(MR_URL, log) == "绿"
    assert adapter.poll_once(MR_URL, log) == "绿"
    assert adapter.poll_once(MR_URL, log) == "绿"

    assert len(log) == 1  # 同一 mr 连续绿：ALL_GREEN 只记第一条


def test_pending_gap_lets_next_terminal_state_record_again(tmp_path):
    log = _log(tmp_path)
    # 失败 → 失败（重复，不记）→ pending（新推了一轮 CI）→ 失败（新结果，要记）
    adapter = CIAdapter(ScriptedWatch([CI_FAILURE, CI_FAILURE, CI_PENDING, CI_FAILURE]))

    verdicts = [adapter.poll_once(MR_URL, log) for _ in range(4)]

    assert verdicts == ["回喂", "回喂", "等待", "回喂"]
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
