"""Tests for checkloop.suite — state management, resume, and pre-suite commit."""

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

        with mock.patch.object(suite, "run_single_check", side_effect=tracking_run):
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
        with mock.patch.object(suite, "run_single_check", return_value=no_change_outcome):
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
# _resolve_cycle_checks — ordering preservation
# =============================================================================

class TestResolveCycleChecksOrdering:
    """Tests for _resolve_cycle_checks ordering when resuming."""

    def test_resume_preserves_checkpoint_order(self) -> None:
        """Resumed checks should follow the checkpoint's active_check_ids order,
        not the selected_checks order."""
        state = suite._SuiteState()
        state.resume_active_check_ids = ["dry", "readability", "tests"]
        state.start_check_index = 0
        state.resume_changed = set()
        # selected_checks in a different order than checkpoint
        selected = [make_check("readability"), make_check("tests"), make_check("dry")]
        active, start_idx, changed = suite._resolve_cycle_checks(selected, state)
        ids = [c["id"] for c in active]
        assert ids == ["dry", "readability", "tests"]

    def test_resume_state_consumed_after_first_call(self) -> None:
        """After _resolve_cycle_checks consumes resume state, subsequent calls return fresh state."""
        state = suite._SuiteState()
        state.resume_active_check_ids = ["a", "b"]
        state.start_check_index = 1
        state.resume_changed = {"a"}
        selected = [make_check("a"), make_check("b")]

        # First call consumes resume state
        active1, idx1, changed1 = suite._resolve_cycle_checks(selected, state)
        assert idx1 == 1
        assert changed1 == {"a"}

        # Second call returns fresh state
        active2, idx2, changed2 = suite._resolve_cycle_checks(selected, state)
        assert idx2 == 0
        assert changed2 is None


# =============================================================================
# _commit_uncommitted_changes
# =============================================================================

class TestCommitUncommittedChanges:
    """Tests for _commit_uncommitted_changes() pre-suite snapshot."""

    def test_no_changes_does_nothing(self) -> None:
        """When there are no uncommitted changes, nothing happens."""
        with mock.patch.object(suite, "has_uncommitted_changes", return_value=False) as mock_check, \
             mock.patch.object(suite, "git_commit_all") as mock_commit:
            suite._commit_uncommitted_changes("/tmp", skip_permissions=False)
            mock_check.assert_called_once_with("/tmp")
            mock_commit.assert_not_called()

    def test_commits_with_claude_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When there are changes and Claude succeeds, uses Claude's message."""
        with mock.patch.object(suite, "has_uncommitted_changes", return_value=True), \
             mock.patch.object(suite, "get_uncommitted_diff", return_value="diff --git a/foo.py"), \
             mock.patch.object(suite, "generate_commit_message", return_value="Fix typo in foo.py") as mock_gen, \
             mock.patch.object(suite, "git_commit_all", return_value=True) as mock_commit:
            suite._commit_uncommitted_changes("/tmp", skip_permissions=True)
            mock_gen.assert_called_once()
            mock_commit.assert_called_once_with("/tmp", "Fix typo in foo.py")
        out = capsys.readouterr().out
        assert "Committed uncommitted changes" in out

    def test_falls_back_when_claude_fails(self) -> None:
        """When Claude fails to generate a message, uses the fallback."""
        with mock.patch.object(suite, "has_uncommitted_changes", return_value=True), \
             mock.patch.object(suite, "get_uncommitted_diff", return_value="some diff"), \
             mock.patch.object(suite, "generate_commit_message", return_value=None), \
             mock.patch.object(suite, "git_commit_all", return_value=True) as mock_commit:
            suite._commit_uncommitted_changes("/tmp", skip_permissions=False)
            mock_commit.assert_called_once_with("/tmp", suite._FALLBACK_COMMIT_MSG)

    def test_falls_back_when_diff_empty(self) -> None:
        """When diff is empty (untracked files only), uses the fallback."""
        with mock.patch.object(suite, "has_uncommitted_changes", return_value=True), \
             mock.patch.object(suite, "get_uncommitted_diff", return_value=""), \
             mock.patch.object(suite, "generate_commit_message") as mock_gen, \
             mock.patch.object(suite, "git_commit_all", return_value=True) as mock_commit:
            suite._commit_uncommitted_changes("/tmp", skip_permissions=False)
            mock_gen.assert_not_called()
            mock_commit.assert_called_once_with("/tmp", suite._FALLBACK_COMMIT_MSG)

    def test_truncates_large_diffs(self) -> None:
        """Diffs larger than _MAX_DIFF_LEN are truncated before sending to Claude."""
        large_diff = "x" * (suite._MAX_DIFF_LEN + 1000)
        with mock.patch.object(suite, "has_uncommitted_changes", return_value=True), \
             mock.patch.object(suite, "get_uncommitted_diff", return_value=large_diff), \
             mock.patch.object(suite, "generate_commit_message", return_value="msg") as mock_gen, \
             mock.patch.object(suite, "git_commit_all", return_value=True):
            suite._commit_uncommitted_changes("/tmp", skip_permissions=False)
            sent_diff = mock_gen.call_args[0][0]
            assert len(sent_diff) < len(large_diff)
            assert "truncated" in sent_diff

    def test_passes_skip_permissions(self) -> None:
        """The skip_permissions flag is forwarded to generate_commit_message."""
        with mock.patch.object(suite, "has_uncommitted_changes", return_value=True), \
             mock.patch.object(suite, "get_uncommitted_diff", return_value="diff"), \
             mock.patch.object(suite, "generate_commit_message", return_value="msg") as mock_gen, \
             mock.patch.object(suite, "git_commit_all", return_value=True):
            suite._commit_uncommitted_changes("/tmp", skip_permissions=True)
            assert mock_gen.call_args[1]["skip_permissions"] is True
