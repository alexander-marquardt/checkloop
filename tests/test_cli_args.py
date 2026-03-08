"""Tests for checkloop.cli — argument parsing, validation, and resolution."""

from __future__ import annotations

import argparse
import logging
from unittest import mock

import pytest

from checkloop import cli, suite, process, checks
from helpers import make_mock_cli_args


# =============================================================================
# Shared test helpers
# =============================================================================

def _parse_cli(args: list[str]) -> argparse.Namespace:
    """Parse CLI args, auto-adding --dir /tmp if not provided."""
    if "--dir" not in args and "-d" not in args:
        args = ["--dir", "/tmp"] + args
    return cli._build_argument_parser().parse_args(args)


# =============================================================================
# _build_argument_parser
# =============================================================================

class TestBuildArgumentParser:
    """Tests for _build_argument_parser() CLI flag parsing."""

    def test_defaults(self) -> None:
        ns = _parse_cli([])
        assert ns.dir == "/tmp"
        assert ns.checks is None
        assert ns.level is None
        assert ns.all_checks is False
        assert ns.cycles == 1
        assert ns.idle_timeout == process.DEFAULT_IDLE_TIMEOUT
        assert ns.dry_run is False
        assert ns.verbose is False
        assert ns.pause == process.DEFAULT_PAUSE_SECONDS

    def test_dir_is_required(self) -> None:
        with pytest.raises(SystemExit):
            cli._build_argument_parser().parse_args([])

    def test_dir(self) -> None:
        ns = _parse_cli(["--dir", "/foo"])
        assert ns.dir == "/foo"

    def test_dir_short(self) -> None:
        ns = _parse_cli(["-d", "/bar"])
        assert ns.dir == "/bar"

    def test_checks_flag(self) -> None:
        ns = _parse_cli(["--checks", "readability", "security"])
        assert ns.checks == ["readability", "security"]

    def test_all_checks(self) -> None:
        ns = _parse_cli(["--all-checks"])
        assert ns.all_checks is True

    def test_cycles(self) -> None:
        ns = _parse_cli(["--cycles", "5"])
        assert ns.cycles == 5

    def test_cycles_short(self) -> None:
        ns = _parse_cli(["-c", "3"])
        assert ns.cycles == 3

    def test_idle_timeout(self) -> None:
        ns = _parse_cli(["--idle-timeout", "300"])
        assert ns.idle_timeout == 300

    def test_dry_run(self) -> None:
        ns = _parse_cli(["--dry-run"])
        assert ns.dry_run is True

    def test_verbose(self) -> None:
        ns = _parse_cli(["--verbose"])
        assert ns.verbose is True

    def test_verbose_short(self) -> None:
        ns = _parse_cli(["-v"])
        assert ns.verbose is True

    def test_pause(self) -> None:
        ns = _parse_cli(["--pause", "10"])
        assert ns.pause == 10

    def test_invalid_check_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_cli(["--checks", "nonexistent"])


# =============================================================================
# _print_run_summary
# =============================================================================

