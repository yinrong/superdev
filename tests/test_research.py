"""Tests for the research discipline core: budget tiers, over-budget refusal, citation checks, knowledge base."""

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
        assert b.max_calls is None  # no numeric cap; the gate is approval

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
        assert can_research(2, "small") is False  # 2 calls used up; the 3rd is refused
        assert can_research(10, "medium") is False
        assert can_research(3, "small") is False

    def test_trivial_refuses_any_call(self):
        assert can_research(0, "trivial") is False

    def test_large_has_no_numeric_cap(self):
        # large is refused via needs_approval, not call count
        assert can_research(999, "large") is True

    def test_unknown_size_raises(self):
        with pytest.raises(ResearchError):
            can_research(0, "xxl")


class TestStrategyCites:
    # The middle line stays Chinese on purpose: it exercises CJK keyword
    # extraction (fullwidth-colon splitting) against real-world notes.
    NOTES = (
        "- pytest addopts in the ini overrides the command-line -q\n"
        "- 锁竞争：db pool 在 Windows 上死锁\n"
        "1. Redis connection pool cap is too low\n"
    )

    def test_cites_keyword_from_notes(self):
        text = "switch to python -m pytest passing args explicitly to bypass the ini addopts"
        assert strategy_cites(text, self.NOTES) is True

    def test_cites_chinese_keyword(self):
        # keyword extraction must keep working for Chinese research notes
        text = "先解掉锁竞争，再动重试逻辑"
        assert strategy_cites(text, self.NOTES) is True

    def test_cites_numbered_bullet_keyword(self):
        assert strategy_cites("raise the Redis connection pool cap", self.NOTES) is True

    def test_no_citation_returns_false(self):
        assert strategy_cites("just retry three times and hope", self.NOTES) is False

    def test_empty_notes_never_cites(self):
        assert strategy_cites("any random strategy", "") is False

    def test_empty_strategy_text_returns_false(self):
        assert strategy_cites("", self.NOTES) is False


@pytest.fixture()
def kb_path(tmp_path):
    return tmp_path / "kb.yaml"


@pytest.fixture()
def filled_kb(kb_path):
    kb = KnowledgeBase(kb_path)
    kb.record("worked", "approach-A", "small-step single-file replacement worked")
    kb.record("worked", "approach-B", "add tests first, then touch the implementation")
    kb.record("pitfall", "pitfall-1", "Windows path not quoted")
    kb.record("pitfall", "pitfall-2", "yaml needs allow_unicode for non-ASCII")
    kb.record("pitfall", "pitfall-3", "pytest.ini addopts swallows arguments")
    return kb


class TestKnowledgeBase:
    def test_record_persists_and_roundtrips(self, filled_kb, kb_path):
        again = KnowledgeBase(kb_path)  # reload to verify persistence
        assert len(again.entries("worked")) == 2
        assert len(again.entries("pitfall")) == 3
        assert again.entries("pitfall")[0] == {"pattern": "pitfall-1", "note": "Windows path not quoted"}

    def test_record_dedupes_exact_duplicates(self, filled_kb, kb_path):
        assert filled_kb.record("pitfall", "pitfall-1", "Windows path not quoted") is False
        assert len(filled_kb.entries("pitfall")) == 3  # nothing duplicate-appended
        assert len(KnowledgeBase(kb_path).entries("pitfall")) == 3  # on-disk state unchanged too

    def test_same_pattern_different_note_appends(self, filled_kb):
        assert filled_kb.record("pitfall", "pitfall-1", "addendum: only triggers under cmd") is True
        assert len(filled_kb.entries("pitfall")) == 4

    def test_unknown_kind_raises(self, kb_path):
        kb = KnowledgeBase(kb_path)
        with pytest.raises(ResearchError):
            kb.record("whatever", "p", "n")

    def test_inject_trivial_is_empty(self, filled_kb):
        assert filled_kb.inject("trivial") == ""

    def test_inject_small_only_first_two_pitfalls(self, filled_kb):
        text = filled_kb.inject("small")
        assert "pitfall-1" in text and "pitfall-2" in text
        assert "pitfall-3" not in text  # small gets only the first 2 pitfalls
        assert "approach-A" not in text  # no worked entries

    def test_inject_medium_is_full(self, filled_kb):
        text = filled_kb.inject("medium")
        assert "approach-A" in text and "approach-B" in text
        assert "pitfall-1" in text and "pitfall-2" in text and "pitfall-3" in text

    def test_inject_small_with_empty_pitfalls_is_empty(self, kb_path):
        kb = KnowledgeBase(kb_path)
        kb.record("worked", "approach-A", "only worked entries, no pitfalls")
        assert kb.inject("small") == ""

    def test_inject_unknown_size_raises(self, filled_kb):
        with pytest.raises(ResearchError):
            filled_kb.inject("giant")


