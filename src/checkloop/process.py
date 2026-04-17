"""Claude Code subprocess management: spawning, streaming, and cleanup.

Each check runs Claude Code as a subprocess in its own process group (via
``os.setsid``).  Output is streamed as JSONL and parsed in real time.  The
module enforces idle timeouts, hard wall-clock timeouts, and child-tree RSS
memory limits, killing the entire process group when any limit is exceeded.
"""

from __future__ import annotations

import logging
import os
import select
import shlex
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO

from checkloop.monitoring import (
    find_all_descendant_pids,
    find_session_pids,
    kill_pids,
    kill_session_stragglers,
    log_memory_usage,
    measure_pid_rss_mb,
    measure_session_rss_mb,
    previous_descendant_pids,
    previous_session_ids,
    snapshot_process_rss,
    verify_pids_dead,
)
from checkloop.streaming import process_jsonl_buffer
from checkloop.terminal import (
    DIM,
    GREEN,
    RED,
    YELLOW,
    clear_inline_status,
    fatal,
    format_duration,
    print_status,
    print_status_inline,
)

logger = logging.getLogger(__name__)


# --- Kill reasons -------------------------------------------------------------

KILL_REASON_MEMORY = "memory_limit"
KILL_REASON_TIMEOUT = "check_timeout"
KILL_REASON_IDLE = "idle_timeout"


@dataclass
class CheckResult:
    """Result of a single Claude Code check invocation."""

    exit_code: int
    kill_reason: str | None = None

DEFAULT_IDLE_TIMEOUT = 300
"""Seconds of silence before killing a subprocess (default for *idle_timeout*)."""

DEFAULT_MAX_MEMORY_MB = 8192
"""Max child-tree RSS in MB before killing (default for *max_memory_mb*)."""

DEFAULT_CHECK_TIMEOUT = 0
"""Hard wall-clock timeout per check in seconds (default for *check_timeout*).

A value of 0 disables the hard timeout entirely.
"""

_NUDGE_BEFORE_TIMEOUT = 60
"""Send a stdin nudge this many seconds before idle timeout would trigger.

During extended thinking, Claude produces no output. Sending a nudge (escape
followed by newline) can prompt it to emit a status update, resetting the
idle timer and avoiding premature kills.
"""

_NUDGE_SEQUENCE = b"\x1b\n"
"""Bytes sent to stdin as a nudge: ESC followed by newline.

This sequence interrupts extended thinking and prompts Claude to produce
output without disrupting the current operation.
"""

_READ_CHUNK_SIZE = 8192  # bytes per stdout read during streaming
_DRAIN_CHUNK_SIZE = 65536  # bytes per read when draining after process exit
_MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10 MB safety cap on JSONL output buffer
_PROCESS_WAIT_TIMEOUT = 5
_MEMORY_CHECK_INTERVAL = 10
_QUIET_STATUS_INTERVAL = 15  # show "still working" status after this many seconds of silence
_QUIET_STATUS_REFRESH = 10  # update the status line every N seconds while quiet

# Perf: build once instead of copying os.environ on every subprocess spawn.
# Strips CLAUDECODE env var whose presence causes nested claude processes
# to refuse to start when checkloop is invoked from within a Claude Code session.
SANITIZED_ENV: dict[str, str] = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


# --- Claude command construction ----------------------------------------------

DEFAULT_CLAUDE_COMMAND = "claude"
"""Default Claude CLI executable name."""

def _build_claude_command(
    prompt: str,
    skip_permissions: bool,
    model: str | None = None,
    claude_command: str = DEFAULT_CLAUDE_COMMAND,
) -> list[str]:
    cmd = [claude_command]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if model:
        cmd += ["--model", model]
    cmd += ["-p", prompt, "--output-format", "stream-json", "--verbose"]
    return cmd


# --- Subprocess lifecycle -----------------------------------------------------

