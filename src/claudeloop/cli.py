#!/usr/bin/env python3
"""
claudeloop — Autonomous multi-pass code review using Claude Code.

Runs a configurable suite of review passes (readability, DRY, tests, security,
etc.) over an existing codebase. Point it at a directory and walk away.

Usage:
    claudeloop                            # review current directory (basic tier)
    claudeloop --dir ~/my-project         # review a specific directory
    claudeloop --cycles 3                 # repeat the full suite 3x
    claudeloop --passes readability dry tests
    claudeloop --all-passes --cycles 2
    claudeloop --dry-run                  # preview without running
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import resource
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
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


RULE_WIDTH = 72
DEFAULT_IDLE_TIMEOUT = 120
DEFAULT_PAUSE_SECONDS = 2
DEFAULT_CONVERGENCE_THRESHOLD = 0.1  # percent of total lines changed

COMMIT_MESSAGE_INSTRUCTIONS = (
    "\n\nIf you make any git commits, follow these commit message rules:\n"
    "- Maximum 5-10 lines\n"
    "- Do not mention Claude, AI, or any AI tools\n"
    "- Provide only a high-level summary of what was cleaned up, fixed, or changed\n"
    "- Use clear, professional commit message style"
)


def _fatal(msg: str) -> NoReturn:
    """Log an error, print it in red, and exit with code 1."""
    logger.error("%s", msg)
    print(f"{RED}{msg}{RESET}")
    sys.exit(1)


def _get_current_rss_mb() -> float:
    """Return the current RSS of this process in MB (not peak — actual current)."""
    try:
        pid = os.getpid()
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip()) / 1024  # ps reports in KB
    except (OSError, ValueError) as exc:
        logger.debug("ps-based RSS lookup failed: %s", exc)
    # Fallback: use resource (peak, not current — better than nothing)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return usage.ru_maxrss / scale


def _get_child_pids() -> list[int]:
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
    for cpid in (pids if pids is not None else _get_child_pids()):
        try:
            os.kill(cpid, signal.SIGKILL)
            killed += 1
            logger.warning("Killed orphaned child process %d", cpid)
        except OSError as exc:
            logger.debug("Could not kill child %d: %s", cpid, exc)
    return killed


def _log_memory_usage(label: str) -> None:
    """Log current RSS and child process count after each pass."""
    rss_mb = _get_current_rss_mb()
    child_pids = _get_child_pids()
    logger.info("Memory [%s]: rss=%.0fMB, children=%d", label, rss_mb, len(child_pids))
    print_status(f"  Memory: {rss_mb:.0f}MB RSS, {len(child_pids)} child processes", DIM)
    if child_pids:
        print(f"{YELLOW}  Warning: {len(child_pids)} child process(es) still alive — killing.{RESET}")
        # Pass pids directly to avoid a second pgrep subprocess spawn.
        killed = _kill_orphaned_children(child_pids)
        if killed:
            print_status(f"  Killed {killed} orphaned process(es).", YELLOW)


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
        logger.error("Failed to run git %s: %s", args[0] if args else "", exc)
        raise


def _is_git_repo(workdir: str) -> bool:
    """Return True if workdir is inside a git repository."""
    return _git_run(workdir, "rev-parse", "--is-inside-work-tree").returncode == 0


def _git_head_sha(workdir: str) -> str | None:
    """Return the current HEAD commit SHA, or None if unavailable."""
    result = _git_run(workdir, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def _git_commit_cycle(workdir: str, cycle: int) -> bool:
    """Stage and commit any uncommitted changes after a review cycle.

    Returns True if a commit was created (i.e. there were changes).
    """
    try:
        _git_run(workdir, "add", "-A", check=True)
        # Check for staged changes
        if _git_run(workdir, "diff", "--cached", "--quiet").returncode == 0:
            logger.debug("No staged changes after cycle %d — nothing to commit", cycle)
            return False  # nothing to commit
        _git_run(workdir, "commit", "-m", f"Review cycle {cycle} cleanup", check=True)
        logger.info("Committed changes after cycle %d", cycle)
        return True
    except subprocess.CalledProcessError as exc:
        logger.warning("Git commit failed after cycle %d: %s", cycle, exc)
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
    return insertions + deletions


def _count_file_lines(filepath: Path) -> int:
    """Count newlines in a text file, reading in chunks. Returns 0 for binary files."""
    with open(filepath, "rb") as fh:
        header = fh.read(8192)
        if b"\0" in header:
            return 0  # binary file
        total = header.count(b"\n")
        for chunk in iter(lambda: fh.read(65536), b""):
            total += chunk.count(b"\n")
        return total


def _count_tracked_lines(workdir: str) -> int:
    """Count total lines across all git-tracked text files.

    Reads files in small chunks to avoid loading large files entirely into
    memory, which matters for long-running sessions on big repos.
    """
    ls_result = _git_run(workdir, "ls-files", "-z", text=False)
    if ls_result.returncode != 0:
        return 1  # avoid division by zero
    files = [f.decode("utf-8", errors="replace") for f in ls_result.stdout.split(b"\0") if f]
    total = 0
    workdir_resolved = Path(workdir).resolve()
    for filename in files:
        try:
            filepath = (workdir_resolved / filename).resolve()
            if not filepath.is_relative_to(workdir_resolved):
                continue  # skip paths that escape the workdir (path traversal)
            total += _count_file_lines(filepath)
        except OSError as exc:
            logger.debug("Could not read tracked file %s: %s", filename, exc)
    return max(total, 1)


_cached_total_lines: dict[str, int] = {}


def _get_change_percentage(workdir: str, base_sha: str) -> float:
    """Return the percentage of total tracked lines that changed since *base_sha*."""
    result = _git_run(workdir, "diff", "--shortstat", base_sha, "HEAD")
    if result.returncode != 0:
        logger.warning("git diff --shortstat failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return 0.0
    lines_changed = _parse_shortstat(result.stdout)
    if lines_changed == 0:
        return 0.0
    # Cache total line count per workdir — it barely changes between cycles and
    # counting requires reading every tracked file, which is expensive on large repos.
    resolved = str(Path(workdir).resolve())
    if resolved not in _cached_total_lines:
        _cached_total_lines[resolved] = _count_tracked_lines(workdir)
    return (lines_changed / _cached_total_lines[resolved]) * 100


def print_banner(title: str, colour: str = CYAN) -> None:
    """Print a prominent section header with horizontal rules."""
    horizontal_rule = "\u2500" * RULE_WIDTH  # ─
    print(f"\n{colour}{BOLD}{horizontal_rule}")
    print(f"  {title}")
    print(f"{horizontal_rule}{RESET}\n")


def print_status(msg: str, colour: str = DIM) -> None:
    """Print a coloured status message to the terminal."""
    print(f"{colour}{msg}{RESET}")


# --- Review passes ------------------------------------------------------------

REVIEW_PASSES: list[dict[str, str]] = [
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
            "Review ALL code in this project (not just recently written code). "
            "Improve naming (variables, functions, classes) throughout. "
            "Break up any function that does more than one logical thing, "
            "or that requires scrolling to read in full. "
            "Prefer small, named functions where the name removes the need for a comment. "
            "Add or improve inline comments where logic is non-obvious, "
            "and ensure consistent formatting across the entire codebase. "
            "Do NOT change any behaviour — only improve clarity."
        ),
    },
    {
        "id": "dry",
        "label": "DRY / Eliminate Repetition",
        "prompt": (
            "Audit the entire codebase for repeated or near-repeated logic. "
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
            "Measure and improve test coverage across the ENTIRE codebase "
            "(not just recently written code). "
            "Cover: happy paths, edge cases, and error conditions for all modules. "
            "Use the testing framework already in the project (or pytest/jest if none). "
            "Target >=90% line coverage across the whole project. "
            "Run the test suite and fix any failures before finishing. "
            "Report the final coverage figure when done."
        ),
    },
    {
        "id": "docs",
        "label": "Documentation",
        "prompt": (
            "Add or improve documentation across the whole project: "
            "update (or create) a README section describing what was built, "
            "add docstrings/JSDoc to all public functions and classes, "
            "and document any non-obvious environment variables or config."
        ),
    },
    # --- Thorough tier ---
    {
        "id": "security",
        "label": "Security Review",
        "prompt": (
            "Do a security review of the entire codebase. "
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
            "Review the codebase for obvious performance issues: "
            "N+1 queries, missing indexes, unnecessary re-renders, "
            "blocking I/O that could be async, large allocations in loops. "
            "Fix anything significant and add a comment explaining the optimisation."
        ),
    },
    {
        "id": "errors",
        "label": "Error Handling",
        "prompt": (
            "Audit error handling across the entire codebase. "
            "Ensure all I/O operations, network calls, and parsing steps "
            "have proper try/except (or try/catch) with meaningful error messages. "
            "Add logging where it would help diagnose production issues."
        ),
    },
    {
        "id": "types",
        "label": "Type Safety",
        "prompt": (
            "Review the entire codebase for type safety issues. "
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
            "Audit the entire codebase for unhandled edge cases and boundary conditions. "
            "Look for: off-by-one errors, empty/null/undefined inputs, integer overflow, "
            "empty collections, zero-length strings, negative numbers where unsigned expected, "
            "concurrent modification, and Unicode/encoding edge cases. "
            "Fix any issues and add tests for the edge cases you find."
        ),
    },
    {
        "id": "complexity",
        "label": "Reduce Complexity",
        "prompt": (
            "Review the entire codebase for excessive complexity. "
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
            "Review the codebase for logging and observability gaps. "
            "Ensure all entry points (API routes, CLI commands, queue consumers) log "
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
            "Review the entire codebase for concurrency issues. "
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
            "Review all UI code (HTML, JSX, templates, components) for accessibility issues. "
            "Ensure: semantic HTML elements are used instead of generic divs/spans, "
            "all images have meaningful alt text, form inputs have associated labels, "
            "ARIA attributes are used correctly, keyboard navigation works, "
            "colour contrast meets WCAG AA standards, and focus management is correct. "
            "If the project has no UI code, report that and skip."
        ),
    },
    {
        "id": "api-design",
        "label": "API Design & Consistency",
        "prompt": (
            "Review all public APIs (REST endpoints, library interfaces, CLI commands, "
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
            "Run the FULL test suite (including any tests written or modified during earlier passes). "
            "If any tests fail, diagnose whether the failure is due to a bug in the source code "
            "or a bad test. Fix the root cause — prefer fixing source code over weakening tests. "
            "Re-run until all tests pass. "
            "Report the final test count and results."
        ),
    },
]

PASS_IDS: list[str] = [p["id"] for p in REVIEW_PASSES]

# --- Review tiers -------------------------------------------------------------

_BOOKEND_START: list[str] = ["test-fix"]
_BOOKEND_END: list[str] = ["test-validate"]
_CORE_BASIC: list[str] = ["readability", "dry", "tests", "docs"]
_CORE_THOROUGH: list[str] = ["security", "perf", "errors", "types"]
_CORE_EXHAUSTIVE: list[str] = ["edge-cases", "complexity", "deps", "logging", "concurrency", "accessibility", "api-design"]

TIER_BASIC: list[str] = _BOOKEND_START + _CORE_BASIC + _BOOKEND_END
TIER_THOROUGH: list[str] = _BOOKEND_START + _CORE_BASIC + _CORE_THOROUGH + _BOOKEND_END
TIER_EXHAUSTIVE: list[str] = PASS_IDS  # all passes (already ordered correctly)

TIERS: dict[str, list[str]] = {
    "basic": TIER_BASIC,
    "thorough": TIER_THOROUGH,
    "exhaustive": TIER_EXHAUSTIVE,
}
DEFAULT_TIER: str = "basic"

# --- Dangerous-prompt guard ---------------------------------------------------

_DANGER_KEYWORDS: list[str] = [
    "rm -rf /",
    "format",
    "wipe",
    "delete all",
    "drop database",
    "drop table",
    "truncate",
    ":(){:|:&};:",
    "sudo rm",
    "chmod 777 /",
    "/etc/passwd",
    "dd if=/dev/zero",
]


def _compile_danger_patterns() -> list[re.Pattern[str]]:
    """Pre-compile regex patterns for all danger keywords."""
    patterns: list[re.Pattern[str]] = []
    for keyword in _DANGER_KEYWORDS:
        escaped = re.escape(keyword)
        prefix = r"\b" if keyword[0].isalnum() else ""
        suffix = r"\b" if keyword[-1].isalnum() else ""
        patterns.append(re.compile(prefix + escaped + suffix, re.IGNORECASE))
    return patterns


# Perf: compile once at import time instead of rebuilding on every call.
_DANGER_PATTERNS: list[re.Pattern[str]] = _compile_danger_patterns()


def _looks_dangerous(text: str) -> bool:
    """Check if a prompt contains any destructive keyword.

    Uses word-boundary anchors (\\b) around alphanumeric edges so e.g.
    "reformat" does not match "format", while keywords containing special
    characters like "rm -rf /" or "/etc/passwd" are still detected.
    """
    return any(pattern.search(text) for pattern in _DANGER_PATTERNS)


# --- Claude runner ------------------------------------------------------------

def _format_duration(total_seconds: float) -> str:
    """Format elapsed seconds into a compact ``XmYYs`` or ``XhYYmZZs`` string."""
    minutes, seconds = divmod(max(0, int(total_seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


_FILE_PATH_TOOLS: set[str] = {"read", "read_file", "edit", "edit_file", "write", "write_file"}


def _summarise_tool_use(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Return a short human-readable summary for a tool-use event."""
    name = tool_name.lower()
    if name in _FILE_PATH_TOOLS and "file_path" in tool_input:
        return f" {tool_input['file_path']}"
    if name == "bash" and "command" in tool_input:
        command = tool_input["command"]
        return f" $ {command[:77]}..." if len(command) > 80 else f" $ {command}"
    if name == "glob" and "pattern" in tool_input:
        return f" {tool_input['pattern']}"
    if name == "grep" and "pattern" in tool_input:
        return f" /{tool_input['pattern']}/"
    return ""


