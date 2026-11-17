"""superdev manager-side command-line entry (the reconciliation surface of design docs §2/§3.3).

Five subcommands:
  check <plan.yml>    validate the ledger; exit 1 on problems
  status <project dir>   read ledger + projection, one line per task (id, status, red count)
  round <project dir>    one reconciliation patrol: refresh the projection + count pending events (no real dispatching this round)
  verify <event log path>   validate the event log hash chain
  import-spec <spec dir> <output ledger path>  spec-kit checklist to dispatch plan; over-cap gets bounced

A project dir means agents/<project>, by convention holding the three files
plan.yml, projection.db and events.jsonl.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from superdev.check import check_plan
from superdev.events import EventLog
from superdev.importspec import MAX_TASKS, SpecImportError, import_spec
from superdev.ledger import LedgerError, load_plan
from superdev.projection import Projection

PLAN_NAME = "plan.yml"
DB_NAME = "projection.db"
EVENTS_NAME = "events.jsonl"


def cmd_check(args: argparse.Namespace) -> int:
    try:
        plan = load_plan(args.plan)
    except LedgerError as e:
        print(f"failed to load dispatch plan: {e}", file=sys.stderr)
        return 1
    # relative paths such as repro scripts resolve against the current directory,
    # matching the default of python -m superdev.check
    problems = check_plan(plan, knowledge_dir=Path.cwd())
    if problems:
        print(f"validation found {len(problems)} issues:")
        for p in problems:
            print(f"- {p}")
        return 1
    print("validation passed: 0 issues")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    proj_dir = Path(args.project)
    try:
        plan = load_plan(proj_dir / PLAN_NAME)
    except LedgerError as e:
        print(f"failed to load ledger: {e}", file=sys.stderr)
        return 1
    db_path = proj_dir / DB_NAME
    if not db_path.exists():
        print(f"projection database not found: {db_path}")
        print(f"run a reconciliation round first: python superdev_cli.py round {proj_dir}")
        return 0
    with Projection(db_path) as proj:
        rows = {r["task_id"]: dict(r) for r in _all_tasks(proj)}
    print(f"project {plan.project}: {len(plan.tasks)} tasks")
    for t in plan.tasks:
        red = rows.get(t.id, {}).get("red_count", 0)
        print(f"{t.id}  {t.status}  red {red}")
    return 0


def _all_tasks(proj: Projection) -> list:
    return proj.conn.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()


def cmd_round(args: argparse.Namespace) -> int:
    proj_dir = Path(args.project)
    try:
        with Projection(proj_dir / DB_NAME) as proj:
            count = proj.refresh_from_ledger(proj_dir / PLAN_NAME)
            log = EventLog(proj_dir / EVENTS_NAME)
            proj.fire_pending(log, handlers={})  # this round only reconciles: no dispatch actions registered
            pending = len(log.unfired())
    except LedgerError as e:
        print(f"failed to load ledger: {e}", file=sys.stderr)
        return 1
    print(f"projection refreshed: {count} tasks")
    print(f"pending events: {pending} (reconciliation only this round, no real dispatching)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    log = EventLog(args.log)
    ok, pos = log.verify()
    if ok:
        print(f"event log verification passed: {len(log)} events, hash chain intact")
        return 0
    print(f"event log verification failed: hash chain broken at event {pos}")
    return 1


def cmd_import_spec(args: argparse.Namespace) -> int:
    try:
        plan = import_spec(args.spec_dir, args.out_plan, max_tasks=args.max_tasks)
    except SpecImportError as e:
        print(f"spec import failed: {e}", file=sys.stderr)
        return 1
    print(f"import complete: {len(plan.tasks)} tasks written to {args.out_plan}")
    # warnings inform but never interrupt: whether to split coarse granularity is the manager's call
    warnings = getattr(plan, "warnings", [])
    if warnings:
        print(f"check warnings: {len(warnings)}")
        for w in warnings:
            print(f"- {w}")
    else:
        print("check warnings: 0")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="superdev_cli.py", description="superdev manager-side reconciliation CLI"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("check", help="validate the dispatch plan; exit 1 on problems")
    p.add_argument("plan", help="path to plan.yml")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", help="one line per task: id, status, red count")
    p.add_argument("project", help="project directory (agents/<project>)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("round", help="one reconciliation patrol: refresh the projection + count pending events")
    p.add_argument("project", help="project directory (agents/<project>)")
    p.set_defaults(func=cmd_round)

    p = sub.add_parser("verify", help="validate the event log hash chain")
    p.add_argument("log", help="event log path (events.jsonl)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("import-spec", help="spec-kit checklist to dispatch plan; over-cap gets bounced")
    p.add_argument("spec_dir", help="spec-kit directory (containing plan.md or tasks.md)")
    p.add_argument("out_plan", help="output ledger path (e.g. agents/<project>/plan.yml)")
    p.add_argument(
        "--max-tasks", type=int, default=MAX_TASKS, help=f"task-count import cap, default {MAX_TASKS}"
    )
    p.set_defaults(func=cmd_import_spec)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