def _spawn_claude_process(
    cmd: list[str],
    workdir: str,
) -> subprocess.Popen[bytes]:
    """Launch the Claude subprocess in its own process group.

    Using a dedicated process group (via ``os.setsid``) ensures that
    ``_kill_process_group`` can terminate the claude process **and** any
    children it spawns (language servers, tool runners, etc.), preventing
    orphaned processes from accumulating memory across many checks.

    Stdin is piped (not DEVNULL) so we can send nudges during extended
    thinking periods to prompt Claude to produce output.
    """
    # Slice up to (not including) the -p flag so the prompt text never
    # appears in INFO-level logs.  The full prompt is logged at DEBUG level
    # in run_claude() before this function is called.
    p_idx = cmd.index("-p") if "-p" in cmd else len(cmd)
    logger.info("Spawning subprocess: %s (cwd=%s)", cmd[:p_idx], workdir)
    try:
        return subprocess.Popen(
            cmd,
            cwd=workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=SANITIZED_ENV,
            start_new_session=True,  # creates a new process group
        )
    except FileNotFoundError:
        # Command not found as a direct executable — it may be a shell
        # alias or function (e.g. `claude-bedrock` aliased to
        # `CLAUDE_CODE_USE_BEDROCK=1 claude`).  Retry through the user's
        # interactive shell so aliases and functions are resolved.
        shell = os.environ.get("SHELL", "/bin/sh")
        logger.info("Retrying via %s -ic — %r not found on PATH", shell, cmd[0])
        try:
            return subprocess.Popen(
                [shell, "-ic", shlex.join(cmd)],
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=SANITIZED_ENV,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError):
            pass  # fall through to the original fatal error
        exe = cmd[0] if cmd else "claude"
        fatal(
            f"Error: `{exe}` not found. Is Claude Code installed?\n"
            "  Install: npm install -g @anthropic-ai/claude-code"
        )
    except OSError as exc:
        fatal(f"Failed to launch claude subprocess: {exc}")
    except Exception as exc:
        logger.error("Unexpected error spawning claude subprocess: %s", exc, exc_info=True)
        fatal(f"Unexpected error launching claude subprocess: {exc}")


def _read_stdout_chunk(stdout: IO[bytes]) -> bytes:
    try:
        # BufferedReader.read1() returns available data without blocking for the
        # full chunk size. Fall back to os.read() for raw file descriptors.
        read1: Callable[[int], bytes] | None = getattr(stdout, "read1", None)
        if read1 is not None:
            return read1(_READ_CHUNK_SIZE)
        return os.read(stdout.fileno(), _READ_CHUNK_SIZE)
    except OSError as exc:
        logger.debug("stdout read failed: %s", exc)
        return b""


def _drain_remaining_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    check_start_time: float,
    debug: bool,
) -> bytearray:
    try:
        while True:
            remaining = os.read(stdout.fileno(), _DRAIN_CHUNK_SIZE)
            if not remaining:
                break
            output_buffer.extend(remaining)
            output_buffer = process_jsonl_buffer(
                output_buffer, check_start_time, debug, max_buffer_size=_MAX_BUFFER_SIZE,
            )
    except OSError as exc:
        logger.debug("Failed to drain remaining stdout: %s", exc)
    return output_buffer


def _send_nudge(process: subprocess.Popen[bytes], idle_seconds: float) -> bool:
    """Send a nudge to stdin to prompt Claude to produce output.

    Returns True if the nudge was sent successfully, False otherwise.
    """
    if process.stdin is None:
        return False
    try:
        process.stdin.write(_NUDGE_SEQUENCE)
        process.stdin.flush()
        logger.info("Sent stdin nudge after %.0fs idle (pid=%d)", idle_seconds, process.pid)
        print_status(f"\n[nudge after {int(idle_seconds)}s idle]", DIM)
        return True
    except (OSError, BrokenPipeError) as exc:
        logger.debug("Failed to send nudge: %s", exc)
        return False