def _print_event(event: dict[str, Any], pass_start_time: float) -> None:
    """Parse a stream-json event and print a human-readable progress line."""
    event_type = event.get("type", "")
    if not event_type:
        return
    elapsed_prefix = f"{DIM}[{_format_duration(time.time() - pass_start_time)}]{RESET} "

    if event_type == "assistant":
        text_blocks = [
            b.get("text", "")
            for b in event.get("message", {}).get("content", [])
            if b.get("type") == "text"
        ]
        for text in text_blocks:
            if text.strip():
                print(f"{elapsed_prefix}{text}")

    elif event_type == "tool_use":
        tool_name = event.get("tool", event.get("name", "unknown"))
        detail = _summarise_tool_use(tool_name, event.get("input") or {})
        print(f"{elapsed_prefix}{BLUE}[{tool_name}]{RESET}{detail}")

    elif event_type == "system":
        system_message = event.get("message", "")
        if system_message:
            print(f"{elapsed_prefix}{DIM}{system_message}{RESET}")

    elif event_type == "result":
        result_text = event.get("result", "")
        if result_text:
            print(f"\n{elapsed_prefix}{GREEN}--- Result ---{RESET}")
            print(result_text)


def _process_jsonl_buffer(
    line_buffer: bytearray,
    pass_start_time: float,
    verbose: bool,
) -> bytearray:
    """Process complete JSONL lines from the buffer, return the remainder."""
    while b"\n" in line_buffer:
        newline_pos = line_buffer.index(b"\n")
        line = bytes(line_buffer[:newline_pos])
        del line_buffer[:newline_pos + 1]
        line_str = line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue
        try:
            _print_event(json.loads(line_str), pass_start_time)
        except json.JSONDecodeError:
            if verbose:
                print(f"{DIM}{line_str}{RESET}")
    return line_buffer


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
    orphaned processes from accumulating memory across many passes.
    """
    # When claudeloop is invoked from within a Claude Code session, the CLAUDECODE
    # env var causes the child `claude` process to refuse to start. Strip it out.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    logger.debug("Spawning subprocess: %s (cwd=%s)", cmd[:3], workdir)
    try:
        return subprocess.Popen(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,  # creates a new process group
        )
    except FileNotFoundError:
        _fatal(
            "Error: `claude` not found. Is Claude Code installed?\n"
            "  Install: npm install -g @anthropic-ai/claude-code"
        )


def _read_stdout_chunk(stdout: IO[bytes]) -> bytes:
    """Read a chunk from stdout, preferring non-blocking read1 when available."""
    try:
        read1 = getattr(stdout, "read1", None)
        if read1 is not None:
            result: bytes = read1(8192)
            return result
        return os.read(stdout.fileno(), 8192)
    except OSError as exc:
        logger.debug("stdout read failed: %s", exc)
        return b""


def _drain_remaining_stdout(
    stdout: IO[bytes],
    output_buffer: bytearray,
    pass_start_time: float,
    verbose: bool,
) -> bytearray:
    """Read all remaining data from stdout after the process has exited."""
    try:
        while True:
            remaining = os.read(stdout.fileno(), 65536)
            if not remaining:
                break
            output_buffer.extend(remaining)
            output_buffer = _process_jsonl_buffer(output_buffer, pass_start_time, verbose)
    except OSError as exc:
        logger.debug("Failed to drain remaining stdout: %s", exc)
    return output_buffer


def _stream_process_output(
    process: subprocess.Popen[bytes],
    idle_timeout: int,
    verbose: bool,
) -> float:
    """Stream and display JSONL output from the Claude process.

    Kills the process if it produces no output for *idle_timeout* seconds.
    Returns the wall-clock start time used for elapsed-time display.
    """
    assert process.stdout is not None
    stdout = process.stdout
    pass_start_time = time.time()
    last_output_time = time.time()
    output_buffer = bytearray()

    try:
        while True:
            now = time.time()
            if now - last_output_time > idle_timeout:
                print(
                    f"\n{RED}Idle for {idle_timeout}s — killing "
                    f"(ran {_format_duration(now - pass_start_time)}).{RESET}"
                )
                _kill_process_group(process)
                break

            try:
                ready, _, _ = select.select([stdout], [], [], 1.0)
            except (OSError, ValueError) as exc:
                logger.debug("select() failed (fd may be closed): %s", exc)
                break

            if not ready:
                if process.poll() is not None:
                    output_buffer = _drain_remaining_stdout(
                        stdout, output_buffer, pass_start_time, verbose,
                    )
                    break
                continue

            chunk = _read_stdout_chunk(stdout)
            if not chunk:
                break

            last_output_time = time.time()
            output_buffer.extend(chunk)
            output_buffer = _process_jsonl_buffer(output_buffer, pass_start_time, verbose)

    finally:
        # Flush any remaining partial line by appending a newline
        output_buffer.extend(b"\n")
        _process_jsonl_buffer(output_buffer, pass_start_time, verbose)

        try:
            stdout.close()
        except OSError as exc:
            logger.debug("Failed to close stdout pipe: %s", exc)

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
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    _signal_process_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("Process %d did not exit after SIGKILL", process.pid)


def run_claude(
    prompt: str,
    workdir: str,
    *,
    skip_permissions: bool = False,
    dry_run: bool = False,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    verbose: bool = False,
) -> int:
    """Run a single Claude Code review pass.

    Uses ``--output-format stream-json`` so progress events stream in real time.
    There is no hard timeout — the process runs as long as it produces output.
    It is only killed after *idle_timeout* seconds of silence.
    """
    cmd = _build_claude_command(prompt, skip_permissions)
    print_status(f"$ {' '.join(cmd[:3])} [prompt omitted for brevity]", DIM)

    if dry_run:
        print(f"{YELLOW}[DRY RUN] Would run in {workdir}:{RESET}")
        truncated = prompt[:120] + ("..." if len(prompt) > 120 else "")
        print(f"  Prompt: {truncated}")
        return 0

    process = _spawn_claude_process(cmd, workdir)
    pass_start_time = _stream_process_output(process, idle_timeout, verbose)

    try:
        process.wait(timeout=idle_timeout)
    except subprocess.TimeoutExpired:
        logger.warning("process.wait() timed out after %ds — killing group", idle_timeout)
        _kill_process_group(process)

    # Safety net: ensure the entire process group is dead even on normal exit.
    # Claude may have spawned child processes (language servers, etc.) that
    # survive after the main process exits.
    _kill_process_group(process)

    elapsed = _format_duration(time.time() - pass_start_time)
    exit_code = process.returncode if process.returncode is not None else -1
    if process.returncode is None:
        logger.warning("Process exited without a return code (may not have terminated cleanly)")
    colour = GREEN if exit_code == 0 else YELLOW
    status = "completed" if exit_code == 0 else f"exited with code {exit_code}"
    logger.info("Pass %s (exit_code=%d, elapsed=%s)", status, exit_code, elapsed)
    print_status(f"  Pass {status} in {elapsed}", colour)
    _log_memory_usage(f"after pass")
    return exit_code


# --- CLI entry point ----------------------------------------------------------

def _build_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the CLI argument parser."""
    tier_names = ", ".join(TIERS)
    parser = argparse.ArgumentParser(
        description="Autonomous multi-pass code review using Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Review tiers:",
            f"  basic       {', '.join(TIER_BASIC)}",
            f"  thorough    basic + {', '.join(p for p in TIER_THOROUGH if p not in TIER_BASIC)}",
            f"  exhaustive  thorough + {', '.join(p for p in TIER_EXHAUSTIVE if p not in TIER_THOROUGH)}",
            "",
            "All available passes (use with --passes to override tier):",
            *(f"  {p['id']:14s}  {p['label']}" for p in REVIEW_PASSES),
            "",
            "Examples:",
            "  claudeloop                                  # basic tier (default)",
            "  claudeloop --level thorough                 # thorough tier",
            "  claudeloop --level exhaustive --cycles 2    # exhaustive, repeat 2x",
            "  claudeloop --passes readability security    # manual override",
            "  claudeloop --all-passes                     # same as --level exhaustive",
            "  claudeloop --dry-run",
        ]),
    )

    parser.add_argument(
        "--dir", "-d", default=".",
        help="Project directory to review (default: current directory)",
    )
    parser.add_argument(
        "--level", "-l", choices=list(TIERS), default=None,
        metavar="TIER",
        help=f"Review depth: {tier_names} (default: basic)",
    )
    parser.add_argument(
        "--passes", nargs="+", choices=PASS_IDS, default=None,
        metavar="PASS",
        help="Manually select passes (overrides --level)",
    )
    parser.add_argument(
        "--all-passes", action="store_true",
        help="Run every available review pass (same as --level exhaustive)",
    )
    parser.add_argument(
        "--cycles", "-c", type=int, default=1, metavar="N",
        help="Repeat the full suite N times (default: 1)",
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT, metavar="SECS",
        help=f"Kill a pass after this many seconds of silence (default: {DEFAULT_IDLE_TIMEOUT})",
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
        "--pause", type=int, default=DEFAULT_PAUSE_SECONDS,
        help=f"Seconds to pause between passes (default: {DEFAULT_PAUSE_SECONDS})",
    )
    parser.add_argument(
        "--dangerously-skip-permissions", action="store_true",
        help="Pass --dangerously-skip-permissions to Claude Code (bypasses all permission checks)",
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
    selected_passes: list[dict[str, str]],
    num_cycles: int,
    total_steps: int,
    idle_timeout: int,
    dry_run: bool,
    convergence_threshold: float = 0.0,
) -> None:
    """Print a summary of the configured review run before starting."""
    print(f"\n{BOLD}claudeloop{RESET}")
    print(f"  Directory    : {workdir}")
    print(f"  Passes       : {', '.join(p['id'] for p in selected_passes)}")
    print(f"  Cycles       : {num_cycles} (max)")
    print(f"  Total steps  : {total_steps}  ({len(selected_passes)} passes x {num_cycles} cycle{'s' if num_cycles != 1 else ''}) max")
    print(f"  Idle timeout : {idle_timeout}s (no hard limit)")
    if convergence_threshold > 0:
        print(f"  Convergence  : stop when < {convergence_threshold}% of lines change")
    if dry_run:
        print(f"  {YELLOW}DRY RUN{RESET}")


