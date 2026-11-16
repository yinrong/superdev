"""评审工人（§8.7）：门禁前的第二双眼。

规则是底座，engine 缺省也能跑：
- diff 删了测试文件 → needs_fix（测试只增不减）；
- 新增代码行数超过 500 → needs_fix"拆小"；
- 新增代码含 TODO/FIXME → 追加提示但不拦（只提醒，不翻 verdict）；
- 其余 pass。
engine 注入时先调 run_round 拿一份分析垫在意见最前，再叠加规则判定；
引擎抛错或返回空不阻塞，规则照跑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from superdev.engines import Engine

__all__ = ["ReviewResult", "Reviewer"]


@dataclass(frozen=True)
class ReviewResult:
    """一次评审的结论：verdict 是 pass / needs_fix，comments 逐条给理由。"""

    verdict: str
    comments: list[str] = field(default_factory=list)


# 测试文件路径：tests/ 目录、test_*.py、*_test.py（正反斜杠都认）。
_TEST_PATH_RE = re.compile(
    r"(?:^|[\\/])tests?[\\/]"
    r"|(?:^|[\\/])test_[^\\/]+\.py$"
    r"|(?:^|[\\/])[^\\/]+_test\.py$"
)
_TODO_RE = re.compile(r"TODO|FIXME")


def _strip_ab_prefix(path: str) -> str:
    """去掉 git diff 路径里的 a/ b/ 前缀。"""
    return path[2:] if path[:2] in ("a/", "b/") else path


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path.strip().strip('"')))


class Reviewer:
    """评审工：review 出结构化结论，gate 给门禁一个布尔。"""

    PASS = "pass"
    NEEDS_FIX = "needs_fix"
    MAX_ADDED_LINES = 500

    def __init__(self, engine: Engine | None = None):
        self.engine = engine

    def review(self, diff_text: str) -> ReviewResult:
        """评审一份 unified diff：规则判定 verdict，意见按"引擎分析 → 规则"排序。"""
        lines = (diff_text or "").splitlines()
        comments: list[str] = []
        blocking = False

        analysis = self._engine_analysis(diff_text or "")
        if analysis:
            comments.append(f"引擎分析：{analysis}")

        deleted_tests = self._deleted_test_files(lines)
        if deleted_tests:
            blocking = True
            for path in deleted_tests:
                comments.append(f"删测试文件：{path}，测试只增不减，先补回或说明理由")

        added = [l for l in lines if l.startswith("+") and not l.startswith("+++")]
        if len(added) > self.MAX_ADDED_LINES:
            blocking = True
            comments.append(
                f"新增 {len(added)} 行，超过 {self.MAX_ADDED_LINES} 行上限，请拆小后再提交"
            )

        todo_hits = [l for l in added if _TODO_RE.search(l)]
        if todo_hits:
            comments.append(
                f"新增代码含 TODO/FIXME（{len(todo_hits)} 处），请清理或登记成任务"
            )

        verdict = self.NEEDS_FIX if blocking else self.PASS
        return ReviewResult(verdict=verdict, comments=comments)

    def gate(self, review: ReviewResult) -> bool:
        """门禁前置：pass 放行，needs_fix 拦下。"""
        return review.verdict == self.PASS

    # ---- 内部 -------------------------------------------------------

    def _engine_analysis(self, diff_text: str) -> str:
        if self.engine is None:
            return ""
        try:
            out = self.engine.run_round(
                "评审以下 diff：查测试覆盖、命名、明显缺陷", diff_text
            )
        except Exception:  # 引擎缺席/失败不阻塞评审，规则兜底
            return ""
        return (out or "").strip()

    @staticmethod
    def _deleted_test_files(lines: list[str]) -> list[str]:
        """被删的测试文件：目标侧是 /dev/null 且来源路径长得像测试。"""
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
