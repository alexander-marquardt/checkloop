"""checkloop — Autonomous multi-check code review using Claude Code.

This package provides a CLI tool that orchestrates multiple focused checks
over a codebase using the Claude Code CLI. Each check targets a specific
quality dimension (readability, DRY, tests, security, etc.) so the model
can focus deeply on one concern at a time.

Public API:
    main()          — CLI entry point (also available as the ``checkloop`` command).
    run_claude()    — Run a single Claude Code check programmatically.
    CHECKS          — Ordered list of all available check definitions.
    CHECK_IDS       — List of valid check ID strings.
    TIERS           — Maps tier name to its list of check IDs.
    TIER_BASIC      — Check IDs for the basic tier.
    TIER_THOROUGH   — Check IDs for the thorough tier.
    TIER_EXHAUSTIVE — Check IDs for the exhaustive tier.
"""

from checkloop.checks import (
    CHECK_IDS,
    CHECKS,
    CheckDef,
    TIER_BASIC,
    TIER_EXHAUSTIVE,
    TIER_THOROUGH,
    TIERS,
)
from checkloop.cli import main
from checkloop.process import run_claude

__all__ = [
    "CHECK_IDS",
    "CHECKS",
    "CheckDef",
    "TIER_BASIC",
    "TIER_EXHAUSTIVE",
    "TIER_THOROUGH",
    "TIERS",
    "main",
    "run_claude",
]
