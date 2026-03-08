"""Tests for checkloop.monitoring — memory measurement, orphan detection, and session cleanup."""

from __future__ import annotations

import os
import signal
from unittest import mock

import pytest

from checkloop import monitoring
from helpers import make_git_result


class TestMeasureCurrentRssMb:
    """Tests for _measure_current_rss_mb() memory measurement."""

    def test_returns_positive_value(self) -> None:
        rss = monitoring._measure_current_rss_mb()
        assert rss > 0

    def test_fallback_on_ps_failure(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=1)):
            rss = monitoring._measure_current_rss_mb()
            assert rss > 0

    def test_oserror_falls_back_to_resource(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            rss = monitoring._measure_current_rss_mb()
            assert rss > 0

    def test_valueerror_falls_back_to_resource(self) -> None:
        with mock.patch("subprocess.run", side_effect=ValueError("bad")):
            rss = monitoring._measure_current_rss_mb()
            assert rss > 0

    def test_getrusage_oserror_returns_zero(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            with mock.patch("resource.getrusage", side_effect=OSError("no resource")):
                rss = monitoring._measure_current_rss_mb()
                assert rss == 0.0

    def test_ps_returns_non_numeric(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="not_a_number\n")):
            result = monitoring._measure_current_rss_mb()
            assert isinstance(result, float)

    def test_ps_fails(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no ps")):
            result = monitoring._measure_current_rss_mb()
            assert isinstance(result, float)

    def test_multiline_ps_output(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="12345\n67890\n")):
            rss = monitoring._measure_current_rss_mb()
            # _sum_rss_from_ps sums all lines (12345 + 67890 KB)
            assert abs(rss - (12345 + 67890) / 1024) < 0.01

    def test_ps_output_with_leading_whitespace(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="  54321  \n")):
            rss = monitoring._measure_current_rss_mb()
            assert abs(rss - 54321 / 1024) < 0.01


class TestFindChildPids:
    """Tests for _find_child_pids()."""

    def test_returns_empty_when_no_children(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=1)):
            assert monitoring._find_child_pids() == []

    def test_returns_pids(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="123\n456\n")):
            assert monitoring._find_child_pids() == [123, 456]

    def test_oserror_returns_empty(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            assert monitoring._find_child_pids() == []

    def test_invalid_pid_lines_skipped(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="123\nnot_a_number\n456\n")):
            assert monitoring._find_child_pids() == [123, 456]


class TestKillOrphanedChildren:
    """Tests for _kill_orphaned_children()."""

    def test_returns_zero_when_none(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=1)):
            assert monitoring._kill_orphaned_children() == 0

    def test_kills_found_children(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="999\n")):
            with mock.patch("os.kill") as mock_kill:
                killed = monitoring._kill_orphaned_children()
                assert killed == 1
                mock_kill.assert_called_once_with(999, signal.SIGKILL)

    def test_handles_already_dead_child(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="999\n")):
            with mock.patch("os.kill", side_effect=OSError("No such process")):
                killed = monitoring._kill_orphaned_children()
                assert killed == 0


class TestLogMemoryUsage:
    """Tests for log_memory_usage() reporting."""

    def test_prints_memory_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=42.0):
            with mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
                monitoring.log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "42MB" in out
        assert "0 child" in out

    def test_kills_orphans_when_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=100.0):
            with mock.patch.object(monitoring, "_find_child_pids", return_value=[123, 456]):
                with mock.patch.object(monitoring, "_kill_orphaned_children", return_value=2):
                    monitoring.log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "2 child" in out
        assert "Warning" in out
        assert "Killed 2" in out

    def test_orphans_found_but_none_killed(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=50.0):
            with mock.patch.object(monitoring, "_find_child_pids", return_value=[999]):
                with mock.patch.object(monitoring, "_kill_orphaned_children", return_value=0):
                    monitoring.log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "Killed" not in out


