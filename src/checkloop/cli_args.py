"""CLI argument parsing, validation, resolution, and pre-run display.

Defines the argument parser, validates user-provided values, resolves
check selections and working directory, and displays the pre-run summary
and permission warning.  Separated from ``cli.py`` so the entry-point
module focuses on orchestration while this module owns the argument contract.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from checkloop.checks import (
    CHECK_IDS,
    CHECKS,
    CheckDef,
    DEFAULT_TIER,
    TIER_BASIC,
    TIER_CONFIGS,
    TIER_EXHAUSTIVE,
    TIER_THOROUGH,
    TIERS,
)
from checkloop.tier_config import BUILTIN_TIER_NAMES, TierConfig, load_builtin_tier, load_tier_file
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
)
from checkloop.terminal import BOLD, RED, RESET, RULE_WIDTH, YELLOW, fatal, print_status

logger = logging.getLogger(__name__)

DEFAULT_PAUSE_SECONDS = 2
"""Seconds to pause between consecutive checks (default for ``--pause``)."""

DEFAULT_CONVERGENCE_THRESHOLD = 0.1
"""Percent of total lines changed below which cycles stop early (default for ``--convergence-threshold``)."""


# --- Argument parsing ---------------------------------------------------------

def build_argument_parser() -> argparse.ArgumentParser:
    tier_names = ", ".join(BUILTIN_TIER_NAMES)
    parser = argparse.ArgumentParser(
        description="Autonomous multi-check code review using Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Built-in tiers (loaded from TOML files in src/checkloop/tiers/):",
            f"  basic       {', '.join(TIER_BASIC)}",
            f"  thorough    basic + {', '.join(cid for cid in TIER_THOROUGH if cid not in TIER_BASIC)}",
            f"  exhaustive  thorough + {', '.join(cid for cid in TIER_EXHAUSTIVE if cid not in TIER_THOROUGH)}",
            "",
            "Each tier file specifies a per-check model (sonnet or opus).",
            "Use --model to override all checks to a single model.",
            "",
            "All available checks (use with --checks to override tier):",
            *(f"  {check['id']:14s}  {check['label']}" for check in CHECKS),
            "",
            "Examples:",
            "  checkloop --dir .                                  # basic tier (default)",
            "  checkloop --dir ~/proj --tier thorough             # thorough tier",
            "  checkloop --dir ~/proj --tier exhaustive --cycles 2",
            "  checkloop --dir ~/proj --checks readability security",
            "  checkloop --dir ~/proj --all-checks                # same as --tier exhaustive",
            "  checkloop --dir ~/proj --tier thorough --checks cleanup-ai-slop",
            "  checkloop --dir ~/proj --tier ./my-tier.toml       # custom tier file",
            "  checkloop --dir ~/proj --model opus                # force all checks to opus",
            "  checkloop --dir ~/proj --dry-run",
        ]),
    )

    parser.add_argument(
        "--dir", "-d", required=True,
        help="Project directory to check",
    )
    parser.add_argument(
        "--tier", "-t", default=None, metavar="TIER",
        help=(
            f"Tier name or path to a custom TOML tier file. "
            f"Built-in tiers: {tier_names} (default: basic). "
            "If the value is a path to a .toml file, it is loaded as a custom tier."
        ),
    )
    parser.add_argument(
        "--checks", nargs="+", choices=CHECK_IDS, default=None,
        metavar="CHECK",
        help="Manually select checks. Alone: runs only these checks. Combined with --tier: adds these checks to the tier.",
    )
    parser.add_argument(
        "--all-checks", action="store_true",
        help="Run every available check (same as --tier exhaustive)",
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
    parser.add_argument(
        "--model", "-m", default=None, metavar="MODEL",
        help=(
            "Claude model override for ALL checks. Accepts aliases (e.g. 'sonnet', 'opus') "
            "or full model IDs (e.g. 'claude-sonnet-4-6'). When omitted, each check uses "
            "the model specified in the tier file."
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
    *,
    convergence_threshold: float = 0.0,
    max_memory_mb: int = 0,
    check_timeout: int = 0,
) -> None:
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


def _resolve_tier_config(args: argparse.Namespace) -> TierConfig | None:
    """Resolve a TierConfig from --tier, --all-checks, or the default.

    The ``--tier`` flag accepts either a pre-populated tier name (basic,
    thorough, exhaustive) or a path to any TOML tier file.

    Returns ``None`` when the user specified ``--checks`` without ``--tier``
    (ad-hoc check selection with no tier context).
    """
    if args.all_checks:
        return load_builtin_tier("exhaustive")
    tier = getattr(args, "tier", None)
    if tier:
        if tier in BUILTIN_TIER_NAMES:
            return load_builtin_tier(tier)
        return load_tier_file(tier)
    # --checks alone: no tier context.
    if args.checks:
        return None
    # Nothing specified: use the default tier.
    return load_builtin_tier(DEFAULT_TIER)


def resolve_selected_checks(args: argparse.Namespace) -> list[CheckDef]:
    """Resolve CLI flags to an ordered list of CheckDef objects.

    When both ``--tier`` and ``--checks`` are specified, the manual check
    list is *added* to the tier rather than replacing it.  This lets the user
    append a single out-of-tier check (e.g. ``--checks cleanup-ai-slop``) to
    a tier without rewriting the full check list.

    Also sets ``args.check_models`` — a dict mapping check ID to model name
    from the tier file.  The ``--model`` CLI flag overrides this per-check
    mapping when specified.
    """
    tier_config = _resolve_tier_config(args)

    if tier_config and args.checks:
        # Tier + --checks: add manual checks on top of the tier.
        selected_ids = set(tier_config.check_ids()) | set(args.checks)
    elif tier_config:
        # Pure tier selection (--tier, --all-checks, or default).
        selected_ids = set(tier_config.check_ids())
    elif args.checks:
        # Ad-hoc check selection with no tier context.
        selected_ids = set(args.checks)
    else:
        selected_ids = set(TIERS[DEFAULT_TIER])

    selected = [check for check in CHECKS if check["id"] in selected_ids]

    # Build per-check model map from the tier config.
    check_models: dict[str, str] = {}
    if tier_config:
        check_models = tier_config.model_map()
    args.check_models = check_models

    logger.info("Selected %d checks: %s", len(selected), [check["id"] for check in selected])
    logger.info("Per-check models: %s", check_models)
    return selected


# --- Mypy availability check --------------------------------------------------

_PYTHON_PROJECT_MARKERS: list[str] = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"]


def _is_python_project(workdir: str) -> bool:
    root = Path(workdir)
    if any((root / marker).exists() for marker in _PYTHON_PROJECT_MARKERS):
        return True
    return any(root.glob("*.py"))


def warn_if_mypy_unavailable(workdir: str) -> None:
    """Print a warning if the target is a Python project but mypy is not on PATH."""
    if not _is_python_project(workdir):
        return
    if shutil.which("mypy") is None:
        print(f"\n{YELLOW}{BOLD}Note: mypy not found on PATH.{RESET}")
        print(f"{YELLOW}  This looks like a Python project. The test-fix and test-validate checks")
        print(f"  will skip mypy type checking until it is available.")
        print(f"  To enable it, add mypy as a dev dependency in your project.{RESET}")


# --- Pre-run warning ----------------------------------------------------------

_WARNING_COUNTDOWN_SECONDS = 5  # countdown seconds before starting review


def display_pre_run_warning(skip_permissions: bool) -> None:
    """Enforce --dangerously-skip-permissions and show a countdown warning.

    Exits immediately if ``--dangerously-skip-permissions`` is not set —
    checkloop cannot relay interactive permission prompts, so running without
    it wastes tokens on checks that will fail or hang.

    With the flag set, shows a red warning and gives the user 5 seconds to
    Ctrl+C before the suite begins.
    """
    if not skip_permissions:
        print(f"\n{RED}{BOLD}{'=' * RULE_WIDTH}")
        print(f"  ERROR: --dangerously-skip-permissions is required")
        print(f"{'=' * RULE_WIDTH}{RESET}")
        print(f"{RED}  checkloop cannot relay interactive permission prompts (stdin is disconnected).")
        print(f"  Without this flag, checks that modify code will fail or hang, wasting tokens.")
        print(f"")
        print(f"  Re-run with:")
        print(f"    {BOLD}checkloop --dangerously-skip-permissions ...{RESET}{RED}")
        print(f"{RESET}")
        sys.exit(1)

    print(f"\n{RED}{BOLD}{'=' * RULE_WIDTH}")
    print(f"  WARNING: --dangerously-skip-permissions is ENABLED")
    print(f"{'=' * RULE_WIDTH}{RESET}")
    print(f"{RED}  Claude Code will execute ALL actions without asking for approval.")
    print(f"  This includes writing files, running shell commands, and deleting code.")
    print(f"  Make sure you have committed or backed up your work before proceeding.{RESET}")
    print(f"\n{RED}  Starting in {_WARNING_COUNTDOWN_SECONDS} seconds (Ctrl+C to abort)...{RESET}")

    try:
        time.sleep(_WARNING_COUNTDOWN_SECONDS)
    except KeyboardInterrupt:
        print_status("\nAborted.")
        sys.exit(0)
