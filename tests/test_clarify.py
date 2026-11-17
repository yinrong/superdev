"""Tests for the requirement clarifier (§8.6): judging, one question at a time, answer merging, records on disk.

The vague-word list stays Chinese by design: the clarifier detects Chinese vague
words, so the requirement inputs in these tests remain Chinese while the option
labels, questions and record output are English.
"""

import pytest

from superdev.clarify import Clarifier, Question
from superdev.engines import EchoEngine

CLEAR_REQ = "用户登录失败连续五次后锁定账号十五分钟"


class TestNeedsClarification:
    def test_clear_requirement_needs_no_clarification(self):
        assert Clarifier().needs_clarification(CLEAR_REQ) is False

    def test_exactly_ten_chars_without_vague_word_passes(self):
        # boundary: exactly 10 characters, no vague word -> no clarification
        # (the rule is "fewer than 10")
        assert len("给报表页加上导出按钮") == 10
        assert Clarifier().needs_clarification("给报表页加上导出按钮") is False

    def test_short_requirement_needs_clarification(self):
        assert Clarifier().needs_clarification("加个登录") is True

    def test_empty_requirement_needs_clarification(self):
        assert Clarifier().needs_clarification("") is True

    @pytest.mark.parametrize("word", ["可能", "大概", "再说", "尽量", "也许", "差不多"])
    def test_each_vague_word_triggers_clarification(self, word):
        assert Clarifier().needs_clarification(f"接口{word}需要加一层重试逻辑") is True

    def test_vague_word_in_long_text_still_triggers(self):
        # a long requirement carrying a vague word still needs clarifying; length is not the only signal
        assert Clarifier().needs_clarification("订单超时未支付自动取消，取消前可能发一条提醒短信") is True


class TestNextQuestion:
    def test_question_has_two_to_four_options(self):
        q = Clarifier().next_question("登录可能要加验证码校验")
        assert isinstance(q, Question)
        assert 2 <= len(q.options) <= 4

    def test_question_targets_the_vague_word(self):
        q = Clarifier().next_question("登录可能要加验证码校验")
        assert "可能" in q.text

    def test_short_requirement_gets_goal_question(self):
        q = Clarifier().next_question("加个登录")
        assert "10" in q.text or "characters" in q.text
        assert 2 <= len(q.options) <= 4

    def test_clear_requirement_refuses_to_ask(self):
        with pytest.raises(ValueError):
            Clarifier().next_question(CLEAR_REQ)

    def test_engine_refines_question_text(self):
        engine = EchoEngine(['Does "可能" mean captcha is required, or not?'])
        q = Clarifier(engine).next_question("登录可能要加验证码校验")
        assert q.text == 'Does "可能" mean captcha is required, or not?'
        # the engine really was called once, and it got the original requirement
        assert len(engine.calls) == 1
        assert "登录可能要加验证码校验" in engine.calls[0][1]

    def test_engine_failure_falls_back_to_rule_question(self):
        # the EchoEngine script runs out and raises RuntimeError; the clarifier falls back to the rule version
        q = Clarifier(EchoEngine([])).next_question("登录可能要加验证码校验")
        assert "可能" in q.text
        assert 2 <= len(q.options) <= 4


class TestRecordAnswer:
    def test_answer_replaces_vague_word(self):
        c = Clarifier()
        q = c.next_question("尽量复用现有的工具函数")
        merged = c.record_answer(
            "尽量复用现有的工具函数", q, "must be done, part of acceptance criteria"
        )
        assert "尽量" not in merged
        assert merged == "must be done, part of acceptance criteria复用现有的工具函数"

    def test_short_requirement_answer_appended(self):
        c = Clarifier()
        q = c.next_question("加个登录")
        merged = c.record_answer("加个登录", q, "account-password login with rate limiting")
        assert merged == "加个登录 (supplement: account-password login with rate limiting)"

    def test_followup_second_question_after_partial_merge(self):
        # follow-ups are supported by design: a merged requirement that still has
        # a vague word -> keep asking the second one
        c = Clarifier()
        req = "可能大概要加日志"
        q1 = c.next_question(req)
        assert "可能" in q1.text
        req = c.record_answer(req, q1, "must be supported")
        assert c.needs_clarification(req) is True  # 大概 is still in there
        q2 = c.next_question(req)
        assert "大概" in q2.text
        req = c.record_answer(req, q2, "implement the minimum usable version")
        assert c.needs_clarification(req) is False  # fully clear after two questions

    def test_merged_answer_recorded_in_history(self):
        c = Clarifier()
        q = c.next_question("尽量复用现有的工具函数")
        c.record_answer("尽量复用现有的工具函数", q, "must be done, part of acceptance criteria")
        assert len(c.records) == 1
        assert c.records[0]["question"] == q.text
        assert c.records[0]["answer"] == "must be done, part of acceptance criteria"


class TestRecordsMarkdown:
    def test_append_records_writes_markdown(self, tmp_path):
        c = Clarifier()
        req = "登录可能要加验证码校验"
        q = c.next_question(req)
        c.record_answer(req, q, "must be supported")
        md = c.append_records(tmp_path / "notes" / "clarify.md")
        content = (tmp_path / "notes" / "clarify.md").read_text(encoding="utf-8")
        assert "## Requirement clarification log" in content
        assert '- Q: How should "可能" be defined?' in content
        assert "A: must be supported" in content
        assert "must be supported" in " / ".join(q.options)
        assert content == md  # the return value is exactly the text written

    def test_append_records_appends_not_overwrites(self, tmp_path):
        path = tmp_path / "clarify.md"
        c1 = Clarifier()
        q1 = c1.next_question("尽量复用现有的工具函数")
        c1.record_answer("尽量复用现有的工具函数", q1, "must be done, part of acceptance criteria")
        c1.append_records(path)
        c2 = Clarifier()
        q2 = c2.next_question("再说要不要加缓存")
        c2.record_answer("再说要不要加缓存", q2, "do it this iteration")
        c2.append_records(path)
        content = path.read_text(encoding="utf-8")
        assert content.count("## Requirement clarification log") == 2  # both records are still there

    def test_append_records_with_empty_history_writes_nothing(self, tmp_path):
        path = tmp_path / "clarify.md"
        assert Clarifier().append_records(path) == ""
        assert path.exists() is False
