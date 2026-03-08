"""Tests for checkloop.suite — memory-kill feedback loop and follow-up fixes."""

from __future__ import annotations

from unittest import mock

import pytest

from checkloop import suite, process
from checkloop.checks import CheckDef
from checkloop.process import CheckResult
from helpers import make_suite_args


class TestMemoryKillFeedbackLoop:
    """Tests for automatic memory-fix follow-up when a check is killed for OOM."""

    def test_memory_kill_triggers_fix_check(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When _invoke_claude returns kill_reason=KILL_REASON_MEMORY, _run_memory_fix is called."""
        check_def = CheckDef(id="readability", label="Readability", prompt="review code")
        args = make_suite_args(dry_run=False, max_memory_mb=4096)
        oom_result = CheckResult(exit_code=-9, kill_reason=process.KILL_REASON_MEMORY)

        with mock.patch.object(suite, "_invoke_claude", return_value=oom_result), \
             mock.patch.object(suite, "_run_memory_fix") as mock_fix, \
             mock.patch.object(suite, "is_git_repo", return_value=False):
            suite._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_called_once_with("/tmp", args, False)

    def test_no_memory_fix_on_normal_exit(self) -> None:
        """_run_memory_fix is NOT called when the check exits normally."""
        check_def = CheckDef(id="readability", label="Readability", prompt="review code")
        args = make_suite_args(dry_run=False)
        ok_result = CheckResult(exit_code=0)

        with mock.patch.object(suite, "_invoke_claude", return_value=ok_result), \
             mock.patch.object(suite, "_run_memory_fix") as mock_fix, \
             mock.patch.object(suite, "is_git_repo", return_value=False):
            suite._run_single_check(check_def, "/tmp", args, "[1/1]", is_git=False)
            mock_fix.assert_not_called()

    def test_memory_fix_invokes_claude_with_fix_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_run_memory_fix calls _invoke_claude with the memory fix prompt."""
        args = make_suite_args(dry_run=False, max_memory_mb=8192)

        with mock.patch.object(suite, "_invoke_claude", return_value=CheckResult(exit_code=0)) as mock_invoke:
            suite._run_memory_fix("/tmp", args, is_git=False)
            prompt_used = mock_invoke.call_args[0][0]
            assert "8192MB limit exceeded" in prompt_used
            assert "excessive memory usage" in prompt_used

    def test_memory_fix_commits_when_git(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_run_memory_fix commits changes when is_git=True."""
        args = make_suite_args(dry_run=False, max_memory_mb=4096)

        with mock.patch.object(suite, "_invoke_claude", return_value=CheckResult(exit_code=0)), \
             mock.patch.object(suite, "git_commit_all", return_value=True) as mock_commit:
            suite._run_memory_fix("/tmp", args, is_git=True)
            mock_commit.assert_called_once()
        out = capsys.readouterr().out
        assert "Committed memory-fix changes" in out

    def test_memory_fix_nonzero_exit_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When the memory-fix check exits non-zero, a warning is printed."""
        args = make_suite_args(dry_run=False, max_memory_mb=4096)

        with mock.patch.object(suite, "_invoke_claude", return_value=CheckResult(exit_code=1)):
            suite._run_memory_fix("/tmp", args, is_git=False)
        out = capsys.readouterr().out
        assert "did not complete cleanly" in out
