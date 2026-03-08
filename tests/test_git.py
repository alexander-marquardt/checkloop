"""Tests for checkloop.git — core git operations: run, repo detection, commit, squash, diff stats."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from checkloop import git


class TestGitRun:
    """Tests for _git_run()."""

    def test_empty_args(self) -> None:
        result = git._git_run("/tmp")
        assert result.returncode != 0

    def test_git_not_found(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(FileNotFoundError):
                git._git_run("/tmp", "status")

    def test_oserror_reraised(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("permission denied")):
            with pytest.raises(OSError):
                git._git_run("/tmp", "status")


class TestGitRunEdgeCases:
    """Edge case tests for _git_run()."""

    def test_git_not_installed(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(FileNotFoundError):
                git._git_run("/tmp", "status")

    def test_os_error(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("disk error")):
            with pytest.raises(OSError):
                git._git_run("/tmp", "status")


class TestIsGitRepo:
    """Tests for _is_git_repo() detection."""

    def test_true_when_git_succeeds(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0)
            assert git._is_git_repo("/tmp") is True

    def test_false_when_git_fails(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=128)
            assert git._is_git_repo("/tmp") is False


class TestIsGitRepoOSError:
    """Edge case tests for _is_git_repo() exception handling."""

    def test_oserror_returns_false(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            assert git._is_git_repo("/tmp") is False


class TestGitHeadSha:
    """Tests for _git_head_sha() SHA retrieval."""

    def test_returns_sha(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="abc123\n")
            assert git._git_head_sha("/tmp") == "abc123"

    def test_returns_none_on_failure(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=128, stdout="")
            assert git._git_head_sha("/tmp") is None


class TestGitHeadShaEdgeCases:
    """Edge case tests for _git_head_sha()."""

    def test_empty_stdout_returns_none(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="")
            assert git._git_head_sha("/tmp") is None

    def test_whitespace_only_stdout_returns_none(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="   \n  ")
            assert git._git_head_sha("/tmp") is None

    def test_oserror_returns_none(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            assert git._git_head_sha("/tmp") is None


class TestGitCommitAll:
    """Tests for _git_commit_all() post-cycle commit."""

    def test_commits_when_changes_exist(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=0),  # git add
                mock.MagicMock(returncode=1),  # git diff --cached --quiet (changes exist)
                mock.MagicMock(returncode=0),  # git commit
                mock.MagicMock(returncode=0, stdout="abc123\n"),  # git rev-parse HEAD
            ]
            assert git._git_commit_all("/tmp", "test commit") is True

    def test_no_commit_when_clean(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=0),  # git add
                mock.MagicMock(returncode=0),  # git diff --cached --quiet (no changes)
            ]
            assert git._git_commit_all("/tmp", "test commit") is False

    def test_returns_false_on_error(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert git._git_commit_all("/tmp", "test commit") is False

    def test_git_add_failure(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git add")):
            assert git._git_commit_all("/tmp", "test commit") is False

    def test_oserror_during_commit(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("disk full")):
            assert git._git_commit_all("/tmp", "test commit") is False


class TestGitSquashSince:
    """Tests for _git_squash_since() squash functionality."""

    def test_squash_creates_commit(self) -> None:
        """Happy path: commits exist after base_sha, squash succeeds."""
        with mock.patch.object(git, "_git_commit_all", return_value=True):
            with mock.patch.object(
                git, "_git_head_sha", side_effect=["new_sha_abc", "squashed_sha"]
            ):
                with mock.patch.object(git, "_git_run"):
                    result = git._git_squash_since("/tmp", "base_sha_123", "squash msg")
        assert result is True

    def test_squash_no_commits_since_base(self) -> None:
        """No new commits since base — returns False without squashing."""
        with mock.patch.object(git, "_git_commit_all", return_value=False):
            with mock.patch.object(git, "_git_head_sha", return_value="base_sha_123"):
                result = git._git_squash_since("/tmp", "base_sha_123", "squash msg")
        assert result is False

    def test_squash_called_process_error(self) -> None:
        """CalledProcessError during squash returns False."""
        with mock.patch.object(
            git,
            "_git_commit_all",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ):
            result = git._git_squash_since("/tmp", "base_sha", "msg")
        assert result is False

    def test_squash_oserror(self) -> None:
        """OSError during squash returns False."""
        with mock.patch.object(git, "_git_commit_all", side_effect=OSError("fail")):
            result = git._git_squash_since("/tmp", "base_sha", "msg")
        assert result is False


class TestParseShortstat:
    """Tests for _parse_shortstat() diff output parsing."""

    def test_insertions_and_deletions(self) -> None:
        text = " 3 files changed, 20 insertions(+), 10 deletions(-)"
        assert git._parse_shortstat(text) == 30

    def test_insertions_only(self) -> None:
        text = " 1 file changed, 5 insertions(+)"
        assert git._parse_shortstat(text) == 5

    def test_deletions_only(self) -> None:
        text = " 2 files changed, 8 deletions(-)"
        assert git._parse_shortstat(text) == 8

    def test_empty_string(self) -> None:
        assert git._parse_shortstat("") == 0

    def test_no_match(self) -> None:
        assert git._parse_shortstat("nothing here") == 0

    def test_zero_insertions_and_deletions(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 0 insertions(+), 0 deletions(-)") == 0


class TestParseShortstatEdgeCases:
    """Edge case tests for _parse_shortstat()."""

    def test_whitespace_only(self) -> None:
        assert git._parse_shortstat("   \n\t  ") == 0

    def test_only_files_changed(self) -> None:
        assert git._parse_shortstat(" 3 files changed") == 0

    def test_only_insertions(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 42 insertions(+)") == 42

    def test_only_deletions(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 10 deletions(-)") == 10

    def test_both_insertions_and_deletions(self) -> None:
        assert git._parse_shortstat(" 2 files changed, 10 insertions(+), 5 deletions(-)") == 15

    def test_large_numbers(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 999999 insertions(+), 888888 deletions(-)") == 1888887

    def test_singular_insertion(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 1 insertion(+)") == 1

    def test_singular_deletion(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 1 deletion(-)") == 1


class TestCountLinesChanged:
    """Tests for _count_lines_changed()."""

    def test_empty_base_sha_returns_zero(self) -> None:
        result = git._count_lines_changed("/tmp", "")
        assert result == 0

    def test_valid_sha_with_empty_target(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.MagicMock(
                returncode=0, stdout=" 1 file changed, 3 insertions(+)"
            )
            result = git._count_lines_changed("/tmp", "abc123", target="")
        assert result == 3
        call_args = mock_git.call_args[0]
        assert "abc123" in call_args
        assert "" not in call_args[2:]

    def test_git_diff_nonzero_returncode(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.MagicMock(
                returncode=128, stdout="", stderr="fatal: bad revision"
            )
            result = git._count_lines_changed("/tmp", "badref")
        assert result == 0

    def test_git_diff_oserror(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            result = git._count_lines_changed("/tmp", "abc123")
        assert result == 0
