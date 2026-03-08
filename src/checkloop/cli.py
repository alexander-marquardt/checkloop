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
import json
import logging
import math
import os
import re
import resource
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import IO, Any, Literal, NoReturn, overload

logger = logging.getLogger(__name__)

# --- ANSI helpers -------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
BLUE   = "\033[94m"


RULE_WIDTH = 72  # character width for banner horizontal rules
DEFAULT_IDLE_TIMEOUT = 300  # seconds before killing a silent subprocess
DEFAULT_PAUSE_SECONDS = 2  # seconds between consecutive checks
DEFAULT_CONVERGENCE_THRESHOLD = 0.1  # percent of total lines changed

_READ_CHUNK_SIZE = 8192  # bytes per stdout read during streaming
_DRAIN_CHUNK_SIZE = 65536  # bytes per read when draining after process exit
_PROCESS_WAIT_TIMEOUT = 5  # seconds to wait for process group to die
_PRE_RUN_WARNING_DELAY = 5  # countdown seconds before starting review
_BASH_DISPLAY_LIMIT = 80  # max chars shown for bash commands in tool summaries

# Perf: build once instead of copying os.environ on every subprocess spawn.
# Strips CLAUDECODE env var whose presence causes nested claude processes
# to refuse to start when checkloop is invoked from within a Claude Code session.
_SANITIZED_ENV: dict[str, str] = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

FULL_CODEBASE_SCOPE: str = (
    "Review ALL code in this project (not just recently written code). "
)
"""Default scope prefix prepended to every check when --changed-only is not used."""

COMMIT_MESSAGE_INSTRUCTIONS: str = (
    "\n\nIf you make any git commits, follow these commit message rules:\n"
    "- Maximum 5-10 lines\n"
    "- Do not mention Claude, AI, or any AI tools\n"
    "- Do not add Co-Authored-By or Signed-off-by trailers\n"
    "- Provide only a high-level summary of what was cleaned up, fixed, or changed\n"
    "- Use clear, professional commit message style"
)
"""Instructions appended to every check prompt to enforce clean commit messages."""


def _fatal(msg: str) -> NoReturn:
    """Log an error, print it in red, and exit with code 1."""
    logger.error("%s", msg)
    _print_status(msg, RED)
    sys.exit(1)


def _measure_current_rss_mb() -> float:
    """Return the current RSS of this process in MB (not peak — actual current)."""
    try:
        pid = os.getpid()
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            # ps may return multiple lines; take only the first non-empty line.
            first_line = result.stdout.strip().splitlines()[0].strip()
            return int(first_line) / 1024  # ps reports in KB
    except (OSError, ValueError) as exc:
        logger.debug("ps-based RSS lookup failed: %s", exc)
    # Fallback: use resource (peak, not current — better than nothing)
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS reports ru_maxrss in bytes; Linux reports in kilobytes.
        scale = 1024 * 1024 if sys.platform == "darwin" else 1024
        return usage.ru_maxrss / scale
    except OSError as exc:
        logger.warning("resource.getrusage() failed: %s", exc)
        return 0.0


