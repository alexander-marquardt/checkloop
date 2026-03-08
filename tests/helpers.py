"""Shared test constants and helpers for checkloop tests."""

from __future__ import annotations

from typing import Any
from unittest import mock

from checkloop import process
from checkloop.cli import DEFAULT_CONVERGENCE_THRESHOLD

# Default argument values shared across test_cli, test_cli_args, and test_suite.
SHARED_ARG_DEFAULTS: dict[str, Any] = dict(
    pause=0,
    idle_timeout=process.DEFAULT_IDLE_TIMEOUT,
    verbose=False,
    debug=False,
    dangerously_skip_permissions=False,
)


def make_mock_cli_args(*, dry_run: bool = False, **overrides: Any) -> mock.MagicMock:
    """Build a MagicMock with all attributes main() reads from parsed args."""
    args = mock.MagicMock()
    defaults: dict[str, Any] = {
        **SHARED_ARG_DEFAULTS,
        "dir": "/tmp",
        "cycles": 1,
        "converged_at_percentage": DEFAULT_CONVERGENCE_THRESHOLD,
        "all_checks": False,
        "checks": ["readability"],
        "level": None,
        "dry_run": dry_run,
        "changed_only": None,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(args, key, value)
    return args
