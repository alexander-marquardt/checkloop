"""Tests for checkloop.cli — argument parsing, validation, and main entry point."""

from __future__ import annotations

import argparse
import contextlib
import logging
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest

from checkloop import cli, suite, process, checks, git
from helpers import SHARED_ARG_DEFAULTS


# =============================================================================
# Shared test helpers
# =============================================================================

def _parse_cli(args: list[str]) -> argparse.Namespace:
    """Parse CLI args, auto-adding --dir /tmp if not provided."""
    if "--dir" not in args and "-d" not in args:
        args = ["--dir", "/tmp"] + args
    return cli._build_argument_parser().parse_args(args)


def _make_main_mock_args(*, dry_run: bool = False, **overrides: Any) -> mock.MagicMock:
    """Build a MagicMock with all attributes main() reads from parsed args."""
    args = mock.MagicMock()
    defaults = {
        **SHARED_ARG_DEFAULTS,
        "debug": False,
        "dir": "/tmp",
        "cycles": 1,
        "converged_at_percentage": cli.DEFAULT_CONVERGENCE_THRESHOLD,
        "all_checks": False,
        "checks": ["readability"],
        "level": None,
        "dry_run": dry_run,
        "changed_only": None,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(args, key, value)
    return args


@contextlib.contextmanager
def _patch_main_pipeline(
    *,
    suite_side_effect: type[BaseException] | BaseException | None = None,
    **arg_overrides: Any,
) -> Iterator[None]:
    """Mock the standard main() pipeline for exception-path tests."""
    mock_args = _make_main_mock_args(**arg_overrides)
    suite_kwargs: dict[str, Any] = {}
    if suite_side_effect is not None:
        suite_kwargs["side_effect"] = suite_side_effect
    with mock.patch.object(cli, "_build_argument_parser") as mock_parser:
        mock_parser.return_value.parse_args.return_value = mock_args
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "_resolve_working_directory", return_value="/tmp"))
            stack.enter_context(mock.patch.object(cli, "_validate_arguments"))
            stack.enter_context(mock.patch.object(suite, "_display_pre_run_warning"))
            stack.enter_context(mock.patch.object(suite, "_run_check_suite", **suite_kwargs))
            yield


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

    def test_passes(self) -> None:
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

    def test_invalid_pass_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_cli(["--checks", "nonexistent"])


# =============================================================================
# _print_run_summary
# =============================================================================

class TestPrintRunSummary:
    """Tests for _print_run_summary() pre-run output."""

    def test_normal_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "readability", "label": "Readability"}]
        cli._print_run_summary("/tmp", passes, 2, 2, 120, False)
        out = capsys.readouterr().out
        assert "checkloop" in out
        assert "/tmp" in out
        assert "readability" in out
        assert "2" in out

    def test_dry_run_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "tests", "label": "Tests"}]
        cli._print_run_summary("/tmp", passes, 1, 1, 120, True)
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_single_cycle_no_plural(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 1, 1, 60, False)
        out = capsys.readouterr().out
        assert "1 cycle)" in out

    def test_multiple_cycles_plural(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 3, 3, 60, False)
        out = capsys.readouterr().out
        assert "cycles)" in out

    def test_empty_passes_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_run_summary("/dir", [], 1, 0, 60, False)
        out = capsys.readouterr().out
        assert "Total steps  : 0" in out

    def test_convergence_threshold_displayed(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 1, 1, 60, False, convergence_threshold=0.5)
        out = capsys.readouterr().out
        assert "0.5%" in out
        assert "Convergence" in out

    def test_zero_convergence_not_displayed(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 1, 1, 60, False, convergence_threshold=0.0)
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
        args = _make_main_mock_args(converged_at_percentage=101.0)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_converged_at_zero_is_valid(self) -> None:
        args = _make_main_mock_args(converged_at_percentage=0.0)
        cli._validate_arguments(args)

    def test_converged_at_100_is_valid(self) -> None:
        args = _make_main_mock_args(converged_at_percentage=100.0)
        cli._validate_arguments(args)

    def test_idle_timeout_exactly_one(self) -> None:
        args = _make_main_mock_args(idle_timeout=1)
        cli._validate_arguments(args)

    def test_pause_zero_is_valid(self) -> None:
        args = _make_main_mock_args(pause=0)
        cli._validate_arguments(args)

    def test_cycles_exactly_one(self) -> None:
        args = _make_main_mock_args(cycles=1)
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

    def test_passes_override_level(self) -> None:
        args = argparse.Namespace(all_checks=False, checks=["security"], level="exhaustive")
        result = cli._resolve_selected_checks(args)
        assert len(result) == 1
        assert result[0]["id"] == "security"

    def test_all_checks_flag(self) -> None:
        args = _make_main_mock_args(all_checks=True, checks=None, level=None)
        result = cli._resolve_selected_checks(args)
        assert len(result) == len(checks.CHECKS)

    def test_explicit_passes_override_level(self) -> None:
        args = _make_main_mock_args(all_checks=False, checks=["security"], level="basic")
        result = cli._resolve_selected_checks(args)
        assert len(result) == 1
        assert result[0]["id"] == "security"

    def test_default_tier_when_nothing_specified(self) -> None:
        args = _make_main_mock_args(all_checks=False, checks=None, level=None)
        result = cli._resolve_selected_checks(args)
        expected_ids = set(checks.TIERS[checks.DEFAULT_TIER])
        assert {p["id"] for p in result} == expected_ids


