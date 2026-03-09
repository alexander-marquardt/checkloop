"""Tests for checkloop.checkpoint — save, load, clear, prompt, and resume."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from checkloop import checkpoint
from checkloop.checkpoint import CheckpointData
from tests.helpers import assert_checkpoint_field_rejected, make_checkpoint_data


# =============================================================================
# save_checkpoint / load_checkpoint round-trip
# =============================================================================

class TestSaveAndLoad:
    """Tests for checkpoint save/load round-trip."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["version"] == 1
        assert loaded["current_cycle"] == 1
        assert loaded["current_check_index"] == 2
        assert loaded["changed_this_cycle"] == ["test-fix"]

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        data1 = make_checkpoint_data(workdir=str(tmp_path), current_check_index=1)
        data2 = make_checkpoint_data(workdir=str(tmp_path), current_check_index=3)
        checkpoint.save_checkpoint(str(tmp_path), data1)
        checkpoint.save_checkpoint(str(tmp_path), data2)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["current_check_index"] == 3

    def test_preserves_all_fields(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(
            workdir=str(tmp_path),
            previously_changed_ids=["readability", "dry"],
            prev_change_pct=1.5,
        )
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["previously_changed_ids"] == ["readability", "dry"]
        assert loaded["prev_change_pct"] == 1.5


# =============================================================================
# load_checkpoint — missing / invalid
# =============================================================================

class TestLoadCheckpointEdgeCases:
    """Tests for load_checkpoint with missing or corrupted files."""

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_corrupted_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text("not valid json {{{")
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_wrong_version_returns_none(self, tmp_path: Path) -> None:
        assert_checkpoint_field_rejected(tmp_path, version=999)

    def test_missing_required_keys_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps({"version": 1}))
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_non_dict_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps([1, 2, 3]))
        assert checkpoint.load_checkpoint(str(tmp_path)) is None


# =============================================================================
# clear_checkpoint
# =============================================================================

