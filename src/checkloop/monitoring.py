"""Process monitoring: memory measurement, orphan detection, and session cleanup.

Tracks child processes spawned by Claude Code checks and ensures they are
cleaned up after each check and on program exit.  Measures RSS via ``ps``
(with a ``resource.getrusage`` fallback) and provides an ``atexit`` handler
that kills all tracked sessions.
"""

from __future__ import annotations

import logging
import os
import resource
import signal
import subprocess
import sys

from checkloop.terminal import DIM, YELLOW, print_status

logger = logging.getLogger(__name__)

_KB_PER_MB = 1024  # ps reports RSS in kilobytes
_CMD_TIMEOUT = 10


# --- Shared subprocess helpers ------------------------------------------------

def _run_cmd_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=_CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out after %ds", cmd[0], _CMD_TIMEOUT)
        return None
    except FileNotFoundError:
        logger.debug("%s binary not found", cmd[0])
        return None
    except (OSError, ValueError) as exc:
        logger.debug("%s command failed: %s", cmd[0], exc)
        return None


def _parse_int_lines(stdout: str) -> list[int]:
    values: list[int] = []
    for line in stdout.strip().splitlines():
        stripped = line.strip()
        if stripped:
            try:
                values.append(int(stripped))
            except ValueError:
                logger.debug("Skipping non-integer line: %r", stripped)
    return values


# --- Memory measurement ------------------------------------------------------

def _sum_rss_from_ps(*ps_args: str) -> float:
    """Run ``ps -o rss= <ps_args>`` and return the total RSS in MB."""
    result = _run_cmd_quiet(["ps", "-o", "rss=", *ps_args])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return 0.0
    return sum(_parse_int_lines(result.stdout)) / _KB_PER_MB


def _measure_current_rss_mb() -> float:
    """Return the current RSS of this process in MB (not peak — actual current)."""
    rss = _sum_rss_from_ps("-p", str(os.getpid()))
    if rss > 0:
        return rss
    # Fallback: use resource (peak, not current — better than nothing)
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS reports ru_maxrss in bytes; Linux reports in kilobytes.
        scale = 1024 * 1024 if sys.platform == "darwin" else 1024
        return usage.ru_maxrss / scale
    except OSError as exc:
        logger.warning("resource.getrusage() failed: %s", exc)
        return 0.0


def measure_session_rss_mb(session_id: int) -> float:
    """Return the total RSS (in MB) of all processes in a session.

    Uses ``ps -o rss= -s <session_id>`` to sum the RSS of every process
    in the session.  Returns 0.0 if the session has no processes or if
    the measurement fails.
    """
    return _sum_rss_from_ps("-s", str(session_id))


