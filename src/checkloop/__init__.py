"""checkloop — Autonomous multi-check code review using Claude Code.

This package provides a CLI tool that orchestrates multiple focused checks
over a codebase using the Claude Code CLI. Each check targets a specific
quality dimension (readability, DRY, tests, security, etc.) so the model
can focus deeply on one concern at a time.

Public API:
    main()          — CLI entry point (also available as the ``checkloop`` command).
    run_claude()    — Run a single Claude Code check programmatically.
    looks_dangerous() — Check if a prompt contains destructive keywords.
    CheckResult     — Return type of ``run_claude()``.
    CheckDef        — TypedDict describing a single check (id, label, prompt).
    CHECKS          — Ordered list of all available check definitions.
    CHECK_IDS       — List of valid check ID strings.
    TIERS           — Maps tier name to its list of check IDs.
    TIER_CONFIGS    — Maps tier name to its full ``TierConfig`` (including per-check models).
    DEFAULT_TIER    — Default tier name (``"basic"``).

Tier configuration:
    TierConfig      — Parsed tier config (name, description, checks with models).
    TierCheckEntry  — A single check entry in a tier (id, model).
    load_builtin_tier() — Load a built-in tier by name.
    load_tier_file()    — Load a custom tier from a TOML file.
    TIER_BASIC      — Check IDs for the basic tier.
    TIER_THOROUGH   — Check IDs for the thorough tier.
    TIER_EXHAUSTIVE — Check IDs for the exhaustive tier.

Kill-reason constants (possible values of ``CheckResult.kill_reason``):
    KILL_REASON_IDLE    — Subprocess produced no output for too long.
    KILL_REASON_TIMEOUT — Hard wall-clock timeout exceeded.
    KILL_REASON_MEMORY  — Child process tree RSS exceeded limit.

Default resource limits (match ``run_claude()`` keyword defaults):
    DEFAULT_IDLE_TIMEOUT          — Seconds before killing a silent subprocess (300).
    DEFAULT_CHECK_TIMEOUT         — Hard wall-clock timeout per check in seconds (0 = disabled).
    DEFAULT_MAX_MEMORY_MB         — Max child-tree RSS in MB before killing (8192).
    DEFAULT_PAUSE_SECONDS         — Seconds between consecutive checks (2).
    DEFAULT_CONVERGENCE_THRESHOLD — Percent of lines changed below which cycles stop (0.1).
"""

from checkloop.checks import (
    CHECK_IDS,
    CHECKS,
    CheckDef,
    DEFAULT_TIER,
    TIER_BASIC,
    TIER_CONFIGS,
    TIER_EXHAUSTIVE,
    TIER_THOROUGH,
    TIERS,
    looks_dangerous,
)
from checkloop.tier_config import (
    TierCheckEntry,
    TierConfig,
    load_builtin_tier,
    load_tier_file,
)
from checkloop.cli import main
from checkloop.cli_args import DEFAULT_CONVERGENCE_THRESHOLD, DEFAULT_PAUSE_SECONDS
from checkloop.process import (
    CheckResult,
    DEFAULT_CHECK_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_MEMORY_MB,
    KILL_REASON_IDLE,
    KILL_REASON_MEMORY,
    KILL_REASON_TIMEOUT,
    run_claude,
)

__all__ = [
    "CHECK_IDS",
    "CHECKS",
    "CheckDef",
    "CheckResult",
    "DEFAULT_CHECK_TIMEOUT",
    "DEFAULT_CONVERGENCE_THRESHOLD",
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_MAX_MEMORY_MB",
    "DEFAULT_PAUSE_SECONDS",
    "DEFAULT_TIER",
    "KILL_REASON_IDLE",
    "KILL_REASON_MEMORY",
    "KILL_REASON_TIMEOUT",
    "TIER_BASIC",
    "TIER_CONFIGS",
    "TIER_EXHAUSTIVE",
    "TIER_THOROUGH",
    "TierCheckEntry",
    "TierConfig",
    "TIERS",
    "load_builtin_tier",
    "load_tier_file",
    "looks_dangerous",
    "main",
    "run_claude",
]
