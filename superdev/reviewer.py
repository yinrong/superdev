"""Review worker (§8.7): a second pair of eyes before the gate.

The rules are the base, so it runs even without an engine:
- the diff deletes a test file -> needs_fix (tests may only grow);
- more than 500 added code lines -> needs_fix "split it up";
- added code containing TODO/FIXME -> an appended hint but no block
  (remind only, never flip the verdict);
- otherwise pass.
When an engine is injected, run_round is called first and its analysis is
prepended to the comments before the rule verdicts stack on top; an engine
error or empty output blocks nothing, the rules still run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from superdev.engines import Engine

__all__ = ["ReviewResult", "Reviewer"]


@dataclass(frozen=True)
class ReviewResult:
    """One review's conclusion: verdict is pass / needs_fix, comments give the reasons one by one."""

    verdict: str
    comments: list[str] = field(default_factory=list)


# test file paths: tests/ directories, test_*.py, *_test.py (forward and back slashes both recognized).
_TEST_PATH_RE = re.compile(
    r"(?:^|[\\/])tests?[\\/]"
    r"|(?:^|[\\/])test_[^\\/]+\.py$"
    r"|(?:^|[\\/])[^\\/]+_test\.py$"
)
_TODO_RE = re.compile(r"TODO|FIXME")


def _strip_ab_prefix(path: str) -> str:
    """Strip the a/ b/ prefixes git diff puts on paths."""
    return path[2:] if path[:2] in ("a/", "b/") else path


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path.strip().strip('"')))


class Reviewer:
    """Review worker: review produces a structured conclusion, gate hands the gate a boolean."""

    PASS = "pass"
    NEEDS_FIX = "needs_fix"
    MAX_ADDED_LINES = 500

    def __init__(self, engine: Engine | None = None):
        self.engine = engine

    def review(self, diff_text: str) -> ReviewResult:
        """Review one unified diff: the rules decide the verdict; comments are ordered "engine analysis -> rules"."""
        lines = (diff_text or "").splitlines()
        comments: list[str] = []
        blocking = False

        analysis = self._engine_analysis(diff_text or "")
        if analysis:
            comments.append(f"engine analysis: {analysis}")

        deleted_tests = self._deleted_test_files(lines)
        if deleted_tests:
            blocking = True
            for path in deleted_tests:
                comments.append(
                    f"deleted test file: {path}; tests may only grow, restore it or state the reason"
                )

        added = [l for l in lines if l.startswith("+") and not l.startswith("+++")]
        if len(added) > self.MAX_ADDED_LINES:
            blocking = True
            comments.append(
                f"added {len(added)} lines, over the {self.MAX_ADDED_LINES}-line limit, split it up before submitting"
            )

        todo_hits = [l for l in added if _TODO_RE.search(l)]
        if todo_hits:
            comments.append(
                f"added code contains TODO/FIXME ({len(todo_hits)} spots), clean them up or register them as tasks"
            )

        verdict = self.NEEDS_FIX if blocking else self.PASS
        return ReviewResult(verdict=verdict, comments=comments)

    def gate(self, review: ReviewResult) -> bool:
        """Gate precondition: pass lets it through, needs_fix blocks."""
        return review.verdict == self.PASS

    # ---- internals ---------------------------------------------------

    def _engine_analysis(self, diff_text: str) -> str:
        if self.engine is None:
            return ""
        try:
            out = self.engine.run_round(
                "Review the following diff: check test coverage, naming, obvious defects",
                diff_text,
            )
        except Exception:  # an absent/failed engine must not block the review; the rules carry it
            return ""
        return (out or "").strip()

    @staticmethod
    def _deleted_test_files(lines: list[str]) -> list[str]:
        """Deleted test files: the target side is /dev/null and the source path looks like a test."""
        deleted: list[str] = []
        source = ""
        for line in lines:
            if line.startswith("--- "):
                source = _strip_ab_prefix(line[4:].strip())
            elif line.startswith("+++ "):
                target = line[4:].strip()
                if target == "/dev/null" and source and _is_test_path(source):
                    deleted.append(source)
        return deleted
