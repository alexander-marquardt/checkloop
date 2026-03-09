"""Tests for checkloop.git — branch detection, changed files, and change stats."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from checkloop import git
from tests.helpers import make_git_result


class TestComputeChangeStats:
    """Tests for compute_change_stats() convergence metric."""

    def test_calculates_lines_and_percentage(self) -> None:
        resolved = str(Path("/tmp").resolve())
        git._total_lines_cache.pop(resolved, None)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_git_result(stdout=" 2 files changed, 10 insertions(+), 5 deletions(-)"),
                make_git_result(stdout=b"file1.py\0file2.py\0"),
            ]
            file_content = b"line\n" * 1000
            mock_open = mock.mock_open(read_data=file_content)
            with mock.patch("builtins.open", mock_open):
                lines, pct = git.compute_change_stats("/tmp", "abc123")
                assert lines == 15
                assert 0 < pct < 100
        git._total_lines_cache.pop(resolved, None)

    def test_zero_when_no_changes(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result()):
            lines, pct = git.compute_change_stats("/tmp", "abc123")
            assert lines == 0
            assert pct == 0.0

    def test_failed_git_diff_returns_zero(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.Mock(returncode=1, stderr="error")
            lines, pct = git.compute_change_stats("/tmp", "abc123")
            assert lines == 0
            assert pct == 0.0


class TestComputeChangeStatsEdgeCases:
    """Edge case tests for compute_change_stats()."""

    def test_zero_lines_changed(self) -> None:
        with mock.patch.object(git, "_count_lines_changed", return_value=0):
            lines, pct = git.compute_change_stats("/tmp", "abc123")
        assert lines == 0
        assert pct == 0.0

    def test_all_lines_changed(self) -> None:
        with mock.patch.object(git, "_count_lines_changed", return_value=1000):
            with mock.patch.object(git, "_cached_total_tracked_lines", return_value=1000):
                lines, pct = git.compute_change_stats("/tmp", "abc123")
        assert lines == 1000
        assert pct == 100.0

    def test_exception_returns_zero(self) -> None:
        """When _count_lines_changed raises, compute_change_stats returns (0, 0.0)."""
        with mock.patch.object(git, "_count_lines_changed", side_effect=RuntimeError("git broke")):
            lines, pct = git.compute_change_stats("/tmp", "abc123")
        assert lines == 0
        assert pct == 0.0


class TestDetectDefaultBranch:
    """Tests for detect_default_branch()."""

    def test_detect_default_branch_main(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result()):
            assert git.detect_default_branch("/tmp") == "main"

    def test_detect_default_branch_master(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                make_git_result(returncode=1),
                make_git_result(),
            ]
            assert git.detect_default_branch("/tmp") == "master"

    def test_detect_default_branch_fallback(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result(returncode=1)):
            assert git.detect_default_branch("/tmp") == "main"

    def test_oserror_on_both_branches_returns_main(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            assert git.detect_default_branch("/tmp") == "main"

    def test_oserror_on_main_then_master_found(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                OSError("no main"),
                make_git_result(),
            ]
            assert git.detect_default_branch("/tmp") == "master"


class TestGetChangedFiles:
    """Tests for get_changed_files()."""

    def test_get_changed_files(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                make_git_result(stdout="abc123"),
                make_git_result(stdout="src/a.py\nsrc/b.py\n"),
            ]
            files = git.get_changed_files("/tmp", "main")
            assert files == ["src/a.py", "src/b.py"]

    def test_get_changed_files_merge_base_fails(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result(returncode=1)):
            assert git.get_changed_files("/tmp", "main") == []

    def test_empty_diff_output(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=[
            make_git_result(stdout="abc123\n"),
            make_git_result(),
        ]):
            result = git.get_changed_files("/tmp", "main")
        assert result == []

    def test_merge_base_failure(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result(returncode=128)):
            result = git.get_changed_files("/tmp", "nonexistent")
        assert result == []

    def test_merge_base_oserror(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            assert git.get_changed_files("/tmp", "main") == []

    def test_diff_oserror(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                make_git_result(stdout="abc123"),
                OSError("diff failed"),
            ]
            assert git.get_changed_files("/tmp", "main") == []

    def test_diff_nonzero_returncode(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                make_git_result(stdout="abc123"),
                make_git_result(returncode=128),
            ]
            assert git.get_changed_files("/tmp", "main") == []

    def test_diff_output_with_blank_lines_filtered(self) -> None:
        """Blank lines in diff output are filtered out."""
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                make_git_result(stdout="abc123"),
                make_git_result(stdout="\n\nsrc/a.py\n\n\n"),
            ]
            files = git.get_changed_files("/tmp", "main")
        assert files == ["src/a.py"]

    def test_whitespace_only_merge_base_returns_empty(self) -> None:
        """Whitespace-only merge-base stdout produces empty SHA, so return empty list."""
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                make_git_result(stdout="   \n"),
            ]
            files = git.get_changed_files("/tmp", "main")
        assert files == []

    def test_empty_merge_base_stdout_returns_empty(self) -> None:
        """Empty merge-base stdout returns empty list instead of passing empty ref to git diff."""
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                make_git_result(stdout=""),
            ]
            files = git.get_changed_files("/tmp", "main")
        assert files == []


class TestBuildChangedFilesPrefix:
    """Tests for build_changed_files_prefix()."""

    def test_build_changed_files_prefix(self) -> None:
        prefix = git.build_changed_files_prefix(["src/a.py", "src/b.py"])
        assert "2 file(s)" in prefix
        assert "src/a.py" in prefix
        assert "src/b.py" in prefix
        assert "IMPORTANT" in prefix

    def test_single_file(self) -> None:
        result = git.build_changed_files_prefix(["src/main.py"])
        assert "1 file(s)" in result
        assert "src/main.py" in result

    def test_file_with_spaces(self) -> None:
        result = git.build_changed_files_prefix(["src/my file.py"])
        assert "my file.py" in result

    def test_file_with_unicode(self) -> None:
        result = git.build_changed_files_prefix(["src/日本語.py"])
        assert "日本語.py" in result

    def test_empty_list_returns_empty_string(self) -> None:
        result = git.build_changed_files_prefix([])
        assert result == ""


class TestBuildChangedFilesPrefixEdgeCases:
    """Edge cases for build_changed_files_prefix()."""

    def test_single_empty_string_filename(self) -> None:
        """An empty-string filename is unusual but should not crash."""
        result = git.build_changed_files_prefix([""])
        assert "1 file(s)" in result

    def test_very_long_filename(self) -> None:
        """A filename with 1000 characters should work."""
        long_name = "a" * 1000 + ".py"
        result = git.build_changed_files_prefix([long_name])
        assert long_name in result


# =============================================================================
# git._git_run — exception chaining
# =============================================================================


class TestGitRunExceptionChaining:
    """Verify that TimeoutExpired is chained as the cause of the raised OSError."""

    def test_timeout_exception_is_chained(self) -> None:
        """The OSError raised on timeout should have __cause__ set to TimeoutExpired."""
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git status", timeout=120),
        ):
            with pytest.raises(OSError) as exc_info:
                git._git_run("/tmp", "status")
            assert isinstance(exc_info.value.__cause__, subprocess.TimeoutExpired)

    def test_timeout_with_no_args(self) -> None:
        """_git_run with no args should still produce a readable error on timeout."""
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120),
        ):
            with pytest.raises(OSError, match="timed out"):
                git._git_run("/tmp")

    def test_timeout_error_message_includes_command(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git status", timeout=120),
        ):
            with pytest.raises(OSError, match="status timed out"):
                git._git_run("/tmp", "status")

    def test_timeout_error_message_empty_args(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120),
        ):
            with pytest.raises(OSError, match="timed out"):
                git._git_run("/tmp")


# =============================================================================
# git.compute_change_stats — boundary conditions
# =============================================================================


class TestComputeChangeStatsBoundary:
    """Boundary conditions for compute_change_stats."""

    def test_single_line_changed_in_large_repo(self) -> None:
        """One line changed in a 100k-line repo should give a very small percentage."""
        with mock.patch.object(git, "_count_lines_changed", return_value=1):
            with mock.patch.object(git, "_cached_total_tracked_lines", return_value=100000):
                lines, pct = git.compute_change_stats("/tmp", "abc123")
        assert lines == 1
        assert abs(pct - 0.001) < 0.0001

    def test_more_lines_changed_than_total(self) -> None:
        """Lines changed can exceed total (e.g., rewrite entire file plus add new lines)."""
        with mock.patch.object(git, "_count_lines_changed", return_value=2000):
            with mock.patch.object(git, "_cached_total_tracked_lines", return_value=1000):
                lines, pct = git.compute_change_stats("/tmp", "abc123")
        assert lines == 2000
        assert pct == 200.0


# =============================================================================
# git._count_lines_changed — edge cases
# =============================================================================


class TestCountLinesChangedEdgeCases:
    """Additional edge cases for _count_lines_changed."""

    def test_target_default_is_head(self) -> None:
        """Default target is 'HEAD' — verify it's appended to the diff command."""
        with mock.patch.object(git, "_git_run", return_value=mock.MagicMock(
            returncode=0, stdout=" 1 file changed, 5 insertions(+)",
        )) as mock_run:
            result = git._count_lines_changed("/tmp", "abc123")
        args = mock_run.call_args[0]
        assert "HEAD" in args

    def test_empty_target_omits_head(self) -> None:
        """Empty string target means diff against working tree — no 'HEAD' in args."""
        with mock.patch.object(git, "_git_run", return_value=mock.MagicMock(
            returncode=0, stdout=" 1 file changed, 3 insertions(+)",
        )) as mock_run:
            git._count_lines_changed("/tmp", "abc123", target="")
        args = mock_run.call_args[0]
        assert "HEAD" not in args[2:]