def _check_cycle_convergence(
    workdir: str,
    cycle: int,
    base_sha: str,
    convergence_threshold: float,
    prev_change_pct: float | None,
) -> tuple[bool, float | None]:
    """Commit changes and check whether the review loop has converged.

    Returns (should_stop, updated_prev_change_pct).
    """
    _git_commit_cycle(workdir, cycle)
    current_sha = _git_head_sha(workdir)

    if current_sha == base_sha:
        logger.info("Cycle %d: no changes detected — converged", cycle)
        print(f"\n{GREEN}No changes in cycle {cycle} — converged.{RESET}")
        return True, prev_change_pct

    change_pct = _get_change_percentage(workdir, base_sha)
    print(f"\n{DIM}Cycle {cycle}: {change_pct:.2f}% of lines changed "
          f"(threshold: {convergence_threshold}%){RESET}")

    if prev_change_pct is not None and change_pct > prev_change_pct:
        print(f"{YELLOW}Warning: changes increased ({prev_change_pct:.2f}% -> {change_pct:.2f}%) — "
              f"possible oscillation.{RESET}")

    if change_pct < convergence_threshold:
        logger.info("Cycle %d: converged at %.2f%% (threshold: %.2f%%)", cycle, change_pct, convergence_threshold)
        print(f"{GREEN}Converged at {change_pct:.2f}% (below {convergence_threshold}% threshold).{RESET}")
        return True, change_pct

    logger.info("Cycle %d: %.2f%% lines changed (threshold: %.2f%%), continuing", cycle, change_pct, convergence_threshold)
    return False, change_pct


