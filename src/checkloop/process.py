"""Claude Code subprocess management: spawning, streaming, and cleanup."""

from __future__ import annotations

import logging
import os
import resource
import select
import signal
import subprocess
import sys
import time
from typing import IO, Any

from checkloop.terminal import (
    DIM,
    GREEN,
    RED,
    YELLOW,
    _fatal,
    _format_duration,
    _print_status,
)
from checkloop.streaming import _process_jsonl_buffer

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT = 300  # seconds before killing a silent subprocess
DEFAULT_PAUSE_SECONDS = 2  # seconds between consecutive checks

_READ_CHUNK_SIZE = 8192  # bytes per stdout read during streaming
_DRAIN_CHUNK_SIZE = 65536  # bytes per read when draining after process exit
_MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10 MB safety cap on JSONL output buffer
_PROCESS_WAIT_TIMEOUT = 5  # seconds to wait for process group to die

# Perf: build once instead of copying os.environ on every subprocess spawn.
# Strips CLAUDECODE env var whose presence causes nested claude processes
# to refuse to start when checkloop is invoked from within a Claude Code session.
_SANITIZED_ENV: dict[str, str] = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


# --- Memory and process monitoring -------------------------------------------

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


def _log_memory_usage(label: str) -> None:
    """Log current RSS and child process count after each check."""
    rss_mb = _measure_current_rss_mb()
    child_pids = _find_child_pids()
    logger.info("Memory [%s]: rss=%.0fMB, children=%d", label, rss_mb, len(child_pids))
    _print_status(f"  Memory: {rss_mb:.0f}MB RSS, {len(child_pids)} child processes", DIM)
    if child_pids:
        _warn_and_kill_orphan_processes(child_pids)


def _warn_and_kill_orphan_processes(child_pids: list[int]) -> None:
    """Warn about surviving child processes and kill them."""
    _print_status(f"  Warning: {len(child_pids)} child process(es) still alive — killing.", YELLOW)
    # Pass pids directly to avoid a second pgrep subprocess spawn.
    killed = _kill_orphaned_children(child_pids)
    if killed:
        _print_status(f"  Killed {killed} orphaned process(es).", YELLOW)


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
        _fatal(
            "Error: `claude` not found. Is Claude Code installed?\n"
            "  Install: npm install -g @anthropic-ai/claude-code"
        )
    except OSError as exc:
        _fatal(f"Failed to launch claude subprocess: {exc}")


def _read_stdout_chunk(stdout: IO[bytes]) -> bytes:
    """Read a chunk from stdout, preferring non-blocking read1 when available."""
    try:
        # BufferedReader.read1() returns available data without blocking for the
        # full chunk size. Fall back to os.read() for raw file descriptors.
        read1 = getattr(stdout, "read1", None)
        if read1 is not None:
            return read1(_READ_CHUNK_SIZE)
        return os.read(stdout.fileno(), _READ_CHUNK_SIZE)
    except OSError as exc:
        logger.debug("stdout read failed: %s", exc)
        return b""


def _drain_remaining_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    pass_start_time: float,
    debug: bool,
) -> bytearray:
    """Read all remaining data from stdout after the process has exited."""
    try:
        while True:
            remaining = os.read(stdout.fileno(), _DRAIN_CHUNK_SIZE)
            if not remaining:
                break
            output_buffer.extend(remaining)
            output_buffer = _process_jsonl_buffer(output_buffer, pass_start_time, debug)
    except OSError as exc:
        logger.debug("Failed to drain remaining stdout: %s", exc)
    return output_buffer


def _check_idle_timeout(
    last_output_time: float,
    idle_timeout: int,
    pass_start_time: float,
    process: subprocess.Popen[bytes],
) -> bool:
    """Return True and kill the process if it has been idle too long."""
    now = time.time()
    if now - last_output_time > idle_timeout:
        logger.warning("Idle timeout: pid=%d, idle=%.0fs, elapsed=%s",
                       process.pid, now - last_output_time,
                       _format_duration(now - pass_start_time))
        _print_status(f"\nIdle for {idle_timeout}s — killing "
                     f"(ran {_format_duration(now - pass_start_time)}).", RED)
        _kill_process_group(process)
        return True
    return False


def _flush_and_close_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    pass_start_time: float,
    debug: bool,
) -> None:
    """Flush any remaining partial line and close the stdout pipe."""
    # Append a newline to force any trailing incomplete JSONL line through the parser
    output_buffer.extend(b"\n")
    _process_jsonl_buffer(output_buffer, pass_start_time, debug)
    try:
        stdout.close()
    except OSError as exc:
        logger.debug("Failed to close stdout pipe: %s", exc)


def _stream_process_output(
    process: subprocess.Popen[bytes],
    idle_timeout: int,
    debug: bool,
) -> float:
    """Stream and display JSONL output from the Claude process.

    Kills the process if it produces no output for *idle_timeout* seconds.
    Returns the wall-clock start time used for elapsed-time display.
    """
    if process.stdout is None:
        logger.error("Subprocess stdout is None — cannot stream output (pid=%d)", process.pid)
        return time.time()
    stdout = process.stdout
    pass_start_time = time.time()
    last_output_time = pass_start_time  # idle timer starts from launch
    output_buffer = bytearray()

    try:
        while True:
            if _check_idle_timeout(last_output_time, idle_timeout, pass_start_time, process):
                break

            try:
                # 1s timeout lets us check idle timeout and process exit between reads
                ready, _, _ = select.select([stdout], [], [], 1.0)
            except (OSError, ValueError) as exc:
                logger.debug("select() failed (fd may be closed): %s", exc)
                break

            if not ready:
                if process.poll() is not None:
                    output_buffer = _drain_remaining_stdout(
                        stdout, output_buffer, pass_start_time, debug,
                    )
                    break
                continue

            chunk = _read_stdout_chunk(stdout)
            if not chunk:
                break

            last_output_time = time.time()
            output_buffer.extend(chunk)
            output_buffer = _process_jsonl_buffer(output_buffer, pass_start_time, debug)

            if len(output_buffer) > _MAX_BUFFER_SIZE:
                logger.warning("Output buffer exceeded %d bytes — truncating", _MAX_BUFFER_SIZE)
                output_buffer.clear()

    finally:
        _flush_and_close_stdout(stdout, output_buffer, pass_start_time, debug)

    return pass_start_time