# =============================================================================
# git.build_changed_files_prefix — additional edge cases
# =============================================================================


class TestBuildChangedFilesPrefixNewEdgeCases:
    """Additional edge cases for build_changed_files_prefix."""

    def test_files_with_newlines_in_names(self) -> None:
        """File names containing newlines should not break the prefix format."""
        result = git.build_changed_files_prefix(["file\nname.py"])
        assert "1 file(s)" in result

    def test_many_files(self) -> None:
        """A large number of changed files should all appear in the prefix."""
        files = [f"src/file_{i}.py" for i in range(100)]
        result = git.build_changed_files_prefix(files)
        assert "100 file(s)" in result
        assert "file_99.py" in result


# =============================================================================
# git.detect_default_branch — additional edge cases
# =============================================================================


class TestDetectDefaultBranchEdgeCases:
    """Additional edge cases for detect_default_branch."""

    def test_main_check_returns_nonzero_then_master_oserror(self) -> None:
        """When main returns non-zero and master raises OSError, falls back to 'main'."""
        with mock.patch.object(git, "_git_run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=128),  # main doesn't exist
                OSError("git broke"),  # master check fails
            ]
            assert git.detect_default_branch("/tmp") == "main"


# =============================================================================
# git._count_tracked_lines — edge cases
# =============================================================================


class TestCountTrackedLinesEdgeCasesNew:
    """Additional edge cases for _count_tracked_lines."""

    def test_git_ls_files_nonzero_returncode_with_bytes_stderr(self) -> None:
        """When ls-files fails with bytes stderr, it should be decoded safely."""
        ls_result = mock.MagicMock(
            returncode=1,
            stderr=b"fatal: not a git repository\n",
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            result = git._count_tracked_lines("/tmp")
        assert result == 1

    def test_git_ls_files_with_none_stderr(self) -> None:
        """When stderr is None, it should be handled gracefully."""
        ls_result = mock.MagicMock(
            returncode=1,
            stderr=None,
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            result = git._count_tracked_lines("/tmp")
        assert result == 1