def snapshot_process_rss(pids: set[int] | list[int]) -> list[tuple[int, float, str]]:
    """Return ``(pid, rss_mb, command)`` for each live PID.

    Uses a single ``ps`` call.  Dead PIDs are silently omitted.
    Returns an empty list on failure or if *pids* is empty.
    """
    if not pids:
        return []
    pid_arg = ",".join(str(p) for p in pids)
    result = _run_cmd_quiet(["ps", "-o", "pid=,rss=,comm=", "-p", pid_arg])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return []
    entries: list[tuple[int, float, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        comm = parts[2].strip() if len(parts) >= 3 else ""
        entries.append((pid, rss_kb / _KB_PER_MB, comm))
    return entries


def measure_pid_rss_mb(pids: set[int]) -> float:
    """Return the total RSS (in MB) of the given PIDs.

    Queries all PIDs in a single ``ps`` call.  PIDs that no longer exist
    are silently ignored (they contribute 0).  Returns 0.0 on failure or
    if *pids* is empty.
    """
    if not pids:
        return 0.0
    return _sum_rss_from_ps("-p", ",".join(str(p) for p in pids))


# --- Process discovery --------------------------------------------------------

def _run_pgrep(*args: str) -> list[int]:
    result = _run_cmd_quiet(["pgrep", *args])
    if result is None or result.returncode != 0:
        return []
    return _parse_int_lines(result.stdout)


def _find_child_pids() -> list[int]:
    return _run_pgrep("-P", str(os.getpid()))


def find_session_pids(session_id: int) -> list[int]:
    """Return PIDs of all processes in the given session, excluding ourselves."""
    my_pid = os.getpid()
    return [pid for pid in _run_pgrep("-s", str(session_id)) if pid != my_pid]


def find_all_descendant_pids(root_pid: int) -> list[int]:
    """Walk the process tree to find every descendant of *root_pid*.

    Uses a single ``ps`` call to build a parent→children mapping, then
    BFS-walks from *root_pid*.  This catches descendants that created new
    sessions or process groups (e.g. via ``setsid()``) and would escape
    ``os.killpg()`` and ``pgrep -s``.
    """
    result = _run_cmd_quiet(["ps", "-eo", "pid=,ppid="])
    if result is None or result.returncode != 0:
        return []

    children_of: dict[int, list[int]] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                pid, ppid = int(parts[0]), int(parts[1])
                children_of.setdefault(ppid, []).append(pid)
            except ValueError:
                continue

    descendants: list[int] = []
    queue = list(children_of.get(root_pid, []))
    visited: set[int] = {root_pid, os.getpid()}
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        descendants.append(pid)
        queue.extend(children_of.get(pid, []))

    return descendants


# --- Orphan and straggler cleanup --------------------------------------------

def kill_pids(pids: list[int], sig: signal.Signals = signal.SIGKILL) -> int:
    """Send *sig* to each PID in *pids*. Returns the count successfully signalled."""
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            killed += 1
            logger.debug("Sent %s to pid %d", sig.name, pid)
        except OSError as exc:
            logger.debug("Could not signal pid %d: %s", pid, exc)
    return killed


def verify_pids_dead(pids: list[int] | set[int]) -> list[int]:
    """Return the subset of *pids* that are still alive.

    Uses ``kill(pid, 0)`` (signal 0 = existence check, no signal sent).
    """
    survivors: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            survivors.append(pid)  # signal 0 succeeded — process is alive
        except OSError:
            pass  # process is dead — expected
    if survivors:
        snapshot = snapshot_process_rss(set(survivors))
        detail = "; ".join(f"pid={pid} rss={rss:.0f}MB cmd={cmd}" for pid, rss, cmd in snapshot)
        logger.warning("%d process(es) survived kill: %s", len(survivors), detail or survivors)
    return survivors


def _kill_orphaned_children(pids: list[int] | None = None) -> int:
    """Kill surviving child processes. Returns count killed.

    Accepts an optional pre-fetched pid list to avoid a redundant pgrep spawn.
    """
    target_pids = pids if pids is not None else _find_child_pids()
    killed = kill_pids(target_pids)
    if killed:
        logger.warning("Killed %d orphaned child process(es)", killed)
    return killed


previous_session_ids: list[int] = []
"""Session IDs (claude PIDs) from previous checks.

Used by ``log_memory_usage`` and ``cleanup_all_sessions`` to catch processes
that survived ``_kill_process_group``.  Each entry is the PID of a Claude
subprocess that was also the session leader (because of ``start_new_session=True``).
"""

previous_descendant_pids: set[int] = set()
"""Descendant PIDs collected during check execution.

Accumulated by periodic tree walks during ``_stream_process_output`` and at
cleanup time.  Covers descendants that created new sessions (via ``setsid()``)
and escaped both ``os.killpg()`` and session-based cleanup.
"""


def log_memory_usage(label: str) -> None:
    """Log current RSS and kill any surviving processes after each check.

    Reports both checkloop's own RSS and the aggregate RSS across all
    tracked sessions and descendants, so the "after check" snapshot shows
    what the children left behind.

    This is a diagnostic/cleanup helper and must never crash the main check
    loop — all errors are caught and logged so the suite can continue.
    """
    try:
        rss_mb = _measure_current_rss_mb()
        child_pids = _find_child_pids()

        # Measure residual RSS across tracked sessions and descendants.
        session_rss = sum(measure_session_rss_mb(sid) for sid in previous_session_ids)
        descendant_rss = measure_pid_rss_mb(previous_descendant_pids) if previous_descendant_pids else 0.0
        residual_rss = session_rss + descendant_rss

        logger.info("Memory [%s]: self=%.0fMB, residual=%.0fMB (sessions=%.0f, descendants=%.0f), "
                     "children=%d, tracked_sessions=%d, tracked_descendants=%d",
                     label, rss_mb, residual_rss, session_rss, descendant_rss,
                     len(child_pids), len(previous_session_ids), len(previous_descendant_pids))
        residual_str = f", {residual_rss:.0f}MB residual" if residual_rss > 0 else ""
        print_status(f"  Memory: {rss_mb:.0f}MB RSS{residual_str}, {len(child_pids)} child processes", DIM)

        if child_pids:
            _warn_and_kill_orphan_processes(child_pids)
        # Also sweep for stragglers from previous sessions that escaped cleanup.
        _sweep_previous_sessions()
    except Exception as exc:
        logger.warning("Memory monitoring failed (%s): %s", label, exc, exc_info=True)


def kill_session_stragglers(session_id: int) -> int:
    """Find and kill any processes still alive in a session.

    When ``start_new_session=True`` is used, the subprocess becomes the session
    leader (SID = its PID).  Children that call ``setsid()`` or ``setpgid()``
    escape ``os.killpg()`` but remain in the original session.  This function
    catches those stragglers and verifies they actually died.

    Returns the number of processes successfully signalled.
    """
    stragglers = find_session_pids(session_id)
    if not stragglers:
        return 0
    logger.warning("Found %d straggler(s) in session %d: %s", len(stragglers), session_id, stragglers)
    killed = kill_pids(stragglers)
    if killed:
        verify_pids_dead(stragglers)
    return killed


def _warn_and_kill_orphan_processes(child_pids: list[int]) -> None:
    print_status(f"  Warning: {len(child_pids)} child process(es) still alive — killing.", YELLOW)
    # Pass pids directly to avoid a second pgrep subprocess spawn.
    killed = _kill_orphaned_children(child_pids)
    if killed:
        print_status(f"  Killed {killed} orphaned process(es).", YELLOW)


def _sweep_previous_sessions() -> None:
    """Kill stragglers from all previously tracked sessions and prune the watch list.

    Sessions where stragglers were found (and killed) are kept in ``previous_session_ids``
    so they are re-checked on the next sweep.  Sessions with no remaining processes are
    dropped from the list — once a session is fully clean it no longer needs monitoring.
    """
    still_active: list[int] = []
    for sid in previous_session_ids:
        try:
            killed = kill_session_stragglers(sid)
            if killed:
                print_status(f"  Warning: {killed} straggler(s) from session {sid} — killed.", YELLOW)
                still_active.append(sid)
        except Exception as exc:
            logger.warning("Failed to sweep session %d: %s", sid, exc, exc_info=True)
    previous_session_ids[:] = still_active

    # Kill tracked descendants that escaped session/group cleanup (e.g. processes
    # that called setsid() and ended up in a different session).
    if previous_descendant_pids:
        killed = kill_pids(list(previous_descendant_pids))
        if killed:
            print_status(f"  Warning: {killed} tracked descendant(s) still alive — killed.", YELLOW)
        previous_descendant_pids.clear()


def cleanup_all_sessions() -> None:
    """Kill all processes from every tracked session.

    Registered as an ``atexit`` handler and called from signal handlers so
    that subprocess trees are cleaned up even when checkloop is interrupted
    by Ctrl+C, SIGTERM, SIGHUP (terminal close), or ``sys.exit()``.

    Each session is cleaned up independently so that a failure in one does
    not prevent cleanup of the others.
    """
    if previous_session_ids:
        logger.info("Cleaning up %d tracked session(s): %s", len(previous_session_ids), previous_session_ids)
    for sid in previous_session_ids:
        try:
            kill_session_stragglers(sid)
        except Exception as exc:
            logger.warning("Failed to clean up session %d: %s", sid, exc)
    # Kill tracked descendants that escaped session/group cleanup.
    if previous_descendant_pids:
        logger.info("Cleaning up %d tracked descendant PID(s)", len(previous_descendant_pids))
        kill_pids(list(previous_descendant_pids))
        previous_descendant_pids.clear()
    # Also kill any direct children that might have escaped session tracking.
    try:
        child_pids = _find_child_pids()
        if child_pids:
            _kill_orphaned_children(child_pids)
    except Exception as exc:
        logger.warning("Failed to clean up child processes: %s", exc)
    previous_session_ids.clear()
