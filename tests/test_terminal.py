"""Tests for checkloop.terminal — ANSI output, formatting, and error exit."""

from __future__ import annotations

import pytest

from checkloop import terminal


class TestPrintBanner:
    """Tests for the _print_banner() terminal output helper."""

    def test_default_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal._print_banner("Hello")
        out = capsys.readouterr().out
        assert "Hello" in out
        assert terminal.CYAN in out
        assert terminal.BOLD in out
        assert terminal.RESET in out

    def test_custom_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal._print_banner("Title", terminal.GREEN)
        out = capsys.readouterr().out
        assert terminal.GREEN in out
        assert "Title" in out


class TestPrintStatus:
    """Tests for the _print_status() terminal output helper."""

    def test_default_dim(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal._print_status("info")
        out = capsys.readouterr().out
        assert "info" in out
        assert terminal.DIM in out

    def test_custom_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal._print_status("warn", terminal.YELLOW)
        out = capsys.readouterr().out
        assert terminal.YELLOW in out


class TestFormatDuration:
    """Tests for _format_duration() time formatting."""

    def test_zero(self) -> None:
        assert terminal._format_duration(0) == "0m00s"

    def test_seconds_only(self) -> None:
        assert terminal._format_duration(45) == "0m45s"

    def test_minutes_and_seconds(self) -> None:
        assert terminal._format_duration(125) == "2m05s"

    def test_exactly_one_hour(self) -> None:
        assert terminal._format_duration(3600) == "1h00m00s"

    def test_hours_minutes_seconds(self) -> None:
        assert terminal._format_duration(3661) == "1h01m01s"

    def test_large_value(self) -> None:
        assert terminal._format_duration(7384) == "2h03m04s"

    def test_very_large_value(self) -> None:
        assert terminal._format_duration(360000) == "100h00m00s"

    def test_just_under_one_hour(self) -> None:
        assert terminal._format_duration(3599) == "59m59s"

    def test_negative_clamped_to_zero(self) -> None:
        assert terminal._format_duration(-5) == "0m00s"

    def test_negative_large_clamped_to_zero(self) -> None:
        assert terminal._format_duration(-9999) == "0m00s"

    def test_fractional_seconds(self) -> None:
        assert terminal._format_duration(0.9) == "0m00s"

    def test_fractional_just_under_minute(self) -> None:
        assert terminal._format_duration(59.999) == "0m59s"


class TestFormatDurationEdgeCases:
    """Edge case tests for _format_duration() with unusual float inputs."""

    def test_nan_returns_zero(self) -> None:
        assert terminal._format_duration(float("nan")) == "0m00s"

    def test_positive_inf_returns_zero(self) -> None:
        assert terminal._format_duration(float("inf")) == "0m00s"

    def test_negative_inf_returns_zero(self) -> None:
        assert terminal._format_duration(float("-inf")) == "0m00s"

    def test_very_small_positive_float(self) -> None:
        assert terminal._format_duration(0.001) == "0m00s"

    def test_very_large_float(self) -> None:
        result = terminal._format_duration(1_000_000.0)
        assert result == "277h46m40s"


class TestFormatDurationBoundary:
    """Boundary value tests for _format_duration()."""

    def test_exactly_60_seconds(self) -> None:
        assert terminal._format_duration(60) == "1m00s"

    def test_exactly_3599(self) -> None:
        assert terminal._format_duration(3599) == "59m59s"

    def test_exactly_3600(self) -> None:
        assert terminal._format_duration(3600) == "1h00m00s"

    def test_exactly_3601(self) -> None:
        assert terminal._format_duration(3601) == "1h00m01s"

    def test_max_int(self) -> None:
        result = terminal._format_duration(2**31)
        assert "h" in result


class TestFatal:
    """Tests for the _fatal() error-and-exit helper."""

    def test_prints_and_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            terminal._fatal("something went wrong")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "something went wrong" in out
