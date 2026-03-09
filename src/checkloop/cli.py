#!/usr/bin/env python3
"""
checkloop — CLI entry point and run coordination.

Parses arguments (via ``cli_args``), sets up logging and signal handlers,
offers checkpoint resume, and delegates to the check suite.

Usage:
    checkloop --dir ~/my-project                        # basic tier review
    checkloop --dir ~/my-project --level thorough       # thorough review
    checkloop --dir ~/my-project --cycles 3             # repeat the full suite 3x
    checkloop --dir ~/my-project --checks readability dry tests
    checkloop --dir ~/my-project --all-checks --cycles 2
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


def _add_file_log_handler(workdir: str) -> None:
    """Add a DEBUG-level file handler that captures everything to a log file.

    The log file is written to ``<workdir>/.checkloop-run.log`` and is
    overwritten on each run so it always reflects the most recent session.
    Permissions are restricted to owner-only (0600) because the log may
    contain prompt text, file paths, and other potentially sensitive data.
    """
    log_path = Path(workdir) / ".checkloop-run.log"
    try:
        # Create/truncate with restricted permissions (owner read/write only)
        # before handing off to FileHandler. The log captures DEBUG-level
        # content including prompts and file paths that should not be
        # world-readable on shared systems.
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
    for sig in (signal.SIGTERM, signal.SIGHUP):
        def _signal_handler(signum: int, frame: types.FrameType | None) -> None:
            # Avoid calling logger here — logging acquires locks internally,
            # so calling it from a signal handler can deadlock if the signal
            # interrupts the main thread while it holds a logging lock.
            sys.exit(128 + signum)
        try:
            signal.signal(sig, _signal_handler)
        except OSError as exc:
            logger.warning("Could not register handler for %s: %s", sig.name, exc)


# --- Entry point --------------------------------------------------------------

def main() -> None:
    """CLI entry point: parse arguments and run the configured check suite.

    This is the function invoked by the ``checkloop`` console script defined
    in ``pyproject.toml``.  It parses CLI flags, resolves the check tier and
    check list, displays a pre-run summary, then delegates to the check loop.
    """
    _register_cleanup_handlers()

    args = build_argument_parser().parse_args()
    _configure_logging(args)
    logger.info("checkloop started (run_id=%s)", _RUN_ID)
    workdir = resolve_working_directory(args.dir)
    _add_file_log_handler(workdir)
    validate_arguments(args)

    args.changed_files_prefix = resolve_changed_files_prefix(args, workdir)

    selected_checks = resolve_selected_checks(args)
    if not selected_checks:
        fatal("No checks selected. Check your --checks or --level arguments.")
    num_cycles = args.cycles
    total_steps = len(selected_checks) * num_cycles
    convergence_threshold = args.convergence_threshold

    print_run_summary(
        workdir, selected_checks, num_cycles, total_steps,
        args.idle_timeout, args.dry_run,
        convergence_threshold=convergence_threshold,
        max_memory_mb=args.max_memory_mb, check_timeout=args.check_timeout,
    )

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