def _find_child_pids() -> list[int]:
    """Return PIDs of surviving child processes (including orphaned grandchildren)."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True, text=True,
        )
    except OSError as exc:
        logger.debug("pgrep failed: %s", exc)
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    pids: list[int] = []
    for line in result.stdout.strip().split("\n"):
        try:
            pids.append(int(line.strip()))
        except ValueError:
            logger.debug("pgrep returned non-integer PID line: %r", line)
    return pids


def _kill_orphaned_children(pids: list[int] | None = None) -> int:
    """Kill surviving child processes. Returns count killed.

    Accepts an optional pre-fetched pid list to avoid a redundant pgrep spawn.
    """
    killed = 0
    for child_pid in (pids if pids is not None else _find_child_pids()):
        try:
            os.kill(child_pid, signal.SIGKILL)
            killed += 1
            logger.warning("Killed orphaned child process %d", child_pid)
        except OSError as exc:
            logger.debug("Could not kill child %d: %s", child_pid, exc)
    return killed


def _log_memory_usage(label: str) -> None:
    """Log current RSS and child process count after each check."""
    rss_mb = _measure_current_rss_mb()
    child_pids = _find_child_pids()
    logger.info("Memory [%s]: rss=%.0fMB, children=%d", label, rss_mb, len(child_pids))
    _print_status(f"  Memory: {rss_mb:.0f}MB RSS, {len(child_pids)} child processes", DIM)
    if child_pids:
        _warn_and_kill_orphan_processes(child_pids)


def _warn_and_kill_orphan_processes(child_pids: list[int]) -> None:
    """Warn about surviving child processes and kill them."""
    _print_status(f"  Warning: {len(child_pids)} child process(es) still alive — killing.", YELLOW)
    # Pass pids directly to avoid a second pgrep subprocess spawn.
    killed = _kill_orphaned_children(child_pids)
    if killed:
        _print_status(f"  Killed {killed} orphaned process(es).", YELLOW)


# --- Git helpers (convergence detection) --------------------------------------

@overload
def _git_run(
    workdir: str,
    *args: str,
    check: bool = False,
    text: Literal[True] = ...,
) -> subprocess.CompletedProcess[str]: ...

@overload
def _git_run(
    workdir: str,
    *args: str,
    check: bool = False,
    text: Literal[False] = ...,
) -> subprocess.CompletedProcess[bytes]: ...

def _git_run(
    workdir: str,
    *args: str,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a git command in *workdir* with captured output."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=text,
            check=check,
        )
    except FileNotFoundError:
        logger.error("git binary not found — is git installed?")
        raise
    except OSError as exc:
        logger.error("Failed to run git %s: %s", args[0] if args else "", exc, exc_info=True)
        raise


def _is_git_repo(workdir: str) -> bool:
    """Return True if workdir is inside a git repository."""
    try:
        is_repo = _git_run(workdir, "rev-parse", "--is-inside-work-tree").returncode == 0
    except OSError as exc:
        logger.warning("Could not check git repo status for %s: %s", workdir, exc)
        return False
    if not is_repo:
        logger.info("Not a git repo: %s", workdir)
    return is_repo


def _git_head_sha(workdir: str) -> str | None:
    """Return the current HEAD commit SHA, or None if unavailable."""
    try:
        result = _git_run(workdir, "rev-parse", "HEAD")
    except OSError as exc:
        logger.warning("Could not read HEAD SHA in %s: %s", workdir, exc)
        return None
    sha = result.stdout.strip() if result.returncode == 0 else ""
    return sha or None  # treat empty stdout as unavailable


def _git_commit_all(workdir: str, message: str) -> bool:
    """Stage and commit any uncommitted changes.

    Returns True if a commit was created (i.e. there were changes to commit).
    """
    try:
        _git_run(workdir, "add", "-A", check=True)
        if _git_run(workdir, "diff", "--cached", "--quiet").returncode == 0:
            logger.debug("No staged changes — nothing to commit")
            return False
        _git_run(workdir, "commit", "-m", message, check=True)
        new_sha = _git_head_sha(workdir)
        logger.info("Committed: %s (sha=%s)", message, new_sha)
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Git commit failed: %s", exc, exc_info=True)
        return False


def _parse_shortstat(text: str) -> int:
    """Parse ``git diff --shortstat`` output into total lines changed."""
    insertions = deletions = 0
    match = re.search(r"(\d+) insertion", text)
    if match:
        insertions = int(match.group(1))
    match = re.search(r"(\d+) deletion", text)
    if match:
        deletions = int(match.group(1))
    if insertions == 0 and deletions == 0 and text.strip():
        logger.debug("No insertions/deletions parsed from shortstat: %r", text)
    return insertions + deletions


def _count_file_lines(filepath: Path) -> int:
    """Count newlines in a text file, reading in chunks. Returns 0 for binary files."""
    try:
        raw_file = open(filepath, "rb")
    except OSError as exc:
        logger.debug("Cannot open file for line counting %s: %s", filepath, exc)
        return 0
    with raw_file:
        try:
            # Read a small header to check for null bytes (binary file indicator).
            # If the file is text, count newlines in the header, then continue
            # counting through the rest of the file in larger chunks.
            header = raw_file.read(_READ_CHUNK_SIZE)
            if b"\0" in header:
                return 0
            total = header.count(b"\n")
            for chunk in iter(lambda: raw_file.read(_DRAIN_CHUNK_SIZE), b""):
                total += chunk.count(b"\n")
            return total
        except OSError as exc:
            logger.debug("Read error during line counting %s: %s", filepath, exc)
            return 0


def _count_tracked_lines(workdir: str) -> int:
    """Count total lines across all git-tracked text files.

    Reads files in small chunks to avoid loading large files entirely into
    memory, which matters for long-running sessions on big repos.
    """
    start_time = time.time()
    try:
        ls_result = _git_run(workdir, "ls-files", "-z", text=False)
    except OSError as exc:
        logger.warning("git ls-files failed in %s: %s", workdir, exc)
        return 1  # avoid division by zero
    if ls_result.returncode != 0:
        logger.warning("git ls-files failed (rc=%d) — cannot count tracked lines", ls_result.returncode)
        return 1  # avoid division by zero
    # -z flag outputs null-separated paths; split once, iterate as generator
    # to avoid materialising a full decoded-path list for large repos.
    raw_path_segments = ls_result.stdout.split(b"\0")
    total_lines = 0
    file_count = 0
    resolved_workdir = Path(workdir).resolve()
    for raw_path in raw_path_segments:
        if not raw_path:
            continue
        relative_path = raw_path.decode("utf-8", errors="replace")
        file_count += 1
        try:
            absolute_path = (resolved_workdir / relative_path).resolve()
            if not absolute_path.is_relative_to(resolved_workdir):
                continue  # skip paths that escape the workdir (path traversal guard)
            total_lines += _count_file_lines(absolute_path)
        except OSError as exc:
            logger.debug("Could not read tracked file %s: %s", relative_path, exc)
    # Clamp to minimum 1 to prevent division-by-zero in convergence percentage calculations.
    total_clamped = max(total_lines, 1)
    elapsed = time.time() - start_time
    logger.info("Counted %d tracked lines across %d files in %.2fs", total_clamped, file_count, elapsed)
    return total_clamped


# Cache: resolved workdir path → total tracked line count. Avoids re-scanning per check.
_total_lines_cache: dict[str, int] = {}


def _count_lines_changed(workdir: str, base_sha: str, target: str = "HEAD") -> int:
    """Return total lines changed (insertions + deletions) between two refs.

    If *target* is ``"HEAD"``, compares *base_sha* to ``HEAD``.  Pass a different
    ref or SHA to compare arbitrary points.  To include uncommitted working-tree
    changes, pass ``target=""`` (empty string triggers ``git diff <base>``).
    """
    if not base_sha:
        logger.warning("_count_lines_changed called with empty base_sha")
        return 0
    diff_args = ["diff", "--shortstat", base_sha]
    if target:  # empty string means diff against working tree (uncommitted changes)
        diff_args.append(target)
    try:
        result = _git_run(workdir, *diff_args)
    except OSError as exc:
        logger.warning("git diff --shortstat failed in %s: %s", workdir, exc)
        return 0
    if result.returncode != 0:
        logger.warning("git diff --shortstat failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return 0
    return _parse_shortstat(result.stdout)


def _cached_total_tracked_lines(workdir: str) -> int:
    """Return cached total line count for all tracked files in *workdir*."""
    cache_key = str(Path(workdir).resolve())
    if cache_key not in _total_lines_cache:
        _total_lines_cache[cache_key] = _count_tracked_lines(workdir)
    return _total_lines_cache[cache_key]


def _detect_default_branch(workdir: str) -> str:
    """Return the name of the default branch (main or master), falling back to 'main'."""
    for branch in ("main", "master"):
        try:
            result = _git_run(workdir, "rev-parse", "--verify", f"refs/heads/{branch}")
        except OSError as exc:
            logger.warning("Could not verify branch '%s': %s", branch, exc)
            continue
        if result.returncode == 0:
            return branch
    return "main"


def _get_changed_files(workdir: str, base_ref: str) -> list[str]:
    """Return list of files changed between *base_ref* and HEAD.

    Uses ``git merge-base`` to find the common ancestor, then ``git diff --name-only``
    to list changed files. Returns an empty list if the diff fails.
    """
    try:
        merge_base = _git_run(workdir, "merge-base", base_ref, "HEAD")
    except OSError as exc:
        logger.warning("git merge-base failed for ref '%s': %s", base_ref, exc)
        return []
    if merge_base.returncode != 0:
        logger.warning("git merge-base failed for ref '%s' (rc=%d)", base_ref, merge_base.returncode)
        return []
    base_sha = merge_base.stdout.strip()
    try:
        result = _git_run(workdir, "diff", "--name-only", base_sha, "HEAD")
    except OSError as exc:
        logger.warning("git diff --name-only failed: %s", exc)
        return []
    if result.returncode != 0:
        logger.warning("git diff --name-only failed (rc=%d)", result.returncode)
        return []
    return [f for f in result.stdout.strip().split("\n") if f]


def _build_changed_files_prefix(changed_files: list[str]) -> str:
    """Build a prompt prefix that restricts review to the given files."""
    file_list = "\n".join(f"  - {f}" for f in changed_files)
    return (
        f"IMPORTANT: Only review the following {len(changed_files)} file(s) that have changed. "
        "Do NOT review or modify any other files.\n"
        f"Changed files:\n{file_list}\n\n"
    )


def _compute_change_stats(workdir: str, base_sha: str) -> tuple[int, float]:
    """Return (lines_changed, change_percentage) since *base_sha*."""
    lines_changed = _count_lines_changed(workdir, base_sha)
    if lines_changed == 0:
        return 0, 0.0
    return lines_changed, (lines_changed / _cached_total_tracked_lines(workdir)) * 100


def _print_banner(title: str, colour: str = CYAN) -> None:
    """Print a prominent section header with horizontal rules.

    Args:
        title: Text to display between the rules.
        colour: ANSI colour code for the banner (default: CYAN).
    """
    horizontal_rule = "\u2500" * RULE_WIDTH  # ─
    print(f"\n{colour}{BOLD}{horizontal_rule}")
    print(f"  {title}")
    print(f"{horizontal_rule}{RESET}\n")


def _print_status(msg: str, colour: str = DIM) -> None:
    """Print a coloured status message to the terminal.

    Args:
        msg: The status text to display.
        colour: ANSI colour code to wrap the message in (default: DIM).
    """
    print(f"{colour}{msg}{RESET}")


# --- Checks -------------------------------------------------------------------

# Ordered list of all available checks.
#
# Each entry is a dict with keys:
#   id:     Short identifier used on the CLI (e.g. "readability", "dry").
#   label:  Human-readable name shown in banners and summaries.
#   prompt: The review prompt sent to Claude Code for this check.
#
# Ordering matters: bookend checks (test-fix, test-validate) are first and
# last; the remaining checks are grouped by tier (basic -> thorough -> exhaustive).
CHECKS: list[dict[str, str]] = [
    # --- Bookend: run first ---
    {
        "id": "test-fix",
        "label": "Run Existing Tests & Fix Failures",
        "prompt": (
            "Find and run the existing test suite for this project. "
            "Use whatever test runner is already configured (pytest, jest, go test, cargo test, etc.). "
            "If tests fail, diagnose and fix the root cause in the SOURCE code — not by weakening or "
            "deleting the tests. Re-run until all existing tests pass. "
            "Do NOT write new tests in this step — only fix failures in the existing suite. "
            "Report what you found and fixed."
        ),
    },
    # --- Basic tier (default) ---
    {
        "id": "readability",
        "label": "Readability & Code Quality",
        "prompt": (
            "Improve naming (variables, functions, classes). "
            "Break up any function that does more than one logical thing, "
            "or that requires scrolling to read in full. "
            "Prefer small, named functions where the name removes the need for a comment. "
            "If any source file is longer than roughly 500 lines, split it into "
            "smaller, well-named modules with clear responsibilities — group "
            "related functions together and use imports to reconnect them. "
            "Apply the same standard to test files: split large test files "
            "so each module has a corresponding focused test file. "
            "Add or improve inline comments where logic is non-obvious, "
            "and ensure consistent formatting. "
            "Do NOT change any behaviour — only improve clarity."
        ),
    },
    {
        "id": "dry",
        "label": "DRY / Eliminate Repetition",
        "prompt": (
            "Find repeated or near-repeated logic. "
            "Extract shared helpers, base classes, or utility modules to eliminate "
            "duplication. Consolidate config values or magic numbers into constants. "
            "Ensure each concept has a single canonical home in the code. "
            "Do NOT change observable behaviour — only reduce repetition."
        ),
    },
    {
        "id": "tests",
        "label": "Write / Improve Tests",
        "prompt": (
            "Measure and improve test coverage. "
            "Cover: happy paths, edge cases, and error conditions. "
            "Use the testing framework already in the project (or pytest/jest if none). "
            "Target >=90% line coverage. "
            "Run the test suite and fix any failures before finishing. "
            "Report the final coverage figure when done."
        ),
    },
    {
        "id": "docs",
        "label": "Documentation",
        "prompt": (
            "Add or improve documentation: "
            "update (or create) a README section describing what was built, "
            "add docstrings/JSDoc to public functions and classes, "
            "and document any non-obvious environment variables or config."
        ),
    },
    # --- Thorough tier ---
    {
        "id": "security",
        "label": "Security Review",
        "prompt": (
            "Do a security review. "
            "Look for: injection vulnerabilities, insecure defaults, "
            "hardcoded secrets, missing input validation, "
            "overly broad permissions, and unsafe dependencies. "
            "Fix any issues you find and explain what you changed."
        ),
    },
    {
        "id": "perf",
        "label": "Performance",
        "prompt": (
            "Review for obvious performance issues: "
            "N+1 queries, missing indexes, unnecessary re-renders, "
            "blocking I/O that could be async, large allocations in loops. "
            "Fix anything significant and add a comment explaining the optimisation."
        ),
    },
    {
        "id": "errors",
        "label": "Error Handling",
        "prompt": (
            "Audit error handling. "
            "Ensure I/O operations, network calls, and parsing steps "
            "have proper try/except (or try/catch) with meaningful error messages. "
            "Add logging where it would help diagnose production issues."
        ),
    },
    {
        "id": "types",
        "label": "Type Safety",
        "prompt": (
            "Review for type safety issues. "
            "Add or fix type annotations (Python type hints, TypeScript types, JSDoc @param/@returns). "
            "Replace uses of Any, Object, or untyped collections with precise types. "
            "Ensure function signatures, return types, and class attributes are all typed. "
            "Run the type checker (mypy, tsc, etc.) if available and fix any errors. "
            "Do NOT change runtime behaviour — only improve type coverage."
        ),
    },
    # --- Exhaustive tier ---
    {
        "id": "edge-cases",
        "label": "Edge Cases & Boundary Conditions",
        "prompt": (
            "Look for unhandled edge cases and boundary conditions: "
            "off-by-one errors, empty/null/undefined inputs, integer overflow, "
            "empty collections, zero-length strings, negative numbers where unsigned expected, "
            "concurrent modification, and Unicode/encoding edge cases. "
            "Fix any issues and add tests for the edge cases you find."
        ),
    },
    {
        "id": "complexity",
        "label": "Reduce Complexity",
        "prompt": (
            "Review for excessive complexity. "
            "Simplify deeply nested conditionals (flatten with early returns or guard clauses). "
            "Break apart functions with high cyclomatic complexity. "
            "Replace complex boolean expressions with named variables or helper functions. "
            "Simplify state machines, reduce the number of code paths where possible. "
            "Do NOT change observable behaviour — only reduce complexity."
        ),
    },
    {
        "id": "deps",
        "label": "Dependency Hygiene",
        "prompt": (
            "Audit the project's dependencies for issues. "
            "Identify unused dependencies and remove them. "
            "Check for outdated packages with known vulnerabilities. "
            "Flag dependencies that are unmaintained or have better alternatives. "
            "Ensure lock files are consistent with declared dependencies. "
            "Check that dependency version constraints are neither too loose nor too tight."
        ),
    },
    {
        "id": "logging",
        "label": "Logging & Observability",
        "prompt": (
            "Review for logging and observability gaps. "
            "Ensure entry points (API routes, CLI commands, queue consumers) log "
            "request/response summaries. Add structured logging with context (request IDs, "
            "user IDs, operation names) where missing. Ensure errors are logged with stack traces. "
            "Remove or downgrade noisy debug logs that would clutter production. "
            "Add metrics or timing instrumentation to performance-critical paths if appropriate."
        ),
    },
    {
        "id": "concurrency",
        "label": "Concurrency & Thread Safety",
        "prompt": (
            "Review for concurrency issues. "
            "Look for: race conditions, shared mutable state without synchronisation, "
            "deadlock potential, missing locks around critical sections, "
            "non-atomic read-modify-write sequences, and unsafe use of globals. "
            "Check async code for missing awaits, unawaited coroutines, and blocking calls "
            "in async contexts. Fix any issues you find."
        ),
    },
    {
        "id": "accessibility",
        "label": "Accessibility (a11y)",
        "prompt": (
            "Review UI code (HTML, JSX, templates, components) for accessibility issues. "
            "Ensure: semantic HTML elements are used instead of generic divs/spans, "
            "images have meaningful alt text, form inputs have associated labels, "
            "ARIA attributes are used correctly, keyboard navigation works, "
            "colour contrast meets WCAG AA standards, and focus management is correct. "
            "If the project has no UI code, report that and skip."
        ),
    },
    {
        "id": "api-design",
        "label": "API Design & Consistency",
        "prompt": (
            "Review public APIs (REST endpoints, library interfaces, CLI commands, "
            "exported functions) for consistency and usability. "
            "Check for: consistent naming conventions, predictable parameter ordering, "
            "appropriate HTTP methods and status codes, consistent error response formats, "
            "proper use of pagination, versioning where needed, and idempotency of mutating operations. "
            "Fix inconsistencies and document any breaking changes."
        ),
    },
    # --- Bookend: run last ---
    {
        "id": "test-validate",
        "label": "Validate All Tests Pass",
        "prompt": (
            "Run the FULL test suite (including any tests written or modified during earlier checks). "
            "If any tests fail, diagnose whether the failure is due to a bug in the source code "
            "or a bad test. Fix the root cause — prefer fixing source code over weakening tests. "
            "Re-run until all tests pass. "
            "Report the final test count and results."
        ),
    },
]

# All valid check IDs, derived from CHECKS to stay in sync.
CHECK_IDS: list[str] = [p["id"] for p in CHECKS]

# --- Check tiers --------------------------------------------------------------
# Tiers control which checks run at each review depth.  Each tier is a list of
# check IDs that includes the bookend checks (test-fix first, test-validate last)
# plus a progressively larger set of checks.

_BOOKEND_FIRST_CHECKS: list[str] = ["test-fix"]
_BOOKEND_LAST_CHECKS: list[str] = ["test-validate"]
_BOOKEND_IDS: set[str] = {*_BOOKEND_FIRST_CHECKS, *_BOOKEND_LAST_CHECKS}
_CORE_BASIC: list[str] = ["readability", "dry", "tests", "docs"]
_CORE_THOROUGH: list[str] = ["security", "perf", "errors", "types"]
_CORE_EXHAUSTIVE: list[str] = ["edge-cases", "complexity", "deps", "logging", "concurrency", "accessibility", "api-design"]

# Public tier lists — used by --level and exposed for programmatic access.
TIER_BASIC: list[str] = _BOOKEND_FIRST_CHECKS + _CORE_BASIC + _BOOKEND_LAST_CHECKS
TIER_THOROUGH: list[str] = _BOOKEND_FIRST_CHECKS + _CORE_BASIC + _CORE_THOROUGH + _BOOKEND_LAST_CHECKS
TIER_EXHAUSTIVE: list[str] = CHECK_IDS  # all checks (already ordered correctly)

# Maps tier name (used by --level) to the list of check IDs for that tier.
TIERS: dict[str, list[str]] = {
    "basic": TIER_BASIC,
    "thorough": TIER_THOROUGH,
    "exhaustive": TIER_EXHAUSTIVE,
}
DEFAULT_TIER: str = "basic"

# --- Dangerous-prompt guard ---------------------------------------------------
# Safety net: reject check prompts that contain destructive keywords.
# These are checked with word-boundary-aware regexes (see _compile_danger_patterns).

_DANGEROUS_PROMPT_KEYWORDS: list[str] = [
    "rm -rf /",
    "format c:",
    "format /dev",
    "mkfs",
    "wipe disk",
    "wipe drive",
    "wipe partition",
    "delete all files",
    "drop database",
    "drop table",
    "truncate table",
    ":(){:|:&};:",
    "sudo rm",
    "chmod 777 /",
    "/etc/passwd",
    "dd if=/dev/zero",
    "dd of=/dev",
]


def _compile_danger_patterns() -> list[re.Pattern[str]]:
    """Pre-compile regex patterns for all danger keywords.

    Adds word-boundary anchors (\\b) only at alphanumeric edges, so
    "reformat" won't match "format" but "/etc/passwd" still matches.
    """
    patterns: list[re.Pattern[str]] = []
    for keyword in _DANGEROUS_PROMPT_KEYWORDS:
        if not keyword:
            continue
        escaped = re.escape(keyword)
        leading_boundary = r"\b" if keyword[0].isalnum() else ""
        trailing_boundary = r"\b" if keyword[-1].isalnum() else ""
        patterns.append(re.compile(leading_boundary + escaped + trailing_boundary, re.IGNORECASE))
    return patterns


# Perf: compile once at import time instead of rebuilding on every call.
_DANGEROUS_PROMPT_PATTERNS: list[re.Pattern[str]] = _compile_danger_patterns()


def _looks_dangerous(text: str) -> bool:
    """Check if a prompt contains any destructive keyword.

    Uses word-boundary anchors (\\b) around alphanumeric edges so e.g.
    "reformat" does not match "format", while keywords containing special
    characters like "rm -rf /" or "/etc/passwd" are still detected.
    """
    return any(pattern.search(text) for pattern in _DANGEROUS_PROMPT_PATTERNS)


# --- Claude runner ------------------------------------------------------------

def _format_duration(total_seconds: float) -> str:
    """Format elapsed seconds into a compact ``XmYYs`` or ``XhYYmZZs`` string."""
    if math.isnan(total_seconds) or math.isinf(total_seconds):
        return "0m00s"
    minutes, seconds = divmod(max(0, int(total_seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


_FILE_PATH_TOOL_NAMES: set[str] = {"read", "read_file", "edit", "edit_file", "write", "write_file"}


def _summarise_tool_use(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Return a short human-readable summary for a tool-use event."""
    normalized_name = tool_name.lower()
    if normalized_name in _FILE_PATH_TOOL_NAMES and "file_path" in tool_input:
        return f" {tool_input['file_path']}"
    if normalized_name == "bash" and "command" in tool_input:
        command = str(tool_input["command"])
        if len(command) > _BASH_DISPLAY_LIMIT:
            return f" $ {command[:_BASH_DISPLAY_LIMIT - 3]}..."
        return f" $ {command}"
    if normalized_name == "glob" and "pattern" in tool_input:
        return f" {tool_input['pattern']}"
    if normalized_name == "grep" and "pattern" in tool_input:
        return f" /{tool_input['pattern']}/"
    return ""


