"""研究纪律的执行内核：预算分档、引用校验、模块知识库。

对应详细设计 §1.4（研究纪律）与 §8.8（模块知识库）。
预算由派发器按台账的任务规模字段定死，不靠 AI 自己分档；
事后研究的策略变更必须引用 research_notes，否则派发器拒绝。
只用标准库 + PyYAML。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:  # 只为类型标注，避免运行期导入投影模块
    from superdev.projection import Projection

__all__ = [
    "ResearchError",
    "Budget",
    "budget",
    "can_research",
    "strategy_cites",
    "KnowledgeBase",
    "derive_from_events",
    "sync_from_projection",
]


class ResearchError(Exception):
    """研究纪律违规：未知任务规模，或知识库文件/操作非法。"""


@dataclass(frozen=True)
class Budget:
    """一档研究预算。max_calls 为 None 表示无数字上限（仅 large）。"""

    max_calls: int | None
    needs_approval: bool = False


# §1.4 预算分档表：trivial 跳过、small ≤2、medium ≤10、large 单独批准。
_BUDGET_TABLE: dict[str, tuple[int | None, bool]] = {
    "trivial": (0, False),
    "small": (2, False),
    "medium": (10, False),
    "large": (None, True),
}


def budget(size: str) -> Budget:
    """按任务规模返回研究预算，未知规模抛 ResearchError。"""
    key = str(size).strip().lower()
    if key not in _BUDGET_TABLE:
        raise ResearchError(
            f"未知任务规模: {size!r}，合法值: {sorted(_BUDGET_TABLE)}"
        )
    max_calls, needs_approval = _BUDGET_TABLE[key]
    return Budget(max_calls=max_calls, needs_approval=needs_approval)


def can_research(calls_used: int, size: str) -> bool:
    """是否还允许下一次工具调用：超预算即 False（程序拒绝的依据）。

    large 无数字预算，恒 True；它的门禁是 budget().needs_approval，
    由派发器在开工前单独审批，不在此处判断。
    """
    b = budget(size)
    if b.max_calls is None:
        return True
    return calls_used < b.max_calls


# 行首的列表符号：- * • 、1. 1) 1、 一、 等，剥掉后取首个词当关键词。
_BULLET_RE = re.compile(r"^(?:[-*•]|\d+[.、)]|[一二三四五六七八九十]+[、.])\s*")

# 行首常见虚词/框架词：不是可引用的关键词，剔掉防止误判。
_STOPWORDS = {
    "the", "a", "an", "to", "be", "is", "are", "was", "on", "for",
    "with", "if", "and", "or", "of", "in", "at", "it", "this", "that",
    "如果", "因为", "所以", "但是", "然后", "注意", "需要", "应该", "可能",
}


def _keywords(research_notes: str) -> list[str]:
    """从研究结论里提关键词：每行剥掉列表符号后的首个名词性行首词。"""
    keywords: list[str] = []
    for raw in research_notes.splitlines():
        line = _BULLET_RE.sub("", raw.strip())
        if not line:
            continue
        token = line.split()[0]
        token = re.split(r"[：:]", token)[0]  # “锁竞争：db”整段无空格，冒号前才是名词
        token = token.rstrip("：:，,、；;。.")
        if len(token) < 2 or token.lower() in _STOPWORDS:
            continue
        keywords.append(token)
    return keywords


def strategy_cites(strategy_text: str, research_notes: str) -> bool:
    """策略变更是否引用了研究结论：至少一个关键词出现在策略文本中。"""
    text = (strategy_text or "").lower()
    return any(kw.lower() in text for kw in _keywords(research_notes or ""))


class KnowledgeBase:
    """模块知识库（§8.8）：跨任务记忆“什么改法灵过、什么坑踩过”。

    YAML 结构：灵过/踩坑 各是 [{pattern, note}] 列表。
    record 追加去重并落盘；inject 按任务规模返回注入文本。
    """

    KINDS = ("灵过", "踩坑")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, list[dict[str, str]]] = {k: [] for k in self.KINDS}
        if not self.path.exists():
            return
        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if loaded is None:
            return
        if not isinstance(loaded, dict):
            raise ResearchError(f"知识库文件不是映射: {self.path}")
        for kind in self.KINDS:
            entries = loaded.get(kind)
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise ResearchError(f"知识库字段 {kind} 不是列表: {self.path}")
            self._data[kind] = [dict(e) for e in entries]

    def record(self, kind: str, pattern: str, note: str) -> bool:
        """追加一条经验，完全同条已存在则跳过。返回是否真的写入了。"""
        self._check_kind(kind)
        if any(
            e.get("pattern") == pattern and e.get("note") == note
            for e in self._data[kind]
        ):
            return False
        self._data[kind].append({"pattern": pattern, "note": note})
        self._save()
        return True

    def entries(self, kind: str) -> list[dict[str, str]]:
        """返回某一类的所有条目（拷贝，防止外部改内部状态）。"""
        self._check_kind(kind)
        return [dict(e) for e in self._data[kind]]

    def inject(self, size: str) -> str:
        """按任务规模返回注入文本：trivial 空、small 踩坑前 2 条、medium 全量。

        large 视同 medium（批下来之后至少是全量侦察）。
        """
        key = str(size).strip().lower()
        if key == "trivial":
            return ""
        if key == "small":
            sections = (("踩坑", self._data["踩坑"][:2]),)
        elif key in ("medium", "large"):
            sections = (
                ("灵过", self._data["灵过"]),
                ("踩坑", self._data["踩坑"]),
            )
        else:
            raise ResearchError(f"未知任务规模: {size!r}，合法值: trivial/small/medium/large")

        lines: list[str] = []
        for name, entries in sections:
            if not entries:
                continue
            lines.append(f"[{name}]")
            lines.extend(f"- {e['pattern']}: {e['note']}" for e in entries)
        return "\n".join(lines)

    def _check_kind(self, kind: str) -> None:
        if kind not in self.KINDS:
            raise ResearchError(f"未知知识类型: {kind!r}，合法值: {list(self.KINDS)}")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


# ---- §8.8 知识库自动提炼：从回归事件派生"灵过/踩坑" ------------------------

# 日志里"像错误"的行：pytest 的 E 前缀、Traceback、常见错误词。
_ERROR_LINE_RE = re.compile(r"^E\b|Traceback|Error|ERROR|FAILED|Exception|失败")


def _first_error_line(log_tail: str) -> str:
    """取日志尾部第一个像错误的行；没有则退回首个非空行，再没有返回空串。"""
    lines = [l.strip() for l in (log_tail or "").splitlines()]
    for line in lines:
        if line and _ERROR_LINE_RE.search(line):
            return line
    for line in lines:
        if line:
            return line
    return ""


def _error_keyword(log_tail: str) -> str:
    """从首个错误行截取关键词（首个词，剥掉 pytest 的 E 前缀与标点）。"""
    line = _first_error_line(log_tail)
    if not line:
        return "未知错误"
    line = re.sub(r"^E\s+", "", line)
    tokens = line.split()
    keyword = tokens[0].strip("：:，,、；;。.`'\"()") if tokens else ""
    return keyword or line[:40]


def derive_from_events(rows: Iterable[Any]) -> list[tuple[str, str, str]]:
    """从回归事件行（task_id/attempt/green/log_tail）提炼知识库记录。

    每个任务按 attempt 排序后套三条规则，产出 (kind, pattern, note)：
    - 红>=2 后转绿 → ("灵过", "曾两红后转绿：<首个错误行关键词>", note)；
    - 首轮一次绿   → ("灵过", "首绿路径", "<task_id> 首轮即绿")；
    - 红且从未绿   → ("踩坑", <首个错误行关键词>, note)。
    一红后绿不出记录：没有稳定可提炼的模式。
    """
    by_task: dict[str, list[Any]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)

    records: list[tuple[str, str, str]] = []
    for task_id in sorted(by_task):
        attempts = sorted(by_task[task_id], key=lambda r: r["attempt"])
        greens = [bool(r["green"]) for r in attempts]
        first_red_log = next(
            (r["log_tail"] for r in attempts if not bool(r["green"])), ""
        )
        reds_before_green = 0
        for green in greens:
            if green:
                break
            reds_before_green += 1

        if any(greens) and reds_before_green >= 2:
            records.append((
                "灵过",
                f"曾两红后转绿：{_error_keyword(first_red_log)}",
                f"任务 {task_id} 连红 {reds_before_green} 次后转绿，先查同关键词错误再动手",
            ))
        elif any(greens) and reds_before_green == 0:
            records.append(("灵过", "首绿路径", f"{task_id} 首轮即绿"))
        elif not any(greens):
            records.append((
                "踩坑",
                _error_keyword(first_red_log),
                f"任务 {task_id} 红 {len(attempts)} 次未绿，勿重复此路",
            ))
    return records


def sync_from_projection(kb: KnowledgeBase, projection: Projection, project: str) -> int:
    """从投影拉回归事件 → 提炼 → kb.record 去重写入，返回本次新增条数。

    只同步 tasks.project 匹配的任务；未入 tasks 表的事件没有归属项目，
    不参与本次同步。重复内容 record 自己去重，二次 sync 返回 0。
    """
    rows = projection.conn.execute(
        """
        SELECT re.task_id, re.attempt, re.green, re.log_tail
        FROM regression_events re
        JOIN tasks t ON t.task_id = re.task_id
        WHERE t.project = ?
        ORDER BY re.task_id, re.attempt
        """,
        (project,),
    ).fetchall()
    added = 0
    for kind, pattern, note in derive_from_events(rows):
        if kb.record(kind, pattern, note):
            added += 1
    return added
