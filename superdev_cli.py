"""superdev 管理端命令行入口（设计文档 §2/§3.3 的对账面）。

五个子命令：
  check <派工计划.yml>    校验台账，有问题退出 1
  status <项目目录>       读台账+投影，每任务一行（id、状态、红次数）
  round <项目目录>        一轮巡检对账：刷新投影 + 清点待办事件（本轮不真派工）
  verify <事件日志路径>   校验事件日志哈希链
  import-spec <spec目录> <输出台账路径>  spec-kit 清单转派工计划，超限打回

项目目录指 agents/<项目>，约定里面放 派工计划.yml、projection.db、events.jsonl 三个文件。
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

PLAN_NAME = "派工计划.yml"
DB_NAME = "projection.db"
EVENTS_NAME = "events.jsonl"


def cmd_check(args: argparse.Namespace) -> int:
    try:
        plan = load_plan(args.plan)
    except LedgerError as e:
        print(f"派工计划加载失败：{e}", file=sys.stderr)
        return 1
    # 复现脚本等相对路径按当前目录解析，与 python -m superdev.check 的缺省一致
    problems = check_plan(plan, knowledge_dir=Path.cwd())
    if problems:
        print(f"校验发现 {len(problems)} 处问题：")
        for p in problems:
            print(f"- {p}")
        return 1
    print("校验通过：0 处问题")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    proj_dir = Path(args.project)
    try:
        plan = load_plan(proj_dir / PLAN_NAME)
    except LedgerError as e:
        print(f"台账读取失败：{e}", file=sys.stderr)
        return 1
    db_path = proj_dir / DB_NAME
    if not db_path.exists():
        print(f"投影库不存在：{db_path}")
        print(f"请先跑一轮对账：python superdev_cli.py round {proj_dir}")
        return 0
    with Projection(db_path) as proj:
        rows = {r["task_id"]: dict(r) for r in _all_tasks(proj)}
    print(f"项目 {plan.project}：{len(plan.tasks)} 个任务")
    for t in plan.tasks:
        red = rows.get(t.id, {}).get("red_count", 0)
        print(f"{t.id}  {t.status}  红{red}次")
    return 0


def _all_tasks(proj: Projection) -> list:
    return proj.conn.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()


def cmd_round(args: argparse.Namespace) -> int:
    proj_dir = Path(args.project)
    try:
        with Projection(proj_dir / DB_NAME) as proj:
            count = proj.refresh_from_ledger(proj_dir / PLAN_NAME)
            log = EventLog(proj_dir / EVENTS_NAME)
            proj.fire_pending(log, handlers={})  # 本轮只对账：不注册任何派发动作
            pending = len(log.unfired())
    except LedgerError as e:
        print(f"台账读取失败：{e}", file=sys.stderr)
        return 1
    print(f"投影已刷新：{count} 个任务")
    print(f"待办事件：{pending} 条（本轮只做对账，不真派工）")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    log = EventLog(args.log)
    ok, pos = log.verify()
    if ok:
        print(f"事件日志校验通过：{len(log)} 条事件，哈希链完整")
        return 0
    print(f"事件日志校验失败：第 {pos} 条事件的哈希链断裂")
    return 1


def cmd_import_spec(args: argparse.Namespace) -> int:
    try:
        plan = import_spec(args.spec_dir, args.out_plan, max_tasks=args.max_tasks)
    except SpecImportError as e:
        print(f"spec 导入失败：{e}", file=sys.stderr)
        return 1
    print(f"导入完成：{len(plan.tasks)} 个任务，已写入 {args.out_plan}")
    # 警告只提示不打断：粒度偏大之类的由管理端决定要不要拆
    warnings = getattr(plan, "warnings", [])
    if warnings:
        print(f"check 警告 {len(warnings)} 处：")
        for w in warnings:
            print(f"- {w}")
    else:
        print("check 警告：0 处")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="superdev_cli.py", description="superdev 管理端对账命令行"
    )
    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    p = sub.add_parser("check", help="校验派工计划，有问题退出 1")
    p.add_argument("plan", help="派工计划.yml 路径")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", help="每任务一行：id、状态、红次数")
    p.add_argument("project", help="项目目录（agents/<项目>）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("round", help="一轮巡检对账：刷新投影 + 清点待办事件")
    p.add_argument("project", help="项目目录（agents/<项目>）")
    p.set_defaults(func=cmd_round)

    p = sub.add_parser("verify", help="校验事件日志哈希链")
    p.add_argument("log", help="事件日志路径（events.jsonl）")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("import-spec", help="spec-kit 清单转派工计划，超限打回")
    p.add_argument("spec_dir", help="spec-kit 目录（内含 plan.md 或 tasks.md）")
    p.add_argument("out_plan", help="输出台账路径（如 agents/<项目>/派工计划.yml）")
    p.add_argument(
        "--max-tasks", type=int, default=MAX_TASKS, help=f"导入任务数上限，默认 {MAX_TASKS}"
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
