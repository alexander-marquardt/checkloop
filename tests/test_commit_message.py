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
        result = mock.MagicMock(returncode=0, stdout="Update the README with the new install section.", stderr="")
        with mock.patch("subprocess.run", return_value=result) as mock_run:
            commit_message.generate_commit_message("diff", "/tmp", skip_permissions=True)
            cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" in cmd

    def test_omits_skip_permissions_when_false(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="Update the README with the new install section.", stderr="")
        with mock.patch("subprocess.run", return_value=result) as mock_run:
            commit_message.generate_commit_message("diff", "/tmp", skip_permissions=False)
            cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" not in cmd

    def test_returns_none_on_empty_output(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="  \n  ", stderr="")
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message("diff", "/tmp")
        assert msg is None


class TestLooksLikeGarbage:
    """Tests for _looks_like_garbage() message validation."""

    def test_rejects_empty_diff_text(self) -> None:
        assert commit_message._looks_like_garbage(
            "The diff is empty — there are no uncommitted changes to describe.",
        )

    def test_rejects_no_changes_to_describe(self) -> None:
        assert commit_message._looks_like_garbage(
            "The diff provided is empty, so there are no changes to describe.",
        )

    def test_rejects_please_share_the_diff(self) -> None:
        assert commit_message._looks_like_garbage(
            "Please share the actual diff you'd like a commit message for.",
        )

    def test_rejects_could_you_share(self) -> None:
        assert commit_message._looks_like_garbage(
            "Could you share the actual diff? I can't write a message without it.",
        )

    def test_rejects_would_you_share(self) -> None:
        assert commit_message._looks_like_garbage(
            "Would you share the diff you'd like a commit message for?",
        )

    def test_rejects_sorry_prefix(self) -> None:
        assert commit_message._looks_like_garbage(
            "Sorry, I can't write a commit message without seeing the diff.",
        )

    def test_rejects_i_cant_prefix(self) -> None:
        assert commit_message._looks_like_garbage(
            "I can't generate a commit message from an empty diff, please retry.",
        )

    def test_rejects_too_short(self) -> None:
        assert commit_message._looks_like_garbage("Short.")

    def test_rejects_too_long(self) -> None:
        assert commit_message._looks_like_garbage("x" * 2001)

    def test_accepts_valid_message(self) -> None:
        assert not commit_message._looks_like_garbage(
            "Refactor the readability scorer to use explicit thresholds instead "
            "of magic numbers, improving test coverage in the process.",
        )

    def test_discards_garbage_in_generate(self) -> None:
        """End-to-end: generate_commit_message returns None when Claude emits garbage."""
        result = mock.MagicMock(
            returncode=0,
            stdout="The diff is empty — there are no uncommitted changes to describe.\n",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message("", "/tmp")
        assert msg is None
