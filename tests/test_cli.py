"""Tests for checkloop.cli — main() entry point and integration."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest

from checkloop import cli, cli_args, suite, checks
from checkloop.checks import CheckDef
from tests.helpers import make_checkpoint_data, make_mock_cli_args


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
    with mock.patch.object(cli, "build_argument_parser") as mock_parser:
        mock_parser.return_value.parse_args.return_value = mock_args
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "resolve_working_directory", return_value="/tmp"))
            stack.enter_context(mock.patch.object(cli, "validate_arguments"))
            stack.enter_context(mock.patch.object(cli, "display_pre_run_warning"))
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
        exhaustive_ids = set(checks.TIER_EXHAUSTIVE)
        for check in checks.CHECKS:
            if check["id"] in exhaustive_ids:
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
            with mock.patch.object(cli, "display_pre_run_warning") as mock_warn:
                with mock.patch.object(cli, "run_suite_with_error_handling"):
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
        with mock.patch.object(cli, "build_argument_parser") as mock_parser:
            mock_parser.return_value.parse_args.return_value = mock_args
            with mock.patch.object(cli, "resolve_working_directory", return_value="/tmp"):
                with mock.patch.object(cli, "validate_arguments"):
                    with mock.patch.object(cli, "resolve_selected_checks", return_value=[]):
                        with pytest.raises(SystemExit) as exc_info:
                            cli.main()
                        assert exc_info.value.code == 1


class TestMainNegativeConvergenceExit:
    """Test that main() exits with negative convergence percentage."""

    def test_negative_convergence_threshold_exits(self) -> None:
        with mock.patch("sys.argv", ["checkloop", "--dir", ".", "--convergence-threshold", "-1"]):
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


class TestAddFileLogHandlerOSError:
    """Test _add_file_log_handler when FileHandler creation fails."""

    def test_oserror_returns_without_adding_handler(self) -> None:
        with mock.patch("logging.FileHandler", side_effect=OSError("disk full")):
            cli._add_file_log_handler("/tmp")
        # No crash and no handler added — verified by no exception.


class TestSignalHandler:
    """Test signal handler body inside main()."""

    def test_sigterm_handler_exits(self) -> None:
        """Verify that the SIGTERM handler calls sys.exit(128 + signum)."""
        import signal as signal_mod

        captured_handlers: dict[int, object] = {}
        original_signal = signal_mod.signal

        def capture_signal(signum: int, handler: object) -> object:
            captured_handlers[signum] = handler
            return original_signal(signum, signal_mod.SIG_DFL)

        mock_args = make_mock_cli_args(dry_run=True)
        with mock.patch.object(cli, "build_argument_parser") as mock_parser:
            mock_parser.return_value.parse_args.return_value = mock_args
            with mock.patch.object(cli, "resolve_working_directory", return_value="/tmp"), \
                 mock.patch.object(cli, "validate_arguments"), \
                 mock.patch.object(cli, "display_pre_run_warning"), \
                 mock.patch.object(cli, "run_suite_with_error_handling"), \
                 mock.patch("signal.signal", side_effect=capture_signal):
                cli.main()

        handler = captured_handlers.get(signal_mod.SIGTERM)
        assert handler is not None
        with pytest.raises(SystemExit) as exc_info:
            handler(signal_mod.SIGTERM, None)  # type: ignore[operator]
        assert exc_info.value.code == 128 + signal_mod.SIGTERM

    def test_signal_registration_oserror_is_caught(self) -> None:
        """When signal.signal raises OSError, main() logs a warning and continues."""
        import signal as signal_mod

        original_signal = signal_mod.signal

        def failing_signal(signum: int, handler: object) -> object:
            raise OSError("Operation not permitted")

        mock_args = make_mock_cli_args(dry_run=True)
        with mock.patch.object(cli, "build_argument_parser") as mock_parser:
            mock_parser.return_value.parse_args.return_value = mock_args
            with mock.patch.object(cli, "resolve_working_directory", return_value="/tmp"), \
                 mock.patch.object(cli, "validate_arguments"), \
                 mock.patch.object(cli, "display_pre_run_warning"), \
                 mock.patch.object(cli, "run_suite_with_error_handling"), \
                 mock.patch("signal.signal", side_effect=failing_signal):
                # Should not raise — the OSError is caught
                cli.main()


# =============================================================================
# _try_resume_from_checkpoint — workdir resolution and mismatch
# =============================================================================

class TestTryResumeCheckpointWorkdir:
    """Tests for _try_resume_from_checkpoint workdir validation paths."""

    def test_oserror_resolving_saved_workdir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """When Path.resolve() raises OSError on the saved workdir, start fresh."""
        from pathlib import Path as RealPath
        from checkloop.checkpoint import save_checkpoint

        # Save a checkpoint with a workdir that will fail to resolve
        data = make_checkpoint_data(
            workdir="/some/bad/path\x00with\x00nulls",
            check_ids=["readability"],
            num_cycles=1,
            current_check_index=0,
            active_check_ids=["readability"],
            changed_this_cycle=[],
        )
        save_checkpoint(str(tmp_path), data)

        selected_checks: list[CheckDef] = [CheckDef(id="readability", label="Readability", prompt="review code")]
        original_resolve = RealPath.resolve

        def resolve_side_effect(self: Path, *args: Any, **kwargs: Any) -> Path:
            if "\x00" in str(self):
                raise OSError("invalid path")
            return original_resolve(self, *args, **kwargs)

        with mock.patch.object(RealPath, "resolve", resolve_side_effect):
            result = cli._try_resume_from_checkpoint(str(tmp_path), selected_checks)
        assert result is None
        out = capsys.readouterr().out
        assert "invalid workdir" in out

    def test_workdir_mismatch_starts_fresh(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """When the checkpoint's workdir differs from the current dir, start fresh."""
        from checkloop.checkpoint import save_checkpoint

        data = make_checkpoint_data(
            workdir="/completely/different/directory",
            check_ids=["readability"],
            num_cycles=1,
            current_check_index=0,
            active_check_ids=["readability"],
            changed_this_cycle=[],
        )
        save_checkpoint(str(tmp_path), data)

        selected_checks: list[CheckDef] = [CheckDef(id="readability", label="Readability", prompt="review code")]
        result = cli._try_resume_from_checkpoint(str(tmp_path), selected_checks)
        assert result is None
        out = capsys.readouterr().out
        assert "workdir differs" in out


