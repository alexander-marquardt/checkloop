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
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO


# --- Kill reasons -------------------------------------------------------------

KILL_REASON_MEMORY = "memory"
"""Kill reason when the child process tree exceeds the RSS memory limit."""

KILL_REASON_TIMEOUT = "check_timeout"
"""Kill reason when the hard wall-clock timeout per check is exceeded."""

KILL_REASON_IDLE = "idle_timeout"
"""Kill reason when the subprocess produces no output for too long."""


@dataclass
class CheckResult:
    """Result of a single Claude Code check invocation.

    Attributes:
        exit_code: The subprocess exit code (0 for success, non-zero for failure,
            -1 if the process never set a return code).
        kill_reason: One of the ``KILL_REASON_*`` constants if the process was
            killed, or ``None`` for a normal exit.
    """

    exit_code: int
    kill_reason: str | None = None

# --- Deferred imports (after CheckResult definition) -------------------------
# These imports must appear after CheckResult is defined to break a circular
# dependency chain: monitoring imports from terminal, and process imports from
# monitoring — but CheckResult must exist first because external callers
# import it alongside symbols from monitoring.
from checkloop.monitoring import (
    kill_session_stragglers,
    log_memory_usage,
    measure_session_rss_mb,
    previous_session_ids,
)
from checkloop.terminal import (
    DIM,
    GREEN,
    RED,
    YELLOW,
    fatal,
    format_duration,
    print_status,
)
from checkloop.streaming import process_jsonl_buffer

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT = 300
"""Seconds of silence before killing a subprocess (default for *idle_timeout*)."""

DEFAULT_PAUSE_SECONDS = 2
"""Seconds to pause between consecutive checks (default for *pause*)."""

DEFAULT_MAX_MEMORY_MB = 8192
"""Max child-tree RSS in MB before killing (default for *max_memory_mb*)."""

DEFAULT_CHECK_TIMEOUT = 0
"""Hard wall-clock timeout per check in seconds (default for *check_timeout*).

A value of 0 disables the hard timeout entirely.
"""

_READ_CHUNK_SIZE = 8192  # bytes per stdout read during streaming
_DRAIN_CHUNK_SIZE = 65536  # bytes per read when draining after process exit
_MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10 MB safety cap on JSONL output buffer
_PROCESS_WAIT_TIMEOUT = 5  # seconds to wait for process group to die
_MEMORY_CHECK_INTERVAL = 10  # seconds between child-tree memory checks

# Perf: build once instead of copying os.environ on every subprocess spawn.
# Strips CLAUDECODE env var whose presence causes nested claude processes
# to refuse to start when checkloop is invoked from within a Claude Code session.
_SANITIZED_ENV: dict[str, str] = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


# --- Claude command construction ----------------------------------------------