def _describe_active_work(root_pid: int) -> str:
    """Summarise what descendant processes are running, for user-facing status.

    Returns a short description like "running pytest" or "running pytest, node"
    based on the leaf commands in the process tree.  Falls back to a process
    count if command names can't be determined.
    """
    descendants = find_all_descendant_pids(root_pid)
    if not descendants:
        return "working"
    snapshot = snapshot_process_rss(set(descendants))
    if not snapshot:
        return f"{len(descendants)} subprocess(es) active"
    # Extract recognisable tool names from command basenames, ignoring
    # shells and wrappers that aren't informative to the user.
    _IGNORE = {"zsh", "bash", "sh", "tail", "head", "cat", "tee", "uv", "node", "claude"}
    tools: list[str] = []
    for _pid, _rss, cmd in snapshot:
        basename = cmd.rsplit("/", 1)[-1].split()[0] if cmd else ""
        if basename and basename not in _IGNORE:
            tools.append(basename)
    if tools:
        unique = list(dict.fromkeys(tools))[:3]
        return "running " + ", ".join(unique)
    return f"{len(descendants)} subprocess(es) active"


def _check_idle_timeout(
    last_output_time: float,
    idle_timeout: int,
    check_start_time: float,
    process: subprocess.Popen[bytes],
) -> bool:
    now = time.time()
    idle_seconds = now - last_output_time
    if idle_seconds > idle_timeout:
        # Before killing, check if there are active descendant processes.
        # If Claude is waiting for subprocesses (like pytest), that's not
        # truly idle — it's waiting for results. Only kill if Claude AND
        # all its children have been silent.
        descendants = find_all_descendant_pids(process.pid)
        if descendants:
            # There are still child processes running (e.g., test suites).
            # Log this and skip the idle kill — the work is still in progress.
            logger.info("Idle timeout would trigger, but %d descendant(s) still running: %s",
                        len(descendants), descendants[:5])  # Log first 5 PIDs
            elapsed = format_duration(now - check_start_time)
            print_status_inline(
                f"  [{elapsed}] waiting — {len(descendants)} subprocess(es) still running "
                f"({int(idle_seconds)}s idle)", DIM)
            return False

        logger.warning("Idle timeout: pid=%d, idle=%.0fs, elapsed=%s",
                       process.pid, idle_seconds,
                       format_duration(now - check_start_time))
        clear_inline_status()
        print_status(f"\nIdle for {idle_timeout}s — killing "
                     f"(ran {format_duration(now - check_start_time)}).", RED)
        _kill_process_group(process)
        return True
    return False


def _check_hard_timeout(
    check_start_time: float,
    check_timeout: int,
    process: subprocess.Popen[bytes],
) -> bool:
    if check_timeout <= 0:
        return False
    elapsed = time.time() - check_start_time
    if elapsed > check_timeout:
        logger.warning("Hard timeout: pid=%d, elapsed=%s, limit=%ds",
                       process.pid, format_duration(elapsed), check_timeout)
        print_status(f"\nHard timeout ({format_duration(elapsed)} > "
                     f"{format_duration(check_timeout)}) — killing.", RED)
        _kill_process_group(process)
        return True
    return False


def _log_per_pid_breakdown(session_id: int, known_descendants: set[int] | None) -> None:
    """Log per-PID RSS breakdown for forensic analysis.

    Called when memory usage is notable (over 50% of limit or exceeded).
    Uses a single ``ps`` call to snapshot every relevant process.
    """
    all_pids: set[int] = set()
    session_member_pids = find_session_pids(session_id)
    all_pids.update(session_member_pids)
    if known_descendants:
        all_pids.update(known_descendants)
    all_pids.discard(os.getpid())
    if not all_pids:
        return
    entries = snapshot_process_rss(all_pids)
    if not entries:
        return
    entries.sort(key=lambda e: e[1], reverse=True)  # largest first
    lines = [f"  pid={pid:>7d}  rss={rss:>7.0f}MB  cmd={cmd}" for pid, rss, cmd in entries]
    logger.info("Per-PID RSS breakdown (session %d, %d processes):\n%s",
                session_id, len(entries), "\n".join(lines))