# --- Process group cleanup ----------------------------------------------------

def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    """Send a signal to a process group, ignoring errors if already gone."""
    try:
        os.killpg(pgid, sig)
    except OSError as exc:
        logger.debug("%s to pgid %d failed: %s", sig.name, pgid, exc)


def _kill_session_stragglers(session_id: int) -> None:
    """Kill any processes still alive in the session that escaped the group kill.

    When ``start_new_session=True`` is used, the subprocess becomes the session
    leader (SID = its PID).  Children that call ``setsid()`` or ``setpgid()``
    escape ``os.killpg()`` but remain in the original session.  This function
    catches those stragglers.
    """
    stragglers = _find_session_pids(session_id)
    if not stragglers:
        return
    logger.warning("Found %d straggler(s) in session %d: %s", len(stragglers), session_id, stragglers)
    for pid in stragglers:
        try:
            os.kill(pid, signal.SIGKILL)
            logger.info("Killed session straggler pid=%d", pid)
        except OSError as exc:
            logger.debug("Could not kill straggler %d: %s", pid, exc)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process and its entire process group.

    Sends SIGTERM first (graceful), waits briefly, then SIGKILL if needed.
    This prevents orphaned child processes from leaking memory.
    """
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        pgid = None  # process already gone

    if pgid is not None:
        _signal_process_group(pgid, signal.SIGTERM)
        try:
            process.wait(timeout=_PROCESS_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _signal_process_group(pgid, signal.SIGKILL)
            try:
                process.wait(timeout=_PROCESS_WAIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.warning("Process %d did not exit after SIGKILL", process.pid)

    # Kill any processes that escaped the process group but remain in the
    # session created by start_new_session=True.  The session ID equals the
    # claude PID because setsid() makes it the session leader.
    _kill_session_stragglers(process.pid)


# --- Public API: run a single Claude Code check ------------------------------

def run_claude(
    prompt: str,
    workdir: str,
    *,
    skip_permissions: bool = False,
    dry_run: bool = False,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    debug: bool = False,
) -> int:
    """Run a single Claude Code check.

    Uses ``--output-format stream-json`` so progress events stream in real time.
    There is no hard timeout — the process runs as long as it produces output.
    It is only killed after *idle_timeout* seconds of silence.

    Args:
        prompt: The check prompt to send to Claude Code.
        workdir: Absolute path to the project directory to check.
        skip_permissions: Pass ``--dangerously-skip-permissions`` to Claude Code.
        dry_run: If True, print what would run without invoking Claude.
        idle_timeout: Kill the subprocess after this many seconds of no output.
        debug: Show raw subprocess output lines that fail JSON parsing.

    Returns:
        The subprocess exit code (0 on success).
    """
    cmd = _build_claude_command(prompt, skip_permissions)
    logger.info("run_claude: workdir=%s, prompt_len=%d, skip_permissions=%s, idle_timeout=%d",
                workdir, len(prompt), skip_permissions, idle_timeout)
    _print_status(f"$ {' '.join(cmd[:3])} [prompt omitted for brevity]", DIM)

    if dry_run:
        _print_status(f"[DRY RUN] Would run in {workdir}:", YELLOW)
        truncated = prompt[:120] + ("..." if len(prompt) > 120 else "")
        print(f"  Prompt: {truncated}")
        return 0

    return _execute_claude_process(cmd, workdir, idle_timeout, debug)


def _execute_claude_process(
    cmd: list[str],
    workdir: str,
    idle_timeout: int,
    debug: bool,
) -> int:
    """Spawn the Claude subprocess, stream its output, and clean up.

    Returns the subprocess exit code (0 on success, -1 if it never set one).
    """
    process = _spawn_claude_process(cmd, workdir)
    try:
        pass_start_time = _stream_process_output(process, idle_timeout, debug)

        try:
            process.wait(timeout=idle_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("process.wait() timed out after %ds — killing group", idle_timeout)
    finally:
        # Ensure the entire process group is dead on every exit path.
        # Claude may spawn child processes (language servers, etc.) that
        # survive after the main process exits.
        _kill_process_group(process)

    return _report_check_exit_status(process, pass_start_time)


def _report_check_exit_status(process: subprocess.Popen[bytes], pass_start_time: float) -> int:
    """Log and display the exit status of a completed check.

    Returns the subprocess exit code (0 on success, -1 if never set).
    """
    elapsed = _format_duration(time.time() - pass_start_time)
    exit_code = process.returncode
    if exit_code is None:
        logger.warning("Process exited without a return code (may not have terminated cleanly)")
        exit_code = -1
    status_colour = GREEN if exit_code == 0 else YELLOW
    status_text = "completed" if exit_code == 0 else f"exited with code {exit_code}"
    logger.info("Check %s (exit_code=%d, elapsed=%s)", status_text, exit_code, elapsed)
    _print_status(f"  Check {status_text} in {elapsed}", status_colour)
    _log_memory_usage("after check")
    return exit_code