def _print_assistant_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print text blocks from an assistant response event."""
    content = event.get("message", {}).get("content") or []
    text_blocks = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    for text in text_blocks:
        if text.strip():
            print(f"{elapsed_prefix}{text}")


def _print_tool_use_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print a tool invocation with its name and a short summary of inputs."""
    tool_name = event.get("tool", event.get("name", "unknown"))
    detail = _summarise_tool_use(tool_name, event.get("input") or {})
    print(f"{elapsed_prefix}{BLUE}[{tool_name}]{RESET}{detail}")


def _print_system_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print a system-level message (e.g. initialisation status)."""
    system_message = event.get("message", "")
    if system_message:
        print(f"{elapsed_prefix}{DIM}{system_message}{RESET}")


def _print_result_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print the final result summary from a completed check."""
    result_text = event.get("result", "")
    if result_text:
        print(f"\n{elapsed_prefix}{GREEN}--- Result ---{RESET}")
        print(result_text)


# Type alias for event handler functions used by _print_event dispatch.
_EventHandler = Callable[[dict[str, Any], str], None]

# Maps stream-json event types to their display handlers.
_EVENT_TYPE_HANDLERS: dict[str, _EventHandler] = {
    "assistant": _print_assistant_event,
    "tool_use": _print_tool_use_event,
    "system": _print_system_event,
    "result": _print_result_event,
}