class TestKillSessionStragglers:
    """Tests for kill_session_stragglers() post-group-kill cleanup."""

    def test_kills_stragglers_when_found(self) -> None:
        with mock.patch.object(monitoring, "find_session_pids", return_value=[111, 222]):
            with mock.patch.object(monitoring, "kill_pids") as mock_kill:
                killed = monitoring.kill_session_stragglers(42)
                mock_kill.assert_called_once_with([111, 222])
                assert killed == mock_kill.return_value

    def test_no_op_when_no_stragglers(self) -> None:
        with mock.patch.object(monitoring, "find_session_pids", return_value=[]):
            with mock.patch.object(monitoring, "kill_pids") as mock_kill:
                killed = monitoring.kill_session_stragglers(42)
                mock_kill.assert_not_called()
                assert killed == 0


class TestSweepPreviousSessions:
    """Tests for _sweep_previous_sessions() straggler cleanup."""

    def test_kills_stragglers_and_keeps_active_sessions(self, capsys: pytest.CaptureFixture[str]) -> None:
        monitoring.previous_session_ids[:] = [100, 200]
        with mock.patch.object(monitoring, "find_session_pids", side_effect=[[111], []]):
            with mock.patch.object(monitoring, "kill_pids") as mock_kill:
                monitoring._sweep_previous_sessions()
                mock_kill.assert_called_once_with([111])
        # Session 100 had stragglers so it stays; session 200 is cleared.
        assert monitoring.previous_session_ids == [100]
        out = capsys.readouterr().out
        assert "straggler" in out
        monitoring.previous_session_ids.clear()

    def test_no_stragglers_clears_all_sessions(self) -> None:
        monitoring.previous_session_ids[:] = [300, 400]
        with mock.patch.object(monitoring, "find_session_pids", return_value=[]):
            monitoring._sweep_previous_sessions()
        assert monitoring.previous_session_ids == []


class TestKillPidsEdgeCases:
    """Edge case tests for kill_pids()."""

    def test_empty_list_returns_zero(self) -> None:
        assert monitoring.kill_pids([]) == 0

    def test_mixed_alive_and_dead(self) -> None:
        def kill_side_effect(pid: int, sig: signal.Signals) -> None:
            if pid == 200:
                raise OSError("No such process")

        with mock.patch("os.kill", side_effect=kill_side_effect):
            killed = monitoring.kill_pids([100, 200, 300])
        assert killed == 2


class TestMeasureSessionRssMb:
    """Tests for measure_session_rss_mb() child tree memory measurement."""

    def test_sums_multiple_processes(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="10240\n20480\n5120\n")):
            rss = monitoring.measure_session_rss_mb(12345)
        assert abs(rss - (10240 + 20480 + 5120) / 1024) < 0.01

    def test_empty_session_returns_zero(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=1)):
            assert monitoring.measure_session_rss_mb(12345) == 0.0

    def test_oserror_returns_zero(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            assert monitoring.measure_session_rss_mb(12345) == 0.0

    def test_non_numeric_lines_skipped(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="10240\nnot_a_number\n20480\n")):
            rss = monitoring.measure_session_rss_mb(12345)
        assert abs(rss - (10240 + 20480) / 1024) < 0.01

    def test_single_process(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="  51200  \n")):
            rss = monitoring.measure_session_rss_mb(99)
        assert abs(rss - 51200 / 1024) < 0.01


class TestFindSessionPidsEdgeCases:
    """Edge case tests for find_session_pids()."""

    def test_excludes_own_pid(self) -> None:
        my_pid = os.getpid()
        with mock.patch.object(monitoring, "_run_pgrep", return_value=[my_pid, 999]):
            result = monitoring.find_session_pids(42)
        assert my_pid not in result
        assert result == [999]

    def test_only_own_pid_returns_empty(self) -> None:
        my_pid = os.getpid()
        with mock.patch.object(monitoring, "_run_pgrep", return_value=[my_pid]):
            result = monitoring.find_session_pids(42)
        assert result == []


