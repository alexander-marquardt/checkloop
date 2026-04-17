"""Tests for checkloop.telemetry — sampler, retention, and event markers.

Safety notes
------------
These tests exercise the telemetry module that was added to diagnose
system-stall incidents.  Because the module spins a background thread that
reads real ``vm_stat`` / ``/proc/meminfo`` and ``ps`` output, we are
deliberately careful to keep its side effects out of the pytest process:

* Every file operation uses ``tmp_path``.
* ``_kill_process_group`` is never exercised (the pressure-kill tests all
  mock it explicitly).
* ``conftest._block_telemetry_sampler`` patches ``telemetry.start`` /
  ``telemetry.stop`` by default; only the few lifecycle tests in this file
  opt in via ``@pytest.mark.uses_real_telemetry``.  Those tests wrap every
  ``start()`` in a ``try/finally`` that calls ``stop()`` — and the fixture
  has its own defensive ``stop()`` on teardown as a second line of defence.
* ``_collect_sample`` is patched in the lifecycle tests to a trivial no-op
  so the sampler thread cannot shell out to ``ps`` / ``vm_stat`` during
  the test (it would work, but it adds noise and a real subprocess is
  exactly the class of dependency we want to keep out of the suite).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from checkloop import process, telemetry


# ---------------------------------------------------------------------------
# System memory readers
# ---------------------------------------------------------------------------


class TestReadMacosMemory:
    """Parses ``vm_stat`` + ``sysctl`` output on Darwin."""

    _VM_STAT_FIXTURE = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               12345.\n"
        "Pages active:                             23456.\n"
        "Pages inactive:                            2000.\n"
        "Pages wired down:                          5000.\n"
        "Pages occupied by compressor:              1000.\n"
    )

    def test_parses_vm_stat_fields(self) -> None:
        def fake_run(cmd: list[str]) -> str | None:
            if cmd[0] == "vm_stat":
                return self._VM_STAT_FIXTURE
            if cmd == ["sysctl", "-n", "hw.memsize"]:
                return "17179869184\n"  # 16 GiB
            if cmd == ["sysctl", "-n", "vm.swapusage"]:
                return "total = 2048.00M  used = 500.00M  free = 1548.00M  (encrypted)\n"
            return None

        with mock.patch.object(telemetry, "_run_cmd", side_effect=fake_run):
            mem = telemetry._read_macos_memory()

        # page_size = 16384 bytes = 0.015625 MB per page.
        # free+inactive pages = 12345 + 2000 = 14345 → 14345 * 0.015625 = 224.1 MB
        assert mem["system_free_mb"] == pytest.approx(224.1, abs=0.2)
        assert mem["system_active_mb"] == pytest.approx(23456 * 0.015625, abs=0.2)
        assert mem["system_wired_mb"] == pytest.approx(5000 * 0.015625, abs=0.2)
        assert mem["system_compressed_mb"] == pytest.approx(1000 * 0.015625, abs=0.2)
        assert mem["system_total_mb"] == pytest.approx(16384.0, abs=0.2)
        assert mem["swap_used_mb"] == pytest.approx(500.0)
        assert mem["swap_total_mb"] == pytest.approx(2048.0)

    def test_missing_vm_stat_returns_empty_fields(self) -> None:
        """When vm_stat fails, swap/sysctl reads alone still produce a dict."""
        def fake_run(cmd: list[str]) -> str | None:
            if cmd[0] == "vm_stat":
                return None
            if cmd == ["sysctl", "-n", "hw.memsize"]:
                return "8589934592\n"  # 8 GiB
            return None

        with mock.patch.object(telemetry, "_run_cmd", side_effect=fake_run):
            mem = telemetry._read_macos_memory()

        assert "system_free_mb" not in mem  # vm_stat was the only source
        assert mem["system_total_mb"] == pytest.approx(8192.0, abs=0.2)


class TestReadLinuxMemory:
    """Parses ``/proc/meminfo`` on Linux."""

    def test_parses_meminfo(self, tmp_path: Path) -> None:
        """With a fake meminfo, known fields are populated and swap_used is derived."""
        fake_meminfo = tmp_path / "meminfo"
        fake_meminfo.write_text(
            "MemTotal:       16000000 kB\n"
            "MemFree:         1000000 kB\n"
            "MemAvailable:    4000000 kB\n"
            "SwapTotal:       2000000 kB\n"
            "SwapFree:        1500000 kB\n"
            "Buffers:          100000 kB\n"
        )

        real_open = open

        def fake_open(path: str, *args: object, **kwargs: object) -> object:
            if path == "/proc/meminfo":
                return real_open(fake_meminfo, *args, **kwargs)  # type: ignore[call-arg]
            return real_open(path, *args, **kwargs)  # type: ignore[call-arg]

        with mock.patch("builtins.open", side_effect=fake_open):
            mem = telemetry._read_linux_memory()

        assert mem["system_total_mb"] == pytest.approx(16000000 / 1024, abs=0.2)
        assert mem["system_free_mb"] == pytest.approx(4000000 / 1024, abs=0.2)
        assert mem["swap_total_mb"] == pytest.approx(2000000 / 1024, abs=0.2)
        assert mem["swap_used_mb"] == pytest.approx(500000 / 1024, abs=0.2)

    def test_missing_meminfo_returns_empty(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("no proc")):
            mem = telemetry._read_linux_memory()
        assert mem == {}


class TestGetSystemFreeMb:
    """``get_system_free_mb`` returns None — not 0 — when unavailable."""

    def test_returns_value_when_present(self) -> None:
        with mock.patch.object(telemetry, "read_system_memory",
                               return_value={"system_free_mb": 1234.5}):
            assert telemetry.get_system_free_mb() == 1234.5

    def test_returns_none_when_absent(self) -> None:
        """Distinguishes 'measurement unavailable' from 'genuinely zero'."""
        with mock.patch.object(telemetry, "read_system_memory", return_value={}):
            assert telemetry.get_system_free_mb() is None


# ---------------------------------------------------------------------------
# Retention / pruning
# ---------------------------------------------------------------------------


def _make_telemetry_file(dir_path: Path, suffix: str, mtime: float, size: int = 10) -> Path:
    """Create a telemetry-named file at a specific size and mtime."""
    path = dir_path / f"telemetry-{suffix}.jsonl"
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


class TestPruneOldTelemetry:
    """Tests for age-based and size-cap pruning."""

    def test_missing_directory_returns_zero(self, tmp_path: Path) -> None:
        deleted = telemetry.prune_old_telemetry(tmp_path / "does-not-exist")
        assert deleted == 0

    def test_ignores_non_telemetry_files(self, tmp_path: Path) -> None:
        # README and a random jsonl without the prefix should never be touched.
        readme = tmp_path / "README.md"
        readme.write_text("hello")
        other = tmp_path / "other.jsonl"
        other.write_text("{}")
        now = time.time()
        os.utime(readme, (now - 365 * 86400, now - 365 * 86400))
        os.utime(other, (now - 365 * 86400, now - 365 * 86400))

        deleted = telemetry.prune_old_telemetry(tmp_path, retention_days=1)
        assert deleted == 0
        assert readme.exists()
        assert other.exists()

    def test_deletes_files_older_than_retention(self, tmp_path: Path) -> None:
        now = time.time()
        keep = _make_telemetry_file(tmp_path, "2026-04-17", now - 1 * 86400)
        drop = _make_telemetry_file(tmp_path, "2026-04-01", now - 30 * 86400)

        deleted = telemetry.prune_old_telemetry(tmp_path, retention_days=14)

        assert deleted == 1
        assert keep.exists()
        assert not drop.exists()

    def test_caps_total_size_by_dropping_oldest(self, tmp_path: Path) -> None:
        """If total size exceeds cap, oldest files are removed first."""
        now = time.time()
        # Three files within retention but over the 150-byte cap.  Oldest
        # (100 bytes) should be dropped first, which already fits the cap.
        oldest = _make_telemetry_file(tmp_path, "2026-04-10", now - 6 * 86400, size=100)
        middle = _make_telemetry_file(tmp_path, "2026-04-13", now - 3 * 86400, size=80)
        newest = _make_telemetry_file(tmp_path, "2026-04-16", now - 1 * 86400, size=60)

        deleted = telemetry.prune_old_telemetry(
            tmp_path, retention_days=14, max_dir_bytes=150,
        )

        assert deleted == 1
        assert not oldest.exists()
        assert middle.exists()
        assert newest.exists()

    def test_no_deletion_when_under_cap(self, tmp_path: Path) -> None:
        now = time.time()
        f1 = _make_telemetry_file(tmp_path, "2026-04-16", now - 1 * 86400, size=50)
        f2 = _make_telemetry_file(tmp_path, "2026-04-15", now - 2 * 86400, size=50)

        deleted = telemetry.prune_old_telemetry(
            tmp_path, retention_days=14, max_dir_bytes=10_000,
        )

        assert deleted == 0
        assert f1.exists()
        assert f2.exists()


# ---------------------------------------------------------------------------
# Sample collection
# ---------------------------------------------------------------------------


class TestCollectSample:
    """``_collect_sample`` must never raise and always return a dict with core fields."""

    def test_core_fields_always_present(self) -> None:
        with mock.patch.object(telemetry.monitoring, "_measure_current_rss_mb",
                               return_value=42.0), \
             mock.patch.object(telemetry.monitoring, "_find_child_pids",
                               return_value=[]), \
             mock.patch.object(telemetry, "read_system_memory",
                               return_value={"system_free_mb": 1000.0}):
            telemetry._state.run_id = "abcd1234"
            telemetry._state.current_label = "check:x:cycle1"
            sample = telemetry._collect_sample()

        assert sample["run_id"] == "abcd1234"
        assert sample["label"] == "check:x:cycle1"
        assert sample["pid"] == os.getpid()
        assert sample["parent_rss_mb"] == 42.0
        assert sample["child_count"] == 0
        assert sample["children_rss_mb"] == 0.0
        assert sample["system_free_mb"] == 1000.0

    def test_survives_rss_measurement_failure(self) -> None:
        """A broken RSS measurement must not blow up the sampler."""
        with mock.patch.object(telemetry.monitoring, "_measure_current_rss_mb",
                               side_effect=RuntimeError("ps blew up")), \
             mock.patch.object(telemetry.monitoring, "_find_child_pids",
                               return_value=[]), \
             mock.patch.object(telemetry, "read_system_memory",
                               return_value={}):
            sample = telemetry._collect_sample()

        # Core identifying fields still present; failure captured in parent_err.
        assert "parent_err" in sample
        assert sample["pid"] == os.getpid()

    def test_top_children_limited(self) -> None:
        """The ``top_children`` list is capped at ``_TOP_CHILDREN_LIMIT`` entries,
        sorted by RSS descending."""
        many_pids = [1000 + i for i in range(20)]
        # RSS values 0..19 — largest 5 should be pids 1015..1019.
        snapshot = [(1000 + i, float(i), f"cmd{i}") for i in range(20)]
        with mock.patch.object(telemetry.monitoring, "_measure_current_rss_mb",
                               return_value=1.0), \
             mock.patch.object(telemetry.monitoring, "_find_child_pids",
                               return_value=many_pids), \
             mock.patch.object(telemetry.monitoring, "snapshot_process_rss",
                               return_value=snapshot), \
             mock.patch.object(telemetry, "read_system_memory",
                               return_value={}):
            sample = telemetry._collect_sample()

        top = sample["top_children"]
        assert isinstance(top, list)
        assert len(top) == telemetry._TOP_CHILDREN_LIMIT
        # Sorted largest-first: first entry has the largest RSS in the snapshot.
        rss_values = [entry["rss_mb"] for entry in top]
        assert rss_values == sorted(rss_values, reverse=True)
        assert rss_values[0] == 19.0


# ---------------------------------------------------------------------------
# Lifecycle (start / stop / record_event)
#
# These tests use mocked _collect_sample so the sampler thread does nothing
# but write a tiny deterministic dict and sleep.  Real subprocess reads
# (ps, vm_stat) are never made.  Each test runs for a fraction of a second
# and always stops the sampler in a finally block.
# ---------------------------------------------------------------------------


def _stub_sample() -> dict[str, object]:
    """Minimal sample used in place of the real _collect_sample during lifecycle tests."""
    return {
        "t": round(time.time(), 3),
        "iso": "2026-04-17T00:00:00",
        "run_id": telemetry._state.run_id,
        "pid": os.getpid(),
        "label": telemetry._state.current_label,
        "parent_rss_mb": 0.1,
        "children_rss_mb": 0.0,
        "child_count": 0,
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Parse every non-blank line of a JSONL file into a list of dicts."""
    result: list[dict[str, object]] = []
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        result.append(json.loads(line))
    return result