def _run_single_pass(
    review_pass: dict[str, str],
    workdir: str,
    args: argparse.Namespace,
    step_label: str,
) -> None:
    """Execute a single review pass, skipping if the prompt looks dangerous."""
    logger.info("Pass started: id=%s, label=%s, step=%s", review_pass["id"], review_pass["label"], step_label)
    print_banner(f"{step_label} {review_pass['label']}", CYAN)

    prompt = review_pass["prompt"] + COMMIT_MESSAGE_INSTRUCTIONS

    if _looks_dangerous(prompt):
        logger.warning("Skipping pass '%s' — dangerous keywords detected in prompt", review_pass["id"])
        print(f"{YELLOW}Skipping '{review_pass['id']}' — dangerous keywords detected.{RESET}")
        return

    exit_code = run_claude(
        prompt,
        workdir,
        skip_permissions=args.dangerously_skip_permissions,
        dry_run=args.dry_run,
        idle_timeout=args.idle_timeout,
        verbose=getattr(args, "debug", False),
    )
    if exit_code != 0:
        logger.warning("Pass '%s' exited with code %d", review_pass["id"], exit_code)
        print(f"{YELLOW}Pass '{review_pass['id']}' exited with code {exit_code}. Continuing...{RESET}")


def _run_review_suite(
    selected_passes: list[dict[str, str]],
    num_cycles: int,
    workdir: str,
    args: argparse.Namespace,
    convergence_threshold: float = 0.0,
) -> None:
    """Execute all review passes across all cycles.

    When *convergence_threshold* > 0 and the project is a git repo, a commit
    is created after each cycle and the percentage of lines changed is compared
    to the threshold.  If changes fall below the threshold the loop stops early.
    """
    total_steps = len(selected_passes) * num_cycles
    current_step = 0
    use_convergence = convergence_threshold > 0 and _is_git_repo(workdir)
    prev_change_pct: float | None = None

    for cycle in range(1, num_cycles + 1):
        logger.info("Cycle %d/%d started", cycle, num_cycles)
        if num_cycles > 1:
            print(f"\n{BOLD}{CYAN}===  Cycle {cycle}/{num_cycles}  ==={RESET}")

        base_sha = _git_head_sha(workdir) if use_convergence else None

        for review_pass in selected_passes:
            time.sleep(args.pause)
            current_step += 1
            cycle_label = f" (cycle {cycle}/{num_cycles})" if num_cycles > 1 else ""
            step_label = f"[{current_step}/{total_steps}]{cycle_label}"
            _run_single_pass(review_pass, workdir, args, step_label)

        if use_convergence and base_sha and not args.dry_run:
            converged, prev_change_pct = _check_cycle_convergence(
                workdir, cycle, base_sha, convergence_threshold, prev_change_pct,
            )
            if converged:
                break


