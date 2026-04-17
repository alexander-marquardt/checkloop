"""Tests for checkloop.monitoring — memory measurement, orphan detection, and session cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
from unittest import mock

import pytest

from checkloop import monitoring
from tests.helpers import make_git_result


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
    """Tests for measure_session_rss_mb() child tree memory measurement.

    The implementation walks the ppid chain (portable on macOS + Linux) and
    sums RSS across the session-leader PID and every descendant.
    """

    def test_sums_tree_rss_including_root(self) -> None:
        with mock.patch.object(monitoring, "find_all_descendant_pids", return_value=[200, 300]), \
             mock.patch.object(monitoring, "measure_pid_rss_mb", return_value=36.0) as mock_rss:
            rss = monitoring.measure_session_rss_mb(100)
        assert rss == 36.0
        # Root is included alongside descendants (minus self).
        called_pids = mock_rss.call_args[0][0]
        assert 100 in called_pids and 200 in called_pids and 300 in called_pids

    def test_excludes_own_pid(self) -> None:
        my_pid = os.getpid()
        with mock.patch.object(monitoring, "find_all_descendant_pids", return_value=[my_pid, 200]), \
             mock.patch.object(monitoring, "measure_pid_rss_mb", return_value=5.0) as mock_rss:
            monitoring.measure_session_rss_mb(100)
        called_pids = mock_rss.call_args[0][0]
        assert my_pid not in called_pids

    def test_empty_tree_falls_back_to_root(self) -> None:
        with mock.patch.object(monitoring, "find_all_descendant_pids", return_value=[]), \
             mock.patch.object(monitoring, "measure_pid_rss_mb", return_value=12.0) as mock_rss:
            rss = monitoring.measure_session_rss_mb(100)
        assert rss == 12.0
        assert mock_rss.call_args[0][0] == {100}

    def test_propagates_measure_failure(self) -> None:
        with mock.patch.object(monitoring, "find_all_descendant_pids", return_value=[200]), \
             mock.patch.object(monitoring, "measure_pid_rss_mb", return_value=0.0):
            assert monitoring.measure_session_rss_mb(100) == 0.0


class TestFindSessionPidsEdgeCases:
    """Edge case tests for find_session_pids()."""

    def test_excludes_own_pid(self) -> None:
        my_pid = os.getpid()
        with mock.patch.object(monitoring, "find_all_descendant_pids", return_value=[my_pid, 999]):
            result = monitoring.find_session_pids(42)
        assert my_pid not in result
        assert result == [999]

    def test_only_own_pid_returns_empty(self) -> None:
        my_pid = os.getpid()
        with mock.patch.object(monitoring, "find_all_descendant_pids", return_value=[my_pid]):
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


class TestRunCmdQuietFileNotFound:
    """Tests for _run_cmd_quiet FileNotFoundError branch."""

    def test_file_not_found_returns_none(self) -> None:
        """When the binary does not exist, _run_cmd_quiet returns None."""
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
            result = monitoring._run_cmd_quiet(["nonexistent_binary", "--version"])
        assert result is None


# =============================================================================
# _parse_int_lines — edge cases
# =============================================================================


class TestParseIntLinesEdgeCases:
    """Edge cases for _parse_int_lines."""

    def test_empty_string(self) -> None:
        assert monitoring._parse_int_lines("") == []

    def test_whitespace_only(self) -> None:
        assert monitoring._parse_int_lines("   \n\t\n  ") == []

    def test_single_value(self) -> None:
        assert monitoring._parse_int_lines("42") == [42]

    def test_negative_numbers(self) -> None:
        """Negative numbers should be parsed as valid integers."""
        assert monitoring._parse_int_lines("-1\n-100") == [-1, -100]

    def test_mixed_valid_and_invalid(self) -> None:
        assert monitoring._parse_int_lines("10\nabc\n20\n\n30") == [10, 20, 30]

    def test_leading_trailing_whitespace(self) -> None:
        assert monitoring._parse_int_lines("  42  \n  99  ") == [42, 99]

    def test_float_values_rejected(self) -> None:
        """Floats are not valid ints and should be skipped."""
        assert monitoring._parse_int_lines("3.14\n42") == [42]

    def test_very_large_number(self) -> None:
        big = str(2**63)
        assert monitoring._parse_int_lines(big) == [2**63]


# =============================================================================
# _run_cmd_quiet — timeout handling
# =============================================================================


class TestRunCmdQuietTimeout:
    """Test _run_cmd_quiet timeout handling."""

    def test_timeout_returns_none(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=10),
        ):
            result = monitoring._run_cmd_quiet(["ps", "-o", "rss="])
        assert result is None


# =============================================================================
# find_all_descendant_pids — BFS tree walking
# =============================================================================


class TestFindAllDescendantPids:
    """Tests for find_all_descendant_pids() process-tree BFS walk."""

    def _make_ps_output(self, pairs: list[tuple[int, int]]) -> str:
        """Build ps -eo pid=,ppid= output from (pid, ppid) pairs."""
        return "\n".join(f"  {pid}  {ppid}" for pid, ppid in pairs) + "\n"

    def test_simple_tree(self) -> None:
        """A root with two direct children and one grandchild."""
        ps_out = self._make_ps_output([
            (100, 1),     # root_pid
            (200, 100),   # child of 100
            (300, 100),   # child of 100
            (400, 200),   # grandchild
        ])
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_out)):
            result = monitoring.find_all_descendant_pids(100)
        assert set(result) == {200, 300, 400}

    def test_excludes_self(self) -> None:
        """The current process (os.getpid()) must never appear in results."""
        my_pid = os.getpid()
        ps_out = self._make_ps_output([
            (100, 1),
            (my_pid, 100),   # this is us — should be excluded
            (300, 100),
        ])
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_out)):
            result = monitoring.find_all_descendant_pids(100)
        assert my_pid not in result
        assert set(result) == {300}

    def test_no_children(self) -> None:
        """A root with no children returns an empty list."""
        ps_out = self._make_ps_output([
            (100, 1),
            (200, 1),  # sibling, not a descendant
        ])
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_out)):
            result = monitoring.find_all_descendant_pids(100)
        assert result == []

    def test_cycle_in_process_tree(self) -> None:
        """Cycles in ps output (should not happen, but defensive) don't loop forever."""
        ps_out = self._make_ps_output([
            (100, 1),
            (200, 100),
            (300, 200),
            (200, 300),   # cycle: 200 is also a child of 300
        ])
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_out)):
            result = monitoring.find_all_descendant_pids(100)
        assert set(result) == {200, 300}

    def test_malformed_lines_skipped(self) -> None:
        """Lines with non-integer values or wrong column count are ignored."""
        ps_out = "  200  100\n  abc  100\n  300\n  400  200\n"
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_out)):
            result = monitoring.find_all_descendant_pids(100)
        assert set(result) == {200, 400}

    def test_ps_failure_returns_empty(self) -> None:
        """If ps fails, return an empty list."""
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=1)):
            result = monitoring.find_all_descendant_pids(100)
        assert result == []

    def test_ps_timeout_returns_empty(self) -> None:
        """If ps times out, return an empty list."""
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=10)):
            result = monitoring.find_all_descendant_pids(100)
        assert result == []

    def test_deep_tree(self) -> None:
        """A chain of 5 descendants should all be found."""
        ps_out = self._make_ps_output([
            (100, 1),
            (200, 100),
            (300, 200),
            (400, 300),
            (500, 400),
            (600, 500),
        ])
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_out)):
            result = monitoring.find_all_descendant_pids(100)
        assert set(result) == {200, 300, 400, 500, 600}

    def test_setsid_escaped_descendants(self) -> None:
        """Descendants that called setsid() still appear in ps output.

        setsid() changes the session ID but not the parent PID, so the
        process tree walk still finds them.
        """
        ps_out = self._make_ps_output([
            (100, 1),      # root
            (200, 100),    # normal child
            (300, 200),    # child that called setsid() — still has ppid=200
        ])
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_out)):
            result = monitoring.find_all_descendant_pids(100)
        assert set(result) == {200, 300}

    def test_refuses_pid_zero(self) -> None:
        """Walking from PID 0 (kernel) would return the whole system; refuse."""
        with mock.patch("subprocess.run") as mock_run:
            result = monitoring.find_all_descendant_pids(0)
        assert result == []
        mock_run.assert_not_called()

    def test_refuses_pid_one(self) -> None:
        """Walking from PID 1 (launchd/init) would kill the user's session; refuse.

        Regression guard: on 2026-04-17 a test passed pid=1 through a
        fallback path that SIGKILL'd the entire login session.
        """
        with mock.patch("subprocess.run") as mock_run:
            result = monitoring.find_all_descendant_pids(1)
        assert result == []
        mock_run.assert_not_called()

    def test_refuses_own_pid(self) -> None:
        """Walking from our own PID would return our own children; refuse."""
        with mock.patch("subprocess.run") as mock_run:
            result = monitoring.find_all_descendant_pids(os.getpid())
        assert result == []
        mock_run.assert_not_called()


