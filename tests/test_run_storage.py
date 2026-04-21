"""Tests for checkloop.run_storage — per-run debug directory management."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

import pytest

from checkloop import run_storage


@pytest.fixture(autouse=True)
def _isolate_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CHECKLOOP_STATE_HOME at a fresh tmp dir for every test."""
    monkeypatch.setenv("CHECKLOOP_STATE_HOME", str(tmp_path))


class TestGetRunsRoot:
    """Tests for get_runs_root()."""

    def test_uses_env_override(self, tmp_path: Path) -> None:
        assert run_storage.get_runs_root() == tmp_path / "runs"

    def test_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CHECKLOOP_STATE_HOME", raising=False)
        assert run_storage.get_runs_root() == Path.home() / ".checkloop" / "runs"


class TestIsoTimestamp:
    """Tests for iso_timestamp()."""

    def test_has_expected_format(self) -> None:
        ts = run_storage.iso_timestamp()
        # e.g. "2026-04-21T23-25-31Z"
        assert len(ts) == 20
        assert ts.endswith("Z")
        assert ":" not in ts

    def test_is_branch_safe(self) -> None:
        ts = run_storage.iso_timestamp()
        for ch in (":", " ", "~", "^", "?", "*", "["):
            assert ch not in ts


class TestCreateRunDir:
    """Tests for create_run_dir()."""

    def test_creates_named_dir(self) -> None:
        run_dir = run_storage.create_run_dir("/tmp/my-project", timestamp="2026-04-21T00-00-00Z")
        assert run_dir.exists()
        assert run_dir.name == "my-project-2026-04-21T00-00-00Z"

    def test_sanitizes_special_chars_in_basename(self) -> None:
        run_dir = run_storage.create_run_dir("/tmp/weird name@v1", timestamp="2026-01-01T00-00-00Z")
        assert run_dir.name == "weird-name-v1-2026-01-01T00-00-00Z"

    def test_empty_basename_falls_back_to_target(self) -> None:
        run_dir = run_storage.create_run_dir("/", timestamp="2026-01-01T00-00-00Z")
        assert run_dir.name == "target-2026-01-01T00-00-00Z"

    def test_returns_path_even_on_mkdir_failure(self, tmp_path: Path) -> None:
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            run_dir = run_storage.create_run_dir("/tmp/proj", timestamp="2026-01-01T00-00-00Z")
        # Callers must tolerate a non-existent dir — downstream writers log-and-skip.
        assert isinstance(run_dir, Path)

    def test_uses_auto_timestamp_when_not_provided(self) -> None:
        run_dir = run_storage.create_run_dir("/tmp/proj")
        assert run_dir.name.startswith("proj-")
        assert run_dir.name.endswith("Z")


class TestPruneOldRuns:
    """Tests for prune_old_runs()."""

    def test_no_root_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKLOOP_STATE_HOME", str(tmp_path / "does-not-exist"))
        assert run_storage.prune_old_runs() == 0

    def test_removes_old_run_dirs(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        old = runs / "proj-old"
        old.mkdir()
        (old / "file.log").write_text("x")
        fresh = runs / "proj-fresh"
        fresh.mkdir()

        # Make `old` look 30 days old via utime.
        old_mtime = time.time() - (30 * 24 * 60 * 60)
        os.utime(old, (old_mtime, old_mtime))

        removed = run_storage.prune_old_runs(max_age_days=14)

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_keeps_recent_runs(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        recent = runs / "proj-recent"
        recent.mkdir()
        assert run_storage.prune_old_runs(max_age_days=14) == 0
        assert recent.exists()

    def test_ignores_files_at_root(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        stray = runs / "not-a-run.txt"
        stray.write_text("hi")
        old_mtime = time.time() - (30 * 24 * 60 * 60)
        os.utime(stray, (old_mtime, old_mtime))
        assert run_storage.prune_old_runs(max_age_days=14) == 0
        assert stray.exists()

    def test_custom_max_age(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        d = runs / "proj"
        d.mkdir()
        os.utime(d, (time.time() - 3600, time.time() - 3600))  # 1h old
        assert run_storage.prune_old_runs(max_age_days=1) == 0
        # max_age_days of 0 → anything older than "now" is removed.
        # Use a sub-zero cutoff via time window.
        os.utime(d, (time.time() - (2 * 86400), time.time() - (2 * 86400)))
        assert run_storage.prune_old_runs(max_age_days=1) == 1


class TestSanitizeBasename:
    """Tests for _sanitize_basename()."""

    def test_alnum_unchanged(self) -> None:
        assert run_storage._sanitize_basename("abc123") == "abc123"

    def test_replaces_special_chars_with_dash(self) -> None:
        assert run_storage._sanitize_basename("a b@c") == "a-b-c"

    def test_strips_leading_trailing_dashes(self) -> None:
        assert run_storage._sanitize_basename("@@abc@@") == "abc"

    def test_empty_becomes_target(self) -> None:
        assert run_storage._sanitize_basename("") == "target"
        assert run_storage._sanitize_basename("@@@") == "target"
