"""Shared fixtures for the checkloop test suite.

The primary goal is to prevent process-cleanup side effects from leaking
out of tests and into the pytest process itself.  Without these guards,
tests that call ``cli.main()`` register real ``atexit`` handlers and
SIGTERM/SIGHUP signal handlers on the pytest process — which can kill the
terminal or parent process when pytest exits.

Also records a persistent per-test start/end trace to ``.pytest-trace.log``
in the project root so that *if* a test ever takes the terminal down (past
incident), the last lines of the trace file identify the culprit after a
reboot.  The file flushes and fsyncs after every line and is size-capped
so it cannot grow without bound.
"""

from __future__ import annotations

import atexit
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from unittest import mock

import pytest

from checkloop import cli as _cli_module, monitoring, telemetry

# Capture the real function before any mocking happens (module-load time).
_REAL_REGISTER_CLEANUP_HANDLERS = _cli_module._register_cleanup_handlers


# ----------------------------------------------------------------------------
# Persistent test-run trace log
# ----------------------------------------------------------------------------

# Keep the trace file next to the project root so post-crash inspection is
# obvious.  Size-capped at 20 MB; when exceeded we truncate back to the last
# ~10 MB (keeping the most recent entries, which is what you want for
# post-mortem).
_TRACE_FILE = Path(__file__).resolve().parent.parent / ".pytest-trace.log"
_TRACE_MAX_BYTES = 20 * 1024 * 1024
_TRACE_KEEP_BYTES = 10 * 1024 * 1024


def _trace_rotate_if_large() -> None:
    """Keep the tail when the trace file grows past _TRACE_MAX_BYTES."""
    try:
        size = _TRACE_FILE.stat().st_size
    except OSError:
        return
    if size < _TRACE_MAX_BYTES:
        return
    try:
        with open(_TRACE_FILE, "rb") as fh:
            fh.seek(-_TRACE_KEEP_BYTES, os.SEEK_END)
            # Realign to the next newline so we don't truncate mid-line.
            fh.readline()
            tail = fh.read()
        _TRACE_FILE.write_bytes(tail)
    except OSError:
        pass


def _trace_write(line: str) -> None:
    """Append one line to the trace file, flushed + fsynced."""
    try:
        with open(_TRACE_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
    except OSError:
        pass


def _trace_session_marker(kind: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    _trace_write(f"{stamp} [pid={os.getpid()}] SESSION_{kind}")


@pytest.fixture(autouse=True, scope="session")
def _pytest_session_trace() -> None:
    """Bracket the whole session with SESSION_START / SESSION_END markers.

    Uses atexit (rather than yield+teardown) for SESSION_END because if a
    test hangs or kills pytest, the fixture teardown may never run; atexit
    fires even when sys.exit is called.
    """
    _trace_rotate_if_large()
    _trace_session_marker("START")
    atexit.register(lambda: _trace_session_marker("END"))
    yield  # type: ignore[misc]


@pytest.fixture(autouse=True)
def _pytest_per_test_trace(request: pytest.FixtureRequest) -> None:
    """Log TEST_START / TEST_END around every single test.

    Writes happen with flush+fsync so even a terminal-killing test leaves
    its node id as the last entry in the trace file — the first place to
    look after a reboot is the last TEST_START with no matching TEST_END.
    """
    nodeid = request.node.nodeid
    start = time.monotonic()
    stamp = datetime.now().isoformat(timespec="seconds")
    _trace_write(f"{stamp} [pid={os.getpid()}] TEST_START {nodeid}")
    try:
        yield  # type: ignore[misc]
    finally:
        elapsed = time.monotonic() - start
        stamp = datetime.now().isoformat(timespec="seconds")
        _trace_write(f"{stamp} [pid={os.getpid()}] TEST_END   {nodeid} ({elapsed:.3f}s)")


# ----------------------------------------------------------------------------
# Safety guards against tests leaking state into the pytest process
# ----------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _block_real_log_handler(request: pytest.FixtureRequest) -> None:
    """Prevent tests that call ``cli.main()`` from opening the real log file.

    ``cli.main(--dir ".")`` calls ``_add_file_log_handler(".")`` which opens
    ``./.checkloop-run.log`` (the project root) and attaches a FileHandler
    to the root logger.  Every subsequent logging.getLogger("checkloop.*")
    emit during the test suite then writes to that real file — producing
    the test-pollution that triggered this work.  Patch it out so tests
    never touch the developer's on-disk log.

    Tests that need to exercise the real ``_add_file_log_handler`` (e.g. to
    verify rotation behaviour) can opt out with
    ``@pytest.mark.uses_real_log_handler``.
    """
    if request.node.get_closest_marker("uses_real_log_handler"):
        yield
        return
    with mock.patch("checkloop.cli._add_file_log_handler"):
        yield


@pytest.fixture(autouse=True)
def _block_telemetry_sampler(request: pytest.FixtureRequest) -> None:
    """Prevent tests from starting the real telemetry sampler thread.

    The sampler thread reads ``monitoring.previous_session_ids`` and spawns
    ``ps`` / ``vm_stat`` subprocesses every few seconds.  In a test context
    that is unwanted (and, if the thread outlives the test, it could race
    with ``_reset_monitoring_globals`` or with other tests' mocked state).

    Tests that need to exercise the real sampler (e.g. ``test_telemetry.py``)
    can opt out with ``@pytest.mark.uses_real_telemetry``.  Even those tests
    are responsible for calling ``telemetry.stop()`` in a finally block — the
    fixture only controls whether ``start()``/``stop()`` are patched.
    """
    # Reset module state unconditionally — tests that opt in are expected to
    # call start() themselves, and tests that opt out benefit from starting
    # each test with a clean slate.
    telemetry._state.thread = None
    telemetry._state.stop_event = None
    telemetry._state.file_handle = None
    telemetry._state.file_path = None
    telemetry._state.current_label = "startup"

    if request.node.get_closest_marker("uses_real_telemetry"):
        yield
        # Defensive: if the opt-in test forgot to stop(), force-stop it so
        # the sampler thread doesn't leak into the next test.
        if telemetry._state.thread is not None:
            try:
                telemetry.stop(event="fixture_teardown")
            except Exception:
                pass
        return

    with mock.patch("checkloop.telemetry.start"), \
         mock.patch("checkloop.telemetry.stop"):
        yield


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so pytest doesn't warn about them."""
    config.addinivalue_line(
        "markers",
        "uses_real_log_handler: allow the real cli._add_file_log_handler to run",
    )
    config.addinivalue_line(
        "markers",
        "uses_real_telemetry: allow the real telemetry.start/stop to run",
    )


# ----------------------------------------------------------------------------
# Opt-in fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def real_register_cleanup_handlers() -> Callable[[], None]:
    """Provide the real ``_register_cleanup_handlers`` for tests that need it.

    Tests that verify signal-handler registration behaviour should request
    this fixture, then call the returned function with ``atexit.register``
    and ``signal.signal`` suitably mocked.
    """
    return _REAL_REGISTER_CLEANUP_HANDLERS


@pytest.fixture
def silence_trace_logger(caplog: pytest.LogCaptureFixture) -> None:
    """Silence checkloop.telemetry DEBUG noise for tests asserting on logs."""
    caplog.set_level(logging.WARNING, logger="checkloop.telemetry")
    yield  # type: ignore[misc]
