"""Tests for superdev.guard: tasklist parsing (fed a sample CSV string), the
over-limit action list, and the 10% overload boundary. terminate appears twice:
a signature check + a one-shot real kill (only kills a subprocess the test
itself started, with a finally fallback to prevent leaks)."""

import inspect
import subprocess
import sys

import pytest

from superdev import guard

# Sample output of a real `tasklist /FO CSV /NH` (columns: image name/PID/session name/session#/memory)
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
        assert first["cmdline"] == ""  # tasklist gives no command line; left empty by convention

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
        procs = guard.snapshot()  # Windows standard-library implementation; really calls tasklist
        assert isinstance(procs, list)
        assert len(procs) > 0  # a live system always has processes
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
            "action": "terminate",
        }

    def test_boundary_equal_not_listed(self):
        exact = [_proc(9, "edge.exe", 8192.0)]
        assert guard.enforce(exact, mem_limit_mb=8192) == []  # exactly at the limit does not count as over

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
        snap = [_proc(1, "a.exe", 900.0)]  # free is exactly 10%
        assert guard.overload(snap, total_mem_mb=1000) is False

    def test_healthy_not_overloaded(self):
        snap = [_proc(1, "a.exe", 400.0), _proc(2, "b.exe", 300.0)]
        assert guard.overload(snap, total_mem_mb=1000) is False

    def test_empty_snapshot_not_overloaded(self):
        assert guard.overload([], total_mem_mb=1000) is False


class TestTerminate:
    def test_defined_callable_never_invoked(self):
        # only checks the signature and callability; never kills a real process
        assert callable(guard.terminate)
        params = inspect.signature(guard.terminate).parameters
        assert list(params) == ["pid"]

    def test_real_kill_terminates_subprocess(self):
        # safe real-kill test: start a subprocess sleeping 60s; it must exit within 10s of terminate
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            guard.terminate(proc.pid)
            proc.wait(timeout=10)  # not exiting in time raises TimeoutExpired and turns the test red
            assert proc.returncode is not None  # killed and exited: returncode is set
        finally:
            if proc.poll() is None:  # fallback so no process leaks
                proc.kill()
                proc.wait(timeout=10)
