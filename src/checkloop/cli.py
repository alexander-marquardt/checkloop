#!/usr/bin/env python3
"""
checkloop — Autonomous multi-check code review using Claude Code.

Runs a configurable suite of focused checks (readability, DRY, tests, security,
etc.) over an existing codebase. Point it at a directory and walk away.

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
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from checkloop.checks import (
    CHECK_IDS,
    CHECKS,
    CheckDef,
    DEFAULT_TIER,
    TIER_BASIC,
    TIER_EXHAUSTIVE,
    TIER_THOROUGH,
    TIERS,
)
from checkloop.git import (
    detect_default_branch,
    get_changed_files,
    build_changed_files_prefix,
    is_git_repo,
)
from checkloop.checkpoint import CheckpointData, clear_checkpoint, load_checkpoint, prompt_resume
from checkloop.monitoring import cleanup_all_sessions
from checkloop.process import (
    DEFAULT_CHECK_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_MEMORY_MB,
    DEFAULT_PAUSE_SECONDS,
)
from checkloop.suite import run_suite_with_error_handling
from checkloop.terminal import BOLD, RED, RESET, RULE_WIDTH, YELLOW, fatal, print_status

logger = logging.getLogger(__name__)

DEFAULT_CONVERGENCE_THRESHOLD = 0.1
"""Percent of total lines changed below which cycles stop early (default for ``--convergence-threshold``)."""


# --- Argument parsing ---------------------------------------------------------

def _build_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the CLI argument parser."""
    tier_names = ", ".join(TIERS)
    parser = argparse.ArgumentParser(
        description="Autonomous multi-check code review using Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Check tiers:",
            f"  basic       {', '.join(TIER_BASIC)}",
            f"  thorough    basic + {', '.join(cid for cid in TIER_THOROUGH if cid not in TIER_BASIC)}",
            f"  exhaustive  thorough + {', '.join(cid for cid in TIER_EXHAUSTIVE if cid not in TIER_THOROUGH)}",
            "",
            "All available checks (use with --checks to override tier):",
            *(f"  {check['id']:14s}  {check['label']}" for check in CHECKS),
            "",
            "Examples:",
            "  checkloop --dir .                                  # basic tier (default)",
            "  checkloop --dir ~/proj --level thorough            # thorough tier",
            "  checkloop --dir ~/proj --level exhaustive --cycles 2",
            "  checkloop --dir ~/proj --checks readability security",
            "  checkloop --dir ~/proj --all-checks                # same as --level exhaustive",
            "  checkloop --dir ~/proj --dry-run",
        ]),
    )

    parser.add_argument(
        "--dir", "-d", required=True,
        help="Project directory to check",
    )
    parser.add_argument(
        "--level", "-l", choices=list(TIERS), default=None,
        metavar="TIER",
        help=f"Check depth: {tier_names} (default: basic)",
    )
    parser.add_argument(
        "--checks", nargs="+", choices=CHECK_IDS, default=None,
        metavar="CHECK",
        help="Manually select checks (overrides --level)",
    )
    parser.add_argument(
        "--all-checks", action="store_true",
        help="Run every available check (same as --level exhaustive)",
    )
    parser.add_argument(
        "--cycles", "-c", type=int, default=1, metavar="N",
        help="Repeat the full suite N times (default: 1)",
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT, metavar="SECS",
        help=f"Kill a check after this many seconds of silence (default: {DEFAULT_IDLE_TIMEOUT})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would run without invoking Claude",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show operational events, timing, and memory info (INFO-level logging)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show all details including raw subprocess output (DEBUG-level logging)",
    )
    parser.add_argument(
        "--pause", type=int, default=DEFAULT_PAUSE_SECONDS, metavar="SECS",
        help=f"Seconds to pause between checks (default: {DEFAULT_PAUSE_SECONDS})",
    )
    parser.add_argument(
        "--dangerously-skip-permissions", action="store_true",
        help="Pass --dangerously-skip-permissions to Claude Code (bypasses all permission checks)",
    )
    parser.add_argument(
        "--changed-only", nargs="?", const="auto", default=None, metavar="REF",
        help=(
            "Only check files that changed compared to a base ref. "
            "With no argument, auto-detects main/master. "
            "Pass a branch or SHA to compare against (e.g. --changed-only develop)."
        ),
    )
    parser.add_argument(
        "--convergence-threshold", type=float, default=DEFAULT_CONVERGENCE_THRESHOLD,
        metavar="PCT",
        help=(
            f"Stop cycling early when less than PCT%% of total lines changed "
            f"in a cycle (default: {DEFAULT_CONVERGENCE_THRESHOLD}). "
            "Requires a git repo. Set to 0 to disable convergence detection."
        ),
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Ignore any existing checkpoint and start fresh (no resume prompt).",
    )
    parser.add_argument(
        "--max-memory-mb", type=int, default=DEFAULT_MAX_MEMORY_MB, metavar="MB",
        help=(
            f"Kill a check if its child process tree exceeds this many MB of RSS "
            f"(default: {DEFAULT_MAX_MEMORY_MB}). Set to 0 to disable."
        ),
    )
    parser.add_argument(
        "--check-timeout", type=int, default=DEFAULT_CHECK_TIMEOUT, metavar="SECS",
        help=(
            f"Hard wall-clock timeout per check in seconds "
            f"(default: {DEFAULT_CHECK_TIMEOUT}, 0 = no limit). "
            "Unlike --idle-timeout, this kills even actively-running checks."
        ),
    )

    return parser


