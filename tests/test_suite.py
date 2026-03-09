"""Tests for checkloop.suite — suite orchestration and convergence.

State management tests (resume, cycle resolution, pre-suite commit) live in
test_suite_state.py.
"""

from __future__ import annotations

from unittest import mock

import pytest

from checkloop import check_runner, suite
from checkloop.checks import CheckDef
from checkloop.process import CheckResult
from tests.helpers import make_check, make_suite_args, patch_suite_git


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


# =============================================================================
# _print_cycle_summary — single-cycle suppression
# =============================================================================

class TestPrintCycleSummary:
    """Tests for _print_cycle_summary() output suppression."""

    def test_single_cycle_does_not_print(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With num_cycles=1, per-cycle summary is suppressed to avoid redundancy."""
        outcomes = [
            suite.CheckOutcome(
                check_id="a", label="A", cycle=1, exit_code=0, kill_reason=None,
                made_changes=True, lines_changed=10, change_pct=1.0, duration_seconds=5.0,
            ),
        ]
        suite._print_cycle_summary(outcomes, cycle=1, num_cycles=1)
        out = capsys.readouterr().out
        assert out == ""

    def test_multi_cycle_prints_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With num_cycles>1, per-cycle summary is printed."""
        outcomes = [
            suite.CheckOutcome(
                check_id="a", label="A", cycle=1, exit_code=0, kill_reason=None,
                made_changes=True, lines_changed=10, change_pct=1.0, duration_seconds=5.0,
            ),
        ]
        suite._print_cycle_summary(outcomes, cycle=1, num_cycles=3)
        out = capsys.readouterr().out
        assert "Cycle 1/3 Summary" in out

    def test_empty_outcomes_does_not_print(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With empty outcomes, nothing is printed regardless of cycle count."""
        suite._print_cycle_summary([], cycle=1, num_cycles=3)
        assert capsys.readouterr().out == ""


# =============================================================================
# _print_summary — single-cycle branch
# =============================================================================

class TestPrintSummarySingleCycle:
    """Tests for _print_summary when outcomes span a single cycle."""

    def test_single_cycle_calls_run_summary_table(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When outcomes are all from one cycle, print_run_summary_table is called."""
        outcomes = [
            suite.CheckOutcome(
                check_id="a", label="A", cycle=1, exit_code=0, kill_reason=None,
                made_changes=True, lines_changed=10, change_pct=1.0, duration_seconds=5.0,
            ),
            suite.CheckOutcome(
                check_id="b", label="B", cycle=1, exit_code=0, kill_reason=None,
                made_changes=False, lines_changed=0, change_pct=0.0, duration_seconds=3.0,
            ),
        ]
        with mock.patch.object(suite, "print_run_summary_table") as mock_table:
            suite._print_summary(outcomes, "0m08s")
            mock_table.assert_called_once()
            call_kwargs = mock_table.call_args
            assert call_kwargs[1]["banner_title"] == "Run Summary"

    def test_empty_outcomes_does_not_print(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty outcomes should produce no output."""
        suite._print_summary([], "0m00s")
        assert capsys.readouterr().out == ""


# =============================================================================
# run_suite_with_error_handling — generic exception prints partial results
# =============================================================================

class TestRunSuiteWithErrorHandlingPartialResults:
    """Tests for run_suite_with_error_handling printing partial results on error."""

    def test_generic_exception_prints_partial_results_and_reraises(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When _run_check_suite raises a generic exception, partial results
        are printed before re-raising."""
        selected_checks: list[CheckDef] = [
            make_check("a", "A", "do a"),
            make_check("b", "B", "do b"),
        ]
        args = make_suite_args(dry_run=False)

        def fill_outcomes_then_crash(
            checks: list[CheckDef], *a: object, all_outcomes: list[suite.CheckOutcome] | None = None, **kw: object,
        ) -> list[suite.CheckOutcome]:
            if all_outcomes is not None:
                all_outcomes.append(suite.CheckOutcome(
                    check_id="a", label="A", cycle=1, exit_code=0, kill_reason=None,
                    made_changes=True, lines_changed=10, change_pct=1.0, duration_seconds=5.0,
                ))
            raise ValueError("unexpected crash")

        with mock.patch.object(suite, "_run_check_suite", side_effect=fill_outcomes_then_crash):
            with pytest.raises(ValueError, match="unexpected crash"):
                suite.run_suite_with_error_handling(
                    selected_checks, 1, "/tmp", args, 0.1,
                )
        out = capsys.readouterr().out
        assert "Unexpected error" in out
        # Partial result from check "a" should appear in summary
        assert "Total lines  : 10" in out

    def test_keyboard_interrupt_prints_partial_results(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """KeyboardInterrupt should print partial results before exiting."""
        selected_checks: list[CheckDef] = [make_check("a", "A", "do a")]
        args = make_suite_args(dry_run=False)

        def fill_then_interrupt(
            checks: list[CheckDef], *a: object, all_outcomes: list[suite.CheckOutcome] | None = None, **kw: object,
        ) -> list[suite.CheckOutcome]:
            if all_outcomes is not None:
                all_outcomes.append(suite.CheckOutcome(
                    check_id="a", label="A", cycle=1, exit_code=0, kill_reason=None,
                    made_changes=True, lines_changed=42, change_pct=2.0, duration_seconds=10.0,
                ))
            raise KeyboardInterrupt

        with mock.patch.object(suite, "_run_check_suite", side_effect=fill_then_interrupt):
            with pytest.raises(SystemExit) as exc_info:
                suite.run_suite_with_error_handling(
                    selected_checks, 1, "/tmp", args, 0.1,
                )
            assert exc_info.value.code == 130
        out = capsys.readouterr().out
        assert "Interrupted" in out


# =============================================================================
# Multi-cycle convergence early stop integration
# =============================================================================

class TestMultiCycleConvergenceEarlyStop:
    """Integration test for convergence causing early cycle termination."""

    def test_convergence_stops_after_first_cycle(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When changes are below threshold after cycle 1, cycle 2 should not run."""
        selected_checks: list[CheckDef] = [make_check("a", "A", "do a")]
        args = make_suite_args(dry_run=False)
        # SHAs: base for cycle 1, before check, after check (different = changes), convergence check
        with patch_suite_git(["base1", "sha1", "sha2", "sha2"], lines_changed=1, total_tracked=10000):
            # 1 line in 10000 = 0.01%, below 0.1% threshold
            suite._run_check_suite(selected_checks, 3, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Converged" in out
        assert "Cycle 2" not in out


