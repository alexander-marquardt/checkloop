"""Tests for checkloop.suite — suite orchestration and convergence."""

from __future__ import annotations

import argparse
import contextlib
import time
from typing import Any, Iterator
from unittest import mock

import pytest

from checkloop import suite, process
from helpers import SHARED_ARG_DEFAULTS


# =============================================================================
# Shared test helpers
# =============================================================================

def _make_suite_args(*, dry_run: bool = True, **overrides: Any) -> argparse.Namespace:
    """Build an argparse.Namespace for _run_check_suite / _run_single_check."""
    defaults = {**SHARED_ARG_DEFAULTS, "dry_run": dry_run}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@contextlib.contextmanager
def _patch_suite_git(
    sha_sequence: list[str],
    *,
    run_claude_return: int = 0,
    lines_changed: int | None = None,
    total_tracked: int | None = None,
) -> Iterator[None]:
    """Mock common git/claude dependencies for _run_check_suite."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(suite, "run_claude", return_value=run_claude_return))
        stack.enter_context(mock.patch.object(suite, "_is_git_repo", return_value=True))
        stack.enter_context(mock.patch.object(suite, "_git_head_sha", side_effect=sha_sequence))
        stack.enter_context(mock.patch.object(suite, "_git_commit_all", return_value=True))
        stack.enter_context(mock.patch.object(suite, "_git_squash_since", return_value=True))
        if lines_changed is not None:
            stack.enter_context(mock.patch.object(suite, "_compute_change_stats", return_value=(lines_changed, lines_changed / (total_tracked or 1000) * 100)))
        if total_tracked is not None and lines_changed is None:
            pass  # total_tracked only meaningful with lines_changed
        yield


# =============================================================================
# _run_check_suite
# =============================================================================

class TestRunCheckSuite:
    """Tests for _run_check_suite() multi-check execution."""

    def test_single_check_single_cycle(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args()
        suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "Readability" in out
        assert "DRY RUN" in out

    def test_multi_cycle_banner(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = _make_suite_args()
        suite._run_check_suite(selected_checks, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out

    def test_dangerous_prompt_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "evil", "label": "Evil", "prompt": "rm -rf / everything"}]
        args = _make_suite_args(dry_run=False)
        with mock.patch.object(suite, "run_claude") as mock_run:
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
            mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "dangerous" in out.lower() or "Skipping" in out

    def test_nonzero_exit_continues(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [
            {"id": "a", "label": "A", "prompt": "do a"},
            {"id": "b", "label": "B", "prompt": "do b"},
        ]
        args = _make_suite_args(dry_run=False)
        with mock.patch.object(suite, "run_claude", return_value=1):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "exited with code 1" in out
        assert "A" in out
        assert "B" in out

    def test_noop_checks_skipped_on_cycle2(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [
            {"id": "readability", "label": "Readability", "prompt": "review code"},
            {"id": "dry", "label": "DRY", "prompt": "check dry"},
        ]
        args = _make_suite_args(dry_run=False)
        sha_sequence = [
            "cycle1_base",
            "sha_r_before", "sha_r_after",
            "sha_d_before", "sha_d_before",
            "cycle2_base",
            "sha_r2_before", "sha_r2_after",
        ]
        with _patch_suite_git(sha_sequence, lines_changed=10, total_tracked=1000):
            suite._run_check_suite(selected_checks, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Skipping 1 check(s)" in out

    def test_bookend_checks_always_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [
            {"id": "test-fix", "label": "Test Fix", "prompt": "fix tests"},
            {"id": "readability", "label": "Readability", "prompt": "review code"},
            {"id": "test-validate", "label": "Test Validate", "prompt": "validate tests"},
        ]
        args = _make_suite_args(dry_run=False)
        sha_sequence = [
            "c1_base",
            "s1", "s1",  "s2", "s2",  "s3", "s3",
            "c2_base",
            "s4", "s4",  "s5", "s5",
        ]
        with _patch_suite_git(sha_sequence):
            suite._run_check_suite(selected_checks, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Skipping 1 check(s)" in out
        assert out.count("Test Fix") == 2
        assert out.count("Test Validate") == 2
        assert out.count("Readability") == 1

    def test_all_checks_active_no_skip(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [
            {"id": "readability", "label": "Readability", "prompt": "review code"},
            {"id": "dry", "label": "DRY", "prompt": "check dry"},
        ]
        args = _make_suite_args(dry_run=False)
        sha_sequence = [
            "c1_base",
            "a1", "a2",  "b1", "b2",
            "c2_base",
            "c1", "c2",  "d1", "d2",
        ]
        with _patch_suite_git(sha_sequence, lines_changed=10, total_tracked=1000):
            suite._run_check_suite(selected_checks, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Skipping" not in out

    def test_check_change_stats_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["base", "sha1", "sha2"], lines_changed=42, total_tracked=5000):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "42 lines changed" in out
        assert "0.84%" in out

    def test_no_change_stats_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["base", "same", "same"]):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "dry: no changes" in out


# =============================================================================
# _filter_active_checks
# =============================================================================

class TestFilterActiveChecks:
    """Tests for _filter_active_checks()."""

    def test_empty_check_list(self) -> None:
        assert suite._filter_active_checks([], None) == []

    def test_empty_check_list_with_previous(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = suite._filter_active_checks([], set())
        assert result == []

    def test_all_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "readability", "label": "R", "prompt": "p"}]
        result = suite._filter_active_checks(selected_checks, set())
        assert result == []
        assert "Skipping 1" in capsys.readouterr().out

    def test_bookend_always_included(self) -> None:
        selected_checks = [
            {"id": "test-fix", "label": "TF", "prompt": "p"},
            {"id": "readability", "label": "R", "prompt": "p"},
            {"id": "test-validate", "label": "TV", "prompt": "p"},
        ]
        result = suite._filter_active_checks(selected_checks, set())
        assert len(result) == 2
        assert result[0]["id"] == "test-fix"
        assert result[1]["id"] == "test-validate"


# =============================================================================
# _check_cycle_convergence
# =============================================================================

class TestCheckCycleConvergence:
    """Tests for _check_cycle_convergence() convergence detection."""

    def test_no_changes_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "_git_commit_all"):
            with mock.patch.object(suite, "_git_head_sha", return_value="abc123"):
                should_stop, pct = suite._check_cycle_convergence(
                    "/tmp", cycle=1, base_sha="abc123",
                    convergence_threshold=0.1, prev_change_pct=None,
                )
        assert should_stop is True
        assert pct is None
        assert "converged" in capsys.readouterr().out.lower()

    def test_oscillation_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "_git_commit_all"), \
             mock.patch.object(suite, "_git_head_sha", return_value="def456"), \
             mock.patch.object(suite, "_compute_change_stats", return_value=(50, 5.0)):
            should_stop, pct = suite._check_cycle_convergence(
                "/tmp", cycle=2, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=2.0,
            )
        assert should_stop is False
        assert pct == 5.0
        assert "oscillation" in capsys.readouterr().out.lower()

    def test_not_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "_git_commit_all"), \
             mock.patch.object(suite, "_git_head_sha", return_value="def456"), \
             mock.patch.object(suite, "_compute_change_stats", return_value=(15, 1.5)):
            should_stop, pct = suite._check_cycle_convergence(
                "/tmp", cycle=1, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=None,
            )
        assert should_stop is False
        assert pct == 1.5

    def test_no_changes_converges(self) -> None:
        with mock.patch.object(suite, "_git_commit_all"):
            with mock.patch.object(suite, "_git_head_sha", return_value="same_sha"):
                converged, pct = suite._check_cycle_convergence(
                    "/tmp", 1, "same_sha", 0.1, None,
                )
        assert converged is True
        assert pct is None

    def test_changes_below_threshold_converges(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "_git_commit_all"):
            with mock.patch.object(suite, "_git_head_sha", return_value="new_sha"):
                with mock.patch.object(suite, "_compute_change_stats", return_value=(5, 0.05)):
                    converged, pct = suite._check_cycle_convergence(
                        "/tmp", 1, "old_sha", 0.1, None,
                    )
        assert converged is True
        assert pct == 0.05

    def test_increasing_changes_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "_git_commit_all"):
            with mock.patch.object(suite, "_git_head_sha", return_value="new_sha"):
                with mock.patch.object(suite, "_compute_change_stats", return_value=(100, 5.0)):
                    converged, pct = suite._check_cycle_convergence(
                        "/tmp", 2, "old_sha", 0.1, 2.0,
                    )
        assert converged is False
        assert pct == 5.0
        out = capsys.readouterr().out
        assert "oscillation" in out.lower()


# =============================================================================
# Convergence in suite
# =============================================================================

class TestConvergenceInSuite:
    """Tests for convergence detection within _run_check_suite."""

    def test_stops_early_when_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["sha1", "sha2", "sha2", "sha3"]), \
             mock.patch.object(suite, "_git_commit_all", return_value=True), \
             mock.patch.object(suite, "_compute_change_stats", return_value=(1, 0.05)):
            suite._run_check_suite(selected_checks, 3, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Converged" in out

    def test_continues_when_not_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["sha1"] * 10), \
             mock.patch.object(suite, "_check_cycle_convergence", return_value=(False, 5.0)):
            suite._run_check_suite(selected_checks, 2, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out

    def test_no_convergence_without_git(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = _make_suite_args()
        suite._run_check_suite(selected_checks, 2, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out


# =============================================================================
# _run_single_check
# =============================================================================

class TestRunSingleCheck:
    """Tests for _run_single_check()."""

    def test_missing_changed_files_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        check_def = {"id": "readability", "label": "Readability", "prompt": "review code"}
        args = _make_suite_args(dry_run=True)
        if hasattr(args, "changed_files_prefix"):
            delattr(args, "changed_files_prefix")
        with mock.patch.object(suite, "_is_git_repo", return_value=False):
            result = suite._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
        assert result is True

    def test_changed_prefix_prepended_to_prompt(self) -> None:
        selected_checks = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args(dry_run=False)
        args.changed_files_prefix = "ONLY THESE FILES: a.py\n\n"
        with mock.patch.object(suite, "run_claude", return_value=0) as mock_run, \
             mock.patch.object(suite, "_is_git_repo", return_value=False):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
            prompt_used = mock_run.call_args[0][0]
            assert prompt_used.startswith("ONLY THESE FILES: a.py")
            assert "review code" in prompt_used


# =============================================================================
# _report_check_changes
# =============================================================================

class TestReportCheckChanges:
    """Tests for _report_check_changes()."""

    def test_no_git_repo_assumes_changes(self) -> None:
        assert suite._report_check_changes("/tmp", "test", None) is True

    def test_same_sha_no_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "_git_head_sha", return_value="sha1"):
            result = suite._report_check_changes("/tmp", "test", "sha1")
        assert result is False
        assert "no changes" in capsys.readouterr().out

    def test_different_sha_reports_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "_git_head_sha", return_value="sha2"):
            with mock.patch.object(suite, "_compute_change_stats", return_value=(10, 0.50)):
                result = suite._report_check_changes("/tmp", "test", "sha1")
        assert result is True
        assert "10 lines changed" in capsys.readouterr().out


# =============================================================================
# Commit message instructions
# =============================================================================

class TestCommitMessageInstructions:
    """Tests that commit message instructions are appended to prompts."""

    def test_prompt_includes_commit_instructions(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args()
        with mock.patch.object(suite, "run_claude", return_value=0) as mock_run:
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
            prompt_used = mock_run.call_args[0][0]
            assert "commit message rules" in prompt_used
            assert "Do not mention Claude" in prompt_used


# =============================================================================
# _display_pre_run_warning
# =============================================================================

class TestDisplayPreRunWarning:
    """Tests for _display_pre_run_warning() permission warnings."""

    def test_skip_permissions_true(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("time.sleep"):
            suite._display_pre_run_warning(skip_permissions=True)
        out = capsys.readouterr().out
        assert "dangerously-skip-permissions is ENABLED" in out
        assert f"Starting in {suite._PRE_RUN_WARNING_DELAY} seconds" in out

    def test_skip_permissions_false(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("time.sleep"):
            suite._display_pre_run_warning(skip_permissions=False)
        out = capsys.readouterr().out
        assert "Running without --dangerously-skip-permissions" in out
        assert "Re-run with" in out
        assert "Continuing anyway" in out

    def test_keyboard_interrupt_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                suite._display_pre_run_warning(skip_permissions=True)
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Aborted" in out
