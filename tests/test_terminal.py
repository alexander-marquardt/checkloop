"""Tests for checkloop.terminal — ANSI output, formatting, and error exit."""

from __future__ import annotations

import pytest

from checkloop import terminal
from tests.helpers import make_summary_row


class TestPrintBanner:
    """Tests for the print_banner() terminal output helper."""

    def test_default_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.print_banner("Hello")
        out = capsys.readouterr().out
        assert "Hello" in out
        assert terminal.CYAN in out
        assert terminal.BOLD in out
        assert terminal.RESET in out

    def test_custom_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.print_banner("Title", terminal.GREEN)
        out = capsys.readouterr().out
        assert terminal.GREEN in out
        assert "Title" in out


class TestPrintStatus:
    """Tests for the print_status() terminal output helper."""

    def test_default_dim(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.print_status("info")
        out = capsys.readouterr().out
        assert "info" in out
        assert terminal.DIM in out

    def test_custom_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.print_status("warn", terminal.YELLOW)
        out = capsys.readouterr().out
        assert terminal.YELLOW in out


class TestFormatDuration:
    """Tests for format_duration() time formatting."""

    def test_zero(self) -> None:
        assert terminal.format_duration(0) == "0m00s"

    def test_seconds_only(self) -> None:
        assert terminal.format_duration(45) == "0m45s"

    def test_minutes_and_seconds(self) -> None:
        assert terminal.format_duration(125) == "2m05s"

    def test_exactly_one_hour(self) -> None:
        assert terminal.format_duration(3600) == "1h00m00s"

    def test_hours_minutes_seconds(self) -> None:
        assert terminal.format_duration(3661) == "1h01m01s"

    def test_large_value(self) -> None:
        assert terminal.format_duration(7384) == "2h03m04s"

    def test_very_large_value(self) -> None:
        assert terminal.format_duration(360000) == "100h00m00s"

    def test_just_under_one_hour(self) -> None:
        assert terminal.format_duration(3599) == "59m59s"

    def test_negative_clamped_to_zero(self) -> None:
        assert terminal.format_duration(-5) == "0m00s"

    def test_negative_large_clamped_to_zero(self) -> None:
        assert terminal.format_duration(-9999) == "0m00s"

    def test_fractional_seconds(self) -> None:
        assert terminal.format_duration(0.9) == "0m00s"

    def test_fractional_just_under_minute(self) -> None:
        assert terminal.format_duration(59.999) == "0m59s"


class TestFormatDurationEdgeCases:
    """Edge case tests for format_duration() with unusual float inputs."""

    def test_nan_returns_zero(self) -> None:
        assert terminal.format_duration(float("nan")) == "0m00s"

    def test_positive_inf_returns_zero(self) -> None:
        assert terminal.format_duration(float("inf")) == "0m00s"

    def test_negative_inf_returns_zero(self) -> None:
        assert terminal.format_duration(float("-inf")) == "0m00s"

    def test_very_small_positive_float(self) -> None:
        assert terminal.format_duration(0.001) == "0m00s"

    def test_very_large_float(self) -> None:
        result = terminal.format_duration(1_000_000.0)
        assert result == "277h46m40s"


class TestFormatDurationBoundary:
    """Boundary value tests for format_duration()."""

    def test_exactly_60_seconds(self) -> None:
        assert terminal.format_duration(60) == "1m00s"

    def test_exactly_3599(self) -> None:
        assert terminal.format_duration(3599) == "59m59s"

    def test_exactly_3600(self) -> None:
        assert terminal.format_duration(3600) == "1h00m00s"

    def test_exactly_3601(self) -> None:
        assert terminal.format_duration(3601) == "1h00m01s"

    def test_max_int(self) -> None:
        result = terminal.format_duration(2**31)
        assert "h" in result


class TestPrintRunSummaryTableEdgeCases:
    """Edge cases for print_run_summary_table()."""

    def test_empty_results_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Should print nothing when given an empty list."""
        terminal.print_run_summary_table([], "0m00s")
        assert capsys.readouterr().out == ""

    def test_row_with_all_none_optional_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Rows where lines_changed and change_pct are None should display dashes."""
        row = terminal.SummaryRow(
            check_id="test-check",
            label="Test",
            cycle=1,
            exit_code=0,
            kill_reason=None,
            made_changes=False,
            lines_changed=None,
            change_pct=None,
            duration="0m01s",
        )
        terminal.print_run_summary_table([row], "0m01s")
        output = capsys.readouterr().out
        assert "test-check" in output
        assert "—" in output  # dash for None values

    def test_row_with_zero_lines_changed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Zero lines changed should display '0', not a dash."""
        row = terminal.SummaryRow(
            check_id="noop",
            label="No-op",
            cycle=1,
            exit_code=0,
            kill_reason=None,
            made_changes=False,
            lines_changed=0,
            change_pct=0.0,
            duration="0m00s",
        )
        terminal.print_run_summary_table([row], "0m00s")
        output = capsys.readouterr().out
        assert "noop" in output

    def test_long_check_id_truncated(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Check IDs longer than 20 chars should be truncated."""
        row = terminal.SummaryRow(
            check_id="a" * 30,
            label="Long",
            cycle=1,
            exit_code=0,
            kill_reason=None,
            made_changes=False,
            lines_changed=0,
            change_pct=0.0,
            duration="0m00s",
        )
        terminal.print_run_summary_table([row], "0m00s")
        output = capsys.readouterr().out
        assert "a" * 20 in output


class TestPrintRunSummaryTableColours:
    """Tests for print_run_summary_table row colour selection."""

    def test_killed_row_uses_red(self, capsys: pytest.CaptureFixture[str]) -> None:
        results = [make_summary_row(kill_reason="timeout")]
        terminal.print_run_summary_table(results, "0m10s")
        out = capsys.readouterr().out
        assert terminal.RED in out

    def test_made_changes_row_uses_green(self, capsys: pytest.CaptureFixture[str]) -> None:
        results = [make_summary_row(made_changes=True, lines_changed=10)]
        terminal.print_run_summary_table(results, "0m10s")
        out = capsys.readouterr().out
        assert terminal.GREEN in out

    def test_nonzero_exit_row_uses_yellow(self, capsys: pytest.CaptureFixture[str]) -> None:
        results = [make_summary_row(exit_code=1)]
        terminal.print_run_summary_table(results, "0m10s")
        out = capsys.readouterr().out
        assert terminal.YELLOW in out


class TestParseDuration:
    """Tests for _parse_duration() round-tripping from format_duration output."""

    def test_minutes_and_seconds(self) -> None:
        assert terminal._parse_duration("2m30s") == 150.0

    def test_zero(self) -> None:
        assert terminal._parse_duration("0m00s") == 0.0

    def test_hours(self) -> None:
        assert terminal._parse_duration("1h02m30s") == 3750.0

    def test_invalid_string(self) -> None:
        assert terminal._parse_duration("garbage") == 0.0


class TestComputeCycleSummaries:
    """Tests for compute_cycle_summaries() per-cycle aggregation."""

    def test_single_cycle(self) -> None:
        rows = [make_summary_row(cycle=1, made_changes=True, lines_changed=10, duration="1m00s"),
                make_summary_row(cycle=1, made_changes=True, lines_changed=20, duration="1m00s")]
        summaries = terminal.compute_cycle_summaries(rows)
        assert len(summaries) == 1
        assert summaries[0].cycle == 1
        assert summaries[0].total_lines == 30
        assert summaries[0].total_checks == 2

    def test_multiple_cycles(self) -> None:
        rows = [make_summary_row(cycle=1, made_changes=True, lines_changed=50, duration="1m00s"),
                make_summary_row(cycle=2, made_changes=True, lines_changed=20, duration="1m00s")]
        summaries = terminal.compute_cycle_summaries(rows)
        assert len(summaries) == 2
        assert summaries[0].total_lines == 50
        assert summaries[1].total_lines == 20

    def test_empty_input(self) -> None:
        assert terminal.compute_cycle_summaries([]) == []

    def test_failed_checks_counted(self) -> None:
        rows = [make_summary_row(cycle=1, exit_code=1, made_changes=True, lines_changed=10, duration="1m00s"),
                make_summary_row(cycle=1, exit_code=0, made_changes=True, lines_changed=10, duration="1m00s")]
        summaries = terminal.compute_cycle_summaries(rows)
        assert summaries[0].failed == 1
        assert summaries[0].succeeded == 1


class TestPrintOverallSummaryTable:
    """Tests for print_overall_summary_table() cross-cycle overview."""

    def test_empty_results(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.print_overall_summary_table([], "0m00s")
        assert capsys.readouterr().out == ""

    def test_shows_blue_banner(self, capsys: pytest.CaptureFixture[str]) -> None:
        rows = [make_summary_row(cycle=1, made_changes=True, lines_changed=10, duration="1m00s"),
                make_summary_row(cycle=2, made_changes=True, lines_changed=5, duration="1m00s")]
        terminal.print_overall_summary_table(rows, "2m00s")
        out = capsys.readouterr().out
        assert "Overall Summary" in out
        assert terminal.BLUE in out

    def test_shows_delta_for_decreasing_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        rows = [make_summary_row(cycle=1, made_changes=True, lines_changed=100, duration="1m00s"),
                make_summary_row(cycle=2, made_changes=True, lines_changed=30, duration="1m00s")]
        terminal.print_overall_summary_table(rows, "2m00s")
        out = capsys.readouterr().out
        assert "-70" in out
        assert terminal.GREEN in out  # decreasing = converging

    def test_shows_delta_for_increasing_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        rows = [make_summary_row(cycle=1, made_changes=True, lines_changed=10, duration="1m00s"),
                make_summary_row(cycle=2, made_changes=True, lines_changed=50, duration="1m00s")]
        terminal.print_overall_summary_table(rows, "2m00s")
        out = capsys.readouterr().out
        assert "+40" in out
        assert terminal.YELLOW in out  # increasing = diverging

    def test_footer_shows_totals(self, capsys: pytest.CaptureFixture[str]) -> None:
        rows = [make_summary_row(cycle=1, made_changes=True, lines_changed=10, duration="1m00s"),
                make_summary_row(cycle=2, made_changes=True, lines_changed=5, duration="1m00s")]
        terminal.print_overall_summary_table(rows, "3m00s")
        out = capsys.readouterr().out
        assert "Total cycles : 2" in out
        assert "Total lines  : 15" in out
        assert "3m00s" in out


class TestBannerTitleAndColourParams:
    """Tests for the banner_title and banner_colour parameters on print_run_summary_table."""

    def test_custom_banner_title(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.print_run_summary_table(
            [make_summary_row(duration="0m01s")], "0m01s",
            banner_title="Cycle 2/3 Summary", banner_colour=terminal.CYAN,
        )
        out = capsys.readouterr().out
        assert "Cycle 2/3 Summary" in out

    def test_custom_banner_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.print_run_summary_table(
            [make_summary_row(duration="0m01s")], "0m01s",
            banner_title="Test", banner_colour=terminal.YELLOW,
        )
        out = capsys.readouterr().out
        assert terminal.YELLOW in out


class TestFatal:
    """Tests for the fatal() error-and-exit helper."""

    def test_prints_and_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            terminal.fatal("something went wrong")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "something went wrong" in out


class TestComputeSummaryStatsEdgeCases:
    """Edge cases for compute_summary_stats()."""

    def test_empty_results_returns_all_zeros(self) -> None:
        stats = terminal.compute_summary_stats([])
        assert stats.succeeded == 0
        assert stats.failed == 0
        assert stats.killed == 0
        assert stats.total_lines == 0
        assert stats.with_changes == 0

    def test_all_none_lines_changed(self) -> None:
        """lines_changed=None should be treated as 0 in sum."""
        row = terminal.SummaryRow(
            check_id="a", label="A", cycle=1, exit_code=0,
            kill_reason=None, made_changes=True,
            lines_changed=None, change_pct=None, duration="0m01s",
        )
        stats = terminal.compute_summary_stats([row])
        assert stats.total_lines == 0
        assert stats.with_changes == 1

    def test_mix_of_none_and_int_lines_changed(self) -> None:
        rows = [
            terminal.SummaryRow(
                check_id="a", label="A", cycle=1, exit_code=0,
                kill_reason=None, made_changes=True,
                lines_changed=None, change_pct=None, duration="0m01s",
            ),
            terminal.SummaryRow(
                check_id="b", label="B", cycle=1, exit_code=0,
                kill_reason=None, made_changes=True,
                lines_changed=42, change_pct=1.0, duration="0m02s",
            ),
        ]
        stats = terminal.compute_summary_stats(rows)
        assert stats.total_lines == 42


class TestFormatDurationAdditional:
    """Additional edge cases for format_duration()."""

    def test_negative_zero(self) -> None:
        assert terminal.format_duration(-0.0) == "0m00s"

    def test_one_second(self) -> None:
        assert terminal.format_duration(1) == "0m01s"

    def test_59_seconds(self) -> None:
        assert terminal.format_duration(59) == "0m59s"

    def test_61_seconds(self) -> None:
        assert terminal.format_duration(61) == "1m01s"


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


class TestComputeSummaryStatsEdgeCasesNew:
    """Additional edge cases for compute_summary_stats."""

    def test_killed_with_exit_code_zero(self) -> None:
        """A check killed (has kill_reason) but with exit_code=0 counts as succeeded AND killed."""
        row = make_summary_row(exit_code=0, kill_reason="memory_limit", made_changes=True, lines_changed=50)
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