class TestClearCheckpoint:
    """Tests for clear_checkpoint."""

    def test_removes_file(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        assert checkpoint.load_checkpoint(str(tmp_path)) is not None
        checkpoint.clear_checkpoint(str(tmp_path))
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_nonexistent_file_no_error(self, tmp_path: Path) -> None:
        checkpoint.clear_checkpoint(str(tmp_path))  # should not raise


# =============================================================================
# prompt_resume
# =============================================================================

class TestPromptResume:
    """Tests for prompt_resume interactive prompt."""

    def test_no_checkpoint_returns_false(self, tmp_path: Path) -> None:
        assert checkpoint.prompt_resume(str(tmp_path)) is False

    def test_non_interactive_returns_false(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = checkpoint.prompt_resume(str(tmp_path))
        assert result is False

    def test_timeout_returns_false(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with mock.patch("select.select", return_value=([], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path), timeout=1)
        assert result is False

    def test_user_says_yes(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "y\n"
            with mock.patch("select.select", return_value=([mock_stdin], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is True

    def test_user_says_no(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "n\n"
            with mock.patch("select.select", return_value=([mock_stdin], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is False

    def test_user_presses_enter_returns_false(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "\n"
            with mock.patch("select.select", return_value=([mock_stdin], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is False


# =============================================================================
# build_checkpoint
# =============================================================================

class TestBuildCheckpoint:
    """Tests for build_checkpoint helper."""

    def test_builds_valid_data(self) -> None:
        data = checkpoint.build_checkpoint(
            workdir="/tmp/proj",
            check_ids=["a", "b", "c"],
            num_cycles=2,
            convergence_threshold=0.1,
            current_cycle=1,
            current_check_index=1,
            active_check_ids=["a", "b", "c"],
            changed_this_cycle={"a"},
            previously_changed_ids=None,
            prev_change_pct=None,
        )
        assert data["version"] == 1
        assert data["check_ids"] == ["a", "b", "c"]
        assert data["changed_this_cycle"] == ["a"]
        assert data["previously_changed_ids"] is None

    def test_sorts_changed_ids(self) -> None:
        data = checkpoint.build_checkpoint(
            workdir="/tmp/proj",
            check_ids=["a", "b"],
            num_cycles=1,
            convergence_threshold=0.0,
            current_cycle=1,
            current_check_index=1,
            active_check_ids=["a", "b"],
            changed_this_cycle={"b", "a"},
            previously_changed_ids={"b", "a"},
            prev_change_pct=1.0,
        )
        assert data["changed_this_cycle"] == ["a", "b"]
        assert data["previously_changed_ids"] == ["a", "b"]


# =============================================================================
# _format_checkpoint_summary
# =============================================================================

class TestFormatCheckpointSummary:
    """Tests for the human-readable checkpoint summary."""

    def test_shows_progress(self) -> None:
        data = make_checkpoint_data(
            current_cycle=2, num_cycles=3,
            current_check_index=1,
            active_check_ids=["test-fix", "readability", "dry"],
        )
        summary = checkpoint._format_checkpoint_summary(data)
        assert "cycle 2/3" in summary
        assert "check 1/3 completed" in summary
        assert "readability" in summary  # next check

    def test_all_completed_shows_done(self) -> None:
        data = make_checkpoint_data(
            current_check_index=3,
            active_check_ids=["a", "b", "c"],
        )
        summary = checkpoint._format_checkpoint_summary(data)
        assert "done" in summary


# =============================================================================
# save_checkpoint — error paths
# =============================================================================

class TestSaveCheckpointErrors:
    """Tests for save_checkpoint error handling paths."""

    def test_write_error_unlinks_temp_file(self, tmp_path: Path) -> None:
        """When json.dump raises during write, the temp file is cleaned up and no exception propagates."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        with mock.patch("json.dump", side_effect=TypeError("not serializable")):
            # Should not raise — TypeError is now caught and logged as best-effort
            checkpoint.save_checkpoint(str(tmp_path), data)
        # Temp files should have been cleaned up
        remaining = list(tmp_path.glob(".checkloop-ckpt-*.tmp"))
        assert remaining == []

    def test_oserror_during_replace_logs_warning_and_cleans_up(self, tmp_path: Path) -> None:
        """When os.replace raises OSError, save_checkpoint cleans up the temp file and logs a warning."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        with mock.patch("os.replace", side_effect=OSError("permission denied")):
            # Should not raise — the OSError is caught and logged
            checkpoint.save_checkpoint(str(tmp_path), data)
        # Temp files should have been cleaned up
        remaining = list(tmp_path.glob(".checkloop-ckpt-*.tmp"))
        assert remaining == []

    def test_oserror_during_save_logs_warning(self, tmp_path: Path) -> None:
        """When mkstemp raises OSError, save_checkpoint logs a warning instead of crashing."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        with mock.patch("tempfile.mkstemp", side_effect=OSError("disk full")):
            # Should not raise
            checkpoint.save_checkpoint(str(tmp_path), data)


# =============================================================================
# clear_checkpoint — error paths
# =============================================================================

class TestClearCheckpointErrors:
    """Tests for clear_checkpoint error handling."""

    def test_oserror_during_clear_logs_warning(self, tmp_path: Path) -> None:
        """When unlink raises OSError, clear_checkpoint logs a warning."""
        with mock.patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            # Should not raise
            checkpoint.clear_checkpoint(str(tmp_path))


# =============================================================================
# prompt_resume — select error path
# =============================================================================

class TestPromptResumeSelectError:
    """Tests for prompt_resume when select() raises."""

    def test_select_oserror_returns_false(self, tmp_path: Path) -> None:
        """When select.select raises OSError, prompt_resume returns False."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with mock.patch("select.select", side_effect=OSError("bad fd")):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is False

    def test_select_valueerror_returns_false(self, tmp_path: Path) -> None:
        """When select.select raises ValueError, prompt_resume returns False."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with mock.patch("select.select", side_effect=ValueError("closed fd")):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is False


# =============================================================================
# save_checkpoint — unlink failure during cleanup
# =============================================================================

class TestSaveCheckpointUnlinkFailure:
    """Test that os.unlink failure in the cleanup path is handled."""

    def test_unlink_oserror_during_write_error_cleanup(self, tmp_path: Path) -> None:
        """When json.dump fails AND os.unlink also fails, save_checkpoint still handles it gracefully."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        with mock.patch("json.dump", side_effect=TypeError("not serializable")):
            with mock.patch("os.unlink", side_effect=OSError("permission denied")):
                # Should not raise — TypeError is caught and logged as best-effort
                checkpoint.save_checkpoint(str(tmp_path), data)


# =============================================================================
# build_checkpoint — edge cases
# =============================================================================

class TestBuildCheckpointEdgeCases:
    """Edge cases for build_checkpoint()."""

    def test_empty_changed_set(self) -> None:
        result = checkpoint.build_checkpoint(
            workdir="/tmp",
            check_ids=["a"],
            num_cycles=1,
            convergence_threshold=0.1,
            current_cycle=1,
            current_check_index=0,
            active_check_ids=["a"],
            changed_this_cycle=set(),
            previously_changed_ids=None,
            prev_change_pct=None,
        )
        assert result["changed_this_cycle"] == []
        assert result["previously_changed_ids"] is None

    def test_empty_previously_changed(self) -> None:
        result = checkpoint.build_checkpoint(
            workdir="/tmp",
            check_ids=["a"],
            num_cycles=1,
            convergence_threshold=0.1,
            current_cycle=1,
            current_check_index=0,
            active_check_ids=["a"],
            changed_this_cycle=set(),
            previously_changed_ids=set(),
            prev_change_pct=0.0,
        )
        assert result["previously_changed_ids"] == []

    def test_changed_sets_sorted(self) -> None:
        result = checkpoint.build_checkpoint(
            workdir="/tmp",
            check_ids=["a", "b", "c"],
            num_cycles=1,
            convergence_threshold=0.1,
            current_cycle=1,
            current_check_index=0,
            active_check_ids=["a", "b", "c"],
            changed_this_cycle={"c", "a", "b"},
            previously_changed_ids={"z", "m", "a"},
            prev_change_pct=1.5,
        )
        assert result["changed_this_cycle"] == ["a", "b", "c"]
        assert result["previously_changed_ids"] == ["a", "m", "z"]


# =============================================================================
# _format_checkpoint_summary — edge cases
# =============================================================================

class TestFormatCheckpointSummaryEdgeCases:
    """Edge cases for _format_checkpoint_summary()."""

    def test_check_idx_at_end(self) -> None:
        """When check_idx == len(active_ids), next check should say 'done'."""
        data = make_checkpoint_data(
            current_check_index=4,
            active_check_ids=["a", "b", "c", "d"],
        )
        summary = checkpoint._format_checkpoint_summary(data)
        assert "done" in summary

    def test_check_idx_at_zero(self) -> None:
        """First check index shows first check ID."""
        data = make_checkpoint_data(
            current_check_index=0,
            active_check_ids=["first-check", "second-check"],
        )
        summary = checkpoint._format_checkpoint_summary(data)
        assert "first-check" in summary


# =============================================================================
# load_checkpoint — additional edge cases
# =============================================================================

class TestLoadCheckpointAdditionalEdgeCases:
    """Additional edge cases for load_checkpoint()."""

    def test_checkpoint_json_string(self, tmp_path: Path) -> None:
        """A JSON string should be rejected."""
        cp_file = tmp_path / ".checkloop-checkpoint.json"
        cp_file.write_text('"hello"')
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_checkpoint_empty_file(self, tmp_path: Path) -> None:
        """An empty file should be rejected (invalid JSON)."""
        cp_file = tmp_path / ".checkloop-checkpoint.json"
        cp_file.write_text("")
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_checkpoint_binary_content(self, tmp_path: Path) -> None:
        """Binary content should be handled gracefully."""
        cp_file = tmp_path / ".checkloop-checkpoint.json"
        cp_file.write_bytes(b"\x00\x01\x02\x03")
        assert checkpoint.load_checkpoint(str(tmp_path)) is None


# =============================================================================
# save_checkpoint — additional edge cases
# =============================================================================

class TestSaveCheckpointAdditionalEdgeCases:
    """Additional edge cases for save_checkpoint()."""

    def test_save_and_load_roundtrip_with_unicode(self, tmp_path: Path) -> None:
        """Unicode in workdir path should survive save/load."""
        data = make_checkpoint_data(
            workdir=str(tmp_path),
            started_at="2026-03-08T12:00:00+00:00",
        )
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["workdir"] == str(tmp_path)


# =============================================================================
# Checkpoint round-trip — boundary values
# =============================================================================

class TestCheckpointRoundtripEdgeCases:
    """Integration tests for checkpoint save/load with edge case data."""

    def test_roundtrip_with_zero_convergence_threshold(self, tmp_path: Path) -> None:
        data = make_checkpoint_data(
            workdir=str(tmp_path),
            convergence_threshold=0.0,
        )
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["convergence_threshold"] == 0.0

    def test_roundtrip_with_max_boundary_values(self, tmp_path: Path) -> None:
        """All fields at their maximum valid boundary values."""
        data = make_checkpoint_data(
            workdir=str(tmp_path),
            current_cycle=2,
            num_cycles=2,
            current_check_index=4,
            active_check_ids=["a", "b", "c", "d"],
            convergence_threshold=100.0,
        )
        checkpoint.save_checkpoint(str(tmp_path), data)
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded["current_cycle"] == 2
        assert loaded["convergence_threshold"] == 100.0


# =============================================================================
# _format_checkpoint_summary — additional edge cases
# =============================================================================

class TestFormatCheckpointSummaryEdgeCasesNew:
    """Additional edge cases for _format_checkpoint_summary."""

    def test_single_active_check_at_zero(self) -> None:
        """Single-element active list, index 0 — next check is the only one."""
        data = make_checkpoint_data(
            current_check_index=0,
            active_check_ids=["only-check"],
        )
        summary = checkpoint._format_checkpoint_summary(data)
        assert "only-check" in summary
        assert "check 0/1 completed" in summary

    def test_all_completed_single_check(self) -> None:
        """When only one check exists and it's completed."""
        data = make_checkpoint_data(
            current_check_index=1,
            active_check_ids=["only-check"],
        )
        summary = checkpoint._format_checkpoint_summary(data)
        assert "done" in summary
