"""Tests for checkloop.git — core git operations: run, repo detection, commit, diff stats."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from checkloop import git
from tests.helpers import make_git_result


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


class TestIsGitRepo:
    """Tests for is_git_repo() detection."""

    def test_true_when_git_succeeds(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result()):
            assert git.is_git_repo("/tmp") is True

    def test_false_when_git_fails(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=128)):
            assert git.is_git_repo("/tmp") is False

    def test_oserror_returns_false(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            assert git.is_git_repo("/tmp") is False


class TestGitHeadSha:
    """Tests for git_head_sha() SHA retrieval."""

    def test_returns_sha(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="abc123\n")):
            assert git.git_head_sha("/tmp") == "abc123"

    def test_returns_none_on_failure(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=128)):
            assert git.git_head_sha("/tmp") is None

    def test_empty_stdout_returns_none(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result()):
            assert git.git_head_sha("/tmp") is None

    def test_whitespace_only_stdout_returns_none(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="   \n  ")):
            assert git.git_head_sha("/tmp") is None

    def test_oserror_returns_none(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            assert git.git_head_sha("/tmp") is None


class TestGitCommitAll:
    """Tests for git_commit_all() post-cycle commit."""

    def test_commits_when_changes_exist(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_git_result(),                       # git add
                make_git_result(returncode=1),           # git diff --cached --quiet (changes exist)
                make_git_result(),                       # git commit
                make_git_result(stdout="abc123\n"),       # git rev-parse HEAD
            ]
            assert git.git_commit_all("/tmp", "test commit") is True

    def test_no_commit_when_clean(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_git_result(),  # git add
                make_git_result(),  # git diff --cached --quiet (no changes)
            ]
            assert git.git_commit_all("/tmp", "test commit") is False

    def test_returns_false_on_error(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert git.git_commit_all("/tmp", "test commit") is False

    def test_git_add_failure(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git add")):
            assert git.git_commit_all("/tmp", "test commit") is False

    def test_oserror_during_commit(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("disk full")):
            assert git.git_commit_all("/tmp", "test commit") is False


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

    def test_whitespace_only(self) -> None:
        assert git._parse_shortstat("   \n\t  ") == 0

    def test_only_files_changed(self) -> None:
        assert git._parse_shortstat(" 3 files changed") == 0

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
        with mock.patch.object(git, "_git_run", return_value=make_git_result(
            stdout=" 1 file changed, 3 insertions(+)"
        )) as mock_git:
            result = git._count_lines_changed("/tmp", "abc123", target="")
        assert result == 3
        call_args = mock_git.call_args[0]
        assert "abc123" in call_args
        assert "" not in call_args[2:]

    def test_git_diff_nonzero_returncode(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result(
            returncode=128, stderr="fatal: bad revision"
        )):
            result = git._count_lines_changed("/tmp", "badref")
        assert result == 0

    def test_git_diff_oserror(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            result = git._count_lines_changed("/tmp", "abc123")
        assert result == 0

    def test_none_base_sha_returns_zero(self) -> None:
        """None is falsy, so _count_lines_changed should return 0."""
        result = git._count_lines_changed("/tmp", None)  # type: ignore[arg-type]
        assert result == 0

    def test_whitespace_only_base_sha_proceeds(self) -> None:
        """Whitespace-only SHA is truthy, so git is called (and likely fails)."""
        with mock.patch.object(git, "_git_run", return_value=make_git_result(
            returncode=128, stderr="fatal: bad revision"
        )):
            result = git._count_lines_changed("/tmp", "   ")
        assert result == 0


class TestParseShortstatEdgeCases:
    """Additional edge case tests for _parse_shortstat()."""

    def test_multiple_insertion_matches_uses_first(self) -> None:
        """If text somehow has multiple insertion matches, first is used."""
        text = " 1 file changed, 5 insertions(+), 10 insertions(+)"
        assert git._parse_shortstat(text) == 5

    def test_very_large_insertion_count(self) -> None:
        """Ensure parsing handles very large numbers without overflow."""
        text = f" 1 file changed, {2**31} insertions(+)"
        assert git._parse_shortstat(text) == 2**31


class TestParseShortstatAdditional:
    """Additional edge cases for _parse_shortstat()."""

    def test_negative_number_before_insertion(self) -> None:
        """\\d+ matches digits after the minus sign (git never outputs negatives)."""
        assert git._parse_shortstat(" 1 file changed, -5 insertions(+)") == 5

    def test_decimal_number_partial_match(self) -> None:
        """'1.5 insertions' — regex matches '5' as the digits before ' insertion'."""
        result = git._parse_shortstat(" 1 file changed, 1.5 insertions(+)")
        assert result == 5

    def test_no_space_before_insertion(self) -> None:
        """Without a space before 'insertion', the regex might not match."""
        result = git._parse_shortstat(" 1 file changed,5 insertions(+)")
        assert result == 5