@pytest.mark.uses_real_telemetry
class TestStartStopLifecycle:
    """End-to-end start/stop — with every subprocess dependency stubbed."""

    def test_start_creates_dir_and_writes_marker(self, tmp_path: Path) -> None:
        """After start() + stop(), the jsonl file contains at least the run_start
        and run_end markers, and the directory exists with 0o700 permissions."""
        with mock.patch.object(telemetry, "_collect_sample", side_effect=_stub_sample):
            try:
                telemetry.start(str(tmp_path), run_id="test-run")
                # Give the sampler a short breather to write at least one loop sample,
                # but don't depend on it — the run_start marker is written synchronously.
                time.sleep(0.1)
            finally:
                telemetry.stop(event="test_end")

        tel_dir = tmp_path / ".checkloop-telemetry"
        assert tel_dir.is_dir()
        mode = tel_dir.stat().st_mode & 0o777
        # 0o700 is the requested mode; umask could mask bits but never add them,
        # so assert the group/other bits are not set.
        assert mode & 0o077 == 0

        files = list(tel_dir.glob("telemetry-*.jsonl"))
        assert len(files) == 1

        entries = _read_jsonl(files[0])
        events = [e.get("event") for e in entries if "event" in e]
        assert "run_start" in events
        assert "test_end" in events
        # The run_id must appear on every entry.
        assert all(e["run_id"] == "test-run" for e in entries)

    def test_double_start_is_idempotent(self, tmp_path: Path) -> None:
        """Calling start() twice must not create a second thread or second file handle."""
        with mock.patch.object(telemetry, "_collect_sample", side_effect=_stub_sample):
            try:
                telemetry.start(str(tmp_path), run_id="first")
                first_thread = telemetry._state.thread
                first_file = telemetry._state.file_handle
                telemetry.start(str(tmp_path), run_id="second")
                # Second call is a no-op: same thread, same file handle, original run_id.
                assert telemetry._state.thread is first_thread
                assert telemetry._state.file_handle is first_file
                assert telemetry._state.run_id == "first"
            finally:
                telemetry.stop()

    def test_stop_without_start_is_safe(self) -> None:
        """stop() before start() must not raise."""
        telemetry._state.thread = None
        telemetry._state.stop_event = None
        telemetry._state.file_handle = None
        telemetry.stop()  # Should simply return.

    def test_stop_joins_thread(self, tmp_path: Path) -> None:
        """After stop(), the sampler thread is no longer alive."""
        with mock.patch.object(telemetry, "_collect_sample", side_effect=_stub_sample):
            telemetry.start(str(tmp_path), run_id="join-test")
            thread = telemetry._state.thread
            assert isinstance(thread, threading.Thread)
            assert thread.is_alive()
            telemetry.stop()
            assert not thread.is_alive()
            assert telemetry._state.thread is None