class TestCleanupAllSessions:
    """Tests for cleanup_all_sessions() atexit handler."""

    def test_kills_tracked_sessions_and_children(self) -> None:
        """cleanup_all_sessions kills pids from tracked sessions and direct children."""
        monitoring.previous_session_ids[:] = [100, 200]
        with mock.patch.object(monitoring, "find_session_pids", side_effect=[[111], [222]]) as mock_find, \
             mock.patch.object(monitoring, "kill_pids") as mock_kill, \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[333]):
            monitoring.cleanup_all_sessions()
            # Should have killed session pids and child pids
            assert mock_kill.call_count == 3  # session 100, session 200, children
        assert monitoring.previous_session_ids == []

    def test_no_sessions_no_children(self) -> None:
        """cleanup_all_sessions handles empty state gracefully."""
        monitoring.previous_session_ids[:] = []
        with mock.patch.object(monitoring, "find_session_pids", return_value=[]) as mock_find, \
             mock.patch.object(monitoring, "kill_pids") as mock_kill, \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
            monitoring.cleanup_all_sessions()
            mock_kill.assert_not_called()

    def test_sessions_with_no_pids(self) -> None:
        """cleanup_all_sessions handles sessions with no remaining pids."""
        monitoring.previous_session_ids[:] = [500]
        with mock.patch.object(monitoring, "find_session_pids", return_value=[]), \
             mock.patch.object(monitoring, "kill_pids") as mock_kill, \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
            monitoring.cleanup_all_sessions()
            mock_kill.assert_not_called()
        assert monitoring.previous_session_ids == []


class TestLogMemoryUsageException:
    """Tests for log_memory_usage exception handling."""

    def test_exception_during_monitoring_is_caught(self) -> None:
        """When _measure_current_rss_mb raises, log_memory_usage does not propagate."""
        with mock.patch.object(monitoring, "_measure_current_rss_mb", side_effect=RuntimeError("boom")):
            # Should not raise
            monitoring.log_memory_usage("test-label")


class TestSweepPreviousSessionsException:
    """Tests for _sweep_previous_sessions exception handling."""

    def test_exception_during_sweep_is_caught(self) -> None:
        """When find_session_pids raises, the sweep continues without propagating."""
        monitoring.previous_session_ids[:] = [100, 200]
        with mock.patch.object(monitoring, "find_session_pids", side_effect=RuntimeError("boom")):
            # Should not raise — exceptions are caught and logged
            monitoring._sweep_previous_sessions()
        # Sessions with exceptions are not added to still_active, so the list is cleared
        assert monitoring.previous_session_ids == []


class TestCleanupAllSessionsExceptions:
    """Tests for cleanup_all_sessions exception handling."""

    def test_session_cleanup_exception_is_caught(self) -> None:
        """When find_session_pids raises during cleanup, it does not propagate."""
        monitoring.previous_session_ids[:] = [100]
        with mock.patch.object(monitoring, "find_session_pids", side_effect=RuntimeError("boom")), \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
            # Should not raise
            monitoring.cleanup_all_sessions()
        assert monitoring.previous_session_ids == []

    def test_child_cleanup_exception_is_caught(self) -> None:
        """When _find_child_pids raises during cleanup, it does not propagate."""
        monitoring.previous_session_ids[:] = []
        with mock.patch.object(monitoring, "_find_child_pids", side_effect=RuntimeError("boom")):
            # Should not raise
            monitoring.cleanup_all_sessions()


class TestMonitoringEdgeCases:
    """Edge cases for monitoring module."""

    def test_sum_rss_from_ps_empty_stdout(self) -> None:
        """ps returning empty stdout should return 0.0."""
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="")):
            assert monitoring._sum_rss_from_ps("-p", "1") == 0.0

    def test_sum_rss_from_ps_whitespace_only_stdout(self) -> None:
        """ps returning whitespace-only stdout should return 0.0."""
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="   \n  \n")):
            result = monitoring._sum_rss_from_ps("-p", "1")
            assert result == 0.0

    def test_sum_rss_from_ps_all_non_numeric(self) -> None:
        """ps returning only non-numeric lines should return 0.0."""
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="abc\ndef\n")):
            result = monitoring._sum_rss_from_ps("-p", "1")
            assert result == 0.0

    def test_kill_pids_single_pid(self) -> None:
        """Killing a single PID should return 1 if successful."""
        with mock.patch("os.kill"):
            assert monitoring.kill_pids([42]) == 1

    def test_kill_pids_all_dead(self) -> None:
        """If all PIDs are already dead, killed count should be 0."""
        with mock.patch("os.kill", side_effect=OSError("No such process")):
            assert monitoring.kill_pids([1, 2, 3]) == 0