# =============================================================================
# _try_resume_from_checkpoint — accept / decline / mismatch
# =============================================================================

class TestTryResumeFromCheckpoint:
    """Tests for _try_resume_from_checkpoint flow."""

    def test_user_accepts_resume(self, tmp_path: Path) -> None:
        """When prompt_resume returns True, the checkpoint is returned."""
        from checkloop.checkpoint import save_checkpoint
        selected: list[CheckDef] = [CheckDef(id="readability", label="R", prompt="p")]
        ckpt = make_checkpoint_data(
            workdir=str(tmp_path), check_ids=["readability"],
            num_cycles=1, convergence_threshold=0.0,
            current_check_index=0, active_check_ids=["readability"],
            changed_this_cycle=[],
        )
        save_checkpoint(str(tmp_path), ckpt)
        with mock.patch.object(cli, "prompt_resume", return_value=True):
            result = cli._try_resume_from_checkpoint(str(tmp_path), selected)
        assert result is not None
        assert result["check_ids"] == ["readability"]

    def test_user_declines_resume(self, tmp_path: Path) -> None:
        """When prompt_resume returns False, checkpoint is cleared and None returned."""
        from checkloop.checkpoint import save_checkpoint
        selected: list[CheckDef] = [CheckDef(id="readability", label="R", prompt="p")]
        ckpt = make_checkpoint_data(
            workdir=str(tmp_path), check_ids=["readability"],
            num_cycles=1, convergence_threshold=0.0,
            current_check_index=0, active_check_ids=["readability"],
            changed_this_cycle=[],
        )
        save_checkpoint(str(tmp_path), ckpt)
        with mock.patch.object(cli, "prompt_resume", return_value=False):
            result = cli._try_resume_from_checkpoint(str(tmp_path), selected)
        assert result is None

    def test_mismatched_check_ids_starts_fresh(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """When checkpoint check_ids differ from current selection, start fresh."""
        from checkloop.checkpoint import save_checkpoint
        selected: list[CheckDef] = [CheckDef(id="security", label="S", prompt="p")]
        ckpt = make_checkpoint_data(
            workdir=str(tmp_path), check_ids=["readability"],  # different from current
            num_cycles=1, convergence_threshold=0.0,
            current_check_index=0, active_check_ids=["readability"],
            changed_this_cycle=[],
        )
        save_checkpoint(str(tmp_path), ckpt)
        result = cli._try_resume_from_checkpoint(str(tmp_path), selected)
        assert result is None
        out = capsys.readouterr().out
        assert "check selection differs" in out


# =============================================================================
# _resolve_path_safe — empty input
# =============================================================================

class TestResolvePathSafe:
    """Tests for _resolve_path_safe edge cases."""

    def test_empty_string_returns_none(self) -> None:
        """An empty string should return None without raising."""
        assert cli._resolve_path_safe("") is None


# =============================================================================
# _try_resume_from_checkpoint — current workdir resolution failure
# =============================================================================

class TestTryResumeCurrentWorkdirResolutionFailure:
    """Cover the branch where the *current* workdir cannot be resolved."""

    def test_current_workdir_resolve_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """When Path.resolve() raises OSError on the current workdir, start fresh."""
        from pathlib import Path as RealPath
        from checkloop.checkpoint import save_checkpoint

        real_workdir = str(tmp_path)
        data = make_checkpoint_data(
            workdir=real_workdir,
            check_ids=["readability"],
            num_cycles=1,
            current_check_index=0,
            active_check_ids=["readability"],
            changed_this_cycle=[],
        )
        save_checkpoint(real_workdir, data)

        selected_checks: list[CheckDef] = [CheckDef(id="readability", label="Readability", prompt="review code")]
        original_resolve = RealPath.resolve
        call_count = 0

        def resolve_side_effect(self_path: Path, *args: Any, **kwargs: Any) -> Path:
            nonlocal call_count
            call_count += 1
            # First call resolves the saved workdir (succeeds).
            # Second call resolves the current workdir (fails).
            if call_count >= 2 and str(self_path) == real_workdir:
                raise OSError("disk error")
            return original_resolve(self_path, *args, **kwargs)

        with mock.patch.object(RealPath, "resolve", resolve_side_effect):
            result = cli._try_resume_from_checkpoint(real_workdir, selected_checks)
        assert result is None
        out = capsys.readouterr().out
        assert "Cannot resolve current workdir" in out
