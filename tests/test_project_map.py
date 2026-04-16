"""Tests for project_map module: generation, caching, staleness detection, and prompt injection."""

from __future__ import annotations

import os
import subprocess
from unittest import mock

import pytest

from checkloop import project_map


# --- Fingerprint computation ---------------------------------------------------

class TestComputeFileTreeFingerprint:

    def test_returns_hex_hash(self) -> None:
        git_output = "src/main.py\nsrc/util.py\ntests/test_main.py\n"
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=git_output,
            )
            fp = project_map._compute_file_tree_fingerprint("/tmp")
        assert fp is not None
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_deterministic_regardless_of_order(self) -> None:
        """Fingerprint should be the same even if git ls-files returns files in different order."""
        files_a = "b.py\na.py\nc.py\n"
        files_b = "c.py\na.py\nb.py\n"
        results = []
        for output in (files_a, files_b):
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=output,
                )
                results.append(project_map._compute_file_tree_fingerprint("/tmp"))
        assert results[0] == results[1]

    def test_returns_none_on_git_failure(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=128, stdout="",
            )
            assert project_map._compute_file_tree_fingerprint("/tmp") is None

    def test_returns_none_on_timeout(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
            assert project_map._compute_file_tree_fingerprint("/tmp") is None


# --- Cache read/write ----------------------------------------------------------

class TestReadCachedMap:

    def test_reads_valid_file(self, tmp_path: pytest.TempPathFactory) -> None:
        map_file = tmp_path / project_map.MAP_FILENAME
        map_file.write_text(
            "<!-- checkloop-fingerprint: abc123def456 -->\nThis is the map body.\nLine 2.\n"
        )
        fp, body = project_map._read_cached_map(str(tmp_path))
        assert fp == "abc123def456"
        assert "This is the map body." in body
        assert "Line 2." in body

    def test_returns_none_for_missing_file(self, tmp_path: pytest.TempPathFactory) -> None:
        fp, body = project_map._read_cached_map(str(tmp_path))
        assert fp is None
        assert body is None

    def test_returns_none_for_malformed_header(self, tmp_path: pytest.TempPathFactory) -> None:
        map_file = tmp_path / project_map.MAP_FILENAME
        map_file.write_text("No fingerprint here.\nJust text.\n")
        fp, body = project_map._read_cached_map(str(tmp_path))
        assert fp is None
        assert body is None

    def test_returns_none_for_empty_file(self, tmp_path: pytest.TempPathFactory) -> None:
        map_file = tmp_path / project_map.MAP_FILENAME
        map_file.write_text("")
        fp, body = project_map._read_cached_map(str(tmp_path))
        assert fp is None
        assert body is None


class TestSaveMap:

    def test_writes_file_with_fingerprint_header(self, tmp_path: pytest.TempPathFactory) -> None:
        project_map._save_map(str(tmp_path), "aabbccdd", "Map body here.")
        map_file = tmp_path / project_map.MAP_FILENAME
        content = map_file.read_text()
        assert content.startswith("<!-- checkloop-fingerprint: aabbccdd -->")
        assert "Map body here." in content

    def test_roundtrips_through_read(self, tmp_path: pytest.TempPathFactory) -> None:
        project_map._save_map(str(tmp_path), "1234abcd", "Body text.")
        fp, body = project_map._read_cached_map(str(tmp_path))
        assert fp == "1234abcd"
        assert body == "Body text."


# --- ensure_project_map -------------------------------------------------------

class TestEnsureProjectMap:

    def test_returns_cached_when_fingerprint_matches(self, tmp_path: pytest.TempPathFactory) -> None:
        project_map._save_map(str(tmp_path), "aabb1122", "Cached map.")
        with mock.patch.object(project_map, "_compute_file_tree_fingerprint", return_value="aabb1122"):
            result = ensure_project_map_wrapper(str(tmp_path))
        assert result == "Cached map."

    def test_regenerates_when_fingerprint_differs(self, tmp_path: pytest.TempPathFactory) -> None:
        project_map._save_map(str(tmp_path), "aa11bb22cc33dd44", "Old map.")
        with (
            mock.patch.object(project_map, "_compute_file_tree_fingerprint", return_value="ee55ff66aa77bb88"),
            mock.patch.object(project_map, "_generate_map", return_value="New map."),
        ):
            result = ensure_project_map_wrapper(str(tmp_path))
        assert result == "New map."
        # Verify it was saved with the new fingerprint.
        fp, body = project_map._read_cached_map(str(tmp_path))
        assert fp == "ee55ff66aa77bb88"
        assert body == "New map."

    def test_generates_when_no_cache_exists(self, tmp_path: pytest.TempPathFactory) -> None:
        with (
            mock.patch.object(project_map, "_compute_file_tree_fingerprint", return_value="1234567890abcdef"),
            mock.patch.object(project_map, "_generate_map", return_value="Fresh map."),
        ):
            result = ensure_project_map_wrapper(str(tmp_path))
        assert result == "Fresh map."

    def test_returns_stale_cache_when_generation_fails(self, tmp_path: pytest.TempPathFactory) -> None:
        project_map._save_map(str(tmp_path), "ccddee0011223344", "Stale map.")
        with (
            mock.patch.object(project_map, "_compute_file_tree_fingerprint", return_value="5566778899aabbcc"),
            mock.patch.object(project_map, "_generate_map", return_value=None),
        ):
            result = ensure_project_map_wrapper(str(tmp_path))
        assert result == "Stale map."

    def test_returns_empty_when_not_git_repo(self, tmp_path: pytest.TempPathFactory) -> None:
        with mock.patch.object(project_map, "_compute_file_tree_fingerprint", return_value=None):
            result = ensure_project_map_wrapper(str(tmp_path))
        assert result == ""

    def test_returns_empty_when_generation_fails_and_no_cache(self, tmp_path: pytest.TempPathFactory) -> None:
        with (
            mock.patch.object(project_map, "_compute_file_tree_fingerprint", return_value="1234567890abcdef"),
            mock.patch.object(project_map, "_generate_map", return_value=None),
        ):
            result = ensure_project_map_wrapper(str(tmp_path))
        assert result == ""


def ensure_project_map_wrapper(workdir: str) -> str:
    """Call ensure_project_map with default kwargs."""
    return project_map.ensure_project_map(workdir)


# --- load_project_map ----------------------------------------------------------

class TestLoadProjectMap:

    def test_loads_existing_map(self, tmp_path: pytest.TempPathFactory) -> None:
        project_map._save_map(str(tmp_path), "aabb112233445566", "The map.")
        assert project_map.load_project_map(str(tmp_path)) == "The map."

    def test_returns_empty_when_missing(self, tmp_path: pytest.TempPathFactory) -> None:
        assert project_map.load_project_map(str(tmp_path)) == ""


# --- Prompt injection ----------------------------------------------------------

class TestPromptInjection:

    def test_map_injected_into_prompt(self) -> None:
        from checkloop.check_runner import _build_check_prompt
        from checkloop.checks import CheckDef

        check: CheckDef = {"id": "test", "label": "Test", "prompt": "Do the thing."}
        args = mock.MagicMock()
        args.changed_files_prefix = ""
        args.project_map = "This is the project structure."

        prompt = _build_check_prompt(check, args)
        assert "This is the project structure." in prompt
        assert "Do the thing." in prompt

    def test_no_map_section_when_empty(self) -> None:
        from checkloop.check_runner import _build_check_prompt
        from checkloop.checks import CheckDef

        check: CheckDef = {"id": "test", "label": "Test", "prompt": "Do the thing."}
        args = mock.MagicMock()
        args.changed_files_prefix = ""
        args.project_map = ""

        prompt = _build_check_prompt(check, args)
        assert "project's structure" not in prompt
        assert "Do the thing." in prompt
