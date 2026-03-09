"""Tests for checkloop.suite — suite orchestration and convergence."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from checkloop import check_runner, checkpoint, suite
from checkloop.checks import CheckDef
from checkloop.process import CheckResult
from tests.helpers import make_check, make_checkpoint_data, make_suite_args, patch_suite_git


# =============================================================================
# _run_check_suite
# =============================================================================

class TestRunCheckSuite:
    """Tests for _run_check_suite() multi-check execution."""

    def test_single_check_single_cycle(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("readability", "Readability", "review code")]
        args = make_suite_args()
        suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "Readability" in out
        assert "DRY RUN" in out

    def test_multi_cycle_banner(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("dry", "DRY", "check dry")]
        args = make_suite_args()
        suite._run_check_suite(selected_checks, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out

    def test_dangerous_prompt_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("evil", "Evil", "rm -rf / everything")]
        args = make_suite_args(dry_run=False)
        with mock.patch.object(check_runner, "_invoke_claude") as mock_run:
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
            mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "dangerous" in out.lower() or "Skipping" in out

    def test_nonzero_exit_continues(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [
            make_check("a", "A", "do a"),
            make_check("b", "B", "do b"),
        ]
        args = make_suite_args(dry_run=False)
        with mock.patch.object(check_runner, "_invoke_claude", return_value=CheckResult(exit_code=1)):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "exited with code 1" in out
        assert "A" in out
        assert "B" in out

    def test_all_checks_run_every_cycle(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Even if a check made no changes in cycle 1, it still runs in cycle 2."""
        selected_checks: list[CheckDef] = [
            make_check("readability", "Readability", "review code"),
            make_check("dry", "DRY", "check dry"),
        ]
        args = make_suite_args(dry_run=False)
        sha_sequence = [
            "cycle1_base",
            "sha_r_before", "sha_r_after",
            "sha_d_before", "sha_d_before",  # dry made no changes in cycle 1
            "cycle2_base",
            "sha_r2_before", "sha_r2_after",
            "sha_d2_before", "sha_d2_after",  # dry still runs in cycle 2
        ]
        with patch_suite_git(sha_sequence, lines_changed=10, total_tracked=1000):
            suite._run_check_suite(selected_checks, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Skipping" not in out
        assert out.count("Readability") == 2
        assert out.count("DRY") == 2

    def test_check_change_stats_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("readability", "Readability", "review code")]
        args = make_suite_args(dry_run=False)
        with patch_suite_git(["base", "sha1", "sha2"], lines_changed=42, total_tracked=5000):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "42 lines changed" in out
        assert "0.84%" in out

    def test_no_change_stats_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("dry", "DRY", "check dry")]
        args = make_suite_args(dry_run=False)
        with patch_suite_git(["base", "same", "same"]):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "dry: no changes" in out