def _print_event(event: dict[str, Any], pass_start_time: float) -> None:
    """Parse a stream-json event and dispatch to the appropriate printer."""
    event_type = event.get("type", "")
    printer = _EVENT_TYPE_HANDLERS.get(event_type)
    if printer is None:
        return
    elapsed_prefix = f"{DIM}[{_format_duration(time.time() - pass_start_time)}]{RESET} "
    printer(event, elapsed_prefix)


def _process_jsonl_buffer(
    output_buffer: bytearray,
    pass_start_time: float,
    debug: bool,
) -> bytearray:
    """Process complete JSONL lines from the buffer, return the remainder.

    Parses each complete line as JSON and dispatches to the appropriate
    event printer.  Incomplete trailing data is left in the buffer for
    the next call.

    Args:
        output_buffer: Mutable byte buffer containing raw subprocess output.
        pass_start_time: Wall-clock time when the current check started
            (used for elapsed-time prefixes).
        debug: If True, print lines that fail JSON parsing (raw output).

    Returns:
        The same *output_buffer* object, with consumed lines removed.
    """
    # Find the last complete line boundary. Everything before it can be parsed;
    # everything after stays in the buffer for the next call.
    # This single-delete approach avoids O(n²) cost from repeated del [:n].
    last_newline = output_buffer.rfind(b"\n")
    if last_newline == -1:
        return output_buffer  # no complete line yet
    complete_lines_bytes = bytes(output_buffer[:last_newline])
    del output_buffer[:last_newline + 1]
    for line_bytes in complete_lines_bytes.split(b"\n"):
        line_str = line_bytes.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue
        try:
            _print_event(json.loads(line_str), pass_start_time)
        except json.JSONDecodeError:
            if debug:
                print(f"{DIM}{line_str}{RESET}")
    return output_buffer