def _check_memory_limit(
    session_id: int,
    max_memory_mb: int,
    check_start_time: float,
    process: subprocess.Popen[bytes],
    last_memory_check: float,
    known_descendants: set[int] | None = None,
    prev_rss_mb: float = 0.0,
) -> tuple[bool, float, float]:
    """Check child tree RSS against the memory limit.

    Returns ``(should_kill, last_check_time, current_rss_mb)``.  Only
    samples every ``_MEMORY_CHECK_INTERVAL`` seconds to avoid excessive
    ``ps`` calls.

    When *known_descendants* is provided, their RSS is measured separately
    and added to the session total.  This catches processes that called
    ``setsid()`` and escaped the original session — without this, they
    consume memory invisibly until the check ends.

    *prev_rss_mb* is the RSS from the previous measurement, used to detect
    rapid growth and log per-PID breakdowns at key thresholds.
    """
    if max_memory_mb <= 0:
        return False, last_memory_check, 0.0
    now = time.time()
    if now - last_memory_check < _MEMORY_CHECK_INTERVAL:
        return False, last_memory_check, prev_rss_mb
    session_rss = measure_session_rss_mb(session_id)
    # Measure descendants that escaped the session (called setsid()).
    # Filter out PIDs already counted by the session measurement to
    # avoid double-counting.
    escaped_rss = 0.0
    if known_descendants:
        session_pids = set(find_all_descendant_pids(process.pid))
        escaped_pids = known_descendants - session_pids - {process.pid}
        escaped_rss = measure_pid_rss_mb(escaped_pids)
    rss_mb = session_rss + escaped_rss
    logger.debug("Session %d RSS: %.0f MB (session=%.0f, escaped=%.0f, limit: %d MB)",
                 session_id, rss_mb, session_rss, escaped_rss, max_memory_mb)

    # --- Memory growth trend warnings ---
    pct_of_limit = (rss_mb / max_memory_mb) * 100 if max_memory_mb > 0 else 0
    prev_pct = (prev_rss_mb / max_memory_mb) * 100 if max_memory_mb > 0 else 0
    # Warn when crossing 50% or 75% thresholds.
    for threshold in (50, 75):
        if prev_pct < threshold <= pct_of_limit:
            logger.warning("Memory at %d%% of limit: %.0fMB / %dMB (session=%d)",
                           int(pct_of_limit), rss_mb, max_memory_mb, session_id)
            _log_per_pid_breakdown(session_id, known_descendants)
    # Warn when RSS doubles between consecutive measurements.
    if prev_rss_mb > 0 and rss_mb >= prev_rss_mb * 2:
        logger.warning("Memory doubled: %.0fMB → %.0fMB (session=%d, limit=%dMB)",
                       prev_rss_mb, rss_mb, session_id, max_memory_mb)
        _log_per_pid_breakdown(session_id, known_descendants)

    if rss_mb > max_memory_mb:
        elapsed = format_duration(now - check_start_time)
        logger.warning("Memory limit exceeded: pid=%d, rss=%.0fMB (session=%.0f, escaped=%.0f), "
                       "limit=%dMB, elapsed=%s",
                       process.pid, rss_mb, session_rss, escaped_rss, max_memory_mb, elapsed)
        _log_per_pid_breakdown(session_id, known_descendants)
        print_status(f"\nChild tree using {rss_mb:.0f}MB (limit: {max_memory_mb}MB) "
                     f"after {elapsed} — killing.", RED)
        _kill_process_group(process)
        return True, now, rss_mb
    return False, now, rss_mb


def _flush_and_close_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    check_start_time: float,
    debug: bool,
) -> None:
    # Append a newline to force any trailing incomplete JSONL line through the parser
    output_buffer.extend(b"\n")
    process_jsonl_buffer(output_buffer, check_start_time, debug)
    try:
        stdout.close()
    except OSError as exc:
        logger.debug("Failed to close stdout pipe: %s", exc)


