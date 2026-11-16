# Contributing to superdev

This project is driven by an AI-worker pipeline, so the rules below are
enforced by programs, not just review. 中文交流同样欢迎。

## Setup and tests

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Run tests from the repo root. The full suite must be green before you
open a PR — no exceptions, no "it passed on my machine".

## The worker contract

Read [`commands/superdev-work.md`](commands/superdev-work.md) before
writing any code. It binds every change: research budget by task size,
red-green discipline (start from green, revert on red, research after
two consecutive failures), and file ownership — touch only the files
listed in your work order. Never run `git commit` or `git push` yourself.

## Task flow

1. Tasks enter the ledger (台账, `agents/superdev-dev/派工计划.yml`)
   first; no off-ledger work.
2. Claim a work order, follow the contract, keep the suite green.
3. Report evidence: files changed, tail of `python -m pytest -q`,
   one-line self-assessment.

## Pull requests

- All tests green (342+), one task per PR.
- Stay inside your work order's file list; no drive-by doc edits.