def _build_claude_command(prompt: str, skip_permissions: bool) -> list[str]:
    """Assemble the CLI command list for invoking Claude Code."""
    cmd = ["claude"]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd += ["-p", prompt, "--output-format", "stream-json", "--verbose"]
    return cmd


def _spawn_claude_process(
    cmd: list[str],
    workdir: str,
) -> subprocess.Popen[bytes]:
    """Launch the Claude subprocess in its own process group.

    Using a dedicated process group (via ``os.setsid``) ensures that
    ``_kill_process_group`` can terminate the claude process **and** any
    children it spawns (language servers, tool runners, etc.), preventing
    orphaned processes from accumulating memory across many checks.
    """
    logger.info("Spawning subprocess: %s (cwd=%s)", cmd[:3], workdir)
    try:
        return subprocess.Popen(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_SANITIZED_ENV,
            start_new_session=True,  # creates a new process group
        )
    except FileNotFoundError:
        _fatal(
            "Error: `claude` not found. Is Claude Code installed?\n"
            "  Install: npm install -g @anthropic-ai/claude-code"
        )
    except OSError as exc:
        _fatal(f"Failed to launch claude subprocess: {exc}")


def _read_stdout_chunk(stdout: IO[bytes]) -> bytes:
    """Read a chunk from stdout, preferring non-blocking read1 when available."""
    try:
        # BufferedReader.read1() returns available data without blocking for the
        # full chunk size. Fall back to os.read() for raw file descriptors.
        read1 = getattr(stdout, "read1", None)
        if read1 is not None:
            return read1(_READ_CHUNK_SIZE)
        return os.read(stdout.fileno(), _READ_CHUNK_SIZE)
    except OSError as exc:
        logger.debug("stdout read failed: %s", exc)
        return b""


