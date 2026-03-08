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
import logging
from pathlib import Path

from checkloop.checks import (
    CHECK_IDS,
    CHECKS,
    DEFAULT_TIER,
    TIER_BASIC,
    TIER_EXHAUSTIVE,
    TIER_THOROUGH,
    TIERS,
)
from checkloop.git import (
    _detect_default_branch,
    _get_changed_files,
    _build_changed_files_prefix,
    _is_git_repo,
)
from checkloop.process import DEFAULT_IDLE_TIMEOUT, DEFAULT_PAUSE_SECONDS
from checkloop.suite import (
    _display_pre_run_warning,
    _run_suite_with_error_handling,
)
from checkloop.terminal import BOLD, RESET, YELLOW, _fatal, _print_status

logger = logging.getLogger(__name__)

DEFAULT_CONVERGENCE_THRESHOLD = 0.1  # percent of total lines changed


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
        "--converged-at-percentage", type=float, default=DEFAULT_CONVERGENCE_THRESHOLD,
        metavar="PCT",
        help=(
            f"Stop cycling early when less than PCT%% of total lines changed "
            f"in a cycle (default: {DEFAULT_CONVERGENCE_THRESHOLD}). "
            "Requires a git repo. Set to 0 to disable convergence detection."
        ),
    )

    return parser


# --- Pre-run summary ----------------------------------------------------------

def _print_run_summary(
    workdir: str,
    selected_checks: list[dict[str, str]],
    num_cycles: int,
    total_steps: int,
    idle_timeout: int,
    dry_run: bool,
    convergence_threshold: float = 0.0,
) -> None:
    """Print a summary of the configured check run before starting."""
    print(f"\n{BOLD}checkloop{RESET}")
    print(f"  Directory    : {workdir}")
    print(f"  Checks       : {', '.join(check['id'] for check in selected_checks)}")
    print(f"  Cycles       : {num_cycles} (max)")
    print(f"  Total steps  : {total_steps}  ({len(selected_checks)} checks x {num_cycles} cycle{'s' if num_cycles != 1 else ''}) max")
    print(f"  Idle timeout : {idle_timeout}s (no hard limit)")
    if convergence_threshold > 0:
        print(f"  Convergence  : stop when < {convergence_threshold}% of lines change")
    if dry_run:
        _print_status("  DRY RUN", YELLOW)


# --- Validation and resolution ------------------------------------------------

def _resolve_working_directory(dir_arg: str) -> str:
    """Resolve and validate the --dir argument, exiting on error."""
    try:
        workdir = str(Path(dir_arg).resolve())
    except OSError as exc:
        _fatal(f"Cannot resolve directory '{dir_arg}': {exc}")
    if not Path(workdir).is_dir():
        _fatal(f"Directory not found: {workdir}")
    return workdir


def _validate_arguments(args: argparse.Namespace) -> None:
    """Exit with an error if any CLI arguments have invalid values."""
    if args.idle_timeout < 1:
        _fatal("--idle-timeout must be at least 1 second")
    if args.pause < 0:
        _fatal("--pause cannot be negative")
    if args.cycles < 1:
        _fatal("--cycles must be at least 1")
    if args.converged_at_percentage < 0:
        _fatal("--converged-at-percentage cannot be negative")
    if args.converged_at_percentage > 100:
        _fatal("--converged-at-percentage cannot exceed 100")


def _resolve_changed_files_prefix(args: argparse.Namespace, workdir: str) -> str:
    """Resolve --changed-only into a prompt prefix, or return empty string."""
    if args.changed_only is None:
        return ""
    if not _is_git_repo(workdir):
        _fatal("--changed-only requires a git repository")
    base_ref = args.changed_only if args.changed_only != "auto" else _detect_default_branch(workdir)
    logger.info("--changed-only: comparing against base ref '%s'", base_ref)
    changed_files = _get_changed_files(workdir, base_ref)
    if not changed_files:
        _fatal(f"No changed files found compared to '{base_ref}'")
    print(f"  Reviewing {len(changed_files)} changed file(s) (vs {base_ref})")
    return _build_changed_files_prefix(changed_files)


def _resolve_selected_checks(args: argparse.Namespace) -> list[dict[str, str]]:
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
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# --- Entry point --------------------------------------------------------------

def main() -> None:
    """CLI entry point: parse arguments and run the configured check suite.

    This is the function invoked by the ``checkloop`` console script defined
    in ``pyproject.toml``.  It parses CLI flags, resolves the check tier and
    check list, displays a pre-run summary, then delegates to the check loop.
    """
    args = _build_argument_parser().parse_args()
    _configure_logging(args)

    workdir = _resolve_working_directory(args.dir)
    _validate_arguments(args)

    args.changed_files_prefix = _resolve_changed_files_prefix(args, workdir)

    selected_checks = _resolve_selected_checks(args)
    if not selected_checks:
        _fatal("No checks selected. Check your --checks or --level arguments.")
    num_cycles = args.cycles
    total_steps = len(selected_checks) * num_cycles
    convergence_threshold = args.converged_at_percentage

    _print_run_summary(workdir, selected_checks, num_cycles, total_steps, args.idle_timeout, args.dry_run, convergence_threshold)

    logger.info(
        "Suite started: workdir=%s, checks=[%s], cycles=%d, idle_timeout=%d, convergence=%.2f%%",
        workdir,
        ", ".join(check["id"] for check in selected_checks),
        num_cycles,
        args.idle_timeout,
        convergence_threshold,
    )

    if not args.dry_run:
        _display_pre_run_warning(args.dangerously_skip_permissions)

    _run_suite_with_error_handling(selected_checks, num_cycles, workdir, args, convergence_threshold)


if __name__ == "__main__":  # pragma: no cover
    main()