class TestRunCheckSuiteEdgeCases:
    """Edge case tests for _run_check_suite."""

    def test_empty_selected_checks(self) -> None:
        """Suite with zero checks should complete without error."""
        args = make_suite_args(dry_run=True)
        with mock.patch.object(suite, "clear_checkpoint"):
            suite._run_check_suite([], 1, "/tmp", args)

    def test_checkpoint_save_exception_is_swallowed(self) -> None:
        """If save_checkpoint raises inside _save_after_check, the suite continues."""
        selected_checks: list[CheckDef] = [
            make_check("a", "A", "do a"),
            make_check("b", "B", "do b"),
        ]
        args = make_suite_args(dry_run=True)
        with mock.patch.object(suite, "save_checkpoint", side_effect=RuntimeError("disk full")):
            with mock.patch.object(suite, "clear_checkpoint"):
                # Should NOT raise — the exception is caught and logged.
                suite._run_check_suite(selected_checks, 1, "/tmp", args)

    def test_single_cycle_no_convergence_check(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With 1 cycle and convergence enabled, convergence is checked but loop exits after 1 cycle."""
        selected_checks: list[CheckDef] = [make_check("a", "A", "do a")]
        args = make_suite_args(dry_run=False)
        # SHAs: base, before-check, after-check, convergence-check
        with patch_suite_git(["sha1", "sha2", "sha3", "sha3"], lines_changed=5, total_tracked=1000):
            suite._run_check_suite(selected_checks, 1, "/tmp", args, convergence_threshold=0.1)
        # Should complete without "Cycle 2" appearing
        out = capsys.readouterr().out
        assert "Cycle 2" not in out


# =============================================================================
# _check_cycle_convergence
# =============================================================================

class TestCheckCycleConvergence:
    """Tests for _check_cycle_convergence() convergence detection."""

    def test_no_changes_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "git_head_sha", return_value="abc123"):
            should_stop, pct = suite._check_cycle_convergence(
                "/tmp", cycle=1, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=None,
            )
        assert should_stop is True
        assert pct is None
        assert "converged" in capsys.readouterr().out.lower()

    def test_oscillation_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "git_head_sha", return_value="def456"), \
             mock.patch.object(suite, "compute_change_stats", return_value=(50, 5.0)):
            should_stop, pct = suite._check_cycle_convergence(
                "/tmp", cycle=2, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=2.0,
            )
        assert should_stop is False
        assert pct == 5.0
        assert "oscillation" in capsys.readouterr().out.lower()

    def test_not_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "git_head_sha", return_value="def456"), \
             mock.patch.object(suite, "compute_change_stats", return_value=(15, 1.5)):
            should_stop, pct = suite._check_cycle_convergence(
                "/tmp", cycle=1, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=None,
            )
        assert should_stop is False
        assert pct == 1.5

    def test_changes_below_threshold_converges(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(suite, "git_head_sha", return_value="new_sha"):
            with mock.patch.object(suite, "compute_change_stats", return_value=(5, 0.05)):
                converged, pct = suite._check_cycle_convergence(
                        "/tmp", 1, "old_sha", 0.1, None,
                    )
        assert converged is True
        assert pct == 0.05


class TestCheckCycleConvergenceNoneSha:
    """Tests for _check_cycle_convergence when HEAD SHA is unavailable."""

    def test_current_sha_none_skips_convergence(self) -> None:
        """If git_head_sha returns None, convergence check is skipped."""
        with mock.patch.object(suite, "git_head_sha", return_value=None):
            should_stop, pct = suite._check_cycle_convergence(
                "/tmp", cycle=1, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=None,
            )
        assert should_stop is False
        assert pct is None


# =============================================================================
# Convergence in suite
# =============================================================================

class TestConvergenceInSuite:
    """Tests for convergence detection within _run_check_suite."""

    def test_stops_early_when_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("readability", "Readability", "review code")]
        args = make_suite_args(dry_run=False)
        with patch_suite_git(["sha1", "sha2", "sha2", "sha3"]), \
             mock.patch.object(suite, "compute_change_stats", return_value=(1, 0.05)):
            suite._run_check_suite(selected_checks, 3, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Converged" in out

    def test_continues_when_not_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("readability", "Readability", "review code")]
        args = make_suite_args(dry_run=False)
        with patch_suite_git(["sha1"] * 10), \
             mock.patch.object(suite, "_check_cycle_convergence", return_value=(False, 5.0)):
            suite._run_check_suite(selected_checks, 2, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out

    def test_no_convergence_without_git(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("dry", "DRY", "check dry")]
        args = make_suite_args()
        suite._run_check_suite(selected_checks, 2, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out


# =============================================================================
# Resume from checkpoint
# =============================================================================

class TestResumeFromCheckpoint:
    """Tests for resuming a suite from a checkpoint."""

    def test_resume_skips_completed_checks(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When resuming at check_index=1, the first check should be skipped."""
        selected_checks: list[CheckDef] = [
            make_check("readability", "Readability", "review code"),
            make_check("dry", "DRY", "check dry"),
            make_check("tests", "Tests", "run tests"),
        ]
        args = make_suite_args(dry_run=False)
        resume_data = make_checkpoint_data(
            workdir="/tmp",
            check_ids=["readability", "dry", "tests"],
            num_cycles=1,
            convergence_threshold=0.0,
            current_check_index=1,
            active_check_ids=["readability", "dry", "tests"],
            changed_this_cycle=["readability"],
        )
        call_ids = []

        def tracking_run(check: CheckDef, *a: Any, **kw: Any) -> suite.CheckOutcome:
            call_ids.append(check["id"])
            return suite.CheckOutcome(
                check_id=check["id"], label=check["label"], cycle=1,
                exit_code=0, kill_reason=None, made_changes=False,
                lines_changed=0, change_pct=0.0, duration_seconds=0.1,
            )

        with mock.patch.object(suite, "_run_single_check", side_effect=tracking_run):
            with mock.patch.object(suite, "is_git_repo", return_value=False):
                with mock.patch.object(suite, "clear_checkpoint"):
                    suite._run_check_suite(
                        selected_checks, 1, "/tmp", args,
                        resume_from=resume_data,
                    )
        # "readability" (index 0) should be skipped; "dry" and "tests" should run.
        assert "readability" not in call_ids
        assert "dry" in call_ids
        assert "tests" in call_ids

    def test_checkpoint_saved_after_each_check(self, tmp_path: Path) -> None:
        """Verify save_checkpoint is called after each check completes."""
        selected_checks: list[CheckDef] = [
            make_check("a", "A", "do a"),
            make_check("b", "B", "do b"),
        ]
        args = make_suite_args(dry_run=True)
        with mock.patch.object(suite, "save_checkpoint") as mock_save:
            with mock.patch.object(suite, "clear_checkpoint"):
                suite._run_check_suite(selected_checks, 1, "/tmp", args)
        # save_checkpoint should be called once per check.
        assert mock_save.call_count == 2

    def test_checkpoint_cleared_on_success(self) -> None:
        """Verify clear_checkpoint is called when suite completes."""
        selected_checks: list[CheckDef] = [make_check("a", "A", "do a")]
        args = make_suite_args(dry_run=True)
        with mock.patch.object(suite, "clear_checkpoint") as mock_clear:
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
        mock_clear.assert_called_once_with("/tmp")

    def test_resume_preserves_changed_set(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Resumed checks should include already-changed IDs from checkpoint."""
        selected_checks: list[CheckDef] = [
            make_check("a", "A", "do a"),
            make_check("b", "B", "do b"),
        ]
        args = make_suite_args(dry_run=False)
        resume_data = make_checkpoint_data(
            workdir="/tmp",
            check_ids=["a", "b"],
            num_cycles=1,
            convergence_threshold=0.0,
            current_check_index=1,
            active_check_ids=["a", "b"],
            changed_this_cycle=["a"],
        )
        # Make check "b" return no changes.
        no_change_outcome = suite.CheckOutcome(
            check_id="b", label="B", cycle=1, exit_code=0, kill_reason=None,
            made_changes=False, lines_changed=0, change_pct=0.0, duration_seconds=0.1,
        )
        with mock.patch.object(suite, "_run_single_check", return_value=no_change_outcome):
            with mock.patch.object(suite, "is_git_repo", return_value=False):
                with mock.patch.object(suite, "save_checkpoint") as mock_save:
                    with mock.patch.object(suite, "clear_checkpoint"):
                        suite._run_check_suite(
                            selected_checks, 1, "/tmp", args,
                            resume_from=resume_data,
                        )
        # The last save should still have "a" in changed_this_cycle (from checkpoint).
        last_call_data = mock_save.call_args[0][1]
        assert "a" in last_call_data["changed_this_cycle"]


# =============================================================================
# _build_suite_state edge cases
# =============================================================================

class TestBuildSuiteStateEdgeCases:
    """Edge cases for _build_suite_state()."""

    def test_none_resume_creates_fresh_state(self) -> None:
        state = suite._build_suite_state(None)
        assert state.start_cycle == 1
        assert state.start_check_index == 0
        assert state.resume_active_check_ids is None
        assert state.resume_changed == set()
        assert state.prev_change_pct is None
        assert state.previously_changed_ids is None
        assert state.started_at != ""

    def test_resume_with_previously_changed_ids(self) -> None:
        data = make_checkpoint_data(
            previously_changed_ids=["a", "b"],
            prev_change_pct=2.5,
        )
        state = suite._build_suite_state(data)
        assert state.previously_changed_ids == {"a", "b"}
        assert state.prev_change_pct == 2.5


# =============================================================================
# _resolve_cycle_checks edge cases
# =============================================================================

class TestResolveCycleChecksEdgeCases:
    """Edge cases for _resolve_cycle_checks()."""

    def test_resume_check_not_in_selected(self) -> None:
        """When resume active_check_ids includes IDs not in selected_checks, they're filtered."""
        state = suite._SuiteState()
        state.resume_active_check_ids = ["a", "b", "c"]
        state.start_check_index = 0
        state.resume_changed = set()
        selected = [make_check("a"), make_check("c")]
        active, start_idx, changed = suite._resolve_cycle_checks(selected, state)
        ids = [c["id"] for c in active]
        assert "b" not in ids
        assert "a" in ids
        assert "c" in ids

    def test_non_resume_returns_all_selected(self) -> None:
        state = suite._SuiteState()
        selected = [make_check("x"), make_check("y")]
        active, start_idx, changed = suite._resolve_cycle_checks(selected, state)
        assert len(active) == 2
        assert start_idx == 0
        assert changed is None


# =============================================================================
# _print_summary — multi-cycle branch
# =============================================================================

class TestPrintSummaryMultiCycle:
    """Tests for _print_summary when outcomes span multiple cycles."""

    def test_multi_cycle_calls_overall_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When outcomes span 2+ cycles, print_overall_summary_table is called."""
        outcomes = [
            suite.CheckOutcome(
                check_id="a", label="A", cycle=1, exit_code=0, kill_reason=None,
                made_changes=True, lines_changed=10, change_pct=1.0, duration_seconds=5.0,
            ),
            suite.CheckOutcome(
                check_id="a", label="A", cycle=2, exit_code=0, kill_reason=None,
                made_changes=False, lines_changed=0, change_pct=0.0, duration_seconds=3.0,
            ),
        ]
        with mock.patch.object(suite, "print_overall_summary_table") as mock_overall:
            suite._print_summary(outcomes, "0m08s")
            mock_overall.assert_called_once()
            summary_dicts = mock_overall.call_args[0][0]
            assert len(summary_dicts) == 2
