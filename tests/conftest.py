"""Shared fixtures for the checkloop test suite.

The primary goal is to prevent process-cleanup side effects from leaking
out of tests and into the pytest process itself.  Without these guards,
tests that call ``cli.main()`` register real ``atexit`` handlers and
SIGTERM/SIGHUP signal handlers on the pytest process — which can kill the
terminal or parent process when pytest exits.
"""

from __future__ import annotations

from typing import Callable

from unittest import mock

import pytest

from checkloop import cli as _cli_module, monitoring

# Capture the real function before any mocking happens (module-load time).
_REAL_REGISTER_CLEANUP_HANDLERS = _cli_module._register_cleanup_handlers


@pytest.fixture(autouse=True)
def _reset_monitoring_globals() -> None:
    """Reset module-level tracking state before and after every test.

    ``monitoring.previous_session_ids`` and ``previous_descendant_pids``
    are module globals that accumulate PIDs across checks.  If a test
    mutates them and doesn't clean up (or fails mid-way), the dirty
    state can leak into other tests — or worse, into the ``atexit``
    handler that fires when pytest exits, causing real ``os.kill()``
    calls against stale PIDs.
    """
    saved_sessions = list(monitoring.previous_session_ids)
    saved_descendants = set(monitoring.previous_descendant_pids)

    monitoring.previous_session_ids.clear()
    monitoring.previous_descendant_pids.clear()

    yield  # type: ignore[misc]

    monitoring.previous_session_ids[:] = saved_sessions
    monitoring.previous_descendant_pids.clear()
    monitoring.previous_descendant_pids.update(saved_descendants)


@pytest.fixture(autouse=True)
def _block_cleanup_handlers() -> None:
    """Prevent ``_register_cleanup_handlers`` from installing real handlers.

    ``cli.main()`` calls ``_register_cleanup_handlers()`` which registers
    ``atexit.register(cleanup_all_sessions)`` and real SIGTERM/SIGHUP
    signal handlers on the current process.  During tests the "current
    process" is pytest — so those handlers end up on the test runner
    and fire when pytest exits, running real ``pgrep`` and ``os.kill``
    calls.  This fixture neutralises that by patching the function to
    a no-op for every test.
    """
    with mock.patch("checkloop.cli._register_cleanup_handlers"):
        yield


@pytest.fixture
def real_register_cleanup_handlers() -> Callable[[], None]:
    """Provide the real ``_register_cleanup_handlers`` for tests that need it.

    Tests that verify signal-handler registration behaviour should request
    this fixture, then call the returned function with ``atexit.register``
    and ``signal.signal`` suitably mocked.
    """
    return _REAL_REGISTER_CLEANUP_HANDLERS
