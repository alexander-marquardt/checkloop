"""Tests for checkloop.git — file and tracked-line counting."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from checkloop import git


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
        content = b"x" * (git._BINARY_CHECK_SIZE + 10)
        content = content[:git._BINARY_CHECK_SIZE + 5] + b"\0" + content[git._BINARY_CHECK_SIZE + 6:]
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
