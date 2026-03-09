"""Single-check execution: prompt assembly, invocation, and change reporting.

Handles the lifecycle of running one check: building the prompt from the
check definition and CLI args, invoking Claude Code, detecting whether the
check produced any changes, and optionally running a follow-up fix when
the check is killed for excessive memory usage.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass

from checkloop.checks import (
    COMMIT_MESSAGE_INSTRUCTIONS,
    CheckDef,
    FULL_CODEBASE_SCOPE,
    looks_dangerous,
)
from checkloop.git import (
    compute_change_stats,
    git_commit_all,
    git_head_sha,
)
from checkloop.process import KILL_REASON_MEMORY, CheckResult, run_claude
from checkloop.terminal import (
    CYAN,
    GREEN,
    SummaryRow,
    YELLOW,
    format_duration,
    print_banner,
    print_status,
)

logger = logging.getLogger(__name__)


# --- Per-check outcome tracking -----------------------------------------------

@dataclass
class CheckOutcome:
    """Result of a single check execution, used for the post-run summary.

    Attributes:
        check_id: Short identifier of the check (e.g. ``"readability"``).
        label: Human-readable check name shown in banners.
        cycle: Which cycle this check ran in (1-based).
        exit_code: Subprocess exit code (0 = success).
        kill_reason: One of the ``KILL_REASON_*`` constants if killed, else None.
        made_changes: Whether the check modified any tracked files.
        lines_changed: Total insertions + deletions, or None if unavailable.
        change_pct: Percentage of total tracked lines changed, or None.
        duration_seconds: Wall-clock time the check took.
    """

    check_id: str
    label: str
    cycle: int
    exit_code: int
    kill_reason: str | None
    made_changes: bool
    lines_changed: int | None
    change_pct: float | None
    duration_seconds: float

    def to_summary_dict(self) -> SummaryRow:
        """Convert to a SummaryRow for print_run_summary_table."""
        return SummaryRow(
            check_id=self.check_id,
            label=self.label,
            cycle=self.cycle,
            exit_code=self.exit_code,
            kill_reason=self.kill_reason,
            made_changes=self.made_changes,
            lines_changed=self.lines_changed,
            change_pct=self.change_pct,
            duration=format_duration(self.duration_seconds),
        )


def _make_outcome(
    check: CheckDef,
    cycle: int,
    check_start: float,
    *,
    exit_code: int = 0,
    kill_reason: str | None = None,
    made_changes: bool = False,
    lines_changed: int | None = None,
    change_pct: float | None = None,
) -> CheckOutcome:
    """Build a CheckOutcome with the common fields filled in from context."""
    return CheckOutcome(
        check_id=check["id"],
        label=check["label"],
        cycle=cycle,
        exit_code=exit_code,
        kill_reason=kill_reason,
        made_changes=made_changes,
        lines_changed=lines_changed,
        change_pct=change_pct,
        duration_seconds=time.time() - check_start,
    )


# --- Prompt assembly ---------------------------------------------------------

_MEMORY_FIX_PROMPT = (
    "The previous check was killed because its child processes consumed too much memory "
    "({rss_limit}MB limit exceeded). Before doing anything else, investigate and fix the "
    "root cause of excessive memory usage in this project's test suite or build process. "
    "Common causes include:\n"
    "- pytest with --cov in pyproject.toml addopts (forces coverage on every run)\n"
    "- Missing test timeouts (add pytest-timeout with a reasonable default)\n"
    "- Tests that load very large datasets into memory\n"
    "- Infinite loops or unbounded recursion in tests\n"
    "Fix the root cause so that running the test suite stays within normal memory bounds."
)


def _build_check_prompt(check: CheckDef, args: argparse.Namespace) -> str:
    """Assemble the full prompt for a check from its definition and CLI args.

    Prepends the scope prefix (--changed-only file list or the default
    "review ALL code" instruction) and appends commit-message rules.
    """
    scope_prefix = getattr(args, "changed_files_prefix", "") or FULL_CODEBASE_SCOPE
    prompt = scope_prefix + check["prompt"] + COMMIT_MESSAGE_INSTRUCTIONS
    scope_mode = "changed-only" if getattr(args, "changed_files_prefix", "") else "full-codebase"
    logger.debug("Built prompt for check '%s': scope=%s, length=%d chars", check["id"], scope_mode, len(prompt))
    return prompt


# --- Claude invocation --------------------------------------------------------

def _invoke_claude(
    prompt: str,
    workdir: str,
    args: argparse.Namespace,
) -> CheckResult:
    """Call run_claude with the standard set of arguments from args."""
    return run_claude(
        prompt,
        workdir,
        skip_permissions=args.dangerously_skip_permissions,
        dry_run=args.dry_run,
        idle_timeout=args.idle_timeout,
        debug=args.debug,
        check_timeout=args.check_timeout,
        max_memory_mb=args.max_memory_mb,
    )


# --- Memory-fix follow-up ----------------------------------------------------

def _run_memory_fix(
    workdir: str,
    args: argparse.Namespace,
    is_git: bool,
) -> None:
    """Run a one-shot follow-up check to diagnose and fix excessive memory usage.

    This is triggered automatically when a check is killed for exceeding the
    memory limit.  The follow-up prompt instructs Claude to investigate common
    causes (--cov in addopts, missing test timeouts, etc.) and fix them.

    This is a best-effort operation — any failure is logged and the suite
    continues.
    """
    logger.info("Running memory-fix follow-up after OOM kill (limit=%dMB)", args.max_memory_mb)
    print_banner("Memory fix — investigating excessive memory usage", YELLOW)
    try:
        fix_prompt = _MEMORY_FIX_PROMPT.format(rss_limit=args.max_memory_mb) + COMMIT_MESSAGE_INSTRUCTIONS

        fix_result = _invoke_claude(fix_prompt, workdir, args)
        if fix_result.exit_code != 0:
            logger.warning("Memory-fix check exited with code %d", fix_result.exit_code)
            print_status("Memory-fix check did not complete cleanly. Continuing...", YELLOW)
        else:
            logger.info("Memory-fix follow-up completed successfully")
            print_status("Memory-fix check completed.", GREEN)

        if is_git:
            committed = git_commit_all(workdir, "Commit uncommitted changes left by memory-fix check")
            if committed:
                print_status("  Committed memory-fix changes.", GREEN)
    except Exception as exc:
        logger.error("Memory-fix follow-up failed: %s", exc, exc_info=True)
        print_status("Memory-fix follow-up failed — continuing with remaining checks.", YELLOW)


# --- Change detection ---------------------------------------------------------

def _report_check_changes(
    workdir: str, check_id: str, sha_before: str | None,
) -> tuple[bool, int | None, float | None]:
    """Compare git state before/after a check and print stats.

    Returns ``(made_changes, lines_changed, change_pct)``.
    """
    if sha_before is None:
        logger.info("Change detection unavailable for check '%s' (not a git repo or HEAD unreadable)", check_id)
        return True, None, None
    sha_after = git_head_sha(workdir)
    if sha_after is None:
        logger.warning("Could not read HEAD SHA after check '%s' — assuming changes were made", check_id)
        return True, None, None
    if sha_after == sha_before:
        print_status(f"  {check_id}: no changes")
        return False, 0, 0.0
    lines_changed, pct = compute_change_stats(workdir, sha_before)
    print_status(f"  {check_id}: {lines_changed} lines changed ({pct:.2f}% of codebase)")
    return True, lines_changed, pct


# --- Single check execution --------------------------------------------------

def run_single_check(
    check: CheckDef,
    workdir: str,
    args: argparse.Namespace,
    step_label: str,
    *,
    is_git: bool = False,
    cycle: int = 1,
) -> CheckOutcome:
    """Execute a single check.

    Builds the prompt, checks for dangerous keywords, snapshots the git
    state, invokes Claude Code, and reports what changed.

    If the check is killed for exceeding the memory limit, a follow-up
    "memory fix" check is run once to diagnose and fix the root cause
    before the suite continues.

    Returns a ``CheckOutcome`` with full details for the post-run summary.
    """
    check_start = time.time()
    logger.info("Check started: id=%s, label=%s, step=%s", check["id"], check["label"], step_label)
    print_banner(f"{step_label} {check['label']}", CYAN)

    prompt = _build_check_prompt(check, args)

    if looks_dangerous(prompt):
        logger.warning("Skipping check '%s' — dangerous keywords detected in prompt", check["id"])
        print_status(f"Skipping '{check['id']}' — dangerous keywords detected.", YELLOW)
        return _make_outcome(check, cycle, check_start, exit_code=-1, kill_reason="dangerous_prompt")

    sha_before = git_head_sha(workdir) if is_git else None

    try:
        result = _invoke_claude(prompt, workdir, args)
    except Exception as exc:
        logger.error("Check '%s' raised an unexpected exception: %s", check["id"], exc, exc_info=True)
        print_status(f"Check '{check['id']}' failed with error: {exc}. Continuing...", YELLOW)
        return _make_outcome(check, cycle, check_start, exit_code=-1)

    if result.kill_reason == KILL_REASON_MEMORY:
        _run_memory_fix(workdir, args, is_git)

    if result.exit_code != 0:
        logger.warning("Check '%s' exited with code %d (kill_reason=%s)",
                       check["id"], result.exit_code, result.kill_reason)
        print_status(f"Check '{check['id']}' exited with code {result.exit_code}. Continuing...", YELLOW)

    if is_git:
        committed = git_commit_all(
            workdir,
            f"Commit uncommitted changes left by '{check['id']}' check",
        )
        if not committed:
            logger.debug("No uncommitted changes left after check '%s'", check["id"])
    made_changes, lines_changed, change_pct = _report_check_changes(workdir, check["id"], sha_before)
    elapsed = time.time() - check_start
    logger.info("Check '%s' completed: made_changes=%s, lines_changed=%s, duration=%.1fs",
                check["id"], made_changes, lines_changed, elapsed)
    return _make_outcome(
        check, cycle, check_start,
        exit_code=result.exit_code, kill_reason=result.kill_reason,
        made_changes=made_changes, lines_changed=lines_changed, change_pct=change_pct,
    )
