"""Process monitoring: memory measurement, orphan detection, and session cleanup."""

from __future__ import annotations

import logging
import os
import resource
import signal
import subprocess
import sys

from checkloop.terminal import DIM, YELLOW, _print_status

logger = logging.getLogger(__name__)


# --- Memory measurement ------------------------------------------------------

def _measure_current_rss_mb() -> float:
    """Return the current RSS of this process in MB (not peak — actual current)."""
    try:
        pid = os.getpid()
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            # ps may return multiple lines; take only the first non-empty line.
            first_line = result.stdout.strip().splitlines()[0].strip()
            return int(first_line) / 1024  # ps reports in KB
    except (OSError, ValueError) as exc:
        logger.debug("ps-based RSS lookup failed: %s", exc)
    # Fallback: use resource (peak, not current — better than nothing)
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS reports ru_maxrss in bytes; Linux reports in kilobytes.
        scale = 1024 * 1024 if sys.platform == "darwin" else 1024
        return usage.ru_maxrss / scale
    except OSError as exc:
        logger.warning("resource.getrusage() failed: %s", exc)
        return 0.0


# --- Process discovery --------------------------------------------------------

def _parse_pgrep_output(result: subprocess.CompletedProcess[str]) -> list[int]:
    """Extract integer PIDs from pgrep stdout."""
    if result.returncode != 0 or not result.stdout.strip():
        return []
    pids: list[int] = []
    for line in result.stdout.strip().split("\n"):
        try:
            pids.append(int(line.strip()))
        except ValueError:
            logger.debug("pgrep returned non-integer PID line: %r", line)
    return pids


def _find_child_pids() -> list[int]:
    """Return PIDs of surviving child processes (direct children only)."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True, text=True,
        )
    except OSError as exc:
        logger.debug("pgrep failed: %s", exc)
        return []
    return _parse_pgrep_output(result)


def _find_session_pids(session_id: int) -> list[int]:
    """Return PIDs of all processes in the given session, excluding ourselves."""
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-s", str(session_id)],
            capture_output=True, text=True,
        )
    except OSError as exc:
        logger.debug("pgrep -s failed: %s", exc)
        return []
    return [pid for pid in _parse_pgrep_output(result) if pid != my_pid]


# --- Orphan and straggler cleanup --------------------------------------------

def _kill_orphaned_children(pids: list[int] | None = None) -> int:
    """Kill surviving child processes. Returns count killed.

    Accepts an optional pre-fetched pid list to avoid a redundant pgrep spawn.
    """
    killed = 0
    for child_pid in (pids if pids is not None else _find_child_pids()):
        try:
            os.kill(child_pid, signal.SIGKILL)
            killed += 1
            logger.warning("Killed orphaned child process %d", child_pid)
        except OSError as exc:
            logger.debug("Could not kill child %d: %s", child_pid, exc)
    return killed


# Session IDs (claude PIDs) from previous checks. Used by _log_memory_usage
# as a fallback to catch processes that somehow survived _kill_process_group.
_previous_session_ids: list[int] = []


def _log_memory_usage(label: str) -> None:
    """Log current RSS and kill any surviving processes after each check."""
    rss_mb = _measure_current_rss_mb()
    child_pids = _find_child_pids()
    logger.info("Memory [%s]: rss=%.0fMB, children=%d", label, rss_mb, len(child_pids))
    _print_status(f"  Memory: {rss_mb:.0f}MB RSS, {len(child_pids)} child processes", DIM)
    if child_pids:
        _warn_and_kill_orphan_processes(child_pids)
    # Also sweep for stragglers from previous sessions that escaped cleanup.
    _sweep_previous_sessions()


def _warn_and_kill_orphan_processes(child_pids: list[int]) -> None:
    """Warn about surviving child processes and kill them."""
    _print_status(f"  Warning: {len(child_pids)} child process(es) still alive — killing.", YELLOW)
    # Pass pids directly to avoid a second pgrep subprocess spawn.
    killed = _kill_orphaned_children(child_pids)
    if killed:
        _print_status(f"  Killed {killed} orphaned process(es).", YELLOW)


def _sweep_previous_sessions() -> None:
    """Kill any surviving processes from sessions of previous checks."""
    still_active: list[int] = []
    for sid in _previous_session_ids:
        stragglers = _find_session_pids(sid)
        if stragglers:
            logger.warning("Session %d still has %d straggler(s): %s", sid, len(stragglers), stragglers)
            _print_status(f"  Warning: {len(stragglers)} straggler(s) from session {sid} — killing.", YELLOW)
            for pid in stragglers:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            still_active.append(sid)
    _previous_session_ids[:] = still_active