def _check_resource_limits(
    process: subprocess.Popen[bytes],
    check_start_time: float,
    last_output_time: float,
    idle_timeout: int,
    check_timeout: int,
    max_memory_mb: int,
    last_memory_check: float,
    last_nudge_time: float,
    known_descendants: set[int] | None = None,
    prev_rss_mb: float = 0.0,
) -> tuple[str | None, float, float, float]:
    """Check all resource limits in one pass, sending nudges before timeout.

    Returns ``(kill_reason, last_memory_check, last_nudge_time, prev_rss_mb)``.
    *kill_reason* is one of the ``KILL_REASON_*`` constants if a limit was
    exceeded, or ``None``.

    A nudge is sent to stdin when idle time approaches the timeout threshold
    (within ``_NUDGE_BEFORE_TIMEOUT`` seconds). This prompts Claude to produce
    output during extended thinking, which resets the idle timer and avoids
    premature kills.
    """
    now = time.time()
    idle_seconds = now - last_output_time
    nudge_threshold = idle_timeout - _NUDGE_BEFORE_TIMEOUT

    # Send a nudge if we're approaching idle timeout and haven't nudged recently.
    # Only nudge once per idle period (last_nudge_time resets when output arrives).
    if nudge_threshold > 0 and idle_seconds >= nudge_threshold and last_nudge_time < last_output_time:
        if _send_nudge(process, idle_seconds):
            last_nudge_time = now

    if _check_idle_timeout(last_output_time, idle_timeout, check_start_time, process):
        return KILL_REASON_IDLE, last_memory_check, last_nudge_time, prev_rss_mb
    if _check_hard_timeout(check_start_time, check_timeout, process):
        return KILL_REASON_TIMEOUT, last_memory_check, last_nudge_time, prev_rss_mb
    # process.pid == session ID because start_new_session=True makes the
    # subprocess the session leader (SID = its PID).
    exceeded, last_memory_check, prev_rss_mb = _check_memory_limit(
        process.pid, max_memory_mb, check_start_time, process, last_memory_check,
        known_descendants=known_descendants, prev_rss_mb=prev_rss_mb,
    )
    if exceeded:
        return KILL_REASON_MEMORY, last_memory_check, last_nudge_time, prev_rss_mb
    return None, last_memory_check, last_nudge_time, prev_rss_mb