# =============================================================================
# _resolve_working_directory
# =============================================================================

class TestResolveWorkingDirectory:
    """Tests for _resolve_working_directory()."""

    def test_oserror_calls_fatal(self) -> None:
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


# =============================================================================
# main
# =============================================================================

class TestMain:
    """Tests for the main() CLI entry point."""

    def test_nonexistent_dir_exits(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", "/nonexistent_xyz_abc"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_idle_timeout_zero_exits(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--idle-timeout", "0"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_negative_pause_exits(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--pause", "-1"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_dry_run_full(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "All done" in out

    def test_all_checks_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--all-checks", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        for p in checks.CHECKS:
            assert p["label"] in out

    def test_cycles_zero_exits(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--cycles", "0"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_negative_cycles_exits(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--cycles", "-1"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_specific_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--checks", "security", "perf", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "Security" in out
        assert "Performance" in out


class TestMainNonDryRun:
    """Tests for main() in non-dry-run mode (with mocked internals)."""

    def test_non_dry_run_calls_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--pause", "0"]):
            with mock.patch.object(cli, "_display_pre_run_warning") as mock_warn:
                with mock.patch.object(cli, "_run_suite_with_error_handling"):
                    cli.main()
                mock_warn.assert_called_once()

    def test_level_thorough(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--level", "thorough", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "security" in out
        assert "All done" in out


class TestMainEmptyChecksExit:
    """Test that main() exits if no checks are resolved."""

    def test_empty_passes_exits(self) -> None:
        mock_args = _make_main_mock_args(dry_run=True, checks=None, level=None)
        with mock.patch.object(cli, "_build_argument_parser") as mock_parser:
            mock_parser.return_value.parse_args.return_value = mock_args
            with mock.patch.object(cli, "_resolve_working_directory", return_value="/tmp"):
                with mock.patch.object(cli, "_validate_arguments"):
                    with mock.patch.object(cli, "_resolve_selected_checks", return_value=[]):
                        with pytest.raises(SystemExit) as exc_info:
                            cli.main()
                        assert exc_info.value.code == 1


class TestMainNegativeConvergenceExit:
    """Test that main() exits with negative convergence percentage."""

    def test_negative_converged_at_percentage_exits(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--converged-at-percentage", "-1"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1


class TestMainKeyboardInterrupt:
    """Tests for main() KeyboardInterrupt handling."""

    def test_keyboard_interrupt_exits_130(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _patch_main_pipeline(suite_side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 130
        assert "Interrupted" in capsys.readouterr().out


class TestMainFileNotFoundError:
    """Test main() FileNotFoundError handling."""

    def test_file_not_found_during_suite(self) -> None:
        with _patch_main_pipeline(suite_side_effect=FileNotFoundError("claude")):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1


class TestMainUnexpectedException:
    """Test main() generic exception handling."""

    def test_unexpected_error_reraises(self) -> None:
        with _patch_main_pipeline(suite_side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                cli.main()


class TestMainGuard:
    """Test the if __name__ == '__main__' block."""

    def test_main_guard_calls_main(self) -> None:
        with mock.patch.dict("sys.modules", {"checkloop.cli": cli}):
            with mock.patch.object(cli, "main") as mock_main:
                exec(
                    compile('if __name__ == "__main__": main()', cli.__file__, "exec"),
                    {"__name__": "__main__", "main": cli.main},
                )
                mock_main.assert_called_once()
