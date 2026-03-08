"""Tests for checkloop.git — branch detection, changed files, and change stats."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from checkloop import git


class TestComputeChangeStats:
    """Tests for _compute_change_stats() convergence metric."""

    def test_calculates_lines_and_percentage(self) -> None:
        resolved = str(Path("/tmp").resolve())
        git._total_lines_cache.pop(resolved, None)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=0, stdout=" 2 files changed, 10 insertions(+), 5 deletions(-)"),
                mock.MagicMock(returncode=0, stdout=b"file1.py\0file2.py\0"),
            ]
            file_content = b"line\n" * 1000
            mock_open = mock.mock_open(read_data=file_content)
            with mock.patch("builtins.open", mock_open):
                lines, pct = git._compute_change_stats("/tmp", "abc123")
                assert lines == 15
                assert 0 < pct < 100
        git._total_lines_cache.pop(resolved, None)

    def test_zero_when_no_changes(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="")
            lines, pct = git._compute_change_stats("/tmp", "abc123")
            assert lines == 0
            assert pct == 0.0

    def test_failed_git_diff_returns_zero(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.Mock(returncode=1, stderr="error")
            lines, pct = git._compute_change_stats("/tmp", "abc123")
            assert lines == 0
            assert pct == 0.0


class TestComputeChangeStatsEdgeCases:
    """Edge case tests for _compute_change_stats()."""

    def test_zero_lines_changed(self) -> None:
        with mock.patch.object(git, "_count_lines_changed", return_value=0):
            lines, pct = git._compute_change_stats("/tmp", "abc123")
        assert lines == 0
        assert pct == 0.0

    def test_all_lines_changed(self) -> None:
        with mock.patch.object(git, "_count_lines_changed", return_value=1000):
            with mock.patch.object(git, "_cached_total_tracked_lines", return_value=1000):
                lines, pct = git._compute_change_stats("/tmp", "abc123")
        assert lines == 1000
        assert pct == 100.0


class TestDetectDefaultBranch:
    """Tests for _detect_default_branch()."""

    def test_detect_default_branch_main(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.MagicMock(returncode=0)
            assert git._detect_default_branch("/tmp") == "main"

    def test_detect_default_branch_master(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                mock.MagicMock(returncode=1),
                mock.MagicMock(returncode=0),
            ]
            assert git._detect_default_branch("/tmp") == "master"

    def test_detect_default_branch_fallback(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.MagicMock(returncode=1)
            assert git._detect_default_branch("/tmp") == "main"

    def test_oserror_on_both_branches_returns_main(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            assert git._detect_default_branch("/tmp") == "main"

    def test_oserror_on_main_then_master_found(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                OSError("no main"),
                mock.MagicMock(returncode=0),
            ]
            assert git._detect_default_branch("/tmp") == "master"


class TestGetChangedFiles:
    """Tests for _get_changed_files()."""

    def test_get_changed_files(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                mock.MagicMock(returncode=0, stdout="abc123"),
                mock.MagicMock(returncode=0, stdout="src/a.py\nsrc/b.py\n"),
            ]
            files = git._get_changed_files("/tmp", "main")
            assert files == ["src/a.py", "src/b.py"]

    def test_get_changed_files_merge_base_fails(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.MagicMock(returncode=1)
            assert git._get_changed_files("/tmp", "main") == []

    def test_empty_diff_output(self) -> None:
        merge_base_result = mock.MagicMock(returncode=0, stdout="abc123\n")
        diff_result = mock.MagicMock(returncode=0, stdout="")
        with mock.patch.object(git, "_git_run", side_effect=[merge_base_result, diff_result]):
            result = git._get_changed_files("/tmp", "main")
        assert result == []

    def test_merge_base_failure(self) -> None:
        merge_base_result = mock.MagicMock(returncode=128, stdout="", stderr="")
        with mock.patch.object(git, "_git_run", return_value=merge_base_result):
            result = git._get_changed_files("/tmp", "nonexistent")
        assert result == []

    def test_merge_base_oserror(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            assert git._get_changed_files("/tmp", "main") == []

    def test_diff_oserror(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                mock.MagicMock(returncode=0, stdout="abc123"),
                OSError("diff failed"),
            ]
            assert git._get_changed_files("/tmp", "main") == []

    def test_diff_nonzero_returncode(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.side_effect = [
                mock.MagicMock(returncode=0, stdout="abc123"),
                mock.MagicMock(returncode=128, stdout=""),
            ]
            assert git._get_changed_files("/tmp", "main") == []


class TestBuildChangedFilesPrefix:
    """Tests for _build_changed_files_prefix()."""

    def test_build_changed_files_prefix(self) -> None:
        prefix = git._build_changed_files_prefix(["src/a.py", "src/b.py"])
        assert "2 file(s)" in prefix
        assert "src/a.py" in prefix
        assert "src/b.py" in prefix
        assert "IMPORTANT" in prefix

    def test_single_file(self) -> None:
        result = git._build_changed_files_prefix(["src/main.py"])
        assert "1 file(s)" in result
        assert "src/main.py" in result

    def test_file_with_spaces(self) -> None:
        result = git._build_changed_files_prefix(["src/my file.py"])
        assert "my file.py" in result

    def test_file_with_unicode(self) -> None:
        result = git._build_changed_files_prefix(["src/日本語.py"])
        assert "日本語.py" in result

    def test_empty_list_produces_zero_files(self) -> None:
        result = git._build_changed_files_prefix([])
        assert "0 file(s)" in result