def _stream_process_output(
    process: subprocess.Popen[bytes],
    idle_timeout: int,
    debug: bool,
    *,
    check_timeout: int = 0,
    max_memory_mb: int = 0,
    accumulated_descendant_pids: set[int] | None = None,
    raw_log_file: IO[bytes] | None = None,
) -> tuple[float, str | None]:
    """Stream and display JSONL output from the Claude process.

    Kills the process if it produces no output for *idle_timeout* seconds,
    if the hard *check_timeout* is exceeded, or if the child process tree
    exceeds *max_memory_mb* of RSS.

    Before killing for idle timeout, sends a nudge (ESC + newline) to stdin
    to prompt Claude to produce output during extended thinking periods.

    If *accumulated_descendant_pids* is provided, the process tree is
    periodically scanned (every ``_MEMORY_CHECK_INTERVAL`` seconds) and
    newly discovered descendant PIDs are added to the set.  This catches
    processes that call ``setsid()`` mid-flight and would escape normal
    group/session cleanup at teardown.

    Returns ``(check_start_time, kill_reason)`` where *kill_reason* is one of
    the ``KILL_REASON_*`` constants, or None for a normal exit.
    """
    if process.stdout is None:
        logger.error("Subprocess stdout is None — cannot stream output (pid=%d)", process.pid)
        return time.time(), None
    stdout = process.stdout
    check_start_time = time.time()
    last_output_time = check_start_time  # idle timer starts from launch
    last_memory_check = check_start_time
    last_nudge_time = 0.0  # tracks when we last sent a nudge (0 = never)
    last_descendant_scan = check_start_time
    last_quiet_status = 0.0  # when we last showed a "still working" inline status
    prev_rss_mb = 0.0  # previous RSS measurement for growth trend detection
    output_buffer = bytearray()
    kill_reason: str | None = None

    try:
        while True:
            # Periodic descendant tree scan — runs before resource limit
            # checks so that escaped-session PIDs are included in the
            # memory measurement on the same cycle they are discovered.
            if accumulated_descendant_pids is not None:
                now = time.time()
                if now - last_descendant_scan >= _MEMORY_CHECK_INTERVAL:
                    descendants = find_all_descendant_pids(process.pid)
                    new_pids = set(descendants) - accumulated_descendant_pids
                    accumulated_descendant_pids.update(descendants)
                    last_descendant_scan = now
                    # Log newly discovered descendants with their parent
                    # relationships for process-tree forensics.
                    if new_pids:
                        snapshot = snapshot_process_rss(new_pids)
                        if snapshot:
                            lines = [f"  pid={pid:>7d}  rss={rss:>7.0f}MB  cmd={cmd}"
                                     for pid, rss, cmd in snapshot]
                            logger.info("New descendant(s) of pid %d: %d discovered\n%s",
                                        process.pid, len(new_pids), "\n".join(lines))

            kill_reason, last_memory_check, last_nudge_time, prev_rss_mb = _check_resource_limits(
                process, check_start_time, last_output_time, idle_timeout,
                check_timeout, max_memory_mb, last_memory_check, last_nudge_time,
                known_descendants=accumulated_descendant_pids, prev_rss_mb=prev_rss_mb,
            )
            if kill_reason:
                break

            try:
                # 1s timeout lets us check idle timeout and process exit between reads
                ready, _, _ = select.select([stdout], [], [], 1.0)
            except InterruptedError:
                # SIGINT (Ctrl+C) interrupted select — let KeyboardInterrupt
                # propagate so the suite-level handler can exit cleanly.
                raise KeyboardInterrupt
            except (OSError, ValueError) as exc:
                logger.debug("select() failed (fd may be closed): %s", exc)
                break

            if not ready:
                if process.poll() is not None:
                    logger.debug("Process exited (rc=%s) — draining remaining stdout", process.returncode)
                    output_buffer = _drain_remaining_stdout(
                        stdout, output_buffer, check_start_time, debug,
                    )
                    break

                now = time.time()
                quiet_seconds = now - last_output_time
                if quiet_seconds >= _QUIET_STATUS_INTERVAL and now - last_quiet_status >= _QUIET_STATUS_REFRESH:
                    elapsed = format_duration(now - check_start_time)
                    activity = _describe_active_work(process.pid)
                    print_status_inline(
                        f"  [{elapsed}] {activity} ({int(quiet_seconds)}s since last output)",
                        DIM,
                    )
                    last_quiet_status = now

                continue

            chunk = _read_stdout_chunk(stdout)
            if not chunk:
                logger.debug("EOF on stdout (pid=%d)", process.pid)
                break

            # Clear any inline "waiting" status before printing new output.
            clear_inline_status()
            last_output_time = time.time()
            if raw_log_file is not None:
                raw_log_file.write(chunk)
                raw_log_file.flush()
            output_buffer.extend(chunk)
            output_buffer = process_jsonl_buffer(
                output_buffer, check_start_time, debug, max_buffer_size=_MAX_BUFFER_SIZE,
            )

    finally:
        clear_inline_status()
        _flush_and_close_stdout(stdout, output_buffer, check_start_time, debug)

    return check_start_time, kill_reason


# --- Process group cleanup ----------------------------------------------------

def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError as exc:
        logger.debug("%s to pgid %d failed: %s", sig.name, pgid, exc)


def _kill_remaining_descendants(all_known: set[int], root_pid: int) -> None:
    """Kill descendant PIDs that survived process-group and session cleanup.

    *all_known* is the union of PIDs collected during streaming and a fresh
    tree snapshot taken at teardown time.  PIDs that match this process or
    *root_pid* (the Claude session leader) are excluded — we only target
    descendants that called ``setsid()`` and escaped ``os.killpg()`` /
    ``pgrep -s`` cleanup.

    Surviving PIDs are also added to the module-level
    ``previous_descendant_pids`` set so that ``_sweep_previous_sessions``
    and ``cleanup_all_sessions`` can re-check them later.
    """
    my_pid = os.getpid()
    targets = [pid for pid in all_known if pid != my_pid and pid != root_pid]
    if not targets:
        return
    killed = kill_pids(targets)
    if killed:
        logger.info("Killed %d descendant(s) that escaped group/session cleanup", killed)
        verify_pids_dead(targets)
    # Track survivors for follow-up sweeps.
    previous_descendant_pids.update(targets)