# ---- §8.8 automatic knowledge base distillation --------------------------

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
            ("worked", "turned green after two reds: AssertionError",
             "Task T1 turned green after 2 consecutive reds; check for the same error keyword before starting")
        ]

    def test_first_attempt_green_yields_first_green_path(self):
        rows = [self._row("T2", 1, True, "")]
        assert derive_from_events(rows) == [("worked", "first-green path", "T2 green on first attempt")]

    def test_all_red_never_green_yields_pitfall(self):
        rows = [
            self._row("T3", 1, False, LOG_IMPORT),
            self._row("T3", 2, False, LOG_IMPORT),
        ]
        assert derive_from_events(rows) == [
            ("pitfall", "ImportError", "Task T3 stayed red across 2 attempt(s); do not repeat this path")
        ]

    def test_one_red_then_green_yields_nothing(self):
        # one red then green has no stable pattern; by design it yields no record
        rows = [self._row("T4", 1, False, LOG_ASSERT), self._row("T4", 2, True, "")]
        assert derive_from_events(rows) == []

    def test_keyword_extraction_strips_pytest_prefix(self):
        # strip the E prefix, take the first word, drop the trailing colon -> ValueError
        rows = [self._row("T5", 1, False, "E       ValueError: bad input\n")]
        assert derive_from_events(rows) == [
            ("pitfall", "ValueError", "Task T5 stayed red across 1 attempt(s); do not repeat this path")
        ]

    def test_keyword_falls_back_to_first_nonempty_line(self):
        rows = [self._row("T6", 1, False, "some random output line\n")]
        assert derive_from_events(rows)[0][1] == "some"

    def test_keyword_unknown_when_log_empty(self):
        rows = [self._row("T7", 1, False, "")]
        assert derive_from_events(rows)[0][1] == "unknown error"


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
        assert kb.entries("worked") == [
            {"pattern": "turned green after two reds: AssertionError",
             "note": "Task T1 turned green after 2 consecutive reds; check for the same error keyword before starting"}
        ]

    def test_sync_twice_dedupes(self, tmp_path, proj):
        self._add_task(proj, "T2", "demo")
        proj.record_regression("T2", 1, True, "s1", "")
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 1
        assert sync_from_projection(kb, proj, "demo") == 0  # a repeated sync dedupes everything

    def test_sync_filters_by_project(self, tmp_path, proj):
        self._add_task(proj, "T3", "demo")
        self._add_task(proj, "T4", "other")
        proj.record_regression("T3", 1, True, "s", "")
        proj.record_regression("T4", 1, True, "s", "")
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 1
        assert kb.entries("worked")[0]["note"] == "T3 green on first attempt"

    def test_sync_ignores_events_without_task_row(self, tmp_path, proj):
        # record_regression allows tasks absent from the tasks table; sync JOINs, so those events do not participate
        proj.record_regression("T-ghost", 1, True, "s", "")
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 0

    def test_sync_mixed_records_count(self, tmp_path, proj):
        self._add_task(proj, "T5", "demo")
        self._add_task(proj, "T6", "demo")
        proj.record_regression("T5", 1, True, "s", "")  # first green
        proj.record_regression("T6", 1, False, "s", LOG_IMPORT)  # all red
        proj.record_regression("T6", 2, False, "s", LOG_IMPORT)
        kb = KnowledgeBase(tmp_path / "kb.yaml")
        assert sync_from_projection(kb, proj, "demo") == 2
        assert len(kb.entries("worked")) == 1
        assert len(kb.entries("pitfall")) == 1