class TestPrintRunSummary:
    """Tests for _print_run_summary() pre-run output."""

    def test_normal_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "readability", "label": "Readability"}]
        cli._print_run_summary("/tmp", selected_checks, 2, 2, 120, False)
        out = capsys.readouterr().out
        assert "checkloop" in out
        assert "/tmp" in out
        assert "readability" in out
        assert "2" in out

    def test_dry_run_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "tests", "label": "Tests"}]
        cli._print_run_summary("/tmp", selected_checks, 1, 1, 120, True)
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_single_cycle_no_plural(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", selected_checks, 1, 1, 60, False)
        out = capsys.readouterr().out
        assert "1 cycle)" in out

    def test_multiple_cycles_plural(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", selected_checks, 3, 3, 60, False)
        out = capsys.readouterr().out
        assert "cycles)" in out

    def test_empty_checks_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_run_summary("/dir", [], 1, 0, 60, False)
        out = capsys.readouterr().out
        assert "Total steps  : 0" in out

    def test_convergence_threshold_displayed(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", selected_checks, 1, 1, 60, False, convergence_threshold=0.5)
        out = capsys.readouterr().out
        assert "0.5%" in out
        assert "Convergence" in out

    def test_zero_convergence_not_displayed(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", selected_checks, 1, 1, 60, False, convergence_threshold=0.0)
        out = capsys.readouterr().out
        assert "Convergence" not in out


# =============================================================================
# _validate_arguments
# =============================================================================

class TestValidateArguments:
    """Tests for _validate_arguments()."""

    def test_cycles_zero_exits(self) -> None:
        args = argparse.Namespace(idle_timeout=120, pause=0, cycles=0, converged_at_percentage=0.1)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_negative_cycles_exits(self) -> None:
        args = argparse.Namespace(idle_timeout=120, pause=0, cycles=-5, converged_at_percentage=0.1)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_negative_convergence_exits(self) -> None:
        args = argparse.Namespace(idle_timeout=120, pause=0, cycles=1, converged_at_percentage=-0.5)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_valid_arguments_no_exit(self) -> None:
        args = argparse.Namespace(idle_timeout=1, pause=0, cycles=1, converged_at_percentage=0.0)
        cli._validate_arguments(args)  # should not raise


class TestValidateArgumentsEdgeCases:
    """Edge case tests for _validate_arguments()."""

    def test_converged_at_percentage_over_100_exits(self) -> None:
        args = make_mock_cli_args(converged_at_percentage=101.0)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_converged_at_zero_is_valid(self) -> None:
        args = make_mock_cli_args(converged_at_percentage=0.0)
        cli._validate_arguments(args)

    def test_converged_at_100_is_valid(self) -> None:
        args = make_mock_cli_args(converged_at_percentage=100.0)
        cli._validate_arguments(args)

    def test_idle_timeout_exactly_one(self) -> None:
        args = make_mock_cli_args(idle_timeout=1)
        cli._validate_arguments(args)

    def test_pause_zero_is_valid(self) -> None:
        args = make_mock_cli_args(pause=0)
        cli._validate_arguments(args)

    def test_cycles_exactly_one(self) -> None:
        args = make_mock_cli_args(cycles=1)
        cli._validate_arguments(args)


# =============================================================================
# _resolve_selected_checks
# =============================================================================

class TestResolveSelectedChecks:
    """Tests for _resolve_selected_checks()."""

    def test_level_basic(self) -> None:
        args = argparse.Namespace(all_checks=False, checks=None, level="basic")
        result = cli._resolve_selected_checks(args)
        ids = [p["id"] for p in result]
        assert ids == checks.TIER_BASIC

    def test_all_checks(self) -> None:
        args = argparse.Namespace(all_checks=True, checks=None, level=None)
        result = cli._resolve_selected_checks(args)
        assert len(result) == len(checks.CHECKS)

    def test_checks_override_level(self) -> None:
        args = argparse.Namespace(all_checks=False, checks=["security"], level="exhaustive")
        result = cli._resolve_selected_checks(args)
        assert len(result) == 1
        assert result[0]["id"] == "security"

    def test_all_checks_flag(self) -> None:
        args = make_mock_cli_args(all_checks=True, checks=None, level=None)
        result = cli._resolve_selected_checks(args)
        assert len(result) == len(checks.CHECKS)

    def test_explicit_checks_override_level(self) -> None:
        args = make_mock_cli_args(all_checks=False, checks=["security"], level="basic")
        result = cli._resolve_selected_checks(args)
        assert len(result) == 1
        assert result[0]["id"] == "security"

    def test_default_tier_when_nothing_specified(self) -> None:
        args = make_mock_cli_args(all_checks=False, checks=None, level=None)
        result = cli._resolve_selected_checks(args)
        expected_ids = set(checks.TIERS[checks.DEFAULT_TIER])
        assert {p["id"] for p in result} == expected_ids


# =============================================================================
# _resolve_working_directory
# =============================================================================

class TestResolveWorkingDirectory:
    """Tests for _resolve_working_directory()."""

    def test_oserror_calls_fatal(self) -> None:
        from pathlib import Path
        with mock.patch.object(Path, "resolve", side_effect=OSError("bad path")):
            with pytest.raises(SystemExit):
                cli._resolve_working_directory("/nonexistent/\x00path")


# =============================================================================
# _resolve_changed_files_prefix
# =============================================================================

class TestResolveChangedFilesPrefix:
    """Tests for _resolve_changed_files_prefix()."""

    def test_changed_only_none_returns_empty(self) -> None:
        args = argparse.Namespace(changed_only=None)
        result = cli._resolve_changed_files_prefix(args, "/tmp")
        assert result == ""

    def test_not_git_repo_exits(self) -> None:
        args = argparse.Namespace(changed_only="auto")
        with mock.patch.object(cli, "_is_git_repo", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                cli._resolve_changed_files_prefix(args, "/tmp")
            assert exc_info.value.code == 1

    def test_no_changed_files_exits(self) -> None:
        args = argparse.Namespace(changed_only="main")
        with mock.patch.object(cli, "_is_git_repo", return_value=True), \
             mock.patch.object(cli, "_get_changed_files", return_value=[]):
            with pytest.raises(SystemExit) as exc_info:
                cli._resolve_changed_files_prefix(args, "/tmp")
            assert exc_info.value.code == 1

    def test_returns_prefix_with_changed_files(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = argparse.Namespace(changed_only="main")
        with mock.patch.object(cli, "_is_git_repo", return_value=True), \
             mock.patch.object(cli, "_get_changed_files", return_value=["a.py", "b.py"]):
            result = cli._resolve_changed_files_prefix(args, "/tmp")
        assert "a.py" in result
        assert "b.py" in result
        out = capsys.readouterr().out
        assert "2 changed file(s)" in out


# =============================================================================
# --converged-at-percentage and --changed-only CLI args
# =============================================================================

class TestConvergedAtPercentageArg:
    """Tests for --converged-at-percentage CLI argument."""

    def test_default(self) -> None:
        ns = _parse_cli(["--dir", "/tmp"])
        assert ns.converged_at_percentage == cli.DEFAULT_CONVERGENCE_THRESHOLD

    def test_custom_value(self) -> None:
        ns = _parse_cli(["--dir", "/tmp", "--converged-at-percentage", "0.5"])
        assert ns.converged_at_percentage == 0.5

    def test_zero_disables(self) -> None:
        ns = _parse_cli(["--dir", "/tmp", "--converged-at-percentage", "0"])
        assert ns.converged_at_percentage == 0.0


class TestChangedOnly:
    """Tests for --changed-only flag."""

    def test_changed_only_default_is_none(self) -> None:
        ns = _parse_cli(["--dir", "/tmp"])
        assert ns.changed_only is None

    def test_changed_only_no_arg_sets_auto(self) -> None:
        ns = _parse_cli(["--dir", "/tmp", "--changed-only"])
        assert ns.changed_only == "auto"

    def test_changed_only_with_ref(self) -> None:
        ns = _parse_cli(["--dir", "/tmp", "--changed-only", "develop"])
        assert ns.changed_only == "develop"


# =============================================================================
# _configure_logging
# =============================================================================

class TestConfigureLogging:
    """Tests for _configure_logging()."""

    def test_default_level_is_warning(self) -> None:
        args = argparse.Namespace(verbose=False, debug=False)
        with mock.patch("logging.basicConfig") as mock_log:
            cli._configure_logging(args)
            mock_log.assert_called_once()
            assert mock_log.call_args.kwargs["level"] == logging.WARNING

    def test_verbose_sets_info_level(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--verbose", "--dry-run", "--pause", "0"]):
            with mock.patch("logging.basicConfig") as mock_log:
                with mock.patch.object(suite, "_run_check_suite"):
                    cli.main()
                mock_log.assert_called_once()
                assert mock_log.call_args.kwargs["level"] == logging.INFO

    def test_debug_sets_debug_level(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--debug", "--dry-run", "--pause", "0"]):
            with mock.patch("logging.basicConfig") as mock_log:
                with mock.patch.object(suite, "_run_check_suite"):
                    cli.main()
                mock_log.assert_called_once()
                assert mock_log.call_args.kwargs["level"] == logging.DEBUG