def _drain_remaining_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    pass_start_time: float,
    debug: bool,
) -> bytearray:
    """Read all remaining data from stdout after the process has exited."""
    try:
        while True:
            remaining = os.read(stdout.fileno(), _DRAIN_CHUNK_SIZE)
            if not remaining:
                break
            output_buffer.extend(remaining)
            output_buffer = _process_jsonl_buffer(output_buffer, pass_start_time, debug)
    except OSError as exc:
        logger.debug("Failed to drain remaining stdout: %s", exc)
    return output_buffer


def _check_idle_timeout(
    last_output_time: float,
    idle_timeout: int,
    pass_start_time: float,
    process: subprocess.Popen[bytes],
) -> bool:
    """Return True and kill the process if it has been idle too long."""
    now = time.time()
    if now - last_output_time > idle_timeout:
        logger.warning("Idle timeout: pid=%d, idle=%.0fs, elapsed=%s",
                       process.pid, now - last_output_time,
                       _format_duration(now - pass_start_time))
        _print_status(f"\nIdle for {idle_timeout}s — killing "
                     f"(ran {_format_duration(now - pass_start_time)}).", RED)
        _kill_process_group(process)
        return True
    return False


def _flush_and_close_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    pass_start_time: float,
    debug: bool,
) -> None:
    """Flush any remaining partial line and close the stdout pipe."""
    # Append a newline to force any trailing incomplete JSONL line through the parser
    output_buffer.extend(b"\n")
    _process_jsonl_buffer(output_buffer, pass_start_time, debug)
    try:
        stdout.close()
    except OSError as exc:
        logger.debug("Failed to close stdout pipe: %s", exc)


