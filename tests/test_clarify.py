"""需求澄清器（§8.6）的测试：判定、一次一问、答案合并、记录落盘。"""

import pytest

from superdev.clarify import Clarifier, Question
from superdev.engines import EchoEngine

CLEAR_REQ = "用户登录失败连续五次后锁定账号十五分钟"


class TestNeedsClarification:
    def test_clear_requirement_needs_no_clarification(self):
        assert Clarifier().needs_clarification(CLEAR_REQ) is False

    def test_exactly_ten_chars_without_vague_word_passes(self):
        # 边界：恰好 10 个字、无模糊词 → 不澄清（规则是"少于 10"）
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
        # 长需求带模糊词同样要澄清，不是只看长度
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
        assert "10" in q.text or "字" in q.text
        assert 2 <= len(q.options) <= 4

    def test_clear_requirement_refuses_to_ask(self):
        with pytest.raises(ValueError):
            Clarifier().next_question(CLEAR_REQ)

    def test_engine_refines_question_text(self):
        engine = EchoEngine(["「可能」到底支不支持验证码？"])
        q = Clarifier(engine).next_question("登录可能要加验证码校验")
        assert q.text == "「可能」到底支不支持验证码？"
        # 引擎确实被叫过一次，且拿到了原始需求
        assert len(engine.calls) == 1
        assert "登录可能要加验证码校验" in engine.calls[0][1]

    def test_engine_failure_falls_back_to_rule_question(self):
        # EchoEngine 脚本演完会抛 RuntimeError，澄清器应回退规则式
        q = Clarifier(EchoEngine([])).next_question("登录可能要加验证码校验")
        assert "可能" in q.text
        assert 2 <= len(q.options) <= 4


class TestRecordAnswer:
    def test_answer_replaces_vague_word(self):
        c = Clarifier()
        q = c.next_question("尽量复用现有的工具函数")
        merged = c.record_answer("尽量复用现有的工具函数", q, "必须做到，进验收标准")
        assert "尽量" not in merged
        assert "必须做到，进验收标准复用现有的工具函数" == merged

    def test_short_requirement_answer_appended(self):
        c = Clarifier()
        q = c.next_question("加个登录")
        merged = c.record_answer("加个登录", q, "支持账号密码登录并限流")
        assert merged == "加个登录（补充：支持账号密码登录并限流）"

    def test_followup_second_question_after_partial_merge(self):
        # 设计支持追问：合并后仍有模糊词 → 继续问第二个
        c = Clarifier()
        req = "可能大概要加日志"
        q1 = c.next_question(req)
        assert "可能" in q1.text
        req = c.record_answer(req, q1, "必须支持")
        assert c.needs_clarification(req) is True  # 还剩「大概」
        q2 = c.next_question(req)
        assert "大概" in q2.text
        req = c.record_answer(req, q2, "按最小可用实现")
        assert c.needs_clarification(req) is False  # 两问之后彻底清晰

    def test_merged_answer_recorded_in_history(self):
        c = Clarifier()
        q = c.next_question("尽量复用现有的工具函数")
        c.record_answer("尽量复用现有的工具函数", q, "必须做到，进验收标准")
        assert len(c.records) == 1
        assert c.records[0]["question"] == q.text
        assert c.records[0]["answer"] == "必须做到，进验收标准"


class TestRecordsMarkdown:
    def test_append_records_writes_markdown(self, tmp_path):
        c = Clarifier()
        req = "登录可能要加验证码校验"
        q = c.next_question(req)
        c.record_answer(req, q, "必须支持")
        md = c.append_records(tmp_path / "notes" / "clarify.md")
        content = (tmp_path / "notes" / "clarify.md").read_text(encoding="utf-8")
        assert "## 需求澄清记录" in content
        assert "问：「可能」如何界定？" in content
        assert "答：必须支持" in content
        assert "必须支持" in " / ".join(q.options)
        assert content == md  # 返回值就是写入的文本

    def test_append_records_appends_not_overwrites(self, tmp_path):
        path = tmp_path / "clarify.md"
        c1 = Clarifier()
        q1 = c1.next_question("尽量复用现有的工具函数")
        c1.record_answer("尽量复用现有的工具函数", q1, "必须做到，进验收标准")
        c1.append_records(path)
        c2 = Clarifier()
        q2 = c2.next_question("再说要不要加缓存")
        c2.record_answer("再说要不要加缓存", q2, "本期就要做")
        c2.append_records(path)
        content = path.read_text(encoding="utf-8")
        assert content.count("## 需求澄清记录") == 2  # 两条记录都还在

    def test_append_records_with_empty_history_writes_nothing(self, tmp_path):
        path = tmp_path / "clarify.md"
        assert Clarifier().append_records(path) == ""
        assert path.exists() is False
