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
