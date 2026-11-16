"""研究纪律内核的测试：预算分档、超预算拒绝、引用判定、知识库。"""

import pytest

from superdev.projection import Projection
from superdev.research import (
    Budget,
    KnowledgeBase,
    ResearchError,
    budget,
    can_research,
    derive_from_events,
    strategy_cites,
    sync_from_projection,
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


# ---- §8.8 知识库自动提炼 ------------------------------------------------

LOG_ASSERT = "collected 5 items\nE       AssertionError: expected 3, got 2\n"
LOG_IMPORT = "E       ImportError: cannot import name 'foo'\n"


class TestDeriveFromEvents:
    def _row(self, task, attempt, green, log=""):
        return {"task_id": task, "attempt": attempt, "green": green, "log_tail": log}

    def test_two_reds_then_green_yields_win(self):
        rows = [
            self._row("T1", 1, False, LOG_ASSERT),
            self._row("T1", 2, False, LOG_ASSERT),
            self._row("T1", 3, True, ""),
        ]
        assert derive_from_events(rows) == [
            ("灵过", "曾两红后转绿：AssertionError",
             "任务 T1 连红 2 次后转绿，先查同关键词错误再动手")
        ]

    def test_first_attempt_green_yields_first_green_path(self):
        rows = [self._row("T2", 1, True, "")]
        assert derive_from_events(rows) == [("灵过", "首绿路径", "T2 首轮即绿")]

    def test_all_red_never_green_yields_pitfall(self):
        rows = [
            self._row("T3", 1, False, LOG_IMPORT),
            self._row("T3", 2, False, LOG_IMPORT),
        ]
        assert derive_from_events(rows) == [
            ("踩坑", "ImportError", "任务 T3 红 2 次未绿，勿重复此路")
        ]

    def test_one_red_then_green_yields_nothing(self):
        # 一红后绿没有稳定模式，设计上不出记录
        rows = [self._row("T4", 1, False, LOG_ASSERT), self._row("T4", 2, True, "")]
        assert derive_from_events(rows) == []

    def test_keyword_extraction_strips_pytest_prefix(self):
        # E 前缀剥掉、取首个词、去尾部冒号 → ValueError
        rows = [self._row("T5", 1, False, "E       ValueError: bad input\n")]
        assert derive_from_events(rows) == [
            ("踩坑", "ValueError", "任务 T5 红 1 次未绿，勿重复此路")
        ]

    def test_keyword_falls_back_to_first_nonempty_line(self):
        rows = [self._row("T6", 1, False, "some random output line\n")]
        assert derive_from_events(rows)[0][1] == "some"

    def test_keyword_unknown_when_log_empty(self):
        rows = [self._row("T7", 1, False, "")]
        assert derive_from_events(rows)[0][1] == "未知错误"


class TestSyncFromProjection:
    @pytest.fixture()
    def proj(self, tmp_path):
        p = Projection(tmp_path / "proj.db")
        yield p
        p.close()

    @staticmethod
    def _add_task(p, task_id, project):
        p.conn.execute(
            "INSERT INTO tasks (task_id, project, status) VALUES (?, ?, 'ready')",
            (task_id, project),
        )
        p.conn.commit()

    def test_sync_writes_derived_records(self, tmp_path, proj):
        self._add_task(proj, "T1", "demo")
        proj.record_regression("T1", 1, False, "s1", LOG_ASSERT)
        proj.record_regression("T1", 2, False, "s2", LOG_ASSERT)
        proj.record_regression("T1", 3, True, "s3", "")
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 1
        assert kb.entries("灵过") == [
            {"pattern": "曾两红后转绿：AssertionError",
             "note": "任务 T1 连红 2 次后转绿，先查同关键词错误再动手"}
        ]

    def test_sync_twice_dedupes(self, tmp_path, proj):
        self._add_task(proj, "T2", "demo")
        proj.record_regression("T2", 1, True, "s1", "")
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 1
        assert sync_from_projection(kb, proj, "demo") == 0  # 重复 sync 全部去重

    def test_sync_filters_by_project(self, tmp_path, proj):
        self._add_task(proj, "T3", "demo")
        self._add_task(proj, "T4", "other")
        proj.record_regression("T3", 1, True, "s", "")
        proj.record_regression("T4", 1, True, "s", "")
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 1
        assert kb.entries("灵过")[0]["note"] == "T3 首轮即绿"

    def test_sync_ignores_events_without_task_row(self, tmp_path, proj):
        # record_regression 允许任务不在 tasks 表；sync 靠 JOIN，这些事件不参与
        proj.record_regression("T-ghost", 1, True, "s", "")
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 0

    def test_sync_mixed_records_count(self, tmp_path, proj):
        self._add_task(proj, "T5", "demo")
        self._add_task(proj, "T6", "demo")
        proj.record_regression("T5", 1, True, "s", "")  # 首绿
        proj.record_regression("T6", 1, False, "s", LOG_IMPORT)  # 纯红
        proj.record_regression("T6", 2, False, "s", LOG_IMPORT)
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 2
        assert len(kb.entries("灵过")) == 1
        assert len(kb.entries("踩坑")) == 1
