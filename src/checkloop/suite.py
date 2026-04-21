"""Check suite orchestration: running checks, convergence detection, and error handling.

Coordinates the full lifecycle of a checkloop run: iterating through cycles,
executing individual checks, tracking which checks produced changes, detecting
convergence.  All checks run every cycle so that cascading improvements are
never missed.  Per-check commits are preserved individually for easier
debugging.  Supports resuming from a saved checkpoint.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from checkloop.check_runner import CheckOutcome as CheckOutcome, run_single_check
from checkloop.checkpoint import (
    CheckpointData,
    build_checkpoint,
    clear_checkpoint,
    save_checkpoint,
)
from checkloop.checks import CheckDef
from checkloop.clone import is_remote_url
from checkloop.git import (
    _git_stdout,
    branch_exists,
    checkout_branch,
    compute_change_stats,
    count_commits_between,
    create_scratch_branch,
    get_uncommitted_diff,
    git_commit_all,
    git_head_sha,
    has_uncommitted_changes,
    is_git_repo,
)
from checkloop.commit_message import generate_commit_message
from checkloop.terminal import (
    BOLD,
    CYAN,
    DIM,
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

    _added, _deleted, lines_changed, change_pct = compute_change_stats(workdir, base_sha)
    print_status(f"\nCycle {cycle}: {change_pct:.2f}% of lines changed "
                 f"(threshold: {convergence_threshold}%)")

    if prev_change_pct is not None and change_pct > prev_change_pct:
        logger.warning("Cycle %d: possible oscillation — changes increased from %.2f%% to %.2f%%",
                       cycle, prev_change_pct, change_pct)
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


# --- Pre-suite uncommitted change snapshot ------------------------------------

_MAX_DIFF_LEN = 50_000  # truncate diffs beyond this to avoid overwhelming Claude
_FALLBACK_COMMIT_MSG = "Snapshot uncommitted work before checkloop review"


def _commit_uncommitted_changes(workdir: str, skip_permissions: bool, model: str | None = None, claude_command: str = "claude") -> None:
    """Commit any uncommitted changes with a Claude-generated message.

    Called at the start of a suite run to preserve the user's in-progress
    work before checks begin modifying files.  If Claude fails to generate
    a message, falls back to a generic description.
    """
    if not has_uncommitted_changes(workdir):
        return

    diff = get_uncommitted_diff(workdir)
    message = _FALLBACK_COMMIT_MSG
    if diff:
        if len(diff) > _MAX_DIFF_LEN:
            diff = diff[:_MAX_DIFF_LEN] + f"\n\n... (truncated, {len(diff) - _MAX_DIFF_LEN} more characters)"
        message = generate_commit_message(diff, workdir, skip_permissions=skip_permissions, model=model, claude_command=claude_command) or _FALLBACK_COMMIT_MSG

    committed = git_commit_all(workdir, message)
    if committed:
        logger.info("Committed uncommitted changes before suite: %s", message[:120])
        print_status("Committed uncommitted changes before starting checks.", GREEN)
    else:
        logger.debug("git_commit_all returned False despite has_uncommitted_changes=True")


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
        scratch_branch: Name of the disposable ``checkloop/run-*`` branch
            checkloop is committing on, or None outside git repos / dry runs.
        scratch_base_sha: SHA where the scratch branch was forked from — used
            to count commits and diff against the user's original state.
        original_branch: Branch the user was on before the scratch branch was
            created (None if HEAD was detached).  Used in the post-run summary.
    """

    start_cycle: int = 1
    start_check_index: int = 0
    resume_active_check_ids: list[str] | None = None
    resume_changed: set[str] = field(default_factory=set)
    prev_change_pct: float | None = None
    previously_changed_ids: set[str] | None = None
    started_at: str = ""
    scratch_branch: str | None = None
    scratch_base_sha: str | None = None
    original_branch: str | None = None