def _display_pre_run_warning(skip_permissions: bool) -> None:
    """Show a warning about permissions and wait 5 seconds for the user to abort."""
    if skip_permissions:
        colour = RED
        heading = "WARNING: --dangerously-skip-permissions is ENABLED"
        body_lines = [
            "Claude Code will execute ALL actions without asking for approval.",
            "This includes writing files, running shell commands, and deleting code.",
            "Make sure you have committed or backed up your work before proceeding.",
        ]
        countdown = "Starting in 5 seconds (Ctrl+C to abort)..."
    else:
        colour = YELLOW
        heading = "WARNING: Running without --dangerously-skip-permissions"
        body_lines = [
            "Claude Code requires interactive permission prompts to write files,",
            "but claudeloop cannot relay those prompts (stdin is disconnected).",
            "Passes that modify code will likely FAIL or HANG.",
            "",
            "Re-run with:",
            f"  {BOLD}claudeloop --dangerously-skip-permissions ...{RESET}{colour}",
        ]
        countdown = "Continuing anyway in 5 seconds (Ctrl+C to abort)..."

    print(f"\n{colour}{BOLD}{'=' * RULE_WIDTH}")
    print(f"  {heading}")
    print(f"{'=' * RULE_WIDTH}{RESET}")
    for line in body_lines:
        print(f"{colour}  {line}{RESET}")
    print(f"\n{colour}  {countdown}{RESET}")

    try:
        time.sleep(5)
    except KeyboardInterrupt:
        print(f"\n{DIM}Aborted.{RESET}")
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


