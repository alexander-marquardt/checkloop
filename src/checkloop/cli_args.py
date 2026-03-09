"""CLI argument parsing, validation, resolution, and pre-run display.

Defines the argument parser, validates user-provided values, resolves
check selections and working directory, and displays the pre-run summary
and permission warning.  Separated from ``cli.py`` so the entry-point
module focuses on orchestration while this module owns the argument contract.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
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
    build_changed_files_prefix,
    detect_default_branch,
    get_changed_files,
    is_git_repo,
)
from checkloop.process import (
    DEFAULT_CHECK_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_MEMORY_MB,
    DEFAULT_PAUSE_SECONDS,
)
from checkloop.terminal import BOLD, RED, RESET, RULE_WIDTH, YELLOW, fatal, print_status

logger = logging.getLogger(__name__)

DEFAULT_CONVERGENCE_THRESHOLD = 0.1
"""Percent of total lines changed below which cycles stop early (default for ``--convergence-threshold``)."""


# --- Argument parsing ---------------------------------------------------------

def build_argument_parser() -> argparse.ArgumentParser:
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

def print_run_summary(
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

def resolve_working_directory(dir_arg: str) -> str:
    """Resolve and validate the --dir argument, exiting on error."""
    try:
        workdir = str(Path(dir_arg).resolve())
    except OSError as exc:
        fatal(f"Cannot resolve directory '{dir_arg}': {exc}")
    if not Path(workdir).is_dir():
        fatal(f"Directory not found: {workdir}")
    return workdir


def validate_arguments(args: argparse.Namespace) -> None:
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


def resolve_changed_files_prefix(args: argparse.Namespace, workdir: str) -> str:
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


def resolve_selected_checks(args: argparse.Namespace) -> list[CheckDef]:
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


def display_pre_run_warning(skip_permissions: bool) -> None:
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