def _build_suite_state(resume_from: CheckpointData | None) -> _SuiteState:
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
    state.scratch_branch = resume_from.get("scratch_branch")
    state.scratch_base_sha = resume_from.get("scratch_base_sha")
    state.original_branch = resume_from.get("original_branch")
    logger.info("Resuming from checkpoint: cycle=%d, check_index=%d, changed=%s",
                state.start_cycle, state.start_check_index, sorted(state.resume_changed))
    print_status(f"Resuming from cycle {state.start_cycle}, "
                 f"check {state.start_check_index + 1}...", CYAN)
    return state


def _attach_to_scratch_branch(
    workdir: str,
    state: _SuiteState,
    review_branch: str | None = None,
) -> None:
    """Create or reattach to checkloop's disposable scratch branch.

    On a fresh run, forks a branch off the current HEAD and checks it out.
    The branch name includes the review-branch name when given (see
    :func:`checkloop.git._build_scratch_branch_name`).
    On resume, reattaches to the scratch branch recorded in the checkpoint —
    creating it again if the user deleted it, which shouldn't happen but is
    safer than failing the whole resume.

    All subsequent commits (pre-run snapshot, per-check commits, memory-fix
    commits) land on this branch so the user's original branch history stays
    pristine.  Silently returns if a branch couldn't be created (non-fatal —
    the run proceeds on whatever branch the user was on, matching pre-feature
    behaviour).
    """
    if state.scratch_branch:
        # Resume path: we already have a branch name from the checkpoint.
        if branch_exists(workdir, state.scratch_branch):
            if checkout_branch(workdir, state.scratch_branch):
                print_status(
                    f"Reattached to scratch branch {state.scratch_branch} "
                    f"(original: {state.original_branch or '(detached)'}).",
                    CYAN,
                )
                return
            logger.warning("Resume: failed to checkout %s — continuing on current branch",
                           state.scratch_branch)
            return
        logger.warning(
            "Resume: scratch branch %s no longer exists — creating a new one",
            state.scratch_branch,
        )
        # Fall through to fresh-create.

    info = create_scratch_branch(workdir, review_branch=review_branch)
    if info is None:
        # Non-fatal: the run can proceed on the user's current branch.  This is
        # what pre-feature behaviour looked like, so we don't fail the run.
        print_status(
            "Warning: could not create scratch branch — commits will land on the current branch.",
            YELLOW,
        )
        return
    branch_name, base_sha, original = info
    state.scratch_branch = branch_name
    state.scratch_base_sha = base_sha
    state.original_branch = original
    print_status(
        f"Working on scratch branch {branch_name} "
        f"(forked from {original or '(detached HEAD)'}@{base_sha[:7]}). "
        f"Original branch is untouched.",
        GREEN,
    )


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
    check_models: dict[str, str] = getattr(args, "check_models", {})
    check_idle_timeouts: dict[str, int] = getattr(args, "check_idle_timeouts", {})
    global_model: str | None = getattr(args, "model", None)
    for i, check in enumerate(active_checks[start_index:], start=start_index):
        # Only pause between checks, not before the first one we actually run.
        if i > start_index:
            time.sleep(args.pause)
        display_idx = i + 1  # 1-based for display
        cycle_suffix = f" (cycle {cycle}/{num_cycles})" if num_cycles > 1 else ""
        step_label = f"[{display_idx}/{len(active_checks)}]{cycle_suffix}"

        # Per-check model from tier config, overridden by --model if specified.
        per_check_model = global_model or check_models.get(check["id"])
        # Per-check idle timeout override, falling back to global --idle-timeout.
        per_check_idle_timeout = check_idle_timeouts.get(check["id"])
        # Refresh the project map in case the previous check added, renamed,
        # or removed files.  ensure_project_map is a cheap fingerprint-compare
        # no-op when git ls-files is unchanged and only regenerates when the
        # tracked-file set actually changed, so the downstream check sees the
        # new layout instead of a stale map.
        if not args.dry_run:
            from checkloop.project_map import ensure_project_map
            args.project_map = ensure_project_map(
                workdir,
                skip_permissions=args.dangerously_skip_permissions,
                model=getattr(args, "model", None),
                claude_command=getattr(args, "claude_command", "claude"),
            )
        outcome = run_single_check(
            check, workdir, args, step_label,
            is_git=is_git, cycle=cycle, model=per_check_model,
            idle_timeout_override=per_check_idle_timeout,
        )
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
    all_outcomes: list[CheckOutcome] | None = None,
) -> list[CheckOutcome]:
    """Execute all checks across all cycles.

    Every check runs on every cycle — no checks are skipped, because earlier
    checks create work for later ones (cascading improvements).

    When *convergence_threshold* > 0 and the project is a git repo, a commit
    is created after each cycle and the percentage of lines changed is compared
    to the threshold.  If changes fall below the threshold the loop stops early.

    When *resume_from* is provided, the suite picks up from the saved checkpoint
    instead of starting from the beginning.

    When *all_outcomes* is provided, completed check outcomes are appended to it
    progressively.  This allows the caller to access partial results even if the
    suite is interrupted by an exception or KeyboardInterrupt.

    Returns the outcomes list (same object as *all_outcomes* when provided).
    """
    is_git = is_git_repo(workdir)
    state = _build_suite_state(resume_from)
    if is_git and not args.dry_run:
        # Create or reattach to checkloop's disposable scratch branch BEFORE
        # committing anything: the pre-run snapshot of the user's uncommitted
        # work must land on the scratch branch, not on their original branch.
        _attach_to_scratch_branch(workdir, state, review_branch=getattr(args, "review_branch", None))
        _commit_uncommitted_changes(workdir, args.dangerously_skip_permissions, getattr(args, "model", None), getattr(args, "claude_command", "claude"))
    # Expose scratch-branch info on args so the error-handling wrapper can
    # surface the review/merge/discard instructions in the post-run summary
    # even if the suite is interrupted before clearing the checkpoint.
    args.scratch_branch = state.scratch_branch
    args.scratch_base_sha = state.scratch_base_sha
    args.original_branch = state.original_branch
    # On fresh runs, clear previous per-check logs so the directory only
    # contains output from the current session.
    log_dir = Path(workdir) / ".checkloop-logs"
    if resume_from is None:
        shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(exist_ok=True)
    # Generate or validate the project structure map so every check starts
    # with a shared understanding of the codebase layout.
    if not args.dry_run:
        from checkloop.project_map import ensure_project_map
        map_text = ensure_project_map(
            workdir,
            skip_permissions=args.dangerously_skip_permissions,
            model=getattr(args, "model", None),
            claude_command=getattr(args, "claude_command", "claude"),
        )
        if map_text:
            print_status(f"  Project map ready ({len(map_text)} chars)", GREEN)
        # Store on args so _build_check_prompt can access it.
        args.project_map = map_text
    else:
        args.project_map = ""
    convergence_enabled = convergence_threshold > 0 and is_git
    check_ids = [c["id"] for c in selected_checks]
    if all_outcomes is None:
        all_outcomes = []

    def _save_after_check(check_index: int, changed: set[str]) -> None:
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
                scratch_branch=state.scratch_branch,
                scratch_base_sha=state.scratch_base_sha,
                original_branch=state.original_branch,
            )
            save_checkpoint(workdir, data)
        except Exception as exc:
            logger.warning("Failed to save checkpoint after check %d: %s", check_index, exc, exc_info=True)

    for cycle in range(state.start_cycle, num_cycles + 1):
        cycle_start_time = time.time()
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
        cycle_elapsed = time.time() - cycle_start_time
        logger.info("Cycle %d/%d completed in %.1fs: %d/%d checks made changes (%s)",
                     cycle, num_cycles, cycle_elapsed,
                     len(changed_this_cycle), len(active_checks),
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
    summary_dicts = [outcome.to_summary_row() for outcome in cycle_outcomes]
    cycle_duration = format_duration(sum(outcome.duration_seconds for outcome in cycle_outcomes))
    print_run_summary_table(
        summary_dicts, cycle_duration,
        banner_title=f"Cycle {cycle}/{num_cycles} Summary",
        banner_colour=CYAN,
    )


def _print_summary(outcomes: list[CheckOutcome], total_elapsed: str) -> None:
    """Print the final summary after all cycles complete (or on interrupt/error).

    Multi-cycle runs get the cross-cycle overview table (one row per cycle)
    followed by aggregate totals.  Single-cycle runs get the per-check detail
    table directly, since the per-cycle table would be redundant.
    """
    if not outcomes:
        return
    summary_dicts = [outcome.to_summary_row() for outcome in outcomes]
    stats = compute_summary_stats(summary_dicts)
    logger.info(
        "Suite summary: checks=%d, succeeded=%d, failed=%d, killed=%d, "
        "lines_changed=%d, checks_with_changes=%d, elapsed=%s",
        len(outcomes), stats.succeeded, stats.failed, stats.killed,
        stats.total_lines, stats.with_changes, total_elapsed,
    )
    for outcome in outcomes:
        logger.info(
            "  Check outcome: id=%s, cycle=%d, exit_code=%d, kill_reason=%s, "
            "made_changes=%s, lines_changed=%s, duration=%.1fs",
            outcome.check_id, outcome.cycle, outcome.exit_code, outcome.kill_reason,
            outcome.made_changes, outcome.lines_changed, outcome.duration_seconds,
        )
    # For multi-cycle runs, show the cross-cycle overview table.
    # For single-cycle runs, just show the per-check detail table.
    num_cycles = len({outcome.cycle for outcome in outcomes})
    if num_cycles > 1:
        print_overall_summary_table(summary_dicts, total_elapsed)
    else:
        print_run_summary_table(
            summary_dicts, total_elapsed, stats=stats,
            banner_title="Run Summary", banner_colour=CYAN,
        )


# --- Error-handling wrapper ---------------------------------------------------

_RECOMMENDATIONS_FILENAME = ".checkloop-recommendations.md"


def _print_recommendations(workdir: str, suite_start_time: float, dry_run: bool) -> None:
    """Print the meta-review recommendations file if it was written during this run.

    The meta-review check writes ``.checkloop-recommendations.md`` at the root of
    the target project.  We surface it in the terminal after the final summary so
    users see it without having to remember the filename.  Only prints when the
    file's mtime is newer than *suite_start_time*, to avoid resurfacing a stale
    report from an earlier run.
    """
    if dry_run:
        return
    path = Path(workdir) / _RECOMMENDATIONS_FILENAME
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug("Could not stat %s: %s", path, exc)
        return
    if stat.st_mtime < suite_start_time:
        return
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return
    print(f"\n{BOLD}{CYAN}{'─' * 72}{RESET}")
    print(f"{BOLD}{CYAN}  Meta-Review Recommendations{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 72}{RESET}\n")
    print(body.rstrip())
    print(f"\n{DIM}(Saved to {path}){RESET}\n")


def _print_scratch_branch_summary(
    workdir: str,
    scratch_branch: str | None,
    scratch_base_sha: str | None,
    original_branch: str | None,
    dry_run: bool,
    *,
    clone_mode: bool = False,
    original_workdir: str | None = None,
    review_branch: str | None = None,
) -> None:
    """Print post-run instructions for adopting or discarding checkloop's work.

    Only shown when the target directory is a git repo and not a dry run.
    Behaviour depends on ``clone_mode``:

    * In clone mode (default) the scratch branch lives inside a disposable
      clone under ``~/checkloop-runs/``.  Commands direct the user to
      ``cd`` back to their real repo and ``git fetch`` the branch out of the
      clone, so the clone is purely additive and trivially deletable.
    * In in-place mode the scratch branch lives in the user's real repo and
      the commands use local ``git merge`` / ``cherry-pick`` / ``branch -D``
      against it.
    """
    if dry_run or not is_git_repo(workdir):
        return

    print(f"\n{BOLD}{CYAN}{'─' * 72}{RESET}")
    print(f"{BOLD}{CYAN}  Review & Adopt{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 72}{RESET}\n")

    if not scratch_branch or not scratch_base_sha:
        print(f"  {YELLOW}No scratch branch was created — commits landed on the current branch.{RESET}")
        print(f"  {YELLOW}Review with:  git log --oneline -20{RESET}\n")
        return

    commit_count = count_commits_between(workdir, scratch_base_sha, scratch_branch)

    if commit_count == 0:
        print(f"  {YELLOW}Scratch branch {scratch_branch} has no new commits — nothing to adopt.{RESET}")
        if clone_mode:
            print(f"  {YELLOW}You can delete the clone: rm -rf {workdir}{RESET}\n")
        else:
            print(f"  {YELLOW}Delete it with:  git branch -D {scratch_branch}{RESET}\n")
        return

    print(f"  {GREEN}Scratch branch {BOLD}{scratch_branch}{RESET}{GREEN} has "
          f"{commit_count} commit(s) on top of {scratch_base_sha[:7]}.{RESET}\n")

    if clone_mode and original_workdir:
        _print_clone_adoption_commands(
            clone_dir=workdir,
            scratch_branch=scratch_branch,
            scratch_base_sha=scratch_base_sha,
            original_workdir=original_workdir,
            review_branch=review_branch,
        )
    else:
        _print_in_place_adoption_commands(
            scratch_branch=scratch_branch,
            scratch_base_sha=scratch_base_sha,
            original_branch=original_branch,
        )


def _pr_base_from_review_branch(review_branch: str | None) -> str:
    if not review_branch:
        return "<review-branch>"
    return review_branch[len("origin/"):] if review_branch.startswith("origin/") else review_branch


def _clone_origin_url(clone_dir: str) -> str | None:
    """Return the clone's ``origin`` URL, or None if not configured."""
    return _git_stdout(clone_dir, "config", "remote.origin.url")


def _print_clone_adoption_commands(
    *,
    clone_dir: str,
    scratch_branch: str,
    scratch_base_sha: str,
    original_workdir: str,
    review_branch: str | None,
) -> None:
    base_short = scratch_base_sha[:12]
    pr_base = _pr_base_from_review_branch(review_branch)
    origin_url = _clone_origin_url(clone_dir)

    print(f"  {DIM}The clone is at: {clone_dir}{RESET}")
    print(f"  {DIM}Your original repo at {original_workdir} was not touched.{RESET}")
    if is_remote_url(origin_url):
        print(f"  {DIM}The clone's origin points at {origin_url} — you can push directly from it.{RESET}\n")
        _print_clone_push_direct(
            clone_dir=clone_dir,
            scratch_branch=scratch_branch,
            base_short=base_short,
            pr_base=pr_base,
        )
    else:
        print()
        _print_clone_push_via_original(
            clone_dir=clone_dir,
            scratch_branch=scratch_branch,
            base_short=base_short,
            pr_base=pr_base,
            original_workdir=original_workdir,
        )


def _print_clone_push_direct(
    *,
    clone_dir: str,
    scratch_branch: str,
    base_short: str,
    pr_base: str,
) -> None:
    print(f"  {BOLD}Next steps — review, then push and open a PR:{RESET}\n")

    print(f"  {BOLD}1. Switch into the clone{RESET}")
    print(f"     cd {clone_dir}\n")

    print(f"  {BOLD}2. Review what checkloop changed{RESET}")
    print(f"     git log --oneline {base_short}..{scratch_branch}")
    print(f"     git diff {base_short}..{scratch_branch}\n")

    print(f"  {BOLD}3. Optional — ask Claude for a final review of the diff{RESET}")
    print(f"     claude \"Review the diff between {base_short} and HEAD on this branch. "
          f"Flag anything that looks incorrect, risky, or lower quality than the original.\"\n")

    print(f"  {BOLD}4. Push the scratch branch to origin{RESET}")
    print(f"     git push -u origin {scratch_branch}\n")

    print(f"  {BOLD}5. Open a PR targeting {pr_base}{RESET}")
    print(f"     gh pr create --base {pr_base} --head {scratch_branch}\n")

    print(f"  {BOLD}6. Review the PR (yourself or with your team), then merge it{RESET}")
    print(f"     {DIM}checkloop does not merge for you — that is your call.{RESET}\n")

    print(f"  {DIM}When you're done: rm -rf {clone_dir}{RESET}\n")


def _print_clone_push_via_original(
    *,
    clone_dir: str,
    scratch_branch: str,
    base_short: str,
    pr_base: str,
    original_workdir: str,
) -> None:
    print(f"  {DIM}The clone's origin is a local path, so push goes through your original repo.{RESET}\n")
    print(f"  {BOLD}Next steps — review, then push and open a PR:{RESET}\n")

    print(f"  {BOLD}1. Review what checkloop changed{RESET}")
    print(f"     git -C {clone_dir} log --oneline {base_short}..{scratch_branch}")
    print(f"     git -C {clone_dir} diff {base_short}..{scratch_branch}\n")

    print(f"  {BOLD}2. Optional — ask Claude for a final review of the diff{RESET}")
    print(f"     cd {clone_dir}")
    print(f"     claude \"Review the diff between {base_short} and HEAD on this branch. "
          f"Flag anything that looks incorrect, risky, or lower quality than the original.\"\n")

    print(f"  {BOLD}3. Pull the scratch branch into your original repo{RESET}")
    print(f"     cd {original_workdir}")
    print(f"     git fetch {clone_dir} {scratch_branch}:{scratch_branch}\n")

    print(f"  {BOLD}4. Push and open a PR targeting {pr_base}{RESET}")
    print(f"     git push -u origin {scratch_branch}")
    print(f"     gh pr create --base {pr_base} --head {scratch_branch}\n")

    print(f"  {BOLD}5. Review the PR (yourself or with your team), then merge it{RESET}")
    print(f"     {DIM}checkloop does not merge for you — that is your call.{RESET}\n")

    print(f"  {DIM}Alternatives:{RESET}")
    print(f"  {DIM}  Adopt locally without a PR:   git merge --ff-only {scratch_branch}{RESET}")
    print(f"  {DIM}  Cherry-pick specific commits: git cherry-pick <sha>{RESET}")
    print(f"  {DIM}  Discard everything:           rm -rf {clone_dir}{RESET}")
    print(f"  {DIM}                                git branch -D {scratch_branch}  # if already fetched{RESET}\n")


def _print_in_place_adoption_commands(
    *,
    scratch_branch: str,
    scratch_base_sha: str,
    original_branch: str | None,
) -> None:
    base_short = scratch_base_sha[:12]
    target_branch = original_branch or "<your-branch>"
    if original_branch:
        print(f"  {DIM}Your original branch ({original_branch}) was not modified.{RESET}\n")
    else:
        print(f"  {DIM}HEAD was detached when the run started — there is no original branch to return to.{RESET}\n")

    print(f"  {BOLD}Next steps — review, then push and open a PR:{RESET}\n")

    print(f"  {BOLD}1. Review what checkloop changed{RESET}")
    print(f"     git log --oneline {base_short}..{scratch_branch}")
    print(f"     git diff {base_short}..{scratch_branch}\n")

    print(f"  {BOLD}2. Optional — ask Claude for a final review of the diff{RESET}")
    print(f"     claude \"Review the diff between {base_short} and {scratch_branch}. "
          f"Flag anything that looks incorrect, risky, or lower quality than the original.\"\n")

    print(f"  {BOLD}3. Push and open a PR targeting {target_branch}{RESET}")
    print(f"     git push -u origin {scratch_branch}")
    print(f"     gh pr create --base {target_branch} --head {scratch_branch}\n")

    print(f"  {BOLD}4. Review the PR (yourself or with your team), then merge it{RESET}")
    print(f"     {DIM}checkloop does not merge for you — that is your call.{RESET}\n")

    print(f"  {DIM}Alternatives:{RESET}")
    print(f"  {DIM}  Adopt locally without a PR:   git switch {target_branch}{RESET}")
    print(f"  {DIM}                                git merge --ff-only {scratch_branch}{RESET}")
    print(f"  {DIM}  Cherry-pick specific commits: git switch {target_branch}{RESET}")
    print(f"  {DIM}                                git cherry-pick <sha>{RESET}")
    print(f"  {DIM}  Discard everything:           git switch {target_branch}{RESET}")
    print(f"  {DIM}                                git branch -D {scratch_branch}{RESET}\n")


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
        _run_check_suite(
            selected_checks, num_cycles, workdir, args,
            convergence_threshold, resume_from=resume_from,
            all_outcomes=all_outcomes,
        )
    except KeyboardInterrupt:
        elapsed = format_duration(time.time() - suite_start_time)
        logger.warning("Suite interrupted by user after %s", elapsed)
        print_status(f"\nInterrupted after {elapsed}. Partial results may have been applied.", YELLOW)
        _print_summary(all_outcomes, elapsed)
        _print_recommendations(workdir, suite_start_time, args.dry_run)
        _print_scratch_branch_summary(
            workdir,
            getattr(args, "scratch_branch", None),
            getattr(args, "scratch_base_sha", None),
            getattr(args, "original_branch", None),
            args.dry_run,
            clone_mode=getattr(args, "clone_mode", False),
            original_workdir=getattr(args, "original_workdir", None),
            review_branch=getattr(args, "review_branch", None),
        )
        sys.exit(130)
    except FileNotFoundError as exc:
        logger.error("Required external tool not found: %s", exc, exc_info=True)
        fatal(f"Required tool not found: {exc}. Ensure git and claude are installed.")
    except Exception:
        logger.exception("Unexpected error during check suite")
        elapsed = format_duration(time.time() - suite_start_time)
        print_status(f"\nUnexpected error after {elapsed}. Partial results may have been applied.", RED)
        _print_summary(all_outcomes, elapsed)
        _print_recommendations(workdir, suite_start_time, args.dry_run)
        _print_scratch_branch_summary(
            workdir,
            getattr(args, "scratch_branch", None),
            getattr(args, "scratch_base_sha", None),
            getattr(args, "original_branch", None),
            args.dry_run,
            clone_mode=getattr(args, "clone_mode", False),
            original_workdir=getattr(args, "original_workdir", None),
            review_branch=getattr(args, "review_branch", None),
        )
        raise
    suite_elapsed = format_duration(time.time() - suite_start_time)
    logger.info("Suite completed: elapsed=%s, checks=%d, cycles=%d",
                suite_elapsed, len(selected_checks), num_cycles)
    _print_summary(all_outcomes, suite_elapsed)
    print_banner(f"All done! ({suite_elapsed} total)", GREEN)
    _print_recommendations(workdir, suite_start_time, args.dry_run)
    _print_scratch_branch_summary(
        workdir,
        getattr(args, "scratch_branch", None),
        getattr(args, "scratch_base_sha", None),
        getattr(args, "original_branch", None),
        args.dry_run,
        clone_mode=getattr(args, "clone_mode", False),
        original_workdir=getattr(args, "original_workdir", None),
        review_branch=getattr(args, "review_branch", None),
    )