@pytest.mark.uses_real_telemetry
class TestRecordEvent:
    """``record_event`` writes JSONL with extra fields; silent no-op when stopped."""

    def test_writes_event_with_fields(self, tmp_path: Path) -> None:
        with mock.patch.object(telemetry, "_collect_sample", side_effect=_stub_sample):
            try:
                telemetry.start(str(tmp_path), run_id="event-test")
                telemetry.record_event("check_start", check_id="readability", cycle=2)
            finally:
                telemetry.stop()

        files = list((tmp_path / ".checkloop-telemetry").glob("telemetry-*.jsonl"))
        entries = _read_jsonl(files[0])
        matched = [e for e in entries
                   if e.get("event") == "check_start" and e.get("check_id") == "readability"]
        assert len(matched) == 1
        assert matched[0]["cycle"] == 2

    def test_noop_when_no_file_handle(self) -> None:
        """record_event before start (or after stop) must silently return."""
        telemetry._state.file_handle = None  # defensive
        telemetry.record_event("ignored", foo="bar")
        # No exception, no state change.
        assert telemetry._state.file_handle is None


class TestSetCurrentLabel:
    """Label changes are reflected in subsequently collected samples."""

    def test_label_is_picked_up_by_sample(self) -> None:
        telemetry.set_current_label("check:readability:cycle1")
        with mock.patch.object(telemetry.monitoring, "_measure_current_rss_mb",
                               return_value=1.0), \
             mock.patch.object(telemetry.monitoring, "_find_child_pids",
                               return_value=[]), \
             mock.patch.object(telemetry, "read_system_memory",
                               return_value={}):
            sample = telemetry._collect_sample()
        assert sample["label"] == "check:readability:cycle1"


