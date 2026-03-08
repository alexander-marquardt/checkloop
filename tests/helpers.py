"""Shared test constants and helpers for checkloop tests."""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Iterator
from typing import Any, cast
from unittest import mock

from checkloop import check_runner, process, suite
from checkloop.checks import CheckDef
from checkloop.checkpoint import CheckpointData
from checkloop.cli_args import DEFAULT_CONVERGENCE_THRESHOLD
from checkloop.process import CheckResult
from checkloop.terminal import SummaryRow

# Default argument values shared across test_cli, test_cli_args, and test_suite.
SHARED_ARG_DEFAULTS: dict[str, Any] = dict(
    pause=0,
    idle_timeout=process.DEFAULT_IDLE_TIMEOUT,
    check_timeout=process.DEFAULT_CHECK_TIMEOUT,
    max_memory_mb=process.DEFAULT_MAX_MEMORY_MB,
    cycles=1,
    convergence_threshold=DEFAULT_CONVERGENCE_THRESHOLD,
    verbose=False,
    debug=False,
    dangerously_skip_permissions=False,
    changed_files_prefix="",
)


def make_suite_args(*, dry_run: bool = True, **overrides: Any) -> argparse.Namespace:
    """Build an argparse.Namespace for _run_check_suite / _run_single_check."""
    defaults = {**SHARED_ARG_DEFAULTS, "dry_run": dry_run}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def make_mock_cli_args(*, dry_run: bool = False, **overrides: Any) -> mock.MagicMock:
    """Build a MagicMock with all attributes main() reads from parsed args."""
    args = mock.MagicMock()
    defaults: dict[str, Any] = {
        **SHARED_ARG_DEFAULTS,
        "dir": "/tmp",
        "all_checks": False,
        "checks": ["readability"],
        "level": None,
        "dry_run": dry_run,
        "changed_only": None,
        "no_resume": False,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(args, key, value)
    return args


def make_git_result(
    returncode: int = 0,
    stdout: str | bytes = "",
    stderr: str = "",
) -> mock.MagicMock:
    """Build a mock subprocess result mimicking git command output.

    Used throughout git, monitoring, and process tests to avoid
    repeating ``mock.MagicMock(returncode=..., stdout=...)`` inline.
    """
    return mock.MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


@contextlib.contextmanager
def patch_suite_git(
    sha_sequence: list[str],
    *,
    run_claude_return: int = 0,
    lines_changed: int | None = None,
    total_tracked: int | None = None,
) -> Iterator[None]:
    """Mock common git/claude dependencies for _run_check_suite.

    Patches _invoke_claude, is_git_repo, git_head_sha, and git_commit_all
    with sensible defaults.  Optionally patches compute_change_stats when
    *lines_changed* is provided.
    """
    result = CheckResult(exit_code=run_claude_return)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(check_runner, "_invoke_claude", return_value=result))
        stack.enter_context(mock.patch.object(suite, "is_git_repo", return_value=True))
        # Share a single iterator so both modules consume from the same SHA sequence.
        sha_iter = iter(sha_sequence)
        stack.enter_context(mock.patch.object(suite, "git_head_sha", side_effect=sha_iter))
        stack.enter_context(mock.patch.object(check_runner, "git_head_sha", side_effect=sha_iter))
        stack.enter_context(mock.patch.object(check_runner, "git_commit_all", return_value=True))
        if lines_changed is not None:
            change_pct = lines_changed / (total_tracked or 1000) * 100
            stack.enter_context(mock.patch.object(
                suite, "compute_change_stats", return_value=(lines_changed, change_pct),
            ))
            stack.enter_context(mock.patch.object(
                check_runner, "compute_change_stats", return_value=(lines_changed, change_pct),
            ))
        yield


def make_check(check_id: str, label: str = "", prompt: str = "p") -> CheckDef:
    """Build a CheckDef for use in tests."""
    return CheckDef(id=check_id, label=label or check_id, prompt=prompt)


def make_summary_row(**overrides: Any) -> SummaryRow:
    """Build a SummaryRow dict with sensible defaults.

    Used across test_terminal classes to avoid repeating the full
    TypedDict construction inline.
    """
    defaults: dict[str, Any] = dict(
        check_id="chk", label="Check", cycle=1, exit_code=0,
        kill_reason=None, made_changes=False, lines_changed=0,
        change_pct=0.0, duration="0m05s",
    )
    defaults.update(overrides)
    return cast(SummaryRow, defaults)


def make_checkpoint_data(**overrides: Any) -> CheckpointData:
    """Build a valid CheckpointData with sensible defaults.

    All fields have reasonable defaults so callers only need to specify the
    values they care about. Used across test_checkpoint, test_suite, and
    test_cli to avoid repeating the full TypedDict construction inline.
    """
    data: CheckpointData = {
        "version": overrides.pop("version", 1),
        "started_at": overrides.pop("started_at", "2026-03-08T12:00:00+00:00"),
        "workdir": overrides.pop("workdir", "/tmp/test-project"),
        "check_ids": overrides.pop("check_ids", ["test-fix", "readability", "dry", "test-validate"]),
        "num_cycles": overrides.pop("num_cycles", 2),
        "convergence_threshold": overrides.pop("convergence_threshold", 0.1),
        "current_cycle": overrides.pop("current_cycle", 1),
        "current_check_index": overrides.pop("current_check_index", 2),
        "active_check_ids": overrides.pop("active_check_ids", ["test-fix", "readability", "dry", "test-validate"]),
        "changed_this_cycle": overrides.pop("changed_this_cycle", ["test-fix"]),
        "previously_changed_ids": overrides.pop("previously_changed_ids", None),
        "prev_change_pct": overrides.pop("prev_change_pct", None),
    }
    return data
