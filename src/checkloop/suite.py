"""Check suite orchestration: running checks, convergence detection, and pre-run warnings."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from checkloop.checks import (
    COMMIT_MESSAGE_INSTRUCTIONS,
    FULL_CODEBASE_SCOPE,
    _BOOKEND_IDS,
    _looks_dangerous,
)
from checkloop.git import (
    _compute_change_stats,
    _git_commit_all,
    _git_head_sha,
    _git_squash_since,
    _is_git_repo,
)
from checkloop.process import run_claude
from checkloop.terminal import (
    BOLD,
    CYAN,
    GREEN,
    RED,
    RESET,
    RULE_WIDTH,
    YELLOW,
    _fatal,
    _format_duration,
    _print_banner,
    _print_status,
)

logger = logging.getLogger(__name__)

_PRE_RUN_WARNING_DELAY = 5  # countdown seconds before starting review


# --- Single check execution ---------------------------------------------------

def _run_single_check(
    check: dict[str, str],
    workdir: str,
    args: argparse.Namespace,
    step_label: str,
    *,
    is_git: bool = False,
) -> bool:
    """Execute a single check.

    Builds the prompt (with commit-message instructions appended), checks
    for dangerous keywords, snapshots the git state, invokes Claude Code,
    and reports what changed.

    Returns True if the check made changes (or if change detection is unavailable).
    """
    logger.info("Check started: id=%s, label=%s, step=%s", check["id"], check["label"], step_label)
    _print_banner(f"{step_label} {check['label']}", CYAN)

    # Scope prefix: either the --changed-only file list, or the default
    # "review ALL code" instruction for full-codebase checks.
    scope_prefix = getattr(args, "changed_files_prefix", "") or FULL_CODEBASE_SCOPE
    prompt = scope_prefix + check["prompt"] + COMMIT_MESSAGE_INSTRUCTIONS

    if _looks_dangerous(prompt):
        logger.warning("Skipping check '%s' — dangerous keywords detected in prompt", check["id"])
        _print_status(f"Skipping '{check['id']}' — dangerous keywords detected.", YELLOW)
        return False

    # Snapshot git state to detect changes
    sha_before = _git_head_sha(workdir) if is_git else None

    exit_code = run_claude(
        prompt,
        workdir,
        skip_permissions=args.dangerously_skip_permissions,
        dry_run=args.dry_run,
        idle_timeout=args.idle_timeout,
        debug=getattr(args, "debug", False),
    )
    if exit_code != 0:
        logger.warning("Check '%s' exited with code %d", check["id"], exit_code)
        _print_status(f"Check '{check['id']}' exited with code {exit_code}. Continuing...", YELLOW)

    if is_git:
        _git_commit_all(workdir, f"checkloop: {check['id']}")
    made_changes = _report_check_changes(workdir, check["id"], sha_before)
    logger.info("Check '%s' made_changes=%s", check["id"], made_changes)
    return made_changes


def _report_check_changes(workdir: str, pass_id: str, sha_before: str | None) -> bool:
    """Compare git state before/after a check, print stats, and return whether changes were made."""
    if sha_before is None:
        return True  # assume changes if not a git repo
    sha_after = _git_head_sha(workdir)
    if sha_after == sha_before:
        _print_status(f"  {pass_id}: no changes")
        return False
    lines_changed, pct = _compute_change_stats(workdir, sha_before)
    _print_status(f"  {pass_id}: {lines_changed} lines changed ({pct:.2f}% of codebase)")
    return True


# --- Active check filtering ---------------------------------------------------

def _filter_active_checks(
    selected_checks: list[dict[str, str]],
    previously_changed_ids: set[str] | None,
) -> list[dict[str, str]]:
    """Return checks to run this cycle, skipping those that were no-ops last cycle.

    On the first cycle (*previously_changed_ids* is None), all checks run.
    Bookend checks always run regardless of prior activity.
    """
    if previously_changed_ids is None:
        return selected_checks

    active_checks = [
        p for p in selected_checks
        if p["id"] in previously_changed_ids or p["id"] in _BOOKEND_IDS
    ]
    skipped_ids = [
        p["id"] for p in selected_checks
        if p["id"] not in previously_changed_ids and p["id"] not in _BOOKEND_IDS
    ]
    if skipped_ids:
        logger.info("Skipping %d no-op check(s) from last cycle: %s", len(skipped_ids), skipped_ids)
        _print_status(f"Skipping {len(skipped_ids)} check(s) that made no changes last cycle.")
    return active_checks


# --- Convergence detection ----------------------------------------------------

def _check_cycle_convergence(
    workdir: str,
    cycle: int,
    base_sha: str,
    convergence_threshold: float,
    prev_change_pct: float | None,
) -> tuple[bool, float | None]:
    """Commit changes and check whether the check loop has converged.

    Returns a ``(should_stop, change_pct)`` tuple.  *should_stop* is True when
    the loop should exit (either no changes or below threshold).
    """
    current_sha = _git_head_sha(workdir)

    if current_sha == base_sha:
        logger.info("Cycle %d: no changes detected — converged", cycle)
        _print_status(f"\nNo changes in cycle {cycle} — converged.", GREEN)
        return True, prev_change_pct

    lines_changed, change_pct = _compute_change_stats(workdir, base_sha)
    _print_status(f"\nCycle {cycle}: {change_pct:.2f}% of lines changed "
                 f"(threshold: {convergence_threshold}%)")

    if prev_change_pct is not None and change_pct > prev_change_pct:
        _print_status(f"Warning: changes increased ({prev_change_pct:.2f}% -> {change_pct:.2f}%) — "
                     f"possible oscillation.", YELLOW)

    if change_pct < convergence_threshold:
        logger.info("Cycle %d: converged at %.2f%% (%d lines, threshold: %.2f%%)",
                     cycle, change_pct, lines_changed, convergence_threshold)
        _print_status(f"Converged at {change_pct:.2f}% (below {convergence_threshold}% threshold).", GREEN)
        return True, change_pct

    logger.info("Cycle %d: %.2f%% lines changed (%d lines, threshold: %.2f%%), continuing",
                 cycle, change_pct, lines_changed, convergence_threshold)
    return False, change_pct


# --- Suite orchestration ------------------------------------------------------

def _run_check_suite(
    selected_checks: list[dict[str, str]],
    num_cycles: int,
    workdir: str,
    args: argparse.Namespace,
    convergence_threshold: float = 0.0,
) -> None:
    """Execute all checks across all cycles.

    When *convergence_threshold* > 0 and the project is a git repo, a commit
    is created after each cycle and the percentage of lines changed is compared
    to the threshold.  If changes fall below the threshold the loop stops early.

    On cycle 2+, checks that made no changes in the previous cycle are skipped.
    Bookend checks (test-fix, test-validate) always run on every cycle.
    """
    # Perf: check once instead of spawning a git subprocess on every check.
    is_git = _is_git_repo(workdir)
    convergence_enabled = convergence_threshold > 0 and is_git
    prev_change_pct: float | None = None
    # Tracks which checks made changes last cycle; None means "run all" (first cycle).
    previously_changed_ids: set[str] | None = None

    for cycle in range(1, num_cycles + 1):
        logger.info("Cycle %d/%d started", cycle, num_cycles)
        if num_cycles > 1:
            print(f"\n{BOLD}{CYAN}===  Cycle {cycle}/{num_cycles}  ==={RESET}")

        base_sha = _git_head_sha(workdir) if is_git else None
        active_checks = _filter_active_checks(selected_checks, previously_changed_ids)
        changed_this_cycle: set[str] = set()

        for i, check in enumerate(active_checks, 1):
            time.sleep(args.pause)
            cycle_suffix = f" (cycle {cycle}/{num_cycles})" if num_cycles > 1 else ""
            step_label = f"[{i}/{len(active_checks)}]{cycle_suffix}"

            made_changes = _run_single_check(check, workdir, args, step_label, is_git=is_git)
            if made_changes:
                changed_this_cycle.add(check["id"])

        previously_changed_ids = changed_this_cycle

        if is_git and base_sha and changed_this_cycle and not args.dry_run:
            check_names = ", ".join(sorted(changed_this_cycle))
            cycle_label = f" (cycle {cycle}/{num_cycles})" if num_cycles > 1 else ""
            _git_squash_since(workdir, base_sha, f"checkloop{cycle_label}: {check_names}")

        if convergence_enabled and base_sha and not args.dry_run:
            converged, prev_change_pct = _check_cycle_convergence(
                workdir, cycle, base_sha, convergence_threshold, prev_change_pct,
            )
            if converged:
                break


# --- Pre-run warning ----------------------------------------------------------

def _build_permission_warning(skip_permissions: bool) -> tuple[str, str, list[str], str]:
    """Build the warning message components based on permission mode.

    Returns a (colour, heading, body_lines, countdown) tuple.
    """
    if skip_permissions:
        return (
            RED,
            "WARNING: --dangerously-skip-permissions is ENABLED",
            [
                "Claude Code will execute ALL actions without asking for approval.",
                "This includes writing files, running shell commands, and deleting code.",
                "Make sure you have committed or backed up your work before proceeding.",
            ],
            f"Starting in {_PRE_RUN_WARNING_DELAY} seconds (Ctrl+C to abort)...",
        )
    return (
        YELLOW,
        "WARNING: Running without --dangerously-skip-permissions",
        [
            "Claude Code requires interactive permission prompts to write files,",
            "but checkloop cannot relay those prompts (stdin is disconnected).",
            "Checks that modify code will likely FAIL or HANG.",
            "",
            "Re-run with:",
            f"  {BOLD}checkloop --dangerously-skip-permissions ...{RESET}{YELLOW}",
        ],
        f"Continuing anyway in {_PRE_RUN_WARNING_DELAY} seconds (Ctrl+C to abort)...",
    )


def _display_pre_run_warning(skip_permissions: bool) -> None:
    """Show a warning about permissions and count down before starting.

    With ``--dangerously-skip-permissions``, warns that all actions will run
    without approval.  Without it, warns that checks will likely hang because
    checkloop cannot relay interactive permission prompts.  Either way, the
    user has 5 seconds to Ctrl+C before the suite begins.
    """
    warning_colour, heading, body_lines, countdown = _build_permission_warning(skip_permissions)

    print(f"\n{warning_colour}{BOLD}{'=' * RULE_WIDTH}")
    print(f"  {heading}")
    print(f"{'=' * RULE_WIDTH}{RESET}")
    for line in body_lines:
        print(f"{warning_colour}  {line}{RESET}")
    print(f"\n{warning_colour}  {countdown}{RESET}")

    try:
        time.sleep(_PRE_RUN_WARNING_DELAY)
    except KeyboardInterrupt:
        _print_status("\nAborted.")
        sys.exit(0)


# --- Error-handling wrapper ---------------------------------------------------

def _run_suite_with_error_handling(
    selected_checks: list[dict[str, str]],
    num_cycles: int,
    workdir: str,
    args: argparse.Namespace,
    convergence_threshold: float,
) -> None:
    """Run the check suite, handling interrupts and unexpected errors.

    Wraps ``_run_check_suite`` with KeyboardInterrupt handling (exits 130),
    missing-tool detection, and a catch-all that logs the traceback before
    re-raising.  Prints a final timing banner on success.
    """
    suite_start_time = time.time()
    try:
        _run_check_suite(selected_checks, num_cycles, workdir, args, convergence_threshold)
    except KeyboardInterrupt:
        elapsed = _format_duration(time.time() - suite_start_time)
        _print_status(f"\nInterrupted after {elapsed}. Partial results may have been applied.", YELLOW)
        sys.exit(130)
    except FileNotFoundError as exc:
        logger.error("Required external tool not found: %s", exc, exc_info=True)
        _fatal(f"Required tool not found: {exc}. Ensure git and claude are installed.")
    except Exception:
        logger.exception("Unexpected error during check suite")
        elapsed = _format_duration(time.time() - suite_start_time)
        _print_status(f"\nUnexpected error after {elapsed}. Partial results may have been applied.", RED)
        raise
    suite_elapsed = _format_duration(time.time() - suite_start_time)
    logger.info("Suite completed: elapsed=%s", suite_elapsed)
    _print_banner(f"All done! ({suite_elapsed} total)", GREEN)