def _resolve_selected_passes(args: argparse.Namespace) -> list[dict[str, str]]:
    """Determine which review passes to run based on CLI arguments."""
    if args.all_passes:
        selected_ids = set(PASS_IDS)
    elif args.passes:
        selected_ids = set(args.passes)
    else:
        selected_ids = set(TIERS[args.level or DEFAULT_TIER])
    return [p for p in REVIEW_PASSES if p["id"] in selected_ids]


def main() -> None:
    """CLI entry point: parse arguments and run the configured review suite."""
    args = _build_argument_parser().parse_args()

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

    workdir = _resolve_working_directory(args.dir)
    _validate_arguments(args)

    selected_passes = _resolve_selected_passes(args)
    if not selected_passes:
        _fatal("No review passes selected. Check your --passes or --level arguments.")
    num_cycles = args.cycles
    total_steps = len(selected_passes) * num_cycles
    convergence_threshold = args.converged_at_percentage

    _print_run_summary(workdir, selected_passes, num_cycles, total_steps, args.idle_timeout, args.dry_run, convergence_threshold)

    logger.info(
        "Suite started: workdir=%s, passes=[%s], cycles=%d, idle_timeout=%d, convergence=%.2f%%",
        workdir,
        ", ".join(p["id"] for p in selected_passes),
        num_cycles,
        args.idle_timeout,
        convergence_threshold,
    )

    if not args.dry_run:
        _display_pre_run_warning(args.dangerously_skip_permissions)

    suite_start_time = time.time()
    try:
        _run_review_suite(selected_passes, num_cycles, workdir, args, convergence_threshold)
    except KeyboardInterrupt:
        elapsed = _format_duration(time.time() - suite_start_time)
        print(f"\n{YELLOW}Interrupted after {elapsed}. Partial results may have been applied.{RESET}")
        sys.exit(130)
    except FileNotFoundError as exc:
        logger.error("Required external tool not found: %s", exc)
        _fatal(f"Required tool not found: {exc}. Ensure git and claude are installed.")
    except Exception:
        logger.exception("Unexpected error during review suite")
        elapsed = _format_duration(time.time() - suite_start_time)
        print(f"\n{RED}Unexpected error after {elapsed}. Partial results may have been applied.{RESET}")
        raise
    suite_elapsed = _format_duration(time.time() - suite_start_time)
    logger.info("Suite completed: elapsed=%s", suite_elapsed)
    print_banner(f"All done! ({suite_elapsed} total)", GREEN)


if __name__ == "__main__":
    main()