class TestSweepPreviousSessionsDescendants:
    """Tests for _sweep_previous_sessions() descendant cleanup."""

    def test_kills_tracked_descendants(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Tracked descendants in previous_descendant_pids should be killed and cleared."""
        monitoring.previous_descendant_pids.update({1001, 1002, 1003})
        monitoring.previous_session_ids[:] = []
        with mock.patch.object(monitoring, "kill_pids", return_value=3) as mock_kill:
            monitoring._sweep_previous_sessions()
        # Should have been called with the tracked descendants
        mock_kill.assert_called_once()
        assert set(mock_kill.call_args[0][0]) == {1001, 1002, 1003}
        # Descendants should be cleared after sweep
        assert monitoring.previous_descendant_pids == set()
        out = capsys.readouterr().out
        assert "tracked descendant" in out

    def test_no_descendants_skips_kill(self) -> None:
        """When previous_descendant_pids is empty, kill_pids is not called for descendants."""
        monitoring.previous_descendant_pids.clear()
        monitoring.previous_session_ids[:] = []
        with mock.patch.object(monitoring, "kill_pids") as mock_kill:
            monitoring._sweep_previous_sessions()
        mock_kill.assert_not_called()


class TestCleanupAllSessionsDescendants:
    """Tests for cleanup_all_sessions() descendant cleanup."""

    def test_kills_tracked_descendants(self) -> None:
        """cleanup_all_sessions should kill tracked descendant PIDs."""
        monitoring.previous_descendant_pids.update({2001, 2002})
        monitoring.previous_session_ids[:] = []
        with mock.patch.object(monitoring, "kill_pids", return_value=2) as mock_kill, \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
            monitoring.cleanup_all_sessions()
        # kill_pids called once for descendants
        mock_kill.assert_called_once()
        assert set(mock_kill.call_args[0][0]) == {2001, 2002}
        assert monitoring.previous_descendant_pids == set()

    def test_kills_descendants_and_sessions(self) -> None:
        """cleanup_all_sessions handles both sessions and descendants."""
        monitoring.previous_session_ids[:] = [100]
        monitoring.previous_descendant_pids.update({3001})
        with mock.patch.object(monitoring, "find_session_pids", return_value=[111]) as mock_find, \
             mock.patch.object(monitoring, "kill_pids") as mock_kill, \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
            monitoring.cleanup_all_sessions()
        # kill_pids called for session stragglers and for descendants
        assert mock_kill.call_count == 2
        assert monitoring.previous_descendant_pids == set()


# =============================================================================
# snapshot_process_rss — per-PID RSS+command snapshots
# =============================================================================


class TestSnapshotProcessRss:
    """Tests for snapshot_process_rss() per-PID forensic snapshots."""

    def test_empty_pids_returns_empty(self) -> None:
        assert monitoring.snapshot_process_rss(set()) == []
        assert monitoring.snapshot_process_rss([]) == []

    def test_parses_multi_process_output(self) -> None:
        ps_output = "  1001  51200  /usr/bin/node\n  1002  10240  python3\n"
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_output)):
            result = monitoring.snapshot_process_rss({1001, 1002})
        assert len(result) == 2
        pids = {entry[0] for entry in result}
        assert pids == {1001, 1002}
        # RSS should be converted from KB to MB
        for pid, rss_mb, cmd in result:
            if pid == 1001:
                assert abs(rss_mb - 51200 / 1024) < 0.01
                assert cmd == "/usr/bin/node"
            elif pid == 1002:
                assert abs(rss_mb - 10240 / 1024) < 0.01
                assert cmd == "python3"

    def test_ps_failure_returns_empty(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=1)):
            result = monitoring.snapshot_process_rss({1001})
        assert result == []

    def test_ps_oserror_returns_empty(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            result = monitoring.snapshot_process_rss({1001})
        assert result == []

    def test_malformed_lines_skipped(self) -> None:
        """Lines with non-integer PIDs or insufficient columns are skipped."""
        ps_output = "  abc  1024  node\n  1001  2048  python\n  short\n"
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_output)):
            result = monitoring.snapshot_process_rss({1001})
        assert len(result) == 1
        assert result[0][0] == 1001

    def test_missing_command_column(self) -> None:
        """When ps output has no command column, cmd should be empty string."""
        ps_output = "  1001  2048\n"
        with mock.patch("subprocess.run", return_value=make_git_result(stdout=ps_output)):
            result = monitoring.snapshot_process_rss({1001})
        assert len(result) == 1
        assert result[0][2] == ""

    def test_dead_pids_silently_omitted(self) -> None:
        """When ps returns empty/failure for dead PIDs, result is empty."""
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="")):
            result = monitoring.snapshot_process_rss({99999})
        assert result == []


# =============================================================================
# verify_pids_dead — post-kill verification
# =============================================================================


class TestVerifyPidsDead:
    """Tests for verify_pids_dead() kill verification."""

    def test_all_dead_returns_empty(self) -> None:
        """When all PIDs are dead, returns empty list."""
        with mock.patch("os.kill", side_effect=OSError("No such process")):
            result = monitoring.verify_pids_dead([100, 200, 300])
        assert result == []

    def test_survivors_returned(self) -> None:
        """PIDs that are still alive are returned."""
        def kill_side_effect(pid: int, sig: int) -> None:
            if pid == 200:
                return  # alive
            raise OSError("No such process")

        with mock.patch("os.kill", side_effect=kill_side_effect), \
             mock.patch.object(monitoring, "snapshot_process_rss", return_value=[]):
            result = monitoring.verify_pids_dead([100, 200, 300])
        assert result == [200]

    def test_survivors_logged_with_snapshot(self) -> None:
        """Surviving PIDs trigger a snapshot_process_rss call for forensics."""
        with mock.patch("os.kill"):  # all alive
            with mock.patch.object(monitoring, "snapshot_process_rss",
                                   return_value=[(100, 50.0, "node"), (200, 30.0, "python")]) as mock_snap:
                result = monitoring.verify_pids_dead([100, 200])
        assert set(result) == {100, 200}
        mock_snap.assert_called_once()

    def test_empty_pids_returns_empty(self) -> None:
        result = monitoring.verify_pids_dead([])
        assert result == []

    def test_accepts_set_input(self) -> None:
        with mock.patch("os.kill", side_effect=OSError("No such process")):
            result = monitoring.verify_pids_dead({100, 200})
        assert result == []


# =============================================================================
# measure_pid_rss_mb — arbitrary PID set measurement
# =============================================================================


class TestMeasurePidRssMb:
    """Tests for measure_pid_rss_mb() arbitrary PID set measurement."""

    def test_empty_set_returns_zero(self) -> None:
        assert monitoring.measure_pid_rss_mb(set()) == 0.0

    def test_sums_rss_for_pid_set(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="10240\n20480\n")):
            rss = monitoring.measure_pid_rss_mb({100, 200})
        assert abs(rss - (10240 + 20480) / 1024) < 0.01

    def test_ps_failure_returns_zero(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=1)):
            assert monitoring.measure_pid_rss_mb({100}) == 0.0


# =============================================================================
# log_memory_usage — residual RSS reporting
# =============================================================================


class TestLogMemoryUsageResidualRss:
    """Tests for log_memory_usage() residual RSS measurement across sessions and descendants."""

    def test_reports_session_and_descendant_rss(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Residual RSS from sessions and descendants appears in output."""
        monitoring.previous_session_ids[:] = [100]
        monitoring.previous_descendant_pids.update({200, 300})
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=42.0), \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]), \
             mock.patch.object(monitoring, "measure_session_rss_mb", return_value=150.0), \
             mock.patch.object(monitoring, "measure_pid_rss_mb", return_value=50.0):
            monitoring.log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "42MB" in out
        assert "200MB residual" in out  # 150 + 50
        monitoring.previous_session_ids.clear()
        monitoring.previous_descendant_pids.clear()

    def test_no_residual_when_nothing_tracked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When no sessions or descendants are tracked, no residual is shown."""
        monitoring.previous_session_ids[:] = []
        monitoring.previous_descendant_pids.clear()
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=42.0), \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
            monitoring.log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "42MB" in out
        assert "residual" not in out

    def test_zero_residual_not_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When residual RSS is 0, it's not displayed in output."""
        monitoring.previous_session_ids[:] = [100]
        monitoring.previous_descendant_pids.clear()
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=42.0), \
             mock.patch.object(monitoring, "_find_child_pids", return_value=[]), \
             mock.patch.object(monitoring, "measure_session_rss_mb", return_value=0.0):
            monitoring.log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "residual" not in out
        monitoring.previous_session_ids.clear()


# =============================================================================
# kill_session_stragglers — verify_pids_dead integration
# =============================================================================


class TestKillSessionStragglersVerification:
    """Tests for kill_session_stragglers() post-kill verification."""

    def test_calls_verify_after_kill(self) -> None:
        """After killing stragglers, verify_pids_dead is called to confirm."""
        with mock.patch.object(monitoring, "find_session_pids", return_value=[111, 222]), \
             mock.patch.object(monitoring, "kill_pids", return_value=2), \
             mock.patch.object(monitoring, "verify_pids_dead", return_value=[]) as mock_verify:
            monitoring.kill_session_stragglers(42)
        mock_verify.assert_called_once_with([111, 222])

    def test_no_verify_when_nothing_killed(self) -> None:
        """When kill_pids returns 0 (all already dead), verify is not called."""
        with mock.patch.object(monitoring, "find_session_pids", return_value=[111]), \
             mock.patch.object(monitoring, "kill_pids", return_value=0), \
             mock.patch.object(monitoring, "verify_pids_dead") as mock_verify:
            monitoring.kill_session_stragglers(42)
        mock_verify.assert_not_called()
