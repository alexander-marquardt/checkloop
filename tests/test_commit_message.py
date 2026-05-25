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

    def test_check_id_prepended_when_llm_omits_prefix(self) -> None:
        """The LLM is asked to use the prefix, but if it forgets the wrapper
        must inject it so downstream bucketing can rely on the invariant."""
        result = mock.MagicMock(returncode=0, stdout="Tighten the validator for /api/posts.\n", stderr="")
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message(
                "diff", "/tmp", check_id="security",
            )
        assert msg == "[security] Tighten the validator for /api/posts."

    def test_check_id_left_alone_when_llm_already_prefixed(self) -> None:
        """If the LLM follows the prompt, the wrapper must not double-prefix."""
        result = mock.MagicMock(
            returncode=0,
            stdout="[security] Tighten the validator for /api/posts.\n",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message(
                "diff", "/tmp", check_id="security",
            )
        assert msg == "[security] Tighten the validator for /api/posts."

    def test_check_id_prompt_includes_prefix_instruction(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="[readability] Rename d to user_document.\n", stderr="")
        with mock.patch("subprocess.run", return_value=result) as mock_run:
            commit_message.generate_commit_message("diff", "/tmp", check_id="readability")
        prompt = mock_run.call_args.kwargs["input"]
        assert "[readability] " in prompt

    def test_no_check_id_means_no_prefix(self) -> None:
        result = mock.MagicMock(returncode=0, stdout="A plain message with no prefix.\n", stderr="")
        with mock.patch("subprocess.run", return_value=result):
            msg = commit_message.generate_commit_message("diff", "/tmp")
        assert msg == "A plain message with no prefix."

    def test_prompt_is_passed_via_stdin_not_argv(self) -> None:
        """Regression: a large diff used to be embedded in argv via -p <prompt>,
        which exec()'d with ``[Errno 7] Argument list too long`` once argv+envp
        crossed ``ARG_MAX`` (~256 KiB on macOS).  The diff must arrive on stdin,
        so argv stays bounded regardless of diff size."""
        big_diff = "x" * 600_000
        result = mock.MagicMock(returncode=0, stdout="Summarize the bulk update across the touched modules.", stderr="")
        with mock.patch("subprocess.run", return_value=result) as mock_run:
            commit_message.generate_commit_message(big_diff, "/tmp")
        argv = mock_run.call_args.args[0]
        stdin_payload = mock_run.call_args.kwargs["input"]
        assert big_diff in stdin_payload
        assert all(big_diff not in arg for arg in argv)
        # The argv size must not scale with the diff — pin a tight bound that
        # would catch any future regression that smuggles content back into it.
        assert sum(len(arg) for arg in argv) < 1024

    def test_stdin_payload_survives_shell_fallback(self) -> None:
        """When the direct exec raises FileNotFoundError (alias case), the
        retry through ``$SHELL -ic`` must still pipe the prompt on stdin —
        re-embedding it in the shell command line would re-introduce the
        ARG_MAX failure mode."""
        big_diff = "y" * 400_000
        ok = mock.MagicMock(returncode=0, stdout="Tighten the fallback handling for shell alias users.", stderr="")
        with mock.patch("subprocess.run", side_effect=[FileNotFoundError(), ok]) as mock_run:
            commit_message.generate_commit_message(big_diff, "/tmp")
        # Two calls: direct then shell -ic fallback.  Both must pipe via input=.
        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            argv = call.args[0]
            assert big_diff in call.kwargs["input"]
            assert all(big_diff not in arg for arg in argv)


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