def _kill_process_group(
    process: subprocess.Popen[bytes],
    *,
    extra_pids: set[int] | None = None,
) -> None:
    """Terminate the process and its entire process group.

    Sends SIGTERM first (graceful), waits briefly, then SIGKILL if needed.
    This prevents orphaned child processes from leaking memory.

    If *extra_pids* is provided, those PIDs (collected during streaming via
    periodic tree walks) are merged with a fresh descendant snapshot and
    killed after the normal group/session cleanup, catching processes that
    escaped via ``setsid()``.
    """
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        # Process already gone — just clean up session stragglers.
        kill_session_stragglers(process.pid)
        return

    # Snapshot the descendant tree *before* killing the group — some
    # descendants may exit once the leader dies, so grab them now.
    teardown_descendants = set(find_all_descendant_pids(process.pid))

    # Escalate: SIGTERM → wait → SIGKILL → wait
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal_process_group(pgid, sig)
        logger.info("Sent %s to process group %d (pid=%d)", sig.name, pgid, process.pid)
        try:
            process.wait(timeout=_PROCESS_WAIT_TIMEOUT)
            break
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                logger.warning("Process %d did not exit after SIGKILL", process.pid)

    # Kill any processes that escaped the process group but remain in the
    # session created by start_new_session=True.  The session ID equals the
    # claude PID because setsid() makes it the session leader.
    kill_session_stragglers(process.pid)

    # Kill descendants that escaped both group and session cleanup (e.g.
    # processes that called setsid() and ended up in a different session).
    all_known = teardown_descendants
    if extra_pids:
        all_known |= extra_pids
    _kill_remaining_descendants(all_known, process.pid)


# --- Public API: run a single Claude Code check ------------------------------

def run_claude(
    prompt: str,
    workdir: str,
    *,
    skip_permissions: bool = False,
    dry_run: bool = False,
    debug: bool = False,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    check_timeout: int = DEFAULT_CHECK_TIMEOUT,
    max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
    model: str | None = None,
    claude_command: str = DEFAULT_CLAUDE_COMMAND,
    raw_log_file: IO[bytes] | None = None,
) -> CheckResult:
    """Run a single Claude Code check.

    Uses ``--output-format stream-json`` so progress events stream in real time.
    The process is killed if it is idle for *idle_timeout* seconds, exceeds
    *check_timeout* wall-clock seconds, or if its child process tree exceeds
    *max_memory_mb* of RSS.

    Args:
        prompt: The review prompt to send to Claude Code.
        workdir: Absolute path to the project directory to run the check in.
        skip_permissions: If True, passes ``--dangerously-skip-permissions``
            to Claude Code, bypassing all interactive permission prompts.
        dry_run: If True, prints what would run without launching a subprocess.
        debug: If True, prints raw non-JSON subprocess output lines.
        idle_timeout: Seconds of silence (no stdout output) before killing
            the subprocess.
        check_timeout: Hard wall-clock limit per check in seconds.
            0 disables the hard timeout.
        max_memory_mb: Maximum RSS (in MB) for the child process tree before
            killing. 0 disables memory monitoring.

    Returns:
        A ``CheckResult`` with the exit code and, if the process was killed,
        the reason (``KILL_REASON_MEMORY``, ``KILL_REASON_TIMEOUT``, or
        ``KILL_REASON_IDLE``).
    """
    cmd = _build_claude_command(prompt, skip_permissions, model, claude_command)
    logger.info("run_claude: workdir=%s, prompt_len=%d, skip_permissions=%s, model=%s, idle_timeout=%d, "
                "check_timeout=%d, max_memory_mb=%d",
                workdir, len(prompt), skip_permissions, model, idle_timeout, check_timeout, max_memory_mb)
    logger.debug("run_claude prompt: %.1000s", prompt)
    p_idx = cmd.index("-p") if "-p" in cmd else len(cmd)
    print_status(f"$ {' '.join(cmd[:p_idx])} -p [prompt omitted]", DIM)

    if dry_run:
        logger.info("Dry-run mode — skipping actual subprocess invocation (workdir=%s)", workdir)
        print_status(f"[DRY RUN] Would run in {workdir}:", YELLOW)
        truncated = prompt[:120] + ("..." if len(prompt) > 120 else "")
        print(f"  Prompt: {truncated}")
        return CheckResult(exit_code=0)

    return _execute_claude_process(
        cmd, workdir,
        idle_timeout=idle_timeout, debug=debug,
        check_timeout=check_timeout, max_memory_mb=max_memory_mb,
        raw_log_file=raw_log_file,
    )


