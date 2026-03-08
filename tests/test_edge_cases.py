"""Edge case and boundary condition tests for the checkloop codebase.

Covers: exception chaining in git operations, _parse_duration edge cases,
_format_checkpoint_summary bounds, compute_change_stats boundary conditions,
_run_single_check edge cases, and monitoring with zero/negative PIDs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from checkloop import check_runner, git, monitoring, process, streaming, terminal
from checkloop.checkpoint import _format_checkpoint_summary
from checkloop.checks import CheckDef
from checkloop.process import CheckResult
from tests.helpers import make_checkpoint_data, make_suite_args, make_summary_row


# =============================================================================
# git._git_run — exception chaining
# =============================================================================


class TestGitRunExceptionChaining:
    """Verify that TimeoutExpired is chained as the cause of the raised OSError."""

    def test_timeout_exception_is_chained(self) -> None:
        """The OSError raised on timeout should have __cause__ set to TimeoutExpired."""
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git status", timeout=120),
        ):
            with pytest.raises(OSError) as exc_info:
                git._git_run("/tmp", "status")
            assert isinstance(exc_info.value.__cause__, subprocess.TimeoutExpired)

    def test_timeout_with_no_args(self) -> None:
        """_git_run with no args should still produce a readable error on timeout."""
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120),
        ):
            with pytest.raises(OSError, match="timed out"):
                git._git_run("/tmp")


# =============================================================================
# terminal._parse_duration — edge cases
# =============================================================================


class TestParseDurationEdgeCases:
    """Edge cases for _parse_duration beyond what's already tested."""

    def test_seconds_only_format_returns_zero(self) -> None:
        """A string like '30s' (no minutes) doesn't match the regex and returns 0.0."""
        assert terminal._parse_duration("30s") == 0.0

    def test_empty_string_returns_zero(self) -> None:
        assert terminal._parse_duration("") == 0.0

    def test_whitespace_only_returns_zero(self) -> None:
        assert terminal._parse_duration("   ") == 0.0

    def test_negative_components_not_matched(self) -> None:
        """Regex \\d+ doesn't match negative numbers."""
        assert terminal._parse_duration("-1m-30s") == 0.0

    def test_zero_hours_zero_minutes_zero_seconds(self) -> None:
        assert terminal._parse_duration("0h00m00s") == 0.0

    def test_large_hours(self) -> None:
        assert terminal._parse_duration("999h59m59s") == 999 * 3600 + 59 * 60 + 59

    def test_format_parse_roundtrip_all_ranges(self) -> None:
        """Verify format_duration → _parse_duration round-trips for various values."""
        test_values = [0, 1, 59, 60, 61, 3599, 3600, 3661, 86400]
        for secs in test_values:
            formatted = terminal.format_duration(secs)
            parsed = terminal._parse_duration(formatted)
            assert parsed == secs, f"Round-trip failed for {secs}: '{formatted}' → {parsed}"


# =============================================================================
# checkpoint._format_checkpoint_summary — edge cases
# =============================================================================


class TestFormatCheckpointSummaryEdgeCasesNew:
    """Additional edge cases for _format_checkpoint_summary."""

    def test_single_active_check_at_zero(self) -> None:
        """Single-element active list, index 0 — next check is the only one."""
        data = make_checkpoint_data(
            current_check_index=0,
            active_check_ids=["only-check"],
        )
        summary = _format_checkpoint_summary(data)
        assert "only-check" in summary
        assert "check 0/1 completed" in summary

    def test_all_completed_single_check(self) -> None:
        """When only one check exists and it's completed."""
        data = make_checkpoint_data(
            current_check_index=1,
            active_check_ids=["only-check"],
        )
        summary = _format_checkpoint_summary(data)
        assert "done" in summary


# =============================================================================
# git.compute_change_stats — boundary conditions
# =============================================================================


class TestComputeChangeStatsBoundary:
    """Boundary conditions for compute_change_stats."""

    def test_single_line_changed_in_large_repo(self) -> None:
        """One line changed in a 100k-line repo should give a very small percentage."""
        with mock.patch.object(git, "_count_lines_changed", return_value=1):
            with mock.patch.object(git, "_cached_total_tracked_lines", return_value=100000):
                lines, pct = git.compute_change_stats("/tmp", "abc123")
        assert lines == 1
        assert abs(pct - 0.001) < 0.0001

    def test_more_lines_changed_than_total(self) -> None:
        """Lines changed can exceed total (e.g., rewrite entire file plus add new lines)."""
        with mock.patch.object(git, "_count_lines_changed", return_value=2000):
            with mock.patch.object(git, "_cached_total_tracked_lines", return_value=1000):
                lines, pct = git.compute_change_stats("/tmp", "abc123")
        assert lines == 2000
        assert pct == 200.0


