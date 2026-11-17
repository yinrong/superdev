"""Execution core of the research discipline: budget tiers, citation checks, and the module knowledge base.

Maps to detailed design §1.4 (research discipline) and §8.8 (module knowledge base).
Budgets are fixed by the dispatcher from the ledger's task-size field, not chosen
by the AI itself; a post-research strategy change must cite research_notes, or
the dispatcher refuses it. Standard library + PyYAML only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:  # type annotations only; avoids importing the projection module at runtime
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
    """Research discipline violation: an unknown task size, or an illegal knowledge base file/operation."""


@dataclass(frozen=True)
class Budget:
    """One research budget tier. max_calls None means no numeric cap (large only)."""

    max_calls: int | None
    needs_approval: bool = False


# §1.4 budget tier table: trivial skips research, small <=2, medium <=10, large needs separate approval.
_BUDGET_TABLE: dict[str, tuple[int | None, bool]] = {
    "trivial": (0, False),
    "small": (2, False),
    "medium": (10, False),
    "large": (None, True),
}


def budget(size: str) -> Budget:
    """Return the research budget for a task size; unknown sizes raise ResearchError."""
    key = str(size).strip().lower()
    if key not in _BUDGET_TABLE:
        raise ResearchError(
            f"Unknown task size: {size!r}, valid values: {sorted(_BUDGET_TABLE)}"
        )
    max_calls, needs_approval = _BUDGET_TABLE[key]
    return Budget(max_calls=max_calls, needs_approval=needs_approval)


def can_research(calls_used: int, size: str) -> bool:
    """Whether one more tool call is allowed: over budget returns False (the program's basis for refusal).

    large has no numeric budget and is always True; its gate is
    budget().needs_approval, approved separately by the dispatcher before work
    starts, not checked here.
    """
    b = budget(size)
    if b.max_calls is None:
        return True
    return calls_used < b.max_calls


# Leading bullet markers: - * •, 1. 1) 1、 一、 etc.; strip them and take the first word as the keyword.
_BULLET_RE = re.compile(r"^(?:[-*•]|\d+[.、)]|[一二三四五六七八九十]+[、.])\s*")

# Common leading filler/framework words: not citable keywords; dropped to avoid false matches.
_STOPWORDS = {
    "the", "a", "an", "to", "be", "is", "are", "was", "on", "for",
    "with", "if", "and", "or", "of", "in", "at", "it", "this", "that",
    "如果", "因为", "所以", "但是", "然后", "注意", "需要", "应该", "可能",
}


def _keywords(research_notes: str) -> list[str]:
    """Extract keywords from research notes: the first noun-like leading word of each line after stripping bullet markers."""
    keywords: list[str] = []
    for raw in research_notes.splitlines():
        line = _BULLET_RE.sub("", raw.strip())
        if not line:
            continue
        token = line.split()[0]
        token = re.split(r"[：:]", token)[0]  # "lock contention: db" has no space; the noun sits before the colon
        token = token.rstrip("：:，,、；;。.")
        if len(token) < 2 or token.lower() in _STOPWORDS:
            continue
        keywords.append(token)
    return keywords


def strategy_cites(strategy_text: str, research_notes: str) -> bool:
    """Whether a strategy change cites the research notes: at least one keyword appears in the strategy text."""
    text = (strategy_text or "").lower()
    return any(kw.lower() in text for kw in _keywords(research_notes or ""))


class KnowledgeBase:
    """Module knowledge base (§8.8): cross-task memory of "what worked, what bit us".

    YAML structure: worked/pitfall, each a list of {pattern, note}.
    record appends with dedup and persists; inject returns injection text by
    task size.
    """

    KINDS = ("worked", "pitfall")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, list[dict[str, str]]] = {k: [] for k in self.KINDS}
        if not self.path.exists():
            return
        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if loaded is None:
            return
        if not isinstance(loaded, dict):
            raise ResearchError(f"Knowledge base file is not a mapping: {self.path}")
        for kind in self.KINDS:
            entries = loaded.get(kind)
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise ResearchError(f"Knowledge base field {kind} is not a list: {self.path}")
            self._data[kind] = [dict(e) for e in entries]

    def record(self, kind: str, pattern: str, note: str) -> bool:
        """Append one experience entry; an exact duplicate is skipped. Returns whether it was actually written."""
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
        """Return all entries of one kind (a copy, so outsiders cannot mutate internal state)."""
        self._check_kind(kind)
        return [dict(e) for e in self._data[kind]]

    def inject(self, size: str) -> str:
        """Return injection text by task size: trivial empty, small first 2 pitfalls, medium full.

        large is treated like medium (once approved, at least a full
        reconnaissance).
        """
        key = str(size).strip().lower()
        if key == "trivial":
            return ""
        if key == "small":
            sections = (("pitfall", self._data["pitfall"][:2]),)
        elif key in ("medium", "large"):
            sections = (
                ("worked", self._data["worked"]),
                ("pitfall", self._data["pitfall"]),
            )
        else:
            raise ResearchError(f"Unknown task size: {size!r}, valid values: trivial/small/medium/large")

        lines: list[str] = []
        for name, entries in sections:
            if not entries:
                continue
            lines.append(f"[{name}]")
            lines.extend(f"- {e['pattern']}: {e['note']}" for e in entries)
        return "\n".join(lines)

    def _check_kind(self, kind: str) -> None:
        if kind not in self.KINDS:
            raise ResearchError(f"Unknown knowledge kind: {kind!r}, valid values: {list(self.KINDS)}")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


# ---- §8.8 automatic knowledge base distillation: derive "worked/pitfall" from regression events ----

# Lines that "look like errors": pytest's E prefix, Traceback, common error words.
_ERROR_LINE_RE = re.compile(r"^E\b|Traceback|Error|ERROR|FAILED|Exception|失败")


def _first_error_line(log_tail: str) -> str:
    """Take the first error-like line of the log tail; fall back to the first non-empty line, else an empty string."""
    lines = [l.strip() for l in (log_tail or "").splitlines()]
    for line in lines:
        if line and _ERROR_LINE_RE.search(line):
            return line
    for line in lines:
        if line:
            return line
    return ""


def _error_keyword(log_tail: str) -> str:
    """Cut a keyword from the first error line (first word, stripped of pytest's E prefix and punctuation)."""
    line = _first_error_line(log_tail)
    if not line:
        return "unknown error"
    line = re.sub(r"^E\s+", "", line)
    tokens = line.split()
    keyword = tokens[0].strip("：:，,、；;。.`'\"()") if tokens else ""
    return keyword or line[:40]


def derive_from_events(rows: Iterable[Any]) -> list[tuple[str, str, str]]:
    """Distill knowledge base records from regression event rows (task_id/attempt/green/log_tail).

    Each task's rows are sorted by attempt and run through three rules,
    producing (kind, pattern, note):
    - green after >=2 reds   -> ("worked", "turned green after two reds: <first error keyword>", note);
    - green on first attempt -> ("worked", "first-green path", "<task_id> green on first attempt");
    - red and never green    -> ("pitfall", <first error keyword>, note).
    One red then green yields no record: no stable pattern to distill.
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
                "worked",
                f"turned green after two reds: {_error_keyword(first_red_log)}",
                f"Task {task_id} turned green after {reds_before_green} consecutive reds; check for the same error keyword before starting",
            ))
        elif any(greens) and reds_before_green == 0:
            records.append(("worked", "first-green path", f"{task_id} green on first attempt"))
        elif not any(greens):
            records.append((
                "pitfall",
                _error_keyword(first_red_log),
                f"Task {task_id} stayed red across {len(attempts)} attempt(s); do not repeat this path",
            ))
    return records


def sync_from_projection(kb: KnowledgeBase, projection: Projection, project: str) -> int:
    """Pull regression events from the projection -> distill -> kb.record with dedup; return how many entries were added.

    Only tasks whose tasks.project matches are synced; events not in the tasks
    table have no owning project and are excluded. record dedupes identical
    content, so a second sync returns 0.
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