# --- Pre-run summary ----------------------------------------------------------

def _print_run_summary(
    workdir: str,
    selected_checks: list[CheckDef],
    num_cycles: int,
    total_steps: int,
    idle_timeout: int,
    dry_run: bool,
    convergence_threshold: float = 0.0,
    max_memory_mb: int = 0,
    check_timeout: int = 0,
) -> None:
    """Print a summary of the configured check run before starting."""
    print(f"\n{BOLD}checkloop{RESET}")
    print(f"  Directory    : {workdir}")
    print(f"  Checks       : {', '.join(check['id'] for check in selected_checks)}")
    print(f"  Cycles       : {num_cycles} (max)")
    print(f"  Total steps  : {total_steps}  ({len(selected_checks)} checks x {num_cycles} cycle{'s' if num_cycles != 1 else ''}) max")
    print(f"  Idle timeout : {idle_timeout}s")
    if check_timeout > 0:
        print(f"  Check timeout: {check_timeout}s (hard wall-clock limit)")
    if max_memory_mb > 0:
        print(f"  Memory limit : {max_memory_mb}MB per check (child tree RSS)")
    if convergence_threshold > 0:
        print(f"  Convergence  : stop when < {convergence_threshold}% of lines change")
    if dry_run:
        print_status("  DRY RUN", YELLOW)


# --- Validation and resolution ------------------------------------------------

def _resolve_working_directory(dir_arg: str) -> str:
    """Resolve and validate the --dir argument, exiting on error."""
    try:
        workdir = str(Path(dir_arg).resolve())
    except OSError as exc:
        fatal(f"Cannot resolve directory '{dir_arg}': {exc}")
    if not Path(workdir).is_dir():
        fatal(f"Directory not found: {workdir}")
    return workdir


def _validate_arguments(args: argparse.Namespace) -> None:
    """Exit with an error if any CLI arguments have invalid values."""
    if args.idle_timeout < 1:
        fatal("--idle-timeout must be at least 1 second")
    if args.pause < 0:
        fatal("--pause cannot be negative")
    if args.cycles < 1:
        fatal("--cycles must be at least 1")
    if args.convergence_threshold < 0:
        fatal("--convergence-threshold cannot be negative")
    if args.convergence_threshold > 100:
        fatal("--convergence-threshold cannot exceed 100")
    if args.max_memory_mb < 0:
        fatal("--max-memory-mb cannot be negative")
    if args.check_timeout < 0:
        fatal("--check-timeout cannot be negative")


