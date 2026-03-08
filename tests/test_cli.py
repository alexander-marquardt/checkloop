"""Tests for checkloop.cli — main() entry point and integration."""

from __future__ import annotations

import contextlib
from typing import Any, Iterator
from unittest import mock

import pytest

from checkloop import cli, suite, checks
from helpers import make_mock_cli_args


# =============================================================================
# Shared test helpers
# =============================================================================

@contextlib.contextmanager
def _patch_main_pipeline(
    *,
    suite_side_effect: type[BaseException] | BaseException | None = None,
    **arg_overrides: Any,
) -> Iterator[None]:
    """Mock the standard main() pipeline for exception-path tests."""
    mock_args = make_mock_cli_args(**arg_overrides)
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
        for check in checks.CHECKS:
            assert check["label"] in out

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

    def test_specific_checks(self, capsys: pytest.CaptureFixture[str]) -> None:
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

    def test_empty_checks_exits(self) -> None:
        mock_args = make_mock_cli_args(dry_run=True, checks=None, level=None)
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
