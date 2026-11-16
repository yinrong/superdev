"""任务状态机：迁移表、合法性判断、事件到迁移的映射。

设计出处：design/03-详细设计.md §3——迁移表由程序持有，AI 只能提案，
任何状态变更都要先过 can_transition/illegal 这一关。
paused 不在台账 statuses 枚举里，是运行期叠加态：暂停时记下原状态，
恢复时迁回原状态，所以 paused 可以去往任何基态。
事件 kind 沿用 events.py 常量（round_red_1/2 已合并为 ROUND_RED，
两败卡住另发 TASK_STUCK，口径见 events.py 模块注释）。
"""
from __future__ import annotations

from superdev import events as ev

# 基态 = 台账 statuses 的十个值；paused 是运行期叠加态，见模块注释
BASE_STATUSES = [
    "draft", "ready", "assigned", "in_progress", "evidence_wait",
    "stuck", "escalated", "gating", "merged", "done",
]
ALL_STATUSES = BASE_STATUSES + ["paused"]

# §3.1 状态图的完整迁移表；paused 行的含义是"恢复到原状态"
ALLOWED: dict[str, set[str]] = {
    "draft": {"ready", "paused"},
    "ready": {"assigned", "paused"},
    "assigned": {"in_progress", "paused"},
    "in_progress": {"evidence_wait", "stuck", "paused"},
    "evidence_wait": {"gating", "stuck", "paused"},
    "stuck": {"ready", "escalated", "paused"},
    "escalated": {"ready", "paused"},
    "gating": {"merged", "paused"},
    "merged": {"done", "paused"},
    "done": {"paused"},
    "paused": set(BASE_STATUSES),
}


def can_transition(cur: str, nxt: str) -> bool:
    """cur 是否允许直接迁移到 nxt。未知状态一律不允许。"""
    return nxt in ALLOWED.get(cur, set())


def illegal(cur: str, nxt: str) -> str | None:
    """返回迁移不合法的中文原因；合法返回 None。"""
    if cur not in ALLOWED:
        return f"未知状态 '{cur}'，合法状态为：{'、'.join(ALL_STATUSES)}"
    if nxt not in ALLOWED:
        return f"未知状态 '{nxt}'，合法状态为：{'、'.join(ALL_STATUSES)}"
    if nxt not in ALLOWED[cur]:
        targets = "、".join(sorted(ALLOWED[cur]))
        return f"不允许从 {cur} 迁移到 {nxt}（{cur} 的合法目标：{targets}）"
    return None


# §3.2 迁移触发表：kind -> (要求的当前状态, 目标状态, 触发动作)
# ROUND_RED 特殊：第一败/回滚都不迁移（from == to），两败由 TASK_STUCK 走 stuck
EVENT_RULES: dict[str, tuple[str, str, str]] = {
    ev.TASK_CREATED: ("draft", "ready", "检查通过后刷新投影"),
    ev.WORKER_IDLE: ("ready", "assigned", "派发器派工"),
    ev.ROUND_GREEN: ("in_progress", "evidence_wait", "收证据，通知门禁"),
    ev.ROUND_RED: ("in_progress", "in_progress", "回滚回喂"),
    ev.TASK_STUCK: ("in_progress", "stuck", "触发事后研究"),
    ev.REPRO_SPLIT: ("stuck", "ready", "生成复现脚本契约"),
    ev.EXPERT_DECIDED: ("escalated", "ready", "新策略写回台账"),
    ev.ALL_GREEN: ("gating", "merged", "合 main，更新绿点"),
}


def handle_event(kind: str, task_status: str) -> tuple[str | None, str]:
    """把事件映射成 (新状态, 动作名)。

    成功返回 (新状态, 动作)；事件与状态不匹配或 kind 未知时返回
    (None, 中文错误原因)，错误原因与 illegal() 同口径。
    METRICS_STALL 是项目级事件（§3.2 末行）：不迁移任务状态，
    只做项目计数，两次触发即暂停派发并升级。
    """
    if kind == ev.METRICS_STALL:
        if task_status not in ALLOWED:
            return None, illegal(task_status, task_status) or ""
        return task_status, "项目级计数，两次触发暂停派发并升级"
    if kind not in EVENT_RULES:
        return None, f"未知事件类型 '{kind}'，合法类型：{'、'.join(EVENT_RULES)}、{ev.METRICS_STALL}"
    want, to, action = EVENT_RULES[kind]
    if task_status != want:
        return None, f"事件 {kind} 要求任务处于 {want} 状态，现在是 {task_status}"
    return to, action
