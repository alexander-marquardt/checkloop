"""Check suite orchestration: running checks, convergence detection, and error handling.

Coordinates the full lifecycle of a checkloop run: iterating through cycles,
executing individual checks, tracking which checks produced changes, detecting
convergence.  All checks run every cycle so that cascading improvements are
never missed.  Per-check commits are preserved individually for easier
debugging.  Supports resuming from a saved
checkpoint.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from checkloop.check_runner import CheckOutcome, _run_single_check
from checkloop.checkpoint import (
    CheckpointData,
    build_checkpoint,
    clear_checkpoint,
    save_checkpoint,
)
from checkloop.checks import CheckDef
from checkloop.git import (
    compute_change_stats,
    git_head_sha,
    is_git_repo,
)
from checkloop.terminal import (
    BOLD,
    CYAN,
    GREEN,
    RED,
    RESET,
    YELLOW,
    compute_summary_stats,
    fatal,
    format_duration,
    print_banner,
    print_overall_summary_table,
    print_run_summary_table,
    print_status,
)

logger = logging.getLogger(__name__)


# --- Convergence detection ----------------------------------------------------

def _check_cycle_convergence(
    workdir: str,
    cycle: int,
    base_sha: str,
    convergence_threshold: float,
    prev_change_pct: float | None,
) -> tuple[bool, float | None]:
    """Check whether the check loop has converged.

    Returns a ``(should_stop, change_pct)`` tuple.  *should_stop* is True when
    the loop should exit (either no changes or below threshold).
    """
    current_sha = git_head_sha(workdir)

    if current_sha is None:
        logger.warning("Cycle %d: could not read HEAD SHA — skipping convergence check", cycle)
        return False, prev_change_pct

    if current_sha == base_sha:
        logger.info("Cycle %d: no changes detected — converged", cycle)
        print_status(f"\nNo changes in cycle {cycle} — converged.", GREEN)
        return True, prev_change_pct

    lines_changed, change_pct = compute_change_stats(workdir, base_sha)
    print_status(f"\nCycle {cycle}: {change_pct:.2f}% of lines changed "
                 f"(threshold: {convergence_threshold}%)")

    if prev_change_pct is not None and change_pct > prev_change_pct:
        print_status(f"Warning: changes increased ({prev_change_pct:.2f}% -> {change_pct:.2f}%) — "
                     f"possible oscillation.", YELLOW)

    if change_pct < convergence_threshold:
        logger.info("Cycle %d: converged at %.2f%% (%d lines, threshold: %.2f%%)",
                     cycle, change_pct, lines_changed, convergence_threshold)
        print_status(f"Converged at {change_pct:.2f}% (below {convergence_threshold}% threshold).", GREEN)
        return True, change_pct

    logger.info("Cycle %d: %.2f%% lines changed (%d lines, threshold: %.2f%%), continuing",
                 cycle, change_pct, lines_changed, convergence_threshold)
    return False, change_pct


# --- Suite state --------------------------------------------------------------

@dataclass
class _SuiteState:
    """Mutable state carried across cycles in ``_run_check_suite``.

    Attributes:
        start_cycle: Cycle number to begin from (1-based; >1 when resuming).
        start_check_index: 0-based index of the first check to run in the
            starting cycle (>0 when resuming mid-cycle).
        resume_active_check_ids: Check IDs from the checkpoint's active list,
            or None when not resuming.
        resume_changed: Check IDs already marked as changed from the checkpoint.
        prev_change_pct: Percentage of lines changed in the prior cycle,
            used for oscillation detection.
        previously_changed_ids: Check IDs that produced changes in the prior
            cycle, or None if this is the first cycle.
        started_at: ISO 8601 timestamp when the suite was originally started.
    """

    start_cycle: int = 1
    start_check_index: int = 0
    resume_active_check_ids: list[str] | None = None
    resume_changed: set[str] = field(default_factory=set)
    prev_change_pct: float | None = None
    previously_changed_ids: set[str] | None = None
    started_at: str = ""


def _build_suite_state(resume_from: CheckpointData | None) -> _SuiteState:
    """Initialize suite state from a checkpoint or from scratch."""
    state = _SuiteState(started_at=datetime.now(timezone.utc).isoformat())
    if resume_from is None:
        return state
    state.start_cycle = resume_from["current_cycle"]
    state.start_check_index = resume_from["current_check_index"]
    state.resume_active_check_ids = resume_from["active_check_ids"]
    state.resume_changed = set(resume_from["changed_this_cycle"])
    state.prev_change_pct = resume_from.get("prev_change_pct")
    raw_prev = resume_from.get("previously_changed_ids")
    state.previously_changed_ids = set(raw_prev) if raw_prev is not None else None
    state.started_at = resume_from.get("started_at", state.started_at)
    print_status(f"Resuming from cycle {state.start_cycle}, "
                 f"check {state.start_check_index + 1}...", CYAN)
    return state


# --- Suite orchestration ------------------------------------------------------

def _resolve_cycle_checks(
    selected_checks: list[CheckDef],
    state: _SuiteState,
) -> tuple[list[CheckDef], int, set[str] | None]:
    """Determine which checks to run this cycle and where to start.

    On a resume, consumes the saved checkpoint state (active check list,
    start index, already-changed IDs) for the first cycle, then clears
    it so subsequent cycles start fresh.

    Returns ``(active_checks, start_index, initial_changed)``.
    """
    if state.resume_active_check_ids is not None:
        active_checks = [
            c for c in selected_checks if c["id"] in state.resume_active_check_ids
        ]
        # Preserve the checkpoint's ordering.
        id_order = {cid: idx for idx, cid in enumerate(state.resume_active_check_ids)}
        active_checks.sort(key=lambda c: id_order.get(c["id"], 999))
        start_index = state.start_check_index
        initial_changed: set[str] | None = state.resume_changed
        # Consume resume state so subsequent cycles don't re-enter this branch.
        state.resume_active_check_ids = None
        state.start_check_index = 0
        state.resume_changed = set()
        return active_checks, start_index, initial_changed

    return list(selected_checks), 0, None


def _run_single_cycle(
    active_checks: list[CheckDef],
    workdir: str,
    args: argparse.Namespace,
    cycle: int,
    num_cycles: int,
    *,
    is_git: bool,
    start_index: int = 0,
    initial_changed: set[str] | None = None,
    on_check_complete: Callable[[int, set[str]], None] | None = None,
) -> tuple[set[str], list[CheckOutcome]]:
    """Execute all checks for one cycle.

    Returns ``(changed_ids, outcomes)`` — the set of check IDs that made
    changes and a list of ``CheckOutcome`` objects for the summary table.

    When *start_index* > 0, checks before that index are skipped (resume mode).
    *initial_changed* seeds the changed set with IDs from already-completed
    checks (from a checkpoint).
    """
    changed_this_cycle: set[str] = set(initial_changed or ())
    outcomes: list[CheckOutcome] = []
    for i, check in enumerate(active_checks):
        if i < start_index:
            continue
        # Only pause between checks, not before the first one we actually run.
        if i > start_index:
            time.sleep(args.pause)
        display_idx = i + 1  # 1-based for display
        cycle_suffix = f" (cycle {cycle}/{num_cycles})" if num_cycles > 1 else ""
        step_label = f"[{display_idx}/{len(active_checks)}]{cycle_suffix}"

        outcome = _run_single_check(check, workdir, args, step_label, is_git=is_git, cycle=cycle)
        outcomes.append(outcome)
        if outcome.made_changes:
            changed_this_cycle.add(check["id"])

        if on_check_complete is not None:
            on_check_complete(i + 1, changed_this_cycle)

    return changed_this_cycle, outcomes


def _run_check_suite(
    selected_checks: list[CheckDef],
    num_cycles: int,
    workdir: str,
    args: argparse.Namespace,
    convergence_threshold: float = 0.0,
    *,
    resume_from: CheckpointData | None = None,
) -> list[CheckOutcome]:
    """Execute all checks across all cycles.

    Every check runs on every cycle — no checks are skipped, because earlier
    checks create work for later ones (cascading improvements).

    When *convergence_threshold* > 0 and the project is a git repo, a commit
    is created after each cycle and the percentage of lines changed is compared
    to the threshold.  If changes fall below the threshold the loop stops early.

    When *resume_from* is provided, the suite picks up from the saved checkpoint
    instead of starting from the beginning.

    Returns a list of ``CheckOutcome`` objects for the post-run summary.
    """
    is_git = is_git_repo(workdir)
    convergence_enabled = convergence_threshold > 0 and is_git
    check_ids = [c["id"] for c in selected_checks]
    state = _build_suite_state(resume_from)
    all_outcomes: list[CheckOutcome] = []

    def _save_after_check(check_index: int, changed: set[str]) -> None:
        """Checkpoint callback — invoked after each check completes."""
        try:
            data = build_checkpoint(
                workdir=workdir,
                check_ids=check_ids,
                num_cycles=num_cycles,
                convergence_threshold=convergence_threshold,
                current_cycle=cycle,
                current_check_index=check_index,
                active_check_ids=[c["id"] for c in active_checks],
                changed_this_cycle=changed,
                previously_changed_ids=state.previously_changed_ids,
                prev_change_pct=state.prev_change_pct,
                started_at=state.started_at,
            )
            save_checkpoint(workdir, data)
        except Exception as exc:
            logger.warning("Failed to save checkpoint after check %d: %s", check_index, exc, exc_info=True)

    for cycle in range(state.start_cycle, num_cycles + 1):
        logger.info("Cycle %d/%d started", cycle, num_cycles)
        if num_cycles > 1:
            print(f"\n{BOLD}{CYAN}===  Cycle {cycle}/{num_cycles}  ==={RESET}")

        base_sha = git_head_sha(workdir) if is_git else None

        active_checks, cycle_start_index, cycle_initial_changed = _resolve_cycle_checks(
            selected_checks, state,
        )

        changed_this_cycle, cycle_outcomes = _run_single_cycle(
            active_checks, workdir, args, cycle, num_cycles,
            is_git=is_git,
            start_index=cycle_start_index,
            initial_changed=cycle_initial_changed,
            on_check_complete=_save_after_check,
        )
        all_outcomes.extend(cycle_outcomes)
        logger.info("Cycle %d/%d completed: %d/%d checks made changes (%s)",
                     cycle, num_cycles, len(changed_this_cycle), len(active_checks),
                     ", ".join(sorted(changed_this_cycle)) or "none")
        _print_cycle_summary(cycle_outcomes, cycle, num_cycles)
        state.previously_changed_ids = changed_this_cycle

        if convergence_enabled and base_sha is not None and not args.dry_run:
            converged, state.prev_change_pct = _check_cycle_convergence(
                workdir, cycle, base_sha, convergence_threshold, state.prev_change_pct,
            )
            if converged:
                break

    # Suite completed successfully — remove the checkpoint file.
    clear_checkpoint(workdir)
    return all_outcomes


# --- Post-run summary ---------------------------------------------------------

def _print_cycle_summary(
    cycle_outcomes: list[CheckOutcome], cycle: int, num_cycles: int,
) -> None:
    """Print a per-cycle summary table after each cycle completes.

    Only prints when running multiple cycles — single-cycle runs get
    the final "Run Summary" instead to avoid redundant output.
    """
    if not cycle_outcomes or num_cycles <= 1:
        return
    summary_dicts = [o.to_summary_dict() for o in cycle_outcomes]
    cycle_duration = format_duration(sum(o.duration_seconds for o in cycle_outcomes))
    print_run_summary_table(
        summary_dicts, cycle_duration,
        banner_title=f"Cycle {cycle}/{num_cycles} Summary",
        banner_colour=CYAN,
    )


def _print_summary(outcomes: list[CheckOutcome], total_elapsed: str) -> None:
    """Print and log the overall summary table if there are any outcomes to show."""
    if not outcomes:
        return
    summary_dicts = [o.to_summary_dict() for o in outcomes]
    stats = compute_summary_stats(summary_dicts)
    logger.info(
        "Suite summary: checks=%d, succeeded=%d, failed=%d, killed=%d, "
        "lines_changed=%d, checks_with_changes=%d, elapsed=%s",
        len(outcomes), stats.succeeded, stats.failed, stats.killed,
        stats.total_lines, stats.with_changes, total_elapsed,
    )
    for o in outcomes:
        logger.info(
            "  Check outcome: id=%s, cycle=%d, exit_code=%d, kill_reason=%s, "
            "made_changes=%s, lines_changed=%s, duration=%.1fs",
            o.check_id, o.cycle, o.exit_code, o.kill_reason,
            o.made_changes, o.lines_changed, o.duration_seconds,
        )
    # For multi-cycle runs, show the cross-cycle overview table.
    # For single-cycle runs, just show the per-check detail table.
    num_cycles = len({o.cycle for o in outcomes})
    if num_cycles > 1:
        print_overall_summary_table(summary_dicts, total_elapsed)
    else:
        print_run_summary_table(
            summary_dicts, total_elapsed, stats=stats,
            banner_title="Run Summary", banner_colour=CYAN,
        )


# --- Error-handling wrapper ---------------------------------------------------

def run_suite_with_error_handling(
    selected_checks: list[CheckDef],
    num_cycles: int,
    workdir: str,
    args: argparse.Namespace,
    convergence_threshold: float,
    *,
    resume_from: CheckpointData | None = None,
) -> None:
    """Run the check suite, handling interrupts and unexpected errors.

    Wraps ``_run_check_suite`` with KeyboardInterrupt handling (exits 130),
    missing-tool detection, and a catch-all that logs the traceback before
    re-raising.  Prints a final timing banner on success.

    The checkpoint file is intentionally NOT cleared on error — this is what
    allows resume on the next run.
    """
    suite_start_time = time.time()
    all_outcomes: list[CheckOutcome] = []
    try:
        all_outcomes = _run_check_suite(
            selected_checks, num_cycles, workdir, args,
            convergence_threshold, resume_from=resume_from,
        )
    except KeyboardInterrupt:
        elapsed = format_duration(time.time() - suite_start_time)
        logger.warning("Suite interrupted by user after %s", elapsed)
        print_status(f"\nInterrupted after {elapsed}. Partial results may have been applied.", YELLOW)
        _print_summary(all_outcomes, elapsed)
        sys.exit(130)
    except FileNotFoundError as exc:
        logger.error("Required external tool not found: %s", exc, exc_info=True)
        fatal(f"Required tool not found: {exc}. Ensure git and claude are installed.")
    except Exception:
        logger.exception("Unexpected error during check suite")
        elapsed = format_duration(time.time() - suite_start_time)
        print_status(f"\nUnexpected error after {elapsed}. Partial results may have been applied.", RED)
        _print_summary(all_outcomes, elapsed)
        raise
    suite_elapsed = format_duration(time.time() - suite_start_time)
    logger.info("Suite completed: elapsed=%s, checks=%d, cycles=%d",
                suite_elapsed, len(selected_checks), num_cycles)
    _print_summary(all_outcomes, suite_elapsed)
    print_banner(f"All done! ({suite_elapsed} total)", GREEN)
