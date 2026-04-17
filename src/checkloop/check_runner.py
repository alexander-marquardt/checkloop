"""Single-check execution: prompt assembly, invocation, and change reporting.

Handles the lifecycle of running one check: building the prompt from the
check definition and CLI args, invoking Claude Code, detecting whether the
check produced any changes, and optionally running a follow-up fix when
the check is killed for excessive memory usage.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from checkloop.checks import (
    COMMIT_MESSAGE_INSTRUCTIONS,
    CheckDef,
    FULL_CODEBASE_SCOPE,
    HIDE_AI_ATTRIBUTION,
    looks_dangerous,
)
from checkloop.commit_message import generate_commit_message
from checkloop.git import (
    compute_change_stats,
    compute_file_stats,
    get_uncommitted_diff,
    git_commit_all,
    git_head_sha,
    has_uncommitted_changes,
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


def _commit_with_generated_message(workdir: str, args: argparse.Namespace, fallback: str) -> None:
    """Commit any uncommitted changes using a Claude-generated message, or fallback."""
    if not has_uncommitted_changes(workdir):
        return
    diff = get_uncommitted_diff(workdir)
    skip = getattr(args, "dangerously_skip_permissions", False)
    model = getattr(args, "model", None)
    claude_cmd = getattr(args, "claude_command", "claude")
    message = generate_commit_message(diff, workdir, skip_permissions=skip, model=model, claude_command=claude_cmd) or fallback
    git_commit_all(workdir, message)


# --- Per-check outcome tracking -----------------------------------------------

@dataclass
class CheckOutcome:
    """Result of a single check execution, used for the post-run summary."""

    check_id: str
    label: str
    cycle: int
    exit_code: int
    kill_reason: str | None
    made_changes: bool
    lines_changed: int | None
    change_pct: float | None
    duration_seconds: float

    def to_summary_row(self) -> SummaryRow:
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
    "review ALL code" instruction), injects the project map if available,
    and appends commit-message rules.
    """
    changed_files_prefix = getattr(args, "changed_files_prefix", "")
    scope_prefix = changed_files_prefix or FULL_CODEBASE_SCOPE
    project_map = getattr(args, "project_map", "")
    map_section = (
        f"\n\nHere is an overview of this project's structure:\n\n{project_map}\n\n"
        if project_map else ""
    )
    commit_suffix = COMMIT_MESSAGE_INSTRUCTIONS
    if not getattr(args, "allow_ai_attribution", False):
        commit_suffix += HIDE_AI_ATTRIBUTION
    prompt = scope_prefix + map_section + check["prompt"] + commit_suffix
    return prompt


# --- Claude invocation --------------------------------------------------------

