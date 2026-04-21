"""Shared test constants and helpers for checkloop tests."""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest import mock

from checkloop import check_runner, checkpoint, process, suite
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
    system_free_floor_mb=process.DEFAULT_SYSTEM_FREE_FLOOR_MB,
    cycles=1,
    convergence_threshold=DEFAULT_CONVERGENCE_THRESHOLD,
    verbose=False,
    debug=False,
    dangerously_skip_permissions=False,
    changed_files_prefix="",
    in_place=True,
    review_branch=None,
)


def make_suite_args(*, dry_run: bool = True, **overrides: Any) -> argparse.Namespace:
    defaults = {**SHARED_ARG_DEFAULTS, "dry_run": dry_run}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def make_mock_cli_args(*, dry_run: bool = False, **overrides: Any) -> mock.MagicMock:
    args = mock.MagicMock()
    defaults: dict[str, Any] = {
        **SHARED_ARG_DEFAULTS,
        "dir": "/tmp",
        "all_checks": False,
        "checks": ["readability"],
        "plan": None,
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
        # Prevent pre-suite uncommitted-change snapshot from running.
        stack.enter_context(mock.patch.object(suite, "has_uncommitted_changes", return_value=False))
        if lines_changed is not None:
            change_pct = lines_changed / (total_tracked or 1000) * 100
            # compute_change_stats returns (lines_added, lines_deleted, lines_changed, pct)
            # Split lines_changed evenly between added and deleted for testing purposes.
            lines_added = lines_changed // 2
            lines_deleted = lines_changed - lines_added
            stack.enter_context(mock.patch.object(
                suite, "compute_change_stats", return_value=(lines_added, lines_deleted, lines_changed, change_pct),
            ))
            stack.enter_context(mock.patch.object(
                check_runner, "compute_change_stats", return_value=(lines_added, lines_deleted, lines_changed, change_pct),
            ))
            # Also patch compute_file_stats with reasonable defaults.
            stack.enter_context(mock.patch.object(
                check_runner, "compute_file_stats", return_value=(1, 0, 1),
            ))
        yield


def make_check(check_id: str, label: str = "", prompt: str = "p") -> CheckDef:
    return CheckDef(id=check_id, label=label or check_id, prompt=prompt)


def make_summary_row(**overrides: Any) -> SummaryRow:
    defaults: dict[str, Any] = dict(
        check_id="chk", label="Check", cycle=1, exit_code=0,
        kill_reason=None, made_changes=False, lines_changed=0,
        change_pct=0.0, duration="0m05s",
    )
    defaults.update(overrides)
    return cast(SummaryRow, defaults)


def make_checkpoint_data(**overrides: Any) -> CheckpointData:
    data: CheckpointData = {
        "version": overrides.pop("version", checkpoint._CHECKPOINT_VERSION),
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
        "scratch_branch": overrides.pop("scratch_branch", None),
        "scratch_base_sha": overrides.pop("scratch_base_sha", None),
        "original_branch": overrides.pop("original_branch", None),
    }
    return data


def assert_checkpoint_field_rejected(tmp_path: Path, **overrides: Any) -> None:
    overrides.setdefault("workdir", str(tmp_path))
    raw: dict[str, Any] = dict(make_checkpoint_data(**overrides))
    path = tmp_path / checkpoint._CHECKPOINT_FILENAME
    path.write_text(json.dumps(raw))
    assert checkpoint.load_checkpoint(str(tmp_path)) is None