# ---------------------------------------------------------------------------
# System-pressure kill (process.py)
# ---------------------------------------------------------------------------


class TestCheckSystemPressure:
    """Tests for process._check_system_pressure — the swap-thrash safety net."""

    def _mock_proc(self, pid: int = 9999) -> mock.MagicMock:
        """Fake Popen object.  Never spawned, never killed for real."""
        mp = mock.MagicMock()
        mp.pid = pid
        return mp

    def test_disabled_when_floor_is_zero(self) -> None:
        """floor_mb=0 means the check is fully disabled, regardless of free memory."""
        with mock.patch.object(telemetry, "get_system_free_mb", return_value=10.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill:
            triggered = process._check_system_pressure(
                floor_mb=0, process=self._mock_proc(), check_start_time=time.time(),
            )
        assert triggered is False
        mock_kill.assert_not_called()

    def test_skipped_when_reading_unavailable(self) -> None:
        """If get_system_free_mb returns None, we don't kill — prefer running over
        killing spuriously on platforms where the memory read fails."""
        with mock.patch.object(telemetry, "get_system_free_mb", return_value=None), \
             mock.patch.object(process, "_kill_process_group") as mock_kill:
            triggered = process._check_system_pressure(
                floor_mb=500, process=self._mock_proc(), check_start_time=time.time(),
            )
        assert triggered is False
        mock_kill.assert_not_called()

    def test_not_triggered_above_floor(self) -> None:
        with mock.patch.object(telemetry, "get_system_free_mb", return_value=800.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill:
            triggered = process._check_system_pressure(
                floor_mb=500, process=self._mock_proc(), check_start_time=time.time(),
            )
        assert triggered is False
        mock_kill.assert_not_called()

    def test_kills_when_below_floor(self) -> None:
        """The crux of the pressure-kill feature: below floor → kill process group."""
        mp = self._mock_proc(pid=12345)
        with mock.patch.object(telemetry, "get_system_free_mb", return_value=100.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill:
            triggered = process._check_system_pressure(
                floor_mb=500, process=mp, check_start_time=time.time(),
            )
        assert triggered is True
        mock_kill.assert_called_once_with(mp)

    def test_floor_boundary_equal_is_not_killed(self) -> None:
        """free == floor uses a strict-less-than comparison, so exact match is safe."""
        with mock.patch.object(telemetry, "get_system_free_mb", return_value=500.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill:
            triggered = process._check_system_pressure(
                floor_mb=500, process=self._mock_proc(), check_start_time=time.time(),
            )
        assert triggered is False
        mock_kill.assert_not_called()


class TestCheckResourceLimitsSystemPressure:
    """Integration of pressure check into the main resource-limit loop."""

    def _mock_proc(self) -> mock.MagicMock:
        mp = mock.MagicMock()
        mp.pid = 9999
        return mp

    def test_pressure_returns_system_memory_reason(self) -> None:
        """When the pressure check fires, _check_resource_limits returns
        KILL_REASON_SYSTEM_PRESSURE."""
        now = time.time()
        mp = self._mock_proc()
        with mock.patch.object(process, "_check_system_pressure", return_value=True), \
             mock.patch.object(process, "_check_memory_limit",
                               return_value=(False, now, 0.0)):
            kill_reason, *_ = process._check_resource_limits(
                process=mp,
                check_start_time=now - 60,
                last_output_time=now,  # not idle
                idle_timeout=120,
                check_timeout=0,
                max_memory_mb=0,  # memory check disabled
                last_memory_check=now - 20,  # well past interval
                last_nudge_time=0.0,
                system_free_floor_mb=500,
            )
        assert kill_reason == process.KILL_REASON_SYSTEM_PRESSURE

    def test_pressure_check_skipped_when_interval_not_elapsed(self) -> None:
        """Pressure check is gated on the memory-check interval to avoid spamming
        ``vm_stat`` every loop iteration."""
        now = time.time()
        mp = self._mock_proc()
        with mock.patch.object(process, "_check_system_pressure") as mock_pressure, \
             mock.patch.object(process, "_check_memory_limit",
                               return_value=(False, now - 1, 0.0)):
            process._check_resource_limits(
                process=mp,
                check_start_time=now - 60,
                last_output_time=now,
                idle_timeout=120,
                check_timeout=0,
                max_memory_mb=0,
                last_memory_check=now - 1,  # checked 1s ago — under interval
                last_nudge_time=0.0,
                system_free_floor_mb=500,
            )
        mock_pressure.assert_not_called()

    def test_memory_limit_takes_precedence(self) -> None:
        """If memory limit triggers first, pressure check is not consulted."""
        now = time.time()
        mp = self._mock_proc()
        with mock.patch.object(process, "_check_system_pressure") as mock_pressure, \
             mock.patch.object(process, "_check_memory_limit",
                               return_value=(True, now, 9999.0)):
            kill_reason, *_ = process._check_resource_limits(
                process=mp,
                check_start_time=now - 60,
                last_output_time=now,
                idle_timeout=120,
                check_timeout=0,
                max_memory_mb=4096,
                last_memory_check=now - 20,
                last_nudge_time=0.0,
                system_free_floor_mb=500,
            )
        assert kill_reason == process.KILL_REASON_MEMORY
        mock_pressure.assert_not_called()