def _execute_claude_process(
    cmd: list[str],
    workdir: str,
    *,
    idle_timeout: int,
    debug: bool,
    check_timeout: int = 0,
    max_memory_mb: int = 0,
    raw_log_file: IO[bytes] | None = None,
) -> CheckResult:
    """Spawn the Claude subprocess, stream its output, and clean up on exit.

    This is the private implementation of ``run_claude`` after the dry-run and
    logging setup are handled.  It is split out so that ``run_claude`` can
    return early for dry runs without duplicating the cleanup logic.
    """
    process = _spawn_claude_process(cmd, workdir)
    logger.info("Subprocess started: pid=%d, session_id=%d", process.pid, process.pid)
    kill_reason: str | None = None
    # Track session ID so kill_session_stragglers can catch stragglers later.
    previous_session_ids.append(process.pid)
    # Accumulate descendant PIDs during streaming so that setsid-escaped
    # processes can be killed at teardown even if they're no longer in the
    # tree when _kill_process_group runs.
    known_descendants: set[int] = set()
    try:
        check_start_time, kill_reason = _stream_process_output(
            process, idle_timeout, debug,
            check_timeout=check_timeout,
            max_memory_mb=max_memory_mb,
            accumulated_descendant_pids=known_descendants,
            raw_log_file=raw_log_file,
        )

        try:
            # Use a short timeout: streaming is done, so the process should
            # have already exited or been killed.  The finally block will
            # force-kill it if it's still alive.  Using idle_timeout here
            # (default 300s) would block unnecessarily.
            process.wait(timeout=_PROCESS_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning("process.wait() timed out after %ds — killing group (pid=%d)",
                           _PROCESS_WAIT_TIMEOUT, process.pid)
            if kill_reason is None:
                kill_reason = KILL_REASON_IDLE
        except OSError as exc:
            logger.warning("process.wait() failed for pid=%d: %s", process.pid, exc)
    finally:
        # Ensure the entire process group is dead on every exit path.
        # Claude may spawn child processes (language servers, etc.) that
        # survive after the main process exits.
        _kill_process_group(process, extra_pids=known_descendants)

    exit_code = _report_check_exit_status(process, check_start_time)
    if kill_reason is not None:
        logger.warning("Check killed: reason=%s, exit_code=%d, pid=%d",
                       kill_reason, exit_code, process.pid)
    return CheckResult(exit_code=exit_code, kill_reason=kill_reason)


def _report_check_exit_status(process: subprocess.Popen[bytes], check_start_time: float) -> int:
    """Log and display the exit status of a completed check.

    Returns the subprocess exit code (0 on success, -1 if never set).
    """
    elapsed = format_duration(time.time() - check_start_time)
    exit_code = process.returncode
    if exit_code is None:
        logger.warning("Process exited without a return code (may not have terminated cleanly)")
        exit_code = -1
    status_colour = GREEN if exit_code == 0 else YELLOW
    status_text = "completed" if exit_code == 0 else f"exited with code {exit_code}"
    logger.info("Check %s (exit_code=%d, elapsed=%s)", status_text, exit_code, elapsed)
    print_status(f"  Check {status_text} in {elapsed}", status_colour)
    log_memory_usage("after check")
    return exit_code
