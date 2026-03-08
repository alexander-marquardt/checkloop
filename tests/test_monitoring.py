"""Tests for checkloop.monitoring — memory measurement, orphan detection, and session cleanup."""

from __future__ import annotations

import signal
from unittest import mock

import pytest

from checkloop import monitoring


class TestMeasureCurrentRssMb:
    """Tests for _measure_current_rss_mb() memory measurement."""

    def test_returns_positive_value(self) -> None:
        rss = monitoring._measure_current_rss_mb()
        assert rss > 0

    def test_fallback_on_ps_failure(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
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
        mock_result = mock.MagicMock(returncode=0, stdout="not_a_number\n")
        with mock.patch("subprocess.run", return_value=mock_result):
            result = monitoring._measure_current_rss_mb()
            assert isinstance(result, float)

    def test_ps_fails(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no ps")):
            result = monitoring._measure_current_rss_mb()
            assert isinstance(result, float)

    def test_multiline_ps_output(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="12345\n67890\n")
        with mock.patch("subprocess.run", return_value=mock_result):
            rss = monitoring._measure_current_rss_mb()
            assert abs(rss - 12345 / 1024) < 0.01

    def test_ps_output_with_leading_whitespace(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="  54321  \n")
        with mock.patch("subprocess.run", return_value=mock_result):
            rss = monitoring._measure_current_rss_mb()
            assert abs(rss - 54321 / 1024) < 0.01


class TestFindChildPids:
    """Tests for _find_child_pids()."""

    def test_returns_empty_when_no_children(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            assert monitoring._find_child_pids() == []

    def test_returns_pids(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="123\n456\n")):
            assert monitoring._find_child_pids() == [123, 456]

    def test_oserror_returns_empty(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            assert monitoring._find_child_pids() == []

    def test_invalid_pid_lines_skipped(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(
            returncode=0, stdout="123\nnot_a_number\n456\n"
        )):
            assert monitoring._find_child_pids() == [123, 456]


class TestKillOrphanedChildren:
    """Tests for _kill_orphaned_children()."""

    def test_returns_zero_when_none(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            assert monitoring._kill_orphaned_children() == 0

    def test_kills_found_children(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="999\n")):
            with mock.patch("os.kill") as mock_kill:
                killed = monitoring._kill_orphaned_children()
                assert killed == 1
                mock_kill.assert_called_once_with(999, signal.SIGKILL)

    def test_handles_already_dead_child(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="999\n")):
            with mock.patch("os.kill", side_effect=OSError("No such process")):
                killed = monitoring._kill_orphaned_children()
                assert killed == 0


class TestLogMemoryUsage:
    """Tests for _log_memory_usage() reporting."""

    def test_prints_memory_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=42.0):
            with mock.patch.object(monitoring, "_find_child_pids", return_value=[]):
                monitoring._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "42MB" in out
        assert "0 child" in out

    def test_kills_orphans_when_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=100.0):
            with mock.patch.object(monitoring, "_find_child_pids", return_value=[123, 456]):
                with mock.patch.object(monitoring, "_kill_orphaned_children", return_value=2):
                    monitoring._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "2 child" in out
        assert "Warning" in out
        assert "Killed 2" in out

    def test_orphans_found_but_none_killed(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(monitoring, "_measure_current_rss_mb", return_value=50.0):
            with mock.patch.object(monitoring, "_find_child_pids", return_value=[999]):
                with mock.patch.object(monitoring, "_kill_orphaned_children", return_value=0):
                    monitoring._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "Killed" not in out


class TestSweepPreviousSessions:
    """Tests for _sweep_previous_sessions() straggler cleanup."""

    def test_kills_stragglers_and_keeps_active_sessions(self, capsys: pytest.CaptureFixture[str]) -> None:
        monitoring._previous_session_ids[:] = [100, 200]
        with mock.patch.object(monitoring, "_find_session_pids", side_effect=[[111], []]):
            with mock.patch.object(monitoring, "_kill_pids") as mock_kill:
                monitoring._sweep_previous_sessions()
                mock_kill.assert_called_once_with([111])
        # Session 100 had stragglers so it stays; session 200 is cleared.
        assert monitoring._previous_session_ids == [100]
        out = capsys.readouterr().out
        assert "straggler" in out
        monitoring._previous_session_ids.clear()

    def test_no_stragglers_clears_all_sessions(self) -> None:
        monitoring._previous_session_ids[:] = [300, 400]
        with mock.patch.object(monitoring, "_find_session_pids", return_value=[]):
            monitoring._sweep_previous_sessions()
        assert monitoring._previous_session_ids == []


class TestKillPidsEdgeCases:
    """Edge case tests for _kill_pids()."""

    def test_empty_list_returns_zero(self) -> None:
        assert monitoring._kill_pids([]) == 0

    def test_mixed_alive_and_dead(self) -> None:
        def kill_side_effect(pid: int, sig: signal.Signals) -> None:
            if pid == 200:
                raise OSError("No such process")

        with mock.patch("os.kill", side_effect=kill_side_effect):
            killed = monitoring._kill_pids([100, 200, 300])
        assert killed == 2


class TestMeasureSessionRssMb:
    """Tests for _measure_session_rss_mb() child tree memory measurement."""

    def test_sums_multiple_processes(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="10240\n20480\n5120\n")
        with mock.patch("subprocess.run", return_value=mock_result):
            rss = monitoring._measure_session_rss_mb(12345)
        assert abs(rss - (10240 + 20480 + 5120) / 1024) < 0.01

    def test_empty_session_returns_zero(self) -> None:
        mock_result = mock.MagicMock(returncode=1, stdout="")
        with mock.patch("subprocess.run", return_value=mock_result):
            assert monitoring._measure_session_rss_mb(12345) == 0.0

    def test_oserror_returns_zero(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            assert monitoring._measure_session_rss_mb(12345) == 0.0

    def test_non_numeric_lines_skipped(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="10240\nnot_a_number\n20480\n")
        with mock.patch("subprocess.run", return_value=mock_result):
            rss = monitoring._measure_session_rss_mb(12345)
        assert abs(rss - (10240 + 20480) / 1024) < 0.01

    def test_single_process(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="  51200  \n")
        with mock.patch("subprocess.run", return_value=mock_result):
            rss = monitoring._measure_session_rss_mb(99)
        assert abs(rss - 51200 / 1024) < 0.01


class TestFindSessionPidsEdgeCases:
    """Edge case tests for _find_session_pids()."""

    def test_excludes_own_pid(self) -> None:
        my_pid = monitoring.os.getpid()
        with mock.patch.object(monitoring, "_run_pgrep", return_value=[my_pid, 999]):
            result = monitoring._find_session_pids(42)
        assert my_pid not in result
        assert result == [999]

    def test_only_own_pid_returns_empty(self) -> None:
        my_pid = monitoring.os.getpid()
        with mock.patch.object(monitoring, "_run_pgrep", return_value=[my_pid]):
            result = monitoring._find_session_pids(42)
        assert result == []
