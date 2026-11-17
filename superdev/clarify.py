"""Requirement clarifier (§8.6): the manager side runs a clarification pass before splitting tasks.

Rule-based base: a requirement shorter than 10 characters or containing a Chinese
vague word such as 可能/大概/再说/尽量 needs clarification; next_question asks
exactly one question at a time, with 2-4 options attached. Note the vague-word
list stays Chinese on purpose: this detector targets Chinese vagueness, and its
behavior must not change. The engine is the upgrade slot: once injected, it
rewrites the question text; with the engine absent or failing, the rule version
carries on. Clarification records are assembled into markdown by format_records,
append_records appends them to disk, and the write path is passed in by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from superdev.engines import Engine

__all__ = ["Question", "Clarifier"]


@dataclass(frozen=True)
class Question:
    """One clarification question: question text + candidate options."""

    text: str
    options: list[str] = field(default_factory=list)


class Clarifier:
    """Requirement clarifier: judge -> ask -> record the answer, looping until the requirement is clear."""

    # Chinese vague-word list (kept Chinese by design: the detector targets Chinese vague words)
    VAGUE_WORDS = ("可能", "大概", "再说", "尽量", "也许", "差不多")
    MIN_LENGTH = 10  # fewer than 10 characters counts as under-specified

    # Boundary options per vague word (2-4 each); the answer directly replaces the vague word itself.
    _OPTIONS = {
        "可能": ("must be supported", "explicitly unsupported", "decided by a config option"),
        "大概": ("give an exact value or range", "implement the minimum usable version", "unconstrained"),
        "再说": ("do it this iteration", "skip this iteration, leave an interface only", "split into a separate task"),
        "尽量": ("must be done, part of acceptance criteria", "best effort, not acceptance-tested", "eighty percent is enough"),
        "也许": ("must be supported", "explicitly unsupported", "decided by a config option"),
        "差不多": ("give an exact value or range", "implement the minimum usable version", "unconstrained"),
    }
    _SHORT_OPTIONS = (
        "add what to build and the acceptance criteria",
        "add usage scenarios and boundaries",
        "keep it as-is, implement literally",
    )

    def __init__(self, engine: Engine | None = None):
        self.engine = engine
        self.records: list[dict[str, object]] = []  # Q&A records collected so far

    def needs_clarification(self, requirement: str) -> bool:
        """Requirement shorter than 10 characters or containing a vague word -> True."""
        text = (requirement or "").strip()
        if len(text) < self.MIN_LENGTH:
            return True
        return any(word in text for word in self.VAGUE_WORDS)

    def next_question(self, requirement: str) -> Question:
        """Ask about the first vague word hit; with no vague word but too short, ask what the goal is.

        A clear requirement has nothing to ask about, so raise ValueError
        (callers should check needs_clarification first).
        """
        text = (requirement or "").strip()
        if not self.needs_clarification(text):
            raise ValueError("requirement is already clear, nothing to ask")
        word = next((w for w in self.VAGUE_WORDS if w in text), None)
        if word is not None:
            question = Question(f'How should "{word}" be defined?', list(self._OPTIONS[word]))
        else:
            question = Question(
                f"The requirement is only {len(text)} characters (fewer than {self.MIN_LENGTH}); what exactly should be built?",
                list(self._SHORT_OPTIONS),
            )
        return self._refine_by_engine(question, text)

    def record_answer(self, requirement: str, q: Question, answer: str) -> str:
        """Record the Q&A and return the clarified requirement text.

        Merge rule: with a vague word present, the answer replaces the first
        occurrence of that word (de-vaguing the requirement); a short requirement
        has no word to replace, so the answer is appended as a parenthesized supplement.
        """
        text = (requirement or "").strip()
        answer = (answer or "").strip()
        word = next((w for w in self.VAGUE_WORDS if w in text), None)
        if word is not None:
            merged = text.replace(word, answer, 1)
        elif text:
            merged = f"{text} (supplement: {answer})"
        else:
            merged = answer
        self.records.append(
            {"question": q.text, "options": list(q.options), "answer": answer, "merged": merged}
        )
        return merged

    def format_records(self) -> str:
        """Assemble the clarification records into markdown; with no records, return an empty string."""
        if not self.records:
            return ""
        lines = ["## Requirement clarification log", ""]
        for r in self.records:
            opts = " / ".join(r["options"]) if r["options"] else "(no options)"
            lines.append(f"- Q: {r['question']}")
            lines.append(f"  options: {opts}")
            lines.append(f"  A: {r['answer']}")
        return "\n".join(lines) + "\n"

    def append_records(self, path: str | Path) -> str:
        """Append the markdown clarification records to the caller-given path, returning the text written."""
        md = self.format_records()
        if md:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8", newline="\n") as f:
                f.write(md)
        return md

    def _refine_by_engine(self, question: Question, requirement: str) -> Question:
        """Upgrade slot: with an engine present, ask it to rewrite the question text; on failure or empty output, fall back to the rule version."""
        if self.engine is None:
            return question
        try:
            refined = self.engine.run_round(
                "Rewrite the clarification question to be more specific",
                f"requirement: {requirement}\nquestion: {question.text}",
            )
        except Exception:  # an unavailable engine must not stall clarification; the rule version carries it
            return question
        refined = (refined or "").strip()
        return Question(refined, question.options) if refined else question
