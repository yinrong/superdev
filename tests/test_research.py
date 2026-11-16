"""研究纪律内核的测试：预算分档、超预算拒绝、引用判定、知识库。"""

import pytest

from superdev.research import (
    Budget,
    KnowledgeBase,
    ResearchError,
    budget,
    can_research,
    strategy_cites,
)


class TestBudget:
    def test_trivial_is_zero_calls_no_approval(self):
        b = budget("trivial")
        assert b == Budget(max_calls=0, needs_approval=False)

    def test_small_allows_two_calls(self):
        b = budget("small")
        assert b.max_calls == 2
        assert b.needs_approval is False

    def test_medium_allows_ten_calls(self):
        b = budget("medium")
        assert b.max_calls == 10
        assert b.needs_approval is False

    def test_large_needs_approval(self):
        b = budget("large")
        assert b.needs_approval is True
        assert b.max_calls is None  # 无数字上限，门禁走审批

    def test_size_is_case_insensitive(self):
        assert budget("Small") == budget("small")

    def test_unknown_size_raises(self):
        with pytest.raises(ResearchError):
            budget("huge")


class TestCanResearch:
    def test_within_budget_allows_next_call(self):
        assert can_research(1, "small") is True
        assert can_research(9, "medium") is True

    def test_at_or_over_budget_refuses(self):
        assert can_research(2, "small") is False  # 2 次用完，第 3 次拒绝
        assert can_research(10, "medium") is False
        assert can_research(3, "small") is False

    def test_trivial_refuses_any_call(self):
        assert can_research(0, "trivial") is False

    def test_large_has_no_numeric_cap(self):
        # large 的拒绝依据是 needs_approval 审批，不是调用次数
        assert can_research(999, "large") is True

    def test_unknown_size_raises(self):
        with pytest.raises(ResearchError):
            can_research(0, "xxl")


class TestStrategyCites:
    NOTES = (
        "- pytest 的 addopts 会让命令行 -q 失效\n"
        "- 锁竞争：db pool 在 Windows 上死锁\n"
        "1. Redis 连接池上限太低\n"
    )

    def test_cites_keyword_from_notes(self):
        text = "改用 python -m pytest 显式传参，绕开 ini 的 addopts 干扰"
        assert strategy_cites(text, self.NOTES) is True

    def test_cites_chinese_keyword(self):
        text = "先解掉锁竞争，再动重试逻辑"
        assert strategy_cites(text, self.NOTES) is True

    def test_cites_numbered_bullet_keyword(self):
        assert strategy_cites("调大 Redis 连接池上限", self.NOTES) is True

    def test_no_citation_returns_false(self):
        assert strategy_cites("直接重试三次碰运气", self.NOTES) is False

    def test_empty_notes_never_cites(self):
        assert strategy_cites("随便什么策略", "") is False

    def test_empty_strategy_text_returns_false(self):
        assert strategy_cites("", self.NOTES) is False


@pytest.fixture()
def kb_path(tmp_path):
    return tmp_path / "kb.yaml"


@pytest.fixture()
def filled_kb(kb_path):
    kb = KnowledgeBase(kb_path)
    kb.record("灵过", "改法A", "单文件小步替换成功")
    kb.record("灵过", "改法B", "先补测试再动实现")
    kb.record("踩坑", "坑1", "Windows 路径没加引号")
    kb.record("踩坑", "坑2", "yaml 中文要 allow_unicode")
    kb.record("踩坑", "坑3", "pytest.ini 的 addopts 会吃掉参数")
    return kb


class TestKnowledgeBase:
    def test_record_persists_and_roundtrips(self, filled_kb, kb_path):
        again = KnowledgeBase(kb_path)  # 重新加载，验证落盘
        assert len(again.entries("灵过")) == 2
        assert len(again.entries("踩坑")) == 3
        assert again.entries("踩坑")[0] == {"pattern": "坑1", "note": "Windows 路径没加引号"}

    def test_record_dedupes_exact_duplicates(self, filled_kb, kb_path):
        assert filled_kb.record("踩坑", "坑1", "Windows 路径没加引号") is False
        assert len(filled_kb.entries("踩坑")) == 3  # 没有重复追加
        assert len(KnowledgeBase(kb_path).entries("踩坑")) == 3  # 落盘也没变

    def test_same_pattern_different_note_appends(self, filled_kb):
        assert filled_kb.record("踩坑", "坑1", "补充：只在 cmd 下触发") is True
        assert len(filled_kb.entries("踩坑")) == 4

    def test_unknown_kind_raises(self, kb_path):
        kb = KnowledgeBase(kb_path)
        with pytest.raises(ResearchError):
            kb.record("随便", "p", "n")

    def test_inject_trivial_is_empty(self, filled_kb):
        assert filled_kb.inject("trivial") == ""

    def test_inject_small_only_first_two_pitfalls(self, filled_kb):
        text = filled_kb.inject("small")
        assert "坑1" in text and "坑2" in text
        assert "坑3" not in text  # small 只给踩坑前 2 条
        assert "改法A" not in text  # 不含灵过

    def test_inject_medium_is_full(self, filled_kb):
        text = filled_kb.inject("medium")
        assert "改法A" in text and "改法B" in text
        assert "坑1" in text and "坑2" in text and "坑3" in text

    def test_inject_small_with_empty_pitfalls_is_empty(self, kb_path):
        kb = KnowledgeBase(kb_path)
        kb.record("灵过", "改法A", "只有灵过没有坑")
        assert kb.inject("small") == ""

    def test_inject_unknown_size_raises(self, filled_kb):
        with pytest.raises(ResearchError):
            filled_kb.inject("giant")