def _resolve_changed_files_prefix(args: argparse.Namespace, workdir: str) -> str:
    """Resolve --changed-only into a prompt prefix, or return empty string."""
    if args.changed_only is None:
        return ""
    if not is_git_repo(workdir):
        fatal("--changed-only requires a git repository")
    base_ref = args.changed_only if args.changed_only != "auto" else detect_default_branch(workdir)
    logger.info("--changed-only: comparing against base ref '%s'", base_ref)
    changed_files = get_changed_files(workdir, base_ref)
    if not changed_files:
        fatal(f"No changed files found compared to '{base_ref}'")
    print(f"  Reviewing {len(changed_files)} changed file(s) (vs {base_ref})")
    return build_changed_files_prefix(changed_files)


def _resolve_selected_checks(args: argparse.Namespace) -> list[CheckDef]:
    """Determine which checks to run based on CLI arguments."""
    if args.all_checks:
        selected_ids = set(CHECK_IDS)
    elif args.checks:
        selected_ids = set(args.checks)
    else:
        selected_ids = set(TIERS[args.level or DEFAULT_TIER])
    selected = [check for check in CHECKS if check["id"] in selected_ids]
    logger.info("Selected %d checks: %s", len(selected), [check["id"] for check in selected])
    return selected


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

    # Guard against a checkpoint from a different project directory being
    # loaded (e.g. if the file was copied or the directory was renamed).
    saved_workdir = checkpoint.get("workdir", "")
    try:
        resolved_saved = str(Path(saved_workdir).resolve()) if saved_workdir else ""
    except OSError as exc:
        logger.warning("Cannot resolve saved checkpoint workdir '%s': %s", saved_workdir, exc)
        print_status("Checkpoint has invalid workdir — starting fresh.", YELLOW)
        clear_checkpoint(workdir)
        return None
    resolved_current = str(Path(workdir).resolve())
    if resolved_saved != resolved_current:
        print_status("Checkpoint found but workdir differs — starting fresh.", YELLOW)
        logger.warning("Checkpoint workdir mismatch: saved=%s, current=%s", resolved_saved, resolved_current)
        clear_checkpoint(workdir)
        return None

    saved_ids = checkpoint.get("check_ids", [])
    current_ids = [c["id"] for c in selected_checks]
    if saved_ids != current_ids:
        print_status("Checkpoint found but check selection differs — starting fresh.", YELLOW)
        clear_checkpoint(workdir)
        return None

    if prompt_resume(workdir):
        return checkpoint

    clear_checkpoint(workdir)
    return None


_LOG_DATEFMT = "%H:%M:%S"

# Generated once per process; included in every log line so entries from a
# single invocation can be correlated across modules and in the log file.
_RUN_ID: str = uuid.uuid4().hex[:8]

_LOG_FORMAT = f"%(asctime)s [%(levelname)s] [run={_RUN_ID}] %(name)s: %(message)s"


def _configure_logging(args: argparse.Namespace) -> None:
    """Set up logging based on --verbose / --debug flags."""
    if args.debug:
        log_level = logging.DEBUG
    elif args.verbose:
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


# --- Pre-run warning ----------------------------------------------------------

_WARNING_COUNTDOWN_SECONDS = 5  # countdown seconds before starting review


class _PermissionWarning(NamedTuple):
    """Components of the pre-run permission warning message."""

    colour: str
    heading: str
    body_lines: list[str]
    countdown_message: str


def _build_permission_warning(skip_permissions: bool) -> _PermissionWarning:
    """Build the warning message components based on permission mode."""
    if skip_permissions:
        return _PermissionWarning(
            colour=RED,
            heading="WARNING: --dangerously-skip-permissions is ENABLED",
            body_lines=[
                "Claude Code will execute ALL actions without asking for approval.",
                "This includes writing files, running shell commands, and deleting code.",
                "Make sure you have committed or backed up your work before proceeding.",
            ],
            countdown_message=f"Starting in {_WARNING_COUNTDOWN_SECONDS} seconds (Ctrl+C to abort)...",
        )
    return _PermissionWarning(
        colour=YELLOW,
        heading="WARNING: Running without --dangerously-skip-permissions",
        body_lines=[
            "Claude Code requires interactive permission prompts to write files,",
            "but checkloop cannot relay those prompts (stdin is disconnected).",
            "Checks that modify code will likely FAIL or HANG.",
            "",
            "Re-run with:",
            f"  {BOLD}checkloop --dangerously-skip-permissions ...{RESET}{YELLOW}",
        ],
        countdown_message=f"Continuing anyway in {_WARNING_COUNTDOWN_SECONDS} seconds (Ctrl+C to abort)...",
    )


