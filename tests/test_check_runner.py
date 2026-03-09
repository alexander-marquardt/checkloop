"""Tests for checkloop.check_runner — single check execution, change reporting, and memory-fix."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from checkloop import check_runner, process, suite
from checkloop.checks import CheckDef
from checkloop.process import CheckResult
from tests.helpers import make_check, make_suite_args


# =============================================================================
# _run_single_check
# =============================================================================

class TestRunSingleCheck:
    """Tests for _run_single_check()."""

    def test_missing_changed_files_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        check_def: CheckDef = make_check("readability", "Readability", "review code")
        args = make_suite_args(dry_run=True)
        if hasattr(args, "changed_files_prefix"):
            delattr(args, "changed_files_prefix")
        result = check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
        assert result.made_changes is True

    def test_changed_prefix_prepended_to_prompt(self) -> None:
        selected_checks: list[CheckDef] = [make_check("readability", "Readability", "review code")]
        args = make_suite_args(dry_run=False)
        args.changed_files_prefix = "ONLY THESE FILES: a.py\n\n"
        with mock.patch.object(check_runner, "_invoke_claude", return_value=CheckResult(exit_code=0)) as mock_run, \
             mock.patch.object(suite, "is_git_repo", return_value=False):
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
            prompt_used = mock_run.call_args[0][0]
            assert prompt_used.startswith("ONLY THESE FILES: a.py")
            assert "review code" in prompt_used


class TestRunSingleCheckNoCommit:
    """Tests for _run_single_check when git commit returns no changes."""

    def test_no_commit_after_check(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When git_commit_all returns False, the no-commit debug path is hit."""
        check_def: CheckDef = make_check("readability", "Readability", "review code")
        args = make_suite_args(dry_run=False)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=CheckResult(exit_code=0)), \
             mock.patch.object(check_runner, "git_head_sha", return_value="sha1"), \
             mock.patch.object(check_runner, "git_commit_all", return_value=False), \
             mock.patch.object(check_runner, "_report_check_changes", return_value=(False, 0, 0.0)):
            result = check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=True)
        assert result.made_changes is False


class TestRunSingleCheckEdgeCases:
    """Edge cases for _run_single_check."""

    def test_idle_timeout_kill_does_not_trigger_memory_fix(self) -> None:
        """KILL_REASON_IDLE should NOT trigger _run_memory_fix."""
        check_def = CheckDef(id="test", label="Test", prompt="do test")
        args = make_suite_args(dry_run=False)
        idle_result = CheckResult(exit_code=-9, kill_reason=process.KILL_REASON_IDLE)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=idle_result), \
             mock.patch.object(check_runner, "_run_memory_fix") as mock_fix:
            check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_not_called()

    def test_timeout_kill_does_not_trigger_memory_fix(self) -> None:
        """KILL_REASON_TIMEOUT should NOT trigger _run_memory_fix."""
        check_def = CheckDef(id="test", label="Test", prompt="do test")
        args = make_suite_args(dry_run=False)
        timeout_result = CheckResult(exit_code=-9, kill_reason=process.KILL_REASON_TIMEOUT)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=timeout_result), \
             mock.patch.object(check_runner, "_run_memory_fix") as mock_fix:
            check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_not_called()