def _build_claude_command(prompt: str, skip_permissions: bool) -> list[str]:
    """Assemble the CLI command list for invoking Claude Code."""
    cmd = ["claude"]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
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
    """
    logger.info("Spawning subprocess: %s (cwd=%s)", cmd[:3], workdir)
    try:
        return subprocess.Popen(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_SANITIZED_ENV,
            start_new_session=True,  # creates a new process group
        )
    except FileNotFoundError:
        fatal(
            "Error: `claude` not found. Is Claude Code installed?\n"
            "  Install: npm install -g @anthropic-ai/claude-code"
        )
    except OSError as exc:
        fatal(f"Failed to launch claude subprocess: {exc}")


def _read_stdout_chunk(stdout: IO[bytes]) -> bytes:
    """Read a chunk from stdout, preferring non-blocking read1 when available."""
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


def _check_buffer_overflow(output_buffer: bytearray) -> bytearray:
    """Clear the buffer if it exceeds the safety cap, logging a warning."""
    if len(output_buffer) > _MAX_BUFFER_SIZE:
        logger.warning("Output buffer exceeded %d bytes — truncating", _MAX_BUFFER_SIZE)
        output_buffer.clear()
    return output_buffer


def _drain_remaining_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    check_start_time: float,
    debug: bool,
) -> bytearray:
    """Read all remaining data from stdout after the process has exited."""
    try:
        while True:
            remaining = os.read(stdout.fileno(), _DRAIN_CHUNK_SIZE)
            if not remaining:
                break
            output_buffer.extend(remaining)
            output_buffer = process_jsonl_buffer(output_buffer, check_start_time, debug)
            output_buffer = _check_buffer_overflow(output_buffer)
    except OSError as exc:
        logger.debug("Failed to drain remaining stdout: %s", exc)
    return output_buffer


def _check_idle_timeout(
    last_output_time: float,
    idle_timeout: int,
    check_start_time: float,
    process: subprocess.Popen[bytes],
) -> bool:
    """Return True and kill the process if it has been idle too long."""
    now = time.time()
    if now - last_output_time > idle_timeout:
        logger.warning("Idle timeout: pid=%d, idle=%.0fs, elapsed=%s",
                       process.pid, now - last_output_time,
                       format_duration(now - check_start_time))
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
    """Return True and kill the process if the hard wall-clock timeout is exceeded."""
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


def _check_memory_limit(
    session_id: int,
    max_memory_mb: int,
    check_start_time: float,
    process: subprocess.Popen[bytes],
    last_memory_check: float,
) -> tuple[bool, float]:
    """Check child tree RSS against the memory limit.

    Returns ``(should_kill, last_check_time)``.  Only samples every
    ``_MEMORY_CHECK_INTERVAL`` seconds to avoid excessive ``ps`` calls.
    """
    if max_memory_mb <= 0:
        return False, last_memory_check
    now = time.time()
    if now - last_memory_check < _MEMORY_CHECK_INTERVAL:
        return False, last_memory_check
    rss_mb = measure_session_rss_mb(session_id)
    logger.debug("Session %d RSS: %.0f MB (limit: %d MB)", session_id, rss_mb, max_memory_mb)
    if rss_mb > max_memory_mb:
        elapsed = format_duration(now - check_start_time)
        logger.warning("Memory limit exceeded: pid=%d, rss=%.0fMB, limit=%dMB, elapsed=%s",
                       process.pid, rss_mb, max_memory_mb, elapsed)
        print_status(f"\nChild tree using {rss_mb:.0f}MB (limit: {max_memory_mb}MB) "
                     f"after {elapsed} — killing.", RED)
        _kill_process_group(process)
        return True, now
    return False, now


def _flush_and_close_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    check_start_time: float,
    debug: bool,
) -> None:
    """Flush any remaining partial line and close the stdout pipe."""
    # Append a newline to force any trailing incomplete JSONL line through the parser
    output_buffer.extend(b"\n")
    process_jsonl_buffer(output_buffer, check_start_time, debug)
    try:
        stdout.close()
    except OSError as exc:
        logger.debug("Failed to close stdout pipe: %s", exc)


def _stream_process_output(
    process: subprocess.Popen[bytes],
    idle_timeout: int,
    debug: bool,
    *,
    check_timeout: int = 0,
    max_memory_mb: int = 0,
) -> tuple[float, str | None]:
    """Stream and display JSONL output from the Claude process.

    Kills the process if it produces no output for *idle_timeout* seconds,
    if the hard *check_timeout* is exceeded, or if the child process tree
    exceeds *max_memory_mb* of RSS.

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
    output_buffer = bytearray()
    kill_reason: str | None = None

    try:
        while True:
            if _check_idle_timeout(last_output_time, idle_timeout, check_start_time, process):
                kill_reason = KILL_REASON_IDLE
                break

            if _check_hard_timeout(check_start_time, check_timeout, process):
                kill_reason = KILL_REASON_TIMEOUT
                break

            exceeded, last_memory_check = _check_memory_limit(
                process.pid, max_memory_mb, check_start_time, process, last_memory_check,
            )
            if exceeded:
                kill_reason = KILL_REASON_MEMORY
                break

            try:
                # 1s timeout lets us check idle timeout and process exit between reads
                ready, _, _ = select.select([stdout], [], [], 1.0)
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
                continue

            chunk = _read_stdout_chunk(stdout)
            if not chunk:
                logger.debug("EOF on stdout (pid=%d)", process.pid)
                break

            last_output_time = time.time()
            output_buffer.extend(chunk)
            output_buffer = process_jsonl_buffer(output_buffer, check_start_time, debug)
            output_buffer = _check_buffer_overflow(output_buffer)

    finally:
        _flush_and_close_stdout(stdout, output_buffer, check_start_time, debug)

    return check_start_time, kill_reason


# --- Process group cleanup ----------------------------------------------------

def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    """Send a signal to a process group, ignoring errors if already gone."""
    try:
        os.killpg(pgid, sig)
    except OSError as exc:
        logger.debug("%s to pgid %d failed: %s", sig.name, pgid, exc)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process and its entire process group.

    Sends SIGTERM first (graceful), waits briefly, then SIGKILL if needed.
    This prevents orphaned child processes from leaking memory.
    """
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        # Process already gone — just clean up session stragglers.
        kill_session_stragglers(process.pid)
        return

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
    cmd = _build_claude_command(prompt, skip_permissions)
    logger.info("run_claude: workdir=%s, prompt_len=%d, skip_permissions=%s, idle_timeout=%d, "
                "check_timeout=%d, max_memory_mb=%d",
                workdir, len(prompt), skip_permissions, idle_timeout, check_timeout, max_memory_mb)
    logger.debug("run_claude prompt: %.1000s", prompt)
    print_status(f"$ {' '.join(cmd[:3])} [prompt omitted for brevity]", DIM)

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
    )


def _execute_claude_process(
    cmd: list[str],
    workdir: str,
    *,
    idle_timeout: int,
    debug: bool,
    check_timeout: int = 0,
    max_memory_mb: int = 0,
) -> CheckResult:
    """Spawn the Claude subprocess, stream its output, and clean up.

    Returns a ``CheckResult`` with exit code and kill reason.
    """
    process = _spawn_claude_process(cmd, workdir)
    kill_reason: str | None = None
    # Track session ID so kill_session_stragglers can catch stragglers later.
    previous_session_ids.append(process.pid)
    try:
        check_start_time, kill_reason = _stream_process_output(
            process, idle_timeout, debug,
            check_timeout=check_timeout,
            max_memory_mb=max_memory_mb,
        )

        try:
            process.wait(timeout=idle_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("process.wait() timed out after %ds — killing group (pid=%d)",
                           idle_timeout, process.pid)
            if kill_reason is None:
                kill_reason = KILL_REASON_IDLE
    finally:
        # Ensure the entire process group is dead on every exit path.
        # Claude may spawn child processes (language servers, etc.) that
        # survive after the main process exits.
        _kill_process_group(process)

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