def _invoke_claude(
    prompt: str,
    workdir: str,
    args: argparse.Namespace,
    *,
    model: str | None = None,
    idle_timeout_override: int | None = None,
    raw_log_file: IO[bytes] | None = None,
) -> CheckResult:
    effective_model = model or getattr(args, "model", None)
    effective_idle_timeout = idle_timeout_override if idle_timeout_override is not None else args.idle_timeout
    claude_cmd = getattr(args, "claude_command", "claude")
    return run_claude(
        prompt,
        workdir,
        skip_permissions=args.dangerously_skip_permissions,
        dry_run=args.dry_run,
        idle_timeout=effective_idle_timeout,
        debug=args.debug,
        check_timeout=args.check_timeout,
        max_memory_mb=args.max_memory_mb,
        model=effective_model,
        claude_command=claude_cmd,
        raw_log_file=raw_log_file,
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
        commit_suffix = COMMIT_MESSAGE_INSTRUCTIONS
        if not getattr(args, "allow_ai_attribution", False):
            commit_suffix += HIDE_AI_ATTRIBUTION
        fix_prompt = _MEMORY_FIX_PROMPT.format(rss_limit=args.max_memory_mb) + commit_suffix

        fix_result = _invoke_claude(fix_prompt, workdir, args)
        if fix_result.exit_code != 0:
            logger.warning("Memory-fix check exited with code %d", fix_result.exit_code)
            print_status("Memory-fix check did not complete cleanly. Continuing...", YELLOW)
        else:
            logger.info("Memory-fix follow-up completed successfully")
            print_status("Memory-fix check completed.", GREEN)

        if is_git:
            _commit_with_generated_message(workdir, args, "Fix excessive memory usage in test suite")
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
    lines_added, lines_deleted, lines_changed, pct = compute_change_stats(workdir, sha_before)
    # Validate internal consistency: insertions + deletions should equal total changed.
    if lines_added + lines_deleted != lines_changed:
        logger.warning("Line stats inconsistent: %d + %d != %d", lines_added, lines_deleted, lines_changed)
    files_added, files_deleted, files_modified = compute_file_stats(workdir, sha_before)
    # Build human-readable breakdown strings.
    line_parts = []
    if lines_added:
        line_parts.append(f"+{lines_added}")
    if lines_deleted:
        line_parts.append(f"-{lines_deleted}")
    lines_breakdown = f" ({'/'.join(line_parts)})" if line_parts else ""
    file_parts = []
    if files_added:
        file_parts.append(f"+{files_added}")
    if files_deleted:
        file_parts.append(f"-{files_deleted}")
    if files_modified:
        file_parts.append(f"~{files_modified}")
    files_str = f" [{'/'.join(file_parts)} files]" if file_parts else ""
    print_status(f"  {check_id}: {lines_changed} lines{lines_breakdown} ({pct:.2f}% of codebase){files_str}")
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
    model: str | None = None,
    idle_timeout_override: int | None = None,
) -> CheckOutcome:
    """Execute a single check.

    Builds the prompt, checks for dangerous keywords, snapshots the git
    state, invokes Claude Code, and reports what changed.

    If the check is killed for exceeding the memory limit, a follow-up
    "memory fix" check is run once to diagnose and fix the root cause
    before the suite continues.

    When *model* is provided, it overrides the CLI ``--model`` flag for
    this specific check.  This is used by the per-check model assignment
    from tier configuration files.

    When *idle_timeout_override* is provided, it overrides the CLI
    ``--idle-timeout`` flag for this specific check.  This allows checks
    that require extended thinking (security, concurrency) to have longer
    timeouts without affecting other checks.

    Returns a ``CheckOutcome`` with full details for the post-run summary.
    """
    check_start = time.time()
    model_label = f", model={model}" if model else ""
    logger.info("Check started: id=%s, label=%s, step=%s%s", check["id"], check["label"], step_label, model_label)
    print_banner(f"{step_label} {check['label']}", CYAN, timestamp=True)

    prompt = _build_check_prompt(check, args)

    if looks_dangerous(prompt):
        logger.warning("Skipping check '%s' — dangerous keywords detected in prompt", check["id"])
        print_status(f"Skipping '{check['id']}' — dangerous keywords detected.", YELLOW)
        return _make_outcome(check, cycle, check_start, exit_code=-1, kill_reason="dangerous_prompt")

    sha_before = git_head_sha(workdir) if is_git else None

    log_dir = Path(workdir) / ".checkloop-logs"
    log_path = log_dir / f"{check['id']}_cycle{cycle}.jsonl"
    try:
        log_dir.mkdir(exist_ok=True)
        raw_log = open(log_path, "wb")  # noqa: SIM115
    except OSError as exc:
        logger.warning("Could not open raw log %s: %s", log_path, exc)
        raw_log = None

    try:
        result = _invoke_claude(
            prompt, workdir, args,
            model=model, idle_timeout_override=idle_timeout_override, raw_log_file=raw_log,
        )
    except Exception as exc:
        logger.error("Check '%s' raised an unexpected exception: %s", check["id"], exc, exc_info=True)
        print_status(f"Check '{check['id']}' failed with error: {exc}. Continuing...", YELLOW)
        return _make_outcome(check, cycle, check_start, exit_code=-1)
    finally:
        if raw_log is not None:
            raw_log.close()

    if result.kill_reason == KILL_REASON_MEMORY:
        _run_memory_fix(workdir, args, is_git)

    if result.exit_code != 0:
        logger.warning("Check '%s' exited with code %d (kill_reason=%s)",
                       check["id"], result.exit_code, result.kill_reason)
        print_status(f"Check '{check['id']}' exited with code {result.exit_code}. Continuing...", YELLOW)

    if is_git:
        _commit_with_generated_message(workdir, args, f"Apply {check['id']} check improvements")
    made_changes, lines_changed, change_pct = _report_check_changes(workdir, check["id"], sha_before)
    elapsed = time.time() - check_start
    logger.info("Check '%s' completed: made_changes=%s, lines_changed=%s, duration=%.1fs",
                check["id"], made_changes, lines_changed, elapsed)
    return _make_outcome(
        check, cycle, check_start,
        exit_code=result.exit_code, kill_reason=result.kill_reason,
        made_changes=made_changes, lines_changed=lines_changed, change_pct=change_pct,
    )