# =============================================================================
# git._count_lines_changed — edge cases
# =============================================================================


class TestCountLinesChangedEdgeCases:
    """Additional edge cases for _count_lines_changed."""

    def test_target_default_is_head(self) -> None:
        """Default target is 'HEAD' — verify it's appended to the diff command."""
        with mock.patch.object(git, "_git_run", return_value=mock.MagicMock(
            returncode=0, stdout=" 1 file changed, 5 insertions(+)",
        )) as mock_run:
            result = git._count_lines_changed("/tmp", "abc123")
        args = mock_run.call_args[0]
        assert "HEAD" in args

    def test_empty_target_omits_head(self) -> None:
        """Empty string target means diff against working tree — no 'HEAD' in args."""
        with mock.patch.object(git, "_git_run", return_value=mock.MagicMock(
            returncode=0, stdout=" 1 file changed, 3 insertions(+)",
        )) as mock_run:
            git._count_lines_changed("/tmp", "abc123", target="")
        args = mock_run.call_args[0]
        assert "HEAD" not in args[2:]


# =============================================================================
# check_runner — edge cases
# =============================================================================


class TestRunSingleCheckEdgeCases:
    """Edge cases for _run_single_check."""

    def test_idle_timeout_kill_does_not_trigger_memory_fix(self) -> None:
        """KILL_REASON_IDLE should NOT trigger _run_memory_fix."""
        check_def = CheckDef(id="test", label="Test", prompt="do test")
        args = make_suite_args(dry_run=False)
        idle_result = CheckResult(exit_code=-9, kill_reason=process.KILL_REASON_IDLE)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=idle_result), \
             mock.patch.object(check_runner, "_run_memory_fix") as mock_fix:
            check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_not_called()

    def test_timeout_kill_does_not_trigger_memory_fix(self) -> None:
        """KILL_REASON_TIMEOUT should NOT trigger _run_memory_fix."""
        check_def = CheckDef(id="test", label="Test", prompt="do test")
        args = make_suite_args(dry_run=False)
        timeout_result = CheckResult(exit_code=-9, kill_reason=process.KILL_REASON_TIMEOUT)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=timeout_result), \
             mock.patch.object(check_runner, "_run_memory_fix") as mock_fix:
            check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_not_called()


# =============================================================================
# monitoring — edge cases
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
# monitoring — timeout handling
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
# process — edge cases
# =============================================================================


class TestStreamProcessOutputEdgeCases:
    """Edge cases for _stream_process_output."""

    def test_select_valueerror_breaks_loop(self) -> None:
        """ValueError from select() (e.g., closed fd) breaks the streaming loop."""
        mock_proc = mock.MagicMock()
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 5
        mock_stdout.read1 = None
        mock_proc.stdout = mock_stdout
        mock_proc.pid = 9999

        with mock.patch("select.select", side_effect=ValueError("closed fd")):
            _, kill_reason = process._stream_process_output(
                mock_proc, idle_timeout=120, debug=False,
            )
        assert kill_reason is None


# =============================================================================
# streaming — edge cases
# =============================================================================


class TestProcessJsonlBufferMaxBufferDisabled:
    """Test process_jsonl_buffer when max_buffer_size is 0 (disabled)."""

    def test_zero_max_buffer_size_does_not_truncate(self) -> None:
        """When max_buffer_size=0, buffer is never truncated regardless of size."""
        big_buf = bytearray(b"x" * 100_000)
        result = streaming.process_jsonl_buffer(
            big_buf, 0.0, False, max_buffer_size=0,
        )
        assert len(result) == 100_000


# =============================================================================
# terminal — compute_summary_stats edge cases
# =============================================================================


class TestComputeSummaryStatsEdgeCasesNew:
    """Additional edge cases for compute_summary_stats."""

    def test_killed_with_exit_code_zero(self) -> None:
        """A check killed (has kill_reason) but with exit_code=0 counts as succeeded AND killed."""
        row = make_summary_row(exit_code=0, kill_reason="memory", made_changes=True, lines_changed=50)
        stats = terminal.compute_summary_stats([row])
        assert stats.succeeded == 1
        assert stats.killed == 1
        assert stats.failed == 0

    def test_all_checks_failed(self) -> None:
        rows = [
            make_summary_row(exit_code=1),
            make_summary_row(exit_code=2),
            make_summary_row(exit_code=127),
        ]
        stats = terminal.compute_summary_stats(rows)
        assert stats.succeeded == 0
        assert stats.failed == 3

    def test_negative_exit_code(self) -> None:
        """Negative exit code (signal kill) is treated as failure."""
        row = make_summary_row(exit_code=-9)
        stats = terminal.compute_summary_stats([row])
        assert stats.succeeded == 0
        assert stats.failed == 1


# =============================================================================
# git — build_changed_files_prefix edge cases
# =============================================================================