def _stream_process_output(
    process: subprocess.Popen[bytes],
    idle_timeout: int,
    debug: bool,
) -> float:
    """Stream and display JSONL output from the Claude process.

    Kills the process if it produces no output for *idle_timeout* seconds.
    Returns the wall-clock start time used for elapsed-time display.
    """
    if process.stdout is None:
        logger.error("Subprocess stdout is None — cannot stream output (pid=%d)", process.pid)
        return time.time()
    stdout = process.stdout
    pass_start_time = time.time()
    last_output_time = pass_start_time  # idle timer starts from launch
    output_buffer = bytearray()

    try:
        while True:
            if _check_idle_timeout(last_output_time, idle_timeout, pass_start_time, process):
                break

            try:
                # 1s timeout lets us check idle timeout and process exit between reads
                ready, _, _ = select.select([stdout], [], [], 1.0)
            except (OSError, ValueError) as exc:
                logger.debug("select() failed (fd may be closed): %s", exc)
                break

            if not ready:
                if process.poll() is not None:
                    output_buffer = _drain_remaining_stdout(
                        stdout, output_buffer, pass_start_time, debug,
                    )
                    break
                continue

            chunk = _read_stdout_chunk(stdout)
            if not chunk:
                break

            last_output_time = time.time()
            output_buffer.extend(chunk)
            output_buffer = _process_jsonl_buffer(output_buffer, pass_start_time, debug)

    finally:
        _flush_and_close_stdout(stdout, output_buffer, pass_start_time, debug)

    return pass_start_time


