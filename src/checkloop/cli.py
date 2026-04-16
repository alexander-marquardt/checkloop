#!/usr/bin/env python3
"""
checkloop — CLI entry point and run coordination.

Parses arguments (via ``cli_args``), sets up logging and signal handlers,
offers checkpoint resume, and delegates to the check suite.

Usage:
    checkloop --dir ~/my-project                        # basic plan (default)
    checkloop --dir ~/my-project --plan thorough        # thorough plan
    checkloop --dir ~/my-project --plan exhaustive --cycles 3
    checkloop --dir ~/my-project --checks readability dry tests
    checkloop --dir ~/my-project --plan ./my-plan.toml  # your own plan file
    checkloop --dir ~/my-project --dry-run              # preview without running
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import sys
import types
import uuid
from pathlib import Path

from checkloop.checks import CheckDef
from checkloop.checkpoint import (
    CheckpointData,
    clear_checkpoint,
    load_checkpoint,
    prompt_resume,
)
from checkloop.cli_args import (
    DEFAULT_CONVERGENCE_THRESHOLD,
    build_argument_parser,
    display_pre_run_warning,
    print_run_summary,
    resolve_changed_files_prefix,
    resolve_selected_checks,
    resolve_working_directory,
    validate_arguments,
    warn_if_mypy_unavailable,
)
from checkloop.monitoring import cleanup_all_sessions
from checkloop.suite import run_suite_with_error_handling
from checkloop.terminal import YELLOW, fatal, print_status

logger = logging.getLogger(__name__)


# --- Logging configuration ----------------------------------------------------

_LOG_DATEFMT = "%H:%M:%S"

# Generated once per process; included in every log line so entries from a
# single invocation can be correlated across modules and in the log file.
_RUN_ID: str = uuid.uuid4().hex[:8]

_LOG_FORMAT = f"%(asctime)s [%(levelname)s] [run={_RUN_ID}] %(name)s: %(message)s"


def _configure_logging(args: argparse.Namespace) -> None:
    debug: bool = getattr(args, "debug", False)
    verbose: bool = getattr(args, "verbose", False)
    if debug:
        log_level = logging.DEBUG
    elif verbose:
        log_level = logging.INFO
    else:
        log_level = logging.WARNING
    logging.basicConfig(
        level=log_level,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATEFMT,
    )


_LOG_RETENTION = 3
"""Number of previous log files to keep (e.g. .log.1, .log.2, .log.3)."""


def _rotate_log_file(log_path: Path) -> None:
    """Rotate previous log files so the last N runs are preserved.

    Shifts ``.log.1`` → ``.log.2`` etc., then moves the current log to
    ``.log.1``.  Oldest files beyond ``_LOG_RETENTION`` are deleted.
    Errors are silently ignored — log rotation is best-effort.
    """
    # Shift existing rotated logs up by one.
    for i in range(_LOG_RETENTION, 0, -1):
        src = log_path.with_suffix(f".log.{i}")
        dst = log_path.with_suffix(f".log.{i + 1}")
        try:
            if i == _LOG_RETENTION:
                src.unlink(missing_ok=True)  # drop oldest
            elif src.exists():
                src.rename(dst)
        except OSError:
            pass
    # Rotate current → .log.1
    try:
        if log_path.exists():
            log_path.rename(log_path.with_suffix(".log.1"))
    except OSError:
        pass


def _add_file_log_handler(workdir: str) -> None:
    """Add a DEBUG-level file handler that captures everything to a log file.

    The log file is written to ``<workdir>/.checkloop-run.log``.  Previous
    logs are rotated to ``.log.1``, ``.log.2``, etc. (up to
    ``_LOG_RETENTION``) so that diagnostics from recent runs survive even
    if the current run overwrites the main log.

    Permissions are restricted to owner-only (0600) because the log may
    contain prompt text, file paths, and other potentially sensitive data.
    """
    log_path = Path(workdir) / ".checkloop-run.log"
    _rotate_log_file(log_path)
    try:
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        file_handler = logging.FileHandler(str(log_path), mode="a", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not create log file %s: %s", log_path, exc)
        return
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    logging.getLogger().addHandler(file_handler)
    logger.info("Log file: %s", log_path)


# --- Checkpoint resume --------------------------------------------------------

def _resolve_path_safe(path_str: str) -> str | None:
    """Return the resolved absolute path as a string, or None on error or empty input."""
    if not path_str:
        return None
    try:
        return str(Path(path_str).resolve())
    except OSError as exc:
        logger.warning("Cannot resolve path '%s': %s", path_str, exc)
        return None


def _validate_checkpoint_match(
    checkpoint: CheckpointData,
    workdir: str,
    selected_checks: list[CheckDef],
) -> str | None:
    """Check that a checkpoint matches the current run configuration.

    Returns a human-readable mismatch reason, or None if the checkpoint is valid.
    """
    resolved_saved = _resolve_path_safe(checkpoint.get("workdir", ""))
    if resolved_saved is None:
        return "Checkpoint has invalid workdir"
    resolved_current = _resolve_path_safe(workdir)
    if resolved_current is None:
        return "Cannot resolve current workdir"
    if resolved_saved != resolved_current:
        logger.warning("Checkpoint workdir mismatch: saved=%s, current=%s", resolved_saved, resolved_current)
        return "Checkpoint found but workdir differs"

    saved_ids = checkpoint.get("check_ids", [])
    current_ids = [c["id"] for c in selected_checks]
    if saved_ids != current_ids:
        return "Checkpoint found but check selection differs"

    return None


def _try_resume_from_checkpoint(
    workdir: str,
    selected_checks: list[CheckDef],
) -> CheckpointData | None:
    """Check for a checkpoint and prompt the user to resume.

    Returns the checkpoint data if the user chooses to resume, or None to
    start fresh.  If the checkpoint's check selection doesn't match the
    current run, it is discarded automatically.
    """
    checkpoint = load_checkpoint(workdir)
    if checkpoint is None:
        return None

    mismatch = _validate_checkpoint_match(checkpoint, workdir, selected_checks)
    if mismatch:
        print_status(f"{mismatch} — starting fresh.", YELLOW)
        clear_checkpoint(workdir)
        return None

    if prompt_resume(workdir):
        return checkpoint

    clear_checkpoint(workdir)
    return None


# --- Signal and cleanup registration -----------------------------------------

def _register_cleanup_handlers() -> None:
    """Register atexit and signal handlers for subprocess tree cleanup.

    Ensures child processes are terminated on normal exit, sys.exit(),
    Ctrl+C, SIGTERM, or terminal close (SIGHUP).
    """
    atexit.register(cleanup_all_sessions)

    def _exit_handler(signum: int, frame: types.FrameType | None) -> None:
        # Avoid calling logger here — logging acquires locks internally,
        # so calling it from a signal handler can deadlock if the signal
        # interrupts the main thread while it holds a logging lock.
        sys.exit(128 + signum)

    def _sigint_handler(signum: int, frame: types.FrameType | None) -> None:
        # For SIGINT (Ctrl+C), raise KeyboardInterrupt so it propagates
        # through any blocking syscalls. This ensures select(), subprocess.run(),
        # and other blocking operations are interrupted immediately.
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _exit_handler)
        except OSError as exc:
            logger.warning("Could not register handler for %s: %s", sig.name, exc)

    # Explicitly handle SIGINT to ensure KeyboardInterrupt is raised even
    # during blocking C-level syscalls where Python's default handler might
    # be deferred.
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except OSError as exc:
        logger.warning("Could not register SIGINT handler: %s", exc)


# --- Entry point --------------------------------------------------------------

def main() -> None:
    _register_cleanup_handlers()

    args = build_argument_parser().parse_args()
    _configure_logging(args)
    workdir = resolve_working_directory(args.dir)
    _add_file_log_handler(workdir)
    logger.info("checkloop started: run_id=%s, workdir=%s", _RUN_ID, workdir)
    validate_arguments(args)

    args.changed_files_prefix = resolve_changed_files_prefix(args, workdir)

    selected_checks = resolve_selected_checks(args)
    if not selected_checks:
        fatal("No checks selected. Check your --checks or --plan arguments.")
    num_cycles = args.cycles
    total_steps = len(selected_checks) * num_cycles
    convergence_threshold = args.convergence_threshold

    print_run_summary(
        workdir, selected_checks, num_cycles, total_steps,
        args.idle_timeout, args.dry_run,
        convergence_threshold=convergence_threshold,
        max_memory_mb=args.max_memory_mb, check_timeout=args.check_timeout,
    )
    warn_if_mypy_unavailable(workdir)

    check_names = ", ".join(check["id"] for check in selected_checks)
    logger.info(
        "Suite started: workdir=%s, checks=[%s], cycles=%d, idle_timeout=%d, convergence=%.2f%%",
        workdir, check_names, num_cycles, args.idle_timeout, convergence_threshold,
    )

    if not args.dry_run:
        display_pre_run_warning(args.dangerously_skip_permissions)

    # Check for a previous incomplete run and offer to resume.
    resume_from = None
    if not args.dry_run and not args.no_resume:
        resume_from = _try_resume_from_checkpoint(workdir, selected_checks)

    run_suite_with_error_handling(
        selected_checks, num_cycles, workdir, args,
        convergence_threshold, resume_from=resume_from,
    )
    logger.info("checkloop finished (run_id=%s)", _RUN_ID)


if __name__ == "__main__":  # pragma: no cover
    main()
