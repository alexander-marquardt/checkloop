"""Tests for checkloop.git — git helpers and line counting."""

from __future__ import annotations

import subprocess
from pathlib import Path
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


class TestCountFileLines:
    """Tests for _count_file_lines()."""

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        assert git._count_file_lines(f) == 0

    def test_single_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "one.txt"
        f.write_bytes(b"\n")
        assert git._count_file_lines(f) == 1

    def test_no_trailing_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "no_newline.txt"
        f.write_bytes(b"hello")
        assert git._count_file_lines(f) == 0

    def test_binary_file_returns_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x00\x01\x02\n\n\n")
        assert git._count_file_lines(f) == 0

    def test_unicode_content(self, tmp_path: Path) -> None:
        f = tmp_path / "unicode.txt"
        f.write_bytes("こんにちは\nworld\n".encode("utf-8"))
        assert git._count_file_lines(f) == 2

    def test_mixed_line_endings(self, tmp_path: Path) -> None:
        f = tmp_path / "mixed.txt"
        f.write_bytes(b"line1\r\nline2\nline3\r\n")
        assert git._count_file_lines(f) == 3

    def test_null_byte_beyond_header(self, tmp_path: Path) -> None:
        f = tmp_path / "late_null.bin"
        content = b"x" * (git._READ_CHUNK_SIZE + 10)
        content = content[:git._READ_CHUNK_SIZE + 5] + b"\0" + content[git._READ_CHUNK_SIZE + 6:]
        f.write_bytes(content)
        assert git._count_file_lines(f) == 0


class TestCountFileLinesReadError:
    """Edge case tests for _count_file_lines() read errors."""

    def test_oserror_during_chunk_read(self, tmp_path: Path) -> None:
        f = tmp_path / "bad_read.txt"
        f.write_text("line1\nline2\n")
        with mock.patch("builtins.open", side_effect=OSError("permission denied")):
            assert git._count_file_lines(f) == 0


class TestCountFileLinesOSErrorDuringRead:
    """Test _count_file_lines when read fails after header succeeds."""

    def test_oserror_after_header(self, tmp_path: Path) -> None:
        f = tmp_path / "partial.txt"
        f.write_text("line1\nline2\n")

        original_open = open

        def patched_open(filepath, mode="r", **kwargs):
            fh = original_open(filepath, mode, **kwargs)
            original_read = fh.read

            call_count = 0
            def failing_read(size=-1):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return original_read(size)
                raise OSError("disk error during read")

            fh.read = failing_read
            return fh

        with mock.patch("builtins.open", side_effect=patched_open):
            result = git._count_file_lines(f)
        assert result == 0


class TestCountTrackedLines:
    """Tests for _count_tracked_lines() line counting."""

    def test_git_ls_files_failure_returns_1(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=mock.MagicMock(returncode=1)):
            assert git._count_tracked_lines("/tmp") == 1

    def test_skips_binary_files(self, tmp_path: Path) -> None:
        binary_file = tmp_path / "binary.dat"
        binary_file.write_bytes(b"\x00\x01\x02\x03")
        text_file = tmp_path / "hello.txt"
        text_file.write_text("line1\nline2\nline3\n")

        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"binary.dat\x00hello.txt\x00",
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            count = git._count_tracked_lines(str(tmp_path))
            assert count == 3

    def test_large_file_multi_chunk(self, tmp_path: Path) -> None:
        large_file = tmp_path / "big.txt"
        line = "x" * 100 + "\n"
        num_lines = 200
        large_file.write_text(line * num_lines)

        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"big.txt\x00",
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            count = git._count_tracked_lines(str(tmp_path))
            assert count == num_lines

    def test_empty_repo_returns_minimum_one(self) -> None:
        with mock.patch.object(git, "_git_run") as mock_git:
            mock_git.return_value = mock.Mock(returncode=0, stdout=b"")
            result = git._count_tracked_lines("/tmp")
            assert result == 1

    def test_oserror_on_file_open(self, tmp_path: Path) -> None:
        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"nonexistent.txt\x00",
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            count = git._count_tracked_lines(str(tmp_path))
            assert count == 1


class TestCountTrackedLinesEdgeCases:
    """Edge case tests for _count_tracked_lines()."""

    def test_no_tracked_files_returns_one(self) -> None:
        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b""
        with mock.patch.object(git, "_git_run", return_value=mock_result):
            assert git._count_tracked_lines("/tmp") == 1

    def test_git_ls_files_failure(self) -> None:
        mock_result = mock.MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = b""
        with mock.patch.object(git, "_git_run", return_value=mock_result):
            assert git._count_tracked_lines("/tmp") == 1


class TestCountTrackedLinesPathTraversal:
    """Test that path traversal is blocked in _count_tracked_lines."""

    def test_symlink_outside_workdir_skipped(self, tmp_path: Path) -> None:
        real_file = tmp_path / "real.txt"
        real_file.write_text("line1\nline2\n")

        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"real.txt\x00../../../etc/passwd\x00",
        )
        with mock.patch.object(git, "_git_run", return_value=ls_result):
            count = git._count_tracked_lines(str(tmp_path))
            assert count == 2


class TestCountTrackedLinesGitRunOSError:
    """Test _count_tracked_lines when _git_run raises OSError."""

    def test_git_run_oserror_returns_1(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            assert git._count_tracked_lines("/tmp") == 1


class TestCountTrackedLinesFileOSError:
    """Test _count_tracked_lines when resolving a file path raises OSError."""

    def test_oserror_on_resolve(self, tmp_path: Path) -> None:
        ls_result = mock.MagicMock(returncode=0, stdout=b"good.txt\x00")
        (tmp_path / "good.txt").write_text("line\n")

        with mock.patch.object(git, "_git_run", return_value=ls_result):
            original_resolve = Path.resolve

            def failing_resolve(self, *args, **kwargs):
                if self.name == "good.txt" and "good.txt" in str(self):
                    raise OSError("permission denied")
                return original_resolve(self, *args, **kwargs)

            with mock.patch.object(Path, "resolve", failing_resolve):
                result = git._count_tracked_lines(str(tmp_path))
        assert result == 1


class TestCachedTotalTrackedLines:
    """Tests for _cached_total_tracked_lines() caching."""

    def test_cache_hit_skips_line_count(self) -> None:
        resolved = str(Path("/tmp").resolve())
        git._total_lines_cache[resolved] = 500
        try:
            with mock.patch.object(git, "_count_tracked_lines") as mock_count:
                result = git._cached_total_tracked_lines("/tmp")
                mock_count.assert_not_called()
                assert result == 500
        finally:
            git._total_lines_cache.pop(resolved, None)


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