class TestBuildChangedFilesPrefixNewEdgeCases:
    """Additional edge cases for build_changed_files_prefix."""

    def test_files_with_newlines_in_names(self) -> None:
        """File names containing newlines should not break the prefix format."""
        result = git.build_changed_files_prefix(["file\nname.py"])
        assert "1 file(s)" in result

    def test_many_files(self) -> None:
        """A large number of changed files should all appear in the prefix."""
        files = [f"src/file_{i}.py" for i in range(100)]
        result = git.build_changed_files_prefix(files)
        assert "100 file(s)" in result
        assert "file_99.py" in result


# =============================================================================
# git — detect_default_branch edge cases
# =============================================================================


class TestDetectDefaultBranchEdgeCases:
    """Additional edge cases for detect_default_branch."""

    def test_main_check_returns_nonzero_then_master_oserror(self) -> None:
        """When main returns non-zero and master raises OSError, falls back to 'main'."""
        with mock.patch.object(git, "_git_run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=128),  # main doesn't exist
                OSError("git broke"),  # master check fails
            ]
            assert git.detect_default_branch("/tmp") == "main"


# =============================================================================
# git._count_tracked_lines — edge cases
# =============================================================================


class TestCountTrackedLinesEdgeCasesNew:
    """Additional edge cases for _count_tracked_lines."""

    def test_git_ls_files_nonzero_returncode_with_bytes_stderr(self) -> None:
        """When ls-files fails with bytes stderr, it should be decoded safely."""
        ls_result = mock.MagicMock(
            returncode=1,
            stderr=b"fatal: not a git repository\n",
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            result = git._count_tracked_lines("/tmp")
        assert result == 1

    def test_git_ls_files_with_none_stderr(self) -> None:
        """When stderr is None, it should be handled gracefully."""
        ls_result = mock.MagicMock(
            returncode=1,
            stderr=None,
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            result = git._count_tracked_lines("/tmp")
        assert result == 1


# =============================================================================
# process — _report_check_exit_status edge case
# =============================================================================


class TestReportCheckExitStatusEdgeCases:
    """Edge cases for _report_check_exit_status."""

    def test_returncode_none_returns_negative_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = None
        exit_code = process._report_check_exit_status(mock_proc, 0.0)
        assert exit_code == -1

    def test_returncode_zero_reports_completed(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        exit_code = process._report_check_exit_status(mock_proc, 0.0)
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "completed" in out

    def test_negative_returncode_reports_exit_code(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = -9
        exit_code = process._report_check_exit_status(mock_proc, 0.0)
        assert exit_code == -9
        out = capsys.readouterr().out
        assert "exited with code -9" in out


# =============================================================================
# check_runner._build_check_prompt — scope prefix edge cases
# =============================================================================


class TestBuildCheckPromptEdgeCases:
    """Edge cases for _build_check_prompt."""

    def test_empty_changed_files_prefix_uses_full_scope(self) -> None:
        """When changed_files_prefix is empty string, full codebase scope is used."""
        from checkloop.checks import FULL_CODEBASE_SCOPE
        check = CheckDef(id="test", label="Test", prompt="review")
        args = make_suite_args(changed_files_prefix="")
        prompt = check_runner._build_check_prompt(check, args)
        assert prompt.startswith(FULL_CODEBASE_SCOPE)

    def test_changed_files_prefix_prepended(self) -> None:
        """When changed_files_prefix is set, it replaces the full scope prefix."""
        check = CheckDef(id="test", label="Test", prompt="review")
        args = make_suite_args(changed_files_prefix="ONLY THESE: a.py\n\n")
        prompt = check_runner._build_check_prompt(check, args)
        assert prompt.startswith("ONLY THESE: a.py")
        assert "review" in prompt

    def test_missing_changed_files_prefix_attr(self) -> None:
        """When args doesn't have changed_files_prefix, getattr defaults to ''."""
        from checkloop.checks import FULL_CODEBASE_SCOPE
        check = CheckDef(id="test", label="Test", prompt="review")
        args = make_suite_args()
        if hasattr(args, "changed_files_prefix"):
            delattr(args, "changed_files_prefix")
        prompt = check_runner._build_check_prompt(check, args)
        assert prompt.startswith(FULL_CODEBASE_SCOPE)


# =============================================================================
# git._git_run — timeout message with no args
# =============================================================================


class TestGitRunTimeoutEdgeCases:
    """Edge cases for _git_run timeout error messages."""

    def test_timeout_error_message_includes_command(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git status", timeout=120),
        ):
            with pytest.raises(OSError, match="status timed out"):
                git._git_run("/tmp", "status")

    def test_timeout_error_message_empty_args(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120),
        ):
            with pytest.raises(OSError, match="timed out"):
                git._git_run("/tmp")
