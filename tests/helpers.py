"""Shared test constants for checkloop tests."""

from __future__ import annotations

from typing import Any

from checkloop import process

# Default argument values shared across test_cli and test_suite.
SHARED_ARG_DEFAULTS: dict[str, Any] = dict(
    pause=0,
    idle_timeout=process.DEFAULT_IDLE_TIMEOUT,
    verbose=False,
    debug=False,
    dangerously_skip_permissions=False,
)