def _display_pre_run_warning(skip_permissions: bool) -> None:
    """Show a warning about permissions and count down before starting.

    With ``--dangerously-skip-permissions``, warns that all actions will run
    without approval.  Without it, warns that checks will likely hang because
    checkloop cannot relay interactive permission prompts.  Either way, the
    user has 5 seconds to Ctrl+C before the suite begins.
    """
    warning = _build_permission_warning(skip_permissions)

    print(f"\n{warning.colour}{BOLD}{'=' * RULE_WIDTH}")
    print(f"  {warning.heading}")
    print(f"{'=' * RULE_WIDTH}{RESET}")
    for line in warning.body_lines:
        print(f"{warning.colour}  {line}{RESET}")
    print(f"\n{warning.colour}  {warning.countdown_message}{RESET}")

    try:
        time.sleep(_WARNING_COUNTDOWN_SECONDS)
    except KeyboardInterrupt:
        print_status("\nAborted.")
        sys.exit(0)


# --- Entry point --------------------------------------------------------------

def main() -> None:
    """CLI entry point: parse arguments and run the configured check suite.

    This is the function invoked by the ``checkloop`` console script defined
    in ``pyproject.toml``.  It parses CLI flags, resolves the check tier and
    check list, displays a pre-run summary, then delegates to the check loop.
    """
    # Ensure subprocess trees are cleaned up on any exit path: normal exit,
    # sys.exit(), Ctrl+C, SIGTERM, or terminal close (SIGHUP).
    atexit.register(cleanup_all_sessions)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        def _signal_handler(signum: int, frame: object) -> None:
            logger.info("Received signal %d — exiting", signum)
            sys.exit(128 + signum)
        try:
            signal.signal(sig, _signal_handler)
        except OSError as exc:
            logger.warning("Could not register handler for %s: %s", sig.name, exc)

    args = _build_argument_parser().parse_args()
    _configure_logging(args)
    logger.info("checkloop started (run_id=%s)", _RUN_ID)
    logger.debug("argv: %s", sys.argv)

    workdir = _resolve_working_directory(args.dir)
    _add_file_log_handler(workdir)
    _validate_arguments(args)

    args.changed_files_prefix = _resolve_changed_files_prefix(args, workdir)

    selected_checks = _resolve_selected_checks(args)
    if not selected_checks:
        fatal("No checks selected. Check your --checks or --level arguments.")
    num_cycles = args.cycles
    total_steps = len(selected_checks) * num_cycles
    convergence_threshold = args.convergence_threshold

    _print_run_summary(
        workdir, selected_checks, num_cycles, total_steps,
        args.idle_timeout, args.dry_run, convergence_threshold,
        max_memory_mb=args.max_memory_mb, check_timeout=args.check_timeout,
    )

    logger.info(
        "Suite started: workdir=%s, checks=[%s], cycles=%d, idle_timeout=%d, convergence=%.2f%%",
        workdir,
        ", ".join(check["id"] for check in selected_checks),
        num_cycles,
        args.idle_timeout,
        convergence_threshold,
    )

    logger.debug(
        "Resolved config: workdir=%s, checks=[%s], cycles=%d, idle_timeout=%d, "
        "check_timeout=%d, max_memory_mb=%d, convergence=%.2f%%, dry_run=%s, "
        "skip_permissions=%s, changed_only=%s",
        workdir,
        ", ".join(check["id"] for check in selected_checks),
        num_cycles, args.idle_timeout, args.check_timeout, args.max_memory_mb,
        convergence_threshold, args.dry_run, args.dangerously_skip_permissions,
        args.changed_only,
    )

    if not args.dry_run:
        _display_pre_run_warning(args.dangerously_skip_permissions)

    # Check for a previous incomplete run and offer to resume.
    resume_from = None
    if not args.dry_run and not args.no_resume:
        resume_from = _try_resume_from_checkpoint(workdir, selected_checks)

    run_suite_with_error_handling(
        selected_checks, num_cycles, workdir, args,
        convergence_threshold, resume_from=resume_from,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
