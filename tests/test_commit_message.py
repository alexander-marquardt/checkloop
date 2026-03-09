"""Tests for checkloop.commit_message — Claude-generated commit messages."""

from __future__ import annotations

import subprocess
from unittest import mock

from checkloop import commit_message


class TestGenerateCommitMessage:
    """Tests for generate_commit_message()."""

    def test_returns_claude_output(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="Fix a typo in README.\n", stderr="")
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message("diff content", "/tmp")
        assert msg == "Fix a typo in README."

    def test_returns_none_on_nonzero_exit(self) -> None:
        result = mock.MagicMock(returncode=1, stdout="", stderr="error")
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message("diff", "/tmp")
        assert msg is None

    def test_returns_none_on_timeout(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60)):
            msg = commit_message.generate_commit_message("diff", "/tmp")
        assert msg is None

    def test_returns_none_on_missing_binary(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            msg = commit_message.generate_commit_message("diff", "/tmp")
        assert msg is None

    def test_passes_skip_permissions_flag(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="msg", stderr="")
        with mock.patch("subprocess.run", return_value=result) as mock_run:
            commit_message.generate_commit_message("diff", "/tmp", skip_permissions=True)
            cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" in cmd

    def test_omits_skip_permissions_when_false(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="msg", stderr="")
        with mock.patch("subprocess.run", return_value=result) as mock_run:
            commit_message.generate_commit_message("diff", "/tmp", skip_permissions=False)
            cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" not in cmd

    def test_returns_none_on_empty_output(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="  \n  ", stderr="")
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message("diff", "/tmp")
        assert msg is None
