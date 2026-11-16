"""研究纪律的执行内核：预算分档、引用校验、模块知识库。

对应详细设计 §1.4（研究纪律）与 §8.8（模块知识库）。
预算由派发器按台账的任务规模字段定死，不靠 AI 自己分档；
事后研究的策略变更必须引用 research_notes，否则派发器拒绝。
只用标准库 + PyYAML。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = [
    "ResearchError",
    "Budget",
    "budget",
    "can_research",
    "strategy_cites",
    "KnowledgeBase",
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
