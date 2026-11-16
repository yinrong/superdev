"""资源守护（guard，Windows）：进程快照、超限处置清单、过载判定。

设计出处：design/03-详细设计.md §3.3——资源守护从 bigdev 原样保留：
周期性看资源，先出处置清单（enforce，不真杀），真杀（terminate）
留给运维确认清单后人工调用。只用标准库，Windows 的 tasklist/taskkill。
"""
from __future__ import annotations

import csv
import io
import locale
import subprocess

ProcessInfo = dict  # {"pid": int, "name": str, "memory_mb": float, "cmdline": str}

ACTION_TERMINATE = "结束"


def _mem_usage_to_mb(text: str) -> float | None:
    """'150,324 K' → MB 数；解析不了返回 None。"""
    digits = text.replace(",", "").replace("K", "").strip()
    try:
        return int(digits) / 1024
    except ValueError:
        return None


def parse_tasklist(text: str) -> list[ProcessInfo]:
    """解析 `tasklist /FO CSV /NH` 输出。

    列序：映像名、PID、会话名、会话#、内存占用。cmdline 留空——
    tasklist 不提供，需要时另走 wmic/Win32_Process。
    坏行（残行/非数字 PID/非 K 内存）直接跳过。
    """
    procs: list[ProcessInfo] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        memory_mb = _mem_usage_to_mb(row[4])
        if memory_mb is None:
            continue
        procs.append(
            {"pid": pid, "name": row[0], "memory_mb": memory_mb, "cmdline": ""}
        )
    return procs


def snapshot() -> list[ProcessInfo]:
    """当前进程快照（subprocess 调 tasklist，标准库实现）。"""
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"], capture_output=True
    )
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return parse_tasklist(result.stdout.decode(encoding, errors="replace"))


def enforce(snapshot: list[ProcessInfo], mem_limit_mb: float = 8192) -> list[dict]:
    """超限进程列入处置清单（不真杀）。严格大于限值才算超。

    返回 [{pid, name, memory_mb, action:"结束"}]。
    """
    return [
        {
            "pid": p["pid"],
            "name": p["name"],
            "memory_mb": p["memory_mb"],
            "action": ACTION_TERMINATE,
        }
        for p in snapshot
        if p["memory_mb"] > mem_limit_mb
    ]


def overload(snapshot: list[ProcessInfo], total_mem_mb: float) -> bool:
    """空闲内存低于总量 10% 判过载（空闲 = 总量 - 快照占用合计）。

    比较写成 free*10 < total，避开 0.1 的浮点误差，恰好 10% 不算过载。
    """
    used = sum(p["memory_mb"] for p in snapshot)
    free = total_mem_mb - used
    return free * 10 < total_mem_mb


def terminate(pid: int) -> None:
    """真杀进程树（taskkill /T /F，连子进程一起）。只应在处置清单被人工确认后调用。"""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
    )