def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    """Send a signal to a process group, ignoring errors if already gone."""
    try:
        os.killpg(pgid, sig)
    except OSError as exc:
        logger.debug("%s to pgid %d failed: %s", sig.name, pgid, exc)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process and its entire process group.

    Sends SIGTERM first (graceful), waits briefly, then SIGKILL if needed.
    This prevents orphaned child processes from leaking memory.
    """
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        return  # process already gone

    _signal_process_group(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=_PROCESS_WAIT_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass

    _signal_process_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=_PROCESS_WAIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("Process %d did not exit after SIGKILL", process.pid)


def run_claude(
    prompt: str,
    workdir: str,
    *,
    skip_permissions: bool = False,
    dry_run: bool = False,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    debug: bool = False,
) -> int:
    """Run a single Claude Code check.

    Uses ``--output-format stream-json`` so progress events stream in real time.
    There is no hard timeout — the process runs as long as it produces output.
    It is only killed after *idle_timeout* seconds of silence.

    Args:
        prompt: The check prompt to send to Claude Code.
        workdir: Absolute path to the project directory to check.
        skip_permissions: Pass ``--dangerously-skip-permissions`` to Claude Code.
        dry_run: If True, print what would run without invoking Claude.
        idle_timeout: Kill the subprocess after this many seconds of no output.
        debug: Show raw subprocess output lines that fail JSON parsing.

    Returns:
        The subprocess exit code (0 on success).
    """
    cmd = _build_claude_command(prompt, skip_permissions)
    logger.info("run_claude: workdir=%s, prompt_len=%d, skip_permissions=%s, idle_timeout=%d",
                workdir, len(prompt), skip_permissions, idle_timeout)
    _print_status(f"$ {' '.join(cmd[:3])} [prompt omitted for brevity]", DIM)

    if dry_run:
        _print_status(f"[DRY RUN] Would run in {workdir}:", YELLOW)
        truncated = prompt[:120] + ("..." if len(prompt) > 120 else "")
        print(f"  Prompt: {truncated}")
        return 0

    return _execute_claude_process(cmd, workdir, idle_timeout, debug)


def _execute_claude_process(
    cmd: list[str],
    workdir: str,
    idle_timeout: int,
    debug: bool,
) -> int:
    """Spawn the Claude subprocess, stream its output, and clean up.

    Returns the subprocess exit code (0 on success, -1 if it never set one).
    """
    process = _spawn_claude_process(cmd, workdir)
    try:
        pass_start_time = _stream_process_output(process, idle_timeout, debug)

        try:
            process.wait(timeout=idle_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("process.wait() timed out after %ds — killing group", idle_timeout)
    finally:
        # Ensure the entire process group is dead on every exit path.
        # Claude may spawn child processes (language servers, etc.) that
        # survive after the main process exits.
        _kill_process_group(process)

    return _report_check_exit_status(process, pass_start_time)


def _report_check_exit_status(process: subprocess.Popen[bytes], pass_start_time: float) -> int:
    """Log and display the exit status of a completed check.

    Args:
        process: The completed Claude subprocess.
        pass_start_time: Wall-clock time when the check started.

    Returns:
        The subprocess exit code (0 on success, -1 if never set).
    """
    elapsed = _format_duration(time.time() - pass_start_time)
    exit_code = process.returncode
    if exit_code is None:
        logger.warning("Process exited without a return code (may not have terminated cleanly)")
        exit_code = -1
    status_colour = GREEN if exit_code == 0 else YELLOW
    status_text = "completed" if exit_code == 0 else f"exited with code {exit_code}"
    logger.info("Check %s (exit_code=%d, elapsed=%s)", status_text, exit_code, elapsed)
    _print_status(f"  Check {status_text} in {elapsed}", status_colour)
    _log_memory_usage("after check")
    return exit_code


# --- CLI entry point ----------------------------------------------------------

def _build_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the CLI argument parser."""
    tier_names = ", ".join(TIERS)
    parser = argparse.ArgumentParser(
        description="Autonomous multi-check code review using Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Check tiers:",
            f"  basic       {', '.join(TIER_BASIC)}",
            f"  thorough    basic + {', '.join(p for p in TIER_THOROUGH if p not in TIER_BASIC)}",
            f"  exhaustive  thorough + {', '.join(p for p in TIER_EXHAUSTIVE if p not in TIER_THOROUGH)}",
            "",
            "All available checks (use with --checks to override tier):",
            *(f"  {p['id']:14s}  {p['label']}" for p in CHECKS),
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


def _print_run_summary(
    workdir: str,
    selected_checks: list[dict[str, str]],
    num_cycles: int,
    total_steps: int,
    idle_timeout: int,
    dry_run: bool,
    convergence_threshold: float = 0.0,
) -> None:
    """Print a summary of the configured check run before starting.

    Args:
        workdir: Resolved absolute path to the project directory.
        selected_checks: List of check dicts (each with "id", "label", "prompt").
        num_cycles: Maximum number of times to repeat the full check suite.
        total_steps: ``len(selected_checks) * num_cycles``.
        idle_timeout: Per-check idle timeout in seconds.
        dry_run: Whether this is a preview-only run.
        convergence_threshold: Stop cycling when change percentage falls below
            this value (0 disables convergence detection).
    """
    print(f"\n{BOLD}checkloop{RESET}")
    print(f"  Directory    : {workdir}")
    print(f"  Checks       : {', '.join(p['id'] for p in selected_checks)}")
    print(f"  Cycles       : {num_cycles} (max)")
    print(f"  Total steps  : {total_steps}  ({len(selected_checks)} checks x {num_cycles} cycle{'s' if num_cycles != 1 else ''}) max")
    print(f"  Idle timeout : {idle_timeout}s (no hard limit)")
    if convergence_threshold > 0:
        print(f"  Convergence  : stop when < {convergence_threshold}% of lines change")
    if dry_run:
        _print_status("  DRY RUN", YELLOW)


def _check_cycle_convergence(
    workdir: str,
    cycle: int,
    base_sha: str,
    convergence_threshold: float,
    prev_change_pct: float | None,
) -> tuple[bool, float | None]:
    """Commit changes and check whether the check loop has converged.

    Commits all staged changes, then compares the percentage of total
    tracked lines modified against *convergence_threshold*.  Also warns
    if the change percentage increased compared to the previous cycle
    (possible oscillation).

    Args:
        workdir: Resolved absolute path to the project directory.
        cycle: 1-based cycle number (for display and logging).
        base_sha: Git SHA recorded at the start of this cycle.
        convergence_threshold: Stop if change percentage is below this value.
        prev_change_pct: Change percentage from the previous cycle, or None
            if this is the first cycle.

    Returns:
        A ``(should_stop, change_pct)`` tuple.  *should_stop* is True when
        the loop should exit (either no changes or below threshold).
    """
    _git_commit_all(workdir, f"Review cycle {cycle} cleanup")
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

    Args:
        check: Dict with "id", "label", and "prompt" keys.
        workdir: Resolved absolute path to the project directory.
        args: Parsed CLI arguments (used for skip_permissions, dry_run, etc.).
        step_label: Display string like ``"[2/6] (cycle 1/3)"``.
        is_git: Whether *workdir* is a git repo (avoids redundant checks).

    Returns:
        True if the check made changes (or if change detection is unavailable).
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
    """Compare git state before/after a check, print stats, and return whether changes were made.

    Args:
        workdir: Resolved absolute path to the project directory.
        pass_id: Short identifier of the check (e.g. ``"readability"``).
        sha_before: Git HEAD SHA recorded before the check ran, or None if
            the project is not a git repo.

    Returns:
        True if the check made changes (or if not a git repo).
    """
    if sha_before is None:
        return True  # assume changes if not a git repo
    sha_after = _git_head_sha(workdir)
    if sha_after == sha_before:
        _print_status(f"  {pass_id}: no changes")
        return False
    lines_changed, pct = _compute_change_stats(workdir, sha_before)
    _print_status(f"  {pass_id}: {lines_changed} lines changed ({pct:.2f}% of codebase)")
    return True


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

    Args:
        selected_checks: Ordered list of check dicts to run each cycle.
        num_cycles: Maximum number of full cycles to execute.
        workdir: Resolved absolute path to the project directory.
        args: Parsed CLI arguments (pause, dry_run, idle_timeout, etc.).
        convergence_threshold: Stop when change percentage falls below this
            value.  Set to 0 to disable convergence detection.
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

        base_sha = _git_head_sha(workdir) if convergence_enabled else None
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

        if convergence_enabled and base_sha and not args.dry_run:
            converged, prev_change_pct = _check_cycle_convergence(
                workdir, cycle, base_sha, convergence_threshold, prev_change_pct,
            )
            if converged:
                break


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


def _build_permission_warning(skip_permissions: bool) -> tuple[str, str, list[str], str]:
    """Build the warning message components based on permission mode.

    Returns:
        A (colour, heading, body_lines, countdown) tuple.
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
    selected = [p for p in CHECKS if p["id"] in selected_ids]
    logger.info("Selected %d checks: %s", len(selected), [p["id"] for p in selected])
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

    Args:
        selected_checks: Ordered list of check dicts to run each cycle.
        num_cycles: Maximum number of full cycles.
        workdir: Resolved absolute path to the project directory.
        args: Parsed CLI arguments.
        convergence_threshold: Stop when change percentage falls below this.
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
        ", ".join(p["id"] for p in selected_checks),
        num_cycles,
        args.idle_timeout,
        convergence_threshold,
    )

    if not args.dry_run:
        _display_pre_run_warning(args.dangerously_skip_permissions)

    _run_suite_with_error_handling(selected_checks, num_cycles, workdir, args, convergence_threshold)


if __name__ == "__main__":  # pragma: no cover
    main()
