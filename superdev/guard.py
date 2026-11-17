"""Resource guard (guard, Windows): process snapshot, over-limit action list, overload check.

Design source: design/03-detailed-design.md §3.3 — the resource guard carries over from
bigdev unchanged: look at resources periodically, produce the action list first
(enforce, no real kill); the real kill (terminate) is left for ops to invoke
manually after confirming the list. Standard library only; Windows uses
tasklist/taskkill.
"""
from __future__ import annotations

import csv
import io
import locale
import subprocess
import sys

ProcessInfo = dict  # {"pid": int, "name": str, "memory_mb": float, "cmdline": str}

ACTION_TERMINATE = "terminate"


def _mem_usage_to_mb(text: str) -> float | None:
    """'150,324 K' -> MB; returns None when unparseable."""
    digits = text.replace(",", "").replace("K", "").strip()
    try:
        return int(digits) / 1024
    except ValueError:
        return None


def parse_tasklist(text: str) -> list[ProcessInfo]:
    """Parse `tasklist /FO CSV /NH` output.

    Column order: image name, PID, session name, session#, memory usage. cmdline
    is left empty — tasklist does not provide it; go through wmic/Win32_Process
    when needed. Bad rows (truncated rows / non-numeric PID / non-K memory) are
    skipped.
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


def _parse_ps(text: str) -> list[ProcessInfo]:
    """Parse `ps -eo pid,comm,rss --no-headers` output (POSIX fallback, for cross-platform CI)."""
    procs: list[ProcessInfo] = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            memory_mb = float(parts[2]) / 1024.0
        except ValueError:
            continue
        procs.append(
            {"pid": pid, "name": parts[1], "memory_mb": memory_mb, "cmdline": ""}
        )
    return procs


def snapshot() -> list[ProcessInfo]:
    """Current process snapshot (standard-library implementation). Windows goes through tasklist, POSIX through ps; if neither exists, raises OSError."""
    encoding = locale.getpreferredencoding(False) or "utf-8"
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True
        )
        return parse_tasklist(result.stdout.decode(encoding, errors="replace"))
    result = subprocess.run(
        ["ps", "-eo", "pid,comm,rss", "--no-headers"], capture_output=True
    )
    return _parse_ps(result.stdout.decode(encoding, errors="replace"))


def enforce(snapshot: list[ProcessInfo], mem_limit_mb: float = 8192) -> list[dict]:
    """List over-limit processes for the action list (no real kill). Strictly greater than the limit counts.

    Returns [{pid, name, memory_mb, action:"terminate"}].
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
    """Free memory below 10% of total counts as overloaded (free = total - sum of snapshot usage).

    Written as free*10 < total to dodge the floating-point error of 0.1; exactly
    10% does not count as overload.
    """
    used = sum(p["memory_mb"] for p in snapshot)
    free = total_mem_mb - used
    return free * 10 < total_mem_mb


def terminate(pid: int) -> None:
    """Really kill the process tree. Windows uses taskkill /T /F (children included); POSIX uses kill (the process group is the caller's responsibility). Only invoke after the action list has been manually confirmed."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
        )
    else:
        import signal

        try:
            import os

            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
