"""superdev.guard 的测试：tasklist 解析（样例 CSV 字符串喂入）、
超限处置清单、10% 过载边界。terminate 有两处：签名校验 + 一次性真杀
（只杀测试自己起的子进程，finally 兜底防泄漏）。"""

import inspect
import subprocess
import sys

import pytest

from superdev import guard

# 真机 `tasklist /FO CSV /NH` 的样例输出（列：映像名/PID/会话名/会话#/内存）
SAMPLE_CSV = (
    '"System Idle Process","0","Services","0","8 K"\r\n'
    '"python.exe","1234","Console","1","150,324 K"\r\n'
    '"node.exe","5678","Console","1","1,048,576 K"\r\n'
)

GARBAGE_CSV = (
    "\r\n"
    '"onlyonefield"\r\n'
    '"bad.exe","notapid","Console","1","10 K"\r\n'
    '"weird.exe","99","Console","1","lots"\r\n'
    '"good.exe","100","Console","1","1,024 K"\r\n'
)


def _proc(pid, name, mb):
    return {"pid": pid, "name": name, "memory_mb": mb, "cmdline": ""}


class TestParseTasklist:
    def test_row_count_and_fields(self):
        procs = guard.parse_tasklist(SAMPLE_CSV)
        assert len(procs) == 3
        first = procs[0]
        assert first["pid"] == 0
        assert first["name"] == "System Idle Process"
        assert first["memory_mb"] == pytest.approx(8 / 1024)
        assert first["cmdline"] == ""  # tasklist 不给命令行，约定留空

    def test_pid_is_int(self):
        procs = guard.parse_tasklist(SAMPLE_CSV)
        assert all(isinstance(p["pid"], int) for p in procs)

    def test_mem_usage_units(self):
        procs = guard.parse_tasklist(SAMPLE_CSV)
        by_pid = {p["pid"]: p for p in procs}
        assert by_pid[1234]["memory_mb"] == pytest.approx(150324 / 1024)
        assert by_pid[5678]["memory_mb"] == pytest.approx(1024.0)  # 1,048,576 K

    def test_skips_garbage_rows(self):
        procs = guard.parse_tasklist(GARBAGE_CSV)
        assert [p["pid"] for p in procs] == [100]

    def test_empty_text(self):
        assert guard.parse_tasklist("") == []
        assert guard.parse_tasklist("\r\n\r\n") == []


class TestSnapshot:
    def test_snapshot_on_real_machine(self):
        procs = guard.snapshot()  # Windows 标准库实现，真调 tasklist
        assert isinstance(procs, list)
        assert len(procs) > 0  # 系统里总有进程
        sample = procs[0]
        assert isinstance(sample["pid"], int)
        assert isinstance(sample["name"], str) and sample["name"]
        assert sample["memory_mb"] >= 0
        assert sample["cmdline"] == ""


class TestEnforce:
    BIG = [_proc(1, "engine_a.exe", 9000.0), _proc(2, "engine_b.exe", 8000.0)]

    def test_over_limit_listed(self):
        hits = guard.enforce(self.BIG, mem_limit_mb=8192)
        assert len(hits) == 1
        assert hits[0] == {
            "pid": 1,
            "name": "engine_a.exe",
            "memory_mb": 9000.0,
            "action": "结束",
        }

    def test_boundary_equal_not_listed(self):
        exact = [_proc(9, "edge.exe", 8192.0)]
        assert guard.enforce(exact, mem_limit_mb=8192) == []  # 恰好等于不算超

    def test_all_below_returns_empty(self):
        small = [_proc(1, "a.exe", 100.0), _proc(2, "b.exe", 200.0)]
        assert guard.enforce(small, mem_limit_mb=8192) == []

    def test_empty_snapshot(self):
        assert guard.enforce([], mem_limit_mb=8192) == []


class TestOverload:
    def test_free_below_10_percent_overloaded(self):
        snap = [_proc(1, "a.exe", 901.0)]
        assert guard.overload(snap, total_mem_mb=1000) is True

    def test_exactly_10_percent_not_overloaded(self):
        snap = [_proc(1, "a.exe", 900.0)]  # 空闲恰好 10%
        assert guard.overload(snap, total_mem_mb=1000) is False

    def test_healthy_not_overloaded(self):
        snap = [_proc(1, "a.exe", 400.0), _proc(2, "b.exe", 300.0)]
        assert guard.overload(snap, total_mem_mb=1000) is False

    def test_empty_snapshot_not_overloaded(self):
        assert guard.overload([], total_mem_mb=1000) is False


class TestTerminate:
    def test_defined_callable_never_invoked(self):
        # 只验签名与可调用，绝不真杀进程
        assert callable(guard.terminate)
        params = inspect.signature(guard.terminate).parameters
        assert list(params) == ["pid"]

    def test_real_kill_terminates_subprocess(self):
        # 真杀安全测试：起一个睡 60 秒的子进程，terminate 后 10 秒内必须退出
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            guard.terminate(proc.pid)
            proc.wait(timeout=10)  # 超时未退出会抛 TimeoutExpired，测试即红
            assert proc.returncode is not None  # 被杀退出，returncode 有值
        finally:
            if proc.poll() is None:  # 兜底防进程泄漏
                proc.kill()
                proc.wait(timeout=10)
