"""claudeloop — Autonomous multi-pass code review using Claude Code.

This package provides a CLI tool that orchestrates multiple focused review
passes over a codebase using the Claude Code CLI. Each pass targets a
specific quality dimension (readability, DRY, tests, security, etc.) so
the model can focus deeply on one concern at a time.

Public API:
    main()          — CLI entry point (also available as the ``claudeloop`` command).
    run_claude()    — Run a single Claude Code review pass programmatically.
    REVIEW_PASSES   — Ordered list of all available review pass definitions.
    PASS_IDS        — List of valid pass ID strings.
    TIERS           — Maps tier name to its list of pass IDs.
    TIER_BASIC      — Pass IDs for the basic review tier.
    TIER_THOROUGH   — Pass IDs for the thorough review tier.
    TIER_EXHAUSTIVE — Pass IDs for the exhaustive review tier.
"""

from claudeloop.cli import (
    PASS_IDS,
    REVIEW_PASSES,
    TIER_BASIC,
    TIER_EXHAUSTIVE,
    TIER_THOROUGH,
    TIERS,
    main,
    run_claude,
)

__all__ = [
    "PASS_IDS",
    "REVIEW_PASSES",
    "TIER_BASIC",
    "TIER_EXHAUSTIVE",
    "TIER_THOROUGH",
    "TIERS",
    "main",
    "run_claude",
]
