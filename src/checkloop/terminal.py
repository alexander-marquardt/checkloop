"""Terminal output helpers: ANSI colours, banners, status messages, and formatting.

Provides ANSI escape-code constants (``BOLD``, ``CYAN``, ``RED``, etc.) and
small utility functions for printing coloured banners, status lines, and
human-readable durations.  All output goes to stdout.
"""

from __future__ import annotations

import logging
import math
import sys
from typing import NamedTuple, NoReturn, TypedDict

logger = logging.getLogger(__name__)

# --- ANSI colour codes --------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
BLUE   = "\033[94m"

RULE_WIDTH = 72  # character width for banner horizontal rules


def print_banner(title: str, colour: str = CYAN) -> None:
    """Print a prominent section header with horizontal rules."""
    horizontal_rule = "\u2500" * RULE_WIDTH  # ─
    print(f"\n{colour}{BOLD}{horizontal_rule}")
    print(f"  {title}")
    print(f"{horizontal_rule}{RESET}\n")


def print_status(msg: str, colour: str = DIM) -> None:
    """Print a coloured status message to the terminal."""
    print(f"{colour}{msg}{RESET}")


def format_duration(total_seconds: float) -> str:
    """Format elapsed seconds into a compact ``XmYYs`` or ``XhYYmZZs`` string."""
    if math.isnan(total_seconds) or math.isinf(total_seconds):
        return "0m00s"
    minutes, seconds = divmod(max(0, int(total_seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


def fatal(msg: str) -> NoReturn:
    """Log an error, print it in red, and exit with code 1."""
    logger.error("%s", msg)
    print_status(msg, RED)
    sys.exit(1)


class SummaryRow(TypedDict):
    """A single row in the post-run summary table.

    Attributes:
        check_id: Short identifier of the check (e.g. ``"readability"``).
        label: Human-readable check name shown in banners.
        cycle: Which cycle this check ran in (1-based).
        exit_code: Subprocess exit code (0 = success).
        kill_reason: One of the ``KILL_REASON_*`` constants if killed, else None.
        made_changes: Whether the check modified any tracked files.
        lines_changed: Total insertions + deletions, or None if unavailable.
        change_pct: Percentage of total tracked lines changed, or None.
        duration: Human-readable elapsed time string (e.g. ``"2m30s"``).
    """

    check_id: str
    label: str
    cycle: int
    exit_code: int
    kill_reason: str | None
    made_changes: bool
    lines_changed: int | None
    change_pct: float | None
    duration: str


class SummaryStats(NamedTuple):
    """Aggregate statistics computed from a list of SummaryRow dicts.

    Attributes:
        succeeded: Number of checks that exited with code 0.
        failed: Number of checks that exited with a non-zero code.
        killed: Number of checks terminated by a resource limit (timeout or memory).
        total_lines: Sum of lines changed across all checks.
        with_changes: Number of checks that modified at least one tracked file.
    """

    succeeded: int
    failed: int
    killed: int
    total_lines: int
    with_changes: int


def compute_summary_stats(results: list[SummaryRow]) -> SummaryStats:
    """Compute aggregate statistics from summary rows."""
    total = len(results)
    succeeded = sum(1 for r in results if r["exit_code"] == 0)
    failed = total - succeeded
    killed = sum(1 for r in results if r["kill_reason"] is not None)
    total_lines = sum(r["lines_changed"] or 0 for r in results)
    with_changes = sum(1 for r in results if r["made_changes"])
    return SummaryStats(succeeded, failed, killed, total_lines, with_changes)


def print_run_summary_table(
    results: list[SummaryRow],
    total_elapsed: str,
    stats: SummaryStats | None = None,
) -> None:
    """Print a post-run summary table showing per-check outcomes."""
    if not results:
        return

    print_banner("Run Summary", CYAN)

    if stats is None:
        stats = compute_summary_stats(results)
    total_checks = len(results)
    succeeded, failed, killed, total_lines, checks_with_changes = stats

    # Header
    print(f"  {'Check':<20s} {'Cy':>2s}  {'Exit':>4s}  {'Kill Reason':<14s}  {'Lines':>7s}  {'Duration':>8s}")
    print(f"  {'─' * 20} {'─' * 2}  {'─' * 4}  {'─' * 14}  {'─' * 7}  {'─' * 8}")

    for r in results:
        check_id = str(r["check_id"])[:20]
        cycle = str(r["cycle"])
        exit_code = str(r["exit_code"])
        kill = str(r["kill_reason"] or "—")[:14]
        lines = str(r["lines_changed"]) if r["lines_changed"] is not None else "—"
        duration = str(r["duration"])

        # Colour the row based on outcome
        if r["kill_reason"]:
            colour = RED
        elif r["exit_code"] != 0:
            colour = YELLOW
        elif r["made_changes"]:
            colour = GREEN
        else:
            colour = DIM

        print(f"  {colour}{check_id:<20s} {cycle:>2s}  {exit_code:>4s}  {kill:<14s}  {lines:>7s}  {duration:>8s}{RESET}")

    # Footer
    print()
    print(f"  Total checks : {total_checks}  ({succeeded} ok, {failed} failed, {killed} killed)")
    print(f"  Total lines  : {total_lines}")
    print(f"  With changes : {checks_with_changes}/{total_checks}")
    print(f"  Elapsed      : {total_elapsed}")
    print()
