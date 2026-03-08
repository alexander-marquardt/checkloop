"""Terminal output helpers: ANSI colours, banners, status messages, and formatting."""

from __future__ import annotations

import logging
import math
import sys
from typing import NoReturn

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


def _print_banner(title: str, colour: str = CYAN) -> None:
    """Print a prominent section header with horizontal rules."""
    horizontal_rule = "\u2500" * RULE_WIDTH  # ─
    print(f"\n{colour}{BOLD}{horizontal_rule}")
    print(f"  {title}")
    print(f"{horizontal_rule}{RESET}\n")


def _print_status(msg: str, colour: str = DIM) -> None:
    """Print a coloured status message to the terminal."""
    print(f"{colour}{msg}{RESET}")


def _format_duration(total_seconds: float) -> str:
    """Format elapsed seconds into a compact ``XmYYs`` or ``XhYYmZZs`` string."""
    if math.isnan(total_seconds) or math.isinf(total_seconds):
        return "0m00s"
    minutes, seconds = divmod(max(0, int(total_seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


def _fatal(msg: str) -> NoReturn:
    """Log an error, print it in red, and exit with code 1."""
    logger.error("%s", msg)
    _print_status(msg, RED)
    sys.exit(1)
