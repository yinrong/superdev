"""Tests for the review worker (§8.7): rule base, engine layering, gate boolean."""

from superdev.engines import EchoEngine
from superdev.reviewer import ReviewResult, Reviewer

# a diff that deletes a test file
DELETE_TEST_DIFF = """\
diff --git a/tests/test_old.py b/tests/test_old.py
deleted file mode 100644
--- a/tests/test_old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-import pytest
-def test_old(): assert True
"""

# a diff that deletes an ordinary source file (must not block)
DELETE_SRC_DIFF = """\
diff --git a/superdev/legacy.py b/superdev/legacy.py
deleted file mode 100644
--- a/superdev/legacy.py
+++ /dev/null
@@ -1,1 +0,0 @@
-print("legacy")
"""

CLEAN_DIFF = """\
--- a/superdev/util.py
+++ b/superdev/util.py
@@ -1,2 +1,3 @@
 context line
+new_line = 1
 another context
"""

TODO_DIFF = """\
--- a/superdev/util.py
+++ b/superdev/util.py
@@ -1,1 +1,2 @@
 context line
+value = compute()  # TODO: replace with a constant
"""


def _big_diff(added_lines: int) -> str:
    header = "--- a/superdev/big.py\n+++ b/superdev/big.py\n@@ -1 +1,{} @@\n".format(
        added_lines
    )
    return header + "\n".join(f"+line_{i}" for i in range(added_lines)) + "\n"


class TestRulesOnly:
    def test_deleted_test_file_is_blocked(self):
        r = Reviewer().review(DELETE_TEST_DIFF)
        assert r.verdict == "needs_fix"
        assert any("tests/test_old.py" in c for c in r.comments)

    def test_deleted_source_file_is_not_blocked(self):
        r = Reviewer().review(DELETE_SRC_DIFF)
        assert r.verdict == "pass"
        assert r.comments == []

    def test_test_file_delete_under_tests_dir_blocked(self):
        diff = "--- a/tests/unit/test_api.py\n+++ /dev/null\n+-pass\n"
        r = Reviewer().review(diff)
        assert r.verdict == "needs_fix"

    def test_big_diff_over_500_lines_is_blocked(self):
        r = Reviewer().review(_big_diff(501))
        assert r.verdict == "needs_fix"
        assert any("split" in c for c in r.comments)
        assert any("501" in c for c in r.comments)

    def test_exactly_500_added_lines_passes(self):
        # boundary: the rule is "over 500", so exactly 500 lines does not block
        r = Reviewer().review(_big_diff(500))
        assert r.verdict == "pass"

    def test_todo_in_added_line_is_hint_not_block(self):
        r = Reviewer().review(TODO_DIFF)
        assert r.verdict == "pass"  # a hint only, never flips the verdict
        assert any("TODO/FIXME" in c for c in r.comments)

    def test_fixme_in_added_line_also_hinted(self):
        diff = CLEAN_DIFF + "+x = 1  # FIXME later\n"
        r = Reviewer().review(diff)
        assert r.verdict == "pass"
        assert any("TODO/FIXME" in c for c in r.comments)

    def test_todo_only_in_context_or_deleted_line_not_hinted(self):
        diff = "--- a/superdev/util.py\n+++ b/superdev/util.py\n@@ -1,2 +1,2 @@\n-old = 1  # TODO: gone\n+fresh = 2\n"
        r = Reviewer().review(diff)
        assert r.comments == []

    def test_clean_diff_passes_with_no_comments(self):
        r = Reviewer().review(CLEAN_DIFF)
        assert r.verdict == "pass"
        assert r.comments == []

    def test_empty_diff_passes(self):
        r = Reviewer().review("")
        assert r.verdict == "pass"
        assert r.comments == []


class TestEngineLayering:
    def test_engine_analysis_prepended_as_comment(self):
        engine = EchoEngine(["inconsistent naming: load vs fetch mixed"])
        r = Reviewer(engine).review(CLEAN_DIFF)
        assert r.verdict == "pass"
        assert r.comments[0] == "engine analysis: inconsistent naming: load vs fetch mixed"
        assert len(engine.calls) == 1
        assert engine.calls[0][1] == CLEAN_DIFF  # the diff is handed to the engine as context

    def test_engine_analysis_plus_blocking_rule_stack(self):
        engine = EchoEngine(["missing test coverage"])
        r = Reviewer(engine).review(DELETE_TEST_DIFF)
        assert r.verdict == "needs_fix"
        assert r.comments[0].startswith("engine analysis:")
        assert any("deleted test file" in c for c in r.comments)

    def test_engine_failure_falls_back_to_rules(self):
        # the EchoEngine script runs out and raises RuntimeError; the review still returns its rule verdict
        r = Reviewer(EchoEngine([])).review(DELETE_TEST_DIFF)
        assert r.verdict == "needs_fix"
        assert any("deleted test file" in c for c in r.comments)
        assert not any(c.startswith("engine analysis:") for c in r.comments)

    def test_engine_empty_output_adds_no_comment(self):
        r = Reviewer(EchoEngine([""])).review(CLEAN_DIFF)
        assert r.verdict == "pass"
        assert r.comments == []


class TestGate:
    def test_gate_true_for_pass(self):
        result = ReviewResult(verdict="pass", comments=[])
        assert Reviewer().gate(result) is True

    def test_gate_false_for_needs_fix(self):
        result = ReviewResult(verdict="needs_fix", comments=["deleted test file: x"])
        assert Reviewer().gate(result) is False

    def test_gate_end_to_end_with_review(self):
        reviewer = Reviewer()
        assert reviewer.gate(reviewer.review(CLEAN_DIFF)) is True
        assert reviewer.gate(reviewer.review(DELETE_TEST_DIFF)) is False
