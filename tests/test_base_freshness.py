"""Tests for checkloop.base_freshness — --require-base-fresh enforcement."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from checkloop import base_freshness


class TestParseDuration:
    """parse_duration accepts <int><suffix> with required suffix."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("30m", 30 * 60),
            ("12h", 12 * 3600),
            ("1d", 86400),
            ("7d", 7 * 86400),
            ("1w", 604800),
            ("4w", 4 * 604800),
            ("  2h  ", 2 * 3600),  # whitespace stripped
        ],
    )
    def test_accepts_valid_forms(self, text: str, expected: int) -> None:
        assert base_freshness.parse_duration(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "abc",
            "12",          # bare integer — rejected, unit must be explicit
            "12s",         # seconds not supported
            "12y",         # years not supported
            "1.5h",        # no decimals
            "-5h",         # no negatives
            "1 h",         # internal whitespace
            "h12",         # suffix before number
            "12hours",     # only single-letter suffix
        ],
    )
    def test_rejects_invalid_forms(self, text: str) -> None:
        with pytest.raises(base_freshness.FreshnessParseError):
            base_freshness.parse_duration(text)


class TestFormatAge:
    """_format_age picks the two largest non-zero units."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "<1m"),
            (30, "<1m"),                       # under a minute
            (60, "1m"),
            (45 * 60, "45m"),
            (3600, "1h"),                      # exactly one hour
            (3 * 3600 + 12 * 60, "3h 12m"),
            (86400, "1d"),                     # exactly one day
            (5 * 86400 + 3 * 3600, "5d 3h"),
            (5 * 86400, "5d"),                 # whole days
        ],
    )
    def test_format_age(self, seconds: int, expected: str) -> None:
        assert base_freshness._format_age(seconds) == expected


class TestEnforceBaseFreshness:
    """enforce_base_freshness exits when the base is older than threshold."""

    def test_fresh_base_passes(self) -> None:
        # Commit 1 hour ago, threshold 12 hours → pass.
        with mock.patch.object(
            base_freshness, "_get_commit_timestamp", return_value=1_000_000,
        ):
            base_freshness.enforce_base_freshness(
                workdir="/x",
                base_sha="abc1234",
                max_age_seconds=12 * 3600,
                review_branch="main",
                now=1_000_000 + 3600,
            )
            # No SystemExit → pass.

    def test_stale_base_exits(self) -> None:
        # Commit 5 days ago, threshold 12 hours → fail.
        five_days = 5 * 86400
        with mock.patch.object(
            base_freshness, "_get_commit_timestamp", return_value=1_000_000,
        ), mock.patch.object(
            base_freshness, "_get_commits_since", return_value=23,
        ), pytest.raises(SystemExit):
            base_freshness.enforce_base_freshness(
                workdir="/x",
                base_sha="abc1234",
                max_age_seconds=12 * 3600,
                review_branch="main",
                now=1_000_000 + five_days,
            )

    def test_stale_base_message_includes_age_and_ahead(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        five_days_three_hours = 5 * 86400 + 3 * 3600
        with mock.patch.object(
            base_freshness, "_get_commit_timestamp", return_value=1_000_000,
        ), mock.patch.object(
            base_freshness, "_get_commits_since", return_value=23,
        ), pytest.raises(SystemExit):
            base_freshness.enforce_base_freshness(
                workdir="/x",
                base_sha="e783ae46abcd",
                max_age_seconds=12 * 3600,
                review_branch="main",
                now=1_000_000 + five_days_three_hours,
            )
        captured = capsys.readouterr()
        err = captured.out + captured.err
        assert "e783ae4" in err               # short SHA
        assert "5d 3h" in err                 # human-friendly age
        assert "12h" in err                   # the threshold
        assert "origin/main" in err
        assert "23 commits since" in err
        assert "--require-base-fresh ignore" in err

    def test_no_review_branch_omits_ahead_clause(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # --in-place run: no review_branch, so the "N commits since" clause is omitted.
        with mock.patch.object(
            base_freshness, "_get_commit_timestamp", return_value=1_000_000,
        ), mock.patch.object(
            base_freshness, "_get_commits_since",
        ) as mock_ahead, pytest.raises(SystemExit):
            base_freshness.enforce_base_freshness(
                workdir="/x",
                base_sha="abc1234",
                max_age_seconds=3600,
                review_branch=None,
                now=1_000_000 + 5 * 86400,
            )
        mock_ahead.assert_not_called()
        captured = capsys.readouterr()
        err = captured.out + captured.err
        assert "commits since" not in err

    def test_zero_commits_ahead_omits_ahead_clause(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # If the base IS the current tip, no "N commits since" line — just the age.
        with mock.patch.object(
            base_freshness, "_get_commit_timestamp", return_value=1_000_000,
        ), mock.patch.object(
            base_freshness, "_get_commits_since", return_value=0,
        ), pytest.raises(SystemExit):
            base_freshness.enforce_base_freshness(
                workdir="/x",
                base_sha="abc1234",
                max_age_seconds=3600,
                review_branch="main",
                now=1_000_000 + 5 * 86400,
            )
        captured = capsys.readouterr()
        err = captured.out + captured.err
        assert "commits since" not in err

    def test_singular_commit_ahead(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # One-commit-since uses singular "commit", not "commits".
        with mock.patch.object(
            base_freshness, "_get_commit_timestamp", return_value=1_000_000,
        ), mock.patch.object(
            base_freshness, "_get_commits_since", return_value=1,
        ), pytest.raises(SystemExit):
            base_freshness.enforce_base_freshness(
                workdir="/x",
                base_sha="abc1234",
                max_age_seconds=3600,
                review_branch="main",
                now=1_000_000 + 86400,
            )
        captured = capsys.readouterr()
        err = captured.out + captured.err
        assert "has 1 commit since" in err
        assert "has 1 commits since" not in err

    def test_unreadable_timestamp_fails_open(self) -> None:
        # If git returns an error reading the commit's timestamp, the freshness
        # check is skipped (warning logged, no exit).  The check is a guardrail,
        # not a gate — operator shouldn't be blocked on a transient git failure.
        with mock.patch.object(
            base_freshness, "_get_commit_timestamp", return_value=None,
        ):
            base_freshness.enforce_base_freshness(
                workdir="/x",
                base_sha="abc1234",
                max_age_seconds=3600,
                review_branch="main",
                now=1_000_000,
            )
            # No SystemExit → pass.


class TestGetCommitTimestamp:
    """_get_commit_timestamp parses git output and tolerates errors."""

    def test_parses_unix_timestamp(self) -> None:
        completed = mock.MagicMock(spec=subprocess.CompletedProcess)
        completed.stdout = "1700000000\n"
        with mock.patch.object(subprocess, "run", return_value=completed):
            assert base_freshness._get_commit_timestamp("/x", "abc1234") == 1_700_000_000

    def test_returns_none_on_git_failure(self) -> None:
        with mock.patch.object(
            subprocess, "run",
            side_effect=subprocess.CalledProcessError(128, ["git"], "fatal: bad object"),
        ):
            assert base_freshness._get_commit_timestamp("/x", "abc1234") is None

    def test_returns_none_on_unparseable_output(self) -> None:
        completed = mock.MagicMock(spec=subprocess.CompletedProcess)
        completed.stdout = "not-a-number\n"
        with mock.patch.object(subprocess, "run", return_value=completed):
            assert base_freshness._get_commit_timestamp("/x", "abc1234") is None