class TestInvokeClaudeExceptionInRunSingleCheck:
    """Tests for _run_single_check when _invoke_claude raises an unexpected exception."""

    def test_invoke_claude_exception_returns_error_outcome(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When _invoke_claude raises, _run_single_check catches it and returns exit_code=-1."""
        check_def = CheckDef(id="readability", label="Readability", prompt="review code")
        args = make_suite_args(dry_run=False)

        with mock.patch.object(check_runner, "_invoke_claude", side_effect=OSError("connection lost")), \
             mock.patch.object(check_runner, "git_head_sha", return_value="abc123"):
            outcome = check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=True)
        assert outcome.exit_code == -1
        out = capsys.readouterr().out
        assert "failed with error" in out


# =============================================================================
# _report_check_changes
# =============================================================================

class TestReportCheckChanges:
    """Tests for _report_check_changes()."""

    def test_no_git_repo_assumes_changes(self) -> None:
        made, lines, pct = check_runner._report_check_changes("/tmp", "test", None)
        assert made is True
        assert lines is None

    def test_same_sha_no_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(check_runner, "git_head_sha", return_value="sha1"):
            made, lines, pct = check_runner._report_check_changes("/tmp", "test", "sha1")
        assert made is False
        assert lines == 0
        assert "no changes" in capsys.readouterr().out

    def test_different_sha_reports_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(check_runner, "git_head_sha", return_value="sha2"):
            with mock.patch.object(check_runner, "compute_change_stats", return_value=(10, 0.50)):
                made, lines, pct = check_runner._report_check_changes("/tmp", "test", "sha1")
        assert made is True
        assert lines == 10
        assert pct == 0.50
        assert "10 lines changed" in capsys.readouterr().out

    def test_sha_after_none_assumes_changes(self) -> None:
        """When git_head_sha returns None after a check, assume changes were made."""
        with mock.patch.object(check_runner, "git_head_sha", return_value=None):
            made, lines, pct = check_runner._report_check_changes("/tmp", "test", "sha1")
        assert made is True
        assert lines is None


# =============================================================================
# Commit message instructions
# =============================================================================

class TestCommitMessageInstructions:
    """Tests that commit message instructions are appended to prompts."""

    def test_prompt_includes_commit_instructions(self, capsys: pytest.CaptureFixture[str]) -> None:
        selected_checks: list[CheckDef] = [make_check("readability", "Readability", "review code")]
        args = make_suite_args()
        with mock.patch.object(check_runner, "_invoke_claude", return_value=CheckResult(exit_code=0)) as mock_run:
            suite._run_check_suite(selected_checks, 1, "/tmp", args)
            prompt_used = mock_run.call_args[0][0]
            assert "commit message rules" in prompt_used
            assert "Do NOT mention Claude" in prompt_used


# =============================================================================
# CheckOutcome.to_summary_dict
# =============================================================================

class TestCheckOutcomeToSummaryDict:
    """Edge cases for CheckOutcome.to_summary_dict()."""

    def test_all_none_optional_fields(self) -> None:
        outcome = suite.CheckOutcome(
            check_id="test", label="Test", cycle=1,
            exit_code=0, kill_reason=None,
            made_changes=False, lines_changed=None,
            change_pct=None, duration_seconds=0.0,
        )
        row = outcome.to_summary_dict()
        assert row["lines_changed"] is None
        assert row["change_pct"] is None
        assert row["kill_reason"] is None
        assert row["duration"] == "0m00s"

    def test_zero_duration(self) -> None:
        outcome = suite.CheckOutcome(
            check_id="t", label="T", cycle=1,
            exit_code=0, kill_reason=None,
            made_changes=False, lines_changed=0,
            change_pct=0.0, duration_seconds=0.0,
        )
        row = outcome.to_summary_dict()
        assert row["duration"] == "0m00s"

    def test_negative_duration(self) -> None:
        """Negative duration (clock skew) should be handled gracefully."""
        outcome = suite.CheckOutcome(
            check_id="t", label="T", cycle=1,
            exit_code=0, kill_reason=None,
            made_changes=False, lines_changed=0,
            change_pct=0.0, duration_seconds=-5.0,
        )
        row = outcome.to_summary_dict()
        assert row["duration"] == "0m00s"  # format_duration clamps negative to 0


# =============================================================================
# _build_check_prompt
# =============================================================================

class TestBuildCheckPromptEdgeCases:
    """Edge cases for _build_check_prompt."""

    def test_empty_changed_files_prefix_uses_full_scope(self) -> None:
        """When changed_files_prefix is empty string, full codebase scope is used."""
        from checkloop.checks import FULL_CODEBASE_SCOPE
        check = CheckDef(id="test", label="Test", prompt="review")
        args = make_suite_args(changed_files_prefix="")
        prompt = check_runner._build_check_prompt(check, args)
        assert prompt.startswith(FULL_CODEBASE_SCOPE)

    def test_changed_files_prefix_prepended(self) -> None:
        """When changed_files_prefix is set, it replaces the full scope prefix."""
        check = CheckDef(id="test", label="Test", prompt="review")
        args = make_suite_args(changed_files_prefix="ONLY THESE: a.py\n\n")
        prompt = check_runner._build_check_prompt(check, args)
        assert prompt.startswith("ONLY THESE: a.py")
        assert "review" in prompt

    def test_missing_changed_files_prefix_attr(self) -> None:
        """When args doesn't have changed_files_prefix, getattr defaults to ''."""
        from checkloop.checks import FULL_CODEBASE_SCOPE
        check = CheckDef(id="test", label="Test", prompt="review")
        args = make_suite_args()
        if hasattr(args, "changed_files_prefix"):
            delattr(args, "changed_files_prefix")
        prompt = check_runner._build_check_prompt(check, args)
        assert prompt.startswith(FULL_CODEBASE_SCOPE)


# =============================================================================
# Memory-kill feedback loop
# =============================================================================

class TestMemoryKillFeedbackLoop:
    """Tests for automatic memory-fix follow-up when a check is killed for OOM."""

    def test_memory_kill_triggers_fix_check(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When _invoke_claude returns kill_reason=KILL_REASON_MEMORY, _run_memory_fix is called."""
        check_def = CheckDef(id="readability", label="Readability", prompt="review code")
        args = make_suite_args(dry_run=False, max_memory_mb=4096)
        oom_result = CheckResult(exit_code=-9, kill_reason=process.KILL_REASON_MEMORY)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=oom_result), \
             mock.patch.object(check_runner, "_run_memory_fix") as mock_fix:
            check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_called_once_with("/tmp", args, False)

    def test_no_memory_fix_on_normal_exit(self) -> None:
        """_run_memory_fix is NOT called when the check exits normally."""
        check_def = CheckDef(id="readability", label="Readability", prompt="review code")
        args = make_suite_args(dry_run=False)
        ok_result = CheckResult(exit_code=0)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=ok_result), \
             mock.patch.object(check_runner, "_run_memory_fix") as mock_fix:
            check_runner._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_not_called()

    def test_memory_fix_invokes_claude_with_fix_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_run_memory_fix calls _invoke_claude with the memory fix prompt."""
        args = make_suite_args(dry_run=False, max_memory_mb=8192)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=CheckResult(exit_code=0)) as mock_invoke:
            check_runner._run_memory_fix("/tmp", args, is_git=False)
            prompt_used = mock_invoke.call_args[0][0]
            assert "8192MB limit exceeded" in prompt_used
            assert "excessive memory usage" in prompt_used

    def test_memory_fix_commits_when_git(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_run_memory_fix commits changes when is_git=True."""
        args = make_suite_args(dry_run=False, max_memory_mb=4096)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=CheckResult(exit_code=0)), \
             mock.patch.object(check_runner, "git_commit_all", return_value=True) as mock_commit:
            check_runner._run_memory_fix("/tmp", args, is_git=True)
            mock_commit.assert_called_once()
        out = capsys.readouterr().out
        assert "Committed memory-fix changes" in out

    def test_memory_fix_nonzero_exit_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When the memory-fix check exits non-zero, a warning is printed."""
        args = make_suite_args(dry_run=False, max_memory_mb=4096)

        with mock.patch.object(check_runner, "_invoke_claude", return_value=CheckResult(exit_code=1)):
            check_runner._run_memory_fix("/tmp", args, is_git=False)
        out = capsys.readouterr().out
        assert "did not complete cleanly" in out

    def test_memory_fix_exception_is_caught(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When _invoke_claude raises inside _run_memory_fix, the exception is caught and logged."""
        args = make_suite_args(dry_run=False, max_memory_mb=4096)

        with mock.patch.object(check_runner, "_invoke_claude", side_effect=RuntimeError("boom")):
            check_runner._run_memory_fix("/tmp", args, is_git=False)
        out = capsys.readouterr().out
        assert "Memory-fix follow-up failed" in out
