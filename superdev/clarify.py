"""需求澄清器（§8.6）：管理端拆任务前先过一道澄清。

规则式底座：需求少于 10 字或含"可能/大概/再说/尽量"等模糊词 →
需要澄清；next_question 一次只问一个问题、附 2-4 个选项。
engine 是升级位：注入后由引擎改写问题文本，引擎缺席或抛错回退规则式。
澄清记录由 format_records 拼成 markdown，append_records 负责追加落盘，
写入路径由调用方传入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from superdev.engines import Engine

__all__ = ["Question", "Clarifier"]


@dataclass(frozen=True)
class Question:
    """一个澄清问题：问题文本 + 候选选项。"""

    text: str
    options: list[str] = field(default_factory=list)


class Clarifier:
    """需求澄清器：判定 → 提问 → 记答案，循环到需求清晰为止。"""

    VAGUE_WORDS = ("可能", "大概", "再说", "尽量", "也许", "差不多")
    MIN_LENGTH = 10  # 少于 10 个字视为描述不足

    # 每个模糊词对应的界定选项（2-4 个），答案直接替换模糊词本身。
    _OPTIONS = {
        "可能": ("必须支持", "明确不支持", "由配置项决定"),
        "大概": ("给出确切数值或范围", "按最小可用实现", "不限定"),
        "再说": ("本期就要做", "本期不做，只留接口", "拆成独立任务"),
        "尽量": ("必须做到，进验收标准", "尽力即可，不验收", "做到八成即可"),
        "也许": ("必须支持", "明确不支持", "由配置项决定"),
        "差不多": ("给出确切数值或范围", "按最小可用实现", "不限定"),
    }
    _SHORT_OPTIONS = ("补充要做什么与验收标准", "补充使用场景与边界", "维持原样，按字面实现")

    def __init__(self, engine: Engine | None = None):
        self.engine = engine
        self.records: list[dict[str, object]] = []  # 已落下的问答记录

    def needs_clarification(self, requirement: str) -> bool:
        """需求少于 10 字或含模糊词 → True。"""
        text = (requirement or "").strip()
        if len(text) < self.MIN_LENGTH:
            return True
        return any(word in text for word in self.VAGUE_WORDS)

    def next_question(self, requirement: str) -> Question:
        """针对第一个命中的模糊词提问；无模糊词但过短则问需求目标。

        需求已清晰时没有可问的，抛 ValueError（调用方应先判 needs_clarification）。
        """
        text = (requirement or "").strip()
        if not self.needs_clarification(text):
            raise ValueError("需求已清晰，无需提问")
        word = next((w for w in self.VAGUE_WORDS if w in text), None)
        if word is not None:
            question = Question(f"「{word}」如何界定？", list(self._OPTIONS[word]))
        else:
            question = Question(
                f"需求只有 {len(text)} 个字（少于 {self.MIN_LENGTH}），具体要做什么？",
                list(self._SHORT_OPTIONS),
            )
        return self._refine_by_engine(question, text)

    def record_answer(self, requirement: str, q: Question, answer: str) -> str:
        """记下问答并返回澄清后的需求文本。

        合并规则：有模糊词时用答案替换首个该模糊词（需求随之去模糊）；
        短需求没有词可替换，把答案作为补充追加在括号里。
        """
        text = (requirement or "").strip()
        answer = (answer or "").strip()
        word = next((w for w in self.VAGUE_WORDS if w in text), None)
        if word is not None:
            merged = text.replace(word, answer, 1)
        elif text:
            merged = f"{text}（补充：{answer}）"
        else:
            merged = answer
        self.records.append(
            {"question": q.text, "options": list(q.options), "answer": answer, "merged": merged}
        )
        return merged

    def format_records(self) -> str:
        """把澄清记录拼成 markdown；没有记录返回空串。"""
        if not self.records:
            return ""
        lines = ["## 需求澄清记录", ""]
        for r in self.records:
            opts = " / ".join(r["options"]) if r["options"] else "（无选项）"
            lines.append(f"- 问：{r['question']}")
            lines.append(f"  选项：{opts}")
            lines.append(f"  答：{r['answer']}")
        return "\n".join(lines) + "\n"

    def append_records(self, path: str | Path) -> str:
        """把 markdown 澄清记录追加到调用方给的路径，返回写入的文本。"""
        md = self.format_records()
        if md:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8", newline="\n") as f:
                f.write(md)
        return md

    def _refine_by_engine(self, question: Question, requirement: str) -> Question:
        """升级位：引擎在场就请它改写问题文本；失败或空输出回退规则式。"""
        if self.engine is None:
            return question
        try:
            refined = self.engine.run_round(
                "把澄清问题改写得更具体", f"需求：{requirement}\n问题：{question.text}"
            )
        except Exception:  # 引擎不可用不阻断澄清，规则式兜底
            return question
        refined = (refined or "").strip()
        return Question(refined, question.options) if refined else question
