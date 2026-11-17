"""Manager-side hook rulings (design doc §2 hook list): four pure functions for session assembly to call.

- guard_write: before the AI writes a file, block it if the target is outside the ledger directory ("managers may only write the ledger")
- guard_command: before the AI runs a command, block it if the git subcommand is commit/push ("managers do not deliver on workers' behalf")
- post_write_check: after the ledger is written, run check_plan automatically and feed problems back into the session
- stop_gate: session close-out gate: full pytest + event log hash chain verification; either failing blocks close-out
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from superdev.check import check_plan
from superdev.events import EventLog
from superdev.ledger import LedgerError, load_plan

__all__ = [
    "WRITE_DENIED",
    "COMMAND_DENIED",
    "PYTEST_TIMEOUT",
    "guard_write",
    "guard_command",
    "post_write_check",
    "stop_gate",
]

WRITE_DENIED = "managers may only write the ledger"
COMMAND_DENIED = "managers do not deliver on workers' behalf"
PYTEST_TIMEOUT = 300  # seconds: upper bound for the full pytest run inside stop_gate

# git's top-level "options with a value": skip the option together with its value,
# otherwise spellings like `git -C ../repo commit` would slip through
_GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def guard_write(
    target_path: str | os.PathLike, allowed_root: str | os.PathLike
) -> tuple[bool, str]:
    """Ruling before a file write: target_path not under allowed_root -> (False, denial reason).

    Both paths go through os.path.abspath before comparing, so relative paths and
    slash direction never affect the verdict; a path that merely shares a prefix
    but lives in a different directory (ledger vs ledger2) does not count as under it.
    """
    target = os.path.abspath(str(target_path))
    root = os.path.abspath(str(allowed_root))
    try:
        under = os.path.commonpath([target, root]) == root
    except ValueError:
        # the two paths sit on different drives; commonpath raises outright, so it cannot be under the root
        under = False
    if not under:
        return False, WRITE_DENIED
    return True, ""


def guard_command(cmdline: str | list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """Ruling before a command runs: a git subcommand of commit or push -> (False, denial reason).

    String commands are split on whitespace; git may carry any number of options first
    (value-carrying options skip their value too); only when the first non-option word
    is commit/push do we block. Read-only commands like git status / git log pass.
    """
    if isinstance(cmdline, str):
        tokens = cmdline.split()
    else:
        tokens = [str(t) for t in cmdline]
    lowered = [t.lower() for t in tokens]
    for i, tok in enumerate(lowered):
        if tok not in ("git", "git.exe"):
            continue
        j = i + 1
        while j < len(lowered):
            word = lowered[j]
            if word in _GIT_OPTS_WITH_VALUE:
                j += 2  # the option and its value are both skipped
                continue
            if word.startswith("-"):
                j += 1
                continue
            if word in ("commit", "push"):
                return False, COMMAND_DENIED
            break  # some other subcommand (status/log/...) means it is not commit/push
    return True, ""


def post_write_check(plan_path: str | os.PathLike) -> tuple[bool, list[str]]:
    """Automatic check after the ledger is written: check_plan fully passes -> (True, []), otherwise (False, problem list).

    Relative paths such as repro scripts resolve against the ledger file's directory;
    a ledger that itself fails to parse also counts as not passing.
    """
    try:
        plan = load_plan(plan_path)
    except LedgerError as e:
        return False, [f"ledger parse failed: {e}"]
    problems = check_plan(plan, knowledge_dir=Path(plan_path).resolve().parent)
    return not problems, list(problems)


def stop_gate(
    repo_root: str | os.PathLike, event_log_path: str | os.PathLike | None = None
) -> tuple[bool, list[str]]:
    """Session close-out gate: full pytest + event log hash chain verification; either failing -> (False, problem list).

    pytest runs with the current interpreter under repo_root with a PYTEST_TIMEOUT-second
    limit; the event log defaults to repo_root/events.jsonl, and a missing file means
    there is nothing to verify (verify tolerates it).
    """
    problems: list[str] = []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PYTEST_TIMEOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        problems.append(
            f"full test run timed out: not finished within {PYTEST_TIMEOUT}s, cannot close out"
        )
    else:
        if proc.returncode != 0:
            tail = "\n".join((proc.stdout or "").splitlines()[-5:])
            problems.append(
                f"full test run failed (pytest exit code {proc.returncode}), recent output:\n{tail}"
            )

    log_path = (
        Path(event_log_path)
        if event_log_path is not None
        else Path(repo_root) / "events.jsonl"
    )
    ok, pos = EventLog(log_path).verify()
    if not ok:
        problems.append(
            f"event log verification failed: hash chain broken at event {pos}, decision chain untrustworthy"
        )
    return not problems, problems
